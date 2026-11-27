"""Presentation-only revenue shares from the already calculated class rows."""
from decimal import Decimal
from fractions import Fraction


def participacao_classes(analise):
    classes = analise['classes']
    total = analise['total']['faturamento']
    empty = dict(disponivel=False, por_classe={}, segmentos=[])
    if total <= 0 or any(c['faturamento'] < 0 for c in classes):
        return empty
    if sum(c['faturamento'] for c in classes) != total:
        return empty
    # Largest remainders allocate hundredths of a percent, without changing
    # monetary values. Shares close at 100.00 and are independent of table order.
    exact = {c['chave']: Fraction(c['faturamento']) / Fraction(total) * 10000 for c in classes}
    units = {key: value.numerator // value.denominator for key, value in exact.items()}
    remainder = 10000 - sum(units.values())
    for key in sorted(exact, key=lambda k: (-(exact[k] - units[k]), k))[:remainder]:
        units[key] += 1
    shares = {key: Decimal(value) / 100 for key, value in units.items()}
    ranked = sorted(classes, key=lambda c: (-c['faturamento'], c['chave']))
    segments = [dict(chave=c['chave'], nome=c['nome'], percentual=shares[c['chave']],
        margem=c.get('margem'), cor=i+1)
        for i, c in enumerate(ranked[:6])]
    rest = ranked[6:]
    if rest and sum(c['faturamento'] for c in rest) > 0:
        segments.append(dict(chave='', nome='Demais classes',
            percentual=sum(shares[c['chave']] for c in rest), cor=7))
    return dict(disponivel=True, por_classe=shares, segmentos=segments)
