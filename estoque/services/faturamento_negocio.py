"""Consolidação gerencial somente leitura; não recalcula contratos ou pagamentos."""
from locacoes.consulta import faturamento_locacoes
from decimal import Decimal


def consolidar_faturamento(inicio, fim, mercadorias):
    """Uma agregação por competência gerencial, sem exigir baixa operacional.

    Competência aprovada: data_entrega, não cadastro, confirmação ou pagamento.
    O total contratual já apurado por Locações entra uma vez por contrato.
    """
    locacoes = faturamento_locacoes(inicio, fim)
    total = mercadorias + locacoes['faturamento']
    participacao = (locacoes['faturamento'] / total * Decimal('100')).quantize(Decimal('0.01')) if total else Decimal('0.00')
    return dict(inicio=inicio, fim=fim, mercadorias=mercadorias, locacoes=locacoes,
                total=total, participacao_locacoes=participacao)


def simular_margem_geral(faturamento, rentabilidade):
    """Simulação sobre valores já apurados, sem atribuir custos às locações.

    Usa a mesma precedência do lucro gerencial exibido: estimativa histórica,
    resultado completo ou parcela com custo. Ausência de lucro não vira zero.
    """
    estimativa = rentabilidade.get('estimativa_setembro')
    if estimativa:
        lucro = estimativa.get('lucro_parcial')
    elif rentabilidade.get('disponivel'):
        lucro = rentabilidade.get('lucro')
    else:
        lucro = rentabilidade.get('lucro_coberto')
    parcial = bool(estimativa or not rentabilidade.get('disponivel'))
    total = faturamento['total']
    resultado_bruto = None if lucro is None else lucro + faturamento['locacoes']['faturamento']
    valor = None if resultado_bruto is None else (
        (resultado_bruto / total * Decimal('100')).quantize(Decimal('.01'))
        if total else Decimal('0.00'))
    return dict(valor=valor, parcial=parcial, lucro_base=lucro, resultado_bruto=resultado_bruto)
