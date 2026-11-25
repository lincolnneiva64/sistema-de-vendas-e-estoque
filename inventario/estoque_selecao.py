"""Apresentação do saldo atual na seleção; nenhuma conversão de estoque é gravada."""
from decimal import Decimal, ROUND_HALF_UP
import re

from .apresentacao import equivalencia_visual, numero

EMBALAGENS = {'CX', 'FD', 'PCT', 'DZ'}
EMBALAGEM_1KG = re.compile(r'\b\d+\s*/\s*1\s*kg\b', re.IGNORECASE)


def arredondar(valor, casas):
    resultado = valor.quantize(Decimal('1') if casas == 0 else Decimal('.001'), rounding=ROUND_HALF_UP)
    return abs(resultado) if resultado == 0 else resultado


def peso(valor, casas=3):
    return format(valor, '.0f' if casas == 0 else '.3f').replace('.', ',') + ' kg'


def composicao(total, fator, base, secundaria, casas=0):
    embalagens, restante = divmod(abs(total), fator)
    avulsos = peso(restante, casas) if secundaria == 'KG' else numero(restante) + ' UN'
    texto = numero(embalagens) + ' ' + base + ' + ' + avulsos
    return '-(' + texto + ')' if total < 0 else texto


def apresentar_estoque(produto):
    base = (produto.unidade_venda_1 or produto.unidade_compra or '').strip().upper()
    if produto.quantidade is None:
        return {'principal': 'Não informado', 'equivalencia': ''}
    saldo = Decimal(produto.quantidade)
    if base == 'KG':
        return {'principal': peso(arredondar(saldo, 3)), 'equivalencia': ''}
    if base == 'UN':
        return {'principal': numero(arredondar(saldo, 0)) + ' UN', 'equivalencia': ''}
    original = numero(produto.quantidade) + (' ' + ('kg' if base == 'KG' else base) if base else '')
    conversao = equivalencia_visual(produto.quantidade, {
        'base': base,
        'secundaria': produto.unidade_venda_2,
        'fator': produto.fator_conversao,
    })
    equivalente = conversao.get('texto', '').replace(' KG', ' kg')
    secundaria = (produto.unidade_venda_2 or '').strip().upper()
    if equivalente and base in EMBALAGENS and secundaria in {'UN', 'KG'}:
        fator = Decimal(str(produto.fator_conversao))
        # O nome identifica apenas a embalagem de 1 kg. Unidade e fator vêm
        # exclusivamente do cadastro; nenhum peso é inferido do nome.
        inteiro = fator == fator.to_integral_value()
        pacote_1kg = (secundaria == 'KG' and inteiro
                      and EMBALAGEM_1KG.search(getattr(produto, 'nome', '')))
        casas = 0 if secundaria == 'UN' or pacote_1kg else 3
        if secundaria == 'KG' or inteiro:
            total = arredondar(saldo * fator, casas)
            parcelas = composicao(total, fator, base, secundaria, casas)
            if secundaria == 'KG':
                return {'principal': peso(total, casas), 'equivalencia': parcelas}
            return {'principal': parcelas, 'equivalencia': numero(total) + ' UN'}
    if equivalente and secundaria == 'UN':
        equivalente = numero(arredondar(saldo * Decimal(str(produto.fator_conversao)), 0)) + ' UN'
    if equivalente and secundaria == 'KG':
        equivalente = peso(arredondar(saldo * Decimal(str(produto.fator_conversao)), 3))
        return {'principal': equivalente, 'equivalencia': original}
    return {'principal': original, 'equivalencia': equivalente}
