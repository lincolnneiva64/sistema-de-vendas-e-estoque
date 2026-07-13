from dataclasses import dataclass

from estoque.models import FornecedorContatoTelefone


@dataclass(frozen=True)
class TelefoneContatoLegado:
    contato: object
    numero: str
    tipo: str = FornecedorContatoTelefone.TIPO_CELULAR
    whatsapp: bool = True
    principal: bool = True
    ativo: bool = True
    ordem: int = 1
    pk: int | None = None
    id: int | None = None


def _normalizar_numero(valor):
    return "".join(caractere for caractere in str(valor or "") if caractere.isdigit())


def _telefones_queryset(contato):
    return contato.telefones.filter(ativo=True).order_by("-principal", "ordem", "id")


def _telefone_legado(contato):
    numero = _normalizar_numero(
        getattr(contato, "telefone_whatsapp_normalizado", None)
        or getattr(contato, "telefone_whatsapp", None)
    )
    if not numero:
        return None
    return TelefoneContatoLegado(contato=contato, numero=numero)


def telefones_ativos_contato(contato):
    telefones = list(_telefones_queryset(contato))
    if telefones:
        return telefones

    legado = _telefone_legado(contato)
    return [legado] if legado else []


def telefone_principal_contato(contato):
    telefones = telefones_ativos_contato(contato)
    for telefone in telefones:
        if telefone.principal:
            return telefone
    return telefones[0] if telefones else None


def telefones_whatsapp_contato(contato):
    telefones = list(_telefones_queryset(contato))
    if telefones:
        return [telefone for telefone in telefones if telefone.whatsapp]

    legado = _telefone_legado(contato)
    return [legado] if legado else []
