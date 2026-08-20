def revisao_produtos(request):
    """
    View para revisão em lote de produtos importados do sistema antigo.
    
    - GET: Exibe formulário com filtros e lista de produtos
    - POST: Processa edições e marcações de revisão
    
    Produtos para revisão: Aqueles com codigo_legado preenchido
    Produtos revisados: Aqueles com revisado_importacao = True
    """
    from .forms_revisao import ProdutoRevisaoForm, ProdutoRevisaoFiltrosForm
    from django.core.paginator import Paginator
    from django.utils import timezone
    
    # Determinar filtro
    filtro = request.POST.get('filtro', request.GET.get('filtro', 'pendentes'))
    busca = request.POST.get('busca', request.GET.get('busca', ''))
    page_num = request.POST.get('page', request.GET.get('page', 1))
    
    # Query base: apenas produtos com codigo_legado (importados)
    base_query = Produto.objects.filter(
        codigo_legado__isnull=False,
        excluido=False
    ).exclude(codigo_legado='').order_by('id')
    
    # Aplicar filtro de status de revisão
    if filtro == 'pendentes':
        produtos_query = base_query.filter(revisado_importacao=False)
    elif filtro == 'revisados':
        produtos_query = base_query.filter(revisado_importacao=True)
    else:  # 'todos'
        produtos_query = base_query
    
    # Aplicar busca
    if busca:
        produtos_query = produtos_query.filter(
            Q(nome__icontains=busca) |
            Q(codigo__icontains=busca) |
            Q(codigo_legado__icontains=busca)
        )
    
    # Contar totais para exibição
    total_importados = base_query.count()
    total_pendentes = base_query.filter(revisado_importacao=False).count()
    total_revisados = base_query.filter(revisado_importacao=True).count()
    
    # Paginação
    items_por_pagina = 50
    paginator = Paginator(produtos_query, items_por_pagina)
    
    try:
        page_num = int(page_num)
    except (ValueError, TypeError):
        page_num = 1
    
    page = paginator.get_page(page_num)
    
    if request.method == 'POST' and 'salvar' in request.POST:
        # Processar edições e marcações de revisão
        produtos_ids = request.POST.getlist('produto_id')
        
        # Validar que todos os IDs pertencem ao conjunto permitido
        ids_permitidos = set(base_query.values_list('id', flat=True))
        
        with transaction.atomic():
            for produto_id_str in produtos_ids:
                try:
                    produto_id = int(produto_id_str)
                except (ValueError, TypeError):
                    continue
                
                # Segurança: verificar que o ID está no conjunto permitido
                if produto_id not in ids_permitidos:
                    continue
                
                produto = Produto.objects.get(id=produto_id)
                
                # Editar campos
                novo_nome = request.POST.get(f'nome_{produto_id}', '').strip()
                novo_codigo = request.POST.get(f'codigo_{produto_id}', '').strip()
                nova_categoria = request.POST.get(f'categoria_{produto_id}', '').strip()
                revisar_agora = request.POST.get(f'revisar_{produto_id}')
                
                # Atualizar campos
                if novo_nome:
                    produto.nome = novo_nome
                if novo_codigo != '':
                    produto.codigo = novo_codigo
                if nova_categoria:
                    produto.categoria = nova_categoria
                
                # Marcar como revisado ou desmarcar
                if revisar_agora:
                    produto.revisado_importacao = True
                    produto.revisado_importacao_em = timezone.now()
                else:
                    # Se não estiver marcado para revisar, mas já estava revisado
                    # e agora foi desmarcado, resetar
                    if request.POST.get(f'revisar_checkbox_{produto_id}') is None and produto.revisado_importacao:
                        produto.revisado_importacao = False
                        produto.revisado_importacao_em = None
                
                produto.save()
        
        # Redirecionar para o mesmo filtro
        return redirect(f'{reverse("estoque:revisao_produtos")}?filtro={filtro}&busca={busca}&page={page_num}')
    
    # Preparar formulários para exibição
    filtros_form = ProdutoRevisaoFiltrosForm(initial={
        'filtro': filtro,
        'busca': busca,
        'page': page_num,
    })
    
    # Preparar forms para cada produto da página
    produto_forms = []
    for produto in page:
        initial_data = {
            'nome': produto.nome,
            'codigo': produto.codigo or '',
            'categoria': produto.categoria or '',
            'revisado': produto.revisado_importacao,
        }
        form = ProdutoRevisaoForm(initial=initial_data)
        produto_forms.append({
            'produto': produto,
            'form': form,
        })
    
    contexto = {
        'page': page,
        'produto_forms': produto_forms,
        'filtros_form': filtros_form,
        'filtro_atual': filtro,
        'busca_atual': busca,
        'total_importados': total_importados,
        'total_pendentes': total_pendentes,
        'total_revisados': total_revisados,
        'items_por_pagina': items_por_pagina,
    }
    
    return render(request, 'estoque/revisao_produtos.html', contexto)
