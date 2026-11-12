"""Administrative OK on the current round; never write stock or employee credentials."""
from django.core import signing
from django.core.exceptions import PermissionDenied
from django.db import transaction
from django.utils import timezone

from estoque.models import Produto
from . import services
from .conversao import converter, snapshot, cadastro_compativel
from .models import (Inventario, ItemInventario, RodadaContagem, ConfirmacaoContagem,
                     EventoInventario, DecisaoAjuste, AplicacaoAjuste, PropostaAjuste)
from .conversao_fisica import cadastro_contagem, auditoria as auditoria_fisica
from .selecao import itens_incluidos

SALT = 'inventario-contagem-administrativa-v1'
ACAO = 'contagem_admin_confirmada'
NOVA = 'nova_contagem_admin'


def autorizacao(usuario, inventario, rodada):
    return signing.dumps(dict(usuario=usuario.pk, inventario=inventario.pk,
        rodada=rodada.pk, versao=rodada.versao, ciclo=inventario.ciclo), salt=SALT)


def confirmado(rodada):
    return bool(rodada.ok_em and EventoInventario.objects.filter(
        rodada=rodada, acao=ACAO, dados__versao_saida=rodada.versao).exists())


def valores_auditoria(rodada):
    campos = ('embalagens', 'avulsos', 'contado', 'contado_base', 'esperado', 'sequencia_ok', 'ok_em', 'versao')
    return {campo: str(getattr(rodada, campo)) if getattr(rodada, campo) is not None else None for campo in campos}


def mesma_contagem(valores, rodada):
    """An omitted zero-loose-unit field is not a new physical observation."""
    return (valores[0] == rodada.embalagens
        and (valores[1] or 0) == (rodada.avulsos or 0)
        and valores[2:] == (rodada.contado, rodada.contado_base))


@transaction.atomic
def confirmar(usuario, inventario_id, rodada_id, token, embalagens, avulsos, *, tipo='corrigir'):
    services.permitir(usuario, 'revisar_inventario')
    if tipo not in ('corrigir', 'nova'):
        raise ValueError('Escolha corrigir a contagem anterior ou registrar uma nova contagem física agora.')
    try:
        dados = signing.loads(token, salt=SALT, max_age=43200)
    except (signing.BadSignature, TypeError, ValueError):
        raise ValueError('Formulário inválido ou expirado. Atualize a página.')
    if (not isinstance(dados, dict) or dados.get('usuario') != usuario.pk
            or dados.get('inventario') != inventario_id or dados.get('rodada') != rodada_id
            or type(dados.get('versao')) is not int):
        raise PermissionDenied('Formulário fora desta conferência ou deste usuário.')
    inventario = Inventario.objects.select_for_update().get(pk=inventario_id)
    services.exigir_nao_cancelada(inventario)
    referencia = RodadaContagem.objects.get(pk=rodada_id, item__inventario=inventario)
    item = ItemInventario.objects.select_for_update().get(pk=referencia.item_id)
    rodada = RodadaContagem.objects.select_for_update().get(pk=rodada_id)
    valores = converter(cadastro_contagem(item), embalagens, avulsos)
    # An explicit new count reuses the existing round lifecycle. Its accepted
    # request can be retried even after the old round was closed or the new one applied.
    if tipo == 'nova':
        anterior_nova = EventoInventario.objects.filter(inventario=inventario, rodada=rodada,
            usuario=usuario, acao=NOVA, dados__versao_entrada=dados['versao']).first()
        if anterior_nova:
            conf = ConfirmacaoContagem.objects.get(pk=anterior_nova.dados['confirmacao_id'])
            if (conf.embalagens, conf.avulsos) != valores[:2]:
                raise ValueError('Esta versão já registrou outra nova contagem. Atualize a página.')
            return RodadaContagem.objects.get(pk=conf.rodada_id)
    if dados.get('ciclo') != inventario.ciclo:
        raise ValueError('O ciclo da conferência mudou. Atualize a página.')
    if (rodada.encerrada_em or item.rodadas.filter(numero__gt=rodada.numero).exists()
            or not itens_incluidos(inventario).filter(pk=item.pk).exists()):
        raise ValueError('Esta rodada não está disponível. Atualize a página.')
    if (DecisaoAjuste.objects.filter(item=item).exists()
            or AplicacaoAjuste.objects.filter(proposta__rodada__item=item).exists()
            or PropostaAjuste.objects.filter(rodada__item=item, estado='aplicado').exists()):
        raise ValueError('Produto com ajuste de estoque aplicado. A correção exige um processo separado.')
    anterior = rodada.confirmacoes.filter(versao_entrada=dados['versao']).first()
    if anterior:
        # Retry only this administrator's accepted request, never somebody else's OK.
        evento_anterior = EventoInventario.objects.filter(inventario=inventario, rodada=rodada,
            usuario=usuario, acao=ACAO, dados__confirmacao_id=anterior.pk).first()
        if tipo == 'nova' and evento_anterior and evento_anterior.dados.get('tipo') != 'nova_contagem':
            raise ValueError('Esta versão já corrigiu a contagem anterior. Atualize antes de registrar uma nova contagem.')
        if (anterior.acesso_id is None and (anterior.embalagens, anterior.avulsos) == valores[:2]
                and evento_anterior):
            return rodada
        raise ValueError('Esta versão já foi confirmada com outra contagem. Atualize a página.')
    if rodada.versao != dados['versao']:
        raise ValueError('A contagem mudou em outra aba ou acesso. Atualize a página.')
    if tipo == 'nova' and rodada.ok_em:
        nova = services.recontar(usuario, rodada.pk, 'Nova contagem física realizada agora pelo administrador.')
        inventario.refresh_from_db()
        nova = confirmar(usuario, inventario.pk, nova.pk,
            autorizacao(usuario, inventario, nova), embalagens, avulsos)
        conf = nova.confirmacoes.order_by('-versao_entrada').first()
        services.evento(inventario, NOVA, usuario, rodada=rodada,
            versao_entrada=dados['versao'], nova_rodada=nova.pk, confirmacao_id=conf.pk,
            estoque_alterado=False)
        return nova
    ultimo = rodada.confirmacoes.order_by('-versao_entrada').first()
    if rodada.ok_em:
        campos = ('embalagens', 'avulsos', 'contado', 'contado_base', 'esperado', 'sequencia_ok', 'ok_em')
        if not ultimo or any(getattr(ultimo, c) != getattr(rodada, c) for c in campos):
            raise ValueError('Confirmação anterior incompatível. Registre uma nova contagem física agora.')
        if mesma_contagem(valores, rodada):
            return rodada  # Reconfirming identical numbers never creates a new temporal reference.
    services.verificar_observador()
    produto = Produto.objects.select_for_update().get(pk=item.produto_id)
    if not produto.ativo or produto.excluido or produto.quantidade is None or not cadastro_compativel(produto, item.cadastro):
        raise ValueError('Produto ou cadastro alterado. Revise antes de confirmar a contagem.')
    antes = valores_auditoria(rodada)
    rodada.embalagens, rodada.avulsos, rodada.contado, rodada.contado_base = valores
    if not rodada.ok_em:
        rodada.esperado = produto.quantidade
        rodada.sequencia_ok = services.sequencia(produto.pk)
        rodada.ok_em = timezone.now()
    confirmacao = ConfirmacaoContagem.objects.create(rodada=rodada, versao_entrada=rodada.versao,
        embalagens=valores[0], avulsos=valores[1], contado=valores[2], contado_base=valores[3],
        esperado=rodada.esperado, sequencia_ok=rodada.sequencia_ok, ok_em=rodada.ok_em,
        acesso=None, responsavel_nome=usuario.get_full_name() or usuario.get_username())
    rodada.revisao_sequencia = rodada.revisao_por = rodada.revisada_em = None
    rodada.revisao_motivo = ''
    rodada.versao += 1
    rodada.save()
    services.atualizar_versao(inventario)
    services.evento(inventario, ACAO, usuario, rodada=rodada, confirmacao_id=confirmacao.pk,
        confirmacao_anterior_id=ultimo.pk if ultimo else None, antes=antes, depois=valores_auditoria(rodada),
        versao_saida=rodada.versao, produto_id=produto.pk, produto_nome=item.cadastro['nome'],
        unidade=item.cadastro['base'], secundaria=item.cadastro['secundaria'],
        fator=item.cadastro['fator'], conferencia_id=inventario.pk, rodada_numero=rodada.numero,
        ciclo=inventario.ciclo, usuario_nome=usuario.get_username(), estoque_alterado=False,
        tipo='correcao_anterior' if ultimo and antes['ok_em'] else 'nova_contagem',
        contagem_fisica=auditoria_fisica(item, valores[0], valores[1]))
    return rodada
