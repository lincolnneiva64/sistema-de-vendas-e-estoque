"""Consolidação gerencial somente leitura; não recalcula contratos ou pagamentos."""
from locacoes.consulta import faturamento_locacoes


def consolidar_faturamento(inicio, fim, mercadorias):
    """Uma agregação por competência gerencial, sem exigir baixa operacional.

    Competência aprovada: data_entrega, não cadastro, confirmação ou pagamento.
    O total contratual já apurado por Locações entra uma vez por contrato.
    """
    locacoes = faturamento_locacoes(inicio, fim)
    return dict(inicio=inicio, fim=fim, mercadorias=mercadorias, locacoes=locacoes,
                total=mercadorias + locacoes['faturamento'])
