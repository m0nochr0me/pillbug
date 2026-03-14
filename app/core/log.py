"""
Logging
"""

# pyright: basic

import logging

import loguru
from loguru import logger

from app.core.config import settings
from app.schema.log_entry import LogEntry

__all__ = (
    "log_serializer",
    "logger",
    "sink",
    "uvicorn_log_config",
)


class InterceptHandler(logging.Handler):
    def emit(self, record):
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

        logger.opt(depth=depth, exception=record.exc_info).log(level, record.getMessage())


uvicorn_log_config = {
    "version": 1,
    "disable_existing_loggers": False,
    "filters": {
        "correlation_id": {
            "()": "asgi_correlation_id.CorrelationIdFilter",
            "default_value": "",
        },
    },
    "formatters": {
        "default": {
            "format": '{"asctime":"%(asctime)s","levelname":"%(levelname)s","message":"%(correlation_id)s - %(name)s - %(message)s"}',
        },
    },
    "handlers": {
        "default": {
            "class": "logging.FileHandler",
            "formatter": "default",
            "filename": str(settings.LOG_DIR / "mcp_server.log")
            if settings.LOG_DIR
            else "mcp_server.log",
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

    log_entry = LogEntry(
        asctime=record["time"],
        levelname=record["level"].name,
        message=f"{record['name']} - {message}",
    )

    return log_entry.model_dump_json()


def sink(message: loguru.Message) -> None:
    """
    Custom sink for loguru
    """
    print(log_serializer(message.record))


logger.remove()

if settings.LOG_DIR:
    logger.add(
        f"{settings.LOG_DIR}/app.log",
        rotation="50 MB",
        compression="zip",
        level="DEBUG" if settings.DEBUG else "INFO",
        backtrace=True,
        diagnose=True,
    )

logger.add(
    sink,
    level="DEBUG" if settings.DEBUG else "INFO",
)

logging.basicConfig(handlers=[InterceptHandler()], level=logging.INFO)
