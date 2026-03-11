import re

WHITESPACE_RE = re.compile(r"\s+")

def normalize_product_name(name: str) -> str:
    """Normaliza nomes de produtos removendo espaços extras."""
    return WHITESPACE_RE.sub(" ", (name or "").strip())