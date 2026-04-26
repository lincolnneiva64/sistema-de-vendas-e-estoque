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


def categoria_canonica(valor):
    nome = normalizar_espacos(valor)
    if not nome:
        return ""

    mapeamento = {
        chave_categoria(origem): destino
        for origem, destino in MAPEAMENTO_CATEGORIAS.items()
    }
    finais = {
        chave_categoria(categoria): categoria
        for categoria in CATEGORIAS_FINAIS
    }
    chave = chave_categoria(nome)

    if chave in mapeamento:
        return mapeamento[chave]
    if chave in finais:
        return finais[chave]
    return nome


def obter_categoria_final(Categoria, nome):
    categoria = Categoria.objects.filter(nome=nome).first()

    if categoria:
        if not categoria.ativa:
            categoria.ativa = True
            categoria.save(update_fields=["ativa"])
        return categoria

    categoria = Categoria.objects.filter(nome__iexact=nome).first()

    if categoria:
        alterada = False
        if categoria.nome != nome:
            categoria.nome = nome
            alterada = True
        if not categoria.ativa:
            categoria.ativa = True
            alterada = True
        if alterada:
            categoria.save(update_fields=["nome", "ativa"])
        return categoria

    return Categoria.objects.create(nome=nome, ativa=True)


def consolidar_categorias(apps, schema_editor):
    Categoria = apps.get_model("estoque", "Categoria")
    Produto = apps.get_model("estoque", "Produto")

    categorias_finais = {
        nome: obter_categoria_final(Categoria, nome)
        for nome in CATEGORIAS_FINAIS
    }
    chaves_finais = {
        chave_categoria(nome)
        for nome in CATEGORIAS_FINAIS
    }

    for produto in Produto.objects.exclude(categoria__isnull=True).exclude(categoria=""):
        categoria_atual = normalizar_espacos(produto.categoria)
        categoria_nova = categoria_canonica(categoria_atual)

        if categoria_nova and categoria_nova != produto.categoria:
            produto.categoria = categoria_nova
            produto.save(update_fields=["categoria"])

    ids_finais = {categoria.id for categoria in categorias_finais.values()}

    for categoria in Categoria.objects.all():
        if categoria.id in ids_finais:
            continue

        categoria_nova = categoria_canonica(categoria.nome)
        if chave_categoria(categoria_nova) in chaves_finais:
            categoria.ativa = False
            categoria.save(update_fields=["ativa"])
        else:
            categoria.ativa = False
            categoria.save(update_fields=["ativa"])


class Migration(migrations.Migration):

    dependencies = [
        ("estoque", "0012_categoria"),
    ]

    operations = [
        migrations.RunPython(consolidar_categorias, migrations.RunPython.noop),
    ]
