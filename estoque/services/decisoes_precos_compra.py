"""Explicit purchase price decisions, bound to a server-side read projection.

Callers retain the purchase/catalog locks through validation and execution.
No persistence, price calculation or permission bypass lives in this module.
"""
import hashlib
import json
from decimal import Decimal, InvalidOperation

from django.core import signing
from django.urls import reverse

from estoque.models import MembroGrupoProduto

SALT = 'compra.decisao-precos.v1'
CAMPOS = ('preco_vista', 'preco_prazo', 'preco_vista_fracionado', 'preco_prazo_fracionado')


class DecisaoPrecosInvalida(ValueError):
    pass


def _erro():
    return DecisaoPrecosInvalida('Os itens ou preços mudaram, ou falta uma decisão sobre os preços de venda. '
        'Seus dados foram preservados. Revise os preços novamente antes de finalizar.')


def _projecao(compra):
    itens = list(compra.itens.select_related('produto').order_by('produto_id', 'pk'))
    vinculos = {v.produto_id: v for v in MembroGrupoProduto.objects.filter(
        produto_id__in=[i.produto_id for i in itens]).select_related('grupo')}
    grupos = {}
    for grupo_id, produto_id in MembroGrupoProduto.objects.filter(
            grupo_id__in={v.grupo_id for v in vinculos.values()}).values_list('grupo_id', 'produto_id'):
        grupos.setdefault(grupo_id, []).append(produto_id)
    linhas = []
    for item in itens:
        p = item.produto
        if p is None:
            continue
        v = vinculos.get(p.pk)
        anterior = p.preco_compra or Decimal('0')
        # A legacy draft may already own a cost update. Only reuse proven provenance.
        registro = (item.alteracoes_precos or {}).get('preco_compra', {})
        try:
            custo_snapshot = Decimal(str(registro.get('novo')))
        except (InvalidOperation, ValueError):
            custo_snapshot = None
        if (registro and p.autoria_precos.get('preco_compra') == registro.get('alteracao_id')
                and p.preco_compra == custo_snapshot and item.preco_unitario == custo_snapshot):
            anterior = Decimal(registro['anterior'] or '0')
        if item.preco_compra_anterior is not None:
            anterior = item.preco_compra_anterior
        chave = f'g:{v.grupo_id}' if v else f'p:{p.pk}'
        campos = ('preco_compra', 'preco_compra_fracionado', 'preco_venda', *CAMPOS,
                  'unidade_compra', 'unidade_venda_1', 'unidade_venda_2', 'fator_conversao',
                  'vende_fracionado', 'ativo', 'excluido', 'precos_canonicos_adotados')
        dados = {c: str(getattr(p, c)) for c in campos}
        linhas.append(dict(produto_id=p.pk, chave=chave, quantidade=str(item.quantidade),
            unidade=item.unidade or '', custo_novo=str(item.preco_unitario), custo_anterior=str(anterior),
            dados=dados, autoria=p.autoria_precos, grupo=(dict(id=v.grupo_id,
                nome=v.grupo.nome, versao=v.grupo.versao_precos,
                url=reverse("estoque:grupos_produtos_detalhe", args=[v.grupo_id]),
                regularizado=v.grupo.precos_regularizados,
                membros=sorted(grupos[v.grupo_id])) if v else None),
            pendente=item.preco_compra_anterior is not None and not item.revisao_preco_concluida))
    return linhas


def _hash(linhas):
    return hashlib.sha256(json.dumps(linhas, sort_keys=True, default=str).encode()).hexdigest()


def _token(compra, usuario, linhas, alvo='compra'):
    return signing.dumps(dict(compra=compra.pk, usuario=usuario.pk, alvo=alvo,
                              resumo=_hash(linhas)), salt=SALT, compress=True)


def _conferir(token, compra, usuario, linhas, alvo='compra'):
    try:
        if not isinstance(token, str) or len(token) > 10000:
            raise _erro()
        recebido = signing.loads(token, salt=SALT, max_age=1200)
    except signing.BadSignature:
        raise _erro()
    if recebido != dict(compra=compra.pk, usuario=usuario.pk, alvo=alvo, resumo=_hash(linhas)):
        raise _erro()


def preparar(compra, usuario):
    linhas = _projecao(compra)
    return dict(previa=_token(compra, usuario, linhas), itens=linhas)


def validar_finalizacao(compra, usuario, post, precos, custos, produtos_bloqueados=None):
    linhas = _projecao(compra)
    divergentes = [l for l in linhas if Decimal(l['custo_anterior']) != Decimal(l['custo_novo'])]
    chaves = {l['chave'] for l in divergentes}
    if not chaves and not precos and not post.get('previa_decisao_precos'):
        return False, {}
    _conferir(post.get('previa_decisao_precos'), compra, usuario, linhas)
    modo = post.get('decisao_precos_compra')
    if modo == 'adiar':
        if precos or not {l['produto_id'] for l in divergentes}.issubset(set(custos)):
            raise _erro()
        return bool(chaves), {l['produto_id']: Decimal(l['custo_anterior']) for l in divergentes}
    if modo != 'revisado':
        raise _erro()
    try:
        decisoes = json.loads(post.get('decisoes_precos_itens', ''))
    except (TypeError, ValueError):
        raise _erro()
    if not isinstance(decisoes, dict) or set(decisoes) != chaves:
        raise _erro()
    por_produto = {l['produto_id']: l for l in linhas}
    alterados = set()
    comandos_grupo = {}
    for (pid, campo), valor in precos.items():
        linha = por_produto.get(pid)
        if (not linha or campo not in CAMPOS or linha['chave'] not in chaves
                or decisoes[linha['chave']] != 'alterar'):
            raise _erro()
        if Decimal(linha['dados'][campo]) != valor:
            alterados.add(linha['chave'])
            if linha['grupo']:
                comandos_grupo[pid] = linha['grupo']
    for chave, decisao in decisoes.items():
        if decisao not in ('manter', 'alterar') or (decisao == 'alterar' and chave not in alterados):
            raise _erro()
    # Reuse the existing selective approval before costs/stock/financial effects.
    # Purchase callers already lock all peers in ascending product order.
    from .precos_vinculados import diagnosticar_grupo
    assinaturas = {}
    for pid, grupo in comandos_grupo.items():
        obrigatorios = {l['produto_id'] for l in linhas if l['chave'] == f"g:{grupo['id']}"}
        if (pid not in getattr(precos, 'confirmados', set())
                or getattr(precos, 'versoes_grupos', {}).get(pid) != grupo['versao']
                or not obrigatorios.issubset(getattr(precos, 'selecionados', {}).get(pid, set()))):
            raise _erro()
        if grupo['id'] not in assinaturas:
            atuais = [produtos_bloqueados[pk] for pk in grupo['membros']
                      if pk in (produtos_bloqueados or {})]
            if len(atuais) != len(grupo['membros']):
                raise _erro()
            assinaturas[grupo['id']] = diagnosticar_grupo(atuais, validar_precos=False)['assinatura_precos']
        if getattr(precos, 'assinaturas', {}).get(pid) != assinaturas[grupo['id']]:
            raise _erro()
    return False, {}


def preparar_posterior(compra, usuario):
    linhas = _projecao(compra)
    return {chave: _token(compra, usuario, [l for l in linhas if l['chave'] == chave], chave)
            for chave in {l['chave'] for l in linhas if l['pendente']}}


def validar_posterior(compra, item, usuario, post, precos):
    linhas = _projecao(compra)
    alvo = next((l for l in linhas if l['produto_id'] == item.produto_id), None)
    if alvo is None:
        raise _erro()
    try:
        _conferir(post.get('previa_decisao_precos'), compra, usuario,
                  [l for l in linhas if l['chave'] == alvo['chave']], alvo['chave'])
    except DecisaoPrecosInvalida:
        raise DecisaoPrecosInvalida('Esta revisão está desatualizada ou expirou. '
            'Feche e abra a revisão novamente para conferir os valores atuais. '
            'A pendência e os preços foram preservados.')
    decisao = post.get('decisao_precos')
    if decisao == 'manter' and not precos:
        return
    if (decisao != 'alterar' or not precos
            or any(pid != item.produto_id or campo not in CAMPOS for pid, campo in precos)
            or not any(Decimal(alvo['dados'][campo]) != valor for (_, campo), valor in precos.items())):
        raise DecisaoPrecosInvalida('Escolha alterar os preços ou manter os preços atuais. '
            'A revisão continua pendente e nenhum preço foi alterado.')
