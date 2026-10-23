"""Wall-clock diagnostics only; no SQL text/parameters, transactions or extra queries."""
from contextvars import ContextVar
from functools import wraps
import logging
from time import perf_counter

from django.db import connection

logger = logging.getLogger(__name__)
_atual = ContextVar('diagnostico_finalizacao_compra', default=None)


def etapa_finalizacao_compra(nome):
    estado = _atual.get()
    if estado is not None:
        agora = perf_counter()
        estado['etapas'].append((estado['etapa'], agora - estado['inicio_etapa']))
        estado['etapa'] = nome
        estado['inicio_etapa'] = agora
        logger.info('compra_finalizacao compra=%s etapa=%s inicio', estado['compra'], nome)


def diagnosticar_finalizacao_http(func):
    @wraps(func)
    def wrapped(request, *args, **kwargs):
        if request.method != 'POST' or request.POST.get('acao_compra') != 'confirmar_financeiro':
            return func(request, *args, **kwargs)
        inicio = perf_counter()
        estado = dict(compra=kwargs.get('pk', args[0] if args else None), etapa='validacao',
                      inicio_etapa=inicio, etapas=[], consultas=0, sql_segundos=0.0)
        token = _atual.set(estado)
        erro = None
        def medir(execute, sql, params, many, context):
            inicio_sql = perf_counter()
            try:
                return execute(sql, params, many, context)
            finally:
                segundos = perf_counter() - inicio_sql
                estado['consultas'] += 1
                estado['sql_segundos'] += segundos
                if segundos >= 0.25:
                    logger.warning('compra_finalizacao consulta_lenta compra=%s etapa=%s comando=%s segundos=%.3f',
                                   estado['compra'], estado['etapa'], sql.split(None, 1)[0] if sql.strip() else '', segundos)
        try:
            with connection.execute_wrapper(medir):
                return func(request, *args, **kwargs)
        except BaseException as exc:
            erro = type(exc).__name__
            raise
        finally:
            total = perf_counter() - inicio
            estado['etapas'].append((estado['etapa'], perf_counter() - estado['inicio_etapa']))
            log = logger.warning if total >= 1 or erro else logger.info
            log('compra_finalizacao fim compra=%s total_s=%.3f consultas=%s driver_s=%.3f fora_driver_s=%.3f etapas=%s excecao=%s',
                estado['compra'], total, estado['consultas'], estado['sql_segundos'],
                max(0, total - estado['sql_segundos']), estado['etapas'], erro)
            _atual.reset(token)
    return wrapped
