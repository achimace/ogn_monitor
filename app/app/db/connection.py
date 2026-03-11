"""PostgreSQL async connection pool."""

import asyncpg

from app.config import settings

# Global connection pool - initialized in lifespan
db_pool: asyncpg.Pool | None = None


def _get_asyncpg_url() -> str:
    """Convert SQLAlchemy URL to asyncpg format.

    SQLAlchemy: postgresql+asyncpg://user:pass@host:port/db
    asyncpg:    postgresql://user:pass@host:port/db
    """
    return settings.database_url.replace("postgresql+asyncpg://", "postgresql://")


async def init_db() -> asyncpg.Pool:
    """Initialize the database connection pool."""
    global db_pool
    db_pool = await asyncpg.create_pool(
        _get_asyncpg_url(),
        min_size=2,
        max_size=10,
        command_timeout=30,
    )
    return db_pool


async def close_db() -> None:
    """Close the database connection pool."""
    global db_pool
    if db_pool:
        await db_pool.close()
        db_pool = None


def get_db() -> asyncpg.Pool:
    """Get the database pool. Raises if not initialized."""
    if db_pool is None:
        raise RuntimeError("Database not initialized. Call init_db() first.")
    return db_pool
