import json
import textwrap
from io import BytesIO
from decimal import Decimal, InvalidOperation
from pathlib import Path

from django.conf import settings
from django.db import transaction
from django.db.models import Q
from urllib.parse import quote, urlencode
from django.shortcuts import render, redirect, get_object_or_404
from django.urls import reverse
from django.db.models import Case, When, Value, IntegerField, F
from .forms import CategoriaForm, ClienteForm, FuncionarioForm, ProdutoForm, UnidadeForm
from .models import Categoria, Cliente, EntregaChecklistItem, EntregaRota, EntregaRotaItem, EventoVenda, Funcionario, ItemVenda, Produto, Unidade, Venda
from django.contrib import messages
from django.http import Http404, HttpResponse, JsonResponse
from django.utils.dateparse import parse_date
from django.utils import timezone
from django.views.decorators.csrf import ensure_csrf_cookie
from django.views.decorators.http import require_POST
from PIL import Image, ImageDraw, ImageFont
from uuid import uuid4

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
                cliente_salvo_id = tokens_usados[form_token]
                messages.warning(
                    request,
                    "Este envio ja foi processado. Abrimos o cadastro salvo para evitar duplicidade.",
                )
                return redirect(f"{clientes_url}?cliente={cliente_salvo_id}")

            cliente = form.save(commit=False)
            whatsapp_normalizado = Cliente.normalizar_whatsapp(cliente.whatsapp)

            avisos = []
            if not cliente_id:
                cliente_duplicado = None
                nome_cliente = " ".join((cliente.nome or "").strip().split())

                if nome_cliente and cliente.cpf_cnpj:
                    cliente_duplicado = Cliente.objects.filter(
                        nome__iexact=nome_cliente,
                        cpf_cnpj__iexact=cliente.cpf_cnpj,
                    ).first()

                if nome_cliente and whatsapp_normalizado and not cliente_duplicado:
                    cliente_duplicado = Cliente.objects.filter(
                        nome__iexact=nome_cliente,
                        whatsapp_normalizado=whatsapp_normalizado,
                    ).first()

                if cliente_duplicado:
                    messages.warning(
                        request,
                        f'Cliente "{cliente_duplicado.nome}" ja estava cadastrado. Abrimos o cadastro existente para evitar duplicidade.',
                    )
                    return redirect(f"{clientes_url}?cliente={cliente_duplicado.id}")

            if cliente.cpf_cnpj:
                cpf_duplicado = Cliente.objects.exclude(
                    pk=getattr(cliente, "pk", None)
                ).filter(cpf_cnpj__iexact=cliente.cpf_cnpj).first()
                if cpf_duplicado:
                    avisos.append(f'CPF/CNPJ ja usado em "{cpf_duplicado.nome}".')

            if whatsapp_normalizado:
                whatsapp_duplicado = Cliente.objects.exclude(
                    pk=getattr(cliente, "pk", None)
                ).filter(whatsapp_normalizado=whatsapp_normalizado).first()
                if whatsapp_duplicado:
                    avisos.append(f'WhatsApp ja usado em "{whatsapp_duplicado.nome}".')

            cliente.save()
            if not cliente_id and form_token:
                tokens_usados[form_token] = cliente.id
                tokens_itens = list(tokens_usados.items())[-20:]
                request.session["cliente_form_tokens_usados"] = dict(tokens_itens)
                request.session.modified = True

            messages.success(request, f'Cliente "{cliente.nome}" salvo com sucesso.')
            for aviso in avisos:
                messages.warning(request, aviso)
            return redirect(f"{clientes_url}?cliente={cliente.id}")
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
                "prazo_padrao_dias": 0,
                "limite_credito": 0,
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

        if acao == "alternar_status" and cliente_id:
            cliente = get_object_or_404(Cliente, pk=cliente_id)
            cliente.ativo = request.POST.get("ativo") == "1"
            cliente.save(update_fields=["ativo", "atualizado_em"])
            status = "ativado" if cliente.ativo else "desativado"
            messages.success(request, f'Cliente "{cliente.nome}" {status} com sucesso.')
            params = {}
            if termo:
                params["q"] = termo
            destino = clientes_url
            if params:
                destino = f"{destino}?{urlencode(params)}"
            return redirect(destino)

    clientes_qs = Cliente.objects.all().order_by("-ativo", "nome")

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

    return render(
        request,
        "estoque/clientes_consulta.html",
        {
            "clientes": clientes_lista,
            "termo": termo,
            "total_clientes": len(clientes_lista),
        },
    )


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


def clientes_autocomplete(request):
    termo = request.GET.get("q", "").strip()
    clientes_qs = Cliente.objects.filter(ativo=True).order_by("nome")

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
        clientes.append({
            "id": cliente.id,
            "nome": cliente.nome,
            "prazo": cliente.prazo_padrao_dias or 0,
            "limite": str(cliente.limite_credito or 0),
            "status": cliente.status_credito,
            "status_label": cliente.get_status_credito_display(),
            "whatsapp": cliente.whatsapp or "",
        })

    return JsonResponse({"clientes": clientes})

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
    return render(request, 'estoque/vendas_layout_teste.html', {
        'produtos': produtos
    })


@ensure_csrf_cookie
def consultar_vendas(request):
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

    vendas_qs = Venda.objects.select_related("cliente").prefetch_related("itens__produto").order_by("-data_venda", "-id")

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
        venda.whatsapp_url_consulta = montar_link_whatsapp_venda(venda)

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
    whatsapp_url = montar_link_whatsapp_venda(venda)
    entrega_contexto = None
    entrega_id = request.GET.get("entrega")
    if entrega_id:
        entrega_contexto = EntregaRota.objects.filter(pk=entrega_id).first()

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
        },
    )


def venda_editar_revisao(request, pk):
    venda = get_object_or_404(
        Venda.objects.select_related("cliente").prefetch_related("itens__produto", "eventos"),
        pk=pk,
    )
    alteracao_quantidade = request.session.pop(f"venda_quantidade_alterada_{venda.pk}", None)

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
        },
    )


def venda_editar_quantidade_item(request, pk, item_id):
    venda = get_object_or_404(
        Venda.objects.select_related("cliente").prefetch_related("itens__produto"),
        pk=pk,
    )
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
            return redirect("estoque:venda_editar_quantidade_item", pk=venda.pk, item_id=item_venda.pk)

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
        return redirect(f"{reverse('estoque:venda_detalhe', kwargs={'pk': venda.pk})}?nota_atualizada=1")

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
        },
    )


def venda_criar_entrega(request, pk):
    venda = get_object_or_404(Venda, pk=pk)
    
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
        linhas_nome = _quebrar_texto(draw, f"{indice}. {nome}", fonte_tabela_negrito, 850)
        altura_item = 56 + len(linhas_nome[:2]) * 28
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
        for linha in linhas_nome[:2]:
            draw.text((x1 + 24, texto_y), linha, fill=texto, font=fonte_tabela_negrito)
            texto_y += 28

        resumo = f"{quantidade} {unidade} x {preco}"
        resumo_y = topo + altura_item - 38
        draw.text((x1 + 24, resumo_y), resumo, fill=suave, font=fonte_tabela)
        subtotal_largura = _texto_largura(draw, subtotal, fonte_tabela_negrito)
        draw.text((largura - margem - 52 - subtotal_largura, resumo_y), subtotal, fill=texto, font=fonte_tabela_negrito)
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
