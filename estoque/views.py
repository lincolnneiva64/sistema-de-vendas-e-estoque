import json
import textwrap
import unicodedata
from difflib import SequenceMatcher
from io import BytesIO
from decimal import Decimal, InvalidOperation
from pathlib import Path
from datetime import date, timedelta

from django.conf import settings
from django.db import transaction
from django.db.models import Q, Sum, Max
from urllib.parse import quote, urlencode
from django.shortcuts import render, redirect, get_object_or_404
from django.urls import reverse
from django.db.models import Case, When, Value, IntegerField, F, Count
from .forms import CategoriaForm, ClienteForm, FuncionarioForm, PixRecebidoForm, ProdutoForm, UnidadeForm
from .models import Categoria, Cliente, ContaReceber, CreditoCliente, EntregaChecklistItem, EntregaRota, EntregaRotaItem, EventoVenda, Funcionario, ItemVenda, PixRecebido, Produto, RecebimentoContaReceber, Unidade, Venda
from .utils_pix import analisar_comprovante_pix
from django.contrib import messages
from django.http import Http404, HttpResponse, JsonResponse
from django.utils.dateparse import parse_date
from django.utils import timezone
from django.utils.http import url_has_allowed_host_and_scheme
from django.views.decorators.csrf import ensure_csrf_cookie
from django.views.decorators.http import require_POST
from PIL import Image, ImageDraw, ImageFont
from uuid import uuid4

MENSAGEM_CLIENTE_DUPLICADO = (
    "Ja existe um cliente parecido cadastrado. Verifique antes de cadastrar novamente."
)


def _tem_pix_em_atencao():
    return PixRecebido.objects.filter(
        status__in=[
            PixRecebido.STATUS_PENDENTE,
            PixRecebido.STATUS_NAO_IDENTIFICADO,
        ]
    ).exists()


def _pix_duplicado_pendente(dados):
    nome_pagador = normalizar_texto_cliente(dados.get("nome_pagador"))
    valor = dados.get("valor")
    data_pagamento = dados.get("data_pagamento")
    if not (nome_pagador and valor and data_pagamento):
        return None

    inicio = data_pagamento - timedelta(minutes=5)
    fim = data_pagamento + timedelta(minutes=5)
    candidatos = PixRecebido.objects.filter(
        status=PixRecebido.STATUS_PENDENTE,
        valor=valor,
        data_pagamento__range=(inicio, fim),
    ).only("id", "nome_pagador", "valor", "data_pagamento")
    for pix in candidatos:
        if normalizar_texto_cliente(pix.nome_pagador) == nome_pagador:
            return pix
    return None


def normalizar_texto_cliente(valor):
    texto = " ".join(str(valor or "").strip().lower().split())
    texto = unicodedata.normalize("NFD", texto)
    return "".join(caractere for caractere in texto if unicodedata.category(caractere) != "Mn")


def normalizar_documento_cliente(valor):
    return "".join(caractere for caractere in str(valor or "") if caractere.isdigit())


def _valor_total_recebimento_cliente(recebimento):
    observacao = recebimento.observacao or ""
    if "Total recebido:" not in observacao:
        return (recebimento.valor or Decimal("0.00")).quantize(Decimal("0.01"))

    trecho_total = observacao.split("Total recebido:", 1)[1]
    trecho_total = trecho_total.split("Aplicado nesta conta:", 1)[0].strip().rstrip(".")
    try:
        return _decimal_do_front(trecho_total or recebimento.valor, "0.01")
    except ValueError:
        return (recebimento.valor or Decimal("0.00")).quantize(Decimal("0.01"))


def textos_parecidos_cliente(valor_a, valor_b, minimo=0.88):
    texto_a = normalizar_texto_cliente(valor_a)
    texto_b = normalizar_texto_cliente(valor_b)
    if not texto_a or not texto_b:
        return False
    return SequenceMatcher(None, texto_a, texto_b).ratio() >= minimo


def detectar_cliente_duplicado(cliente):
    candidatos = Cliente.objects.all()
    if cliente.pk:
        candidatos = candidatos.exclude(pk=cliente.pk)

    documento = normalizar_documento_cliente(cliente.cpf_cnpj)
    if documento:
        duplicado = None
        for candidato in candidatos.only("id", "nome", "cpf_cnpj"):
            if normalizar_documento_cliente(candidato.cpf_cnpj) == documento:
                duplicado = candidato
                break
        if duplicado:
            return duplicado, "cpf_cnpj"

    whatsapp_normalizado = Cliente.normalizar_whatsapp(cliente.whatsapp)
    if whatsapp_normalizado:
        duplicado = candidatos.filter(whatsapp_normalizado=whatsapp_normalizado).first()
        if not duplicado:
            for candidato in candidatos.only("id", "nome", "whatsapp"):
                if Cliente.normalizar_whatsapp(candidato.whatsapp) == whatsapp_normalizado:
                    duplicado = candidato
                    break
        if duplicado:
            return duplicado, "whatsapp"

    if documento or whatsapp_normalizado:
        return None, ""

    nome = normalizar_texto_cliente(cliente.nome)
    apelido = normalizar_texto_cliente(cliente.apelido_nome_conhecido)
    localidade = normalizar_texto_cliente(cliente.bairro or cliente.cidade)
    if not nome:
        return None

    for candidato in candidatos.only("id", "nome", "apelido_nome_conhecido", "bairro", "cidade"):
        candidato_nome = normalizar_texto_cliente(candidato.nome)
        if candidato_nome == nome:
            return candidato, "nome"

        if not localidade:
            continue

        candidato_localidade = candidato.bairro or candidato.cidade
        if not textos_parecidos_cliente(localidade, candidato_localidade, minimo=0.84):
            continue

        nome_parecido = textos_parecidos_cliente(nome, candidato_nome, minimo=0.88)
        apelido_parecido = bool(apelido) and textos_parecidos_cliente(
            apelido,
            candidato.apelido_nome_conhecido,
            minimo=0.86,
        )
        if nome_parecido and (not apelido or apelido_parecido):
            return candidato, "nome"

    return None, ""


def encontrar_cliente_duplicado(cliente):
    duplicado, _campo = detectar_cliente_duplicado(cliente)
    return duplicado


def montar_checklist_url(request, rota_id):
    checklist_path = reverse("estoque:entrega_rota_checklist", kwargs={"pk": rota_id})
    checklist_base_url = getattr(settings, "CHECKLIST_BASE_URL", "").rstrip("/")
    if checklist_base_url:
        return f"{checklist_base_url}{checklist_path}"
    return f"{request.scheme}://{request.get_host()}{checklist_path}"


def montar_checklist_cliente_url(request, rota_id, venda_id, rota_item_id=None):
    if rota_item_id:
        checklist_path = reverse(
            "estoque:entrega_rota_checklist_item",
            kwargs={"rota_id": rota_id, "rota_item_id": rota_item_id},
        )
    else:
        checklist_path = reverse(
            "estoque:entrega_rota_checklist_cliente",
            kwargs={"rota_id": rota_id, "venda_id": venda_id},
        )
    checklist_base_url = getattr(settings, "CHECKLIST_BASE_URL", "").rstrip("/")
    if checklist_base_url:
        return f"{checklist_base_url}{checklist_path}"
    return f"{request.scheme}://{request.get_host()}{checklist_path}"


def montar_url_publica(request, path):
    base_url = getattr(settings, "CHECKLIST_BASE_URL", "").rstrip("/")
    if base_url:
        return f"{base_url}{path}"
    return request.build_absolute_uri(path)


CHECKLIST_FASE_MARKERS = {
    "carregamento": "[checklist_carregamento_salvo]",
    "entrega": "[checklist_entrega_salva]",
}


def checklist_fase_salva(item_rota, fase):
    marker = CHECKLIST_FASE_MARKERS.get(fase)
    return bool(marker and marker in (item_rota.observacao or ""))


def marcar_checklist_fase_salva(item_rota, fase):
    marker = CHECKLIST_FASE_MARKERS.get(fase)
    if not marker or checklist_fase_salva(item_rota, fase):
        return

    observacao = (item_rota.observacao or "").strip()
    item_rota.observacao = f"{observacao}\n{marker}".strip() if observacao else marker
    item_rota.save(update_fields=["observacao"])


def observacao_bloqueia_exclusao(item_rota):
    observacao = (item_rota.observacao or "").strip()
    if not observacao:
        return False
    if any(marker in observacao for marker in CHECKLIST_FASE_MARKERS.values()):
        return True
    if item_rota.is_pendencia and observacao.startswith("Pendencia incluida da rota #"):
        return False
    return True


def checklist_item_processado(checklist):
    return checklist.carregado or checklist.entregue


def rota_tem_evento_checklist_processado(rota):
    venda_ids = [item_rota.venda_id for item_rota in rota.itens.all()]
    if not venda_ids:
        return False
    return EventoVenda.objects.filter(
        venda_id__in=venda_ids,
        canal="whatsapp_checklist",
        descricao__icontains=f"rota/entrega #{rota.id}",
    ).exists()


def rota_pode_ser_excluida(rota):
    if rota.status != EntregaRota.STATUS_ABERTA:
        return False

    for item_rota in rota.itens.all():
        if (
            item_rota.status != EntregaRotaItem.STATUS_PENDENTE
            or item_rota.conferido_cliente
            or item_rota.entrega_concluida
            or observacao_bloqueia_exclusao(item_rota)
        ):
            return False

        for checklist in item_rota.checklist_itens.all():
            if checklist_item_processado(checklist):
                return False

    return not rota_tem_evento_checklist_processado(rota)


def formatar_nome_rota(valor):
    texto = " ".join(str(valor or "").strip().split())
    if not texto:
        return ""

    palavras_minusculas = {"da", "de", "do", "das", "dos", "e"}
    partes = []
    for indice, palavra in enumerate(texto.lower().split()):
        partes.append(palavra if indice > 0 and palavra in palavras_minusculas else palavra.capitalize())
    return " ".join(partes)


def localidade_principal_rota(vendas_ordenadas):
    contagem = {}
    nomes = {}

    for _, __, venda in vendas_ordenadas:
        cliente = venda.cliente
        localidade = ""
        if cliente:
            localidade = (cliente.bairro or "").strip() or (cliente.cidade or "").strip()
        localidade = formatar_nome_rota(localidade)
        if not localidade:
            continue

        chave = localidade.lower()
        contagem[chave] = contagem.get(chave, 0) + 1
        nomes.setdefault(chave, localidade)

    if not contagem:
        return ""

    maior_contagem = max(contagem.values())
    for _, __, venda in vendas_ordenadas:
        cliente = venda.cliente
        localidade = ""
        if cliente:
            localidade = (cliente.bairro or "").strip() or (cliente.cidade or "").strip()
        chave = formatar_nome_rota(localidade).lower()
        if contagem.get(chave) == maior_contagem:
            return nomes[chave]

    return ""


def listar_pendencias_entrega(limite=None):
    pendencias = []
    pendencias_reprogramadas = set(
        EntregaChecklistItem.objects.filter(
            rota_item__is_pendencia=True,
            rota_item__origem_pendencia_id__isnull=False,
        ).values_list("rota_item__origem_pendencia_id", "item_venda_id")
    )
    itens_rota = (
        EntregaRotaItem.objects.select_related("rota", "venda", "venda__cliente", "origem_pendencia")
        .prefetch_related("checklist_itens__item_venda__produto")
        .order_by("-rota__data", "-rota_id", "ordem_entrega", "id")
    )

    for item_rota in itens_rota:
        checklists = checklists_validos_rota_item(item_rota)
        itens_pendentes = [checklist for checklist in checklists if not checklist.entregue]
        entrega_salva = checklist_fase_salva(item_rota, "entrega")
        entrega_processada = (
            entrega_salva
            or item_rota.conferido_cliente
            or item_rota.entrega_concluida
            or item_rota.status in {
                EntregaRotaItem.STATUS_PARCIAL,
                EntregaRotaItem.STATUS_CANCELADA,
            }
        )
        if not entrega_processada and not (item_rota.is_pendencia and itens_pendentes):
            continue

        if not itens_pendentes and item_rota.status not in {
            EntregaRotaItem.STATUS_PARCIAL,
            EntregaRotaItem.STATUS_CANCELADA,
        }:
            continue

        if itens_pendentes:
            for checklist in itens_pendentes:
                item_venda = checklist.item_venda
                if (
                    not item_rota.is_pendencia
                    and (item_rota.id, item_venda.id if item_venda else None) in pendencias_reprogramadas
                ):
                    continue
                cliente = item_rota.venda.cliente
                produto = item_venda.produto.nome if item_venda and item_venda.produto else "Produto nao identificado"
                pendencias.append({
                    "id": checklist.id,
                    "item_rota_id": item_rota.id,
                    "item_venda_id": item_venda.id if item_venda else "",
                    "cliente": cliente.nome if cliente else "Consumidor",
                    "venda": item_rota.venda,
                    "rota": item_rota.rota,
                    "data": item_rota.rota.data,
                    "produto": produto,
                    "quantidade": item_venda.quantidade if item_venda else "",
                    "unidade": item_venda.unidade if item_venda else "",
                    "localidade": (
                        f"{cliente.bairro or ''} {cliente.cidade or ''}".strip()
                        if cliente
                        else ""
                    ),
                    "origem": f"Venda #{item_rota.venda_id} - Rota #{item_rota.rota_id}",
                    "status": "Item nao entregue",
                })
        else:
            cliente = item_rota.venda.cliente
            pendencias.append({
                "id": "",
                "item_rota_id": item_rota.id,
                "item_venda_id": "",
                "cliente": cliente.nome if cliente else "Consumidor",
                "venda": item_rota.venda,
                "rota": item_rota.rota,
                "data": item_rota.rota.data,
                "produto": "",
                "quantidade": "",
                "unidade": "",
                "localidade": (
                    f"{cliente.bairro or ''} {cliente.cidade or ''}".strip()
                    if cliente
                    else ""
                ),
                "origem": f"Venda #{item_rota.venda_id} - Rota #{item_rota.rota_id}",
                "status": item_rota.get_status_display(),
            })

        if limite and len(pendencias) >= limite:
            return pendencias[:limite]

    return pendencias


def pendencias_sugeriveis_entrega():
    return [pendencia for pendencia in listar_pendencias_entrega() if pendencia.get("id")]


def pendencias_checklist_validas(checklist_ids):
    ids = [int(checklist_id) for checklist_id in checklist_ids if str(checklist_id).isdigit()]
    if not ids:
        return []
    ids_selecionados = set(ids)

    checklists = (
        EntregaChecklistItem.objects.filter(pk__in=ids, entregue=False)
        .select_related(
            "item_venda",
            "item_venda__produto",
            "rota_item",
            "rota_item__rota",
            "rota_item__venda",
            "rota_item__venda__cliente",
        )
        .order_by("rota_item__rota__data", "rota_item_id", "item_venda_id")
    )
    pendencias_disponiveis = {
        pendencia["id"]
        for pendencia in pendencias_sugeriveis_entrega()
    }
    return [
        checklist
        for checklist in checklists
        if checklist.id in ids_selecionados and checklist.id in pendencias_disponiveis
    ]


def resumo_pendencia_rota_item(item_rota):
    if not item_rota.is_pendencia:
        return ""

    partes = []
    checklists = getattr(item_rota, "checklists_ordenados", None)
    if checklists is None:
        checklists = checklists_validos_rota_item(item_rota)

    for checklist in checklists:
        item_venda = checklist.item_venda
        produto = item_venda.produto.nome if item_venda and item_venda.produto else "Produto nao identificado"
        quantidade = item_venda.quantidade if item_venda else ""
        unidade = item_venda.unidade if item_venda else ""
        partes.append(f"{produto} / {quantidade} {unidade}".strip())

    return "; ".join(partes)


def item_venda_ids_pendencia_origem(item_rota):
    if not item_rota.is_pendencia or not item_rota.origem_pendencia_id:
        return None

    origem = item_rota.origem_pendencia
    if not origem.is_pendencia:
        return None

    ids_origem = item_venda_ids_pendencia_origem(origem)
    if ids_origem is not None:
        return ids_origem

    return set(origem.checklist_itens.values_list("item_venda_id", flat=True))


def checklists_validos_rota_item(item_rota):
    checklists = list(item_rota.checklist_itens.all())
    item_venda_ids_validos = item_venda_ids_pendencia_origem(item_rota)
    if item_venda_ids_validos is None:
        return checklists

    return [
        checklist
        for checklist in checklists
        if checklist.item_venda_id in item_venda_ids_validos
    ]


def calcular_total_itens_venda(venda, excluir_item_id=None):
    total = sum(
        (item.valor_total or Decimal("0.00"))
        for item in venda.itens.all()
        if excluir_item_id is None or item.id != excluir_item_id
    )
    return Decimal(total).quantize(Decimal("0.01"))


def recalcular_total_venda(venda):
    venda.total = calcular_total_itens_venda(venda)
    venda.save(update_fields=["total", "atualizado_em"])
    return venda.total


def chave_cliente_entrega(venda):
    if venda and venda.cliente_id:
        return f"cliente:{venda.cliente_id}"
    return f"venda:{venda.id if venda else ''}"


def checklists_pendencia_unicos(checklists):
    checklists_unicos = []
    item_venda_ids = set()
    for checklist in checklists:
        if checklist.item_venda_id in item_venda_ids:
            continue
        checklists_unicos.append(checklist)
        item_venda_ids.add(checklist.item_venda_id)
    return checklists_unicos


def checklists_pendencia_selecionados(checklists, ids_selecionados):
    return [
        checklist
        for checklist in checklists_pendencia_unicos(checklists)
        if checklist.id in ids_selecionados and not checklist.entregue
    ]


def ordem_postada(valor, padrao=9999):
    try:
        return int(valor or padrao)
    except (TypeError, ValueError):
        return padrao


def home(request):
    produto_edicao = None

    # POST: criar/editar/excluir
    if request.method == "POST":
        acao = request.POST.get("acao")

        # EXCLUIR
        if acao == "excluir":
            excluir_id = request.POST.get("excluir_id")
            if excluir_id:
                produto = Produto.objects.filter(id=excluir_id).first()
                if produto:
                    produto.excluido = True
                    produto.save()
            return redirect("estoque:home")
        # CRIAR / EDITAR
        produto_id = request.POST.get("produto_id")
        if produto_id:
            produto_edicao = get_object_or_404(Produto, id=produto_id)
            form = ProdutoForm(request.POST, instance=produto_edicao)
        else:
            form = ProdutoForm(request.POST)

        if form.is_valid():
            produto = form.save()
            return redirect(f"{reverse('estoque:home')}?produto_destacado={produto.id}")

    # GET: carregar formulário de edição
    editar_id = request.GET.get("edit")
    if editar_id:
        produto_edicao = get_object_or_404(Produto, id=editar_id)
        form = ProdutoForm(instance=produto_edicao)
    else:
        form = ProdutoForm()

        # Busca + Filtro
    q = request.GET.get("q", "").strip()
    filtro = request.GET.get("f", "todos").strip().lower()

    produtos_base = Produto.objects.filter(excluido=False).annotate(
        prioridade=Case(
            When(quantidade=0, then=Value(0)),
            When(quantidade__gt=0, quantidade__lte=F("estoque_minimo"), then=Value(1)),
            When(
                quantidade__gt=F("estoque_minimo"),
                quantidade__lte=F("estoque_minimo") + Value(5),
                then=Value(2),
            ),
            default=Value(3),
            output_field=IntegerField(),
        )
    ).order_by("nome")

    produtos = produtos_base

    if q:
        produtos = produtos.filter(
            Q(nome__icontains=q) |
            Q(codigo__icontains=q) |
            Q(categoria__icontains=q)
        )

    if filtro == "zerado":
        produtos = produtos.filter(quantidade=0)
    elif filtro == "critico":
        produtos = produtos.filter(
            quantidade__gt=0,
            quantidade__lte=F("estoque_minimo"),
        )
    elif filtro == "limite":
        produtos = produtos.filter(
            quantidade__gt=F("estoque_minimo"),
            quantidade__lte=F("estoque_minimo") + Value(5),
        )
    elif filtro == "normal":
        produtos = produtos.filter(
            quantidade__gt=F("estoque_minimo") + Value(5),
        )

    # Contadores
    total_produtos = produtos_base.count()
    zerado_count = produtos_base.filter(quantidade=0).count()
    criticos_count = produtos_base.filter(
        quantidade__gt=0,
        quantidade__lte=F("estoque_minimo"),
    ).count()
    limite_count = produtos_base.filter(
        quantidade__gt=F("estoque_minimo"),
        quantidade__lte=F("estoque_minimo") + Value(5),
    ).count()
    normal_count = produtos_base.filter(
        quantidade__gt=F("estoque_minimo") + Value(5),
    ).count()

    # Financeiro
    total_investido = sum((p.preco_compra or 0) * (p.quantidade or 0) for p in produtos)
    total_faturamento = sum((p.preco_venda or 0) * (p.quantidade or 0) for p in produtos)
    lucro_bruto = total_faturamento - total_investido
    margem_percent = (lucro_bruto / total_faturamento * 100) if total_faturamento else 0

    return render(
        request,
        "estoque/home.html",
        {
            "produtos": produtos,
            "produto_edicao": produto_edicao,
            "form": form,
            "q": q,
            "filtro": filtro,
            "total_produtos": total_produtos,
            "zerado_count": zerado_count,
            "criticos_count": criticos_count,
            "limite_count": limite_count,
            "normal_count": normal_count,
            "total_investido": total_investido,
            "total_faturamento": total_faturamento,
            "lucro_bruto": lucro_bruto,
            "margem_percent": margem_percent,
        }
    )

def cadastrar_produto(request):
    criar_mais_produtos = request.GET.get("criar_mais_produtos") == "1"

    if request.method == "POST":
        criar_mais_produtos = request.POST.get("criar_mais_produtos") == "on"
        form = ProdutoForm(request.POST)
        if form.is_valid():
            produto = form.save()
            if criar_mais_produtos:
                return redirect(f"{reverse('estoque:cadastrar_produto')}?criar_mais_produtos=1")
            messages.success(request, f'Produto "{produto.nome}" cadastrado com sucesso!')
            return redirect(f"{reverse('estoque:home')}?produto_destacado={produto.id}")
        else:
            print("ERROS DO FORM:", form.errors)
            print("DADOS RECEBIDOS:", request.POST)
    else:
        form = ProdutoForm()
        
    return render(
        request,
        "estoque/cadastrar_produto.html",
        {
            "form": form,
            "produtos": Produto.objects.all(),
            "criar_mais_produtos": criar_mais_produtos,
        },
    )
def cadastrar_unidade_json_antigo(request):
    if request.method == "POST":
        nome = request.POST.get("nome", "").strip()
        sigla = request.POST.get("sigla", "").strip().upper()
        descricao = request.POST.get("descricao", "").strip()

        if not nome or not sigla:
            return JsonResponse({
                "sucesso": False,
                "mensagem": "Nome e Sigla são obrigatórios."
            })

        Unidade.objects.create(
            nome=nome,
            sigla=sigla,
            descricao=descricao,
            ativa=True
        )

        return JsonResponse({
            "sucesso": True,
            "mensagem": "Unidade cadastrada com sucesso!"
        })

    return JsonResponse({
        "sucesso": False,
        "mensagem": "Método inválido."
    })
def cadastrar_unidade(request):
    if request.method == "POST":
        nome = request.POST.get("nome", "").strip()
        sigla = request.POST.get("sigla", "").strip().upper()
        descricao = request.POST.get("descricao", "").strip()

        if nome and sigla:
            unidade_ja_existe = (
                Unidade.objects.filter(sigla=sigla).exists()
                or Unidade.objects.filter(nome__iexact=nome).exists()
            )

            if not unidade_ja_existe:
                Unidade.objects.create(
                    nome=nome,
                    sigla=sigla,
                    descricao=descricao,
                    ativa=True,
                )

    return redirect("estoque:cadastrar_produto")

def unidades_produto(request):
    termo = request.GET.get("q", "").strip()
    unidade_selecionada = None

    unidades = Unidade.objects.all().order_by("-ativa", "sigla")

    if termo:
        unidades = unidades.filter(
            Q(nome__icontains=termo) |
            Q(sigla__icontains=termo) |
            Q(descricao__icontains=termo)
        )

    if request.method == "POST":
        acao = request.POST.get("acao")
        unidade_id = request.POST.get("unidade_id")

        if acao == "alternar_status" and unidade_id:
            unidade = get_object_or_404(Unidade, pk=unidade_id)
            unidade.ativa = request.POST.get("ativa") == "1"
            unidade.save(update_fields=["ativa"])
            status = "ativada" if unidade.ativa else "desativada"
            messages.success(request, f'Unidade "{unidade.sigla}" {status} com sucesso.')
            params = {"unidade": unidade.id}
            if termo:
                params["q"] = termo
            destino = f"{reverse('estoque:unidades_produto')}?{urlencode(params)}"
            return redirect(destino)

        if unidade_id:
            unidade_selecionada = get_object_or_404(Unidade, pk=unidade_id)
            form = UnidadeForm(request.POST, instance=unidade_selecionada)
        else:
            form = UnidadeForm(request.POST)

        if form.is_valid():
            unidade = form.save()
            messages.success(request, f'Unidade "{unidade.sigla}" salva com sucesso.')
            return redirect(f"{reverse('estoque:unidades_produto')}?unidade={unidade.id}")
        messages.error(request, "Revise os campos destacados para salvar a unidade.")
    else:
        unidade_id = request.GET.get("unidade")
        if unidade_id:
            unidade_selecionada = get_object_or_404(Unidade, pk=unidade_id)
            form = UnidadeForm(instance=unidade_selecionada)
        else:
            form = UnidadeForm(initial={"ativa": True})

    unidades = list(unidades)
    for unidade in unidades:
        unidade.produtos_em_uso = Produto.objects.filter(excluido=False).filter(
            Q(unidade_compra=unidade.sigla) |
            Q(unidade_venda_1=unidade.sigla) |
            Q(unidade_venda_2=unidade.sigla)
        ).count()

    return render(
        request,
        "estoque/unidades_produto.html",
        {
            "form": form,
            "unidades": unidades,
            "termo": termo,
            "unidade_selecionada": unidade_selecionada,
            "total_unidades": len(unidades),
        },
    )

def categorias_produto(request):
    termo = request.GET.get("q", "").strip()
    categoria_selecionada = None

    categorias = Categoria.objects.all().order_by("-ativa", "nome")

    if termo:
        categorias = categorias.filter(
            Q(nome__icontains=termo) |
            Q(descricao__icontains=termo)
        )

    if request.method == "POST":
        acao = request.POST.get("acao")
        categoria_id = request.POST.get("categoria_id")

        if acao == "alternar_status" and categoria_id:
            categoria = get_object_or_404(Categoria, pk=categoria_id)
            categoria.ativa = request.POST.get("ativa") == "1"
            categoria.save(update_fields=["ativa"])
            status = "ativada" if categoria.ativa else "desativada"
            messages.success(request, f'Categoria "{categoria.nome}" {status} com sucesso.')
            params = {"categoria": categoria.id}
            if termo:
                params["q"] = termo
            destino = f"{reverse('estoque:categorias_produto')}?{urlencode(params)}"
            return redirect(destino)

        nome_anterior = None
        if categoria_id:
            categoria_selecionada = get_object_or_404(Categoria, pk=categoria_id)
            nome_anterior = categoria_selecionada.nome
            form = CategoriaForm(request.POST, instance=categoria_selecionada)
        else:
            form = CategoriaForm(request.POST)

        if form.is_valid():
            categoria = form.save()
            if nome_anterior and nome_anterior.casefold() != categoria.nome.casefold():
                Produto.objects.filter(categoria__iexact=nome_anterior).update(
                    categoria=categoria.nome
                )
            messages.success(request, f'Categoria "{categoria.nome}" salva com sucesso.')
            return redirect(f"{reverse('estoque:categorias_produto')}?categoria={categoria.id}")
        messages.error(request, "Revise os campos destacados para salvar a categoria.")
    else:
        categoria_id = request.GET.get("categoria")
        if categoria_id:
            categoria_selecionada = get_object_or_404(Categoria, pk=categoria_id)
            form = CategoriaForm(instance=categoria_selecionada)
        else:
            form = CategoriaForm(initial={"ativa": True})

    categorias = list(categorias)
    for categoria in categorias:
        categoria.produtos_em_uso = Produto.objects.filter(
            excluido=False,
            categoria__iexact=categoria.nome,
        ).count()

    return render(
        request,
        "estoque/categorias_produto.html",
        {
            "form": form,
            "categorias": categorias,
            "termo": termo,
            "categoria_selecionada": categoria_selecionada,
            "total_categorias": len(categorias),
        },
    )

def clientes(request):
    cliente_selecionado = None
    clientes_url = "/estoque/clientes/"
    form_token = request.POST.get("form_token") or uuid4().hex

    if request.method == "POST":
        acao = request.POST.get("acao")
        cliente_id = request.POST.get("cliente_id")

        if acao == "alternar_status" and cliente_id:
            cliente = get_object_or_404(Cliente, pk=cliente_id)
            cliente.ativo = request.POST.get("ativo") == "1"
            cliente.save(update_fields=["ativo", "atualizado_em"])
            status = "ativado" if cliente.ativo else "desativado"
            messages.success(request, f'Cliente "{cliente.nome}" {status} com sucesso.')
            return redirect(f"{clientes_url}?cliente={cliente.id}")

        if cliente_id:
            cliente_selecionado = get_object_or_404(Cliente, pk=cliente_id)
            form = ClienteForm(request.POST, instance=cliente_selecionado)
        else:
            form = ClienteForm(request.POST)

        if form.is_valid():
            tokens_usados = request.session.get("cliente_form_tokens_usados", {})
            if not cliente_id and form_token and form_token in tokens_usados:
                messages.warning(
                    request,
                    "Este envio ja foi processado. O formulario foi limpo para evitar duplicidade.",
                )
                return redirect(clientes_url)

            cliente = form.save(commit=False)

            cliente_duplicado = encontrar_cliente_duplicado(cliente)
            if cliente_duplicado:
                form.add_error(None, MENSAGEM_CLIENTE_DUPLICADO)
                return render(
                    request,
                    "estoque/clientes.html",
                    {
                        "form": form,
                        "cliente_selecionado": cliente_selecionado,
                        "form_token": form_token,
                    },
                )

            cliente.save()
            if not cliente_id and form_token:
                tokens_usados[form_token] = cliente.id
                tokens_itens = list(tokens_usados.items())[-20:]
                request.session["cliente_form_tokens_usados"] = dict(tokens_itens)
                request.session.modified = True

            messages.success(request, f'Cliente "{cliente.nome}" salvo com sucesso.')
            if cliente_id:
                return redirect(f"{clientes_url}?cliente={cliente.id}")
            return redirect(clientes_url)
        messages.error(request, "Revise os campos destacados para salvar o cliente.")
    else:
        cliente_id = request.GET.get("cliente")
        if cliente_id:
            cliente_selecionado = get_object_or_404(Cliente, pk=cliente_id)
            form = ClienteForm(instance=cliente_selecionado)
        else:
            form = ClienteForm(initial={
                "ativo": True,
                "permite_contato_whatsapp": True,
                "status_credito": Cliente.STATUS_CREDITO_LIBERADO,
            })

    return render(
        request,
        "estoque/clientes.html",
        {
            "form": form,
            "cliente_selecionado": cliente_selecionado,
            "form_token": form_token,
        },
    )

def clientes_consulta(request):
    termo = request.GET.get("q", "").strip()
    clientes_url = "/estoque/clientes/consulta/"

    if request.method == "POST":
        acao = request.POST.get("acao")
        cliente_id = request.POST.get("cliente_id")
        params = {}
        if termo:
            params["q"] = termo
        destino = clientes_url
        if params:
            destino = f"{destino}?{urlencode(params)}"

        if acao == "alternar_status" and cliente_id:
            cliente = get_object_or_404(Cliente, pk=cliente_id)
            cliente.ativo = request.POST.get("ativo") == "1"
            cliente.save(update_fields=["ativo", "atualizado_em"])
            status = "ativado" if cliente.ativo else "desativado"
            messages.success(request, f'Cliente "{cliente.nome}" {status} com sucesso.')
            return redirect(destino)

        if acao == "excluir" and cliente_id:
            cliente = get_object_or_404(Cliente, pk=cliente_id)
            if cliente.vendas.exists():
                messages.warning(
                    request,
                    f'Cliente "{cliente.nome}" possui venda vinculada e nao pode ser excluido. Use Desativar.',
                )
            else:
                nome_cliente = cliente.nome
                cliente.delete()
                messages.success(request, f'Cliente "{nome_cliente}" excluido com sucesso.')
            return redirect(destino)

    clientes_qs = Cliente.objects.annotate(total_vendas=Count("vendas")).order_by("-ativo", "nome")

    if termo:
        clientes_qs = clientes_qs.filter(
            Q(nome__icontains=termo) |
            Q(apelido_nome_conhecido__icontains=termo) |
            Q(cpf_cnpj__icontains=termo) |
            Q(whatsapp__icontains=termo) |
            Q(whatsapp_normalizado__icontains=termo) |
            Q(bairro__icontains=termo) |
            Q(cidade__icontains=termo)
        )

    clientes_lista = list(clientes_qs)
    for cliente in clientes_lista:
        cliente.pode_excluir = cliente.total_vendas == 0

    return render(
        request,
        "estoque/clientes_consulta.html",
        {
            "clientes": clientes_lista,
            "termo": termo,
            "total_clientes": len(clientes_lista),
        },
    )


def verificar_cliente_duplicado(request):
    cliente = Cliente(
        nome=request.GET.get("nome", ""),
        apelido_nome_conhecido=request.GET.get("apelido_nome_conhecido", ""),
        cpf_cnpj=request.GET.get("cpf_cnpj", ""),
        whatsapp=request.GET.get("whatsapp", ""),
        bairro=request.GET.get("bairro", ""),
        cidade=request.GET.get("cidade", ""),
    )

    cliente_id = request.GET.get("cliente_id")
    if cliente_id:
        try:
            cliente.pk = int(cliente_id)
        except (TypeError, ValueError):
            cliente.pk = None

    duplicado, campo = detectar_cliente_duplicado(cliente)
    if not duplicado:
        return JsonResponse({"duplicado": False, "mensagem": "", "campo": ""})

    return JsonResponse({
        "duplicado": True,
        "campo": campo or "nome",
        "cliente": duplicado.nome,
        "mensagem": f'Ja existe um cliente parecido cadastrado: "{duplicado.nome}". Verifique antes de cadastrar novamente.',
    })


def funcionarios(request):
    termo = request.GET.get("q", "").strip()
    funcionario_selecionado = None
    funcionarios_url = reverse("estoque:funcionarios")

    funcionarios_qs = Funcionario.objects.all().order_by(
        "-ativo",
        "-pode_receber_checklist",
        "nome",
    )

    if termo:
        funcionarios_qs = funcionarios_qs.filter(
            Q(nome__icontains=termo) |
            Q(telefone_whatsapp__icontains=termo) |
            Q(telefone_whatsapp_normalizado__icontains=termo) |
            Q(observacoes__icontains=termo)
        )

    if request.method == "POST":
        acao = request.POST.get("acao")
        funcionario_id = request.POST.get("funcionario_id")

        if acao == "alternar_status" and funcionario_id:
            funcionario = get_object_or_404(Funcionario, pk=funcionario_id)
            funcionario.ativo = request.POST.get("ativo") == "1"
            if not funcionario.ativo:
                funcionario.pode_receber_checklist = False
            funcionario.save(update_fields=[
                "ativo",
                "pode_receber_checklist",
                "telefone_whatsapp_normalizado",
                "atualizado_em",
            ])
            status = "ativado" if funcionario.ativo else "desativado"
            messages.success(request, f'Funcionario "{funcionario.nome}" {status} com sucesso.')
            params = {"funcionario": funcionario.id}
            if termo:
                params["q"] = termo
            return redirect(f"{funcionarios_url}?{urlencode(params)}")

        if funcionario_id:
            funcionario_selecionado = get_object_or_404(Funcionario, pk=funcionario_id)
            form = FuncionarioForm(request.POST, instance=funcionario_selecionado)
        else:
            form = FuncionarioForm(request.POST)

        if form.is_valid():
            funcionario = form.save()
            messages.success(request, f'Funcionario "{funcionario.nome}" salvo com sucesso.')
            return redirect(f"{funcionarios_url}?funcionario={funcionario.id}")
        messages.error(request, "Revise os campos destacados para salvar o funcionario.")
    else:
        funcionario_id = request.GET.get("funcionario")
        if funcionario_id:
            funcionario_selecionado = get_object_or_404(Funcionario, pk=funcionario_id)
            form = FuncionarioForm(instance=funcionario_selecionado)
        else:
            form = FuncionarioForm(initial={
                "ativo": True,
                "pode_receber_checklist": False,
            })

    funcionarios_lista = list(funcionarios_qs)
    funcionarios_habilitados = sum(
        1 for funcionario in funcionarios_lista
        if funcionario.ativo and funcionario.pode_receber_checklist
    )

    return render(
        request,
        "estoque/funcionarios.html",
        {
            "form": form,
            "funcionarios": funcionarios_lista,
            "funcionario_selecionado": funcionario_selecionado,
            "termo": termo,
            "total_funcionarios": len(funcionarios_lista),
            "funcionarios_habilitados": funcionarios_habilitados,
        },
    )


def _resumo_cliente_venda(cliente, hoje=None):
    hoje = hoje or timezone.localdate()
    credito_disponivel = (
        CreditoCliente.objects.filter(cliente=cliente)
        .aggregate(total=Sum("valor"))
        .get("total")
        or Decimal("0.00")
    ).quantize(Decimal("0.01"))
    credito_disponivel = max(credito_disponivel, Decimal("0.00"))

    contas_abertas_qs = ContaReceber.objects.filter(
        cliente=cliente,
        status__in=[ContaReceber.STATUS_ABERTA, ContaReceber.STATUS_PARCIAL],
    )
    contas_abertas = contas_abertas_qs.aggregate(
        quantidade=Count("id"),
        total=Sum("valor_em_aberto"),
    )
    contas_vencidas = contas_abertas_qs.filter(data_vencimento__lt=hoje).aggregate(
        quantidade=Count("id"),
        total=Sum("valor_em_aberto"),
    )
    vencimento_mais_antigo = (
        contas_abertas_qs
        .filter(data_vencimento__lt=hoje)
        .order_by("data_vencimento")
        .values_list("data_vencimento", flat=True)
        .first()
    )
    maior_atraso_dias = (hoje - vencimento_mais_antigo).days if vencimento_mais_antigo else 0
    contas_preview = _montar_contas_preview_cobranca(contas_abertas_qs, hoje)
    whatsapp_cobranca = _montar_whatsapp_cobranca_cliente(
        cliente,
        contas_abertas,
        contas_vencidas,
        maior_atraso_dias,
        contas_preview,
    )
    whatsapp_cobranca["visual_url"] = reverse("estoque:cliente_cobranca_imagem", kwargs={"cliente_id": cliente.id})
    return {
        "id": cliente.id,
        "nome": cliente.nome,
        "prazo": cliente.prazo_padrao_dias or 0,
        "limite": str(cliente.limite_credito or 0),
        "status": cliente.status_credito,
        "status_label": cliente.get_status_credito_display(),
        "whatsapp": cliente.whatsapp or "",
        "financeiro": {
            "credito_disponivel": str(credito_disponivel),
            "contas_abertas_qtd": contas_abertas.get("quantidade") or 0,
            "contas_abertas_total": str(contas_abertas.get("total") or Decimal("0.00")),
            "contas_vencidas_qtd": contas_vencidas.get("quantidade") or 0,
            "contas_vencidas_total": str(contas_vencidas.get("total") or Decimal("0.00")),
        },
        "whatsapp_cobranca": whatsapp_cobranca,
    }


def clientes_autocomplete(request):
    termo = request.GET.get("q", "").strip()
    clientes_qs = Cliente.objects.filter(ativo=True).order_by("nome")
    hoje = timezone.localdate()

    if termo:
        for parte in termo.split():
            clientes_qs = clientes_qs.filter(
                Q(nome__icontains=parte) |
                Q(apelido_nome_conhecido__icontains=parte) |
                Q(whatsapp__icontains=parte) |
                Q(whatsapp_normalizado__icontains=parte)
            )

    clientes = []
    for cliente in clientes_qs[:12]:
        dados_cliente = _resumo_cliente_venda(cliente, hoje)
        dados_cliente["documento"] = cliente.cpf_cnpj or ""
        dados_cliente["telefone"] = cliente.whatsapp or cliente.telefone_alternativo or ""
        clientes.append(dados_cliente)

    return JsonResponse({"clientes": clientes})


def cliente_cobranca_imagem(request, cliente_id):
    cliente = get_object_or_404(Cliente, pk=cliente_id)
    resumo = _resumo_cliente_venda(cliente)
    financeiro = resumo["financeiro"]
    cobranca = resumo["whatsapp_cobranca"]
    buffer = _gerar_cobranca_cliente_imagem(cliente, financeiro, cobranca)
    response = HttpResponse(buffer.getvalue(), content_type="image/png")
    response["Content-Disposition"] = f'inline; filename="cobranca-cliente-{cliente.id}.png"'
    return response


def _url_retorno_segura(request):
    proxima_url = request.GET.get("next", "").strip()
    if proxima_url and url_has_allowed_host_and_scheme(
        url=proxima_url,
        allowed_hosts={request.get_host()},
        require_https=request.is_secure(),
    ):
        return proxima_url
    return ""


def _querystring_retorno(retorno_url):
    return urlencode({"next": retorno_url}) if retorno_url else ""


def _formatar_data_cobranca(valor):
    return valor.strftime("%d/%m/%Y") if valor else ""


def _montar_contas_preview_cobranca(contas_qs, hoje):
    contas = []
    for conta in contas_qs.select_related("venda").order_by("data_vencimento", "id"):
        vencimento = conta.data_vencimento
        atraso_dias = (hoje - vencimento).days if vencimento and vencimento < hoje else 0
        if atraso_dias == 1:
            status_texto = "Vencida há 1 dia"
        elif atraso_dias > 1:
            status_texto = f"Vencida há {atraso_dias} dias"
        else:
            status_texto = "Em dia"
        venda = conta.venda
        contas.append({
            "titulo": f"Venda #{venda.id}" if venda else f"Conta #{conta.id}",
            "data": _formatar_data_cobranca((venda.data_venda if venda else None) or conta.data_emissao),
            "vencimento": _formatar_data_cobranca(vencimento),
            "valor": _formatar_moeda(conta.valor_em_aberto or Decimal("0.00")),
            "status": status_texto,
            "vencida": atraso_dias > 0,
        })
    return contas


def _montar_whatsapp_cobranca_cliente(cliente, contas_abertas, contas_vencidas, maior_atraso_dias=0, contas_preview=None):
    numero = Cliente.normalizar_whatsapp(cliente.whatsapp_normalizado or cliente.whatsapp)
    if numero and len(numero) in (10, 11):
        numero = "55" + numero

    linhas_mensagem = [
        f"Olá, {cliente.nome}.",
        "",
        "Segue atualização das contas em aberto.",
        "",
        "Obrigado.",
        "LA Neiva",
    ]
    mensagem = "\n".join(linhas_mensagem)
    return {
        "tem_whatsapp": bool(numero),
        "url": f"https://web.whatsapp.com/send?phone={numero}&text={quote(mensagem)}" if numero else "",
        "numero": numero,
        "mensagem": mensagem,
        "maior_atraso_dias": maior_atraso_dias,
        "contas": contas_preview or [],
    }


def _url_com_retorno(url, retorno_url):
    if not retorno_url:
        return url
    separador = "&" if "?" in url else "?"
    return f"{url}{separador}{_querystring_retorno(retorno_url)}"


def _numero_whatsapp_cliente(cliente):
    if not cliente:
        return ""
    numero = Cliente.normalizar_whatsapp(cliente.whatsapp_normalizado or cliente.whatsapp)
    return _normalizar_numero_whatsapp_evento(numero)


def _montar_mensagem_confirmacao_recebimento(dados):
    linhas = [
        f"Olá, {dados['cliente_nome']}.",
        "",
        "Segue comprovante de pagamento.",
        "",
        "Obrigado.",
        "L A Neiva",
    ]
    return "\n".join(linhas)


def _montar_whatsapp_confirmacao_recebimento(cliente, dados):
    numero = _numero_whatsapp_cliente(cliente)
    mensagem = _montar_mensagem_confirmacao_recebimento(dados)
    return {
        "tem_whatsapp": bool(numero),
        "numero": numero,
        "url": f"https://web.whatsapp.com/send?phone={numero}&text={quote(mensagem)}" if numero else "",
        "mensagem": mensagem,
    }


def _serializar_dados_comprovante_recebimento(cliente, dados):
    return {
        "cliente_id": cliente.id,
        "cliente_nome": dados["cliente_nome"],
        "data_recebimento": dados["data_recebimento"],
        "saldo_anterior": str(dados["saldo_anterior"]),
        "valor_pago": str(dados["valor_pago"]),
        "forma_pagamento": dados["forma_pagamento"],
        "saldo_atual": str(dados["saldo_atual"]),
        "credito_gerado": str(dados["credito_gerado"]),
        "contas": [
            {
                "conta_id": conta["conta_id"],
                "venda_id": conta["venda_id"],
                "data_nota": conta.get("data_nota") or "",
                "saldo_antes": str(conta.get("saldo_antes") or "0.00"),
                "nota_inteira_antes": bool(conta.get("nota_inteira_antes")),
                "valor_aplicado": str(conta["valor_aplicado"]),
                "saldo_restante": str(conta.get("saldo_restante") or "0.00"),
                "quitada": bool(conta["quitada"]),
            }
            for conta in dados.get("contas", [])
        ],
        "contas_abertas": [
            {
                "conta_id": conta["conta_id"],
                "venda_id": conta["venda_id"],
                "data_nota": conta.get("data_nota") or "",
                "saldo_atual": str(conta["saldo_atual"]),
                "dias_aberto": int(conta.get("dias_aberto") or 0),
                "em_atraso": bool(conta.get("em_atraso")),
            }
            for conta in dados.get("contas_abertas", [])
        ],
    }


def produto_detalhe(request, pk):
    produto = get_object_or_404(Produto, pk=pk)
    return render(request, "estoque/produto_detalhe.html", {"produto": produto})
def produto_editar(request, pk):
    produto = get_object_or_404(Produto, pk=pk)

    if request.method == "POST":
        form = ProdutoForm(request.POST, instance=produto)
        if form.is_valid():
            form.save()
            return redirect(f"{reverse('estoque:home')}?produto_destacado={produto.id}")
        else:
            print("ERROS DO FORM EDITAR:", form.errors)
            print("DADOS RECEBIDOS EDITAR:", request.POST)
    else:
        form = ProdutoForm(instance=produto)

    return render(request, "estoque/cadastrar_produto.html", {"form": form})
def produto_excluir(request, pk):
    produto = get_object_or_404(Produto, pk=pk)
    produto.excluido = True
    produto.excluido_em = timezone.now()
    produto.save()
    return redirect("estoque:home")
def verificar_produto(request):
    nome = request.GET.get("nome", "").strip()
    produto_id = request.GET.get("produto_id")

    query = Produto.objects.filter(nome__iexact=nome)

    if produto_id:
     query = query.exclude(id=produto_id)

    existe = query.exists()
    return JsonResponse({"existe": existe})
def lixeira(request):
    produtos = Produto.objects.filter(excluido=True)
    return render(request, "estoque/lixeira.html", {"produtos": produtos})
def produto_restaurar(request, pk):
    produto = get_object_or_404(Produto, pk=pk)
    Produto.objects.filter(pk=produto.pk).update(
        excluido=False,
        excluido_em=None,
    )
    return redirect("estoque:lixeira")
def produto_excluir_definitivo(request, pk):
    produto = get_object_or_404(Produto, pk=pk)

    if request.method == "POST":
        produto.delete()

    return redirect("estoque:lixeira")
@ensure_csrf_cookie
def vendas(request):
    produtos = Produto.objects.filter(excluido=False).order_by('nome')
    cliente_inicial = None
    cliente_id = request.GET.get("cliente_id")
    if cliente_id:
        cliente = Cliente.objects.filter(pk=cliente_id, ativo=True).first()
        if cliente:
            cliente_inicial = _resumo_cliente_venda(cliente)
    return render(request, 'estoque/vendas_layout_teste.html', {
        'produtos': produtos,
        'cliente_inicial': cliente_inicial,
        'tem_pix_em_atencao': _tem_pix_em_atencao(),
    })


@ensure_csrf_cookie
def consultar_vendas(request, mostrar_canceladas=False):
    hoje = timezone.localdate()
    primeira_abertura = not request.GET

    data_inicial_texto = request.GET.get("data_inicial", "").strip()
    data_final_texto = request.GET.get("data_final", "").strip()
    cliente_texto = request.GET.get("cliente", "").strip()
    numero_texto = request.GET.get("numero", "").strip()

    if primeira_abertura:
        data_inicial = hoje
        data_final = hoje
        data_inicial_texto = hoje.isoformat()
        data_final_texto = hoje.isoformat()
    else:
        data_inicial = parse_date(data_inicial_texto) if data_inicial_texto else None
        data_final = parse_date(data_final_texto) if data_final_texto else None

    vendas_qs = (
        Venda.objects.select_related("cliente")
        .prefetch_related("itens__produto", "eventos")
        .filter(cancelada=mostrar_canceladas)
        .order_by("-data_venda", "-id")
    )

    if data_inicial:
        vendas_qs = vendas_qs.filter(data_venda__gte=data_inicial)
    elif data_inicial_texto:
        messages.warning(request, "Data inicial invalida. O filtro foi ignorado.")

    if data_final:
        vendas_qs = vendas_qs.filter(data_venda__lte=data_final)
    elif data_final_texto:
        messages.warning(request, "Data final invalida. O filtro foi ignorado.")

    if cliente_texto:
        vendas_qs = vendas_qs.filter(
            Q(cliente__nome__icontains=cliente_texto) |
            Q(cliente__apelido_nome_conhecido__icontains=cliente_texto) |
            Q(cliente__cpf_cnpj__icontains=cliente_texto) |
            Q(cliente__whatsapp__icontains=cliente_texto)
        )

    if numero_texto:
        try:
            vendas_qs = vendas_qs.filter(pk=int(numero_texto))
        except ValueError:
            vendas_qs = vendas_qs.none()
            messages.warning(request, "Numero da venda invalido. Informe apenas numeros.")

    vendas_lista = list(vendas_qs)
    for venda in vendas_lista:
        venda.whatsapp_url_consulta = "" if venda.cancelada else montar_link_whatsapp_venda(venda)
        venda.whatsapp_status_selos = (
            [{"texto": "Cancelada / venda nao realizada", "classe": "cancelada"}]
            if venda.cancelada
            else _status_whatsapp_consulta_venda(venda)
        )

    return render(
        request,
        "estoque/vendas_consulta.html",
        {
            "vendas": vendas_lista,
            "total_vendas": len(vendas_lista),
            "data_inicial": data_inicial_texto,
            "data_final": data_final_texto,
            "cliente": cliente_texto,
            "numero": numero_texto,
            "primeira_abertura": primeira_abertura,
            "mostrar_canceladas": mostrar_canceladas,
        },
    )


@ensure_csrf_cookie
def consultar_vendas_canceladas(request):
    return consultar_vendas(request, mostrar_canceladas=True)


@ensure_csrf_cookie
def contas_receber(request):
    cliente_texto = request.GET.get("cliente", "").strip()
    data_inicial_texto = request.GET.get("data_inicial", "").strip()
    data_final_texto = request.GET.get("data_final", "").strip()
    status_filtro = request.GET.get("status", "em_aberto").strip() or "em_aberto"
    retorno_url = _url_retorno_segura(request)

    data_inicial = parse_date(data_inicial_texto) if data_inicial_texto else None
    data_final = parse_date(data_final_texto) if data_final_texto else None
    status_validos = {
        ContaReceber.STATUS_ABERTA,
        ContaReceber.STATUS_PARCIAL,
        ContaReceber.STATUS_PAGA,
        ContaReceber.STATUS_CANCELADA,
        "em_aberto",
        "todas",
    }

    if status_filtro not in status_validos:
        status_filtro = "em_aberto"
        messages.warning(request, "Status invalido. Mostrando contas em aberto.")

    contas_qs = (
        ContaReceber.objects.select_related("cliente", "venda")
        .order_by("data_vencimento", "data_emissao", "id")
    )

    if status_filtro == "em_aberto":
        contas_qs = contas_qs.filter(status__in=[ContaReceber.STATUS_ABERTA, ContaReceber.STATUS_PARCIAL])
    elif status_filtro != "todas":
        contas_qs = contas_qs.filter(status=status_filtro)

    if data_inicial:
        contas_qs = contas_qs.filter(data_vencimento__gte=data_inicial)
    elif data_inicial_texto:
        messages.warning(request, "Data inicial invalida. O filtro foi ignorado.")

    if data_final:
        contas_qs = contas_qs.filter(data_vencimento__lte=data_final)
    elif data_final_texto:
        messages.warning(request, "Data final invalida. O filtro foi ignorado.")

    if cliente_texto:
        contas_qs = contas_qs.filter(
            Q(cliente__nome__icontains=cliente_texto)
            | Q(cliente__apelido_nome_conhecido__icontains=cliente_texto)
            | Q(cliente__cpf_cnpj__icontains=cliente_texto)
            | Q(cliente__whatsapp__icontains=cliente_texto)
        )

    contas_recebimento_cliente = None
    contas_recebimento_cliente_total = Decimal("0.00")
    contas_recebimento_cliente_qtd = 0
    if cliente_texto and status_filtro == "em_aberto":
        clientes_abertos = list(
            contas_qs.exclude(cliente__isnull=True)
            .values("cliente_id", "cliente__nome")
            .annotate(qtd=Count("id"), total=Sum("valor_em_aberto"))
            .order_by("cliente__nome")[:2]
        )
        if len(clientes_abertos) == 1:
            contas_recebimento_cliente = clientes_abertos[0]
            contas_recebimento_cliente_qtd = contas_recebimento_cliente["qtd"] or 0
            contas_recebimento_cliente_total = contas_recebimento_cliente["total"] or Decimal("0.00")

    creditos_qs = CreditoCliente.objects.select_related("cliente")
    if cliente_texto:
        creditos_qs = creditos_qs.filter(
            Q(cliente__nome__icontains=cliente_texto)
            | Q(cliente__apelido_nome_conhecido__icontains=cliente_texto)
            | Q(cliente__cpf_cnpj__icontains=cliente_texto)
            | Q(cliente__whatsapp__icontains=cliente_texto)
        )

    clientes_com_credito = []
    creditos_agrupados = (
        creditos_qs.values("cliente_id", "cliente__nome")
        .annotate(total_credito=Sum("valor"), credito_mais_recente=Max("criado_em"))
        .filter(total_credito__gt=Decimal("0.00"))
        .order_by("cliente__nome")
    )
    for credito in creditos_agrupados:
        ultimo_credito = (
            creditos_qs.filter(cliente_id=credito["cliente_id"])
            .order_by("-criado_em", "-id")
            .first()
        )
        clientes_com_credito.append(
            {
                "cliente_id": credito["cliente_id"],
                "cliente_nome": credito["cliente__nome"],
                "total_credito": credito["total_credito"] or Decimal("0.00"),
                "credito_mais_recente": credito["credito_mais_recente"],
                "observacao": (ultimo_credito.observacao if ultimo_credito else ""),
            }
        )

    contas = list(contas_qs)
    return render(
        request,
        "estoque/contas_receber.html",
        {
            "contas": contas,
            "total_contas": len(contas),
            "clientes_com_credito": clientes_com_credito,
            "cliente": cliente_texto,
            "data_inicial": data_inicial_texto,
            "data_final": data_final_texto,
            "retorno_url": retorno_url,
            "detalhe_credito_retorno_url": request.get_full_path(),
            "contas_recebimento_cliente": contas_recebimento_cliente,
            "contas_recebimento_cliente_qtd": contas_recebimento_cliente_qtd,
            "contas_recebimento_cliente_total": contas_recebimento_cliente_total,
            "status_filtro": status_filtro,
            "status_opcoes": (
                ("em_aberto", "Em aberto"),
                (ContaReceber.STATUS_ABERTA, "Abertas"),
                (ContaReceber.STATUS_PARCIAL, "Parciais"),
                (ContaReceber.STATUS_PAGA, "Pagas"),
                (ContaReceber.STATUS_CANCELADA, "Canceladas"),
                ("todas", "Todas"),
            ),
            "tem_pix_em_atencao": _tem_pix_em_atencao(),
        },
    )


@ensure_csrf_cookie
def central_pix(request):
    retorno_url = request.GET.get("next", "").strip()
    if not (
        retorno_url.startswith("/")
        and not retorno_url.startswith("//")
        and url_has_allowed_host_and_scheme(
            url=retorno_url,
            allowed_hosts={request.get_host()},
            require_https=request.is_secure(),
        )
    ):
        retorno_url = ""
    central_pix_url = reverse("estoque:central_pix")

    if request.method == "POST":
        form = PixRecebidoForm(request.POST, request.FILES)
        if form.is_valid():
            pix_duplicado = _pix_duplicado_pendente(form.cleaned_data)
            if pix_duplicado:
                form.add_error(
                    None,
                    "Ja existe um Pix pendente com mesmo pagador, valor e horario muito proximo. Confira antes de cadastrar novamente.",
                )
                messages.warning(request, "Pix duplicado nao foi salvo. Confira o registro pendente existente.")
            else:
                form.save()
                messages.success(request, "Pix recebido registrado com sucesso.")
                if retorno_url:
                    return redirect(f"{central_pix_url}?{urlencode({'next': retorno_url})}")
                return redirect("estoque:central_pix")
        else:
            messages.warning(request, "Confira os campos do Pix antes de salvar.")
    else:
        form = PixRecebidoForm()

    pix_recebidos = PixRecebido.objects.select_related("cliente").order_by("-data_pagamento", "-id")
    return render(
        request,
        "estoque/central_pix.html",
        {
            "form": form,
            "pix_recebidos": pix_recebidos,
            "total_pix": pix_recebidos.count(),
            "voltar_url": retorno_url or reverse("estoque:contas_receber"),
        },
    )


@require_POST
def central_pix_analisar_comprovante(request):
    arquivo = request.FILES.get("comprovante")
    if not arquivo:
        return JsonResponse({
            "ok": False,
            "mensagem": "Envie um comprovante para leitura automatica.",
        }, status=400)

    dados = analisar_comprovante_pix(arquivo)
    cliente_sugerido = None
    confianca_cliente = "baixa"
    pagador_normalizado = normalizar_texto_cliente(dados.get("pagador"))
    if pagador_normalizado:
        for cliente in Cliente.objects.filter(ativo=True).only("id", "nome", "apelido_nome_conhecido").order_by("nome"):
            nome_parecido = textos_parecidos_cliente(pagador_normalizado, cliente.nome, minimo=0.96)
            apelido_parecido = bool(cliente.apelido_nome_conhecido) and textos_parecidos_cliente(
                pagador_normalizado,
                cliente.apelido_nome_conhecido,
                minimo=0.96,
            )
            if nome_parecido or apelido_parecido:
                cliente_sugerido = cliente
                confianca_cliente = "alta"
                break

    return JsonResponse({
        "ok": bool(dados.get("ok")),
        "pagador": dados.get("pagador", ""),
        "valor": dados.get("valor", ""),
        "data_pagamento": dados.get("data_pagamento", ""),
        "cliente_sugerido_id": cliente_sugerido.id if cliente_sugerido else None,
        "cliente_sugerido_nome": cliente_sugerido.nome if cliente_sugerido else "",
        "confianca_cliente": confianca_cliente,
        "mensagem": dados.get("mensagem", ""),
        "observacao": (
            "Dados lidos automaticamente do comprovante. Conferir antes de confirmar."
            if dados.get("ok")
            else ""
        ),
    })


@ensure_csrf_cookie
def receber_cliente(request, cliente_id):
    cliente = get_object_or_404(Cliente, pk=cliente_id)
    feedback_recebimento = request.session.pop("receber_cliente_feedback", None)
    retorno_url = _url_retorno_segura(request)
    contas_url = reverse("estoque:contas_receber")
    destino_retorno = retorno_url or f"{contas_url}?{urlencode({'cliente': cliente.nome, 'status': 'em_aberto'})}"
    destino_pos_recebimento = reverse("estoque:receber_cliente", kwargs={"cliente_id": cliente.id})
    if retorno_url:
        destino_pos_recebimento = f"{destino_pos_recebimento}?{urlencode({'next': retorno_url})}"
    hoje = timezone.localdate()
    formas_pagamento = (
        "Dinheiro",
        "PIX",
        "Cartao de debito",
        "Cartao de credito",
        "Transferencia",
        "Outro",
    )

    contas = list(
        ContaReceber.objects.select_related("venda")
        .filter(
            cliente=cliente,
            status__in=[ContaReceber.STATUS_ABERTA, ContaReceber.STATUS_PARCIAL],
            valor_em_aberto__gt=Decimal("0.00"),
        )
    )
    contas.sort(
        key=lambda conta: (
            0 if conta.data_vencimento and conta.data_vencimento < hoje else 1,
            conta.data_vencimento or date.max,
            conta.data_emissao or date.max,
            conta.id,
        )
    )

    total_em_aberto = sum((conta.valor_em_aberto or Decimal("0.00") for conta in contas), Decimal("0.00"))
    valores = {
        "data_recebimento": hoje.isoformat(),
        "valor": f"{total_em_aberto:.2f}".replace(".", ","),
        "forma_pagamento": "Dinheiro",
        "destino_diferenca": "troco",
    }
    if feedback_recebimento and total_em_aberto <= Decimal("0.00"):
        valores["valor"] = ""
    credito_disponivel = (
        CreditoCliente.objects.filter(cliente=cliente)
        .aggregate(total=Sum("valor"))
        .get("total")
        or Decimal("0.00")
    ).quantize(Decimal("0.01"))
    credito_disponivel = max(credito_disponivel, Decimal("0.00"))
    pagamentos_hoje_preview = [
        float(valor or Decimal("0.00"))
        for valor in RecebimentoContaReceber.objects.filter(
            conta__cliente=cliente,
            criado_em__date=hoje,
        ).values_list("valor", flat=True)
    ]
    limite_recente = timezone.now() - timedelta(hours=72)
    recebimentos_recentes = (
        RecebimentoContaReceber.objects.select_related("conta", "conta__venda")
        .filter(conta__cliente=cliente, criado_em__gte=limite_recente)
        .order_by("-criado_em", "-id")[:8]
    )
    pagamentos_recentes = []
    for recebimento in recebimentos_recentes:
        valor_total_recebido = _valor_total_recebimento_cliente(recebimento)
        pagamentos_recentes.append(
            {
                "criado_em": recebimento.criado_em,
                "criado_em_data": timezone.localtime(recebimento.criado_em).date().isoformat(),
                "data_recebimento": recebimento.data_recebimento.isoformat() if recebimento.data_recebimento else "",
                "valor": valor_total_recebido,
                "valor_numero": float(valor_total_recebido or Decimal("0.00")),
                "valor_aplicado": recebimento.valor,
                "forma_pagamento": recebimento.forma_pagamento,
                "conta_id": recebimento.conta_id,
                "venda_id": recebimento.conta.venda_id if recebimento.conta_id else "",
                "observacao": recebimento.observacao,
            }
        )

    if request.method == "POST":
        valores = {
            "data_recebimento": request.POST.get("data_recebimento", "").strip(),
            "valor": request.POST.get("valor", "").strip(),
            "forma_pagamento": request.POST.get("forma_pagamento", "").strip(),
            "destino_diferenca": request.POST.get("destino_diferenca", "troco").strip(),
        }
        data_recebimento = parse_date(valores["data_recebimento"])
        if not data_recebimento:
            messages.warning(request, "Informe uma data de recebimento valida.")
        elif valores["forma_pagamento"] not in formas_pagamento:
            messages.warning(request, "Selecione uma forma de pagamento.")
        elif valores["destino_diferenca"] not in {"troco", "credito"}:
            messages.warning(request, "Selecione como tratar a sobra do pagamento.")
        else:
            try:
                valor_recebido = _decimal_do_front(valores["valor"] or "0", "0.01")
            except ValueError as exc:
                messages.warning(request, str(exc))
            else:
                if valor_recebido <= Decimal("0.00"):
                    messages.warning(request, "Informe um valor recebido maior que zero.")
                elif not contas:
                    messages.warning(request, "Nao ha contas abertas para receber deste cliente.")
                else:
                    try:
                        with transaction.atomic():
                            contas_atualizadas = list(
                                ContaReceber.objects.select_for_update()
                                .select_related("venda")
                                .filter(
                                    cliente=cliente,
                                    status__in=[ContaReceber.STATUS_ABERTA, ContaReceber.STATUS_PARCIAL],
                                    valor_em_aberto__gt=Decimal("0.00"),
                                )
                            )
                            contas_atualizadas.sort(
                                key=lambda conta: (
                                    0 if conta.data_vencimento and conta.data_vencimento < hoje else 1,
                                    conta.data_vencimento or date.max,
                                    conta.data_emissao or date.max,
                                    conta.id,
                                )
                            )
                            if not contas_atualizadas:
                                raise RecebimentoContaErro("Nao ha contas abertas para receber deste cliente.")

                            restante = valor_recebido
                            distribuicao = []
                            for conta_atual in contas_atualizadas:
                                if restante <= Decimal("0.00"):
                                    break
                                saldo_conta = (conta_atual.valor_em_aberto or Decimal("0.00")).quantize(Decimal("0.01"))
                                valor_aplicar = min(restante, saldo_conta).quantize(Decimal("0.01"))
                                if valor_aplicar <= Decimal("0.00"):
                                    continue
                                distribuicao.append([conta_atual, valor_aplicar, Decimal("0.00")])
                                restante = (restante - valor_aplicar).quantize(Decimal("0.01"))

                            if not distribuicao:
                                raise RecebimentoContaErro("Informe um valor recebido maior que zero.")

                            sobra = max(restante, Decimal("0.00")).quantize(Decimal("0.01"))
                            if sobra > Decimal("0.00"):
                                distribuicao[-1][2] = sobra

                            valor_aplicado_total = Decimal("0.00")
                            credito_gerado_total = Decimal("0.00")
                            contas_afetadas = 0
                            contas_atualizadas_ids = []
                            contas_atualizadas_feedback = {}
                            contas_confirmacao_whatsapp = []
                            for conta_atual, valor_aplicar, sobra_conta in distribuicao:
                                valor_entregue_conta = (valor_aplicar + sobra_conta).quantize(Decimal("0.01"))
                                observacao = (
                                    "Recebimento geral do cliente. "
                                    f"Total recebido: {_formatar_moeda(valor_recebido)}. "
                                    f"Aplicado nesta conta: {_formatar_moeda(valor_aplicar)}."
                                )
                                resultado_recebimento = _aplicar_recebimento_conta(
                                    conta_atual,
                                    data_recebimento,
                                    valor_entregue_conta,
                                    valores["forma_pagamento"],
                                    observacao,
                                    valores["destino_diferenca"],
                                )
                                valor_aplicado_total = (valor_aplicado_total + valor_aplicar).quantize(Decimal("0.01"))
                                credito_gerado_total = (
                                    credito_gerado_total + resultado_recebimento["credito_gerado"]
                                ).quantize(Decimal("0.01"))
                                contas_afetadas += 1
                                contas_atualizadas_ids.append(conta_atual.id)
                                contas_atualizadas_feedback[str(conta_atual.id)] = {
                                    "valor_aplicado": _formatar_moeda(valor_aplicar),
                                    "saldo_restante": _formatar_moeda(resultado_recebimento["saldo_restante"]),
                                    "quitada": resultado_recebimento["saldo_restante"] <= Decimal("0.00"),
                                }
                                contas_confirmacao_whatsapp.append({
                                    "conta_id": conta_atual.id,
                                    "venda_id": conta_atual.venda_id,
                                    "data_nota": conta_atual.data_emissao.strftime("%d/%m/%Y") if conta_atual.data_emissao else "",
                                    "saldo_antes": (valor_aplicar + resultado_recebimento["saldo_restante"]).quantize(Decimal("0.01")),
                                    "nota_inteira_antes": abs(
                                        (
                                            (valor_aplicar + resultado_recebimento["saldo_restante"])
                                            - (conta_atual.valor_original or Decimal("0.00"))
                                        ).quantize(Decimal("0.01"))
                                    ) <= Decimal("0.01"),
                                    "valor_aplicado": valor_aplicar,
                                    "saldo_restante": resultado_recebimento["saldo_restante"],
                                    "quitada": resultado_recebimento["saldo_restante"] <= Decimal("0.00"),
                                })
                    except RecebimentoContaErro as exc:
                        messages.warning(request, str(exc))
                    else:
                        saldo_atual_confirmacao = max(
                            (total_em_aberto - valor_aplicado_total).quantize(Decimal("0.01")),
                            Decimal("0.00"),
                        )
                        prazo_cliente = cliente.prazo_padrao_dias or 0
                        contas_abertas_confirmacao = []
                        contas_abertas_atuais = (
                            ContaReceber.objects.filter(
                                cliente=cliente,
                                status__in=[ContaReceber.STATUS_ABERTA, ContaReceber.STATUS_PARCIAL],
                                valor_em_aberto__gt=Decimal("0.00"),
                            )
                            .order_by("data_emissao", "id")
                        )
                        for conta_aberta in contas_abertas_atuais:
                            data_nota = conta_aberta.data_emissao
                            dias_aberto = max((hoje - data_nota).days, 0) if data_nota else 0
                            contas_abertas_confirmacao.append({
                                "conta_id": conta_aberta.id,
                                "venda_id": conta_aberta.venda_id,
                                "data_nota": data_nota.strftime("%d/%m/%Y") if data_nota else "",
                                "saldo_atual": (conta_aberta.valor_em_aberto or Decimal("0.00")).quantize(Decimal("0.01")),
                                "dias_aberto": dias_aberto,
                                "em_atraso": bool(prazo_cliente and dias_aberto > prazo_cliente),
                            })
                        dados_confirmacao_whatsapp = {
                            "cliente_nome": cliente.nome,
                            "data_recebimento": data_recebimento.strftime("%d/%m/%Y"),
                            "saldo_anterior": total_em_aberto,
                            "valor_pago": valor_recebido,
                            "forma_pagamento": valores["forma_pagamento"],
                            "contas": contas_confirmacao_whatsapp,
                            "contas_abertas": contas_abertas_confirmacao,
                            "saldo_atual": saldo_atual_confirmacao,
                            "credito_gerado": credito_gerado_total,
                        }
                        comprovante_token = uuid4().hex
                        comprovante_dados = _serializar_dados_comprovante_recebimento(
                            cliente,
                            dados_confirmacao_whatsapp,
                        )
                        comprovantes_sessao = request.session.get("receber_cliente_comprovantes", {})
                        comprovantes_sessao[comprovante_token] = comprovante_dados
                        if len(comprovantes_sessao) > 8:
                            comprovantes_sessao = dict(list(comprovantes_sessao.items())[-8:])
                        request.session["receber_cliente_comprovantes"] = comprovantes_sessao
                        comprovante_url = reverse(
                            "estoque:receber_cliente_comprovante_imagem",
                            kwargs={"cliente_id": cliente.id, "token": comprovante_token},
                        )
                        request.session["receber_cliente_feedback"] = {
                            "cliente": cliente.nome,
                            "valor_aplicado": _formatar_moeda(valor_aplicado_total),
                            "valor_pago": _formatar_moeda(valor_recebido),
                            "saldo_anterior": _formatar_moeda(total_em_aberto),
                            "saldo_atual": _formatar_moeda(saldo_atual_confirmacao),
                            "credito_gerado": _formatar_moeda(credito_gerado_total),
                            "tem_credito_gerado": credito_gerado_total > Decimal("0.00"),
                            "forma_pagamento": valores["forma_pagamento"],
                            "data_recebimento": data_recebimento.strftime("%d/%m/%Y"),
                            "contas_afetadas": contas_afetadas,
                            "contas_atualizadas_ids": contas_atualizadas_ids,
                            "contas_atualizadas": contas_atualizadas_feedback,
                            "whatsapp_confirmacao": _montar_whatsapp_confirmacao_recebimento(
                                cliente,
                                dados_confirmacao_whatsapp,
                            ),
                            "comprovante_imagem_url": comprovante_url,
                        }
                        return redirect(destino_pos_recebimento)

    contas_preview = [
        {
            "id": conta.id,
            "venda_id": conta.venda_id,
            "valor_em_aberto": float(conta.valor_em_aberto or Decimal("0.00")),
        }
        for conta in contas
    ]

    return render(
        request,
        "estoque/receber_cliente.html",
        {
            "cliente": cliente,
            "contas": contas,
            "contas_preview": contas_preview,
            "total_contas": len(contas),
            "total_em_aberto": total_em_aberto,
            "credito_disponivel": credito_disponivel,
            "formas_pagamento": formas_pagamento,
            "valores": valores,
            "pagamentos_hoje_preview": pagamentos_hoje_preview,
            "pagamentos_recentes": pagamentos_recentes,
            "feedback_recebimento": feedback_recebimento,
            "contas_atualizadas_ids": (
                feedback_recebimento.get("contas_atualizadas_ids", [])
                if feedback_recebimento
                else []
            ),
            "contas_atualizadas_feedback": (
                feedback_recebimento.get("contas_atualizadas", {})
                if feedback_recebimento
                else {}
            ),
            "hoje_iso": hoje.isoformat(),
            "retorno_url": destino_retorno,
            "tem_pix_em_atencao": _tem_pix_em_atencao(),
        },
    )


def receber_cliente_comprovante_imagem(request, cliente_id, token):
    comprovantes = request.session.get("receber_cliente_comprovantes", {})
    dados = comprovantes.get(token)
    if not dados or int(dados.get("cliente_id") or 0) != cliente_id:
        raise Http404("Comprovante nao encontrado.")

    buffer = _gerar_comprovante_recebimento_imagem(dados)
    response = HttpResponse(buffer.getvalue(), content_type="image/png")
    response["Content-Disposition"] = f'inline; filename="comprovante-pagamento-cliente-{cliente_id}.png"'
    return response


@ensure_csrf_cookie
def cliente_credito_detalhe(request, cliente_id):
    cliente = get_object_or_404(Cliente, pk=cliente_id)
    retorno_url = _url_retorno_segura(request)
    movimentos_qs = CreditoCliente.objects.filter(cliente=cliente)
    creditos_qs = (
        CreditoCliente.objects.select_related(
            "origem_conta_receber",
            "origem_conta_receber__venda",
            "origem_recebimento",
        )
        .filter(cliente=cliente)
        .exclude(valor=Decimal("0.00"))
        .order_by("-criado_em", "-id")
    )
    credito_atual = (
        movimentos_qs.aggregate(total=Sum("valor")).get("total")
        or Decimal("0.00")
    ).quantize(Decimal("0.01"))
    credito_atual = max(credito_atual, Decimal("0.00"))
    saldos_por_movimento = {}
    saldo_credito = Decimal("0.00")
    for movimento in creditos_qs.order_by("criado_em", "id"):
        saldo_antes = saldo_credito.quantize(Decimal("0.01"))
        saldo_credito = (saldo_credito + (movimento.valor or Decimal("0.00"))).quantize(Decimal("0.01"))
        saldos_por_movimento[movimento.id] = {
            "credito_antes": saldo_antes,
            "credito_depois": saldo_credito,
        }

    creditos = []
    for credito in creditos_qs:
        recebimento = credito.origem_recebimento
        valor_aplicado = (
            recebimento.valor
            if recebimento
            else Decimal("0.00")
        )
        valor_credito = credito.valor or Decimal("0.00")
        credito_utilizado = valor_credito < Decimal("0.00")
        valor_movimento = abs(valor_credito).quantize(Decimal("0.01"))
        saldos_movimento = saldos_por_movimento.get(
            credito.id,
            {"credito_antes": Decimal("0.00"), "credito_depois": Decimal("0.00")},
        )
        creditos.append(
            {
                "utilizado": credito_utilizado,
                "criado_em": credito.criado_em,
                "conta_id": credito.origem_conta_receber_id,
                "venda_id": (
                    credito.origem_conta_receber.venda_id
                    if credito.origem_conta_receber_id
                    else None
                ),
                "data_recebimento": recebimento.data_recebimento if recebimento else None,
                "valor_entregue": (valor_aplicado + valor_credito).quantize(Decimal("0.01")),
                "valor_aplicado": valor_aplicado,
                "valor_credito": valor_credito,
                "valor_movimento": valor_movimento,
                "credito_antes": saldos_movimento["credito_antes"],
                "credito_depois": saldos_movimento["credito_depois"],
                "forma_pagamento": recebimento.forma_pagamento if recebimento else "",
                "observacao": credito.observacao,
            }
        )

    return render(
        request,
        "estoque/cliente_credito_detalhe.html",
        {
            "cliente": cliente,
            "credito_atual": credito_atual,
            "creditos": creditos,
            "ultimo_movimento": creditos[0] if creditos else None,
            "retorno_url": retorno_url,
        },
    )


class RecebimentoContaErro(ValueError):
    def __init__(self, mensagem, destino="form"):
        super().__init__(mensagem)
        self.destino = destino


def _aplicar_recebimento_conta(
    conta,
    data_recebimento,
    valor_recebido,
    forma_pagamento,
    observacao,
    destino_diferenca,
    credito_utilizado=Decimal("0.00"),
):
    # Deve ser chamado dentro de transaction.atomic(); a conta deve estar bloqueada com select_for_update().
    if conta.status == ContaReceber.STATUS_CANCELADA:
        raise RecebimentoContaErro("Conta cancelada nao pode receber pagamento.", destino="retorno")
    if conta.status == ContaReceber.STATUS_PAGA:
        raise RecebimentoContaErro("Conta ja esta paga.", destino="retorno")

    saldo_atual = (conta.valor_em_aberto or Decimal("0.00")).quantize(Decimal("0.01"))
    if saldo_atual <= Decimal("0.00"):
        conta.status = ContaReceber.STATUS_PAGA
        conta.save(update_fields=["status", "atualizado_em"])
        raise RecebimentoContaErro("Conta ja esta paga.", destino="retorno")

    if credito_utilizado > Decimal("0.00"):
        if not conta.cliente_id:
            raise RecebimentoContaErro("Para usar credito, a conta precisa ter cliente vinculado.")
        credito_atual = (
            CreditoCliente.objects.select_for_update()
            .filter(cliente_id=conta.cliente_id)
            .aggregate(total=Sum("valor"))
            .get("total")
            or Decimal("0.00")
        ).quantize(Decimal("0.01"))
        credito_atual = max(credito_atual, Decimal("0.00"))
        if credito_utilizado > credito_atual:
            raise RecebimentoContaErro("Credito utilizado maior que o saldo disponivel do cliente.")
        if credito_utilizado > saldo_atual:
            raise RecebimentoContaErro("Credito utilizado maior que o saldo em aberto da conta.")

    total_para_baixa = (valor_recebido + credito_utilizado).quantize(Decimal("0.01"))
    valor_aplicado = min(total_para_baixa, saldo_atual).quantize(Decimal("0.01"))
    troco_devolvido = (total_para_baixa - valor_aplicado).quantize(Decimal("0.01"))
    if troco_devolvido > Decimal("0.00") and destino_diferenca == "credito" and not conta.cliente_id:
        raise RecebimentoContaErro("Para deixar diferenca como credito, a conta precisa ter cliente vinculado.")

    observacao_recebimento = observacao

    if troco_devolvido > Decimal("0.00"):
        descricao_diferenca = (
            f"Credito gerado para o cliente: {_formatar_moeda(troco_devolvido)}."
            if destino_diferenca == "credito"
            else f"Troco devolvido: {_formatar_moeda(troco_devolvido)}."
        )
        observacao_automatica = (
            f"Valor entregue pelo cliente: {_formatar_moeda(valor_recebido)}. "
            f"Valor aplicado na conta: {_formatar_moeda(valor_aplicado)}. "
            f"{descricao_diferenca}"
        )
        observacao_recebimento = (
            f"{observacao_recebimento}\n{observacao_automatica}"
            if observacao_recebimento
            else observacao_automatica
        )

    observacao_credito = ""
    if credito_utilizado > Decimal("0.00"):
        observacao_credito = (
            f"Valor recebido: {_formatar_moeda(valor_recebido)}. "
            f"Credito utilizado: {_formatar_moeda(credito_utilizado)}."
        )
        observacao_recebimento = (
            f"{observacao_recebimento}\n{observacao_credito}"
            if observacao_recebimento
            else observacao_credito
        )

    recebimento = RecebimentoContaReceber.objects.create(
        conta=conta,
        data_recebimento=data_recebimento,
        valor=valor_aplicado,
        forma_pagamento=(
            "Credito do cliente"
            if credito_utilizado > Decimal("0.00") and valor_recebido == Decimal("0.00")
            else forma_pagamento
        ),
        observacao=observacao_recebimento,
    )
    credito_gerado = Decimal("0.00")
    if credito_utilizado > Decimal("0.00"):
        CreditoCliente.objects.create(
            cliente=conta.cliente,
            valor=-credito_utilizado,
            tipo=CreditoCliente.TIPO_CREDITO_GERADO,
            origem_conta_receber=conta,
            origem_recebimento=recebimento,
            observacao=observacao_credito,
        )
    if troco_devolvido > Decimal("0.00") and destino_diferenca == "credito":
        CreditoCliente.objects.create(
            cliente=conta.cliente,
            valor=troco_devolvido,
            tipo=CreditoCliente.TIPO_CREDITO_GERADO,
            origem_conta_receber=conta,
            origem_recebimento=recebimento,
            observacao=observacao_recebimento,
        )
        credito_gerado = troco_devolvido

    novo_valor_aberto = (saldo_atual - valor_aplicado).quantize(Decimal("0.01"))
    conta.valor_em_aberto = novo_valor_aberto
    conta.status = (
        ContaReceber.STATUS_PAGA
        if novo_valor_aberto == Decimal("0.00")
        else ContaReceber.STATUS_PARCIAL
    )
    conta.save(update_fields=["valor_em_aberto", "status", "atualizado_em"])

    return {
        "conta": conta,
        "recebimento": recebimento,
        "valor_aplicado": valor_aplicado,
        "troco_devolvido": troco_devolvido,
        "credito_gerado": credito_gerado,
        "credito_utilizado": credito_utilizado,
        "saldo_restante": novo_valor_aberto,
    }


@ensure_csrf_cookie
def conta_receber_receber(request, pk):
    conta = get_object_or_404(
        ContaReceber.objects.select_related("cliente", "venda"),
        pk=pk,
    )
    retorno_url = _url_retorno_segura(request)
    destino_retorno = retorno_url or reverse("estoque:contas_receber")
    url_receber = reverse("estoque:conta_receber_receber", kwargs={"pk": conta.pk})
    if retorno_url:
        url_receber = f"{url_receber}?{urlencode({'next': retorno_url})}"

    if conta.status == ContaReceber.STATUS_CANCELADA:
        messages.warning(request, "Conta cancelada nao pode receber pagamento.")
        return redirect(destino_retorno)
    if conta.status == ContaReceber.STATUS_PAGA:
        messages.warning(request, "Conta ja esta paga.")
        return redirect(destino_retorno)

    formas_pagamento = (
        "Dinheiro",
        "PIX",
        "Cartao de debito",
        "Cartao de credito",
        "Transferencia",
        "Outro",
    )
    valores = {
        "data_recebimento": timezone.localdate().isoformat(),
        "valor": str(conta.valor_em_aberto or Decimal("0.00")),
        "forma_pagamento": "Dinheiro",
        "observacao": "",
        "destino_diferenca": "troco",
        "usar_credito": "",
        "credito_utilizado": "0,00",
    }
    pagamentos_recentes = []
    credito_disponivel = Decimal("0.00")
    credito_sugerido = Decimal("0.00")
    if conta.cliente_id:
        credito_disponivel = (
            CreditoCliente.objects.filter(cliente_id=conta.cliente_id)
            .aggregate(total=Sum("valor"))
            .get("total")
            or Decimal("0.00")
        ).quantize(Decimal("0.01"))
        credito_disponivel = max(credito_disponivel, Decimal("0.00"))
        credito_sugerido = min(
            credito_disponivel,
            conta.valor_em_aberto or Decimal("0.00"),
        ).quantize(Decimal("0.01"))
        valores["credito_utilizado"] = str(credito_sugerido)
        limite_recente = timezone.now() - timedelta(hours=72)
        recebimentos_recentes = (
            RecebimentoContaReceber.objects.select_related("conta", "conta__venda")
            .filter(conta__cliente_id=conta.cliente_id, criado_em__gte=limite_recente)
            .order_by("-criado_em", "-id")[:8]
        )
        pagamentos_recentes = [
            {
                "criado_em": recebimento.criado_em,
                "criado_em_data": timezone.localtime(recebimento.criado_em).date().isoformat(),
                "data_recebimento": recebimento.data_recebimento.isoformat() if recebimento.data_recebimento else "",
                "valor": recebimento.valor,
                "valor_numero": float(recebimento.valor or Decimal("0.00")),
                "forma_pagamento": recebimento.forma_pagamento,
                "conta_id": recebimento.conta_id,
                "venda_id": recebimento.conta.venda_id if recebimento.conta_id else "",
                "observacao": recebimento.observacao,
            }
            for recebimento in recebimentos_recentes
        ]

    if request.method == "POST":
        valores = {
            "data_recebimento": request.POST.get("data_recebimento", "").strip(),
            "valor": request.POST.get("valor", "").strip(),
            "forma_pagamento": request.POST.get("forma_pagamento", "").strip(),
            "observacao": request.POST.get("observacao", "").strip(),
            "destino_diferenca": request.POST.get("destino_diferenca", "troco").strip(),
            "usar_credito": request.POST.get("usar_credito", "").strip(),
            "credito_utilizado": request.POST.get("credito_utilizado", "").strip(),
        }
        data_recebimento = parse_date(valores["data_recebimento"])
        if not data_recebimento:
            messages.warning(request, "Informe uma data de recebimento valida.")
        elif valores["forma_pagamento"] not in formas_pagamento:
            messages.warning(request, "Selecione uma forma de pagamento.")
        elif valores["destino_diferenca"] not in {"troco", "credito"}:
            messages.warning(request, "Selecione como tratar a diferenca do pagamento.")
        else:
            try:
                valor_recebido = _decimal_do_front(valores["valor"] or "0", "0.01")
                credito_utilizado = (
                    _decimal_do_front(valores["credito_utilizado"] or "0", "0.01")
                    if valores["usar_credito"] == "1"
                    else Decimal("0.00")
                )
            except ValueError as exc:
                messages.warning(request, str(exc))
            else:
                if valor_recebido < Decimal("0.00"):
                    messages.warning(request, "Informe um valor recebido valido.")
                elif valores["usar_credito"] == "1" and credito_utilizado <= Decimal("0.00"):
                    messages.warning(request, "Informe um credito utilizado maior que zero.")
                elif valor_recebido <= Decimal("0.00") and credito_utilizado <= Decimal("0.00"):
                    messages.warning(request, "Informe um valor recebido ou use credito do cliente.")
                else:
                    with transaction.atomic():
                        conta_atual = ContaReceber.objects.select_for_update().get(pk=conta.pk)
                        try:
                            _aplicar_recebimento_conta(
                                conta_atual,
                                data_recebimento,
                                valor_recebido,
                                valores["forma_pagamento"],
                                valores["observacao"],
                                valores["destino_diferenca"],
                                credito_utilizado,
                            )
                        except RecebimentoContaErro as exc:
                            messages.warning(request, str(exc))
                            return redirect(
                                destino_retorno
                                if exc.destino == "retorno"
                                else url_receber
                            )

                    messages.success(request, "Recebimento registrado com sucesso.")
                    return redirect(destino_retorno)

    return render(
        request,
        "estoque/conta_receber_receber.html",
        {
            "conta": conta,
            "valores": valores,
            "formas_pagamento": formas_pagamento,
            "pagamentos_recentes": pagamentos_recentes,
            "credito_disponivel": credito_disponivel,
            "credito_sugerido": credito_sugerido,
            "hoje_iso": timezone.localdate().isoformat(),
            "retorno_url": retorno_url,
        },
    )


@ensure_csrf_cookie
def entregas_dia(request):
    hoje = timezone.localdate()
    data_texto = (request.POST.get("data") if request.method == "POST" else request.GET.get("data", "")).strip()
    data_entrega = parse_date(data_texto) if data_texto else hoje
    if not data_entrega:
        data_entrega = hoje
        messages.warning(request, "Data invalida. Mostrando entregas de hoje.")

    if request.method == "POST":
        acao = request.POST.get("acao")
        observacao = request.POST.get("observacao", "").strip()

        if acao == "unitaria":
            venda_id = request.POST.get("venda_unitaria", "").strip()
            if not venda_id.isdigit():
                messages.warning(request, "Selecione uma venda para criar a entrega.", extra_tags="entrega-unitaria")
                return redirect(f"{reverse('estoque:entregas_dia')}?data={data_entrega.isoformat()}")

            venda = Venda.objects.filter(pk=venda_id).first()
            if not venda:
                messages.warning(request, "Selecione uma venda valida para criar a entrega.", extra_tags="entrega-unitaria")
                return redirect(f"{reverse('estoque:entregas_dia')}?data={data_entrega.isoformat()}")

            if EntregaRotaItem.objects.filter(venda=venda).exists():
                messages.warning(request, "Essa venda ja possui entrega criada.", extra_tags="entrega-unitaria")
                return redirect(f"{reverse('estoque:entregas_dia')}?data={data_entrega.isoformat()}")

            with transaction.atomic():
                rota = EntregaRota.objects.create(
                    data=data_entrega,
                    tipo=EntregaRota.TIPO_UNITARIA,
                    observacao=observacao,
                )
                EntregaRotaItem.objects.create(
                    rota=rota,
                    venda=venda,
                    ordem_entrega=1,
                )
            messages.success(request, f"Entrega unitaria #{rota.id} criada para a venda #{venda.id}.", extra_tags="entrega-unitaria")
            return redirect(f"{reverse('estoque:entregas_dia')}?data={data_entrega.isoformat()}")

        if acao == "rota":
            nome_rota = request.POST.get("nome_rota", "").strip()
            venda_ids = [
                venda_id.strip()
                for venda_id in request.POST.getlist("vendas_rota")
                if venda_id.strip().isdigit()
            ]
            pendencia_ids = [
                pendencia_id.strip()
                for pendencia_id in request.POST.getlist("pendencias_rota")
                if pendencia_id.strip().isdigit()
            ]
            pendencia_ids_selecionados = {int(pendencia_id) for pendencia_id in pendencia_ids}
            pendencias_selecionadas = pendencias_checklist_validas(pendencia_ids)
            vendas = list(Venda.objects.filter(pk__in=venda_ids).select_related("cliente"))
            if not vendas and not pendencias_selecionadas:
                messages.warning(request, "Selecione pelo menos uma venda para criar a rota.", extra_tags="rota-entrega")
                return redirect(f"{reverse('estoque:entregas_dia')}?data={data_entrega.isoformat()}")

            vendas_por_id = {str(venda.id): venda for venda in vendas}
            ordenadas = []
            for venda_id in venda_ids:
                venda = vendas_por_id.get(str(venda_id))
                if not venda:
                    continue
                ordem = ordem_postada(request.POST.get(f"ordem_{venda_id}"))
                ordenadas.append((ordem, venda.id, venda))

            ordenadas.sort(key=lambda item: (item[0], item[1]))
            nome_rota_final = localidade_principal_rota(ordenadas) or formatar_nome_rota(nome_rota)
            observacao_rota = observacao
            if nome_rota_final:
                observacao_rota = f"Rota: {nome_rota_final}"
                if observacao:
                    observacao_rota = f"{observacao_rota}\n{observacao}"

            ordem_por_pendencia = {}
            for pendencia_id in pendencia_ids:
                ordem_por_pendencia[int(pendencia_id)] = ordem_postada(
                    request.POST.get(f"ordem_pendencia_{pendencia_id}")
                )

            pendencias_por_venda = {}
            for checklist in pendencias_selecionadas:
                if checklist.id not in pendencia_ids_selecionados:
                    continue
                pendencias_por_venda.setdefault(checklist.rota_item.venda_id, {
                    "ordem": ordem_por_pendencia.get(checklist.id, 9999),
                    "checklists": [],
                })
                grupo = pendencias_por_venda[checklist.rota_item.venda_id]
                grupo["ordem"] = min(grupo["ordem"], ordem_por_pendencia.get(checklist.id, 9999))
                grupo["checklists"].append(checklist)

            pendencias_por_cliente = {}
            for venda_id_pendencia, dados_pendencia in pendencias_por_venda.items():
                checklists = dados_pendencia["checklists"]
                origem = checklists[0].rota_item
                chave_cliente = chave_cliente_entrega(origem.venda)
                pendencias_por_cliente.setdefault(chave_cliente, []).append((
                    dados_pendencia["ordem"],
                    venda_id_pendencia,
                    checklists,
                ))
            for pendencias_cliente in pendencias_por_cliente.values():
                pendencias_cliente.sort(key=lambda item: (item[0], item[1]))

            clientes_com_venda_normal = {
                chave_cliente_entrega(venda)
                for _, __, venda in ordenadas
            }
            itens_ordenaveis = [
                (ordem, venda_id, "normal", venda, None)
                for ordem, venda_id, venda in ordenadas
            ]
            for venda_id_pendencia, dados_pendencia in pendencias_por_venda.items():
                checklists = dados_pendencia["checklists"]
                venda_pendencia = checklists[0].rota_item.venda
                if chave_cliente_entrega(venda_pendencia) in clientes_com_venda_normal:
                    continue
                itens_ordenaveis.append((
                    dados_pendencia["ordem"],
                    venda_id_pendencia,
                    "pendencia",
                    venda_pendencia,
                    checklists,
                ))

            itens_ordenaveis.sort(key=lambda item: (item[0], item[1]))
            sequencia_rota = []
            for indice_item, (_, __, tipo_item, venda, checklists) in enumerate(itens_ordenaveis):
                sequencia_rota.append((tipo_item, venda, checklists))
                if tipo_item != "normal":
                    continue

                chave_cliente = chave_cliente_entrega(venda)
                tem_outra_venda_cliente = any(
                    tipo_futuro == "normal"
                    and chave_cliente_entrega(venda_futura) == chave_cliente
                    for _, __, tipo_futuro, venda_futura, ___ in itens_ordenaveis[indice_item + 1:]
                )
                if tem_outra_venda_cliente:
                    continue
                for _, ___, checklists_pendencia in pendencias_por_cliente.get(chave_cliente, []):
                    sequencia_rota.append(("pendencia", checklists_pendencia[0].rota_item.venda, checklists_pendencia))

            with transaction.atomic():
                rota = EntregaRota.objects.create(
                    data=data_entrega,
                    tipo=EntregaRota.TIPO_ROTA,
                    observacao=observacao_rota,
                )
                for ordem_entrega, (tipo_item, venda, checklists_origem) in enumerate(sequencia_rota, start=1):
                    if tipo_item == "normal":
                        EntregaRotaItem.objects.create(
                            rota=rota,
                            venda=venda,
                            ordem_entrega=ordem_entrega,
                        )
                        continue

                    checklists_para_criar = checklists_pendencia_selecionados(
                        checklists_origem,
                        pendencia_ids_selecionados,
                    )
                    if not checklists_para_criar:
                        continue

                    origem = checklists_origem[0].rota_item
                    item_pendencia = EntregaRotaItem.objects.create(
                        rota=rota,
                        venda=venda,
                        ordem_entrega=ordem_entrega,
                        is_pendencia=True,
                        origem_pendencia=origem,
                        observacao=(
                            f"Pendencia incluida da rota #{origem.rota_id} "
                            f"em {origem.rota.data:%d/%m/%Y}."
                        ),
                    )
                    EntregaChecklistItem.objects.bulk_create([
                        EntregaChecklistItem(rota_item=item_pendencia, item_venda=checklist.item_venda)
                        for checklist in checklists_para_criar
                    ])
            total_entregas = len(sequencia_rota)
            messages.success(request, f"Rota #{rota.id} criada com {total_entregas} entrega(s).", extra_tags="rota-entrega")
            return redirect(f"{reverse('estoque:entregas_dia')}?data={data_entrega.isoformat()}&rota_criada={rota.id}#rota-{rota.id}")

    vendas_lista = list(
        Venda.objects.select_related("cliente")
        .filter(data_venda=data_entrega)
        .order_by("-id")
    )
    vendas_ids = [venda.id for venda in vendas_lista]
    itens_entrega = EntregaRotaItem.objects.filter(
        venda_id__in=vendas_ids,
    ).select_related("rota").order_by("-rota__data", "-rota_id", "-id")
    status_por_venda = {}
    for item in itens_entrega:
        status_por_venda.setdefault(
            item.venda_id,
            f"{item.rota.get_tipo_display()} #{item.rota_id} - {item.get_status_display()}",
        )
    for venda in vendas_lista:
        venda.tem_entrega_criada = venda.id in status_por_venda
        venda.entrega_status_texto = status_por_venda.get(venda.id, "Sem entrega criada")

    rotas = list(
        EntregaRota.objects.filter(data=data_entrega)
        .prefetch_related("itens__venda__cliente", "itens__checklist_itens__item_venda__produto")
        .order_by("-id")
    )
    for rota in rotas:
        itens = list(rota.itens.all())
        for item in itens:
            item.resumo_pendencia = resumo_pendencia_rota_item(item)
        rota.itens_entrega = itens
        rota.itens_carregamento = list(reversed(itens))
        rota.checklist_path = reverse("estoque:entrega_rota_checklist", kwargs={"pk": rota.id})
        rota.checklist_url = montar_checklist_url(request, rota.id)
        rota.pode_excluir = rota_pode_ser_excluida(rota)

    return render(
        request,
        "estoque/entregas_dia.html",
        {
            "data_entrega": data_entrega,
            "vendas": vendas_lista,
            "rotas": rotas,
            "total_vendas": len(vendas_lista),
            "funcionarios_habilitados": Funcionario.habilitados_para_checklist(),
            "rota_criada_id": request.GET.get("rota_criada", ""),
            "total_pendencias_entrega": len(listar_pendencias_entrega()),
            "pendencias_sugeridas": pendencias_sugeriveis_entrega(),
        },
    )


def pendencias_entrega(request):
    pendencias = listar_pendencias_entrega()
    return render(
        request,
        "estoque/pendencias_entrega.html",
        {
            "pendencias": pendencias,
            "total_pendencias": len(pendencias),
        },
    )


def revisar_remocao_pendencia_da_nota(request, checklist_id):
    checklist = (
        EntregaChecklistItem.objects.select_related(
            "item_venda",
            "item_venda__produto",
            "rota_item",
            "rota_item__rota",
            "rota_item__venda",
            "rota_item__venda__cliente",
            "rota_item__origem_pendencia",
        )
        .prefetch_related("rota_item__checklist_itens", "rota_item__venda__itens")
        .filter(pk=checklist_id)
        .first()
    )
    if not checklist:
        messages.warning(request, "Pendencia nao encontrada ou ja resolvida.")
        return redirect("estoque:pendencias_entrega")

    item_rota = checklist.rota_item
    venda = item_rota.venda
    item_venda = checklist.item_venda
    if checklist.entregue or checklist not in checklists_validos_rota_item(item_rota):
        messages.warning(request, "Essa pendencia nao esta mais disponivel para remocao da nota.")
        return redirect("estoque:pendencias_entrega")

    total_atual = venda.total or Decimal("0.00")
    novo_total = calcular_total_itens_venda(venda, excluir_item_id=item_venda.id)
    voltar_url = request.GET.get("next") or reverse("estoque:pendencias_entrega")

    if request.method == "POST":
        with transaction.atomic():
            checklist_confirmacao = (
                EntregaChecklistItem.objects.filter(pk=checklist_id)
                .first()
            )
            if not checklist_confirmacao:
                messages.warning(request, "Pendencia nao encontrada ou ja resolvida.")
                return redirect("estoque:pendencias_entrega")

            item_rota = (
                EntregaRotaItem.objects.select_related("origem_pendencia")
                .prefetch_related("checklist_itens")
                .get(pk=checklist_confirmacao.rota_item_id)
            )
            venda = Venda.objects.get(pk=item_rota.venda_id)
            item_venda = ItemVenda.objects.filter(
                pk=checklist_confirmacao.item_venda_id,
                venda=venda,
            ).select_related("produto").first()
            if not item_venda:
                messages.warning(request, "O item da nota ja nao existe mais. Nenhuma alteracao foi feita.")
                return redirect("estoque:pendencias_entrega")

            if checklist_confirmacao.entregue or checklist_confirmacao not in checklists_validos_rota_item(item_rota):
                messages.warning(request, "Essa pendencia nao esta mais disponivel para remocao da nota.")
                return redirect("estoque:pendencias_entrega")

            produto_nome = item_venda.produto.nome if item_venda.produto else "Produto nao identificado"
            quantidade = item_venda.quantidade
            unidade = item_venda.unidade
            valor_total = item_venda.valor_total or Decimal("0.00")
            venda_id = venda.id
            rota_id = item_rota.rota_id
            item_venda.delete()
            novo_total = recalcular_total_venda(venda)

            _registrar_evento_venda(
                venda,
                "pendencia_removida_da_nota",
                (
                    f"Pendencia da rota #{rota_id} resolvida por remocao da nota. "
                    f"Item removido: {produto_nome} - {quantidade} {unidade} "
                    f"(R$ {valor_total:.2f}). Novo total: R$ {novo_total:.2f}."
                ),
                canal="sistema",
            )

        messages.success(
            request,
            f"Item removido da venda #{venda_id} e pendencia resolvida com sucesso.",
        )
        return redirect(f"{reverse('estoque:venda_detalhe', kwargs={'pk': venda_id})}?origem=pendencias")

    return render(
        request,
        "estoque/revisar_remocao_pendencia_da_nota.html",
        {
            "checklist": checklist,
            "item_rota": item_rota,
            "rota": item_rota.rota,
            "venda": venda,
            "cliente": venda.cliente,
            "item_venda": item_venda,
            "produto_nome": item_venda.produto.nome if item_venda.produto else "Produto nao identificado",
            "total_atual": total_atual,
            "novo_total": novo_total,
            "voltar_url": voltar_url,
        },
    )


@require_POST
def entrega_rota_excluir(request, pk):
    rota = EntregaRota.objects.prefetch_related("itens__checklist_itens").filter(pk=pk).first()
    if not rota:
        data_texto = request.POST.get("data", "").strip()
        data_entrega = parse_date(data_texto) if data_texto else timezone.localdate()
        if not data_entrega:
            data_entrega = timezone.localdate()
        messages.warning(request, "Rota nao encontrada ou ja foi excluida.")
        return redirect(f"{reverse('estoque:entregas_dia')}?data={data_entrega.isoformat()}")

    data_entrega = rota.data

    if not rota_pode_ser_excluida(rota):
        messages.warning(
            request,
            f"Rota #{rota.id} ja possui conferencia ou uso registrado e nao pode ser excluida.",
        )
        return redirect(f"{reverse('estoque:entregas_dia')}?data={data_entrega.isoformat()}#rota-{rota.id}")

    rota_id = rota.id
    rota_foco = (
        EntregaRota.objects.filter(data=data_entrega, id__lt=rota_id)
        .order_by("-id")
        .first()
        or EntregaRota.objects.filter(data=data_entrega, id__gt=rota_id)
        .order_by("id")
        .first()
    )
    with transaction.atomic():
        rota.delete()

    messages.success(request, f"Rota #{rota_id} excluida com sucesso.")
    redirect_url = f"{reverse('estoque:entregas_dia')}?data={data_entrega.isoformat()}"
    if rota_foco:
        redirect_url = f"{redirect_url}&rota_criada={rota_foco.id}#rota-{rota_foco.id}"
    return redirect(redirect_url)


def entrega_rota_detalhe(request, pk):
    rota = get_object_or_404(
        EntregaRota.objects.prefetch_related(
            "itens__venda__cliente",
            "itens__venda__itens__produto",
            "itens__checklist_itens__item_venda__produto",
        ),
        pk=pk,
    )
    itens_entrega = list(rota.itens.all())
    for item_rota in itens_entrega:
        item_rota.checklists_ordenados = checklists_validos_rota_item(item_rota)
        item_rota.resumo_pendencia = resumo_pendencia_rota_item(item_rota)
    itens_carregamento = list(reversed(itens_entrega))
    checklist_url = montar_checklist_url(request, rota.id)

    return render(
        request,
        "estoque/entrega_rota_detalhe.html",
        {
            "rota": rota,
            "itens_entrega": itens_entrega,
            "itens_carregamento": itens_carregamento,
            "checklist_url": checklist_url,
            "funcionarios_habilitados": Funcionario.habilitados_para_checklist(),
        },
    )


def entrega_rota_checklist(request, pk):
    rota = get_object_or_404(
        EntregaRota.objects.prefetch_related(
            "itens__venda__cliente",
            "itens__venda__itens__produto",
            "itens__checklist_itens",
        ),
        pk=pk,
    )
    itens_entrega = list(rota.itens.all())

    with transaction.atomic():
        for item_rota in itens_entrega:
            itens_venda = list(item_rota.venda.itens.all())
            existentes = {
                checklist.item_venda_id: checklist
                for checklist in item_rota.checklist_itens.all()
            }
            novos = [
                EntregaChecklistItem(rota_item=item_rota, item_venda=item_venda)
                for item_venda in itens_venda
                if item_venda.id not in existentes and not item_rota.is_pendencia
            ]
            if novos:
                EntregaChecklistItem.objects.bulk_create(novos)

    rota = get_object_or_404(
        EntregaRota.objects.prefetch_related(
            "itens__venda__cliente",
            "itens__venda__itens__produto",
            "itens__checklist_itens__item_venda__produto",
        ),
        pk=pk,
    )
    itens_entrega = list(rota.itens.all())

    if request.method == "POST":
        bloco_salvo = request.POST.get("salvar_bloco") or request.POST.get("salvar_bloco_alvo", "")
        bloco_partes = bloco_salvo.split(":", 1)
        bloco_rota_item_id = bloco_partes[0] if bloco_partes else ""
        bloco_fase = bloco_partes[1] if len(bloco_partes) > 1 else ""
        rota_item_ids = [item_rota.id for item_rota in itens_entrega]
        bloco_valido = bloco_rota_item_id.isdigit() and int(bloco_rota_item_id) in rota_item_ids
        if bloco_valido:
            rota_item_ids = [int(bloco_rota_item_id)]

        checklists_validos_ids = set()
        for item_rota in itens_entrega:
            if item_rota.id in rota_item_ids:
                checklists_validos_ids.update(
                    checklist.id for checklist in checklists_validos_rota_item(item_rota)
                )
        checklist_qs = EntregaChecklistItem.objects.filter(pk__in=checklists_validos_ids)

        for checklist in checklist_qs:
            if bloco_fase == "carregamento":
                checklist.carregado = f"carregado_{checklist.id}" in request.POST
                checklist.save(update_fields=["carregado", "atualizado_em"])
            elif bloco_fase == "entrega":
                checklist.entregue = f"entregue_{checklist.id}" in request.POST
                checklist.save(update_fields=["entregue", "atualizado_em"])

        if bloco_fase == "entrega":
            for item_rota in itens_entrega:
                if item_rota.id not in rota_item_ids:
                    continue
                item_rota.conferido_cliente = f"conferido_{item_rota.id}" in request.POST
                item_rota.entrega_concluida = f"concluida_{item_rota.id}" in request.POST
                item_rota.save(update_fields=["conferido_cliente", "entrega_concluida"])

        if bloco_valido and bloco_fase in CHECKLIST_FASE_MARKERS:
            item_rota_salvo = next(
                (item_rota for item_rota in itens_entrega if item_rota.id == int(bloco_rota_item_id)),
                None,
            )
            if item_rota_salvo:
                marcar_checklist_fase_salva(item_rota_salvo, bloco_fase)

        redirect_url = reverse("estoque:entrega_rota_checklist", kwargs={"pk": rota.id})
        if bloco_valido and bloco_fase in {"carregamento", "entrega"}:
            redirect_url = f"{redirect_url}?salvo_item={bloco_rota_item_id}&salvo_fase={bloco_fase}"
        else:
            messages.success(request, "Checklist salvo com sucesso.", extra_tags="checklist-global")
        return redirect(redirect_url)

    rota = get_object_or_404(
        EntregaRota.objects.prefetch_related(
            "itens__venda__cliente",
            "itens__venda__itens__produto",
            "itens__checklist_itens__item_venda__produto",
        ),
        pk=pk,
    )
    itens_entrega = list(rota.itens.all())
    salvo_item_id = request.GET.get("salvo_item", "")
    salvo_fase = request.GET.get("salvo_fase", "")
    eventos_checklist_enviado = set(
        EventoVenda.objects.filter(
            venda_id__in=[item_rota.venda_id for item_rota in itens_entrega],
            tipo_evento="checklist_cliente_enviado",
            canal="whatsapp_checklist",
            descricao__icontains=f"rota/entrega #{rota.id}",
        ).values_list("descricao", flat=True)
    )
    for item_rota in itens_entrega:
        item_rota.salvo_carregamento = salvo_item_id == str(item_rota.id) and salvo_fase == "carregamento"
        item_rota.salvo_entrega = salvo_item_id == str(item_rota.id) and salvo_fase == "entrega"
        checklists_validos = checklists_validos_rota_item(item_rota)
        item_rota.checklists_por_item = {
            checklist.item_venda_id: checklist
            for checklist in checklists_validos
        }
        item_rota.checklists_ordenados = [
            item_rota.checklists_por_item.get(item_venda.id)
            for item_venda in item_rota.venda.itens.all()
            if item_rota.checklists_por_item.get(item_venda.id)
        ]
        item_rota.resumo_pendencia = resumo_pendencia_rota_item(item_rota)
        item_rota.carregamento_salvo = checklist_fase_salva(item_rota, "carregamento")
        item_rota.entrega_salva = checklist_fase_salva(item_rota, "entrega")
        item_rota.carregamento_completo = (
            bool(item_rota.checklists_ordenados)
            and all(checklist.carregado for checklist in item_rota.checklists_ordenados)
        )
        item_rota.entrega_completa = (
            bool(item_rota.checklists_ordenados)
            and all(checklist.entregue for checklist in item_rota.checklists_ordenados)
        )
        item_rota.carregamento_conferido = (
            item_rota.salvo_carregamento
            or item_rota.carregamento_salvo
            or item_rota.carregamento_completo
        )
        item_rota.entrega_conferida = (
            item_rota.salvo_entrega
            or item_rota.entrega_salva
            or item_rota.conferido_cliente
            or item_rota.entrega_concluida
            or item_rota.entrega_completa
        )
        item_rota.carregamento_pendente_salva = (
            item_rota.carregamento_conferido
            and bool(item_rota.checklists_ordenados)
            and not item_rota.carregamento_completo
        )
        item_rota.entrega_pendente_salva = (
            item_rota.entrega_conferida
            and bool(item_rota.checklists_ordenados)
            and not item_rota.entrega_completa
        )
        item_rota.checklist_cliente_enviado = any(
            f"bloco #{item_rota.id}" in descricao
            for descricao in eventos_checklist_enviado
        )

    itens_entrega = sorted(
        itens_entrega,
        key=lambda item_rota: (item_rota.ordem_entrega, item_rota.id),
    )
    itens_entrega_pendentes = [
        item_rota for item_rota in itens_entrega if not item_rota.entrega_conferida
    ]
    itens_entrega_conferidos = [
        item_rota for item_rota in itens_entrega if item_rota.entrega_conferida
    ]
    itens_carregamento = sorted(
        itens_entrega,
        key=lambda item_rota: (-item_rota.ordem_entrega, -item_rota.id),
    )
    itens_carregamento_pendentes = [
        item_rota for item_rota in itens_carregamento if not item_rota.carregamento_conferido
    ]
    itens_carregamento_conferidos = [
        item_rota for item_rota in itens_carregamento if item_rota.carregamento_conferido
    ]

    for item_rota in itens_entrega:
        item_rota.checklist_url = montar_checklist_cliente_url(request, rota.id, item_rota.venda_id, item_rota.id)

    return render(
        request,
        "estoque/entrega_checklist.html",
        {
            "rota": rota,
            "itens_entrega": itens_entrega,
            "itens_entrega_pendentes": itens_entrega_pendentes,
            "itens_entrega_conferidos": itens_entrega_conferidos,
            "itens_carregamento": itens_carregamento,
            "itens_carregamento_pendentes": itens_carregamento_pendentes,
            "itens_carregamento_conferidos": itens_carregamento_conferidos,
            "salvo_item_id": salvo_item_id,
            "salvo_fase": salvo_fase,
            "checklist_url": montar_checklist_url(request, rota.id),
            "funcionarios_habilitados": Funcionario.habilitados_para_checklist(),
        },
    )


def entrega_rota_checklist_cliente(request, rota_id, venda_id=None, rota_item_id=None):
    item_rota_qs = EntregaRotaItem.objects.select_related("rota", "venda", "venda__cliente").prefetch_related(
        "checklist_itens__item_venda__produto", "venda__itens__produto"
    )
    if rota_item_id:
        item_rota = get_object_or_404(item_rota_qs, rota_id=rota_id, pk=rota_item_id)
    else:
        item_rota = item_rota_qs.filter(rota_id=rota_id, venda_id=venda_id).order_by("is_pendencia", "id").first()
        if not item_rota:
            raise Http404("Checklist do cliente nao encontrado.")

    with transaction.atomic():
        itens_venda = list(item_rota.venda.itens.all())
        existentes = {
            checklist.item_venda_id: checklist
            for checklist in item_rota.checklist_itens.all()
        }
        novos = [
            EntregaChecklistItem(rota_item=item_rota, item_venda=item_venda)
            for item_venda in itens_venda
            if item_venda.id not in existentes and not item_rota.is_pendencia
        ]
        if novos:
            EntregaChecklistItem.objects.bulk_create(novos)

    checklists_validos = checklists_validos_rota_item(item_rota)
    checklists = {
        checklist.item_venda_id: checklist
        for checklist in checklists_validos
    }

    itens = []
    for item_venda in itens_venda:
        checklist = checklists.get(item_venda.id)
        if not checklist:
            continue
        if checklist.entregue:
            status = "Entregue"
        elif checklist.carregado:
            status = "Carregado"
        else:
            status = "Pendente"
        itens.append({
            "produto_nome": item_venda.produto.nome if item_venda.produto else "Produto nao identificado",
            "quantidade": item_venda.quantidade,
            "unidade": item_venda.unidade,
            "carregado": checklist.carregado,
            "entregue": checklist.entregue,
            "status": status,
        })

    final_statuses = []
    if item_rota.conferido_cliente:
        final_statuses.append("Entrega conferida com cliente")
    if item_rota.entrega_concluida:
        final_statuses.append("Entrega concluída")

    return render(
        request,
        "estoque/entrega_checklist_cliente.html",
        {
            "rota": item_rota.rota,
            "item_rota": item_rota,
            "venda": item_rota.venda,
            "cliente": item_rota.venda.cliente,
            "itens": itens,
            "final_statuses": final_statuses,
            "checklist_url": montar_checklist_cliente_url(request, rota_id, item_rota.venda_id, item_rota.id),
        },
    )


def _decimal_do_front(valor, casas="0.01"):
    if valor is None:
        raise ValueError("Valor numerico ausente.")

    texto = str(valor).strip().replace("R$", "").replace(" ", "")
    if "," in texto and "." in texto:
        texto = texto.replace(".", "").replace(",", ".")
    else:
        texto = texto.replace(",", ".")

    try:
        return Decimal(texto).quantize(Decimal(casas))
    except (InvalidOperation, ValueError):
        raise ValueError("Valor numerico invalido.")


def _recalcular_total_venda_pelos_itens(venda):
    total_recalculado = sum(
        (
            valor or Decimal("0.00")
            for valor in ItemVenda.objects.filter(venda=venda).values_list("valor_total", flat=True)
        ),
        Decimal("0.00"),
    )
    return total_recalculado.quantize(Decimal("0.01"))


def _ids_itens_adicionados_param(request):
    ids = []
    texto_ids = request.GET.get("itens_adicionados") or request.POST.get("itens_adicionados") or ""
    for parte in str(texto_ids).split(","):
        parte = parte.strip()
        if parte.isdigit():
            ids.append(int(parte))

    item_unico = request.GET.get("item_adicionado") or request.POST.get("item_adicionado") or ""
    if str(item_unico).isdigit():
        ids.append(int(item_unico))

    return list(dict.fromkeys(ids))


@require_POST
def gravar_venda(request):
    try:
        dados = json.loads(request.body.decode("utf-8"))
    except (json.JSONDecodeError, UnicodeDecodeError):
        return JsonResponse(
            {"sucesso": False, "mensagem": "Nao foi possivel ler os dados da venda."},
            status=400,
        )

    itens = dados.get("itens") or []
    if not itens:
        return JsonResponse(
            {"sucesso": False, "mensagem": "Inclua pelo menos 1 item antes de gravar a venda."},
            status=400,
        )

    data_venda = parse_date(dados.get("data_venda") or "")
    if not data_venda:
        return JsonResponse(
            {"sucesso": False, "mensagem": "Informe uma data valida para a venda."},
            status=400,
        )

    data_vencimento = (
        parse_date(dados.get("data_vencimento") or "")
        if dados.get("data_vencimento")
        else None
    )
    cliente = None
    cliente_id = dados.get("cliente_id")

    if cliente_id:
        cliente = Cliente.objects.filter(pk=cliente_id, ativo=True).first()
        if not cliente:
            return JsonResponse(
                {"sucesso": False, "mensagem": "Cliente selecionado nao foi encontrado."},
                status=400,
            )

    itens_validados = []
    total_calculado = Decimal("0.00")

    for item in itens:
        produto_nome = str(item.get("produto_nome") or "").strip()
        unidade = str(item.get("unidade") or "").strip()

        if not produto_nome:
            return JsonResponse(
                {"sucesso": False, "mensagem": "Existe item sem produto informado."},
                status=400,
            )

        try:
            quantidade = _decimal_do_front(item.get("quantidade"), "0.001")
            preco_unitario = _decimal_do_front(item.get("preco_unitario"), "0.01")
        except ValueError:
            return JsonResponse(
                {"sucesso": False, "mensagem": f"Revise quantidade e preco do item {produto_nome}."},
                status=400,
            )

        if quantidade <= 0 or preco_unitario <= 0:
            return JsonResponse(
                {"sucesso": False, "mensagem": f"Quantidade e preco precisam ser maiores que zero em {produto_nome}."},
                status=400,
            )

        valor_total = (quantidade * preco_unitario).quantize(Decimal("0.01"))
        produto = Produto.objects.filter(nome__iexact=produto_nome, excluido=False).first()
        total_calculado += valor_total
        itens_validados.append({
            "produto": produto,
            "quantidade": quantidade,
            "unidade": unidade,
            "preco_unitario": preco_unitario,
            "valor_total": valor_total,
        })

    with transaction.atomic():
        venda = Venda.objects.create(
            cliente=cliente,
            data_venda=data_venda,
            data_vencimento=data_vencimento,
            tipo_pagamento=str(dados.get("tipo_pagamento") or "").strip(),
            operador=str(dados.get("operador") or "").strip(),
            total=total_calculado.quantize(Decimal("0.01")),
        )

        ItemVenda.objects.bulk_create([
            ItemVenda(venda=venda, **item)
            for item in itens_validados
        ])

        _registrar_evento_venda(
            venda,
            "venda_gravada",
            "Venda gravada com sucesso.",
            canal="sistema",
            usuario=venda.operador,
        )
        _sincronizar_conta_receber(venda, "venda gravada")

    return JsonResponse({
        "sucesso": True,
        "mensagem": f"Venda #{venda.id} gravada com sucesso.",
        "venda_id": venda.id,
        "visualizar_url": reverse("estoque:venda_detalhe", args=[venda.id]),
    })


@ensure_csrf_cookie
def venda_detalhe(request, pk):
    venda = get_object_or_404(
        Venda.objects.select_related("cliente").prefetch_related("itens__produto", "eventos"),
        pk=pk,
    )
    retorno_url = _url_retorno_segura(request)
    whatsapp_url = montar_link_whatsapp_venda(venda)
    entrega_contexto = None
    entrega_id = request.GET.get("entrega")
    if entrega_id:
        entrega_contexto = EntregaRota.objects.filter(pk=entrega_id).first()
    itens_adicionados_ids = _ids_itens_adicionados_param(request)
    itens_adicionados_param = ",".join(str(item_id) for item_id in itens_adicionados_ids)
    itens_nota = sorted(
        list(venda.itens.all()),
        key=lambda item: ((item.produto.nome if item.produto else "Produto nao identificado").casefold(), item.id),
    )
    whatsapp_atualizacao = None if venda.cancelada else _montar_whatsapp_atualizacao_nota(request, venda)
    conta_receber = _conta_receber_da_venda(venda)
    alteracoes_pendentes_whatsapp = _resumo_alteracoes_pendentes_whatsapp(
        venda,
        itens_nota,
        incluir_edicoes_registradas=request.GET.get("nota_atualizada") == "1",
    )
    itens_adicionados_destacar_ids = set(itens_adicionados_ids) | alteracoes_pendentes_whatsapp["itens_adicionados_ids"]

    # Verificar se venda já tem entrega/rota
    entrega_existente = EntregaRotaItem.objects.filter(venda=venda).select_related("rota").first()
    entrega_info = None
    if entrega_existente:
        rota = entrega_existente.rota
        checklist_url = montar_checklist_url(request, rota.id)
        entrega_info = {
            "rota": rota,
            "checklist_url": checklist_url,
            "status": rota.get_status_display(),
        }

    _registrar_evento_venda(
        venda,
        "nota_visualizada",
        "Nota visualizada.",
        canal="sistema",
    )
    return render(
        request,
        "estoque/venda_detalhe.html",
        {
            "venda": venda,
            "whatsapp_url": whatsapp_url,
            "eventos": EventoVenda.objects.filter(venda=venda),
            "comunicacoes_whatsapp": EventoVenda.objects.filter(
                venda=venda,
                canal__in=["whatsapp", "whatsapp_checklist"],
                tipo_evento__in=[
                    "whatsapp_aberto",
                    "whatsapp_confirmado",
                    "checklist_cliente_whatsapp_aberto",
                    "checklist_cliente_enviado",
                ],
            ),
            "entrega_contexto": entrega_contexto,
            "entrega_info": entrega_info,
            "funcionarios_habilitados": Funcionario.habilitados_para_checklist(),
            "itens_adicionados_ids": itens_adicionados_ids,
            "itens_adicionados_destacar_ids": itens_adicionados_destacar_ids,
            "itens_adicionados_param": itens_adicionados_param,
            "itens_nota": itens_nota,
            "whatsapp_atualizacao": whatsapp_atualizacao,
            "alteracoes_pendentes_whatsapp": alteracoes_pendentes_whatsapp,
            "conta_receber": conta_receber,
            "venda_a_prazo": _venda_a_prazo(venda),
            "retorno_url": retorno_url,
            "retorno_querystring": _querystring_retorno(retorno_url),
        },
    )


def venda_cliente_detalhe(request, pk):
    venda = get_object_or_404(
        Venda.objects.select_related("cliente").prefetch_related("itens__produto"),
        pk=pk,
    )
    return render(
        request,
        "estoque/venda_cliente_detalhe.html",
        {"venda": venda},
    )


def _bloquear_venda_cancelada(request, venda, destino="estoque:venda_detalhe"):
    if not getattr(venda, "cancelada", False):
        return None
    messages.warning(request, "Venda cancelada / venda nao realizada. Esta acao esta bloqueada.")
    return redirect(destino, pk=venda.pk)


def _venda_a_prazo(venda):
    return normalizar_texto_cliente(venda.tipo_pagamento) in {"a prazo", "carteira"}


def _conta_receber_da_venda(venda):
    try:
        return venda.conta_receber
    except ContaReceber.DoesNotExist:
        return None


def _sincronizar_conta_receber(venda, observacao_origem="", permitir_reabrir_cancelada=False):
    conta = _conta_receber_da_venda(venda)
    origem = f" Origem: {observacao_origem}." if observacao_origem else ""

    if venda.cancelada:
        if conta and conta.status == ContaReceber.STATUS_ABERTA:
            conta.status = ContaReceber.STATUS_CANCELADA
            conta.valor_em_aberto = Decimal("0.00")
            conta.observacao = f"Cancelada por venda nao realizada.{origem}".strip()
            conta.save(update_fields=["status", "valor_em_aberto", "observacao", "atualizado_em"])
        return conta

    if not _venda_a_prazo(venda):
        if conta and conta.status == ContaReceber.STATUS_ABERTA:
            conta.status = ContaReceber.STATUS_CANCELADA
            conta.valor_em_aberto = Decimal("0.00")
            conta.observacao = f"Cancelada porque a venda esta marcada como a vista.{origem}".strip()
            conta.save(update_fields=["status", "valor_em_aberto", "observacao", "atualizado_em"])
        return conta

    valor = (venda.total or Decimal("0.00")).quantize(Decimal("0.01"))
    if conta is None:
        return ContaReceber.objects.create(
            venda=venda,
            cliente=venda.cliente,
            data_emissao=venda.data_venda,
            data_vencimento=venda.data_vencimento,
            valor_original=valor,
            valor_em_aberto=valor,
            status=ContaReceber.STATUS_ABERTA,
            observacao=f"Criada automaticamente para venda a prazo.{origem}".strip(),
        )

    if conta.status == ContaReceber.STATUS_ABERTA or (
        permitir_reabrir_cancelada and conta.status == ContaReceber.STATUS_CANCELADA
    ):
        conta.cliente = venda.cliente
        conta.data_emissao = venda.data_venda
        conta.data_vencimento = venda.data_vencimento
        conta.valor_original = valor
        conta.valor_em_aberto = valor
        conta.status = ContaReceber.STATUS_ABERTA
        conta.observacao = f"Sincronizada automaticamente com venda a prazo.{origem}".strip()
        conta.save(update_fields=[
            "cliente",
            "data_emissao",
            "data_vencimento",
            "valor_original",
            "valor_em_aberto",
            "status",
            "observacao",
            "atualizado_em",
        ])

    return conta


TIPOS_EVENTO_EDICAO_NOTA_WHATSAPP = (
    "cabecalho_nota_alterado",
    "quantidade_item_alterada",
    "item_removido_da_nota",
    "produto_adicionado_na_nota",
    "item_adicionado_na_nota",
)

TIPOS_EVENTO_CORTE_WHATSAPP_NOTA = (
    "whatsapp_aberto",
    "whatsapp_confirmado",
)


def _status_whatsapp_consulta_venda(venda, eventos=None):
    eventos_lista = list(eventos if eventos is not None else venda.eventos.all())
    eventos_lista.sort(key=lambda evento: (evento.criado_em, evento.id))
    confirmacoes = [
        evento
        for evento in eventos_lista
        if evento.canal == "whatsapp" and evento.tipo_evento == "whatsapp_confirmado"
    ]

    if not confirmacoes:
        classe = "aberto" if venda.whatsapp_status == Venda.WHATSAPP_ABERTO else ""
        return [{
            "texto": venda.whatsapp_status_texto,
            "classe": classe,
        }]

    selos = [{
        "texto": "Nota enviada por WhatsApp",
        "classe": "enviado-confirmado",
    }]

    primeira_confirmacao = confirmacoes[0]
    reenvios_apos_edicao = 0
    edicao_pendente = False
    for evento in eventos_lista:
        if (evento.criado_em, evento.id) <= (primeira_confirmacao.criado_em, primeira_confirmacao.id):
            continue
        if evento.tipo_evento in TIPOS_EVENTO_EDICAO_NOTA_WHATSAPP:
            edicao_pendente = True
            continue
        if evento.canal == "whatsapp" and evento.tipo_evento == "whatsapp_confirmado" and edicao_pendente:
            reenvios_apos_edicao += 1
            edicao_pendente = False

    if reenvios_apos_edicao == 1:
        selos.append({
            "texto": "Editada e reenviada por WhatsApp",
            "classe": "editada-reenviada",
        })
    elif reenvios_apos_edicao > 1:
        selos.append({
            "texto": f"Editada e reenviada {reenvios_apos_edicao} vezes",
            "classe": "editada-reenviada",
        })

    if edicao_pendente:
        selos.append({
            "texto": (
                "Nova edicao ainda nao reenviada"
                if reenvios_apos_edicao
                else "Editada, mas ainda nao reenviada"
            ),
            "classe": "editada-pendente",
        })

    return selos


def _eventos_edicao_nota_para_whatsapp(venda):
    eventos = list(
        EventoVenda.objects.filter(
            venda=venda,
        )
        .filter(Q(tipo_evento__in=TIPOS_EVENTO_EDICAO_NOTA_WHATSAPP) | Q(canal="whatsapp", tipo_evento="whatsapp_confirmado"))
        .order_by("criado_em", "id")
    )
    if not any(evento.canal == "whatsapp" and evento.tipo_evento == "whatsapp_confirmado" for evento in eventos):
        return EventoVenda.objects.none()

    pendentes_ids = []
    for evento in eventos:
        if evento.canal == "whatsapp" and evento.tipo_evento == "whatsapp_confirmado":
            pendentes_ids = []
            continue
        if evento.tipo_evento in TIPOS_EVENTO_EDICAO_NOTA_WHATSAPP:
            pendentes_ids.append(evento.id)

    if not pendentes_ids:
        return EventoVenda.objects.none()

    return EventoVenda.objects.filter(pk__in=pendentes_ids).order_by("criado_em", "id")


def _produto_evento_por_nome(itens_nota, nome_produto):
    nome_normalizado = (nome_produto or "").strip().casefold()
    if not nome_normalizado:
        return None
    for item in itens_nota:
        nome_item = item.produto.nome if item.produto else "Produto nao identificado"
        if nome_item.strip().casefold() == nome_normalizado:
            return item
    return None


def _linha_resumo_visual_alteracao_item(evento):
    descricao = (evento.descricao or "").strip()

    if evento.tipo_evento == "quantidade_item_alterada":
        return ""

    if evento.tipo_evento == "item_removido_da_nota":
        prefixo = "Item removido da nota: "
        produto = ""
        if descricao.startswith(prefixo):
            produto = descricao[len(prefixo):].split(", quantidade ", 1)[0].strip()
        return f"Item removido: {produto or descricao or 'Item removido'}"

    if evento.tipo_evento in ("produto_adicionado_na_nota", "item_adicionado_na_nota"):
        prefixo = "Item adicionado na nota: "
        produto = descricao[len(prefixo):].split(", quantidade ", 1)[0].strip() if descricao.startswith(prefixo) else ""
        return f"Produto acrescentado: {produto or descricao or 'Produto acrescentado'}"

    return ""


def _resumo_alteracoes_pendentes_whatsapp(venda, itens_nota, incluir_edicoes_registradas=False):
    eventos = list(_eventos_edicao_nota_para_whatsapp(venda))
    if incluir_edicoes_registradas and not eventos:
        eventos = list(
            EventoVenda.objects.filter(
                venda=venda,
                tipo_evento__in=TIPOS_EVENTO_EDICAO_NOTA_WHATSAPP,
            ).order_by("criado_em", "id")
        )
    resumo = {
        "itens_adicionados_ids": set(),
        "itens_quantidade_ids": set(),
        "quantidades": {},
        "removidos": [],
        "eventos": eventos,
        "linhas": [],
    }

    for evento in eventos:
        descricao = (evento.descricao or "").strip()
        linha_visual = _linha_resumo_visual_alteracao_item(evento)
        if linha_visual:
            resumo["linhas"].append({
                "evento_id": evento.id,
                "tipo_evento": evento.tipo_evento,
                "linhas": [linha_visual],
            })

        if evento.tipo_evento in ("produto_adicionado_na_nota", "item_adicionado_na_nota"):
            prefixo = "Item adicionado na nota: "
            produto_nome = descricao[len(prefixo):].split(", quantidade ", 1)[0].strip() if descricao.startswith(prefixo) else ""
            item = _produto_evento_por_nome(itens_nota, produto_nome)
            if item:
                resumo["itens_adicionados_ids"].add(item.id)
            continue

        if evento.tipo_evento == "quantidade_item_alterada":
            prefixo = "Quantidade alterada na nota: "
            produto_nome = ""
            texto_quantidade = ""
            if descricao.startswith(prefixo) and ". De " in descricao:
                produto_nome, resto = descricao[len(prefixo):].split(". De ", 1)
                texto_quantidade = resto.split(". Total", 1)[0].strip()
            item = _produto_evento_por_nome(itens_nota, produto_nome)
            if item:
                resumo["itens_quantidade_ids"].add(item.id)
                if texto_quantidade:
                    resumo["quantidades"][item.id] = texto_quantidade
            continue

        if evento.tipo_evento == "item_removido_da_nota":
            prefixo = "Item removido da nota: "
            produto_nome = descricao[len(prefixo):].split(", quantidade ", 1)[0].strip() if descricao.startswith(prefixo) else ""
            resumo["removidos"].append(produto_nome or descricao or "Item removido")

    return resumo


def _linhas_evento_edicao_nota(evento):
    descricao = (evento.descricao or "").strip()
    if evento.tipo_evento == "quantidade_item_alterada":
        prefixo = "Quantidade alterada na nota: "
        if descricao.startswith(prefixo) and ". De " in descricao and ". Total" in descricao:
            produto, resto = descricao[len(prefixo):].split(". De ", 1)
            quantidades = resto.split(". Total", 1)[0]
            if " para " in quantidades:
                quantidade_anterior, nova_quantidade = quantidades.split(" para ", 1)
                return [
                    f'O item "{produto.strip()}" teve a quantidade alterada:',
                    f"Quantidade anterior: {quantidade_anterior.strip()}",
                    f"Nova quantidade: {nova_quantidade.strip()}",
                ]
        return [descricao or "Quantidade de item alterada na nota."]

    if evento.tipo_evento == "item_removido_da_nota":
        prefixo = "Item removido da nota: "
        if descricao.startswith(prefixo):
            detalhe = descricao[len(prefixo):]
            produto = detalhe.split(", quantidade ", 1)[0].strip()
            if produto:
                return [f'O item "{produto}" foi removido da nota.']
        return [descricao or "Item removido da nota."]

    if evento.tipo_evento in ("produto_adicionado_na_nota", "item_adicionado_na_nota"):
        return [descricao or "Item adicionado na nota."]

    if evento.tipo_evento == "cabecalho_nota_alterado":
        return [descricao or "Cabecalho da nota alterado."]

    return [descricao] if descricao else []


def _montar_whatsapp_atualizacao_nota(request, venda):
    eventos = list(_eventos_edicao_nota_para_whatsapp(venda))
    if not eventos:
        return None

    ultimo_evento = eventos[-1]
    whatsapp_aberto = EventoVenda.objects.filter(
        Q(criado_em__gt=ultimo_evento.criado_em) | Q(criado_em=ultimo_evento.criado_em, id__gt=ultimo_evento.id),
        venda=venda,
        canal="whatsapp",
        tipo_evento="whatsapp_aberto",
    ).exists()
    numero_whatsapp = _numero_whatsapp_cadastro_venda(venda)
    cliente_nome = venda.cliente.nome if venda.cliente else "Cliente"
    nota_path = reverse("estoque:venda_cliente_detalhe", kwargs={"pk": venda.pk})
    nota_url = montar_url_publica(request, nota_path)

    linhas_mensagem = [
        f"Ola, {cliente_nome}.",
        "",
        "Sua nota foi atualizada.",
        "",
    ]
    for indice, evento in enumerate(eventos):
        if indice:
            linhas_mensagem.append("")
        linhas_mensagem.extend(_linhas_evento_edicao_nota(evento))

    linhas_mensagem.extend([
        "",
        f"Novo total da nota: {_formatar_moeda(venda.total or Decimal('0.00'))}",
        "",
        "Segue a nota atualizada para conferencia:",
        nota_url,
    ])
    mensagem_whatsapp = "\n".join(linhas_mensagem)
    quantidade_eventos = len(eventos)
    texto_resumo = (
        "Nota atualizada com 1 alteracao desde o ultimo WhatsApp."
        if quantidade_eventos == 1
        else f"Nota atualizada com {quantidade_eventos} alteracoes desde o ultimo WhatsApp."
    )

    return {
        "tem_whatsapp": bool(numero_whatsapp),
        "url": (
            f"https://web.whatsapp.com/send?phone={numero_whatsapp}&text={quote(mensagem_whatsapp)}"
            if numero_whatsapp
            else ""
        ),
        "texto_resumo": texto_resumo,
        "controle_id": f"{venda.pk}-{'-'.join(str(evento.id) for evento in eventos)}",
        "whatsapp_aberto": whatsapp_aberto,
    }


def venda_editar_revisao(request, pk):
    venda = get_object_or_404(
        Venda.objects.select_related("cliente").prefetch_related("itens__produto", "eventos"),
        pk=pk,
    )
    bloqueio = _bloquear_venda_cancelada(request, venda)
    if bloqueio:
        return bloqueio
    retorno_url = _url_retorno_segura(request)
    alteracao_quantidade = request.session.pop(f"venda_quantidade_alterada_{venda.pk}", None)
    item_removido = request.session.pop(f"venda_item_removido_{venda.pk}", None)

    entregas = (
        EntregaRotaItem.objects.filter(venda=venda)
        .select_related("rota")
        .prefetch_related("checklist_itens")
        .order_by("-rota__data", "-rota_id", "ordem_entrega", "id")
    )
    total_entregas = entregas.count()
    existe_checklist = EntregaChecklistItem.objects.filter(rota_item__venda=venda).exists()
    total_checklists = EntregaChecklistItem.objects.filter(rota_item__venda=venda).count()
    existe_pendencia = EntregaRotaItem.objects.filter(venda=venda, is_pendencia=True).exists()
    whatsapp_atualizacao = _montar_whatsapp_atualizacao_nota(request, venda)

    return render(
        request,
        "estoque/venda_editar_revisao.html",
        {
            "venda": venda,
            "eventos": EventoVenda.objects.filter(venda=venda),
            "entregas": entregas,
            "total_entregas": total_entregas,
            "existe_checklist": existe_checklist,
            "total_checklists": total_checklists,
            "existe_pendencia": existe_pendencia,
            "possui_alerta_operacional": total_entregas or existe_checklist or existe_pendencia,
            "alteracao_quantidade": alteracao_quantidade,
            "whatsapp_atualizacao": whatsapp_atualizacao,
            "retorno_url": retorno_url,
            "retorno_querystring": _querystring_retorno(retorno_url),
        },
    )


def venda_editar_cabecalho(request, pk):
    venda = get_object_or_404(
        Venda.objects.select_related("cliente").prefetch_related("itens__produto"),
        pk=pk,
    )
    bloqueio = _bloquear_venda_cancelada(request, venda)
    if bloqueio:
        return bloqueio
    retorno_url = _url_retorno_segura(request)
    opcoes_pagamento = ("A prazo", "À vista")
    possui_alerta_operacional = (
        EntregaRotaItem.objects.filter(venda=venda).exists()
        or EntregaChecklistItem.objects.filter(rota_item__venda=venda).exists()
        or EntregaRotaItem.objects.filter(venda=venda, is_pendencia=True).exists()
    )
    whatsapp_atualizacao = _montar_whatsapp_atualizacao_nota(request, venda)

    valores = {
        "data_venda": venda.data_venda.isoformat() if venda.data_venda else "",
        "data_vencimento": venda.data_vencimento.isoformat() if venda.data_vencimento else "",
        "tipo_pagamento": venda.tipo_pagamento if venda.tipo_pagamento in opcoes_pagamento else "A prazo",
    }

    if request.method == "POST":
        valores = {
            "data_venda": request.POST.get("data_venda", "").strip(),
            "data_vencimento": request.POST.get("data_vencimento", "").strip(),
            "tipo_pagamento": request.POST.get("tipo_pagamento", "").strip(),
        }
        nova_data_venda = parse_date(valores["data_venda"])
        novo_vencimento = parse_date(valores["data_vencimento"]) if valores["data_vencimento"] else None
        novo_pagamento = valores["tipo_pagamento"]

        if not nova_data_venda:
            messages.warning(request, "Informe uma data da venda valida.")
        elif novo_pagamento not in opcoes_pagamento:
            messages.warning(request, "Selecione uma forma de pagamento valida.")
        else:
            data_anterior = venda.data_venda
            vencimento_anterior = venda.data_vencimento
            pagamento_anterior = venda.tipo_pagamento or "-"
            houve_alteracao = (
                data_anterior != nova_data_venda
                or vencimento_anterior != novo_vencimento
                or pagamento_anterior != novo_pagamento
            )

            if not houve_alteracao:
                messages.warning(request, "Nenhuma alteracao de cabecalho foi informada.")
            else:
                with transaction.atomic():
                    venda.data_venda = nova_data_venda
                    venda.data_vencimento = novo_vencimento
                    venda.tipo_pagamento = novo_pagamento
                    venda.save(update_fields=["data_venda", "data_vencimento", "tipo_pagamento", "atualizado_em"])
                    _registrar_evento_venda(
                        venda,
                        "cabecalho_nota_alterado",
                        (
                            "Cabecalho da nota alterado. "
                            f"Data: {data_anterior.strftime('%d/%m/%Y') if data_anterior else '-'} -> {nova_data_venda.strftime('%d/%m/%Y')}. "
                            f"Vencimento: {vencimento_anterior.strftime('%d/%m/%Y') if vencimento_anterior else '-'} -> {novo_vencimento.strftime('%d/%m/%Y') if novo_vencimento else '-'}. "
                            f"Pagamento: {pagamento_anterior} -> {novo_pagamento}. "
                            "Cliente, itens, financeiro, caixa, entregas e checklist nao foram alterados."
                        ),
                        canal="sistema",
                        usuario=venda.operador,
                    )
                    _sincronizar_conta_receber(
                        venda,
                        "cabecalho da nota alterado",
                        permitir_reabrir_cancelada=True,
                    )

                messages.success(
                    request,
                    "Cabecalho da nota atualizado. O impacto financeiro real sera tratado em etapa futura, se necessario.",
                )
                revisao_url = reverse("estoque:venda_editar_revisao", kwargs={"pk": venda.pk})
                return redirect(_url_com_retorno(revisao_url, retorno_url))

    return render(
        request,
        "estoque/venda_editar_cabecalho.html",
        {
            "venda": venda,
            "valores": valores,
            "opcoes_pagamento": opcoes_pagamento,
            "possui_alerta_operacional": possui_alerta_operacional,
            "whatsapp_atualizacao": whatsapp_atualizacao,
            "retorno_url": retorno_url,
            "retorno_querystring": _querystring_retorno(retorno_url),
        },
    )


def venda_editar_quantidade_item(request, pk, item_id):
    venda = get_object_or_404(
        Venda.objects.select_related("cliente").prefetch_related("itens__produto"),
        pk=pk,
    )
    bloqueio = _bloquear_venda_cancelada(request, venda)
    if bloqueio:
        return bloqueio
    retorno_url = _url_retorno_segura(request)
    item_venda = get_object_or_404(
        ItemVenda.objects.select_related("produto", "venda"),
        pk=item_id,
        venda=venda,
    )
    total_atual_venda = venda.total or Decimal("0.00")
    valor_atual_item = item_venda.valor_total or Decimal("0.00")
    quantidade_preview = None
    novo_valor_item = None
    novo_total_venda = None
    erro_quantidade = ""

    def calcular_previsao(valor_quantidade):
        nova_quantidade = _decimal_do_front(valor_quantidade, "0.001")
        if nova_quantidade <= 0:
            raise ValueError("Informe uma quantidade maior que zero.")

        novo_total_item = (nova_quantidade * item_venda.preco_unitario).quantize(Decimal("0.01"))
        total_previsto = (total_atual_venda - valor_atual_item + novo_total_item).quantize(Decimal("0.01"))
        return nova_quantidade, novo_total_item, total_previsto

    if request.method == "POST":
        quantidade_postada = request.POST.get("nova_quantidade")
        try:
            nova_quantidade, novo_total_item, total_previsto = calcular_previsao(quantidade_postada)
        except ValueError as exc:
            messages.warning(request, str(exc))
            quantidade_url = reverse(
                "estoque:venda_editar_quantidade_item",
                kwargs={"pk": venda.pk, "item_id": item_venda.pk},
            )
            return redirect(_url_com_retorno(quantidade_url, retorno_url))

        quantidade_anterior = item_venda.quantidade
        valor_anterior_item = valor_atual_item
        total_anterior = total_atual_venda
        produto_nome = item_venda.produto.nome if item_venda.produto else "Produto nao identificado"

        with transaction.atomic():
            item_venda.quantidade = nova_quantidade
            item_venda.valor_total = novo_total_item
            item_venda.save(update_fields=["quantidade", "valor_total"])
            venda.total = total_previsto
            venda.save(update_fields=["total", "atualizado_em"])
            novo_total_confirmado = total_previsto
            _registrar_evento_venda(
                venda,
                "quantidade_item_alterada",
                (
                    f"Quantidade alterada na nota: {produto_nome}. "
                    f"De {quantidade_anterior} {item_venda.unidade} para {nova_quantidade} {item_venda.unidade}. "
                    f"Total da venda: R$ {total_anterior} -> R$ {novo_total_confirmado}."
                ),
                canal="sistema",
                usuario=venda.operador,
            )
            _sincronizar_conta_receber(venda, "quantidade de item alterada")

        messages.success(request, f"Quantidade de {produto_nome} atualizada com sucesso.")
        request.session[f"venda_quantidade_alterada_{venda.pk}"] = {
            "item_id": item_venda.pk,
            "produto_nome": produto_nome,
            "unidade": item_venda.unidade,
            "quantidade_anterior": str(quantidade_anterior),
            "nova_quantidade": str(nova_quantidade),
            "valor_anterior_item": str(valor_anterior_item),
            "novo_valor_item": str(novo_total_item),
            "total_anterior_venda": str(total_anterior),
            "novo_total_venda": str(novo_total_confirmado),
        }
        revisao_url = reverse("estoque:venda_editar_revisao", kwargs={"pk": venda.pk})
        return redirect(_url_com_retorno(revisao_url, retorno_url))

    quantidade_informada = request.GET.get("nova_quantidade", "").strip()
    if quantidade_informada:
        try:
            quantidade_preview, novo_valor_item, novo_total_venda = calcular_previsao(quantidade_informada)
        except ValueError as exc:
            erro_quantidade = str(exc)

    possui_alerta_operacional = (
        EntregaRotaItem.objects.filter(venda=venda).exists()
        or EntregaChecklistItem.objects.filter(rota_item__venda=venda).exists()
        or EntregaRotaItem.objects.filter(venda=venda, is_pendencia=True).exists()
    )

    return render(
        request,
        "estoque/venda_editar_quantidade_item.html",
        {
            "venda": venda,
            "item_venda": item_venda,
            "produto_nome": item_venda.produto.nome if item_venda.produto else "Produto nao identificado",
            "total_atual_venda": total_atual_venda,
            "valor_atual_item": valor_atual_item,
            "quantidade_informada": quantidade_informada,
            "quantidade_preview": quantidade_preview,
            "novo_valor_item": novo_valor_item,
            "novo_total_venda": novo_total_venda,
            "erro_quantidade": erro_quantidade,
            "possui_alerta_operacional": possui_alerta_operacional,
            "retorno_url": retorno_url,
            "retorno_querystring": _querystring_retorno(retorno_url),
        },
    )


def venda_adicionar_produto_item(request, pk):
    venda = get_object_or_404(
        Venda.objects.select_related("cliente").prefetch_related("itens__produto"),
        pk=pk,
    )
    bloqueio = _bloquear_venda_cancelada(request, venda)
    if bloqueio:
        return bloqueio
    retorno_url = _url_retorno_segura(request)
    produtos = Produto.objects.filter(excluido=False).order_by("nome")
    total_atual_venda = venda.total or Decimal("0.00")
    produto_selecionado = None
    quantidade_preview = None
    preco_unitario = None
    unidade = ""
    valor_novo_item = None
    novo_total_venda = None
    erro_adicao = ""
    mensagem_produto_duplicado = "Este produto já existe na nota. Use Alterar quantidade no item existente."
    itens_adicionados_ids = _ids_itens_adicionados_param(request)
    itens_adicionados_param = ",".join(str(item_id) for item_id in itens_adicionados_ids)

    def obter_produto(produto_id):
        try:
            return produtos.get(pk=produto_id)
        except (Produto.DoesNotExist, ValueError, TypeError):
            raise ValueError("Selecione um produto cadastrado.")

    def preco_produto(produto):
        preco = produto.preco_venda or produto.preco_vista or Decimal("0.00")
        return Decimal(preco).quantize(Decimal("0.01"))

    def unidade_produto(produto):
        return produto.unidade_venda_1 or produto.unidade_compra or ""

    def validar_produto_novo_na_venda(produto):
        if ItemVenda.objects.filter(venda=venda, produto_id=produto.pk).exists():
            raise ValueError(mensagem_produto_duplicado)

    def calcular_previsao(produto_id, valor_quantidade, valor_preco=None):
        produto = obter_produto(produto_id)
        validar_produto_novo_na_venda(produto)
        quantidade = _decimal_do_front(valor_quantidade, "0.001")
        if quantidade <= 0:
            raise ValueError("Informe uma quantidade maior que zero.")

        preco = _decimal_do_front(valor_preco, "0.01") if str(valor_preco or "").strip() else preco_produto(produto)
        if preco <= 0:
            raise ValueError("Informe um preco unitario maior que zero.")

        total_item = (quantidade * preco).quantize(Decimal("0.01"))
        total_previsto = (total_atual_venda + total_item).quantize(Decimal("0.01"))
        return produto, quantidade, preco, unidade_produto(produto), total_item, total_previsto

    if request.method == "POST":
        produto_id = request.POST.get("produto_id")
        quantidade_informada = request.POST.get("quantidade", "").strip()
        preco_informado = request.POST.get("preco_unitario", "").strip()
        try:
            produto_selecionado, quantidade_preview, preco_unitario, unidade, valor_novo_item, total_previsto = calcular_previsao(
                produto_id,
                quantidade_informada,
                preco_informado,
            )
        except ValueError as exc:
            erro_adicao = str(exc)
            try:
                produto_selecionado = obter_produto(produto_id)
                preco_unitario = _decimal_do_front(preco_informado, "0.01") if preco_informado else preco_produto(produto_selecionado)
                unidade = unidade_produto(produto_selecionado)
            except ValueError:
                pass
        else:
            total_anterior = total_atual_venda
            produto_nome = produto_selecionado.nome

            try:
                with transaction.atomic():
                    validar_produto_novo_na_venda(produto_selecionado)
                    item_criado = ItemVenda.objects.create(
                        venda=venda,
                        produto=produto_selecionado,
                        quantidade=quantidade_preview,
                        unidade=unidade,
                        preco_unitario=preco_unitario,
                        valor_total=valor_novo_item,
                    )
                    total_recalculado = _recalcular_total_venda_pelos_itens(venda)
                    venda.total = total_recalculado
                    venda.save(update_fields=["total", "atualizado_em"])
                    _registrar_evento_venda(
                        venda,
                        "item_adicionado_na_nota",
                        (
                            f"Item adicionado na nota: {produto_nome}, quantidade {quantidade_preview} {unidade or ''}, "
                            f"preco unitario R$ {preco_unitario}, valor acrescentado R$ {valor_novo_item}. "
                            f"Total da venda: R$ {total_anterior} -> R$ {total_recalculado}."
                        ),
                        canal="sistema",
                        usuario=venda.operador,
                    )
                    _sincronizar_conta_receber(venda, "item adicionado na nota")
            except ValueError as exc:
                erro_adicao = str(exc)
                quantidade_preview = None
                valor_novo_item = None
                novo_total_venda = None
            else:
                messages.success(request, f'Produto "{produto_nome}" acrescentado a nota com sucesso.')
                ids_destacados = list(dict.fromkeys([*itens_adicionados_ids, item_criado.pk]))
                detalhe_url = (
                    f"{reverse('estoque:venda_detalhe', kwargs={'pk': venda.pk})}"
                    f"?nota_atualizada=1&itens_adicionados={','.join(str(item_id) for item_id in ids_destacados)}"
                )
                return redirect(_url_com_retorno(detalhe_url, retorno_url))
    else:
        produto_id = request.GET.get("produto_id", "").strip()
        quantidade_informada = request.GET.get("quantidade", "").strip()
        preco_informado = request.GET.get("preco_unitario", "").strip()

    if request.method != "POST" and produto_id:
        try:
            produto_selecionado = obter_produto(produto_id)
            preco_unitario = _decimal_do_front(preco_informado, "0.01") if preco_informado else preco_produto(produto_selecionado)
            unidade = unidade_produto(produto_selecionado)
            if quantidade_informada:
                produto_selecionado, quantidade_preview, preco_unitario, unidade, valor_novo_item, novo_total_venda = calcular_previsao(
                    produto_id,
                    quantidade_informada,
                    preco_informado,
                )
        except ValueError as exc:
            erro_adicao = str(exc)

    possui_alerta_operacional = (
        EntregaRotaItem.objects.filter(venda=venda).exists()
        or EntregaChecklistItem.objects.filter(rota_item__venda=venda).exists()
        or EntregaRotaItem.objects.filter(venda=venda, is_pendencia=True).exists()
    )
    itens_venda_ordenados = sorted(
        list(venda.itens.all()),
        key=lambda item: ((item.produto.nome if item.produto else "Produto nao identificado").casefold(), item.id),
    )
    produtos_existentes_ids = [
        item.produto_id
        for item in itens_venda_ordenados
        if item.produto_id
    ]
    produto_opcoes = [
        {
            "id": produto.id,
            "nome": produto.nome,
            "preco": str(preco_produto(produto)),
            "unidade": unidade_produto(produto),
        }
        for produto in produtos
    ]

    return render(
        request,
        "estoque/venda_adicionar_produto_item.html",
        {
            "venda": venda,
            "produtos": produtos,
            "produto_opcoes": produto_opcoes,
            "produto_id": produto_id,
            "produto_selecionado": produto_selecionado,
            "quantidade_informada": quantidade_informada,
            "preco_informado": preco_informado or (str(preco_unitario) if preco_unitario is not None else ""),
            "quantidade_preview": quantidade_preview,
            "preco_unitario": preco_unitario,
            "unidade": unidade,
            "valor_novo_item": valor_novo_item,
            "total_atual_venda": total_atual_venda,
            "novo_total_venda": novo_total_venda,
            "erro_adicao": erro_adicao,
            "possui_alerta_operacional": possui_alerta_operacional,
            "itens_adicionados_param": itens_adicionados_param,
            "itens_venda_ordenados": itens_venda_ordenados,
            "produtos_existentes_ids": produtos_existentes_ids,
            "retorno_url": retorno_url,
            "retorno_querystring": _querystring_retorno(retorno_url),
        },
    )


def venda_revisar_remocao_item(request, pk, item_id):
    venda = get_object_or_404(
        Venda.objects.select_related("cliente").prefetch_related("itens__produto"),
        pk=pk,
    )
    bloqueio = _bloquear_venda_cancelada(request, venda)
    if bloqueio:
        return bloqueio
    retorno_url = _url_retorno_segura(request)
    item_venda = get_object_or_404(
        ItemVenda.objects.select_related("produto", "venda"),
        pk=item_id,
        venda=venda,
    )

    total_atual_venda = venda.total or Decimal("0.00")
    valor_atual_item = item_venda.valor_total or Decimal("0.00")
    novo_total_venda = max(
        (total_atual_venda - valor_atual_item).quantize(Decimal("0.01")),
        Decimal("0.00"),
    )
    total_itens_venda = ItemVenda.objects.filter(venda=venda).count()
    produto_nome = item_venda.produto.nome if item_venda.produto else "Produto nao identificado"
    possui_alerta_operacional = (
        EntregaRotaItem.objects.filter(venda=venda).exists()
        or EntregaChecklistItem.objects.filter(rota_item__venda=venda).exists()
        or EntregaRotaItem.objects.filter(venda=venda, is_pendencia=True).exists()
    )
    total_checklists_item = EntregaChecklistItem.objects.filter(item_venda=item_venda).count()

    if request.method == "POST":
        quantidade_removida = item_venda.quantidade
        unidade_removida = item_venda.unidade
        valor_abatido = valor_atual_item
        total_anterior = total_atual_venda

        with transaction.atomic():
            item_venda.delete()
            total_recalculado = sum(
                (
                    valor or Decimal("0.00")
                    for valor in ItemVenda.objects.filter(venda=venda).values_list("valor_total", flat=True)
                ),
                Decimal("0.00"),
            )
            total_recalculado = total_recalculado.quantize(Decimal("0.01"))
            venda.total = total_recalculado
            venda.save(update_fields=["total", "atualizado_em"])
            _registrar_evento_venda(
                venda,
                "item_removido_da_nota",
                (
                    f"Item removido da nota: {produto_nome}, quantidade {quantidade_removida} {unidade_removida or ''}, "
                    f"valor abatido R$ {valor_abatido}. Total da venda: R$ {total_anterior} -> R$ {total_recalculado}."
                ),
                canal="sistema",
                usuario=venda.operador,
            )
            _sincronizar_conta_receber(venda, "item removido da nota")

        messages.success(request, f'Item "{produto_nome}" removido da nota com sucesso.')
        request.session[f"venda_item_removido_{venda.pk}"] = {
            "produto_nome": produto_nome,
            "novo_total_venda": str(total_recalculado),
        }
        revisao_url = reverse("estoque:venda_editar_revisao", kwargs={"pk": venda.pk})
        return redirect(_url_com_retorno(revisao_url, retorno_url))

    return render(
        request,
        "estoque/venda_revisar_remocao_item.html",
        {
            "venda": venda,
            "item_venda": item_venda,
            "produto_nome": produto_nome,
            "total_atual_venda": total_atual_venda,
            "valor_atual_item": valor_atual_item,
            "novo_total_venda": novo_total_venda,
            "valor_abatido": valor_atual_item,
            "total_itens_venda": total_itens_venda,
            "nota_ficara_zerada": total_itens_venda == 1,
            "possui_alerta_operacional": possui_alerta_operacional,
            "total_checklists_item": total_checklists_item,
            "retorno_url": retorno_url,
            "retorno_querystring": _querystring_retorno(retorno_url),
        },
    )


def venda_cancelar(request, pk):
    venda = get_object_or_404(
        Venda.objects.select_related("cliente").prefetch_related("itens__produto"),
        pk=pk,
    )
    retorno_url = _url_retorno_segura(request)
    detalhe_url = reverse("estoque:venda_detalhe", kwargs={"pk": venda.pk})
    if venda.cancelada:
        messages.warning(request, "Esta venda ja esta cancelada.")
        return redirect(_url_com_retorno(detalhe_url, retorno_url))

    conta_receber = _conta_receber_da_venda(venda)
    recebimentos_count = conta_receber.recebimentos.count() if conta_receber else 0
    creditos_count = (
        CreditoCliente.objects.filter(origem_conta_receber=conta_receber).count()
        if conta_receber
        else 0
    )
    entregas_count = EntregaRotaItem.objects.filter(venda=venda).count()
    eventos_count = EventoVenda.objects.filter(venda=venda).count()
    venda_antiga = bool(venda.data_venda and venda.data_venda != timezone.localdate())
    sinais_consolidacao = []
    if conta_receber:
        sinais_consolidacao.append(
            f"Conta a receber #{conta_receber.id} vinculada ({conta_receber.get_status_display()})."
        )
    if recebimentos_count:
        sinais_consolidacao.append(f"{recebimentos_count} recebimento(s)/baixa(s) registrado(s).")
    if creditos_count:
        sinais_consolidacao.append(f"{creditos_count} movimento(s) de credito vinculado(s).")
    if venda_antiga:
        sinais_consolidacao.append("Venda de data anterior ao dia de hoje.")
    if entregas_count:
        sinais_consolidacao.append(f"{entregas_count} vinculo(s) de entrega/checklist encontrado(s).")
    if eventos_count:
        sinais_consolidacao.append(f"{eventos_count} registro(s) no historico da venda.")

    itens_nota = sorted(
        list(venda.itens.all()),
        key=lambda item: ((item.produto.nome if item.produto else "Produto nao identificado").casefold(), item.id),
    )
    motivos_cancelamento = (
        "Cliente não estava / comércio fechado",
        "Cliente desistiu da compra",
        "Pedido duplicado",
        "Venda lançada e não realizada",
        "Outro motivo",
    )
    motivo = ""
    motivo_padrao = ""
    observacao_cancelamento = ""
    confirmacao_cancelamento = ""

    if request.method == "POST":
        motivo_padrao = request.POST.get("motivo_padrao", "").strip()
        observacao_cancelamento = request.POST.get("observacao_cancelamento", "").strip()
        confirmacao_cancelamento = request.POST.get("confirmacao_cancelamento", "")
        confirmacao_normalizada = confirmacao_cancelamento.strip().upper()
        if motivo_padrao not in motivos_cancelamento:
            messages.warning(request, "Informe o motivo do cancelamento.")
        elif motivo_padrao == "Outro motivo" and not observacao_cancelamento:
            messages.warning(request, "Informe a observacao adicional para outro motivo.")
        elif confirmacao_normalizada != "CANCELAR":
            messages.warning(request, "Digite CANCELAR exatamente para confirmar o cancelamento da venda.")
        else:
            motivo = motivo_padrao
            if observacao_cancelamento:
                motivo = f"{motivo_padrao} - Observação: {observacao_cancelamento}"
            with transaction.atomic():
                venda.cancelada = True
                venda.cancelada_em = timezone.now()
                venda.motivo_cancelamento = motivo
                venda.save(update_fields=["cancelada", "cancelada_em", "motivo_cancelamento", "atualizado_em"])
                _registrar_evento_venda(
                    venda,
                    "venda_cancelada",
                    (
                        "Venda cancelada / venda nao realizada. "
                        f"Motivo: {motivo}. "
                        "Itens preservados para historico. Conta a receber vinculada cancelada quando existente. Estoque e caixa nao foram alterados nesta fase."
                    ),
                    canal="sistema",
                    usuario=venda.operador,
                )
                _sincronizar_conta_receber(venda, "venda cancelada")

            messages.success(request, "Venda marcada como CANCELADA / VENDA NAO REALIZADA.")
            return redirect(_url_com_retorno(detalhe_url, retorno_url))

    return render(
        request,
        "estoque/venda_cancelar.html",
        {
            "venda": venda,
            "itens_nota": itens_nota,
            "motivo": motivo,
            "motivo_padrao": motivo_padrao,
            "observacao_cancelamento": observacao_cancelamento,
            "motivos_cancelamento": motivos_cancelamento,
            "confirmacao_cancelamento": confirmacao_cancelamento,
            "conta_receber": conta_receber,
            "recebimentos_count": recebimentos_count,
            "creditos_count": creditos_count,
            "entregas_count": entregas_count,
            "eventos_count": eventos_count,
            "venda_antiga": venda_antiga,
            "sinais_consolidacao": sinais_consolidacao,
            "tem_sinais_consolidacao": bool(sinais_consolidacao),
            "retorno_url": retorno_url,
            "retorno_querystring": _querystring_retorno(retorno_url),
        },
    )


def venda_criar_entrega(request, pk):
    venda = get_object_or_404(Venda, pk=pk)
    bloqueio = _bloquear_venda_cancelada(request, venda)
    if bloqueio:
        return bloqueio
    
    # Verificar se já existe entrega para esta venda
    if EntregaRotaItem.objects.filter(venda=venda).exists():
        messages.warning(request, "Esta venda já possui entrega criada.")
        return redirect("estoque:venda_detalhe", pk=venda.pk)
    
    # Criar entrega unitária
    with transaction.atomic():
        rota = EntregaRota.objects.create(
            data=timezone.localdate(),
            tipo=EntregaRota.TIPO_UNITARIA,
            observacao=f"Entrega criada automaticamente da venda #{venda.pk}",
        )
        EntregaRotaItem.objects.create(
            rota=rota,
            venda=venda,
            ordem_entrega=1,
        )
    
    messages.success(request, f"Entrega unitária #{rota.pk} criada para a venda #{venda.pk}.")
    return redirect("estoque:venda_detalhe", pk=venda.pk)


def venda_whatsapp_pdf(request, pk):
    venda = get_object_or_404(
        Venda.objects.select_related("cliente").prefetch_related("itens__produto"),
        pk=pk,
    )
    buffer = _gerar_nota_whatsapp_pdf(venda)
    response = HttpResponse(buffer.getvalue(), content_type="application/pdf")
    response["Content-Disposition"] = f'inline; filename="nota-whatsapp-venda-{venda.id}.pdf"'
    return response


def venda_whatsapp_imagem(request, pk):
    venda = get_object_or_404(
        Venda.objects.select_related("cliente").prefetch_related("itens__produto"),
        pk=pk,
    )
    buffer = _gerar_nota_whatsapp_imagem(venda)
    response = HttpResponse(buffer.getvalue(), content_type="image/png")
    response["Content-Disposition"] = f'inline; filename="nota-whatsapp-venda-{venda.id}.png"'
    return response


@ensure_csrf_cookie
def preparar_whatsapp_venda(request, pk):
    venda = get_object_or_404(
        Venda.objects.select_related("cliente"),
        pk=pk,
    )
    bloqueio = _bloquear_venda_cancelada(request, venda)
    if bloqueio:
        return bloqueio
    whatsapp_url = montar_link_whatsapp_venda(venda)
    return render(
        request,
        "estoque/preparar_whatsapp.html",
        {
            "venda": venda,
            "whatsapp_url": whatsapp_url,
        },
    )


@require_POST
def registrar_impressao(request, pk):
    venda = get_object_or_404(Venda, pk=pk)
    _registrar_evento_venda(
        venda,
        "nota_impressa",
        "Nota impressa.",
        canal="impressao",
    )
    return JsonResponse({"sucesso": True, "mensagem": "Registro de impressão salvo."})


@require_POST
def registrar_whatsapp_aberto(request, pk):
    venda = get_object_or_404(Venda, pk=pk)
    if venda.cancelada:
        return JsonResponse({"sucesso": False, "mensagem": "Venda cancelada. WhatsApp bloqueado."}, status=400)
    numero_usado = ""
    try:
        dados = json.loads(request.body.decode("utf-8") or "{}")
        numero_usado = "".join(ch for ch in str(dados.get("numero_usado") or "") if ch.isdigit())
    except (json.JSONDecodeError, UnicodeDecodeError):
        numero_usado = ""

    numero_whatsapp, origem_numero = _dados_numero_whatsapp_evento(venda, numero_usado)

    if venda.whatsapp_status != Venda.WHATSAPP_ENVIADO_CONFIRMADO:
        venda.whatsapp_status = Venda.WHATSAPP_ABERTO
        venda.whatsapp_aberto_em = timezone.now()
        if numero_whatsapp:
            venda.whatsapp_numero_usado = numero_whatsapp
        venda.save(update_fields=[
            "whatsapp_status",
            "whatsapp_aberto_em",
            "whatsapp_numero_usado",
            "atualizado_em",
        ])

    _registrar_evento_venda(
        venda,
        "whatsapp_aberto",
        "WhatsApp aberto para envio.",
        canal="whatsapp",
        numero_whatsapp=numero_whatsapp,
        origem_numero=origem_numero,
    )
    return JsonResponse({
        "sucesso": True,
        "mensagem": "Registro de abertura no WhatsApp salvo.",
        "whatsapp_status": venda.whatsapp_status,
        "whatsapp_status_texto": venda.whatsapp_status_texto,
    })


@require_POST
def registrar_checklist_whatsapp_aberto(request, pk):
    venda = get_object_or_404(Venda, pk=pk)
    if venda.cancelada:
        return JsonResponse({"sucesso": False, "mensagem": "Venda cancelada. Checklist bloqueado."}, status=400)
    try:
        dados = json.loads(request.body.decode("utf-8") or "{}")
    except (json.JSONDecodeError, UnicodeDecodeError):
        dados = {}

    numero_usado = "".join(ch for ch in str(dados.get("numero_usado") or "") if ch.isdigit())
    rota_id = str(dados.get("rota_id") or "").strip()
    rota_item_id = str(dados.get("rota_item_id") or "").strip()
    numero_whatsapp, origem_numero = _dados_numero_whatsapp_evento(venda, numero_usado)

    detalhes = []
    if rota_id:
        detalhes.append(f"rota/entrega #{rota_id}")
    if rota_item_id:
        detalhes.append(f"bloco #{rota_item_id}")
    detalhes_texto = f" ({', '.join(detalhes)})." if detalhes else "."

    _registrar_evento_venda(
        venda,
        "checklist_cliente_whatsapp_aberto",
        f"WhatsApp aberto para envio do checklist ao cliente{detalhes_texto}",
        canal="whatsapp_checklist",
        numero_whatsapp=numero_whatsapp,
        origem_numero=origem_numero,
    )
    return JsonResponse({
        "sucesso": True,
        "mensagem": "Registro de abertura do checklist no WhatsApp salvo.",
    })


@require_POST
def confirmar_checklist_whatsapp(request, pk):
    venda = get_object_or_404(Venda, pk=pk)
    if venda.cancelada:
        return JsonResponse({"sucesso": False, "mensagem": "Venda cancelada. Checklist bloqueado."}, status=400)
    try:
        dados = json.loads(request.body.decode("utf-8") or "{}")
    except (json.JSONDecodeError, UnicodeDecodeError):
        dados = {}

    numero_usado = "".join(ch for ch in str(dados.get("numero_usado") or "") if ch.isdigit())
    rota_id = str(dados.get("rota_id") or "").strip()
    rota_item_id = str(dados.get("rota_item_id") or "").strip()
    numero_whatsapp, origem_numero = _dados_numero_whatsapp_evento(venda, numero_usado)

    detalhes = []
    if rota_id:
        detalhes.append(f"rota/entrega #{rota_id}")
    if rota_item_id:
        detalhes.append(f"bloco #{rota_item_id}")
    detalhes_texto = f" ({', '.join(detalhes)})." if detalhes else "."
    descricao = f"Checklist enviado ao cliente por WhatsApp{detalhes_texto}"

    ja_existe = EventoVenda.objects.filter(
        venda=venda,
        tipo_evento="checklist_cliente_enviado",
        canal="whatsapp_checklist",
        descricao=descricao,
    ).exists()
    if not ja_existe:
        _registrar_evento_venda(
            venda,
            "checklist_cliente_enviado",
            descricao,
            canal="whatsapp_checklist",
            numero_whatsapp=numero_whatsapp,
            origem_numero=origem_numero,
        )
    return JsonResponse({
        "sucesso": True,
        "mensagem": "Checklist marcado como enviado ao cliente.",
        "already_exists": ja_existe,
    })


@require_POST
def confirmar_whatsapp(request, pk):
    venda = get_object_or_404(Venda, pk=pk)
    if venda.cancelada:
        return JsonResponse({"sucesso": False, "mensagem": "Venda cancelada. WhatsApp bloqueado."}, status=400)
    ultimo_whatsapp_aberto = venda.eventos.filter(
        canal="whatsapp",
        tipo_evento="whatsapp_aberto",
    ).first()
    numero_whatsapp = (
        (ultimo_whatsapp_aberto.numero_whatsapp if ultimo_whatsapp_aberto else "")
        or venda.whatsapp_numero_usado
        or ""
    )
    origem_numero = (
        (ultimo_whatsapp_aberto.origem_numero if ultimo_whatsapp_aberto else "")
        or ""
    )
    if not numero_whatsapp:
        numero_whatsapp, origem_numero = _dados_numero_whatsapp_evento(venda)
    if not origem_numero:
        origem_numero = EventoVenda.ORIGEM_NUMERO_DESCONHECIDO
    venda.whatsapp_status = Venda.WHATSAPP_ENVIADO_CONFIRMADO
    venda.whatsapp_confirmado_em = timezone.now()
    venda.save(update_fields=[
        "whatsapp_status",
        "whatsapp_confirmado_em",
        "atualizado_em",
    ])
    _registrar_evento_venda(
        venda,
        "whatsapp_confirmado",
        "Envio confirmado manualmente.",
        canal="whatsapp",
        numero_whatsapp=numero_whatsapp,
        origem_numero=origem_numero,
    )
    return JsonResponse({
        "sucesso": True,
        "mensagem": "Confirmacao de envio via WhatsApp salva.",
        "whatsapp_status": venda.whatsapp_status,
        "whatsapp_status_texto": venda.whatsapp_status_texto,
        "whatsapp_status_selos": _status_whatsapp_consulta_venda(venda),
        "comunicacao_descricao": "Envio confirmado manualmente.",
        "comunicacao_numero": numero_whatsapp,
        "comunicacao_origem": _rotulo_origem_numero_whatsapp(origem_numero),
    })


def _formatar_moeda(valor):
    numero = Decimal(valor or 0)
    texto = f"{numero:,.2f}"
    return "R$ " + texto.replace(",", "X").replace(".", ",").replace("X", ".")


def _formatar_quantidade(valor):
    numero = Decimal(valor or 0).quantize(Decimal("0.001"))
    return f"{numero:f}".rstrip("0").rstrip(",").rstrip(".")


def _fonte_nota_whatsapp(tamanho, negrito=False):
    nomes = ["segoeuib.ttf", "arialbd.ttf"] if negrito else ["segoeui.ttf", "arial.ttf"]
    for nome in nomes:
        caminho = Path("C:/Windows/Fonts") / nome
        if caminho.exists():
            return ImageFont.truetype(str(caminho), tamanho)
    return ImageFont.load_default()


def _texto_largura(draw, texto, fonte):
    caixa = draw.textbbox((0, 0), str(texto), font=fonte)
    return caixa[2] - caixa[0]


def _quebrar_texto(draw, texto, fonte, largura_maxima):
    texto = str(texto or "-")
    linhas = []
    for bloco in texto.splitlines() or [""]:
        palavras = bloco.split()
        if not palavras:
            linhas.append("")
            continue
        linha = ""
        for palavra in palavras:
            teste = f"{linha} {palavra}".strip()
            if _texto_largura(draw, teste, fonte) <= largura_maxima:
                linha = teste
                continue
            if linha:
                linhas.append(linha)
            if _texto_largura(draw, palavra, fonte) <= largura_maxima:
                linha = palavra
            else:
                partes = textwrap.wrap(palavra, width=18)
                linhas.extend(partes[:-1])
                linha = partes[-1] if partes else palavra
        if linha:
            linhas.append(linha)
    return linhas


def _gerar_paginas_nota_whatsapp(venda):
    largura, altura = 1080, 1600
    margem = 42
    fundo = "#f6f1e8"
    texto = "#1f2933"
    suave = "#64748b"
    verde = "#14532d"
    borda = "#ead7b0"
    card = "#fffdf8"

    fonte_empresa = _fonte_nota_whatsapp(28, True)
    fonte_titulo = _fonte_nota_whatsapp(40, True)
    fonte_subtitulo = _fonte_nota_whatsapp(26, True)
    fonte_label = _fonte_nota_whatsapp(19, True)
    fonte_intro = _fonte_nota_whatsapp(27)
    fonte_texto_negrito = _fonte_nota_whatsapp(25, True)
    fonte_tabela = _fonte_nota_whatsapp(23)
    fonte_tabela_negrito = _fonte_nota_whatsapp(23, True)
    fonte_total = _fonte_nota_whatsapp(38, True)

    paginas = []
    pagina_numero = 0

    def nova_pagina(continua=False):
        nonlocal pagina_numero
        pagina_numero += 1
        imagem = Image.new("RGB", (largura, altura), fundo)
        draw = ImageDraw.Draw(imagem)
        draw.rounded_rectangle(
            (margem, margem, largura - margem, altura - margem),
            radius=28,
            fill=card,
            outline=borda,
            width=3,
        )
        y_atual = margem + 28
        draw.text((margem + 30, y_atual), "LA Neiva", fill=verde, font=fonte_empresa)
        y_atual += 36
        titulo = "Nota de Venda"
        if continua:
            titulo += " (contin.)"
        draw.text((margem + 30, y_atual), titulo, fill=texto, font=fonte_titulo)
        numero_venda = f"Venda #{venda.id}"
        numero_largura = _texto_largura(draw, numero_venda, fonte_subtitulo)
        draw.text((largura - margem - 30 - numero_largura, margem + 40), numero_venda, fill=texto, font=fonte_subtitulo)
        draw.text((margem + 30, altura - margem - 42), f"Pagina {pagina_numero}", fill=suave, font=fonte_label)
        y_atual += 58
        return imagem, draw, y_atual

    imagem, draw, y = nova_pagina()

    def adicionar_pagina_se_precisar(altura_necessaria):
        nonlocal imagem, draw, y
        if y + altura_necessaria <= altura - margem - 74:
            return
        paginas.append(imagem)
        imagem, draw, y = nova_pagina(continua=True)

    def desenhar_campo(x, y_campo, largura_campo, label, valor):
        draw.rounded_rectangle(
            (x, y_campo, x + largura_campo, y_campo + 78),
            radius=15,
            fill="#f8fafc",
            outline="#e2e8f0",
            width=2,
        )
        draw.text((x + 16, y_campo + 10), label.upper(), fill=suave, font=fonte_label)
        linhas = _quebrar_texto(draw, valor, fonte_texto_negrito, largura_campo - 40)
        for indice, linha in enumerate(linhas[:1]):
            draw.text((x + 16, y_campo + 38 + indice * 28), linha, fill=texto, font=fonte_texto_negrito)

    cliente = venda.cliente.nome if venda.cliente else "Consumidor"
    data_venda = venda.data_venda.strftime("%d/%m/%Y")
    vencimento = venda.data_vencimento.strftime("%d/%m/%Y") if venda.data_vencimento else "-"
    pagamento = venda.tipo_pagamento or "-"

    x1 = margem + 30
    draw.rounded_rectangle(
        (x1, y, largura - margem - 30, y + 146),
        radius=18,
        fill="#fff8e8",
        outline="#ead19a",
        width=2,
    )
    draw.text((x1 + 22, y + 18), f"Nota de Venda #{venda.id}", fill=texto, font=fonte_subtitulo)
    draw.text((x1 + 22, y + 56), f"Cliente: {cliente}", fill=texto, font=fonte_texto_negrito)
    draw.text((x1 + 22, y + 90), f"Total: {_formatar_moeda(venda.total)}", fill=verde, font=fonte_texto_negrito)
    draw.text((x1 + 22, y + 118), "Segue a nota referente à venda abaixo.", fill=suave, font=fonte_intro)
    y += 164

    largura_campo = (largura - margem * 2 - 60 - 22) // 2
    x2 = x1 + largura_campo + 22
    desenhar_campo(x1, y, largura_campo, "Cliente", cliente)
    desenhar_campo(x2, y, largura_campo, "Data", data_venda)
    y += 92
    desenhar_campo(x1, y, largura_campo, "Pagamento", pagamento)
    desenhar_campo(x2, y, largura_campo, "Vencimento", vencimento)
    y += 106

    draw.text((x1, y), "Itens da venda", fill=texto, font=fonte_subtitulo)
    y += 42

    def desenhar_item(indice, item):
        nonlocal y
        nome = item.produto.nome if item.produto else "Produto nao identificado"
        quantidade = _formatar_quantidade(item.quantidade)
        unidade = item.unidade or "-"
        preco = _formatar_moeda(item.preco_unitario)
        subtotal = _formatar_moeda(item.valor_total)
        resumo = f"{quantidade} {unidade} × {preco}"
        subtotal_largura = _texto_largura(draw, subtotal, fonte_tabela_negrito)
        x_subtotal = largura - margem - 52 - subtotal_largura
        largura_nome = 850
        linhas_nome = _quebrar_texto(draw, f"{indice}. {nome}", fonte_tabela_negrito, largura_nome)
        linhas_nome = linhas_nome[:2]
        altura_item = 74 if len(linhas_nome) == 1 else 98
        adicionar_pagina_se_precisar(altura_item)
        topo = y
        draw.rounded_rectangle(
            (x1, topo, largura - margem - 30, topo + altura_item - 8),
            radius=14,
            fill="#ffffff",
            outline="#edf1f6",
            width=2,
        )
        texto_y = topo + 13
        for linha in linhas_nome:
            draw.text((x1 + 24, texto_y), linha, fill=texto, font=fonte_tabela_negrito)
            texto_y += 28

        resumo_y = topo + 41 + (len(linhas_nome) - 1) * 24
        draw.text((x1 + 24, resumo_y), resumo, fill="#42526a", font=fonte_tabela)
        draw.text((x_subtotal, resumo_y), subtotal, fill=texto, font=fonte_tabela_negrito)
        y = topo + altura_item + 4

    for indice, item in enumerate(venda.itens.all(), start=1):
        desenhar_item(indice, item)

    adicionar_pagina_se_precisar(116)
    draw.rounded_rectangle(
        (x1, y + 8, largura - margem - 30, y + 92),
        radius=20,
        fill="#e8f5e9",
        outline="#b7e4c7",
        width=2,
    )
    draw.text((x1 + 22, y + 35), "Total da venda", fill=verde, font=fonte_subtitulo)
    total = _formatar_moeda(venda.total)
    total_largura = _texto_largura(draw, total, fonte_total)
    draw.text((largura - margem - 54 - total_largura, y + 27), total, fill=verde, font=fonte_total)

    paginas.append(imagem)
    return paginas


def _gerar_nota_whatsapp_pdf(venda):
    paginas = _gerar_paginas_nota_whatsapp(venda)
    pdf = BytesIO()
    paginas[0].save(pdf, format="PDF", save_all=True, append_images=paginas[1:], resolution=150.0)
    pdf.seek(0)
    return pdf


def _gerar_nota_whatsapp_imagem(venda):
    paginas = _gerar_paginas_nota_whatsapp(venda)
    if len(paginas) == 1:
        imagem_final = paginas[0]
    else:
        largura = max(pagina.width for pagina in paginas)
        espaco = 24
        altura = sum(pagina.height for pagina in paginas) + espaco * (len(paginas) - 1)
        imagem_final = Image.new("RGB", (largura, altura), "#f6f1e8")
        y = 0
        for pagina in paginas:
            imagem_final.paste(pagina, ((largura - pagina.width) // 2, y))
            y += pagina.height + espaco

    png = BytesIO()
    imagem_final.save(png, format="PNG")
    png.seek(0)
    return png


def _gerar_cobranca_cliente_imagem(cliente, financeiro, cobranca):
    largura = 1080
    margem = 42
    contas = cobranca.get("contas", [])
    altura = max(980, 540 + max(len(contas), 1) * 146)

    fundo = "#f6f1e8"
    card = "#fffdf8"
    texto = "#172033"
    suave = "#64748b"
    azul = "#1e3a8a"
    vermelho = "#991b1b"
    borda = "#dbe5f0"

    fonte_empresa = _fonte_nota_whatsapp(30, True)
    fonte_titulo = _fonte_nota_whatsapp(44, True)
    fonte_subtitulo = _fonte_nota_whatsapp(28, True)
    fonte_label = _fonte_nota_whatsapp(20, True)
    fonte_texto = _fonte_nota_whatsapp(25)
    fonte_texto_negrito = _fonte_nota_whatsapp(25, True)
    fonte_rodape = _fonte_nota_whatsapp(26, True)

    imagem = Image.new("RGB", (largura, altura), fundo)
    draw = ImageDraw.Draw(imagem)

    draw.rounded_rectangle(
        (margem, margem, largura - margem, altura - margem),
        radius=28,
        fill=card,
        outline="#ead7b0",
        width=3,
    )

    x = margem + 30
    direita = largura - margem - 30
    y = margem + 28

    draw.text((x, y), "L A Neiva", fill=azul, font=fonte_empresa)
    y += 42
    draw.text((x, y), "Cobrança do cliente", fill=texto, font=fonte_titulo)
    y += 66

    draw.rounded_rectangle((x, y, direita, y + 116), radius=18, fill="#eff6ff", outline="#bfdbfe", width=2)
    draw.text((x + 22, y + 18), f"Cliente: {cliente.nome}", fill=texto, font=fonte_texto_negrito)
    draw.text((x + 22, y + 58), f"WhatsApp: {cliente.whatsapp or '-'}", fill=suave, font=fonte_texto)
    y += 138

    abertas_total = _formatar_moeda(Decimal(financeiro["contas_abertas_total"]))
    vencidas_total = _formatar_moeda(Decimal(financeiro["contas_vencidas_total"]))

    def desenhar_resumo(x_campo, y_campo, largura_campo, label, valor, alerta=False):
        draw.rounded_rectangle(
            (x_campo, y_campo, x_campo + largura_campo, y_campo + 82),
            radius=15,
            fill="#fef2f2" if alerta else "#f8fafc",
            outline="#fecaca" if alerta else "#e2e8f0",
            width=2,
        )
        draw.text((x_campo + 15, y_campo + 11), label.upper(), fill=vermelho if alerta else suave, font=fonte_label)
        draw.text((x_campo + 15, y_campo + 42), valor, fill=vermelho if alerta else texto, font=fonte_texto_negrito)

    def desenhar_resumo_vencidas(x_campo, y_campo, largura_campo, quantidade, valor):
        draw.rounded_rectangle(
            (x_campo, y_campo, x_campo + largura_campo, y_campo + 106),
            radius=15,
            fill="#fef2f2",
            outline="#fecaca",
            width=2,
        )
        quantidade_texto = f"{quantidade} conta" if quantidade == 1 else f"{quantidade} contas"
        draw.text((x_campo + 15, y_campo + 10), "CONTAS VENCIDAS", fill=vermelho, font=fonte_label)
        draw.text((x_campo + 15, y_campo + 39), quantidade_texto, fill=vermelho, font=fonte_texto_negrito)
        draw.text((x_campo + 15, y_campo + 65), valor, fill=vermelho, font=fonte_subtitulo)

    largura_campo = (direita - x - 20) // 2
    x2 = x + largura_campo + 20
    desenhar_resumo(
        x,
        y,
        largura_campo,
        "Contas em aberto",
        f"{financeiro['contas_abertas_qtd']} / {abertas_total}",
    )
    desenhar_resumo_vencidas(x2, y, largura_campo, financeiro["contas_vencidas_qtd"], vencidas_total)
    y += 124
    if cobranca.get("maior_atraso_dias"):
        desenhar_resumo(x, y, largura_campo, "Maior atraso", f"{cobranca['maior_atraso_dias']} dias", True)
        y += 100

    draw.text((x, y), "Contas em aberto", fill=texto, font=fonte_subtitulo)
    y += 44

    if not contas:
        draw.rounded_rectangle((x, y, direita, y + 82), radius=14, fill="#ffffff", outline=borda, width=2)
        draw.text((x + 22, y + 25), "Nenhuma conta em aberto.", fill=suave, font=fonte_texto)
        y += 100
    else:
        for conta in contas:
            vencida = bool(conta.get("vencida"))
            fill = "#fef2f2" if vencida else "#eff6ff"
            outline = "#fecaca" if vencida else "#bfdbfe"
            status_cor = vermelho if vencida else azul
            titulo = conta.get("titulo") or "Conta"
            status = conta.get("status") or "Em dia"
            meta = (
                f"Data: {conta.get('data') or 'Data não informada'} | "
                f"Vencimento: {conta.get('vencimento') or 'Vencimento não informado'} | "
                f"Valor em aberto: {conta.get('valor') or 'R$ 0,00'}"
            )
            linhas_meta = _quebrar_texto(draw, meta, fonte_texto, direita - x - 44)[:2]
            altura_card = 82 + len(linhas_meta) * 30
            draw.rounded_rectangle((x, y, direita, y + altura_card), radius=14, fill=fill, outline=outline, width=2)
            draw.text((x + 20, y + 13), titulo, fill=texto, font=fonte_texto_negrito)
            status_largura = _texto_largura(draw, status, fonte_label)
            draw.rounded_rectangle((direita - status_largura - 46, y + 12, direita - 20, y + 45), radius=16, fill="#fee2e2" if vencida else "#dbeafe")
            draw.text((direita - status_largura - 33, y + 18), status, fill=status_cor, font=fonte_label)
            y_linha = y + 55
            for linha in linhas_meta:
                draw.text((x + 20, y_linha), linha, fill=suave, font=fonte_texto)
                y_linha += 30
            y += altura_card + 14

    draw.text((direita - _texto_largura(draw, "LA Neiva", fonte_rodape), altura - margem - 62), "LA Neiva", fill=azul, font=fonte_rodape)

    png = BytesIO()
    imagem.save(png, format="PNG")
    png.seek(0)
    return png


def _decimal_comprovante(valor):
    try:
        return Decimal(str(valor or "0")).quantize(Decimal("0.01"))
    except (InvalidOperation, ValueError):
        return Decimal("0.00")


def _gerar_comprovante_recebimento_imagem(dados):
    largura = 1080
    margem = 42
    fundo = "#f6f1e8"
    texto = "#1f2933"
    suave = "#64748b"
    verde = "#14532d"
    borda = "#ead7b0"
    card = "#fffdf8"

    fonte_empresa = _fonte_nota_whatsapp(30, True)
    fonte_titulo = _fonte_nota_whatsapp(42, True)
    fonte_subtitulo = _fonte_nota_whatsapp(27, True)
    fonte_label = _fonte_nota_whatsapp(20, True)
    fonte_texto = _fonte_nota_whatsapp(25)
    fonte_texto_negrito = _fonte_nota_whatsapp(26, True)
    fonte_total = _fonte_nota_whatsapp(36, True)
    fonte_rodape = _fonte_nota_whatsapp(24, True)

    contas = dados.get("contas", [])
    contas_abertas = dados.get("contas_abertas", [])
    altura_contas = max(len(contas), 1) * 142
    altura_contas_abertas = max(len(contas_abertas), 1) * 104
    altura = max(1500, 1030 + altura_contas + altura_contas_abertas)
    imagem = Image.new("RGB", (largura, altura), fundo)
    draw = ImageDraw.Draw(imagem)

    draw.rounded_rectangle(
        (margem, margem, largura - margem, altura - margem),
        radius=28,
        fill=card,
        outline=borda,
        width=3,
    )

    x = margem + 30
    direita = largura - margem - 30
    y = margem + 28

    draw.text((x, y), "L A Neiva", fill=verde, font=fonte_empresa)
    y += 40
    draw.text((x, y), "Comprovante de Pagamento", fill=texto, font=fonte_titulo)
    y += 62

    cliente = dados.get("cliente_nome") or "Cliente"
    draw.rounded_rectangle((x, y, direita, y + 142), radius=18, fill="#fff8e8", outline="#ead19a", width=2)
    draw.text((x + 22, y + 18), f"Cliente: {cliente}", fill=texto, font=fonte_texto_negrito)
    draw.text((x + 22, y + 56), f"Data: {dados.get('data_recebimento') or '-'}", fill=texto, font=fonte_texto)
    draw.text((x + 22, y + 92), f"Forma de pagamento: {dados.get('forma_pagamento') or '-'}", fill=suave, font=fonte_texto)
    y += 164

    def desenhar_campo(x_campo, y_campo, largura_campo, label, valor, destaque=False):
        draw.rounded_rectangle(
            (x_campo, y_campo, x_campo + largura_campo, y_campo + 86),
            radius=15,
            fill="#f8fafc" if not destaque else "#e8f5e9",
            outline="#e2e8f0" if not destaque else "#b7e4c7",
            width=2,
        )
        draw.text((x_campo + 16, y_campo + 12), label.upper(), fill=suave, font=fonte_label)
        draw.text((x_campo + 16, y_campo + 43), valor, fill=verde if destaque else texto, font=fonte_texto_negrito)

    largura_campo = (direita - x - 22) // 2
    x2 = x + largura_campo + 22
    saldo_anterior = _decimal_comprovante(dados.get("saldo_anterior"))
    valor_pago = _decimal_comprovante(dados.get("valor_pago"))
    saldo_atual = _decimal_comprovante(dados.get("saldo_atual"))
    credito_gerado = _decimal_comprovante(dados.get("credito_gerado"))

    desenhar_campo(x, y, largura_campo, "Saldo anterior", _formatar_moeda(saldo_anterior))
    desenhar_campo(x2, y, largura_campo, "Valor pago", _formatar_moeda(valor_pago), destaque=True)
    y += 104
    desenhar_campo(x, y, largura_campo, "Saldo apos pagamento", _formatar_moeda(saldo_atual), destaque=saldo_atual <= Decimal("0.00"))
    if credito_gerado > Decimal("0.00"):
        desenhar_campo(x2, y, largura_campo, "Credito gerado", _formatar_moeda(credito_gerado), destaque=True)
    else:
        desenhar_campo(x2, y, largura_campo, "Credito gerado", "-", destaque=False)
    y += 124

    draw.text((x, y), "Aplicacao do pagamento", fill=texto, font=fonte_subtitulo)
    y += 42

    if not contas:
        draw.rounded_rectangle((x, y, direita, y + 74), radius=14, fill="#ffffff", outline="#edf1f6", width=2)
        draw.text((x + 24, y + 23), "Nenhuma conta identificada com seguranca.", fill=suave, font=fonte_texto)
        y += 92
    else:
        for conta in contas:
            venda = conta.get("venda_id") or conta.get("conta_id") or "-"
            saldo_antes = _formatar_moeda(_decimal_comprovante(conta.get("saldo_antes")))
            valor = _formatar_moeda(_decimal_comprovante(conta.get("valor_aplicado")))
            saldo_restante_conta = _decimal_comprovante(conta.get("saldo_restante"))
            status = "quitada" if conta.get("quitada") else "parcial"
            fill_status = "#166534" if conta.get("quitada") else "#92400e"
            data_nota = conta.get("data_nota") or "-"
            rotulo_saldo_antes = "Valor da nota" if conta.get("nota_inteira_antes") else "Saldo da nota antes do pagamento"

            draw.rounded_rectangle((x, y, direita, y + 130), radius=14, fill="#ffffff", outline="#edf1f6", width=2)
            titulo_conta = f"Venda/Nota #{venda} - {data_nota}"
            linhas_nota = _quebrar_texto(draw, titulo_conta, fonte_texto_negrito, 610)
            draw.text((x + 24, y + 14), linhas_nota[0], fill=texto, font=fonte_texto_negrito)
            draw.text((x + 24, y + 50), f"{rotulo_saldo_antes}: {saldo_antes}", fill=texto, font=fonte_texto)
            draw.text((x + 24, y + 80), f"Valor abatido agora: {valor}", fill=texto, font=fonte_texto)
            if not conta.get("quitada"):
                draw.text((x + 24, y + 110), f"Restante desta nota: {_formatar_moeda(saldo_restante_conta)}", fill=suave, font=fonte_label)
            else:
                draw.text((x + 24, y + 110), "Status: quitada", fill=suave, font=fonte_label)
            draw.rounded_rectangle((direita - 170, y + 15, direita - 24, y + 49), radius=16, fill="#ecfdf3" if conta.get("quitada") else "#fef3c7")
            status_largura = _texto_largura(draw, status, fonte_label)
            draw.text((direita - 97 - status_largura // 2, y + 21), status, fill=fill_status, font=fonte_label)
            y += 142

    y += 12
    draw.text((x, y), "Contas em aberto apos pagamento", fill=texto, font=fonte_subtitulo)
    y += 42

    if not contas_abertas:
        draw.rounded_rectangle((x, y, direita, y + 74), radius=14, fill="#ffffff", outline="#edf1f6", width=2)
        draw.text((x + 24, y + 23), "Nenhuma conta em aberto apos este pagamento.", fill=verde, font=fonte_texto)
        y += 92
    else:
        for conta in contas_abertas:
            venda = conta.get("venda_id") or conta.get("conta_id") or "-"
            data_nota = conta.get("data_nota") or "-"
            saldo_conta = _formatar_moeda(_decimal_comprovante(conta.get("saldo_atual")))
            dias_aberto = int(conta.get("dias_aberto") or 0)
            em_atraso = bool(conta.get("em_atraso"))
            cor_dias = "#b91c1c" if em_atraso else "#1e3a8a"
            fundo_dias = "#fee2e2" if em_atraso else "#eff6ff"
            texto_dias = f"{dias_aberto} dia{'s' if dias_aberto != 1 else ''} em aberto"

            draw.rounded_rectangle((x, y, direita, y + 92), radius=14, fill="#ffffff", outline="#edf1f6", width=2)
            titulo_conta = f"Venda/Nota #{venda} - {data_nota}"
            linhas_nota = _quebrar_texto(draw, titulo_conta, fonte_texto_negrito, 590)
            draw.text((x + 24, y + 13), linhas_nota[0], fill=texto, font=fonte_texto_negrito)
            draw.text((x + 24, y + 49), f"Saldo atual: {saldo_conta}", fill=texto, font=fonte_texto)
            draw.rounded_rectangle((direita - 260, y + 28, direita - 24, y + 62), radius=17, fill=fundo_dias)
            dias_largura = _texto_largura(draw, texto_dias, fonte_label)
            draw.text((direita - 142 - dias_largura // 2, y + 34), texto_dias, fill=cor_dias, font=fonte_label)
            y += 104

    y += 12
    draw.rounded_rectangle((x, y, direita, y + 132), radius=20, fill="#e8f5e9", outline="#b7e4c7", width=2)
    draw.text((x + 22, y + 26), "Total pago", fill=verde, font=fonte_subtitulo)
    total_pago = _formatar_moeda(valor_pago)
    total_pago_largura = _texto_largura(draw, total_pago, fonte_total)
    draw.text((direita - 22 - total_pago_largura, y + 18), total_pago, fill=verde, font=fonte_total)
    draw.text((x + 22, y + 82), "Saldo total da divida apos pagamento", fill=verde, font=fonte_label)
    total = _formatar_moeda(saldo_atual)
    total_largura = _texto_largura(draw, total, fonte_texto_negrito)
    draw.text((direita - 22 - total_largura, y + 78), total, fill=verde, font=fonte_texto_negrito)
    y += 166

    draw.text((x, y), "Obrigado.", fill=texto, font=fonte_texto)
    y += 34
    draw.text((x, y), "L A Neiva", fill=verde, font=fonte_rodape)

    png = BytesIO()
    imagem.save(png, format="PNG")
    png.seek(0)
    return png


def _normalizar_numero_whatsapp_evento(numero):
    digitos = "".join(ch for ch in str(numero or "") if ch.isdigit())
    if not digitos:
        return ""
    if not digitos.startswith("55") and len(digitos) in (10, 11):
        digitos = "55" + digitos
    return digitos


def _numero_whatsapp_cadastro_venda(venda):
    cliente = venda.cliente if venda else None
    if not cliente:
        return ""
    numero = Cliente.normalizar_whatsapp(cliente.whatsapp_normalizado or cliente.whatsapp)
    return _normalizar_numero_whatsapp_evento(numero)


def _dados_numero_whatsapp_evento(venda, numero_usado=""):
    numero_manual = _normalizar_numero_whatsapp_evento(numero_usado)
    if numero_manual:
        return numero_manual, EventoVenda.ORIGEM_NUMERO_AVULSO

    numero_cadastro = _numero_whatsapp_cadastro_venda(venda)
    if numero_cadastro:
        return numero_cadastro, EventoVenda.ORIGEM_NUMERO_CADASTRO

    return "", EventoVenda.ORIGEM_NUMERO_DESCONHECIDO


def _rotulo_origem_numero_whatsapp(origem):
    return dict(EventoVenda.ORIGEM_NUMERO_CHOICES).get(origem or "", "")


def _registrar_evento_venda(
    venda,
    tipo_evento,
    descricao,
    canal="sistema",
    usuario=None,
    numero_whatsapp=None,
    origem_numero=None,
):
    if not venda:
        return None
    usuario_texto = usuario if usuario is not None else (venda.operador or "")
    return EventoVenda.objects.create(
        venda=venda,
        tipo_evento=tipo_evento,
        descricao=descricao,
        canal=canal,
        usuario=usuario_texto,
        numero_whatsapp=numero_whatsapp or None,
        origem_numero=origem_numero or None,
    )


def montar_link_whatsapp_venda(venda):
    cliente = venda.cliente
    if not cliente:
        return ""

    numero = Cliente.normalizar_whatsapp(cliente.whatsapp_normalizado or cliente.whatsapp)
    if not numero:
        return ""

    if len(numero) in (10, 11):
        numero = "55" + numero

    linhas = ["Segue nota em imagem."]

    mensagem = "\n".join(linhas)
    return f"https://web.whatsapp.com/send?phone={numero}&text={quote(mensagem)}"
