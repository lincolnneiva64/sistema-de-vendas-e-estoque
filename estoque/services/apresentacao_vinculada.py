"""Explicit, audited presentation and optional sale-price commands."""
import copy
from decimal import Decimal

from django import forms
from django.core import signing
from django.core.exceptions import ValidationError
from django.db import models
from django.db.models import Q
from django.utils import timezone

from estoque.models import Produto, Unidade, GrupoProdutoVinculado, AlteracaoPrecoVinculado
from .precos_vinculados import (serializar_grupos, exigir_operador_precos,
    bloquear_produtos_precos, diagnosticar_grupo, validar_precos_individuais,
    versao_cadastro_produto, _unidade, PRECOS_VINCULADOS, custo_fracionado_produto, _gravar)

SALT = 'estoque.apresentacao_grupo.v1'
CAMPOS = ('unidade_compra', 'unidade_venda_1', 'vende_fracionado',
          'unidade_venda_2', 'fator_conversao')


class ApresentacaoForm(forms.Form):
    unidade_compra = forms.ChoiceField(label='Unidade de compra')
    vende_fracionado = forms.TypedChoiceField(label='Venda fracionada',
        choices=(('True', 'Sim'), ('False', 'Não')), coerce=lambda v: v == 'True')
    unidade_venda_2 = forms.ChoiceField(label='Unidade fracionada', required=False)
    fator_conversao = forms.DecimalField(label='Fator de conversão', required=False,
        max_digits=10, decimal_places=2, min_value=Decimal('0'))

    def __init__(self, dados, produtos):
        super().__init__(dados)
        unidades = set(Unidade.objects.filter(ativa=True).values_list('sigla', flat=True))
        unidades.update(getattr(p, c) for p in produtos for c in
            ('unidade_compra', 'unidade_venda_2') if getattr(p, c))
        opcoes = [(u, u) for u in sorted(unidades)]
        self.fields['unidade_compra'].choices = opcoes
        self.fields['unidade_venda_2'].choices = [('', 'Selecione'), *opcoes]
        if dados.get('incluir_precos') == '1':
            for campo, rotulo in zip(PRECOS_VINCULADOS,
                    ('Venda à vista', 'Venda a prazo', 'Venda fracionada à vista', 'Venda fracionada a prazo')):
                self.fields[campo] = forms.DecimalField(label=rotulo, max_digits=10,
                    decimal_places=2, min_value=Decimal('0'))

    def clean(self):
        dados = super().clean()
        if dados.get('vende_fracionado'):
            if not dados.get('unidade_venda_2'):
                self.add_error('unidade_venda_2', 'Selecione uma unidade cadastrada.')
            if not dados.get('fator_conversao'):
                self.add_error('fator_conversao', 'Informe um fator maior que zero.')
        return dados


def _estado(p):
    return {c: str(getattr(p, c)) if c == 'fator_conversao' else getattr(p, c) for c in CAMPOS}


def _riscos_historicos(produtos, propostas):
    """Batch by relation, not by member; no changes to movements or snapshots.

    Primary units anchor balances/history. Existing fractional conversions may
    also be used by old rows without snapshots. First activation keeps primary
    quantities unchanged, but must not reinterpret old non-primary/unknown units.
    """
    riscos = {}
    bases = set()
    conversoes = set()
    ativacoes = set()
    for p in produtos:
        novo = propostas[p.pk]
        base_mudou = (_unidade(p.unidade_compra) != _unidade(novo.unidade_compra)
            or _unidade(p.unidade_venda_1 or p.unidade_compra) !=
               _unidade(novo.unidade_venda_1 or novo.unidade_compra))
        conversao_mudou = (p.vende_fracionado and
            (p.vende_fracionado != novo.vende_fracionado or
             _unidade(p.unidade_venda_2) != _unidade(novo.unidade_venda_2) or
             p.fator_conversao != novo.fator_conversao))
        if base_mudou:
            bases.add(p.pk)
        if conversao_mudou:
            conversoes.add(p.pk)
        if not p.vende_fracionado and novo.vende_fracionado:
            ativacoes.add(p.pk)
        if (base_mudou or conversao_mudou) and (p.quantidade or p.estoque_minimo):
            riscos[p.pk] = 'A mudança reinterpretaria o saldo ou o mínimo existente. Nenhum saldo será convertido automaticamente.'
    ids = bases | conversoes | ativacoes
    if not ids:
        return riscos
    for rel in Produto._meta.related_objects:
        if not (rel.one_to_many or rel.one_to_one):
            continue
        campos = {f.name for f in rel.related_model._meta.concrete_fields}
        # Metadata (membership, suppliers, sequence counters) carries no quantity.
        if not campos.intersection({'quantidade', 'quantidade_snapshot', 'antes',
                                    'estoque_antes', 'cadastro', 'estoque_atual'}):
            continue
        fk = rel.field.attname
        consulta = rel.related_model._base_manager.filter(**{fk+'__in': ids})
        filtro = Q(**{fk+'__in': bases | conversoes})
        unidade = next((c for c in ('unidade', 'unidade_snapshot') if c in campos), None)
        if unidade:
            for p in produtos:
                if p.pk in ativacoes:
                    # Empty legacy units cannot prove a primary-only operation.
                    filtro |= Q(**{fk: p.pk}) & ~Q(**{unidade+'__iexact': _unidade(p.unidade_compra)})
        for pk in consulta.filter(filtro).values_list(fk, flat=True).distinct():
            riscos.setdefault(pk, 'Há histórico ou operação em andamento ('+
                str(rel.related_model._meta.verbose_name)+'). A mudança pode reinterpretar suas unidades; revise essa operação antes de alterar a apresentação.')
    return riscos


def _preparar(grupo_id, fonte_id, dados):
    grupo = GrupoProdutoVinculado.objects.select_for_update().get(pk=grupo_id)
    ids = list(grupo.produtos.values_list('pk', flat=True))
    bloqueados = bloquear_produtos_precos(ids)
    produtos = [bloqueados[pk] for pk in sorted(ids)]
    fonte = next((p for p in produtos if p.pk == fonte_id), None)
    if fonte is None:
        raise ValidationError('O produto não pertence mais a este grupo. Nada foi salvo. Reabra o cadastro.')
    if dados.get('versao_cadastro_precos') != versao_cadastro_produto(fonte):
        raise ValidationError('O cadastro mudou desde a abertura. Nada foi salvo. Reabra o cadastro antes de confirmar.')
    form = ApresentacaoForm(dados, produtos)
    if not form.is_valid():
        raise ValidationError([f'{fonte.nome} — {form.fields[c].label}: {e}'
            for c, erros in form.errors.items() for e in erros])
    novos = {c: form.cleaned_data[c] for c in
        ('unidade_compra', 'vende_fracionado', 'unidade_venda_2', 'fator_conversao')}
    novos['unidade_venda_1'] = novos['unidade_compra']
    incluir_precos = dados.get('incluir_precos') == '1'
    propostas = {p.pk: copy.copy(p) for p in produtos}
    for p in produtos:
        candidato = propostas[p.pk]
        for c, valor in novos.items():
            # Turning off fractional sales does not erase stored conversion data.
            if c in ('fator_conversao', 'unidade_venda_2') and not novos['vende_fracionado']:
                continue
            setattr(candidato, c, valor)
        if incluir_precos:
            for c in PRECOS_VINCULADOS:
                setattr(candidato, c, form.cleaned_data[c])
            candidato.preco_venda = candidato.preco_vista
            candidato.precos_canonicos_adotados = True
    riscos = _riscos_historicos(produtos, propostas)
    bloqueios = [{'produto_id': p.pk, 'motivo': f'{p.nome}: {riscos[p.pk]}'}
        for p in produtos if p.pk in riscos]
    diagnostico = diagnosticar_grupo(list(propostas.values()), validar_precos=False)
    bloqueios.extend(diagnostico['bloqueios'])
    for p in produtos:
        try:
            validar_precos_individuais(propostas[p.pk])
        except ValidationError as exc:
            bloqueios.append({'produto_id': p.pk, 'motivo': p.nome+': '+' '.join(exc.messages)})
    observada = {'grupo': grupo.pk, 'versao': grupo.versao_precos, 'fonte': fonte_id,
        'produtos': {str(p.pk): versao_cadastro_produto(p) for p in produtos},
        'propostas': {str(p.pk): _estado(propostas[p.pk]) for p in produtos},
        'incluir_precos': incluir_precos,
        'precos': {str(p.pk): {c:str(getattr(propostas[p.pk],c)) for c in PRECOS_VINCULADOS} for p in produtos}}
    previa = {'produtos': [{'id': p.pk, 'nome': p.nome, 'anteriores': _estado(p),
        'novos': _estado(propostas[p.pk]),
        'precos_anteriores': {c:str(getattr(p,c)) for c in PRECOS_VINCULADOS},
        'precos_novos': {c:str(getattr(propostas[p.pk],c)) for c in PRECOS_VINCULADOS},
        'custos': {'principal': str(p.preco_compra),
                   'fracionado': str(custo_fracionado_produto(propostas[p.pk]))}}
        for p in produtos], 'bloqueios': bloqueios, 'incluir_precos': incluir_precos}
    return grupo, produtos, propostas, observada, previa


@serializar_grupos
def previa_apresentacao(grupo_id, fonte_id, dados, *, operador):
    exigir_operador_precos(operador)
    _, _, _, observada, previa = _preparar(grupo_id, fonte_id, dados)
    observada['operador'] = operador.pk
    previa['assinatura'] = signing.dumps(observada, salt=SALT, compress=True)
    return previa


@serializar_grupos
def aplicar_apresentacao(grupo_id, fonte_id, dados, *, operador):
    exigir_operador_precos(operador)
    if dados.get('confirmar') != '1':
        raise ValidationError('Confirme a prévia de todos os integrantes. Nada foi salvo.')
    try:
        assinada = signing.loads(dados.get('assinatura', ''), salt=SALT, max_age=600)
    except signing.BadSignature as exc:
        raise ValidationError('Prévia inválida ou expirada. Nada foi salvo. Confira novamente.') from exc
    grupo, produtos, propostas, observada, previa = _preparar(grupo_id, fonte_id, dados)
    observada['operador'] = operador.pk
    if assinada != observada:
        raise ValidationError('O grupo, os cadastros ou a proposta mudaram. Nada foi salvo. Confira uma nova prévia.')
    if previa['bloqueios']:
        raise ValidationError([b['motivo'] for b in previa['bloqueios']])
    alteracoes = {str(p.pk): {'anteriores': _estado(p), 'novos': _estado(propostas[p.pk])}
        for p in produtos if _estado(p) != _estado(propostas[p.pk])}
    if observada['incluir_precos']:
        # Reuse price provenance/adoption/audit. Its writer merges each row's
        # confirmed presentation with its final prices in the SAME transaction.
        return _gravar(grupo, produtos, propostas, operador, 'apresentacao_precos_grupo',
            apresentacao_confirmada=True,
            evidencia_extra={'apresentacao': alteracoes, 'selecionados': [p.pk for p in produtos],
                             'campos': list(PRECOS_VINCULADOS), 'formato': 2})
    if not alteracoes:
        return
    for p in produtos:
        if str(p.pk) not in alteracoes:
            continue
        # Deliberately bypass the ordinary write guard ONLY after group-wide
        # validation under the existing catalog/group/product locks. No save()
        # hooks, financial/stock writes or price propagation are invoked.
        models.QuerySet.update(Produto.objects.filter(pk=p.pk),
            **{c: getattr(propostas[p.pk], c) for c in CAMPOS}, atualizado_em=timezone.now())
    versao = grupo.versao_precos
    grupo.versao_precos += 1
    grupo.save(update_fields=['versao_precos', 'atualizado_em'])
    AlteracaoPrecoVinculado.objects.create(grupo_id_snapshot=grupo.pk,
        grupo_nome_snapshot=grupo.nome, operador=operador, origem='apresentacao_grupo',
        versao_antes=versao, versao_depois=grupo.versao_precos,
        evento_anterior=grupo.ultima_alteracao_precos,
        evidencia={'apresentacao': alteracoes, 'integrantes': [p.pk for p in produtos],
                   'alteracoes': {}, 'operador_snapshot': {'id': operador.pk, 'nome': operador.get_username()}})
