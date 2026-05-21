from urllib.parse import urlsplit

from django.conf import settings

from .environment import (
    current_environment_label,
    detect_local_ipv4,
    is_local_host,
    is_loopback_host,
    is_online_host,
    normalize_host,
)


LOCAL_LINK_HINT = (
    "Link local nao detectado. Rode o servidor com runserver 0.0.0.0:8000 "
    "e confira o IP da maquina."
)


def ambiente_sistema(request):
    online_url = getattr(settings, "SISTEMA_ONLINE_URL", "https://sistema-de-vendas-e-estoque.onrender.com").rstrip("/")

    if not request:
        return {
            "ambiente_atual": "INDEFINIDO",
            "sistema_online_url": online_url,
            "sistema_local_url": "",
            "sistema_local_aviso": LOCAL_LINK_HINT,
        }

    host = request.get_host()
    hostname = normalize_host(host)
    ambiente = current_environment_label(host)
    local_url = ""
    local_aviso = ""

    if is_local_host(host) and not is_loopback_host(host):
        local_url = f"{request.scheme}://{host}"
    elif is_loopback_host(host):
        local_ip = detect_local_ipv4()
        if local_ip:
            port = urlsplit(f"//{host}").port or 8000
            local_url = f"http://{local_ip}:{port}"
        else:
            local_aviso = LOCAL_LINK_HINT
    else:
        local_aviso = LOCAL_LINK_HINT

    return {
        "ambiente_atual": ambiente,
        "ambiente_host_atual": hostname,
        "ambiente_is_online": is_online_host(host),
        "ambiente_is_local": is_local_host(host),
        "sistema_online_url": online_url,
        "sistema_local_url": local_url,
        "sistema_local_aviso": local_aviso,
    }
