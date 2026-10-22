"""Administrative OK on the current round; never write stock or employee credentials."""
from django.core import signing
from django.core.exceptions import PermissionDenied
from django.db import transaction
from django.utils import timezone

from estoque.models import Produto
from . import services
from .conversao import converter, snapshot
from .models import (Inventario, ItemInventario, RodadaContagem, ConfirmacaoContagem,
                     EventoInventario, DecisaoAjuste, AplicacaoAjuste, PropostaAjuste)
from .selecao import itens_incluidos

SALT = 'inventario-contagem-administrativa-v1'
ACAO = 'contagem_admin_confirmada'


def autorizacao(usuario, inventario, rodada):
    return signing.dumps(dict(usuario=usuario.pk, inventario=inventario.pk,
        rodada=rodada.pk, versao=rodada.versao, ciclo=inventario.ciclo), salt=SALT)


def confirmado(rodada):
    return bool(rodada.ok_em and EventoInventario.objects.filter(
        rodada=rodada, acao=ACAO, dados__versao_saida=rodada.versao).exists())


def valores_auditoria(rodada):
    campos = ('embalagens', 'avulsos', 'contado', 'contado_base', 'esperado', 'sequencia_ok', 'ok_em', 'versao')
    return {campo: str(getattr(rodada, campo)) if getattr(rodada, campo) is not None else None for campo in campos}


@transaction.atomic
def confirmar(usuario, inventario_id, rodada_id, token, embalagens, avulsos):
    services.permitir(usuario, 'revisar_inventario')
    try:
        dados = signing.loads(token, salt=SALT, max_age=43200)
    except (signing.BadSignature, TypeError, ValueError):
        raise ValueError('Formulário inválido ou expirado. Atualize a página.')
    if (not isinstance(dados, dict) or dados.get('usuario') != usuario.pk
            or dados.get('inventario') != inventario_id or dados.get('rodada') != rodada_id
            or type(dados.get('versao')) is not int):
        raise PermissionDenied('Formulário fora desta conferência ou deste usuário.')
    inventario = Inventario.objects.select_for_update().get(pk=inventario_id)
    services.exigir_nao_cancelada(inventario)
    if dados.get('ciclo') != inventario.ciclo:
        raise ValueError('O ciclo da conferência mudou. Atualize a página.')
    referencia = RodadaContagem.objects.get(pk=rodada_id, item__inventario=inventario)
    item = ItemInventario.objects.select_for_update().get(pk=referencia.item_id)
    rodada = RodadaContagem.objects.select_for_update().get(pk=rodada_id)
    if (rodada.encerrada_em or item.rodadas.filter(numero__gt=rodada.numero).exists()
            or not itens_incluidos(inventario).filter(pk=item.pk).exists()):
        raise ValueError('Esta rodada não está disponível. Atualize a página.')
    if (DecisaoAjuste.objects.filter(item=item).exists()
            or AplicacaoAjuste.objects.filter(proposta__rodada__item=item).exists()
            or PropostaAjuste.objects.filter(rodada__item=item, estado='aplicado').exists()):
        raise ValueError('Produto com ajuste de estoque aplicado. A correção exige um processo separado.')
    valores = converter(item.cadastro, embalagens, avulsos)
    anterior = rodada.confirmacoes.filter(versao_entrada=dados['versao']).first()
    if anterior:
        # Retry only this administrator's accepted request, never somebody else's OK.
        if (anterior.acesso_id is None and (anterior.embalagens, anterior.avulsos) == valores[:2]
                and EventoInventario.objects.filter(inventario=inventario, rodada=rodada,
                    usuario=usuario, acao=ACAO, dados__confirmacao_id=anterior.pk).exists()):
            return rodada
        raise ValueError('Esta versão já foi confirmada com outra contagem. Atualize a página.')
    if rodada.versao != dados['versao']:
        raise ValueError('A contagem mudou em outra aba ou acesso. Atualize a página.')
    services.verificar_observador()
    produto = Produto.objects.select_for_update().get(pk=item.produto_id)
    if not produto.ativo or produto.excluido or produto.quantidade is None or snapshot(produto) != item.cadastro:
        raise ValueError('Produto ou cadastro alterado. Revise antes de confirmar a contagem.')
    antes = valores_auditoria(rodada)
    ultimo = rodada.confirmacoes.order_by('-versao_entrada').first()
    rodada.embalagens, rodada.avulsos, rodada.contado, rodada.contado_base = valores
    rodada.esperado = produto.quantidade
    rodada.sequencia_ok = services.sequencia(produto.pk)
    rodada.ok_em = timezone.now()
    confirmacao = ConfirmacaoContagem.objects.create(rodada=rodada, versao_entrada=rodada.versao,
        embalagens=valores[0], avulsos=valores[1], contado=valores[2], contado_base=valores[3],
        esperado=rodada.esperado, sequencia_ok=rodada.sequencia_ok, ok_em=rodada.ok_em,
        acesso=None, responsavel_nome=usuario.get_full_name() or usuario.get_username())
    rodada.revisao_sequencia = rodada.revisao_por = rodada.revisada_em = None
    rodada.revisao_motivo = ''
    rodada.versao += 1
    rodada.save()
    services.atualizar_versao(inventario)
    services.evento(inventario, ACAO, usuario, rodada=rodada, confirmacao_id=confirmacao.pk,
        confirmacao_anterior_id=ultimo.pk if ultimo else None, antes=antes, depois=valores_auditoria(rodada),
        versao_saida=rodada.versao, produto_id=produto.pk, produto_nome=item.cadastro['nome'],
        unidade=item.cadastro['base'], secundaria=item.cadastro['secundaria'],
        fator=item.cadastro['fator'], conferencia_id=inventario.pk, rodada_numero=rodada.numero,
        ciclo=inventario.ciclo, usuario_nome=usuario.get_username(), estoque_alterado=False)
    return rodada
