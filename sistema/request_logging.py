"""Request error diagnostics without request values or exception messages."""
import logging
from pathlib import Path
import traceback


class SafeRequestErrorFormatter(logging.Formatter):
    def format(self, record):
        request = getattr(record, "request", None)
        method = getattr(request, "method", "")
        if method not in {"GET", "HEAD", "POST", "PUT", "PATCH", "DELETE", "OPTIONS", "TRACE"}:
            method = "unknown"
        match = getattr(request, "resolver_match", None)
        # The registered view name identifies the endpoint without bearer paths,
        # URL arguments, query strings, headers, cookies or POST data.
        view = getattr(match, "view_name", None) or "unresolved"
        status = getattr(record, "status_code", None)
        status = status if isinstance(status, int) and 100 <= status <= 599 else "unknown"
        lines = [f"ERROR django.request status={status} method={method} view={view}"]
        if record.exc_info:
            self._exception(lines, record.exc_info[1], record.exc_info[2], set())
        # Deliberately ignore msg/args, exc_text and stack_info. They may contain
        # secrets even when request data and traceback locals are not included.
        return "\n".join(lines)

    def _exception(self, lines, exception, tb, visited):
        if exception is None or id(exception) in visited or len(visited) >= 10:
            return
        visited.add(id(exception))
        cause = exception.__cause__
        if cause is None and not exception.__suppress_context__:
            cause = exception.__context__
        if cause is not None and id(cause) not in visited:
            self._exception(lines, cause, cause.__traceback__, visited)
            lines.append("Chained exception:")
        if tb is not None:
            lines.append("Traceback (most recent call last):")
            for frame, lineno in traceback.walk_tb(tb):
                code = frame.f_code
                lines.append(f'  File "{Path(code.co_filename).name}", line {lineno}, in {code.co_name}')
        kind = type(exception)
        lines.append(f"{kind.__module__}.{kind.__qualname__}: [message omitted]")
