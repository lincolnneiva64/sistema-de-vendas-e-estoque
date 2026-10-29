import hashlib
import secrets
from uuid import UUID
from datetime import timedelta
from decimal import Decimal

from django.conf import settings
from django.core.exceptions import PermissionDenied
from django.db import connection, transaction
from django.utils import timezone

from estoque.models import Fornecedor, Funcionario, Produto, ProdutoFornecedor
from .conversao import cadastro_compativel, converter, decimal_entrada, snapshot
from .conversao_fisica import cadastro_contagem, auditoria as auditoria_fisica, exigir_representavel
from .erros import ErroAcesso
from .selecao import itens_incluidos, rodadas_incluidas
from .rastreabilidade import ORIGENS
from .models import (Inventario, ItemInventario, RodadaContagem, AcessoInventario, AtribuicaoContagem,
                     SequenciaEstoque, MovimentoObservado, ConciliacaoMovimento, DecisaoAjuste, EventoInventario, SinalizacaoFisica, ConfirmacaoContagem)


def permitir(usuario, permissao):
    if not usuario or not usuario.is_authenticated or not usuario.has_perm("inventario." + permissao):
        raise PermissionDenied("Permissão de inventário necessária.")


def evento(inventario, acao, usuario=None, rodada=None, acesso=None, **dados):
    return EventoInventario.objects.create(inventario=inventario, acao=acao, usuario=usuario,
                                           rodada=rodada, acesso=acesso, dados=dados)


def bloquear_inventario_acesso(acesso_id):
    inventario_id = AcessoInventario.objects.values_list("inventario_id", flat=True).get(pk=acesso_id)
    inventario = Inventario.objects.select_for_update().get(pk=inventario_id)
    exigir_nao_cancelada(inventario)
    acesso = AcessoInventario.objects.select_for_update().get(pk=acesso_id)
    return inventario, acesso


def exigir_nao_cancelada(inventario):
    if inventario.cancelada_em:
        raise PermissionDenied("Conferência cancelada. Disponível apenas para consulta.")


def validar_escrita(inventario, acesso, token_hash=None):
    exigir_nao_cancelada(inventario)
    validar_acesso(acesso)
    if token_hash is not None and not secrets.compare_digest(acesso.token_hash, token_hash):
        raise ErroAcesso("credencial_renovada", "Acesso renovado. Abra o novo link em outra aba deste navegador e volte para verificar o acesso nesta tela.")
    if acesso.ciclo != inventario.ciclo:
        raise PermissionDenied("Este acesso pertence a um ciclo anterior. Solicite um novo link.")
    if inventario.finalizada_em:
        raise ValueError("Conferência finalizada e enviada. Somente o responsável pode reabrir.")
    if acesso.enviada_em:
        raise ValueError("Sua atribuição já foi enviada. Solicite recontagem ao responsável.")


def atualizar_versao(inventario):
    inventario.versao += 1
    inventario.save(update_fields=["versao"])


def produtos_disponiveis_para_inventario():
    return Produto.objects.filter(ativo=True, excluido=False).exclude(
        pk__in=getattr(settings, "INVENTARIO_PRODUTOS_EXCLUIDOS_PILOTO", ()),
    )


@transaction.atomic
def criar_inventario(usuario, titulo=None, criterio=None, selecionados=None):
    permitir(usuario, "gerenciar_inventario")
    # Keep the legacy argument for callers, but never trust a client-supplied title.
    produtos = produtos_disponiveis_para_inventario()
    if criterio == "categoria":
        categoria = str(selecionados or "").strip()
        if not categoria:
            raise ValueError("Selecione uma categoria.")
        produtos = produtos.filter(categoria=categoria)
        filtro = {"categoria": categoria}
        descricao = categoria
    elif criterio == "fornecedor":
        try:
            fornecedor_id = int(selecionados)
        except (TypeError, ValueError):
            raise ValueError("Selecione um fornecedor válido.")
        fornecedor = Fornecedor.objects.get(pk=fornecedor_id)
        produtos = produtos.filter(pk__in=ProdutoFornecedor.objects.filter(fornecedor=fornecedor, ativo=True).values("produto_id"))
        filtro = {"fornecedor_id": str(fornecedor.pk)}
        descricao = fornecedor.nome.strip()
    elif criterio == "manual":
        try:
            ids = {int(pk) for pk in selecionados}
        except (TypeError, ValueError):
            raise ValueError("Produtos inválidos.")
        produtos = produtos.filter(pk__in=ids)
        if produtos.count() != len(ids):
            raise ValueError("Há produtos indisponíveis na seleção.")
        filtro = {"produto_ids": sorted(ids)}
        descricao = "Produtos selecionados"
    else:
        raise ValueError("Critério inválido.")
    produtos = list(produtos.select_for_update().order_by("pk"))
    if not produtos:
        raise ValueError("Nenhum produto disponível para esse critério.")
    if not descricao:
        raise ValueError("A seleção não possui nome válido para a conferência.")
    inventario = Inventario.objects.create(titulo="Conferência", criterio=criterio, filtro=filtro, criado_por=usuario)
    inventario.titulo = f"Conferência nº {inventario.pk} — {descricao}"
    if len(inventario.titulo) > Inventario._meta.get_field('titulo').max_length:
        raise ValueError("O nome selecionado excede o limite do título da conferência (160 caracteres).")
    inventario.save(update_fields=['titulo'])
    for produto in produtos:
        item = ItemInventario.objects.create(inventario=inventario, produto=produto, cadastro=snapshot(produto), custo_base=produto.preco_compra)
        RodadaContagem.objects.create(item=item, numero=1)
    evento(inventario, "criado", usuario, quantidade=len(produtos))
    return inventario


@transaction.atomic
def distribuir_participantes(usuario, inventario_id, rodada_ids, funcionarios, horas, versao):
    """Save the single participant list before creating any employee links."""
    from .selecao import salvar_selecao
    permitir(usuario, "gerenciar_inventario")
    inventario = Inventario.objects.select_for_update().get(pk=inventario_id)
    exigir_nao_cancelada(inventario)
    try:
        versao = int(versao)
    except (TypeError, ValueError):
        raise ValueError("Atualize a página antes de distribuir os produtos.")
    if inventario.versao != versao:
        raise ValueError("A conferência mudou. Atualize a página antes de distribuir os produtos.")
    if inventario.finalizada_em:
        raise ValueError("Conferência finalizada. A seleção histórica será preservada.")
    try:
        ids = {int(pk) for pk in rodada_ids}
    except (TypeError, ValueError):
        raise ValueError("Seleção de participantes inválida.")
    rodadas = list(rodadas_incluidas(inventario).filter(
        encerrada_em__isnull=True, pk__in=ids).select_related("atribuicao"))
    if not ids:
        raise ValueError("Selecione pelo menos um produto disponível para atribuir.")
    if len(rodadas) != len(ids):
        raise ValueError("Selecione produtos participantes disponíveis nesta conferência.")
    grupos = {}
    for rodada in rodadas:
        if getattr(rodada, "atribuicao", None) is not None:
            continue  # Existing assignments and their links are immutable here.
        try:
            funcionario_id = int(funcionarios.get(str(rodada.pk), ""))
        except (TypeError, ValueError):
            raise ValueError("Escolha o funcionário de cada produto participante.")
        grupos.setdefault(funcionario_id, []).append(rodada.pk)
    # Removal checks protect all existing assignments, drafts, OKs and history.
    # Any failure in a later employee group rolls back this selection as well.
    salvar_selecao(usuario, inventario.pk, [r.item_id for r in rodadas],
        inventario.versao, "Seleção de participantes confirmada pelo responsável.")
    gerados = [gerar_acesso(usuario, inventario.pk, funcionario_id, ids_grupo, horas)
               for funcionario_id, ids_grupo in sorted(grupos.items())]
    inventario.refresh_from_db()
    atualizar_versao(inventario)
    evento(inventario, "distribuicao_confirmada", usuario,
        participantes=sorted(ids), acessos=[a.pk for a, _ in gerados], ciclo=inventario.ciclo)
    return gerados


@transaction.atomic
def distribuir_produtos(usuario, inventario_id, funcionario_id, rodada_ids, horas, versao):
    """Single-employee adapter, preserving already assigned participants."""
    ids = list(rodada_ids)
    existentes = list(rodadas_incluidas().filter(item__inventario_id=inventario_id,
        encerrada_em__isnull=True, atribuicao__isnull=False).values_list("pk", flat=True))
    gerados = distribuir_participantes(usuario, inventario_id, ids + existentes,
        {str(pk): funcionario_id for pk in ids}, horas, versao)
    if not gerados:
        raise ValueError("Nenhum novo produto para atribuir.")
    return gerados[0]


@transaction.atomic
def gerar_acesso(usuario, inventario_id, funcionario_id, rodada_ids, horas=24):
    permitir(usuario, "gerenciar_inventario")
    inventario = Inventario.objects.select_for_update().get(pk=inventario_id)
    exigir_nao_cancelada(inventario)
    if inventario.finalizada_em:
        raise ValueError("Reabra a conferência antes de distribuir novas contagens.")
    funcionario = Funcionario.objects.get(pk=funcionario_id, ativo=True)
    try:
        ids = {int(pk) for pk in rodada_ids}
    except (TypeError, ValueError):
        raise ValueError("Seleção de produtos inválida.")
    rodadas = list(rodadas_incluidas(inventario).select_for_update().filter(pk__in=ids,
                    encerrada_em__isnull=True).order_by("pk"))
    if not ids:
        raise ValueError("Selecione pelo menos um produto disponível para atribuir.")
    if len(rodadas) != len(ids):
        raise ValueError("Há produtos indisponíveis na seleção. Atualize a página.")
    try:
        horas = int(horas)
    except (TypeError, ValueError):
        raise ValueError("Informe uma validade inteira entre 1 e 168 horas.")
    if not 1 <= horas <= 168:
        raise ValueError("Informe uma validade inteira entre 1 e 168 horas.")
    if AtribuicaoContagem.objects.filter(rodada__in=rodadas).exists():
        raise ValueError("Contagem já atribuída. Reabra em nova rodada para redistribuir.")
    token = secrets.token_urlsafe(32)
    acesso = AcessoInventario.objects.create(inventario=inventario, funcionario=funcionario,
        funcionario_nome=funcionario.nome, token_hash=hashlib.sha256(token.encode()).hexdigest(),
        expira_em=timezone.now() + timedelta(hours=int(horas)), criado_por=usuario, ciclo=inventario.ciclo)
    AtribuicaoContagem.objects.bulk_create([AtribuicaoContagem(rodada=r, acesso=acesso) for r in rodadas])
    evento(inventario, "acesso_criado", usuario, acesso=acesso, rodadas=sorted(ids))
    return acesso, token


def acesso_token(token):
    if len(token) > 100:
        raise PermissionDenied("Acesso indisponível.")
    acesso = AcessoInventario.objects.filter(token_hash=hashlib.sha256(token.encode()).hexdigest()).first()
    validar_acesso(acesso)
    return acesso


def validar_acesso(acesso):
    if not acesso:
        raise ErroAcesso("acesso_indisponivel", "Acesso indisponível. Solicite orientação ao responsável.")
    if acesso.revogado_em:
        raise ErroAcesso("acesso_revogado", "Acesso revogado. Solicite um novo acesso ao responsável.")
    if acesso.expira_em <= timezone.now():
        raise ErroAcesso("acesso_expirado", "Acesso expirado. Solicite um novo link ao responsável.")
    if not Funcionario.objects.filter(pk=acesso.funcionario_id, ativo=True).exists():
        raise ErroAcesso("funcionario_inativo", "Funcionário inativo. Solicite orientação ao responsável.")


@transaction.atomic
def revogar_acesso(usuario, acesso_id):
    permitir(usuario, "gerenciar_inventario")
    inventario, acesso = bloquear_inventario_acesso(acesso_id)
    if acesso.revogado_em is None:
        acesso.revogado_em = timezone.now()
        acesso.save(update_fields=["revogado_em"])
        evento(acesso.inventario, "acesso_revogado", usuario, acesso=acesso)


@transaction.atomic
def renovar_acesso(usuario, acesso_id):
    permitir(usuario, "gerenciar_inventario")
    inventario, acesso = bloquear_inventario_acesso(acesso_id)
    if inventario.finalizada_em or acesso.ciclo != inventario.ciclo:
        raise ValueError("Acesso encerrado para escrita. Distribua a nova rodada em um novo link.")
    if not acesso.funcionario.ativo:
        raise ValueError("Funcionário inativo.")
    token = secrets.token_urlsafe(32)
    acesso.token_hash = hashlib.sha256(token.encode()).hexdigest()
    acesso.expira_em = timezone.now() + timedelta(hours=24)
    acesso.revogado_em = None
    acesso.save(update_fields=["token_hash", "expira_em", "revogado_em"])
    evento(acesso.inventario, "acesso_renovado", usuario, acesso=acesso)
    return acesso, token


def sequencia(produto_id):
    return SequenciaEstoque.objects.filter(produto_id=produto_id).values_list("sequencia", flat=True).first() or 0


def verificar_observador():
    with connection.cursor() as cursor:
        if connection.vendor == "sqlite":
            cursor.execute("SELECT count(*) FROM sqlite_master WHERE type='trigger' AND name='inventario_observar_quantidade'")
        elif connection.vendor == "postgresql":
            cursor.execute("SELECT count(*) FROM pg_trigger WHERE tgname='inventario_observar_quantidade' AND tgrelid='estoque_produto'::regclass AND NOT tgisinternal AND tgenabled IN ('O','A')")
        else:
            raise ValueError("Observador de estoque indisponível neste banco.")
        if cursor.fetchone()[0] != 1:
            raise ValueError("Observador de estoque ausente. Confirmação e ajuste bloqueados.")


@transaction.atomic
def salvar_contagem(acesso_id, rodada_id, versao, embalagens, avulsos, confirmar=False, *, token_hash=None):
    # Global lock serializes editing/finalization/reopening. Product lock still
    # serializes each OK with commercial writers; no stock write is performed.
    inventario, acesso = bloquear_inventario_acesso(acesso_id)
    validar_escrita(inventario, acesso, token_hash)
    atribuicao = AtribuicaoContagem.objects.filter(acesso=acesso, rodada_id=rodada_id).first()
    if not atribuicao:
        raise PermissionDenied("Produto fora desta atribuição.")
    rodada = RodadaContagem.objects.select_for_update().select_related("item").get(pk=rodada_id)
    from .contagem_admin import confirmado
    if confirmado(rodada):
        raise PermissionDenied("Contagem confirmada pelo administrador. Solicite revisão ao responsável.")
    if rodada.encerrada_em:
        raise ValueError("Rodada encerrada. Atualize a página.")
    if DecisaoAjuste.objects.filter(item=rodada.item).exists():
        raise ValueError("Item com decisão de ajuste. Crie outra conferência.")
    try:
        versao = int(versao)
    except (TypeError, ValueError):
        raise ValueError("Versão da contagem inválida. Atualize a página.")
    anterior = rodada.confirmacoes.filter(versao_entrada=versao).first() if confirmar else None
    if anterior:
        valores = converter(cadastro_contagem(rodada.item), embalagens, avulsos)
        if valores[:2] == (anterior.embalagens, anterior.avulsos):
            # Retry of an old accepted request returns current state, never
            # overwrites a newer correction or advances its temporal reference.
            return rodada
        raise ValueError("Esta versão já foi confirmada com outros números. Atualize a página.")
    if versao != rodada.versao:
        raise ValueError("Esta contagem foi atualizada em outra aba. Atualize a página.")
    if confirmar:
        valores = converter(cadastro_contagem(rodada.item), embalagens, avulsos)
    else:
        principal, avulso = decimal_entrada(embalagens), decimal_entrada(avulsos)
        valores = converter(cadastro_contagem(rodada.item), principal if principal is not None else 0,
                           avulso if avulso is not None else 0)
        incompleta = principal is None or (rodada.item.cadastro["fracionado"] and avulso is None)
        valores = (principal, avulso, None if incompleta else valores[2], None if incompleta else valores[3])
    rodada.embalagens, rodada.avulsos, rodada.contado, rodada.contado_base = valores
    if confirmar:
        verificar_observador()
        produto = Produto.objects.select_for_update().get(pk=rodada.item.produto_id)
        validar_escrita(inventario, acesso, token_hash)  # May have waited for a sale past token expiry.
        if not produto.ativo or produto.excluido or produto.quantidade is None:
            raise ValueError("Produto indisponível ou saldo sem referência. Solicite revisão.")
        if not cadastro_compativel(produto, rodada.item.cadastro):
            raise ValueError("Cadastro alterado desde a seleção. Solicite nova conferência.")
        rodada.esperado = produto.quantidade
        rodada.sequencia_ok = sequencia(produto.pk)
        rodada.ok_em = timezone.now()  # Server clock, only after acquiring the product lock.
        ConfirmacaoContagem.objects.create(rodada=rodada, versao_entrada=versao,
            embalagens=valores[0], avulsos=valores[1], contado=valores[2], contado_base=valores[3],
            esperado=rodada.esperado, sequencia_ok=rodada.sequencia_ok, ok_em=rodada.ok_em,
            acesso=acesso, responsavel_nome=acesso.funcionario_nome)
    else:
        # The prior confirmation remains in the immutable history. A draft is
        # not an OK and cannot pass general finalization.
        rodada.esperado = rodada.sequencia_ok = rodada.ok_em = None
    rodada.revisao_sequencia = rodada.revisao_por = rodada.revisada_em = None
    rodada.revisao_motivo = ""
    rodada.versao += 1
    rodada.save()
    atualizar_versao(inventario)
    evento(acesso.inventario, "produto_ok" if confirmar else "progresso_salvo", rodada=rodada, acesso=acesso, versao=rodada.versao, contagem_fisica=auditoria_fisica(rodada.item, valores[0], valores[1]))
    return rodada


def estado_conclusao(inventario, *, exigir_estrutura=False):
    """Avalia participantes; o chamador deve manter o lock da conferência."""
    itens = list(itens_incluidos(inventario).order_by("pk"))
    rodadas = list(rodadas_incluidas(inventario).filter(encerrada_em__isnull=True).order_by("pk"))
    pendencias = []
    if not itens:
        pendencias.append("Nenhum produto participante.")
    por_item = {}
    for rodada in rodadas:
        por_item.setdefault(rodada.item_id, []).append(rodada)
    atribuicoes = {a.rodada_id: a for a in AtribuicaoContagem.objects.filter(
        rodada__in=rodadas).select_related("acesso")}
    from .contagem_admin import ACAO
    versoes = {r.pk: r.versao for r in rodadas if r.ok_em}
    administrativos = {e.rodada_id for e in inventario.eventos.filter(acao=ACAO, rodada__in=rodadas)
                       if e.dados.get('versao_saida') == versoes.get(e.rodada_id)}
    for item in itens:
        nome = item.cadastro["nome"]
        atuais = por_item.get(item.pk, [])
        if len(atuais) != 1:
            if exigir_estrutura:
                raise ValueError("Há itens sem uma rodada atual única. Solicite revisão ao responsável.")
            pendencias.append(f"{nome}: sem rodada atual única.")
            continue
        rodada = atuais[0]
        atribuicao = atribuicoes.get(rodada.pk)
        if not rodada.ok_em:
            pendencias.append(f"{nome}: aguardando OK.")
        if rodada.pk in administrativos:
            continue  # Administrative confirmation needs no employee invitation or submission.
        if not atribuicao:
            pendencias.append(f"{nome}: aguardando atribuição.")
        elif atribuicao.acesso.ciclo != inventario.ciclo:
            pendencias.append(f"{nome}: atribuição de outro ciclo.")
        elif not atribuicao.acesso.enviada_em:
            pendencias.append(f"{nome}: aguardando envio de {atribuicao.acesso.funcionario_nome}.")
    return rodadas, pendencias


def concluir_se_pronta(inventario, *, usuario=None, acesso=None, versao=None, origem="envio_funcionario"):
    """Compartilhada pelos dois fluxos, dentro da transação e lock existentes."""
    exigir_nao_cancelada(inventario)
    if inventario.finalizada_em:
        return []
    rodadas, pendencias = estado_conclusao(inventario)
    if pendencias:
        return pendencias
    inventario.finalizada_em = timezone.now()
    inventario.finalizada_por = usuario
    inventario.finalizada_por_acesso = acesso
    inventario.finalizacao_versao = inventario.versao if versao is None else versao
    inventario.versao += 1
    inventario.save(update_fields=["finalizada_em", "finalizada_por", "finalizada_por_acesso", "finalizacao_versao", "versao"])
    evento(inventario, "conferencia_finalizada", usuario=usuario, acesso=acesso, ciclo=inventario.ciclo,
        origem=origem, rodadas=[{"id": r.pk, "versao": r.versao} for r in rodadas],
        finalizada_em=inventario.finalizada_em.isoformat())
    return []


@transaction.atomic
def finalizar_conferencia(acesso_id, versao, *, token_hash=None):
    inventario, acesso = bloquear_inventario_acesso(acesso_id)
    validar_acesso(acesso)
    if token_hash is not None and not secrets.compare_digest(acesso.token_hash, token_hash):
        raise ErroAcesso("credencial_renovada", "Acesso renovado. Abra o novo link em outra aba deste navegador e volte para verificar o acesso nesta tela.")
    if acesso.ciclo != inventario.ciclo:
        raise PermissionDenied("Acesso de ciclo anterior. Solicite novo link.")
    try:
        versao = int(versao)
    except (TypeError, ValueError):
        raise ValueError("Atualize a página antes de finalizar.")
    if acesso.enviada_em:
        if acesso.envio_versao == versao:
            return inventario
        raise ValueError("Atribuição já enviada. Atualize a página.")
    if inventario.finalizada_em:
        raise ValueError("Conferência já finalizada. Atualize a página.")
    if versao != inventario.versao:
        raise ValueError("As contagens mudaram em outra aba ou acesso. Atualize antes de finalizar.")
    rodadas, _ = estado_conclusao(inventario, exigir_estrutura=True)
    proprias = [r for r in rodadas if AtribuicaoContagem.objects.filter(acesso=acesso, rodada=r).exists()]
    if not proprias:
        raise PermissionDenied("Este acesso não possui contagens atuais para enviar.")
    pendentes = []
    for rodada in proprias:
        try:
            converter(cadastro_contagem(rodada.item), rodada.embalagens, rodada.avulsos)
            completa = rodada.ok_em is not None
        except ValueError:
            completa = False
        if not completa:
            pendentes.append(rodada.item.cadastro["nome"])
    if pendentes:
        raise ValueError("Conferência incompleta. Produtos pendentes: " + "; ".join(pendentes))
    validar_acesso(acesso)
    acesso.enviada_em = timezone.now()
    acesso.envio_versao = versao
    acesso.save(update_fields=["enviada_em", "envio_versao"])
    evento(inventario, "atribuicao_enviada", acesso=acesso, ciclo=inventario.ciclo,
           rodadas=[{"id": r.pk, "versao": r.versao} for r in proprias])
    validar_acesso(acesso)
    if concluir_se_pronta(inventario, acesso=acesso, versao=versao):
        atualizar_versao(inventario)
    return inventario


def abrir_ciclo(inventario, usuario, motivo):
    exigir_nao_cancelada(inventario)
    anterior = {"ciclo": inventario.ciclo, "finalizada_em": inventario.finalizada_em.isoformat() if inventario.finalizada_em else None,
                "acesso": inventario.finalizada_por_acesso_id}
    inventario.ciclo += 1
    inventario.finalizada_em = inventario.finalizacao_versao = inventario.finalizada_por = inventario.finalizada_por_acesso = None
    inventario.versao += 1
    inventario.save(update_fields=["ciclo", "finalizada_em", "finalizacao_versao", "finalizada_por", "finalizada_por_acesso", "versao"])
    evento(inventario, "conferencia_reaberta", usuario, motivo=motivo, anterior=anterior, ciclo=inventario.ciclo)


@transaction.atomic
def reabrir_conferencia(usuario, inventario_id, motivo):
    permitir(usuario, "revisar_inventario")
    if not str(motivo or "").strip():
        raise ValueError("Justifique a reabertura.")
    inventario = Inventario.objects.select_for_update().get(pk=inventario_id)
    exigir_nao_cancelada(inventario)
    if not inventario.finalizada_em:
        raise ValueError("A conferência já está em andamento.")
    if DecisaoAjuste.objects.filter(item__inventario=inventario).exists():
        raise ValueError("Existe ajuste aplicado. Crie outra conferência.")
    abrir_ciclo(inventario, usuario, motivo)
    for rodada in rodadas_incluidas(inventario).filter(encerrada_em__isnull=True).order_by("pk"):
        recontar(usuario, rodada.pk, motivo)
    return inventario


@transaction.atomic
def recontar(usuario, rodada_id, motivo):
    permitir(usuario, "revisar_inventario")
    if not str(motivo).strip():
        raise ValueError("Justifique a reabertura ou recontagem.")
    original = RodadaContagem.objects.get(pk=rodada_id)
    inventario = Inventario.objects.select_for_update().get(pk=original.item.inventario_id)
    exigir_nao_cancelada(inventario)
    if inventario.finalizada_em:
        abrir_ciclo(inventario, usuario, motivo)
    item = ItemInventario.objects.select_for_update().get(pk=original.item_id)
    if not itens_incluidos(inventario).filter(pk=item.pk).exists():
        raise ValueError("Produto retirado da conferencia. Reinclua antes de solicitar contagem.")
    rodada = RodadaContagem.objects.select_for_update().get(pk=rodada_id)
    if rodada.encerrada_em or DecisaoAjuste.objects.filter(item=item).exists():
        raise ValueError("Rodada encerrada ou ajuste já aplicado.")
    rodada.encerrada_em = timezone.now()
    rodada.save(update_fields=["encerrada_em"])
    nova = RodadaContagem.objects.create(item=item, numero=rodada.numero + 1)
    atualizar_versao(inventario)
    evento(item.inventario, "recontagem_solicitada", usuario, rodada=rodada, nova_rodada=nova.pk, motivo=motivo)
    return nova


def posteriores(rodada):
    if rodada.sequencia_ok is None:
        return MovimentoObservado.objects.none()
    return MovimentoObservado.objects.filter(produto_id=rodada.item.produto_id, sequencia__gt=rodada.sequencia_ok).order_by("sequencia")


def movimentos_relevantes(rodada):
    primeira = rodada.confirmacoes.order_by("versao_entrada").first()
    corte = primeira.sequencia_ok if primeira else rodada.sequencia_ok
    if corte is None:
        return MovimentoObservado.objects.none()
    return MovimentoObservado.objects.filter(produto_id=rodada.item.produto_id, sequencia__gt=corte).order_by("sequencia")


@transaction.atomic
def conciliar(usuario, rodada_id, movimento_id, referencia):
    permitir(usuario, "revisar_inventario")
    if not str(referencia).strip():
        raise ValueError("Informe documento e evidência da movimentação física após o OK.")
    exigir_nao_cancelada(Inventario.objects.select_for_update().get(itens__rodadas__pk=rodada_id))
    rodada = RodadaContagem.objects.select_for_update().select_related("item").get(pk=rodada_id)
    if not rodada.ok_em or rodada.encerrada_em or DecisaoAjuste.objects.filter(item=rodada.item).exists():
        raise ValueError("Rodada indisponível para conciliação.")
    movimento = movimentos_relevantes(rodada).get(pk=movimento_id)
    if movimento.origem not in ORIGENS or movimento.origem == "nao_identificada":
        raise ValueError("Origem não identificada exige revisão técnica e recontagem; não pode ser liberada por conciliação.")
    if movimento.antes is None or movimento.depois is None:
        raise ValueError("Movimento sem saldo conhecido exige recontagem.")
    conciliacao, criada = ConciliacaoMovimento.objects.get_or_create(rodada=rodada, movimento=movimento,
        defaults={"responsavel": usuario, "referencia": referencia})
    if not criada and conciliacao.conciliado_em < rodada.ok_em:
        evento(rodada.item.inventario, "conciliacao_anterior_preservada", usuario, rodada=rodada,
            movimento=movimento.pk, responsavel=conciliacao.responsavel_id, referencia=conciliacao.referencia,
            conciliado_em=conciliacao.conciliado_em.isoformat())
        conciliacao.responsavel, conciliacao.referencia, conciliacao.conciliado_em = usuario, referencia, timezone.now()
        conciliacao.save(update_fields=["responsavel", "referencia", "conciliado_em"])
        criada = True
    if criada:
        rodada.revisao_sequencia = None
        rodada.save(update_fields=["revisao_sequencia"])
        evento(rodada.item.inventario, "movimento_conciliado", usuario, rodada=rodada, movimento=movimento.pk, referencia=referencia)
    return conciliacao


def validar_cadeia(rodada, produto, documentos=(), *, _lote=None):
    if _lote is not None:
        _lote.conferir(rodada)
    if not (rodada.item_id in _lote.incluidos if _lote is not None else itens_incluidos().filter(pk=rodada.item_id).exists()):
        raise ValueError("Produto retirado da conferencia.")
    if _lote is None:
        verificar_observador()
    elif _lote.erro_observador:
        raise ValueError(_lote.erro_observador)
    if not rodada.ok_em or rodada.encerrada_em:
        raise ValueError("Confirme uma rodada ativa antes da revisão.")
    if (rodada.item_id in _lote.pendentes if _lote is not None else SinalizacaoFisica.objects.filter(rodada__item_id=rodada.item_id, resolvida_em__isnull=True).exists()):
        raise ValueError("Movimentação física sinalizada sem resolução. Revisão ou recontagem necessária.")
    if not produto.ativo or produto.excluido or produto.quantidade is None or not cadastro_compativel(produto, rodada.item.cadastro):
        raise ValueError("Produto ou conversão alterado. Solicite nova conferência.")
    exigir_representavel(rodada)
    confirmacoes = _lote.confirmacoes[rodada.pk] if _lote is not None else list(rodada.confirmacoes.order_by("versao_entrada"))
    primeira = confirmacoes[0] if confirmacoes else rodada
    movimentos = _lote.movimentos[rodada.item.produto_id] if _lote is not None else list(movimentos_relevantes(rodada))
    proxima, saldo = primeira.sequencia_ok + 1, primeira.esperado
    saldos = {primeira.sequencia_ok: primeira.esperado}
    conciliados = _lote.conciliados[rodada.pk] if _lote is not None else set(rodada.conciliacoes.filter(conciliado_em__gte=rodada.ok_em).values_list("movimento_id", flat=True))
    for movimento in movimentos:
        if movimento.sequencia != proxima or movimento.antes != saldo or movimento.depois is None:
            raise ValueError("Histórico descontínuo. Revisão técnica e recontagem necessárias.")
        if movimento.origem not in ORIGENS or movimento.origem == "nao_identificada":
            raise ValueError("Movimento digital sem origem rastreável. Exige revisão técnica e recontagem; aprovação bloqueada.")
        if movimento.pk not in conciliados and movimento.pk not in documentos:
            raise ValueError("Há movimento entre confirmações ou após o último OK sem conciliação física comprovada.")
        saldo, proxima = movimento.depois, proxima + 1
        saldos[movimento.sequencia] = movimento.depois
    for confirmacao in confirmacoes:
        if saldos.get(confirmacao.sequencia_ok) != confirmacao.esperado:
            raise ValueError("Referências entre confirmações não conciliadas. Solicite revisão técnica ou recontagem.")
    atual = _lote.sequencias.get(produto.pk, 0) if _lote is not None else sequencia(produto.pk)
    if proxima - 1 != atual or saldo != produto.quantidade:
        raise ValueError("Saldo ou sequência sem conciliação. Solicite recontagem.")
    return atual


@transaction.atomic
def sinalizar_fisico(rodada_id, descricao, chave, *, acesso_id=None, usuario=None, token_hash=None):
    descricao = str(descricao or "").strip()
    if not descricao or len(descricao) > 2000:
        raise ValueError("Descreva a movimentação física ou dúvida em até 2000 caracteres.")
    try:
        chave = UUID(str(chave))
    except (TypeError, ValueError):
        raise ValueError("Identificação da sinalização inválida. Atualize a página.")
    acesso = None
    if acesso_id is not None:
        inventario, acesso = bloquear_inventario_acesso(acesso_id)
        validar_escrita(inventario, acesso, token_hash)
        if token_hash is None or not secrets.compare_digest(acesso.token_hash, token_hash):
            raise PermissionDenied("Acesso renovado ou inválido.")
        if not AtribuicaoContagem.objects.filter(acesso=acesso, rodada_id=rodada_id).exists():
            raise PermissionDenied("Produto fora desta atribuição.")
    else:
        permitir(usuario, "revisar_inventario")
        exigir_nao_cancelada(Inventario.objects.select_for_update().get(itens__rodadas__pk=rodada_id))
    referencia = RodadaContagem.objects.get(pk=rodada_id)
    item = ItemInventario.objects.select_for_update().get(pk=referencia.item_id)
    if not itens_incluidos().filter(pk=item.pk).exists():
        raise ValueError("Produto retirado da conferencia.")
    rodada = RodadaContagem.objects.select_for_update().get(pk=rodada_id)
    if rodada.encerrada_em:
        raise ValueError("Rodada encerrada. Sinalize na rodada atual.")
    existente = SinalizacaoFisica.objects.filter(chave=chave).first()
    if existente:
        if (existente.rodada_id, existente.acesso_id, existente.usuario_id, existente.descricao) != (rodada.pk, acesso_id, getattr(usuario, "pk", None), descricao):
            raise ValueError("Identificação já utilizada em outra sinalização.")
        return existente
    sinal = SinalizacaoFisica.objects.create(rodada=rodada, chave=chave, descricao=descricao, acesso=acesso, usuario=usuario)
    RodadaContagem.objects.filter(item=item, encerrada_em__isnull=True).update(revisao_sequencia=None)
    evento(item.inventario, "fisico_sinalizado", usuario, rodada=rodada, acesso=acesso, sinalizacao=sinal.pk, descricao=descricao)
    return sinal


@transaction.atomic
def resolver_sinalizacao(usuario, sinalizacao_id, evidencia):
    permitir(usuario, "revisar_inventario")
    if not str(evidencia or "").strip():
        raise ValueError("Documente a verificação física e a resolução. Sem evidência, solicite recontagem.")
    referencia = SinalizacaoFisica.objects.select_related("rodada").get(pk=sinalizacao_id)
    exigir_nao_cancelada(Inventario.objects.select_for_update().get(pk=referencia.rodada.item.inventario_id))
    item = ItemInventario.objects.select_for_update().get(pk=referencia.rodada.item_id)
    sinal = SinalizacaoFisica.objects.select_for_update().get(pk=sinalizacao_id)
    if sinal.resolvida_em is None:
        sinal.resolvida_em, sinal.resolvida_por, sinal.evidencia = timezone.now(), usuario, evidencia
        sinal.save(update_fields=["resolvida_em", "resolvida_por", "evidencia"])
        RodadaContagem.objects.filter(item=item, encerrada_em__isnull=True).update(revisao_sequencia=None)
        evento(item.inventario, "fisico_resolvido", usuario, rodada=sinal.rodada, sinalizacao=sinal.pk, evidencia=evidencia)
    return sinal


@transaction.atomic
def revisar(usuario, rodada_id, motivo):
    permitir(usuario, "revisar_inventario")
    if not str(motivo).strip():
        raise ValueError("Documente a correspondência física no OK e a ausência de lançamentos pendentes.")
    exigir_nao_cancelada(Inventario.objects.select_for_update().get(itens__rodadas__pk=rodada_id))
    rodada = RodadaContagem.objects.select_for_update().select_related("item").get(pk=rodada_id)
    if DecisaoAjuste.objects.filter(item=rodada.item).exists():
        raise ValueError("Ajuste já aplicado.")
    produto = Produto.objects.select_for_update().get(pk=rodada.item.produto_id)
    rodada.revisao_sequencia = validar_cadeia(rodada, produto)
    rodada.revisao_por = usuario
    rodada.revisao_motivo = motivo
    rodada.revisada_em = timezone.now()
    rodada.save(update_fields=["revisao_sequencia", "revisao_por", "revisao_motivo", "revisada_em"])
    evento(rodada.item.inventario, "revisado", usuario, rodada=rodada, motivo=motivo, sequencia=rodada.revisao_sequencia)
    return rodada


@transaction.atomic
def aprovar(usuario, rodada_id, justificativa):
    permitir(usuario, "aprovar_ajuste")
    # Deliberately disabled in normal settings. Enabling requires a later reviewed release.
    if not getattr(settings, "INVENTARIO_AJUSTES_HABILITADOS", False):
        raise ValueError("Aplicação de ajustes desabilitada. Aguarde revisão e autorização posterior.")
    raise ValueError("Fluxo antigo desativado. Use Ajustes de estoque: aprove a proposta e confirme a aplicação separadamente.")


@transaction.atomic
def cancelar_conferencia(usuario, inventario_id):
    permitir(usuario, "gerenciar_inventario")
    inventario = Inventario.objects.select_for_update().get(pk=inventario_id)
    if inventario.cancelada_em:
        return inventario
    if inventario.finalizada_em:
        raise ValueError("Conferência já finalizada. O cancelamento é permitido somente em andamento.")
    from .models import AplicacaoAjuste, PropostaAjuste
    if (DecisaoAjuste.objects.filter(item__inventario=inventario).exists()
            or AplicacaoAjuste.objects.filter(proposta__rodada__item__inventario=inventario).exists()
            or PropostaAjuste.objects.filter(rodada__item__inventario=inventario, estado="aplicado").exists()):
        raise ValueError("Cancelamento impedido: existem ajustes de estoque já aplicados. Nenhum ajuste será desfeito.")
    agora = timezone.now()
    revogados = inventario.acessos.filter(revogado_em__isnull=True).update(revogado_em=agora)
    inventario.cancelada_em = agora
    inventario.cancelada_por = usuario
    inventario.versao += 1
    inventario.save(update_fields=["cancelada_em", "cancelada_por", "versao"])
    evento(inventario, "conferencia_cancelada", usuario, situacao_anterior="em_andamento",
           acessos_revogados=revogados, cancelada_em=agora.isoformat())
    return inventario
