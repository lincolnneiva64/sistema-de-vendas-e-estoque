import csv
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
from django.db.models import Count, F, Q, OuterRef, Subquery
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
from .erros import ErroAcesso
from .models import Inventario, RodadaContagem, AcessoInventario, DecisaoAjuste, SinalizacaoFisica
from .apresentacao import entrada, quantidade
from .painel import resumo_painel
from .selecao import itens_incluidos, rodadas_incluidas, salvar_selecao
from .rastreabilidade import ORIGENS
from .titulos import preparar_titulos


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
    atuais = rodadas_incluidas().filter(pk=Subquery(ultima), encerrada_em__isnull=True, item__decisao__isnull=True)
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
    em_andamento = list(inventarios.filter(finalizada_em__isnull=True))
    finalizados = list(inventarios.filter(finalizada_em__isnull=False))
    preparar_titulos(em_andamento + finalizados)
    return render(request, "inventario/lista.html", {
        "em_andamento": em_andamento,
        "finalizados": finalizados, "erro": erro,
        "categorias": produtos.exclude(categoria__isnull=True).exclude(categoria="").values_list("categoria", flat=True).distinct().order_by("categoria"),
        "fornecedores": Fornecedor.objects.filter(produtos_vinculados__ativo=True).distinct().order_by("nome"),
        "produtos": produtos, "resumo": resumo_inicial(), "pode_criar": request.user.has_perm("inventario.gerenciar_inventario")})


def linhas_revisao(inventario):
    linhas = []
    for item in itens_incluidos(inventario).select_related("produto").order_by("cadastro__nome", "pk"):
        rodada = item.rodadas.order_by("-numero").first()
        conciliacoes = {c.movimento_id: c for c in rodada.conciliacoes.filter(conciliado_em__gte=rodada.ok_em).select_related("responsavel")} if rodada.ok_em else {}
        todos_movimentos = list(services.movimentos_relevantes(rodada))
        sinalizacoes = list(SinalizacaoFisica.objects.filter(rodada__item=item).select_related("acesso", "usuario", "resolvida_por").order_by("criado_em"))
        diferenca = rodada.contado_base - rodada.esperado if rodada.ok_em else None
        valor = (diferenca * item.custo_base).quantize(Decimal("0.01")) if diferenca is not None and item.custo_base is not None else None
        inseguro = False
        try:
            services.validar_cadeia(rodada, item.produto)
            bloqueio = "Revisão física necessária" if rodada.revisao_sequencia != services.sequencia(item.produto_id) else "Revisão registrada"
        except ValueError as exc:
            bloqueio = str(exc)
            inseguro = rodada.ok_em is not None
        decisao = DecisaoAjuste.objects.filter(item=item).first()
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
            "historico": item.rodadas.prefetch_related("confirmacoes").order_by("numero"), "eventos": inventario.eventos.filter(rodada__item=item).select_related("usuario", "acesso").order_by("criado_em")})
    return sorted(linhas, key=lambda l: (0 if l["destaque"] != "neutro" else 1, l["item"].cadastro["nome"]))


@privado
@require_http_methods(["GET", "POST"])
def detalhe(request, inventario_id):
    gestor(request)
    inventario = get_object_or_404(Inventario, pk=inventario_id)
    erro, link, whatsapp_url, destinatario, sucesso = "", "", "", "", ""
    if request.method == "POST":
        try:
            acao = request.POST.get("acao")
            rodada_id = request.POST.get("rodada")
            if acao in {"recontar", "conciliar", "revisar", "aprovar", "sinalizar"}:
                get_object_or_404(RodadaContagem, pk=rodada_id, item__inventario=inventario)
            if acao == "atualizar":
                ajustes.atualizar(request.user, inventario.pk, request.POST.get("previa"),
                    confirmacao=request.POST.get("confirmacao") == "sim",
                    observacao=request.POST.get("observacao", ""))
                messages.success(request, "Atualização concluída. Os saldos aplicados estão registrados no resultado e no histórico.")
                return redirect(reverse("inventario:detalhe", args=[inventario.pk]) + "#titulo-resumo")
            elif acao == "selecao":
                selecao = salvar_selecao(request.user, inventario.pk, request.POST.getlist("participantes"),
                    request.POST.get("versao"), request.POST.get("motivo"))
                pendencias = getattr(selecao, "pendencias_conclusao", None)
                if selecao.finalizada_em:
                    sucesso = "Seleção salva. Conferência finalizada com os OKs e envios já registrados."
                elif pendencias:
                    messages.warning(request, "Seleção salva. Conferência permanece aberta: " + " ".join(pendencias))
            elif acao == "acesso":
                acesso, token = services.distribuir_produtos(request.user, inventario.pk, request.POST.get("funcionario"),
                    request.POST.getlist("rodadas"), request.POST.get("horas", 24), request.POST.get("versao"),
                    manter_nao_selecionados=request.POST.get("manter_nao_selecionados") == "sim")
                link = request.build_absolute_uri(reverse("inventario:convite", args=[token]))
                sucesso = "Atribuição concluída. Copie o link abaixo e entregue ao funcionário."
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
    prever = (request.method == "GET" and request.GET.get("atualizar") == "prever"
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
    return render(request, "inventario/detalhe.html", {"inventario": inventario, "linhas": linhas,
        "resumo_painel": resumo, "consultado_em": timezone.now(),
        "atualizacao": atualizacao, "previa_lote": previa_lote,
        "erro": erro, "link": link, "sucesso": sucesso, "whatsapp_url": whatsapp_url, "destinatario": destinatario, "funcionarios": Funcionario.objects.filter(ativo=True).order_by("nome"),
        "eventos_selecao": inventario.eventos.filter(acao__in=["produto_retirado", "produto_reincluido"]).select_related("usuario").order_by("pk"),
        "rodadas_disponiveis": rodadas_incluidas(inventario).filter(encerrada_em__isnull=True, atribuicao__isnull=True).count(),
        "acessos": acessos, "ajustes_habilitados": getattr(settings, "INVENTARIO_AJUSTES_HABILITADOS", False),
        "pode_gerenciar": request.user.has_perm("inventario.gerenciar_inventario"),
        "pode_revisar": request.user.has_perm("inventario.revisar_inventario"),
        "pode_aprovar": request.user.has_perm("inventario.aprovar_ajuste")})


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
    rodadas = rodadas_incluidas().filter(atribuicao__acesso=acesso, encerrada_em__isnull=True).select_related("item").order_by("pk")
    estado = estado_conferencia(acesso)
    if acesso.ciclo != acesso.inventario.ciclo:
        rodadas = rodadas.none()
    produtos = [dict(payload_cego(r), chave_sinalizacao=str(uuid4())) for r in rodadas]
    if "application/json" in request.headers.get("Accept", ""):
        return JsonResponse({"produtos": produtos, "conferencia": estado, "csrf": get_token(request),
                             "acesso_id": acesso.pk})
    return render(request, "inventario/mobile.html", {"funcionario": acesso.funcionario_nome, "conferencia": estado,
        "produtos": produtos, "acesso_id": acesso.pk, "conferencia_id": acesso.inventario_id})


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
