import csv
from collections import defaultdict
import secrets
from urllib.parse import urlencode
from uuid import uuid4
from decimal import Decimal
from functools import wraps

from django.conf import settings
from django.contrib.auth.decorators import login_required
from django.contrib import messages
from django.core.exceptions import ObjectDoesNotExist, PermissionDenied
from django.middleware.csrf import get_token
from django.db.models import Count, F, Q, OuterRef, Subquery, Prefetch
from django.db import transaction
from django.http import HttpResponse, JsonResponse
from django.shortcuts import get_object_or_404, redirect, render
from django.urls import reverse
from django.utils import timezone
from django.views.decorators.cache import never_cache
from django.views.decorators.csrf import csrf_protect
from django.views.decorators.http import require_http_methods, require_POST

from estoque.models import Produto, Fornecedor, Funcionario
from .conversao import converter
from . import services, sessoes, ajustes
from . import acesso_ajustes
from . import confirmacao_ajustes
from .erros import ErroAcesso
from .models import Inventario, RodadaContagem, AcessoInventario, DecisaoAjuste, SinalizacaoFisica, ConfirmacaoContagem
from .apresentacao import entrada, quantidade
from .painel import resumo_painel
from .selecao import itens_incluidos, rodadas_incluidas, salvar_selecao
from .rastreabilidade import ORIGENS
from .titulos import preparar_titulos
from .ordenacao import chave_produto


def privado(view):
    @wraps(view)
    @never_cache
    def wrapped(request, *args, **kwargs):
        response = view(request, *args, **kwargs)
        # Keep same-origin form submissions compatible with Django CSRF origin checks.
        # Never send the invitation path to a different origin.
        response["Referrer-Policy"] = "same-origin"
        response["X-Robots-Tag"] = "noindex, nofollow"
        response["Content-Security-Policy"] = "default-src 'self'; style-src 'self' 'unsafe-inline'; script-src 'self'; frame-ancestors 'none'; form-action 'self'; base-uri 'none'"
        return response
    return wrapped


def gestor(request):
    if not request.user.is_authenticated or not any(request.user.has_perm("inventario." + p) for p in
            ("gerenciar_inventario", "revisar_inventario", "aprovar_ajuste")):
        raise PermissionDenied("Acesse com um usuário autorizado de inventário.")


def resumo_inicial():
    # Current rounds only: requesting a recount must replace, not add to, its
    # former round in these totals. Never confirms or reconciles from a GET.
    ultima = RodadaContagem.objects.filter(item_id=OuterRef("item_id")).order_by("-numero").values("pk")[:1]
    atuais = rodadas_incluidas().filter(pk=Subquery(ultima), encerrada_em__isnull=True, item__decisao__isnull=True, item__inventario__cancelada_em__isnull=True)
    return atuais.aggregate(
        conferencias=Count("item__inventario_id", distinct=True, filter=Q(item__inventario__finalizada_em__isnull=True)),
        aguardando_ok=Count("pk", filter=Q(ok_em__isnull=True)),
        divergencias=Count("pk", filter=Q(ok_em__isnull=False, contado_base__isnull=False, esperado__isnull=False) & ~Q(contado_base=F("esperado"))),
        recontagens=Count("pk", filter=Q(numero__gt=1, ok_em__isnull=True)),
    )


@privado
@login_required(login_url="inventario:login")
@require_http_methods(["GET", "POST"])
def lista(request):
    gestor(request)
    erro = ""
    if request.method == "POST":
        try:
            criterio = request.POST.get("criterio")
            selecionados = request.POST.getlist("produtos") if criterio == "manual" else request.POST.get(criterio or "")
            inventario = services.criar_inventario(request.user, None, criterio, selecionados)
            return redirect("inventario:detalhe", inventario.pk)
        except (ValueError, ObjectDoesNotExist) as exc:
            erro = str(exc)
    produtos = services.produtos_disponiveis_para_inventario().order_by("nome")
    inventarios = Inventario.objects.order_by('-criado_em', '-pk')
    em_andamento = list(inventarios.filter(finalizada_em__isnull=True, cancelada_em__isnull=True))
    finalizados = list(inventarios.filter(finalizada_em__isnull=False, cancelada_em__isnull=True))
    preparar_titulos(em_andamento + finalizados)
    return render(request, "inventario/lista.html", {
        "em_andamento": em_andamento,
        "finalizados": finalizados, "erro": erro,
        "categorias": produtos.exclude(categoria__isnull=True).exclude(categoria="").values_list("categoria", flat=True).distinct().order_by("categoria"),
        "fornecedores": Fornecedor.objects.filter(produtos_vinculados__ativo=True).distinct().order_by("nome"),
        "produtos": produtos, "resumo": resumo_inicial(), "pode_criar": request.user.has_perm("inventario.gerenciar_inventario")})


def linhas_revisao(inventario):
    linhas = []
    itens = list(itens_incluidos(inventario).select_related("produto").prefetch_related(
        Prefetch("rodadas", queryset=RodadaContagem.objects.select_related(
            "atribuicao__acesso", "revisao_por").prefetch_related("confirmacoes").order_by("numero"),
            to_attr="rodadas_revisao"),
        "decisao",
    ).order_by("pk"))
    sinais_por_item, eventos_por_item = defaultdict(list), defaultdict(list)
    for sinal in SinalizacaoFisica.objects.filter(rodada__item__in=itens).select_related(
            "rodada", "acesso", "usuario", "resolvida_por").order_by("criado_em"):
        sinais_por_item[sinal.rodada.item_id].append(sinal)
    for registro in inventario.eventos.filter(rodada__item__in=itens).select_related(
            "rodada", "usuario", "acesso").order_by("criado_em"):
        eventos_por_item[registro.rodada.item_id].append(registro)
    from contextlib import nullcontext
    from django.db.models import prefetch_related_objects
    from .contexto_ajustes import carregar
    atuais = [item.rodadas_revisao[-1] for item in itens]
    com_evidencias = [r for r in atuais if r.ok_em or r.confirmacoes.all()]
    prefetch_related_objects(com_evidencias, "conciliacoes__responsavel")
    with carregar(com_evidencias) if com_evidencias else nullcontext(None) as lote:
        for item in itens:
            rodada = item.rodadas_revisao[-1]
            conciliacoes = {c.movimento_id: c for c in rodada.conciliacoes.all() if c.conciliado_em >= rodada.ok_em} if rodada.ok_em else {}
            todos_movimentos = list(lote.movimentos[item.produto_id]) if lote is not None and (rodada.ok_em or rodada.confirmacoes.all()) else []
            sinalizacoes = sinais_por_item[item.pk]
            diferenca = rodada.contado_base - rodada.esperado if rodada.ok_em else None
            valor = (diferenca * item.custo_base).quantize(Decimal("0.01")) if diferenca is not None and item.custo_base is not None else None
            inseguro = False
            try:
                # There is no chain to review before an OK. Keep all validation
                # for confirmed rounds, including observer and stock checks.
                if not rodada.ok_em:
                    raise ValueError("Confirme uma rodada ativa antes da revisão.")
                services.validar_cadeia(rodada, item.produto, _lote=lote)
                bloqueio = "Revisão física necessária" if rodada.revisao_sequencia != lote.sequencias.get(item.produto_id, 0) else "Revisão registrada"
            except ValueError as exc:
                bloqueio = str(exc)
                inseguro = rodada.ok_em is not None
            try:
                decisao = item.decisao
            except DecisaoAjuste.DoesNotExist:
                decisao = None
            situacao = "Aguardando OK" if not rodada.ok_em else "Correto" if diferenca == 0 else "Falta" if diferenca < 0 else "Sobra"
            if rodada.numero > 1 and not rodada.ok_em:
                situacao = "Recontagem"
            if inseguro or any(s.resolvida_em is None for s in sinalizacoes):
                situacao = "Revisão necessária"
            destaque = "falta" if diferenca is not None and diferenca < 0 else "sobra" if diferenca is not None and diferenca > 0 else "neutro"
            linhas.append({"item": item, "rodada": rodada, "diferenca": diferenca, "valor": valor,
                "bloqueio": bloqueio, "decisao": decisao,
                "situacao": situacao, "destaque": destaque,
                "esperado_texto": quantidade(rodada.esperado, item.cadastro),
                "contado_texto": quantidade(rodada.contado_base if rodada.ok_em else None, item.cadastro),
                "diferenca_texto": quantidade(rodada.contado - rodada.esperado if rodada.ok_em else None, item.cadastro, diferenca=True),
                "sinalizacoes": sinalizacoes, "chave_sinalizacao": str(uuid4()),
                "movimentos": [{"registro": m, "origem": ORIGENS.get(m.origem, ORIGENS["nao_identificada"]),
                    "quantidade": quantidade(m.depois - m.antes, item.cadastro, diferenca=True) if m.antes is not None and m.depois is not None else "Saldo desconhecido",
                    "conciliacao": conciliacoes.get(m.pk), "conciliado": m.pk in conciliacoes,
                    "entre_confirmacoes": rodada.sequencia_ok is not None and m.sequencia <= rodada.sequencia_ok} for m in todos_movimentos],
                "historico": item.rodadas_revisao, "eventos": eventos_por_item[item.pk]})
    return sorted(linhas, key=lambda linha: chave_produto(linha["item"]))


@privado
@require_http_methods(["GET", "POST"])
def detalhe(request, inventario_id, *, consulta_auditoria=False):
    try:
        gestor(request)
    except PermissionDenied as exc:
        if confirmacao_ajustes.solicitada(request):
            return confirmacao_ajustes.erro(request, exc, 403)
        raise
    inventario = get_object_or_404(Inventario, pk=inventario_id)
    if inventario.cancelada_em and request.method == "POST":
        raise PermissionDenied("Conferência cancelada. Disponível apenas para consulta.")
    if inventario.cancelada_em and not consulta_auditoria:
        messages.info(request, f"A Conferência nº {inventario.pk} foi cancelada. Seus registros estão preservados para consulta administrativa autorizada; o estoque permaneceu inalterado pelo cancelamento.")
        return redirect("inventario:lista")
    mensagens = confirmacao_ajustes.mensagens_validas(request, inventario_id)
    if request.method == 'GET' and confirmacao_ajustes.solicitada(request) and request.GET.get('resultado_atualizacao'):
        try:
            return confirmacao_ajustes.consultar(request, inventario_id)
        except PermissionDenied as exc:
            return confirmacao_ajustes.erro(request, exc, 403)
        except ValueError as exc:
            return confirmacao_ajustes.erro(request, exc)
    if inventario.finalizada_em and (
            request.GET.get("atualizar") == "prever"
            or request.method == "POST" and request.POST.get("acao") in {"atualizar", "aprovar"}):
        mensagem = 'Conferência em modo histórico. Use Ações administrativas para revisar ajustes pendentes.'
        if confirmacao_ajustes.solicitada(request):
            return confirmacao_ajustes.erro(request, mensagem, 403)
        raise PermissionDenied(mensagem)
    erro, link, whatsapp_url, destinatario, sucesso = "", "", "", "", ""
    links_adicionais = []
    if request.method == "POST":
        try:
            acao = request.POST.get("acao")
            rodada_id = request.POST.get("rodada")
            if acao in {"recontar", "conciliar", "revisar", "aprovar", "sinalizar"}:
                get_object_or_404(RodadaContagem, pk=rodada_id, item__inventario=inventario)
            if acao == "revisar_ajustes_historicos":
                acesso = acesso_ajustes.iniciar(request.user, inventario)
                return redirect(reverse("inventario:ajustes", args=[inventario.pk]) + "?" +
                                urlencode({"revisao_historica": acesso}))
            elif acao == "atualizar":
                with transaction.atomic():
                    atual = Inventario.objects.select_for_update().get(pk=inventario.pk)
                    if atual.finalizada_em:
                        raise PermissionDenied("Conferência em modo histórico. Use Ações administrativas.")
                    ajustes.atualizar(request.user, inventario.pk, request.POST.get("previa"),
                        confirmacao=request.POST.get("confirmacao") == "sim",
                        observacao=request.POST.get("observacao", ""))
                    comprovacao = confirmacao_ajustes.conferir_gravacao(inventario.pk, request.POST.get('previa'))
                return confirmacao_ajustes.concluir(request, inventario.pk, comprovacao)
            elif acao == "selecao":
                selecao = salvar_selecao(request.user, inventario.pk, request.POST.getlist("participantes"),
                    request.POST.get("versao"), request.POST.get("motivo"))
                pendencias = getattr(selecao, "pendencias_conclusao", None)
                if selecao.finalizada_em:
                    sucesso = "Seleção salva. Conferência finalizada com os OKs e envios já registrados."
                elif pendencias:
                    messages.warning(request, "Seleção salva. Conferência permanece aberta: " + " ".join(pendencias))
            elif acao == "acesso":
                if request.POST.get("manter_nao_selecionados"):
                    raise ValueError("Atualize esta página e confirme a lista única de participantes. A reserva foi removida.")
                selecionados = request.POST.getlist("rodadas")
                funcionarios = {pk: request.POST.get("funcionario_" + pk) or request.POST.get("funcionario")
                    for pk in selecionados}
                gerados = services.distribuir_participantes(request.user, inventario.pk, selecionados,
                    funcionarios, request.POST.get("horas", 24), request.POST.get("versao"))
                sucesso = "Participantes salvos. Atribuição concluída. Entregue cada link somente ao funcionário indicado."
                if gerados:
                    acesso, token = gerados[0]
                    link = request.build_absolute_uri(reverse("inventario:convite", args=[token]))
                    links_adicionais = [{"nome": a.funcionario_nome,
                        "link": request.build_absolute_uri(reverse("inventario:convite", args=[t]))}
                        for a, t in gerados[1:]]
            elif acao == "reabrir_conferencia":
                services.reabrir_conferencia(request.user, inventario.pk, request.POST.get("motivo", ""))
            elif acao in {"revogar", "renovar"}:
                acesso = get_object_or_404(AcessoInventario, pk=request.POST.get("acesso"), inventario=inventario)
                if acao == "revogar":
                    services.revogar_acesso(request.user, acesso.pk)
                else:
                    acesso, token = services.renovar_acesso(request.user, acesso.pk)
                    link = request.build_absolute_uri(reverse("inventario:convite", args=[token]))
                    sucesso = "Acesso renovado. O link anterior foi invalidado; copie o novo link abaixo."
            elif acao == "recontar":
                services.recontar(request.user, rodada_id, request.POST.get("motivo", ""))
            elif acao == "conciliar":
                services.conciliar(request.user, rodada_id, request.POST.get("movimento"), request.POST.get("motivo", ""))
            elif acao == "revisar":
                if request.POST.get("correspondencia_fisica") != "sim":
                    raise ValueError("Confirme a verificação física e os lançamentos pendentes.")
                services.revisar(request.user, rodada_id, request.POST.get("motivo", ""))
            elif acao == "aprovar":
                services.aprovar(request.user, rodada_id, request.POST.get("motivo", ""))
            elif acao == "sinalizar":
                services.sinalizar_fisico(rodada_id, request.POST.get("motivo"), request.POST.get("chave"), usuario=request.user)
            elif acao == "resolver_sinalizacao":
                sinal = get_object_or_404(SinalizacaoFisica, pk=request.POST.get("sinalizacao"), rodada__item__inventario=inventario)
                services.resolver_sinalizacao(request.user, sinal.pk, request.POST.get("motivo"))
            else:
                raise ValueError("Ação inválida.")
            if not link:
                messages.success(request, sucesso or "Conferência atualizada com sucesso.")
                return redirect("inventario:detalhe", inventario.pk)
        except (ValueError, ObjectDoesNotExist, PermissionDenied) as exc:
            if confirmacao_ajustes.solicitada(request) and request.POST.get('acao') == 'atualizar':
                return confirmacao_ajustes.erro(request, exc, 403 if isinstance(exc, PermissionDenied) else 400)
            erro = str(exc)
    if link:
        destinatario = acesso.funcionario_nome
        telefone = acesso.funcionario.telefone_whatsapp_normalizado or Funcionario.normalizar_whatsapp(acesso.funcionario.telefone_whatsapp)
        if telefone:
            if len(telefone) in (10, 11):
                telefone = "55" + telefone
            whatsapp_url = "https://web.whatsapp.com/send?" + urlencode({"phone": telefone, "text": "Conferência de Estoque — " + inventario.titulo + "\nAbra no navegador do celular:\n" + link})
    inventario.refresh_from_db()
    linhas = linhas_revisao(inventario)
    resumo = resumo_painel(linhas)
    from . import contagem_admin
    pode_editar_contagem = request.user.has_perm('inventario.revisar_inventario') and not inventario.cancelada_em
    for linha in linhas:
        rodada = linha["rodada"]
        registro_admin = next((e for e in reversed(linha['eventos'])
            if e.acao == contagem_admin.ACAO and rodada.ok_em and e.dados.get('versao_saida') == rodada.versao), None)
        linha['ok_admin_nome'] = registro_admin.dados.get('usuario_nome') if registro_admin else ''
        linha['edicao_admin'] = pode_editar_contagem and not linha['decisao'] and not rodada.encerrada_em
        if linha['edicao_admin']:
            linha['autorizacao_admin'] = contagem_admin.autorizacao(request.user, inventario, rodada)
            linha['embalagens_admin'] = entrada(rodada.embalagens, linha['item'].cadastro['continua'])
            linha['avulsos_admin'] = entrada(rodada.avulsos, linha['item'].cadastro['secundaria_continua'])
        linha["participante_protegido"] = bool(getattr(rodada, "atribuicao", None)
            or rodada.numero != 1 or rodada.versao or rodada.ok_em or rodada.encerrada_em
            or rodada.revisada_em or linha["decisao"] or linha["sinalizacoes"]
            or any(getattr(rodada, campo) is not None for campo in ("embalagens", "avulsos", "contado", "contado_base", "esperado")))
        linha["participa_marcado"] = not erro or request.POST.get("acao") != "acesso" or linha["participante_protegido"] or str(rodada.pk) in request.POST.getlist("rodadas")
        linha["funcionario_escolhido"] = request.POST.get("funcionario_" + str(rodada.pk), "") if erro else ""
    prever = (request.method == "GET" and request.GET.get("atualizar") == "prever"
              and not inventario.finalizada_em and not inventario.cancelada_em
              and request.user.has_perm("inventario.aprovar_ajuste"))
    atualizacao = (ajustes.preparar_resultado(inventario) if prever
                   else ajustes.projetar_resultado(linhas))
    por_rodada = {l['rodada'].pk: l for l in atualizacao['linhas']}
    for linha in linhas:
        linha['atualizacao'] = por_rodada.get(linha['rodada'].pk)
    previa_lote = ajustes.previa_atualizacao(inventario.pk, atualizacao['aptas']) if prever and atualizacao['aptas'] else ''
    acessos = list(inventario.acessos.select_related("funcionario"))
    for acesso in acessos:
        atuais = acesso.atribuicoes.filter(rodada__encerrada_em__isnull=True)
        acesso.total_produtos = atuais.count()
        acesso.total_ok = atuais.filter(rodada__ok_em__isnull=False).count()
        acesso.expirado = acesso.expira_em <= timezone.now()
    encerramento_previa, encerramento_erro = None, ""
    if (not inventario.finalizada_em and not inventario.cancelada_em
            and request.user.has_perm("inventario.gerenciar_inventario")
            and request.user.has_perm("inventario.revisar_inventario")):
        from . import encerramento
        try:
            encerramento_previa = encerramento.previa(request.user, inventario)
        except ValueError as exc:
            encerramento_erro = str(exc)
    return render(request, "inventario/detalhe.html", {"inventario": inventario, "linhas": linhas, "messages": mensagens,
        "modo_historico": bool(inventario.finalizada_em or inventario.cancelada_em),
        "encerramento_previa": encerramento_previa, "encerramento_erro": encerramento_erro,
        "revisao_historica_disponivel": any(l['diferenca'] is not None and l['diferenca'] != 0
            and not l['decisao'] for l in linhas),
        "resumo_painel": resumo, "consultado_em": timezone.now(),
        "atualizacao": atualizacao, "previa_lote": previa_lote,
        "erro": erro, "link": link, "links_adicionais": links_adicionais, "funcionario_padrao": request.POST.get("funcionario", "") if erro else "", "sucesso": sucesso, "whatsapp_url": whatsapp_url, "destinatario": destinatario, "funcionarios": Funcionario.objects.filter(ativo=True).order_by("nome"),
        "eventos_selecao": inventario.eventos.filter(acao__in=["produto_retirado", "produto_reincluido"]).select_related("usuario").order_by("pk"),
        "eventos_auditoria": inventario.eventos.select_related('usuario', 'acesso').order_by('pk'),
        "rodadas_disponiveis": rodadas_incluidas(inventario).filter(encerrada_em__isnull=True, atribuicao__isnull=True).count(),
        "acessos": acessos, "ajustes_habilitados": getattr(settings, "INVENTARIO_AJUSTES_HABILITADOS", False),
        "pode_gerenciar": not inventario.cancelada_em and request.user.has_perm("inventario.gerenciar_inventario"),
        "pode_revisar": not inventario.cancelada_em and request.user.has_perm("inventario.revisar_inventario"),
        "pode_aprovar": not inventario.cancelada_em and request.user.has_perm("inventario.aprovar_ajuste")})


def csv_seguro(valor):
    texto = str(valor if valor is not None else "")
    return "'" + texto if texto.lstrip().startswith(("=", "+", "-", "@")) else texto


@privado
@require_http_methods(["GET"])
def movimento_manual(request, inventario_id, rodada_id, movimento_id):
    gestor(request)
    from estoque.models import MovimentacaoEstoqueManual
    rodada = get_object_or_404(RodadaContagem.objects.select_related('item__inventario'),
        pk=rodada_id, item__inventario_id=inventario_id)
    movimento = get_object_or_404(MovimentacaoEstoqueManual, pk=movimento_id,
        produto_id=rodada.item.produto_id)
    return render(request, 'inventario/movimento_manual.html', {'rodada':rodada,'movimento':movimento})


@privado
@require_http_methods(["GET"])
def historico_produto(request, inventario_id, rodada_id):
    gestor(request)
    from .historico import investigar
    rodada = get_object_or_404(RodadaContagem.objects.select_related('item__inventario'),
                              pk=rodada_id, item__inventario_id=inventario_id)
    return render(request, 'inventario/historico.html', investigar(rodada, request.GET))


@privado
@require_http_methods(["GET"])
def relatorio(request, inventario_id):
    gestor(request)
    inventario = get_object_or_404(Inventario, pk=inventario_id)
    response = HttpResponse(content_type="text/csv; charset=utf-8")
    response["Content-Disposition"] = f'attachment; filename="inventario-{inventario.pk}.csv"'
    response.write("\ufeff")
    writer = csv.writer(response, delimiter=";")
    writer.writerow(["Produto", "Unidade", "Rodada", "OK servidor", "Esperado", "Contado", "Diferença", "Situação", "Valor estimado ao custo", "Revisão", "Diferença em unidade adequada", "Situação operacional"])
    for linha in linhas_revisao(inventario):
        rodada, item, diferenca = linha["rodada"], linha["item"], linha["diferenca"]
        writer.writerow([csv_seguro(item.cadastro["nome"]), csv_seguro(item.cadastro["base"]), rodada.numero,
            rodada.ok_em.isoformat() if rodada.ok_em else "", rodada.esperado, rodada.contado_base,
            diferenca, "Pendente" if diferenca is None else "Falta" if diferenca < 0 else "Sobra" if diferenca > 0 else "Conforme",
            linha["valor"], csv_seguro(linha["bloqueio"]), csv_seguro(linha["diferenca_texto"]), linha["situacao"]])
    return response


@privado
@csrf_protect
@require_http_methods(["GET", "POST"])
def convite(request, token):
    acesso = services.acesso_token(token)
    if request.method == "POST":
        sessoes.estabelecer(request, acesso)
        return redirect("inventario:mobile")
    return render(request, "inventario/convite.html", {"funcionario": acesso.funcionario_nome})


def acesso_sessao(request):
    acesso_id = request.inventario_session.get("inventario_acesso")
    if not acesso_id:
        raise ErroAcesso("sessao_ausente", "Sessão de contagem ausente ou expirada. Abra o link vigente em outra aba deste mesmo navegador e volte para verificar o acesso nesta tela.")
    acesso = AcessoInventario.objects.filter(pk=acesso_id).first()
    services.validar_acesso(acesso)
    if not secrets.compare_digest(acesso.token_hash, request.inventario_session.get("inventario_token_hash", "")):
        raise ErroAcesso("credencial_renovada", "Este acesso foi renovado. Abra o novo link em outra aba deste mesmo navegador e volte para verificar o acesso nesta tela.")
    return acesso


def payload_cego(rodada):
    cadastro = rodada.item.cadastro
    # A concurrent OK may commit after this round was read. Keep the history
    # consistent with the version being returned; a stale write still gets 409.
    if hasattr(rodada, "confirmacoes_mobile"):
        ultimo = next((c for c in rodada.confirmacoes_mobile if c.versao_entrada < rodada.versao), None)
    else:
        ultimo = rodada.confirmacoes.filter(versao_entrada__lt=rodada.versao).order_by("-versao_entrada").first()
    total = ""
    exibir_total = cadastro["base"] == "PCT" and cadastro["secundaria"] == "KG" and cadastro["fracionado"]
    if exibir_total:
        try:
            total = format(converter(cadastro, rodada.embalagens, rodada.avulsos)[3], ".3f").replace(".", ",")
        except ValueError:
            pass
    return {"total_convertido": total, "exibir_total": exibir_total,
        "id": rodada.pk, "nome": cadastro["nome"], "codigo": cadastro["codigo"], "base": cadastro["base"],
        "secundaria": cadastro["secundaria"], "fracionado": cadastro["fracionado"],
        "passo": "0.001" if cadastro["continua"] else "1", "passo_avulsos": "0.001" if cadastro["secundaria_continua"] else "1",
        "embalagens": entrada(rodada.embalagens, cadastro["continua"]),
        "avulsos": entrada(rodada.avulsos, cadastro["secundaria_continua"]), "versao": rodada.versao,
        "guardado": rodada.versao > 0,
        "ultimo_ok": {"embalagens": entrada(ultimo.embalagens, cadastro["continua"]),
                      "avulsos": entrada(ultimo.avulsos, cadastro["secundaria_continua"]),
                      "ok_em": ultimo.ok_em.isoformat()} if ultimo else None,
        "conferido": rodada.ok_em is not None, "ok_em": rodada.ok_em.isoformat() if rodada.ok_em else None}


@privado
@require_http_methods(["GET"])
def mobile(request):
    acesso = acesso_sessao(request)
    rodadas = rodadas_incluidas().filter(atribuicao__acesso=acesso, encerrada_em__isnull=True).select_related("item").prefetch_related(
        Prefetch("confirmacoes", queryset=ConfirmacaoContagem.objects.order_by("-versao_entrada"),
                 to_attr="confirmacoes_mobile")).order_by("pk")
    estado = estado_conferencia(acesso)
    if acesso.ciclo != acesso.inventario.ciclo:
        rodadas = rodadas.none()
    produtos = [dict(payload_cego(r), chave_sinalizacao=str(uuid4())) for r in sorted(rodadas, key=lambda rodada: chave_produto(rodada.item))]
    progresso = {"total": len(produtos), "conferidos": sum(p["conferido"] for p in produtos)}
    progresso["faltam"] = progresso["total"] - progresso["conferidos"]
    if "application/json" in request.headers.get("Accept", ""):
        return JsonResponse({"produtos": produtos, "progresso": progresso, "conferencia": estado, "csrf": get_token(request),
                             "acesso_id": acesso.pk})
    return render(request, "inventario/mobile.html", {"funcionario": acesso.funcionario_nome, "conferencia": estado,
        "produtos": produtos, "progresso": progresso, "acesso_id": acesso.pk, "conferencia_id": acesso.inventario_id})


def estado_conferencia(acesso):
    inventario = Inventario.objects.get(pk=acesso.inventario_id)
    return {"versao": inventario.versao, "finalizada": inventario.finalizada_em is not None,
        "finalizada_em": inventario.finalizada_em.isoformat() if inventario.finalizada_em else None,
        "enviada": acesso.ciclo == inventario.ciclo and AcessoInventario.objects.filter(pk=acesso.pk, enviada_em__isnull=False).exists(),
        "pode_escrever": inventario.finalizada_em is None and acesso.ciclo == inventario.ciclo and not AcessoInventario.objects.filter(pk=acesso.pk, enviada_em__isnull=False).exists()}


@privado
@csrf_protect
@require_POST
def finalizar(request):
    acesso = acesso_sessao(request)
    try:
        services.finalizar_conferencia(acesso.pk, request.POST.get("versao"), token_hash=request.inventario_session.get("inventario_token_hash", ""))
        if "text/html" in request.headers.get("Accept", ""):
            return redirect("inventario:mobile")
        estado = estado_conferencia(acesso)
        mensagem = "Conferência finalizada e enviada ao responsável." if estado["finalizada"] else "Sua atribuição foi enviada. A conferência geral aguarda as demais atribuições."
        return JsonResponse({"conferencia": estado, "mensagem": mensagem})
    except (ValueError, ObjectDoesNotExist) as exc:
        return JsonResponse({"erro": str(exc)}, status=409)


@privado
@csrf_protect
@require_POST
def sinalizar(request, rodada_id):
    acesso = acesso_sessao(request)
    try:
        services.sinalizar_fisico(rodada_id, request.POST.get("descricao"), request.POST.get("chave"),
            acesso_id=acesso.pk, token_hash=request.inventario_session.get("inventario_token_hash", ""))
        if "text/html" in request.headers.get("Accept", ""):
            return redirect("inventario:mobile")
        return JsonResponse({"mensagem": "Sinalização enviada ao responsável. O estoque não foi alterado."})
    except (ValueError, ObjectDoesNotExist) as exc:
        if "text/html" in request.headers.get("Accept", ""):
            return render(request, "inventario/erro_contagem.html", {"erro": str(exc)}, status=409)
        return JsonResponse({"erro": str(exc)}, status=409)


@privado
@csrf_protect
@require_POST
def contar(request, rodada_id):
    acesso = acesso_sessao(request)
    try:
        acao = request.POST.get("acao")
        if acao not in {"salvar", "ok"}:
            raise ValueError("Ação inválida.")
        rodada = services.salvar_contagem(acesso.pk, rodada_id, request.POST.get("versao", "-1"),
            request.POST.get("embalagens"), request.POST.get("avulsos"), confirmar=acao == "ok",
            token_hash=request.inventario_session.get("inventario_token_hash", ""))
        if "text/html" in request.headers.get("Accept", ""):
            return redirect("inventario:mobile")
        return JsonResponse({"produto": payload_cego(rodada), "conferencia": estado_conferencia(acesso),
            "mensagem": "Produto conferido. Use Recontar produto para corrigir antes do envio final." if rodada.ok_em else "Contagem guardada. Confirme o OK ou use Continuar contagem para editar."})
    except (ValueError, ObjectDoesNotExist) as exc:
        if "text/html" in request.headers.get("Accept", ""):
            return render(request, "inventario/erro_contagem.html", {"erro": str(exc)}, status=409)
        return JsonResponse({"erro": str(exc)}, status=409)


@privado
@require_http_methods(["GET"])
def previa_conversao(request, rodada_id):
    """Read-only preview using the same frozen rules as the persisted count."""
    acesso = acesso_sessao(request)
    rodada = get_object_or_404(RodadaContagem.objects.select_related("item"),
        pk=rodada_id, atribuicao__acesso=acesso, encerrada_em__isnull=True,
        item__inventario__ciclo=acesso.ciclo)
    try:
        total = converter(rodada.item.cadastro, request.GET.get("embalagens"), request.GET.get("avulsos"))[3]
        return JsonResponse({"total": format(total, ".3f").replace(".", ",")})
    except ValueError as exc:
        return JsonResponse({"erro": str(exc)}, status=400)


@privado
@csrf_protect
@require_POST
def cancelar(request, inventario_id):
    services.permitir(request.user, "gerenciar_inventario")
    get_object_or_404(Inventario, pk=inventario_id)
    if request.POST.get("confirmacao") != "sim":
        return HttpResponse("Confirme o cancelamento.", status=400)
    try:
        services.cancelar_conferencia(request.user, inventario_id)
    except ValueError as exc:
        messages.error(request, str(exc))
    else:
        messages.success(request, f"Conferência nº {inventario_id} cancelada. Histórico preservado e estoque inalterado.")
        return redirect("inventario:lista")
    return redirect("inventario:detalhe", inventario_id)


@privado
@require_http_methods(["GET"])
def auditoria_cancelada(request, inventario_id):
    services.permitir(request.user, "gerenciar_inventario")
    inventario = get_object_or_404(Inventario, pk=inventario_id)
    if not inventario.cancelada_em:
        return redirect("inventario:detalhe", inventario_id)
    return detalhe(request, inventario_id, consulta_auditoria=True)


@privado
@csrf_protect
@require_POST
def editar_contagem_admin(request, inventario_id, rodada_id):
    from . import contagem_admin
    services.permitir(request.user, 'revisar_inventario')
    get_object_or_404(RodadaContagem, pk=rodada_id, item__inventario_id=inventario_id)
    destino = reverse('inventario:detalhe', args=[inventario_id]) + '#titulo-resumo'
    try:
        if request.POST.get('confirmacao') != 'sim':
            raise ValueError('Confirme a contagem para registrar o OK.')
        contagem_admin.confirmar(request.user, inventario_id, rodada_id,
            request.POST.get('autorizacao'), request.POST.get('embalagens'), request.POST.get('avulsos'))
    except (ValueError, PermissionDenied) as exc:
        if 'application/json' in request.headers.get('Accept', ''):
            return JsonResponse({'erro': str(exc)}, status=403 if isinstance(exc, PermissionDenied) else 409)
        messages.error(request, str(exc))
    else:
        if 'application/json' in request.headers.get('Accept', ''):
            return JsonResponse({'destino': destino})
        messages.success(request, 'Contagem administrativa confirmada. OK anterior preservado e estoque inalterado.')
    return redirect(destino)


@privado
@csrf_protect
@require_POST
def finalizar_administrativamente(request, inventario_id):
    from . import encerramento
    get_object_or_404(Inventario, pk=inventario_id)
    try:
        dados = encerramento.finalizar(request.user, inventario_id, request.POST.get("previa"), request.POST.get("confirmacao"))
    except ValueError as exc:
        messages.error(request, str(exc))
        return redirect("inventario:detalhe", inventario_id=inventario_id)
    if dados.get("ja_finalizada"):
        messages.info(request, f"Conferencia nº {inventario_id} ja finalizada. Nenhum ajuste foi executado.")
    else:
        messages.success(request, f"Conferencia nº {inventario_id} finalizada. {dados['aplicados']} produtos com ajuste ja aplicado; "
            f"{dados['sem_ajuste']} permanecem sem ajuste aplicado, sendo {dados['diferencas_pendentes']} com diferenca registrada. "
            "Nenhum saldo foi alterado pelo encerramento. Bloqueios e historico preservados.")
    return redirect("inventario:lista")
