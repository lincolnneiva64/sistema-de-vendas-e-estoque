"""Read-only labels for physical receipts; never authorize an application."""


def classificar(linhas):
    totais = dict(conferidos=0, confirmados=0, aguardando=0, revisao=0)
    for linha in linhas:
        r = linha['rodada']
        a = linha.get('atualizacao') or {}
        if linha.get('marco'):
            estado, chave = 'Confirmado', 'confirmados'
        elif not r.ok_em or r.encerrada_em:
            estado, chave = 'Aguardando contagem', None
        elif (a.get('bloqueio') or a.get('estado') in ('Rejeitado', 'Aplicado')
                or linha.get('comparacao_atual', {}).get('limite')
                or linha.get('situacao') == 'Revisão necessária'):
            estado, chave = 'Revisão necessária', 'revisao'
        else:
            estado, chave = 'Aguardando confirmação', 'aguardando'
        linha['confirmacao_estoque'] = estado
        if r.ok_em and not r.encerrada_em:
            totais['conferidos'] += 1
        if chave:
            totais[chave] += 1
    return totais


def consultar(inventario, *, detalhar=False):
    from .views import linhas_revisao
    from . import ajustes
    linhas = linhas_revisao(inventario)
    atualizacao = ajustes.projetar_resultado(linhas)
    por_rodada = {l['rodada'].pk: l for l in atualizacao['linhas']}
    for linha in linhas:
        linha['atualizacao'] = por_rodada.get(linha['rodada'].pk)
    totais = classificar(linhas)
    return (totais, linhas) if detalhar else totais


def classificar_avaliacoes(linhas, marcos):
    """Same labels on the existing full preview and compatibility panel."""
    projecoes = [dict(rodada=l['rodada'], marco=marcos.get(l['rodada'].pk),
        atualizacao=l) for l in linhas]
    totais = classificar(projecoes)
    for linha, projecao in zip(linhas, projecoes):
        linha['confirmacao_estoque'] = projecao['confirmacao_estoque']
    return totais
