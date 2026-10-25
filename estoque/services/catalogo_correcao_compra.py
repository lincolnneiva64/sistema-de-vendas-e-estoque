"""Catalog effects of an audited correction, under the purchase transaction."""
from decimal import Decimal
from uuid import uuid4
from types import SimpleNamespace

from django.db import transaction
from django.db.models import Q

from estoque.models import Compra, ItemCompra, Produto, ProdutoFornecedor, EdicaoCompra
from .precos_compra import aplicar_precos_compra_em_lote
from .precos_vinculados import bloquear_catalogo


def custo_base_item(item, produto):
    from estoque.views import _normalizar_unidade_estoque, _quantidade_estoque_para_unidade_base
    _quantidade_estoque_para_unidade_base(produto, item.quantidade, item.unidade)
    unidade = _normalizar_unidade_estoque(item.unidade)
    base = _normalizar_unidade_estoque(produto.unidade_venda_1 or produto.unidade_compra)
    custo = item.preco_unitario
    if unidade and unidade != base:
        # The stock routine above validates the only permitted conversion.
        custo *= Decimal(produto.fator_conversao)
    custo = custo.quantize(Decimal('0.01'))
    if not custo.is_finite() or not Decimal('0') <= custo < Decimal('10000000000'):
        raise ValueError(f'Custo convertido fora do limite para {produto.nome}.')
    return custo


def sincronizar(compra, item_ids, executar=True, anteriores=()):
    """Only affected items; never replay stock or financial finalization."""
    efeitos = []
    comandos = []
    for item in compra.itens.filter(pk__in=item_ids).select_related('produto').order_by('pk'):
        produto = Produto.objects.select_for_update().get(pk=item.produto_id)
        custo = custo_base_item(item, produto)
        # A historical correction cannot replace a more recent purchase cost,
        # including a subsequently entered purchase with a backdated invoice.
        posteriores = ItemCompra.objects.filter(produto=produto,
            compra__status=Compra.STATUS_FINALIZADA, compra__cancelada=False).exclude(compra=compra).filter(
                Q(compra__data_compra__gt=compra.data_compra)
                | Q(compra__data_compra=compra.data_compra, compra_id__gt=compra.pk)
                | Q(compra__criado_em__gt=compra.criado_em))
        if executar and not posteriores.filter(compra__fornecedor=compra.fornecedor).exists():
            vinculo, _ = ProdutoFornecedor.objects.get_or_create(produto=produto, fornecedor=compra.fornecedor)
            if not vinculo.ultima_compra_em or vinculo.ultima_compra_em <= compra.data_compra:
                vinculo.ultimo_preco_compra = custo
                vinculo.ultima_compra_em = compra.data_compra
                vinculo.save(update_fields=['ultimo_preco_compra','ultima_compra_em','atualizado_em'])
        posterior = posteriores.order_by('-compra__data_compra', '-compra_id').first()
        if posterior:
            efeitos.append({'item_id':item.pk,'produto_id':produto.pk,'status':'compra_posterior',
                'compra_posterior_id':posterior.compra_id,'custo_preservado':str(produto.preco_compra)})
            continue
        atualizacoes = {'preco_compra':custo}
        if produto.vende_fracionado:
            fator = Decimal(produto.fator_conversao or 0)
            if fator <= 0:
                raise ValueError(f'Fator de conversão inválido para atualizar o custo de {produto.nome}.')
            atualizacoes['preco_compra_fracionado'] = (custo / fator).quantize(Decimal('0.01'))
        mudou = any(getattr(produto, campo) != valor for campo, valor in atualizacoes.items())
        efeito = {'item_id':item.pk,'produto_id':produto.pk,'status':'atualizado' if mudou else 'inalterado',
            'custo_anterior':str(produto.preco_compra),'custo_novo':str(custo),
            'venda_preservada':True,'revisao_criada':mudou}
        efeitos.append(efeito)
        if not executar:
            continue
        if mudou:
            if item.preco_compra_anterior is None or item.revisao_preco_concluida:
                item.preco_compra_anterior = produto.preco_compra or Decimal('0')
            item.revisao_preco_concluida = False
            item.save(update_fields=['preco_compra_anterior','revisao_preco_concluida'])
            comandos.append({'item':item,'atualizacoes':atualizacoes})
    atuais = set(compra.itens.values_list('produto_id',flat=True))
    for original in anteriores:
        pk = original['produto_id']
        if not pk or pk in atuais:
            continue
        vinculo = ProdutoFornecedor.objects.select_for_update().filter(produto_id=pk,fornecedor=compra.fornecedor).first()
        if not vinculo or vinculo.ultima_compra_em != compra.data_compra:
            continue
        produto = Produto.objects.get(pk=pk)
        custo_original = custo_base_item(SimpleNamespace(quantidade=Decimal(original['quantidade']),
            unidade=original['unidade'],preco_unitario=Decimal(original['preco_unitario'])),produto)
        if vinculo.ultimo_preco_compra != custo_original:
            continue
        ultimo = ItemCompra.objects.filter(produto_id=pk,compra__fornecedor=compra.fornecedor,
            compra__status=Compra.STATUS_FINALIZADA,compra__cancelada=False).exclude(compra=compra).select_related('compra').order_by('-compra__data_compra','-compra_id').first()
        valor = custo_base_item(ultimo,produto) if ultimo else None
        data = ultimo.compra.data_compra if ultimo else None
        efeitos.append({'produto_id':pk,'status':'fornecedor_recalculado',
            'ultimo_preco_anterior':str(vinculo.ultimo_preco_compra),
            'ultimo_preco_corrigido':str(valor) if valor is not None else None})
        if executar:
            vinculo.ultimo_preco_compra=valor;vinculo.ultima_compra_em=data
            vinculo.save(update_fields=['ultimo_preco_compra','ultima_compra_em','atualizado_em'])
    if executar:
        aplicar_precos_compra_em_lote(compra, comandos)
        compra.revisao_precos_pendente = compra.itens.filter(
            preco_compra_anterior__isnull=False, revisao_preco_concluida=False).exists()
        compra.save(update_fields=['revisao_precos_pendente','atualizado_em'])
    return efeitos


def regularizar(compra_id, usuario, executar=False):
    """Explicit opt-in repair of an audited omission; preview by default."""
    from .correcao_compra import _estado
    from .edicao_compra import _json, _hash
    if not usuario.has_perm('estoque.change_compra'):
        raise ValueError('Usuário sem permissão para regularizar a compra.')
    with transaction.atomic(), bloquear_catalogo():
        compra = Compra.objects.select_for_update().get(pk=compra_id)
        if compra.status != Compra.STATUS_FINALIZADA or compra.cancelada:
            raise ValueError('Regularização exige compra finalizada e não cancelada.')
        fontes = list(compra.edicoes.filter(depois__ajustes__isnull=False).order_by('pk'))
        if not fontes:
            raise ValueError('Não existe correção auditada para comprovar esta regularização.')
        origem = fontes[-1]
        anterior = compra.edicoes.filter(depois__regularizacao_origem=origem.pk).first()
        if anterior:
            return {'ja_processado':True,'origem':origem.pk}
        estado_origem = origem.depois['estado']
        itens = list(compra.itens.order_by('pk'))
        esperados = {i['id']:i for i in estado_origem['compra']['itens']}
        if {i.pk for i in itens} != set(esperados) or any(any(
            str(getattr(i,campo)) != str(esperados[i.pk][campo])
            for campo in ('produto_id','quantidade','preco_unitario','unidade')) for i in itens):
            raise ValueError('Itens divergiram da correção auditada. Nenhum custo foi alterado.')
        candidatos = set()
        for evento in fontes:
            if 'catalogo' in evento.depois['ajustes']:
                candidatos.difference_update(e.get('item_id') for e in evento.depois['ajustes']['catalogo'])
                continue
            originais = {i['id']:i for i in evento.antes['compra']['itens']}
            for corrigido in evento.depois['estado']['compra']['itens']:
                original = originais.get(corrigido['id'])
                if not original or any(str(corrigido[c]) != str(original[c]) for c in ('produto_id','preco_unitario','unidade')):
                    candidatos.add(corrigido['id'])
        if not candidatos and 'catalogo' in origem.depois['ajustes']:
            return {'ja_processado':True,'origem':origem.pk}
        afetados = [i for i in itens if i.pk in candidatos]
        baseline = {p['id']:p for p in estado_origem['produtos']}
        produtos = {p.pk:p for p in Produto.objects.select_for_update().filter(
            pk__in=baseline).order_by('pk')}
        removidos = {i['produto_id'] for i in origem.antes['compra']['itens']} - {i.produto_id for i in itens}
        validar = {i.produto_id for i in afetados} | set(ProdutoFornecedor.objects.filter(
            produto_id__in=removidos,fornecedor=compra.fornecedor,ultima_compra_em=compra.data_compra).values_list('produto_id',flat=True))
        for pk in validar:
            p = produtos.get(pk)
            if not p:
                raise ValueError('Produto da correção não está mais disponível para regularização.')
            esperado = baseline.get(p.pk)
            if not esperado or any(str(getattr(p,c)) != str(esperado[c]) for c in (
                'preco_compra','preco_compra_fracionado','fator_conversao','unidade_compra',
                'unidade_venda_1','unidade_venda_2','vende_fracionado')) or any(
                    p.autoria_precos.get(c) != esperado['autoria_precos'].get(c)
                    for c in ('preco_compra','preco_compra_fracionado')):
                raise ValueError(f'O cadastro de {p.nome} mudou após a correção. Regularização automática recusada; nenhum custo foi alterado.')
        antes = _estado(compra,list(Produto.objects.filter(pk__in=produtos).order_by('pk').values()))
        efeitos = sincronizar(compra,[i.pk for i in afetados],executar=executar,anteriores=origem.antes['compra']['itens'])
        if executar:
            EdicaoCompra.objects.create(compra=compra,usuario=usuario,operacao=uuid4().hex,
                motivo=f'Regularização de catálogo da correção {origem.pk}',
                payload_hash=_hash({'regularizacao_origem':origem.pk}),antes=_json(antes),
                depois=_json({'regularizacao_origem':origem.pk,'catalogo':efeitos,
                    'estado':_estado(compra,list(Produto.objects.filter(pk__in=produtos).order_by('pk').values()))}))
        return {'origem':origem.pk,'aplicado':executar,'catalogo':efeitos}
