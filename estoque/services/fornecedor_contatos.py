from dataclasses import dataclass

from django.core.exceptions import ValidationError
from django.db import transaction

from estoque.models import FornecedorContatoTelefone

MAX_TELEFONES_POR_CONTATO = 3


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


def _formatar_numero(valor):
    digitos = _normalizar_numero(valor)
    if len(digitos) == 11:
        return f"({digitos[:2]}) {digitos[2:7]}-{digitos[7:]}"
    if len(digitos) == 10:
        return f"({digitos[:2]}) {digitos[2:6]}-{digitos[6:]}"
    return valor or ""


def _telefone_prefixo(contato_indice, telefone_indice):
    return f"contatos-{contato_indice}-telefones-{telefone_indice}"


def _post_tem_campos_telefone(post_data, contato_indice):
    prefixo = f"contatos-{contato_indice}-telefones-"
    return any(str(chave).startswith(prefixo) for chave in post_data.keys())


def _valor_bool(post_data, nome):
    return str(post_data.get(nome) or "").lower() in {"1", "true", "on", "yes"}


def _linha_vazia(contato_indice, telefone_indice, visivel=False):
    prefixo = _telefone_prefixo(contato_indice, telefone_indice)
    return {
        "index": telefone_indice,
        "prefixo": prefixo,
        "id_name": f"{prefixo}-id",
        "id": "",
        "numero_name": f"{prefixo}-numero",
        "numero": "",
        "tipo_name": f"{prefixo}-tipo",
        "tipo": FornecedorContatoTelefone.TIPO_CELULAR,
        "whatsapp_name": f"{prefixo}-whatsapp",
        "whatsapp": True,
        "principal_name": f"{prefixo}-principal",
        "principal": False,
        "ativo_name": f"{prefixo}-ativo",
        "ativo": True,
        "delete_name": f"{prefixo}-DELETE",
        "delete": False,
        "visivel": visivel,
        "erros": [],
    }


def _linha_de_telefone(contato_indice, telefone_indice, telefone):
    linha = _linha_vazia(contato_indice, telefone_indice, visivel=True)
    linha.update({
        "id": telefone.pk or "",
        "numero": _formatar_numero(telefone.numero),
        "tipo": telefone.tipo,
        "whatsapp": telefone.whatsapp,
        "principal": telefone.principal,
        "ativo": telefone.ativo,
    })
    return linha


def _linha_do_post(post_data, contato_indice, telefone_indice):
    linha = _linha_vazia(contato_indice, telefone_indice)
    prefixo = linha["prefixo"]
    linha.update({
        "id": (post_data.get(f"{prefixo}-id") or "").strip(),
        "numero": post_data.get(f"{prefixo}-numero") or "",
        "tipo": post_data.get(f"{prefixo}-tipo") or FornecedorContatoTelefone.TIPO_CELULAR,
        "whatsapp": _valor_bool(post_data, f"{prefixo}-whatsapp"),
        "principal": _valor_bool(post_data, f"{prefixo}-principal"),
        "ativo": _valor_bool(post_data, f"{prefixo}-ativo"),
        "delete": _valor_bool(post_data, f"{prefixo}-DELETE"),
    })
    linha["visivel"] = bool(
        linha["id"]
        or linha["numero"]
        or linha["principal"]
        or linha["delete"]
        or telefone_indice == 0
    )
    return linha


def _linhas_iniciais(contato_form, contato_indice):
    contato = contato_form.instance
    telefones = []
    if getattr(contato, "pk", None):
        telefones = list(contato.telefones.filter(ativo=True).order_by("-principal", "ordem", "id"))
        if not telefones:
            legado = _telefone_legado(contato)
            if legado:
                telefones = [legado]

    linhas = [
        _linha_de_telefone(contato_indice, indice, telefone)
        for indice, telefone in enumerate(telefones[:MAX_TELEFONES_POR_CONTATO])
    ]
    while len(linhas) < MAX_TELEFONES_POR_CONTATO:
        linhas.append(_linha_vazia(contato_indice, len(linhas), visivel=not linhas))
    return linhas


def preparar_telefones_contatos(contatos_formset, post_data=None):
    for indice, contato_form in enumerate(contatos_formset.forms):
        if post_data is not None and _post_tem_campos_telefone(post_data, indice):
            linhas = [_linha_do_post(post_data, indice, telefone_indice) for telefone_indice in range(MAX_TELEFONES_POR_CONTATO)]
        else:
            linhas = _linhas_iniciais(contato_form, indice)

        erros = getattr(contato_form, "telefones_erros", {})
        for linha in linhas:
            linha["erros"] = erros.get(linha["index"], [])
            if linha["erros"]:
                linha["visivel"] = True

        contato_form.telefones_linhas = linhas
        contato_form.telefones_tipos = FornecedorContatoTelefone.TIPO_CHOICES
        contato_form.telefones_tem_erro = any(linha["erros"] for linha in linhas)


def contato_tem_telefone_no_post(post_data, contato_indice):
    for telefone_indice in range(MAX_TELEFONES_POR_CONTATO):
        prefixo = _telefone_prefixo(contato_indice, telefone_indice)
        if _normalizar_numero(post_data.get(f"{prefixo}-numero")) and not _valor_bool(post_data, f"{prefixo}-DELETE"):
            return True
    return False


def _indices_telefone_no_post(post_data, contato_indice):
    prefixo = f"contatos-{contato_indice}-telefones-"
    indices = set()
    for chave in post_data.keys():
        if not str(chave).startswith(prefixo):
            continue
        restante = str(chave)[len(prefixo):]
        indice = restante.split("-", 1)[0]
        if indice.isdigit():
            indices.add(int(indice))
    return sorted(indices)


def _adicionar_erro_telefone(contato_form, telefone_indice, mensagem):
    if not hasattr(contato_form, "telefones_erros"):
        contato_form.telefones_erros = {}
    contato_form.telefones_erros.setdefault(telefone_indice, []).append(mensagem)
    contato_form.add_error(None, mensagem)


def _mensagens_validacao(erro):
    if hasattr(erro, "message_dict"):
        mensagens = []
        for erros_campo in erro.message_dict.values():
            mensagens.extend(erros_campo)
        return mensagens
    if hasattr(erro, "messages"):
        return erro.messages
    return [str(erro)]


def validar_telefones_contatos(post_data, contatos_formset):
    valido = True
    for contato_indice, contato_form in enumerate(contatos_formset.forms):
        if not _post_tem_campos_telefone(post_data, contato_indice):
            continue

        linhas_ativas = []
        numeros_ativos = {}
        principais = []
        nome_contato = (post_data.get(f"contatos-{contato_indice}-nome") or "").strip()

        indices_enviados = _indices_telefone_no_post(post_data, contato_indice)
        for telefone_indice in indices_enviados:
            prefixo = _telefone_prefixo(contato_indice, telefone_indice)
            numero = _normalizar_numero(post_data.get(f"{prefixo}-numero"))
            delete = _valor_bool(post_data, f"{prefixo}-DELETE")
            ativo = _valor_bool(post_data, f"{prefixo}-ativo")
            principal = _valor_bool(post_data, f"{prefixo}-principal")
            tipo = post_data.get(f"{prefixo}-tipo") or FornecedorContatoTelefone.TIPO_CELULAR

            if delete:
                continue
            if telefone_indice >= MAX_TELEFONES_POR_CONTATO and ativo and numero:
                contato_form.add_error(None, "Cada contato pode ter no maximo 3 telefones ativos.")
                valido = False
                continue
            if not numero and not principal:
                continue
            if numero and not nome_contato:
                contato_form.add_error("nome", "Informe o nome do responsavel para cadastrar telefones.")
                _adicionar_erro_telefone(contato_form, telefone_indice, "Informe o nome do responsavel para este telefone.")
                valido = False
            if numero and len(numero) not in (10, 11):
                _adicionar_erro_telefone(contato_form, telefone_indice, "Informe um telefone com 10 ou 11 digitos.")
                valido = False
            if tipo not in dict(FornecedorContatoTelefone.TIPO_CHOICES):
                _adicionar_erro_telefone(contato_form, telefone_indice, "Escolha um tipo de telefone valido.")
                valido = False
            if principal and not ativo:
                _adicionar_erro_telefone(contato_form, telefone_indice, "Telefone principal precisa estar ativo.")
                valido = False
            if ativo and numero:
                linhas_ativas.append(telefone_indice)
                if numero in numeros_ativos:
                    _adicionar_erro_telefone(contato_form, telefone_indice, "Este telefone ja esta cadastrado para este contato.")
                    _adicionar_erro_telefone(contato_form, numeros_ativos[numero], "Este telefone ja esta cadastrado para este contato.")
                    valido = False
                numeros_ativos[numero] = telefone_indice
                if principal:
                    principais.append(telefone_indice)

        if len(linhas_ativas) > MAX_TELEFONES_POR_CONTATO:
            contato_form.add_error(None, "Cada contato pode ter no maximo 3 telefones ativos.")
            valido = False
        if len(principais) > 1:
            for telefone_indice in principais:
                _adicionar_erro_telefone(contato_form, telefone_indice, "Marque apenas um telefone principal por contato.")
            valido = False

    preparar_telefones_contatos(contatos_formset, post_data)
    return valido


def _linhas_salvamento(post_data, contato_indice):
    linhas = []
    for telefone_indice in range(MAX_TELEFONES_POR_CONTATO):
        linha = _linha_do_post(post_data, contato_indice, telefone_indice)
        linha["numero_normalizado"] = _normalizar_numero(linha["numero"])
        linhas.append(linha)

    ativos = [
        linha for linha in linhas
        if not linha["delete"] and linha["ativo"] and linha["numero_normalizado"]
    ]
    if ativos and not any(linha["principal"] for linha in ativos):
        ativos[0]["principal"] = True
    return linhas


def _sincronizar_telefone_legado(contato):
    telefones = list(contato.telefones.filter(ativo=True).order_by("-principal", "ordem", "id"))
    whatsapp = next((telefone for telefone in telefones if telefone.whatsapp and telefone.principal), None)
    if whatsapp is None:
        whatsapp = next((telefone for telefone in telefones if telefone.whatsapp), None)

    contato.telefone_whatsapp = whatsapp.numero if whatsapp else None
    contato.telefone_whatsapp_normalizado = whatsapp.numero if whatsapp else None
    contato.save(update_fields=["telefone_whatsapp", "telefone_whatsapp_normalizado", "atualizado_em"])


def _localizar_telefone_para_linha(contato, linha):
    if linha["id"]:
        telefone = contato.telefones.filter(pk=linha["id"]).first()
        if telefone:
            return telefone

    numero = linha.get("numero_normalizado")
    if numero:
        return contato.telefones.filter(numero=numero).order_by("-ativo", "id").first()

    return None


def salvar_telefones_contatos(post_data, contatos_formset):
    with transaction.atomic():
        for contato_indice, contato_form in enumerate(contatos_formset.forms):
            if not _post_tem_campos_telefone(post_data, contato_indice):
                continue
            if contato_form.cleaned_data.get("DELETE"):
                continue
            contato = contato_form.instance
            if not getattr(contato, "pk", None):
                continue

            linhas = _linhas_salvamento(post_data, contato_indice)
            ids_enviados = set()

            for linha in linhas:
                telefone = _localizar_telefone_para_linha(contato, linha)
                if telefone:
                    ids_enviados.add(telefone.pk)

                if linha["delete"]:
                    if telefone:
                        telefone.ativo = False
                        telefone.principal = False
                        try:
                            telefone.save()
                        except ValidationError as erro:
                            for mensagem in _mensagens_validacao(erro):
                                _adicionar_erro_telefone(contato_form, linha["index"], mensagem)
                            raise
                    continue

                if not linha["numero_normalizado"]:
                    if telefone:
                        telefone.ativo = False
                        telefone.principal = False
                        telefone.numero = ""
                        try:
                            telefone.save()
                        except ValidationError as erro:
                            for mensagem in _mensagens_validacao(erro):
                                _adicionar_erro_telefone(contato_form, linha["index"], mensagem)
                            raise
                    continue

                if telefone is None:
                    telefone = FornecedorContatoTelefone(contato=contato)

                telefone.numero = linha["numero_normalizado"]
                telefone.tipo = linha["tipo"]
                telefone.whatsapp = linha["whatsapp"]
                telefone.principal = linha["principal"] and linha["ativo"]
                telefone.ativo = linha["ativo"]
                telefone.ordem = linha["index"] + 1
                try:
                    telefone.save()
                except ValidationError as erro:
                    for mensagem in _mensagens_validacao(erro):
                        _adicionar_erro_telefone(contato_form, linha["index"], mensagem)
                    raise
                ids_enviados.add(telefone.pk)

            contato.telefones.filter(ativo=True).exclude(pk__in=ids_enviados).update(ativo=False, principal=False)
            _sincronizar_telefone_legado(contato)
