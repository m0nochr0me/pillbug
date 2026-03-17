"""
Caching
"""

# pyright: basic

from aiocache import RedisCache
from aiocache.base import BaseCache
from aiocache.serializers import JsonSerializer
from redis.asyncio import ConnectionPool, Redis

from app import __project__
from app.core.config import settings
from app.core.redis import redis_pool

__all__ = ("cache",)


class CustomRedisCache(RedisCache):
    def __init__(
        self,
        *,
        client: Redis | None = None,
        connection_pool: ConnectionPool | None = None,
        serializer=None,
        **kwargs,
    ) -> None:
        if client is not None and connection_pool is not None:
            raise ValueError("Provide either client or connection_pool, not both")

        if client is None and connection_pool is None:
            raise ValueError("Provide either client or connection_pool")

        BaseCache.__init__(self, serializer=serializer or JsonSerializer(), **kwargs)
        self._uses_shared_client = client is not None
        self._uses_shared_pool = connection_pool is not None

        pool = client.connection_pool if client is not None else connection_pool
        connection_kwargs = getattr(pool, "connection_kwargs", {})
        self.endpoint = connection_kwargs.get("host", "shared")
        self.port = int(connection_kwargs.get("port", 0) or 0)
        self.db = int(connection_kwargs.get("db", 0) or 0)
        self.password = connection_kwargs.get("password")

        if client is not None:
            self.client = client
            return

        if connection_pool is not None:
            self.client = Redis(connection_pool=connection_pool)
            return

    async def _close(self, *args, _conn=None, **kwargs):
        if self._uses_shared_client:
            return None

        if self._uses_shared_pool:
            await self.client.close(close_connection_pool=False)
            return None

        await self.client.close()

if settings.REDIS_HOST:
    cache = CustomRedisCache(
        namespace=__project__,
        connection_pool=redis_pool,
    )
else:
    from aiocache import SimpleMemoryCache

    cache = SimpleMemoryCache(namespace=__project__)
