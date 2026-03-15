"""
Logging
"""

# pyright: basic

import logging
import sys

import loguru
from loguru import logger as _logger

from app.core.config import settings
from app.schema.log_entry import LogEntry

__all__ = (
    "configure_standard_logging",
    "log_serializer",
    "logger",
    "sink",
    "uvicorn_log_config",
)


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
            "level": "DEBUG",
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
