"""Pure, conservative attribution of a removal event to another sale product."""
from collections import Counter, defaultdict
from datetime import timedelta
from decimal import Decimal, InvalidOperation
import re


CAMPOS = ('id', 'venda_id', 'produto_id', 'produto_nome_snapshot',
    'item_venda_original_id', 'quantidade_snapshot', 'unidade_snapshot',
    'valor_total_snapshot', 'status', 'criado_em', 'revertido_em',
    'estoque_devolvido', 'estoque_devolvido_em', 'ajuste_origem_id')

NUMERO = r'\d+(?:\.\d+)?'
PADRAO = re.compile(
    r'^Item removido da nota: (?P<nome>.+), quantidade (?P<quantidade>' + NUMERO +
    r') (?P<unidade>[^,]+), valor abatido R\$ (?P<valor>' + NUMERO +
    r')\. Registro de remocao #(?P<registro>\d+)\. Total da venda: R\$ ' + NUMERO +
    r' -> R\$ ' + NUMERO + r'\. (?P<devolucao>Estoque devolvido para o produto removido\.'
    r'|Sem devolucao de estoque para este item\.)$')
LEGADO = re.compile(r'^Item removido da nota: (?P<nome>.+), quantidade (?P<quantidade>' +
    NUMERO + r') (?P<unidade>[^,]+), valor abatido R\$ (?P<valor>' + NUMERO + r')\.$')


def nome(valor):
    return str(valor or '').strip().casefold()


def comprovar(venda_id, produto_id, nomes_alvo, itens, eventos, registros, *, legados=None):
    """No textual name is accepted without a unique explicit record identity."""
    por_id = {r['id']: r for r in registros}
    originais = Counter(r['item_venda_original_id'] for r in registros)
    identidades = defaultdict(set)
    for i in itens:
        if i.produto_id:
            identidades[nome(i.produto.nome)].add(i.produto_id)
    for r in registros:
        identidades[nome(r['produto_nome_snapshot'])].add(r['produto_id'])
    remocoes = [e for e in eventos if e['tipo_evento'] == 'item_removido_da_nota']
    matches = []
    for e in remocoes:
        match = PADRAO.fullmatch(e.get('descricao', ''))
        if not match and legados and e['pk'] in legados:
            antigo = LEGADO.fullmatch(e.get('descricao', ''))
            r = por_id.get(legados[e['pk']])
            if antigo and r:
                match = dict(antigo.groupdict(), registro=str(r['id']),
                    devolucao='Estoque devolvido' if r['estoque_devolvido'] else 'Sem devolucao')
        matches.append((e, match))
    referencias = Counter(int(m['registro']) for _, m in matches if m)
    comprovadas = []
    for evento, match in matches:
        erro = 'Remoção sem vínculo histórico único e compatível com outro produto; snapshot não certificado.'
        try:
            r = por_id.get(int(match['registro'])) if match else None
            if (not r or referencias[r['id']] != 1 or r['venda_id'] != venda_id
                    or not r['produto_id'] or r['produto_id'] == produto_id
                    or not r['item_venda_original_id'] or originais[r['item_venda_original_id']] != 1
                    or any(i.pk == r['item_venda_original_id'] or i.produto_id == r['produto_id'] for i in itens)
                    or r['status'] != 'removido' or r['revertido_em'] is not None
                    or r['ajuste_origem_id'] is not None
                    or nome(match['nome']) != nome(r['produto_nome_snapshot'])
                    or nome(r['produto_nome_snapshot']) in nomes_alvo
                    or identidades[nome(r['produto_nome_snapshot'])] != {r['produto_id']}
                    or Decimal(match['quantidade']) != r['quantidade_snapshot']
                    or r['quantidade_snapshot'] <= 0
                    or match['unidade'] != r['unidade_snapshot'] or not r['unidade_snapshot'].strip()
                    or Decimal(match['valor']) != r['valor_total_snapshot']
                    or r['valor_total_snapshot'] < 0
                    or not evento.get('criado_em') or not r['criado_em']
                    or not timedelta(0) <= evento['criado_em'] - r['criado_em'] <= timedelta(seconds=5)
                    or r['estoque_devolvido'] != match['devolucao'].startswith('Estoque devolvido')
                    or r['estoque_devolvido'] != (r['estoque_devolvido_em'] is not None)):
                raise ValueError(erro)
        except (KeyError, TypeError, InvalidOperation):
            raise ValueError(erro) from None
        # Persist the material relationship in the existing proposal JSON. No
        # record is modified; re-evaluation detects a changed removal snapshot.
        comprovadas.append(dict(evento=evento['pk'], registro=r['id'], venda=r['venda_id'],
            produto=r['produto_id'], item_original=r['item_venda_original_id'],
            nome=r['produto_nome_snapshot'], quantidade=str(r['quantidade_snapshot']),
            unidade=r['unidade_snapshot'], valor=str(r['valor_total_snapshot']),
            criado_em=r['criado_em'].isoformat(), estoque_devolvido=r['estoque_devolvido'],
            estoque_devolvido_em=r['estoque_devolvido_em'].isoformat() if r['estoque_devolvido_em'] else None))
    return comprovadas
