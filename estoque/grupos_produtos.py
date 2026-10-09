"""Operações de vínculo; não modificam os campos comerciais dos produtos."""
import re

from django.core.exceptions import ValidationError
from django.db import transaction
from django.db.models.signals import post_delete
from django.dispatch import receiver

from .models import GrupoProdutoVinculado, MembroGrupoProduto, Produto
from .services.precos_vinculados import serializar_grupos


def sugerir_nome(nomes):
    palavras = [re.findall(r"\S+", nome.strip()) for nome in nomes]
    if not palavras:
        return "Grupo de produtos"
    comuns = set(p.casefold() for p in palavras[0])
    for tokens in palavras[1:]:
        comuns.intersection_update(p.casefold() for p in tokens)
    vistos = set()
    resultado = []
    for palavra in palavras[0]:
        chave = palavra.casefold()
        if chave in comuns and chave not in vistos:
            resultado.append(palavra)
            vistos.add(chave)
    return (" ".join(resultado) or "Grupo de produtos")[:120]


def validar_nome(nome):
    nome = " ".join(str(nome or "").split())
    if not nome or len(nome) > 120:
        raise ValidationError("Informe um nome de grupo com até 120 caracteres.")
    return nome


@transaction.atomic
@serializar_grupos
def criar_grupo(produto_ids, nome):
    nome = validar_nome(nome)
    ids = set(produto_ids)
    if len(ids) < 2:
        raise ValidationError("Selecione pelo menos dois produtos para criar um grupo.")
    produtos = list(Produto.objects.select_for_update().filter(pk__in=ids, excluido=False).order_by("pk"))
    if len(produtos) != len(ids):
        raise ValidationError("Um dos produtos selecionados não está disponível.")
    if MembroGrupoProduto.objects.filter(produto_id__in=ids).exists():
        raise ValidationError("Um produto já pertence a um grupo. Remova o vínculo atual antes de continuar.")
    grupo = GrupoProdutoVinculado.objects.create(nome=nome)
    MembroGrupoProduto.objects.bulk_create([MembroGrupoProduto(grupo=grupo, produto=p) for p in produtos])
    return grupo


@transaction.atomic
@serializar_grupos
def adicionar_membros(grupo_id, produto_ids):
    ids = set(produto_ids)
    if not ids:
        raise ValidationError("Selecione pelo menos um produto para adicionar.")
    produtos = list(Produto.objects.select_for_update().filter(
        pk__in=ids, excluido=False, ativo=True,
    ).order_by("pk"))
    if len(produtos) != len(ids):
        raise ValidationError("Selecione apenas produtos ativos disponíveis.")
    grupo = GrupoProdutoVinculado.objects.select_for_update().get(pk=grupo_id)
    if grupo.precos_regularizados:
        raise ValidationError('Grupo ativo: entrada exige previa e confirmacao de precos. Integracao da tela ainda pendente.')
    vinculos = MembroGrupoProduto.objects.filter(produto_id__in=ids)
    if vinculos.exclude(grupo=grupo).exists():
        raise ValidationError("Um produto pertence a outro grupo. Remova o vínculo atual antes de continuar.")
    existentes = set(vinculos.values_list("produto_id", flat=True))
    novos = [MembroGrupoProduto(grupo=grupo, produto=p) for p in produtos if p.pk not in existentes]
    MembroGrupoProduto.objects.bulk_create(novos)
    grupo.save(update_fields=["atualizado_em"])
    return len(novos)


@transaction.atomic
@serializar_grupos
def excluir_grupo(grupo_id):
    grupo = GrupoProdutoVinculado.objects.select_for_update().get(pk=grupo_id)
    grupo.delete()


@transaction.atomic
@serializar_grupos
def renomear_grupo(grupo_id, nome):
    grupo = GrupoProdutoVinculado.objects.select_for_update().get(pk=grupo_id)
    grupo.nome = validar_nome(nome)
    grupo.save(update_fields=["nome", "atualizado_em"])
    return grupo


@transaction.atomic
@serializar_grupos
def remover_membro(grupo_id, produto_id):
    grupo = GrupoProdutoVinculado.objects.select_for_update().get(pk=grupo_id)
    membro = grupo.membros.get(produto_id=produto_id)
    membro.delete()
    if GrupoProdutoVinculado.objects.filter(pk=grupo_id).exists():
        grupo.versao_precos+=1
        grupo.ultima_alteracao_precos=None
        grupo.save(update_fields=["atualizado_em","versao_precos","ultima_alteracao_precos"])


@receiver(post_delete, sender=MembroGrupoProduto)
def excluir_grupo_vazio(sender, instance, using, **kwargs):
    # Também cobre exclusão definitiva de um produto e exclusões em lote.
    GrupoProdutoVinculado.objects.using(using).filter(pk=instance.grupo_id, membros__isnull=True).delete()
