"""
Caching
"""

# pyright: basic

from app import __project__
from app.core.config import settings

__all__ = ("cache",)

if settings.REDIS_HOST:
    from aiocache import RedisCache

    cache = RedisCache(
        namespace=__project__,
        endpoint=settings.REDIS_HOST,
        port=settings.REDIS_PORT,
        password=settings.REDIS_PASSWORD,
        db=settings.REDIS_DB,
    )
else:
    from aiocache import SimpleMemoryCache

    cache = SimpleMemoryCache(namespace=__project__)
