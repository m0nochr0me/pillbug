from redis.asyncio import ConnectionPool, Redis

from app.core.config import settings

__all__ = ("redis", "redis_pool")

redis_pool = ConnectionPool(
    host=settings.REDIS_HOST,
    port=settings.REDIS_PORT,
    db=settings.REDIS_DB,
    password=settings.REDIS_PASSWORD,
)
redis = Redis(
    connection_pool=redis_pool,
    # decode_responses=True,
)
