"""Conference display only: never change counts or authorize adjustments."""
from django import template
from django.utils.html import format_html_join

register = template.Library()


@register.filter
def composicao_curta(texto):
    composicao = str(texto or '').partition(' = ')[0]
    return composicao.removesuffix(' + 0 UN')


@register.filter
def total_curto(texto):
    return str(texto or '').partition(' = ')[2]


@register.filter
def parcelas_quantidade(texto):
    return format_html_join(' + ', '<span class="parcela-quantidade">{}</span>',
                            ((parte,) for parte in str(texto or '').split(' + ')))


@register.filter
def rotulo_simples(texto):
    return {
        'Equivalência física': 'Sem diferença',
        'Equivalência aproximada — resíduo decimal; sem falta física comprovada': 'Diferença de arredondamento',
        'Diferença física não comprovada — referência aproximada': 'Conferir diferença',
        'Resíduo decimal': 'Conferir',
        'Referência aproximada': 'Conferir',
    }.get(str(texto), texto)


@register.filter
def diferenca_resumo(linha):
    """Format existing evidence without changing the adjustment or its authorization."""
    comparacao = linha.get('comparacao_fisica')
    if comparacao:
        if comparacao.get('normalizacao') or comparacao['dados'].get('diferenca_fisica') == '0':
            return 'Sem diferença física'
        return rotulo_simples(comparacao['diferenca'])
    from inventario.apresentacao import quantidade
    diferenca = linha['diferenca']
    if not diferenca:
        return 'Sem diferença física'
    return ('Falta ' if diferenca < 0 else 'Sobra ') + quantidade(
        abs(diferenca), linha['rodada'].item.cadastro, diferenca=True)


@register.filter
def titulo_conferencia(titulo, identificador):
    prefixo = f'Conferência nº {identificador}'
    titulo = str(titulo or '').strip()
    if titulo == prefixo or titulo.startswith(prefixo + ' — '):
        return titulo
    return prefixo + (' — ' + titulo if titulo else '')


@register.filter
def equivalencia_aplicada(linha):
    """Reuse the visual conversion only for the recorded applied balance/unit."""
    atualizacao = linha.get('atualizacao') or {}
    if (atualizacao.get('estado') != 'Aplicado'
            or atualizacao.get('saldo_aplicado') is None):
        return {}
    item, rodada = linha['item'], linha['rodada']
    cadastro = item.get('cadastro', {}) if isinstance(item, dict) else item.cadastro
    aplicada = str(atualizacao.get('unidade_aplicada') or '').strip().upper()
    if not aplicada or aplicada != str(cadastro.get('base') or '').strip().upper():
        return {}
    avulsos = rodada.get('avulsos') if isinstance(rodada, dict) else getattr(rodada, 'avulsos', None)
    from inventario.apresentacao import equivalencia_visual
    return equivalencia_visual(atualizacao['saldo_aplicado'], cadastro, avulsos=avulsos)
