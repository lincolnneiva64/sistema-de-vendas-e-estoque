import json
import textwrap
from io import BytesIO
from decimal import Decimal, InvalidOperation
from pathlib import Path

from django.db import transaction
from django.db.models import Q
from urllib.parse import quote, urlencode
from django.shortcuts import render, redirect, get_object_or_404
from django.urls import reverse
from django.db.models import Case, When, Value, IntegerField, F
from .forms import CategoriaForm, ClienteForm, ProdutoForm, UnidadeForm
from .models import Categoria, Cliente, EntregaChecklistItem, EntregaRota, EntregaRotaItem, EventoVenda, ItemVenda, Produto, Unidade, Venda
from django.contrib import messages
from django.http import HttpResponse, JsonResponse
from django.utils.dateparse import parse_date
from django.utils import timezone
from django.views.decorators.csrf import ensure_csrf_cookie
from django.views.decorators.http import require_POST
from PIL import Image, ImageDraw, ImageFont
from uuid import uuid4
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
            venda_ids = [
                venda_id.strip()
                for venda_id in request.POST.getlist("vendas_rota")
                if venda_id.strip().isdigit()
            ]
            vendas = list(Venda.objects.filter(pk__in=venda_ids).select_related("cliente"))
            if not vendas:
                messages.warning(request, "Selecione pelo menos uma venda para criar a rota.", extra_tags="rota-entrega")
                return redirect(f"{reverse('estoque:entregas_dia')}?data={data_entrega.isoformat()}")

            vendas_por_id = {str(venda.id): venda for venda in vendas}
            ordenadas = []
            for venda_id in venda_ids:
                venda = vendas_por_id.get(str(venda_id))
                if not venda:
                    continue
                try:
                    ordem = int(request.POST.get(f"ordem_{venda_id}") or "9999")
                except ValueError:
                    ordem = 9999
                ordenadas.append((ordem, venda.id, venda))

            ordenadas.sort(key=lambda item: (item[0], item[1]))

            with transaction.atomic():
                rota = EntregaRota.objects.create(
                    data=data_entrega,
                    tipo=EntregaRota.TIPO_ROTA,
                    observacao=observacao,
                )
                EntregaRotaItem.objects.bulk_create([
                    EntregaRotaItem(
                        rota=rota,
                        venda=venda,
                        ordem_entrega=indice,
                    )
                    for indice, (_, __, venda) in enumerate(ordenadas, start=1)
                ])
            messages.success(request, f"Rota #{rota.id} criada com {len(ordenadas)} entrega(s).", extra_tags="rota-entrega")
            return redirect(f"{reverse('estoque:entregas_dia')}?data={data_entrega.isoformat()}")

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
        .prefetch_related("itens__venda__cliente")
        .order_by("-id")
    )
    for rota in rotas:
        itens = list(rota.itens.all())
        rota.itens_entrega = itens
        rota.itens_carregamento = list(reversed(itens))

    return render(
        request,
        "estoque/entregas_dia.html",
        {
            "data_entrega": data_entrega,
            "vendas": vendas_lista,
            "rotas": rotas,
            "total_vendas": len(vendas_lista),
        },
    )


def entrega_rota_detalhe(request, pk):
    rota = get_object_or_404(
        EntregaRota.objects.prefetch_related("itens__venda__cliente", "itens__venda__itens__produto"),
        pk=pk,
    )
    itens_entrega = list(rota.itens.all())
    itens_carregamento = list(reversed(itens_entrega))

    return render(
        request,
        "estoque/entrega_rota_detalhe.html",
        {
            "rota": rota,
            "itens_entrega": itens_entrega,
            "itens_carregamento": itens_carregamento,
        },
    )


def entrega_rota_checklist(request, pk):
    rota = get_object_or_404(
        EntregaRota.objects.prefetch_related("itens__venda__cliente", "itens__venda__itens__produto", "itens__checklist_itens"),
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
                if item_venda.id not in existentes
            ]
            if novos:
                EntregaChecklistItem.objects.bulk_create(novos)

    if request.method == "POST":
        bloco_salvo = request.POST.get("salvar_bloco") or request.POST.get("salvar_bloco_alvo", "")
        bloco_partes = bloco_salvo.split(":", 1)
        bloco_rota_item_id = bloco_partes[0] if bloco_partes else ""
        bloco_fase = bloco_partes[1] if len(bloco_partes) > 1 else ""
        rota_item_ids = [item_rota.id for item_rota in itens_entrega]
        bloco_valido = bloco_rota_item_id.isdigit() and int(bloco_rota_item_id) in rota_item_ids
        if bloco_valido:
            rota_item_ids = [int(bloco_rota_item_id)]

        checklist_qs = EntregaChecklistItem.objects.filter(rota_item_id__in=rota_item_ids)

        for checklist in checklist_qs:
            checklist.carregado = f"carregado_{checklist.id}" in request.POST
            checklist.entregue = f"entregue_{checklist.id}" in request.POST
            checklist.save(update_fields=["carregado", "entregue", "atualizado_em"])

        for item_rota in itens_entrega:
            if item_rota.id not in rota_item_ids:
                continue
            item_rota.conferido_cliente = f"conferido_{item_rota.id}" in request.POST
            item_rota.entrega_concluida = f"concluida_{item_rota.id}" in request.POST
            item_rota.save(update_fields=["conferido_cliente", "entrega_concluida"])

        redirect_url = reverse("estoque:entrega_rota_checklist", kwargs={"pk": rota.id})
        if bloco_valido and bloco_fase in {"carregamento", "entrega"}:
            redirect_url = f"{redirect_url}?salvo_item={bloco_rota_item_id}&salvo_fase={bloco_fase}#bloco-{bloco_rota_item_id}-{bloco_fase}"
        else:
            messages.success(request, "Checklist salvo com sucesso.", extra_tags="checklist-global")
        return redirect(redirect_url)

    rota = get_object_or_404(
        EntregaRota.objects.prefetch_related("itens__venda__cliente", "itens__venda__itens__produto", "itens__checklist_itens__item_venda"),
        pk=pk,
    )
    itens_entrega = list(rota.itens.all())
    salvo_item_id = request.GET.get("salvo_item", "")
    salvo_fase = request.GET.get("salvo_fase", "")
    for item_rota in itens_entrega:
        item_rota.salvo_carregamento = salvo_item_id == str(item_rota.id) and salvo_fase == "carregamento"
        item_rota.salvo_entrega = salvo_item_id == str(item_rota.id) and salvo_fase == "entrega"
        item_rota.checklists_por_item = {
            checklist.item_venda_id: checklist
            for checklist in item_rota.checklist_itens.all()
        }
        item_rota.checklists_ordenados = [
            item_rota.checklists_por_item.get(item_venda.id)
            for item_venda in item_rota.venda.itens.all()
            if item_rota.checklists_por_item.get(item_venda.id)
        ]
        item_rota.carregamento_conferido = item_rota.salvo_carregamento or (
            bool(item_rota.checklists_ordenados)
            and all(checklist.carregado for checklist in item_rota.checklists_ordenados)
        )
        item_rota.entrega_conferida = (
            item_rota.salvo_entrega
            or
            item_rota.conferido_cliente
            or item_rota.entrega_concluida
            or (
                bool(item_rota.checklists_ordenados)
                and all(checklist.entregue for checklist in item_rota.checklists_ordenados)
            )
        )

    itens_entrega = sorted(
        itens_entrega,
        key=lambda item_rota: (item_rota.entrega_conferida, item_rota.ordem_entrega, item_rota.id),
    )
    itens_carregamento = sorted(
        itens_entrega,
        key=lambda item_rota: (
            item_rota.carregamento_conferido,
            -item_rota.ordem_entrega,
            -item_rota.id,
        ),
    )
    itens_carregamento_pendentes = [
        item_rota for item_rota in itens_carregamento if not item_rota.carregamento_conferido
    ]
    itens_carregamento_conferidos = [
        item_rota for item_rota in itens_carregamento if item_rota.carregamento_conferido
    ]

    return render(
        request,
        "estoque/entrega_checklist.html",
        {
            "rota": rota,
            "itens_entrega": itens_entrega,
            "itens_carregamento": itens_carregamento,
            "itens_carregamento_pendentes": itens_carregamento_pendentes,
            "itens_carregamento_conferidos": itens_carregamento_conferidos,
            "salvo_item_id": salvo_item_id,
            "salvo_fase": salvo_fase,
            "checklist_url": request.build_absolute_uri(
                reverse("estoque:entrega_rota_checklist", kwargs={"pk": rota.id})
            ),
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
            "eventos": venda.eventos.all(),
            "comunicacoes_whatsapp": venda.eventos.filter(
                canal="whatsapp",
                tipo_evento__in=["whatsapp_aberto", "whatsapp_confirmado"],
            ),
            "entrega_contexto": entrega_contexto,
        },
    )


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
