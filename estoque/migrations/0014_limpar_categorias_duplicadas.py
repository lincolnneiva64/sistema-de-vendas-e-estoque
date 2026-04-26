import unicodedata

from django.db import migrations


CATEGORIAS_FINAIS = [
    "Bebidas",
    "Cervejas",
    "Refrigerantes e Afins",
    "Bebidas Quentes",
    "Hortifruti",
    "Granjeiro",
    "Estivas",
    "Mercearia",
    "Frios e Embutidos",
    "Enlatados",
    "Massas",
    "Milho e Derivados",
    "Limpeza",
    "Higiene",
    "Descartáveis",
    "Diversos",
]

MAPEAMENTO_CATEGORIAS = {
    "Cerveja": "Cervejas",
    "Cervejas": "Cervejas",
    "Refrigerante": "Refrigerantes e Afins",
    "Refrigerante,": "Refrigerantes e Afins",
    "Refrigerantes E Afins": "Refrigerantes e Afins",
    "Refrigerantes e Afins": "Refrigerantes e Afins",
    "Frios/Embutidos": "Frios e Embutidos",
    "Frios E Embutidos": "Frios e Embutidos",
    "Frios e Embutidos": "Frios e Embutidos",
    "Milho/Derivados": "Milho e Derivados",
    "Milho/Dervados": "Milho e Derivados",
    "Milho e Derivados": "Milho e Derivados",
    "Hortefrutegranjeiro": "Hortifruti",
    "Hortifruti": "Hortifruti",
    "Granjeiro": "Granjeiro",
    "Estiva": "Estivas",
    "Estivas": "Estivas",
    "Higiene Pessoal": "Higiene",
    "Higiene": "Higiene",
    "Descartaveis": "Descartáveis",
    "Descartáveis": "Descartáveis",
    "Congelados": "Diversos",
    "Temperos e Condimentos": "Mercearia",
}


def normalizar_espacos(valor):
    return " ".join(str(valor or "").strip().split())


def chave_categoria(valor):
    normalizado = unicodedata.normalize("NFD", normalizar_espacos(valor))
    sem_acentos = "".join(
        caractere
        for caractere in normalizado
        if unicodedata.category(caractere) != "Mn"
    )
    return sem_acentos.casefold()


def limpar_categorias_duplicadas(apps, schema_editor):
    Categoria = apps.get_model("estoque", "Categoria")
    Produto = apps.get_model("estoque", "Produto")

    finais_por_chave = {
        chave_categoria(categoria): categoria
        for categoria in CATEGORIAS_FINAIS
    }
    mapeamento_por_chave = {
        chave_categoria(origem): destino
        for origem, destino in MAPEAMENTO_CATEGORIAS.items()
    }

    for nome_final in CATEGORIAS_FINAIS:
        categoria_final = Categoria.objects.filter(nome=nome_final).first()
        if not categoria_final:
            categoria_final = Categoria.objects.create(nome=nome_final)

        if not categoria_final.ativa:
            categoria_final.ativa = True
            categoria_final.save(update_fields=["ativa"])

    for produto in Produto.objects.exclude(categoria__isnull=True).exclude(categoria=""):
        categoria_atual = normalizar_espacos(produto.categoria)
        chave_atual = chave_categoria(categoria_atual)
        categoria_final = mapeamento_por_chave.get(chave_atual) or finais_por_chave.get(chave_atual)

        if categoria_final and categoria_atual != categoria_final:
            Produto.objects.filter(pk=produto.pk).update(categoria=categoria_final)

    chaves_finais = set(finais_por_chave)
    ids_finais = set()

    for nome_final in CATEGORIAS_FINAIS:
        categoria_final = Categoria.objects.filter(nome=nome_final).first()
        if categoria_final:
            ids_finais.add(categoria_final.id)

    categorias_para_remover = []
    for categoria in Categoria.objects.all():
        if categoria.id in ids_finais:
            continue
        if chave_categoria(categoria.nome) not in chaves_finais:
            categorias_para_remover.append(categoria.id)
            continue
        categorias_para_remover.append(categoria.id)

    if categorias_para_remover:
        Categoria.objects.filter(id__in=categorias_para_remover).delete()


class Migration(migrations.Migration):

    dependencies = [
        ("estoque", "0013_consolidar_categorias"),
    ]

    operations = [
        migrations.RunPython(limpar_categorias_duplicadas, migrations.RunPython.noop),
    ]
