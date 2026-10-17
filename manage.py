#!/usr/bin/env python
"""Django's command-line utility for administrative tasks."""
import os
import sys


def main():
    """Run administrative tasks."""
    # Only this local development launcher may enable the local sale transport.
    # No host/header or DEBUG flag can enable it in a WSGI/Render process.
    if len(sys.argv) > 1 and sys.argv[1] == "runserver":
        addresses = [arg for arg in sys.argv[2:] if not arg.startswith("-")]
        address = addresses[0] if addresses else "127.0.0.1:8000"
        loopback = address.isdigit() or address.split(":", 1)[0] in {"127.0.0.1", "localhost"}
        if loopback and not any(os.environ.get(k) for k in ("RENDER", "RENDER_SERVICE_ID")):
            os.environ["_LOCAL_SALES_LOOPBACK"] = "1"
        else:
            os.environ.pop("_LOCAL_SALES_LOOPBACK", None)
    os.environ.setdefault('DJANGO_SETTINGS_MODULE', 'sistema.settings')
    try:
        from django.core.management import execute_from_command_line
    except ImportError as exc:
        raise ImportError(
            "Couldn't import Django. Are you sure it's installed and "
            "available on your PYTHONPATH environment variable? Did you "
            "forget to activate a virtual environment?"
        ) from exc
    execute_from_command_line(sys.argv)


if __name__ == '__main__':
    main()
