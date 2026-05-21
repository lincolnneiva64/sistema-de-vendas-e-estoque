import ipaddress
import socket

from django.http.request import split_domain_port


ONLINE_HOST_SUFFIX = "onrender.com"


def normalize_host(host):
    domain, _port = split_domain_port(host or "")
    return (domain or host or "").strip("[]").lower()


def is_local_host(host):
    host = normalize_host(host)
    if host in {"localhost", "127.0.0.1"}:
        return True

    try:
        ip = ipaddress.ip_address(host)
    except ValueError:
        return False

    return ip.version == 4 and ip.is_private


def is_loopback_host(host):
    host = normalize_host(host)
    return host in {"localhost", "127.0.0.1"}


def is_online_host(host):
    return normalize_host(host).endswith(ONLINE_HOST_SUFFIX)


def detect_local_ipv4():
    candidates = []

    try:
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as sock:
            sock.connect(("8.8.8.8", 80))
            candidates.append(sock.getsockname()[0])
    except OSError:
        pass

    try:
        hostname = socket.gethostname()
        candidates.extend(socket.gethostbyname_ex(hostname)[2])
    except OSError:
        pass

    for candidate in candidates:
        try:
            ip = ipaddress.ip_address(candidate)
        except ValueError:
            continue
        if ip.version == 4 and ip.is_private and not ip.is_loopback:
            return str(ip)

    return ""


def current_environment_label(host):
    if is_online_host(host):
        return "ONLINE / Render"
    if is_local_host(host):
        return "LOCAL / Wi-Fi"
    return "INDEFINIDO"
