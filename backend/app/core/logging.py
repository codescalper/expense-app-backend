"""Structured logging (structlog) with sensitive-field redaction."""

from __future__ import annotations

import logging
import sys
from collections.abc import Mapping, MutableMapping
from typing import Any

import structlog

# Any key containing one of these is redacted ...
SENSITIVE_KEY_PARTS = (
    "password",
    "passwd",
    "secret",
    "token",
    "authorization",
    "cookie",
    "cvv",
    "api_key",
)
# ... and these only on an exact match (so e.g. "status_code" stays visible).
SENSITIVE_EXACT_KEYS = frozenset({"code", "otp", "pin", "upi_pin", "mpin", "card_number"})
REDACTED = "[REDACTED]"


def _is_sensitive(key: str) -> bool:
    k = key.lower()
    return k in SENSITIVE_EXACT_KEYS or any(part in k for part in SENSITIVE_KEY_PARTS)


def redact(value: Any) -> Any:
    """Recursively redact values whose key looks sensitive."""
    if isinstance(value, Mapping):
        return {k: (REDACTED if _is_sensitive(str(k)) else redact(v)) for k, v in value.items()}
    if isinstance(value, list | tuple):
        return [redact(v) for v in value]
    return value


def _redact_processor(
    _logger: Any, _method: str, event_dict: MutableMapping[str, Any]
) -> MutableMapping[str, Any]:
    for key in list(event_dict.keys()):
        if key == "event":
            continue
        if _is_sensitive(key):
            event_dict[key] = REDACTED
        else:
            event_dict[key] = redact(event_dict[key])
    return event_dict


def configure_logging(environment: str) -> None:
    json_logs = environment in ("staging", "production")
    shared: list[Any] = [
        structlog.contextvars.merge_contextvars,
        structlog.stdlib.add_log_level,
        structlog.stdlib.add_logger_name,
        structlog.processors.TimeStamper(fmt="iso", utc=True),
        _redact_processor,
    ]
    # Plain tracebacks only: the dev default (rich / better-exceptions) prints every frame's
    # local variables, which include passwords, tokens and request bodies.
    renderer: Any = (
        structlog.processors.JSONRenderer()
        if json_logs
        else structlog.dev.ConsoleRenderer(exception_formatter=structlog.dev.plain_traceback)
    )
    structlog.configure(
        processors=[*shared, structlog.stdlib.ProcessorFormatter.wrap_for_formatter],
        logger_factory=structlog.stdlib.LoggerFactory(),
        wrapper_class=structlog.stdlib.BoundLogger,
        cache_logger_on_first_use=True,
    )
    formatter = structlog.stdlib.ProcessorFormatter(
        foreign_pre_chain=shared,
        processors=[
            structlog.stdlib.ProcessorFormatter.remove_processors_meta,
            structlog.processors.format_exc_info if json_logs else (lambda _l, _m, e: e),
            renderer,
        ],
    )
    handler = logging.StreamHandler(sys.stdout)
    handler.setFormatter(formatter)
    root = logging.getLogger()
    root.handlers = [handler]
    root.setLevel(logging.INFO)
    # uvicorn's access log is replaced by our request-logging middleware.
    logging.getLogger("uvicorn.access").handlers = []
    logging.getLogger("uvicorn.access").propagate = False
    for name in ("uvicorn", "uvicorn.error"):
        logging.getLogger(name).handlers = []
        logging.getLogger(name).propagate = True


def get_logger(name: str | None = None) -> Any:
    return structlog.get_logger(name)
