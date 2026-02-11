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


def cleanup_guild_lock(guild_id: int) -> None:
    """Xóa lock của guild khi bot rời guild để tránh memory leak."""
    lock = _LOCKS.get(guild_id)
    # Chỉ xóa khi lock không đang bị giữ
    if lock is not None and not lock.locked():
        _LOCKS.pop(guild_id, None)


def cleanup_stale_locks(active_guild_ids: set[int]) -> None:
    """Xóa lock của tất cả guild không còn active."""
    stale = [gid for gid in _LOCKS if gid not in active_guild_ids]
    for gid in stale:
        lock = _LOCKS.get(gid)
        if lock is not None and not lock.locked():
            del _LOCKS[gid]
