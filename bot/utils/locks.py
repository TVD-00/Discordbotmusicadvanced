from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager


_LOCKS: dict[int, asyncio.Lock] = {}


def _get_lock(guild_id: int) -> asyncio.Lock:
    lock = _LOCKS.get(guild_id)
    if lock is None:
        lock = asyncio.Lock()
        _LOCKS[guild_id] = lock
    return lock


@asynccontextmanager
async def guild_lock(guild_id: int):
    lock = _get_lock(guild_id)
    async with lock:
        yield
