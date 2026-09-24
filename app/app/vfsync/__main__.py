"""Entrypoint: python -m app.vfsync"""

import asyncio

from app.vfsync.main import run

if __name__ == "__main__":
    asyncio.run(run())
