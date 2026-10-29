"""Conference-only physical relation. No commercial product writes."""
from decimal import Decimal, InvalidOperation
from django.core import signing
from django.core.exceptions import PermissionDenied
from django.db import transaction
from .conversao import decimal_entrada, avulsos_fisicos, cadastro_compativel
from .models import Inventario, ItemInventario, RodadaContagem, EventoInventario, DecisaoAjuste

ACAO = 'conversao_fisica_confirmada'
BASES = {'PCT', 'CX', 'FD', 'DZ'}


def carregar(itens):
    itens = list(itens)
    registros = EventoInventario.objects.filter(acao=ACAO, inventario_id__in={i.inventario_id for i in itens}).order_by('pk')
    por_item = {e.dados.get('item_id'): e for e in registros}
    for item in itens:
        item._relacao_fisica = por_item.get(item.pk)


def cadastro_contagem(item):
    cadastro = dict(item.cadastro)
    if cadastro['fracionado'] or avulsos_fisicos(cadastro):
        return cadastro
    if not hasattr(item, '_relacao_fisica'):
        item._relacao_fisica = EventoInventario.objects.filter(inventario_id=item.inventario_id,
            acao=ACAO, dados__item_id=item.pk).order_by('-pk').first()
    registro = item._relacao_fisica
    if registro and registro.dados.get('base') == cadastro['base']:
        cadastro.update(fator=registro.dados['unidades_por_embalagem'], secundaria='UN',
            secundaria_continua=False, avulsos_fisicos=True)
    return cadastro


def auditoria(item, principal, avulsos):
    cadastro = cadastro_contagem(item)
    if not avulsos_fisicos(cadastro) or principal is None:
        return {}
    total = Decimal(principal) * Decimal(cadastro['fator']) + Decimal(avulsos or 0)
    return {'base': cadastro['base'], 'unidades_por_embalagem': cadastro['fator'],
        'embalagens': str(principal), 'avulsos': str(avulsos or 0), 'total_unidades': format(total.normalize(), 'f')}


def exigir_representavel(rodada):
    fisico = auditoria(rodada.item, rodada.embalagens, rodada.avulsos)
    if fisico and rodada.contado_base is not None:
        if rodada.contado_base * Decimal(fisico['unidades_por_embalagem']) != Decimal(fisico['total_unidades']):
            raise ValueError('Contagem física exata preservada em UN. Ajuste automático indisponível: o saldo em embalagens não representa essas unidades sem arredondamento.')


class PrecisaoInsuficiente(ValueError):
    def __init__(self, contagem_fisica):
        self.contagem_fisica = contagem_fisica
        super().__init__(
            f'Contagem preservada: {contagem_fisica}. O saldo com três casas decimais '
            'não representa essa quantidade física exatamente. Nenhum ajuste será aplicado.')


def exigir_precisao_ajuste(rodada):
    """Compare by multiplication: never round a physical count to authorize a write."""
    cadastro = rodada.item.cadastro
    if not cadastro['fracionado'] and not avulsos_fisicos(cadastro):
        if rodada.avulsos in (None, Decimal(0)):
            return
        cadastro = cadastro_contagem(rodada.item)
    if not cadastro['fracionado'] and not avulsos_fisicos(cadastro):
        raise ValueError('Contagem com avulsos sem relação física confirmada.')
    try:
        fator = Decimal(str(cadastro.get('fator', '0')))
    except InvalidOperation:
        raise ValueError('Conversão congelada inválida. Revise a conferência.') from None
    if not fator.is_finite() or fator <= 0 or not cadastro.get('secundaria'):
        raise ValueError('Conversão congelada inválida. Revise a conferência.')
    if rodada.embalagens is None or rodada.avulsos is None or rodada.contado_base is None:
        raise ValueError('Contagem física incompleta. Revise a conferência.')
    fisico = rodada.embalagens * fator + rodada.avulsos
    if rodada.contado_base * fator != fisico:
        quantidade = format(fisico.normalize(), 'f').replace('.', ',')
        raise PrecisaoInsuficiente(f"{quantidade} {cadastro['secundaria']}")


@transaction.atomic
def confirmar(usuario, inventario_id, rodada_id, token, fator):
    from . import services, contagem_admin
    services.permitir(usuario, 'revisar_inventario')
    try:
        dados = signing.loads(token, salt=contagem_admin.SALT, max_age=43200)
    except (signing.BadSignature, TypeError, ValueError):
        raise ValueError('Formulário inválido ou expirado. Atualize a página.')
    if not isinstance(dados, dict) or (dados.get('usuario'),dados.get('inventario'),dados.get('rodada')) != (usuario.pk,inventario_id,rodada_id):
        raise PermissionDenied('Confirmação fora desta conferência ou usuário.')
    inventario = Inventario.objects.select_for_update().get(pk=inventario_id)
    services.exigir_nao_cancelada(inventario)
    rodada = RodadaContagem.objects.select_related('item').get(pk=rodada_id,item__inventario=inventario)
    item = ItemInventario.objects.select_for_update().get(pk=rodada.item_id)
    valor = decimal_entrada(fator)
    if valor is None or valor <= 1 or valor != valor.to_integral_value():
        raise ValueError('Informe quantas UN inteiras existem em uma embalagem: número inteiro maior que 1.')
    if item.cadastro['fracionado'] or item.cadastro['base'] not in BASES:
        raise ValueError('Este produto deve manter sua contagem atual.')
    atual = cadastro_contagem(item)
    if avulsos_fisicos(atual):
        if Decimal(atual['fator']) == valor:
            return item
        raise ValueError('Relação já confirmada nesta conferência. Não pode ser substituída por esta tela.')
    if (rodada.encerrada_em or dados.get('versao') != rodada.versao or dados.get('ciclo') != inventario.ciclo
            or inventario.finalizada_em or DecisaoAjuste.objects.filter(item=item).exists()):
        raise ValueError('Conferência ou contagem alterada/encerrada. Atualize a página.')
    from .selecao import itens_incluidos
    if not itens_incluidos(inventario).filter(pk=item.pk).exists() or not item.produto.ativo or item.produto.excluido or not cadastro_compativel(item.produto,item.cadastro):
        raise ValueError('Produto ou cadastro alterado. Revise a conferência.')
    services.evento(inventario, ACAO, usuario, rodada=rodada, item_id=item.pk,
        produto_id=item.produto_id, base=item.cadastro['base'], secundaria='UN',
        unidades_por_embalagem=str(valor), origem='confirmacao_responsavel',
        usuario_nome=usuario.get_username(), estoque_alterado=False, cadastro_comercial_alterado=False)
    services.atualizar_versao(inventario)
    return item
