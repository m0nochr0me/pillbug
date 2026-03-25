"""
Logging
"""

# pyright: basic

import logging
import sys
import time
from collections.abc import Callable, Mapping
from dataclasses import dataclass

import loguru
from loguru import logger as _logger

from app.core.config import settings
from app.schema.log_entry import LogEntry

__all__ = (
    "ThrottledExceptionLogger",
    "configure_standard_logging",
    "format_exception_summary",
    "format_failure_message",
    "log_serializer",
    "logger",
    "sink",
    "uvicorn_log_config",
)


@dataclass(slots=True)
class _ThrottledErrorLogState:
    error_summary: str
    last_logged_at: float
    suppressed_count: int = 0


class InterceptHandler(logging.Handler):
    def emit(self, record: logging.LogRecord) -> None:
        # Get corresponding Loguru level
        try:
            level = logger.level(record.levelname).name
        except ValueError:
            level = record.levelno

        # Find caller to get correct stack depth
        frame, depth = logging.currentframe(), 2
        while frame.f_back and frame.f_code.co_filename == logging.__file__:
            frame = frame.f_back
            depth += 1

        bound_logger = logger.bind(stdlib_logger=record.name)

        correlation_id = getattr(record, "correlation_id", "")
        if correlation_id:
            bound_logger = bound_logger.bind(correlation_id=correlation_id)

        bound_logger.opt(depth=depth, exception=record.exc_info).log(level, record.getMessage())


uvicorn_log_config = {
    "version": 1,
    "disable_existing_loggers": False,
    "filters": {
        "correlation_id": {
            "()": "asgi_correlation_id.CorrelationIdFilter",
            "default_value": "",
        },
    },
    "handlers": {
        "default": {
            "class": "app.core.log.InterceptHandler",
            "filters": ["correlation_id"],
        },
    },
    "loggers": {
        "uvicorn": {
            "handlers": ["default"],
            "level": "DEBUG",
            "propagate": False,
        },
        "uvicorn.access": {
            "handlers": ["default"],
            "level": "WARNING",
            "propagate": False,
        },
        "uvicorn.error": {
            "handlers": ["default"],
            "level": "ERROR",
            "propagate": False,
        },
    },
    "root": {"handlers": ["default"], "level": "DEBUG"},
}


def _normalize_external_logger(name: str) -> None:
    external_logger = logging.getLogger(name)
    external_logger.handlers.clear()
    external_logger.propagate = True
    external_logger.setLevel(logging.NOTSET)


def format_exception_summary(exc: BaseException) -> str:
    parts = [f"type={type(exc).__name__}"]

    for attr_name in ("error_code", "method", "status_code"):
        attr_value = getattr(exc, attr_name, None)
        if attr_value not in (None, ""):
            parts.append(f"{attr_name}={attr_value}")

    description = getattr(exc, "description", None)
    if description not in (None, ""):
        parts.append(f"description={description}")
    else:
        message = str(exc).strip()
        if message:
            parts.append(f"message={message}")

    return " ".join(parts)


def format_failure_message(
    subject: str,
    action: str,
    *,
    exc: BaseException | None = None,
    error_summary: str | None = None,
    context: Mapping[str, object] | None = None,
) -> str:
    details = error_summary or format_exception_summary(exc or RuntimeError(action))
    parts = [f"{subject} {action}", details]

    if context is not None:
        for key, value in context.items():
            if value is not None:
                parts.append(f"{key}={value}")

    return " ".join(parts)


class ThrottledExceptionLogger:
    def __init__(
        self,
        *,
        subject: str,
        is_transient: Callable[[BaseException], bool],
        cooldown_seconds: float = 60.0,
    ) -> None:
        self._subject = subject
        self._is_transient = is_transient
        self._cooldown_seconds = cooldown_seconds
        self._state_by_key: dict[str, _ThrottledErrorLogState] = {}

    def log(
        self,
        *,
        action: str,
        exc: BaseException,
        suppression_key: str,
        context: Mapping[str, object] | None = None,
    ) -> None:
        if self._is_transient(exc):
            self._log_transient_warning(
                action=action,
                exc=exc,
                suppression_key=suppression_key,
                context=context,
            )
            return

        logger.exception(format_failure_message(self._subject, action, exc=exc, context=context))

    def _log_transient_warning(
        self,
        *,
        action: str,
        exc: BaseException,
        suppression_key: str,
        context: Mapping[str, object] | None,
    ) -> None:
        error_summary = format_exception_summary(exc)
        now = time.monotonic()
        previous_state = self._state_by_key.get(suppression_key)

        if (
            previous_state is not None
            and previous_state.error_summary == error_summary
            and now - previous_state.last_logged_at < self._cooldown_seconds
        ):
            previous_state.suppressed_count += 1
            return

        if previous_state is not None and previous_state.suppressed_count > 0:
            logger.warning(
                format_failure_message(
                    self._subject,
                    f"{action} repeated {previous_state.suppressed_count} additional times",
                    error_summary=previous_state.error_summary,
                    context=context,
                )
            )

        logger.warning(format_failure_message(self._subject, action, error_summary=error_summary, context=context))
        self._state_by_key[suppression_key] = _ThrottledErrorLogState(
            error_summary=error_summary,
            last_logged_at=now,
        )


def log_serializer(record: loguru.Record) -> str:
    """
    Custom log serializer for loguru
    """

    message = record["message"]
    if exc := record.get("exception"):
        message += f" - {exc.type}({exc.value})"

    logger_name = str(record["extra"].get("stdlib_logger") or record["name"])
    correlation_id = str(record["extra"].get("correlation_id") or "").strip()

    message_prefix = f"{logger_name} - "
    if correlation_id:
        message_prefix = f"{correlation_id} - {message_prefix}"

    log_entry = LogEntry(
        asctime=record["time"],
        levelname=record["level"].name,
        message=f"{message_prefix}{message}",
    )

    return log_entry.model_dump_json()


def _patch_serialized_log_line(record: loguru.Record) -> None:
    record["extra"]["serialized"] = log_serializer(record)


def sink(message: loguru.Message) -> None:
    """
    Custom sink for loguru
    """
    print(log_serializer(message.record))


logger = _logger.patch(_patch_serialized_log_line)


def configure_standard_logging() -> None:
    intercept_handler = InterceptHandler()
    logging.basicConfig(handlers=[intercept_handler], level=logging.INFO, force=True)
    logging.captureWarnings(True)

    warnings_logger = logging.getLogger("py.warnings")
    warnings_logger.handlers = [intercept_handler]
    warnings_logger.setLevel(logging.WARNING)
    warnings_logger.propagate = False

    _normalize_external_logger("fastmcp")


logger.remove()

if settings.LOG_DIR:
    settings.LOG_DIR.mkdir(parents=True, exist_ok=True)
    logger.add(
        f"{settings.LOG_DIR}/app.log.jsonl",
        format="{extra[serialized]}",
        rotation="50 MB",
        compression="zip",
        level="DEBUG" if settings.DEBUG else "INFO",
        backtrace=True,
        diagnose=True,
    )

if "cli" not in settings.enabled_channels():
    logger.add(
        sys.stderr,
        format="{extra[serialized]}",
        level="DEBUG" if settings.DEBUG else "INFO",
    )

configure_standard_logging()
