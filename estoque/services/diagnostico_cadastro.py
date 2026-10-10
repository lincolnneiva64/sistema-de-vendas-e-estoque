"""Read-only conflict evidence. Never authorizes or changes a product."""
import json
import logging
from uuid import uuid4

from django.core import signing
from django.utils.crypto import constant_time_compare, salted_hmac

logger = logging.getLogger(__name__)
SALT = 'estoque.diagnostico_cadastro.v1'
MAX_AGE = 24 * 60 * 60
LABELS = {
    'custo': 'Custo', 'estoque': 'Estoque', 'fracionamento': 'Fracionamento',
    'comercial': 'Demais dados comerciais', 'metadados': 'Metadados',
}
CUSTO = {'preco_compra', 'preco_compra_fracionado'}
ESTOQUE = {'quantidade', 'estoque_minimo', 'estoque_conferido'}
FRACIONAMENTO = {'unidade_compra', 'unidade_venda_1', 'unidade_venda_2',
                 'fator_conversao', 'vende_fracionado', 'descricao_conversao'}


def fingerprints(produto, nonce):
    categorias = {categoria: {} for categoria in LABELS}
    for field in produto._meta.concrete_fields:
        campo = field.attname
        if campo in CUSTO:
            categoria = 'custo'
        elif campo in ESTOQUE:
            categoria = 'estoque'
        elif campo in FRACIONAMENTO:
            categoria = 'fracionamento'
        elif field.primary_key or campo.endswith(('_em', '_por_id')) or campo == 'autoria_precos':
            categoria = 'metadados'
        else:
            categoria = 'comercial'
        categorias[categoria][campo] = getattr(produto, campo)
    return {categoria: salted_hmac(
        SALT + '.' + categoria,
        nonce + ':' + json.dumps(dados, sort_keys=True, default=str),
        algorithm='sha256',
    ).hexdigest() for categoria, dados in categorias.items()}


def criar_evidencia(produto, operador, token):
    if not operador or not operador.is_authenticated:
        return ''
    nonce = uuid4().hex
    return signing.dumps({'v': 1, 'produto': produto.pk, 'operador': operador.pk,
        'token': token, 'nonce': nonce, 'categorias': fingerprints(produto, nonce)}, salt=SALT)


def diagnosticar_recusa(produto, operador, token, evidencia):
    """Only categorical data reaches logs; malformed input is never logged."""
    indisponivel = 'Diagnóstico indisponível'
    if not operador or not operador.is_authenticated or not isinstance(evidencia, str) or len(evidencia) > 8192:
        return indisponivel
    try:
        dados = signing.loads(evidencia, salt=SALT, max_age=MAX_AGE, fallback_keys=[])
        if (dados['v'] != 1 or dados['produto'] != produto.pk or dados['operador'] != operador.pk
                or not constant_time_compare(dados['token'], token or '')
                or not isinstance(dados['nonce'], str) or len(dados['nonce']) != 32
                or set(dados['categorias']) != set(LABELS)):
            return indisponivel
        atuais = fingerprints(produto, dados['nonce'])
        divergentes = [c for c in LABELS if not constant_time_compare(dados['categorias'][c], atuais[c])]
    except (signing.BadSignature, ValueError, TypeError, KeyError, AttributeError):
        return indisponivel
    identificador = uuid4().hex
    logger.warning('conflito_cadastro diagnostico=%s categorias=%s', identificador, ','.join(divergentes) or 'nenhuma')
    categorias = ', '.join(LABELS[c] for c in divergentes) or 'Nenhuma categoria do cadastro; confira a versão do grupo'
    return f'Diagnóstico {identificador}: {categorias}.'
