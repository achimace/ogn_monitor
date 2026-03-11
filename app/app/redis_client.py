"""Redis connection management."""

import redis.asyncio as aioredis

from app.config import settings

# Global Redis connection pool - initialized in lifespan
redis_pool: aioredis.Redis | None = None


async def init_redis() -> aioredis.Redis:
    """Initialize Redis connection pool."""
    global redis_pool
    redis_pool = aioredis.from_url(
        settings.redis_url,
        encoding="utf-8",
        decode_responses=True,
        max_connections=20,
    )
    # Verify connection
    await redis_pool.ping()
    return redis_pool


async def close_redis() -> None:
    """Close Redis connection pool."""
    global redis_pool
    if redis_pool:
        await redis_pool.close()
        redis_pool = None


def get_redis() -> aioredis.Redis:
    """Get the Redis connection. Raises if not initialized."""
    if redis_pool is None:
        raise RuntimeError("Redis not initialized. Call init_redis() first.")
    return redis_pool
