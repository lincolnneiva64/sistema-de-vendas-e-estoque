import re
import unicodedata

WHITESPACE_RE = re.compile(r"\s+")

FINAL_PRODUCT_CATEGORIES = [
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
    "Higiene Pessoal",
    "Descartáveis",
    "Diversos",
]

CATEGORY_ALIASES = {
    "cerveja": "Cervejas",
    "cervejas": "Cervejas",
    "refrigerante": "Refrigerantes e Afins",
    "refrigerante,": "Refrigerantes e Afins",
    "refigerante": "Refrigerantes e Afins",
    "refigerante,": "Refrigerantes e Afins",
    "refrigerantes e afins": "Refrigerantes e Afins",
    "frios/embutidos": "Frios e Embutidos",
    "frios e embutidos": "Frios e Embutidos",
    "milho/derivados": "Milho e Derivados",
    "milho/dervados": "Milho e Derivados",
    "milho e derivados": "Milho e Derivados",
    "hortefrutegranjeiro": "Hortifruti",
    "hortifruti": "Hortifruti",
    "granjeiro": "Granjeiro",
    "estiva": "Estivas",
    "estivas": "Estivas",
    "higiene pessoal": "Higiene Pessoal",
    "higiene": "Higiene Pessoal",
    "descartaveis": "Descartáveis",
    "descartáveis": "Descartáveis",
    "congelados": "Diversos",
    "temperos e condimentos": "Mercearia",
}


def normalize_product_name(name: str) -> str:
    return WHITESPACE_RE.sub(" ", (name or "").strip())


def normalize_category_key(name: str) -> str:
    normalized = unicodedata.normalize("NFD", normalize_product_name(name))
    without_accents = "".join(
        char for char in normalized if unicodedata.category(char) != "Mn"
    )
    return without_accents.casefold()


def normalize_category_name(name: str) -> str:
    clean_name = normalize_product_name(name)
    if not clean_name:
        return ""

    key = normalize_category_key(clean_name)
    aliases = {
        normalize_category_key(alias): canonical
        for alias, canonical in CATEGORY_ALIASES.items()
    }
    finals = {
        normalize_category_key(category): category
        for category in FINAL_PRODUCT_CATEGORIES
    }

    if key in aliases:
        return aliases[key]
    if key in finals:
        return finals[key]

    return clean_name
