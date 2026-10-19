"""Never print employee bearer paths in application/access logging."""
import logging
import re

_PATH = re.compile(r'(/recolhimento-individual/|%2Frecolhimento-individual%2F)[A-Za-z0-9_-]+', re.IGNORECASE)


def redact(message):
    return _PATH.sub(r'\1[oculto]', message)


class PickupTokenFilter(logging.Filter):
    def filter(self, record):
        record.msg = redact(record.getMessage())
        record.args = ()
        if record.exc_info:
            record.exc_text = redact(record.exc_text or logging.Formatter().formatException(record.exc_info))
        return True


def install():
    for name in ('django.request', 'django.server', 'django.security.csrf', 'gunicorn.access', 'gunicorn.error'):
        logger = logging.getLogger(name)
        if not any(isinstance(f, PickupTokenFilter) for f in logger.filters):
            logger.addFilter(PickupTokenFilter())
