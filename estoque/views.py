import json
from decimal import Decimal, InvalidOperation

from django.db import transaction
from django.db.models import Q
from urllib.parse import quote, urlencode
from django.shortcuts import render, redirect, get_object_or_404
from django.urls import reverse
from django.db.models import Case, When, Value, IntegerField, F
from .forms import CategoriaForm, ClienteForm, ProdutoForm, UnidadeForm
from .models import Categoria, Cliente, ItemVenda, Produto, Unidade, Venda
from django.contrib import messages
from django.http import JsonResponse
from django.utils.dateparse import parse_date
from django.utils import timezone
from django.views.decorators.csrf import ensure_csrf_cookie
from django.views.decorators.http import require_POST
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

    return JsonResponse({
        "sucesso": True,
        "mensagem": f"Venda #{venda.id} gravada com sucesso.",
        "venda_id": venda.id,
        "visualizar_url": reverse("estoque:venda_detalhe", args=[venda.id]),
    })


def venda_detalhe(request, pk):
    venda = get_object_or_404(
        Venda.objects.select_related("cliente").prefetch_related("itens__produto"),
        pk=pk,
    )
    whatsapp_url = montar_link_whatsapp_venda(venda)
    return render(
        request,
        "estoque/venda_detalhe.html",
        {
            "venda": venda,
            "whatsapp_url": whatsapp_url,
        },
    )


def _formatar_moeda(valor):
    numero = Decimal(valor or 0)
    texto = f"{numero:,.2f}"
    return "R$ " + texto.replace(",", "X").replace(".", ",").replace("X", ".")


def montar_link_whatsapp_venda(venda):
    cliente = venda.cliente
    if not cliente:
        return ""

    numero = Cliente.normalizar_whatsapp(cliente.whatsapp_normalizado or cliente.whatsapp)
    if not numero:
        return ""

    if len(numero) in (10, 11):
        numero = "55" + numero

    linhas = [
        f"Ola, {cliente.nome}.",
        "",
        f"Segue o resumo da venda #{venda.id}.",
        f"Data da venda: {venda.data_venda.strftime('%d/%m/%Y')}",
    ]

    if venda.data_vencimento:
        linhas.append(f"Vencimento: {venda.data_vencimento.strftime('%d/%m/%Y')}")

    linhas.extend(["", "Itens:"])

    for item in venda.itens.all():
        nome_produto = item.produto.nome if item.produto else "Produto nao identificado"
        linhas.append(
            f"- {item.quantidade} {item.unidade} - {nome_produto} | "
            f"Unit.: {_formatar_moeda(item.preco_unitario)} | "
            f"Total: {_formatar_moeda(item.valor_total)}"
        )

    linhas.extend([
        "",
        f"Total da venda: {_formatar_moeda(venda.total)}",
    ])

    mensagem = "\n".join(linhas)
    return f"https://wa.me/{numero}?text={quote(mensagem)}"
