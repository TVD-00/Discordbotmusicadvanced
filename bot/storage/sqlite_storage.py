# ##############################################################################
# MODULE: SQLITE STORAGE
# DESCRIPTION: Lớp quản lý lưu trữ dữ liệu bền vững (Persistence) bằng SQLite.
#              Sử dụng thư viện aiosqlite cho các thao tác bất đồng bộ.
# ##############################################################################

from __future__ import annotations

import json
import os
import time
from typing import Any

import aiosqlite
import wavelink

from bot.storage.memory import GuildSettings

# ------------------------------------------------------------------------------
# Constants: Giới hạn để tránh database phình to
# ------------------------------------------------------------------------------
MAX_LIKED_PER_USER = 200        # Tối đa bài yêu thích mỗi user/guild
MAX_PLAYLIST_ITEMS = 500        # Tối đa bài trong mỗi playlist
DEFAULT_LIKED_TTL_DAYS = 365    # Xóa liked tracks cũ hơn 1 năm


# ------------------------------------------------------------------------------
# Helper: _now_ts
# Purpose: Lấy timestamp hiện tại (int seconds).
# ------------------------------------------------------------------------------
def _now_ts() -> int:
    return int(time.time())


# ------------------------------------------------------------------------------
# Helper: _track_fallback
# Purpose: Tạo dữ liệu giả lập cho track nếu không lấy được raw data chuẩn.
# ------------------------------------------------------------------------------
def _track_fallback(track: wavelink.Playable) -> dict[str, Any]:
    return {
        "encoded": track.encoded,
        "info": {
            "identifier": track.identifier,
            "isSeekable": track.is_seekable,
            "author": track.author,
            "length": track.length,
            "isStream": track.is_stream,
            "position": 0,
            "title": track.title,
            "uri": track.uri,
            "artworkUrl": track.artwork,
            "isrc": track.isrc,
            "sourceName": track.source,
        },
        "pluginInfo": {},
        "userData": dict(track.extras),
    }


# ------------------------------------------------------------------------------
# Helper: _track_to_json
# Purpose: Serialize đối tượng Track thành chuỗi JSON để lưu vào DB.
# ------------------------------------------------------------------------------
def _track_to_json(track: wavelink.Playable) -> str:
    try:
        payload = track.raw_data
        return json.dumps(payload, ensure_ascii=True)
    except Exception:
        return json.dumps(_track_fallback(track), ensure_ascii=True)


# ------------------------------------------------------------------------------
# Helper: _track_from_json
# Purpose: Deserialize chuỗi JSON từ DB thành đối tượng Track.
# ------------------------------------------------------------------------------
def _track_from_json(raw: str) -> wavelink.Playable:
    data = json.loads(raw)
    return wavelink.Playable(data=data)


# ------------------------------------------------------------------------------
# Class: SQLiteStorage
# Purpose: Cung cấp các phương thức CRUD tương tác với file SQLite.
# ------------------------------------------------------------------------------
class SQLiteStorage:
    def __init__(self, path: str) -> None:
        self._path = path
        self._conn: aiosqlite.Connection | None = None

    @property
    def path(self) -> str:
        return self._path

    def _require_conn(self) -> aiosqlite.Connection:
        # Helper để đảm bảo đã kết nối DB trước khi thực hiện truy vấn.
        if self._conn is None:
            raise RuntimeError("SQLiteStorage is not connected")
        return self._conn

    # --------------------------------------------------------------------------
    # Method: connect
    # Purpose: Mở kết nối và tạo các bảng (Schema) nếu chưa tồn tại.
    # --------------------------------------------------------------------------
    async def connect(self) -> None:
        # Kết nối tới file SQLite và khởi tạo bảng nếu chưa tồn tại.
        # Nên gọi hàm này trong setup_hook của bot.
        db_dir = os.path.dirname(self._path)
        if db_dir:
            os.makedirs(db_dir, exist_ok=True)

        self._conn = await aiosqlite.connect(self._path)
        self._conn.row_factory = aiosqlite.Row

        # Tối ưu hiệu năng (WAL mode) và bật ràng buộc khóa ngoại
        await self._conn.execute("PRAGMA journal_mode=WAL")
        await self._conn.execute("PRAGMA foreign_keys=ON")

        # Bảng settings của Guild
        await self._conn.execute(
            """
            CREATE TABLE IF NOT EXISTS guild_settings (
              guild_id INTEGER PRIMARY KEY,
              volume_default INTEGER NOT NULL,
              stay_247 INTEGER NOT NULL,
              announce_enabled INTEGER NOT NULL,
              announce_channel_id INTEGER,
              dj_role_id INTEGER,
              filters_preset TEXT NOT NULL,
              buttons_enabled INTEGER NOT NULL
            )
            """
        )

        # Bảng danh sách kênh cho phép (Allowed Channels)
        await self._conn.execute(
            """
            CREATE TABLE IF NOT EXISTS allowed_channels (
              guild_id INTEGER NOT NULL,
              channel_id INTEGER NOT NULL,
              PRIMARY KEY (guild_id, channel_id)
            )
            """
        )

        # Bảng giới hạn lệnh (Command Restrictions)
        await self._conn.execute(
            """
            CREATE TABLE IF NOT EXISTS command_restrictions (
              guild_id INTEGER NOT NULL,
              command_name TEXT NOT NULL,
              channel_id INTEGER NOT NULL,
              PRIMARY KEY (guild_id, command_name)
            )
            """
        )

        # Bảng bài hát yêu thích (Liked Tracks)
        await self._conn.execute(
            """
            CREATE TABLE IF NOT EXISTS liked_tracks (
              guild_id INTEGER NOT NULL,
              user_id INTEGER NOT NULL,
              identifier TEXT NOT NULL,
              title TEXT,
              author TEXT,
              uri TEXT,
              length_ms INTEGER,
              source TEXT,
              track_json TEXT NOT NULL,
              created_at INTEGER NOT NULL,
              PRIMARY KEY (guild_id, user_id, identifier)
            )
            """
        )

        # Bảng Playlists người dùng
        await self._conn.execute(
            """
            CREATE TABLE IF NOT EXISTS playlists (
              playlist_id INTEGER PRIMARY KEY AUTOINCREMENT,
              guild_id INTEGER NOT NULL,
              owner_user_id INTEGER NOT NULL,
              name TEXT NOT NULL,
              created_at INTEGER NOT NULL,
              UNIQUE (guild_id, owner_user_id, name)
            )
            """
        )

        # Bảng chi tiết bài hát trong Playlist
        await self._conn.execute(
            """
            CREATE TABLE IF NOT EXISTS playlist_items (
              playlist_id INTEGER NOT NULL,
              position INTEGER NOT NULL,
              identifier TEXT NOT NULL,
              title TEXT,
              author TEXT,
              uri TEXT,
              length_ms INTEGER,
              source TEXT,
              track_json TEXT NOT NULL,
              PRIMARY KEY (playlist_id, position),
              FOREIGN KEY (playlist_id) REFERENCES playlists(playlist_id) ON DELETE CASCADE
            )
            """
        )

        await self._conn.commit()

    async def close(self) -> None:
        if self._conn is None:
            return
        await self._conn.close()
        self._conn = None

    # --------------------------------------------------------------------------
    # Group: DB Maintenance - Tối ưu và dọn dẹp database
    # --------------------------------------------------------------------------
    async def vacuum(self) -> None:
        # Thu gọn database, giải phóng dung lượng từ các record đã xóa.
        conn = self._require_conn()
        # VACUUM không chạy được trong transaction, cần isolation_level=None
        await conn.execute("VACUUM")

    async def get_db_stats(self) -> dict[str, int]:
        # Lấy thống kê số lượng record trong các bảng chính.
        conn = self._require_conn()
        stats: dict[str, int] = {}

        for table in ["guild_settings", "allowed_channels", "command_restrictions",
                      "liked_tracks", "playlists", "playlist_items"]:
            cur = await conn.execute(f"SELECT COUNT(*) FROM {table}")
            row = await cur.fetchone()
            stats[table] = int(row[0]) if row else 0

        return stats

    async def prune_old_liked(self, max_age_days: int = DEFAULT_LIKED_TTL_DAYS) -> int:
        # Xóa các bài yêu thích cũ hơn max_age_days.
        # Trả về số record đã xóa.
        conn = self._require_conn()
        cutoff = _now_ts() - (max_age_days * 86400)
        cur = await conn.execute(
            "DELETE FROM liked_tracks WHERE created_at < ?",
            (cutoff,),
        )
        await conn.commit()
        return int(cur.rowcount)

    async def cleanup_orphaned_guilds(self, active_guild_ids: set[int]) -> dict[str, int]:
        # Xóa dữ liệu của các guild không còn trong bot.
        # Trả về dict với số record đã xóa mỗi bảng.
        if not active_guild_ids:
            return {}

        conn = self._require_conn()
        deleted: dict[str, int] = {}

        # Tạo placeholder cho IN clause
        placeholders = ",".join("?" for _ in active_guild_ids)
        guild_list = tuple(active_guild_ids)

        # Xóa từ các bảng có guild_id
        tables = ["guild_settings", "allowed_channels", "command_restrictions", "liked_tracks"]
        for table in tables:
            cur = await conn.execute(
                f"DELETE FROM {table} WHERE guild_id NOT IN ({placeholders})",
                guild_list,
            )
            deleted[table] = int(cur.rowcount)

        # Xóa playlists và playlist_items (cascade) của guild không còn
        cur = await conn.execute(
            f"DELETE FROM playlists WHERE guild_id NOT IN ({placeholders})",
            guild_list,
        )
        deleted["playlists"] = int(cur.rowcount)

        await conn.commit()
        return deleted

    async def _enforce_liked_limit(self, guild_id: int, user_id: int) -> int:
        # Đảm bảo user không vượt quá MAX_LIKED_PER_USER.
        # Xóa bài cũ nhất nếu vượt quá. Trả về số bài đã xóa.
        conn = self._require_conn()

        # Đếm số bài hiện có
        cur = await conn.execute(
            "SELECT COUNT(*) FROM liked_tracks WHERE guild_id=? AND user_id=?",
            (guild_id, user_id),
        )
        row = await cur.fetchone()
        count = int(row[0]) if row else 0

        if count <= MAX_LIKED_PER_USER:
            return 0

        # Xóa các bài cũ nhất vượt quá giới hạn
        excess = count - MAX_LIKED_PER_USER
        cur = await conn.execute(
            """
            DELETE FROM liked_tracks
            WHERE rowid IN (
                SELECT rowid FROM liked_tracks
                WHERE guild_id=? AND user_id=?
                ORDER BY created_at ASC
                LIMIT ?
            )
            """,
            (guild_id, user_id, excess),
        )
        await conn.commit()
        return int(cur.rowcount)

    async def _enforce_playlist_limit(self, playlist_id: int) -> int:
        # Đảm bảo playlist không vượt quá MAX_PLAYLIST_ITEMS.
        # Xóa bài cuối nếu vượt quá. Trả về số bài đã xóa.
        conn = self._require_conn()

        cur = await conn.execute(
            "SELECT COUNT(*) FROM playlist_items WHERE playlist_id=?",
            (playlist_id,),
        )
        row = await cur.fetchone()
        count = int(row[0]) if row else 0

        if count <= MAX_PLAYLIST_ITEMS:
            return 0

        excess = count - MAX_PLAYLIST_ITEMS
        cur = await conn.execute(
            """
            DELETE FROM playlist_items
            WHERE rowid IN (
                SELECT rowid FROM playlist_items
                WHERE playlist_id=?
                ORDER BY position DESC
                LIMIT ?
            )
            """,
            (playlist_id, excess),
        )
        await conn.commit()
        return int(cur.rowcount)

    # --------------------------------------------------------------------------
    # Group: Guild Settings
    # --------------------------------------------------------------------------
    async def load_guild_settings_all(self) -> dict[int, GuildSettings]:
        conn = self._require_conn()
        cur = await conn.execute(
            """
            SELECT guild_id, volume_default, stay_247, announce_enabled,
                   announce_channel_id, dj_role_id, filters_preset, buttons_enabled
            FROM guild_settings
            """
        )
        rows = await cur.fetchall()

        out: dict[int, GuildSettings] = {}
        for r in rows:
            guild_id = int(r["guild_id"])
            out[guild_id] = GuildSettings(
                volume_default=int(r["volume_default"]),
                stay_247=bool(int(r["stay_247"])),
                announce_enabled=bool(int(r["announce_enabled"])),
                announce_channel_id=int(r["announce_channel_id"]) if r["announce_channel_id"] is not None else None,
                dj_role_id=int(r["dj_role_id"]) if r["dj_role_id"] is not None else None,
                filters_preset=str(r["filters_preset"] or "off"),
                buttons_enabled=bool(int(r["buttons_enabled"])),
            )

        return out

    async def upsert_guild_settings(self, guild_id: int, settings: GuildSettings) -> None:
        conn = self._require_conn()
        await conn.execute(
            """
            INSERT INTO guild_settings (
              guild_id, volume_default, stay_247, announce_enabled,
              announce_channel_id, dj_role_id, filters_preset, buttons_enabled
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(guild_id) DO UPDATE SET
              volume_default=excluded.volume_default,
              stay_247=excluded.stay_247,
              announce_enabled=excluded.announce_enabled,
              announce_channel_id=excluded.announce_channel_id,
              dj_role_id=excluded.dj_role_id,
              filters_preset=excluded.filters_preset,
              buttons_enabled=excluded.buttons_enabled
            """
            ,
            (
                guild_id,
                int(settings.volume_default),
                1 if settings.stay_247 else 0,
                1 if settings.announce_enabled else 0,
                settings.announce_channel_id,
                settings.dj_role_id,
                settings.filters_preset,
                1 if settings.buttons_enabled else 0,
            ),
        )
        await conn.commit()

    # --------------------------------------------------------------------------
    # Group: Allowed Channels (Whitelist)
    # --------------------------------------------------------------------------
    async def load_allowed_channels_all(self) -> dict[int, set[int]]:
        conn = self._require_conn()
        cur = await conn.execute("SELECT guild_id, channel_id FROM allowed_channels")
        rows = await cur.fetchall()

        out: dict[int, set[int]] = {}
        for r in rows:
            gid = int(r["guild_id"])
            cid = int(r["channel_id"])
            out.setdefault(gid, set()).add(cid)

        return out

    async def add_allowed_channel(self, guild_id: int, channel_id: int) -> None:
        conn = self._require_conn()
        await conn.execute(
            "INSERT OR IGNORE INTO allowed_channels (guild_id, channel_id) VALUES (?, ?)",
            (guild_id, channel_id),
        )
        await conn.commit()

    async def remove_allowed_channel(self, guild_id: int, channel_id: int) -> None:
        conn = self._require_conn()
        await conn.execute(
            "DELETE FROM allowed_channels WHERE guild_id=? AND channel_id=?",
            (guild_id, channel_id),
        )
        await conn.commit()

    async def clear_allowed_channels(self, guild_id: int) -> None:
        conn = self._require_conn()
        await conn.execute("DELETE FROM allowed_channels WHERE guild_id=?", (guild_id,))
        await conn.commit()

    # --------------------------------------------------------------------------
    # Group: Command Restrictions
    # --------------------------------------------------------------------------
    async def load_command_restrictions_all(self) -> dict[int, dict[str, int]]:
        conn = self._require_conn()
        cur = await conn.execute("SELECT guild_id, command_name, channel_id FROM command_restrictions")
        rows = await cur.fetchall()

        out: dict[int, dict[str, int]] = {}
        for r in rows:
            gid = int(r["guild_id"])
            cmd = str(r["command_name"])
            cid = int(r["channel_id"])
            out.setdefault(gid, {})[cmd] = cid

        return out

    async def set_command_restriction(self, guild_id: int, command_name: str, channel_id: int) -> None:
        conn = self._require_conn()
        await conn.execute(
            """
            INSERT INTO command_restrictions (guild_id, command_name, channel_id)
            VALUES (?, ?, ?)
            ON CONFLICT(guild_id, command_name) DO UPDATE SET channel_id=excluded.channel_id
            """,
            (guild_id, command_name, channel_id),
        )
        await conn.commit()

    async def clear_command_restriction(self, guild_id: int, command_name: str) -> None:
        conn = self._require_conn()
        await conn.execute(
            "DELETE FROM command_restrictions WHERE guild_id=? AND command_name=?",
            (guild_id, command_name),
        )
        await conn.commit()

    # --------------------------------------------------------------------------
    # Group: Liked Tracks
    # --------------------------------------------------------------------------
    async def like_track(self, guild_id: int, user_id: int, track: wavelink.Playable) -> bool:
        conn = self._require_conn()
        identifier = track.identifier
        raw = _track_to_json(track)

        cur = await conn.execute(
            """
            INSERT OR IGNORE INTO liked_tracks (
              guild_id, user_id, identifier, title, author, uri, length_ms, source, track_json, created_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                guild_id,
                user_id,
                identifier,
                track.title,
                track.author,
                track.uri,
                int(track.length),
                track.source,
                raw,
                _now_ts(),
            ),
        )
        await conn.commit()

        # Enforce giới hạn số bài yêu thích
        if cur.rowcount > 0:
            await self._enforce_liked_limit(guild_id, user_id)

        return cur.rowcount > 0

    async def unlike_track(self, guild_id: int, user_id: int, identifier: str) -> bool:
        conn = self._require_conn()
        cur = await conn.execute(
            "DELETE FROM liked_tracks WHERE guild_id=? AND user_id=? AND identifier=?",
            (guild_id, user_id, identifier),
        )
        await conn.commit()
        return cur.rowcount > 0

    async def clear_liked(self, guild_id: int, user_id: int) -> int:
        conn = self._require_conn()
        cur = await conn.execute(
            "DELETE FROM liked_tracks WHERE guild_id=? AND user_id=?",
            (guild_id, user_id),
        )
        await conn.commit()
        return int(cur.rowcount)

    async def list_liked(self, guild_id: int, user_id: int) -> list[wavelink.Playable]:
        conn = self._require_conn()
        cur = await conn.execute(
            """
            SELECT track_json FROM liked_tracks
            WHERE guild_id=? AND user_id=?
            ORDER BY created_at DESC
            """,
            (guild_id, user_id),
        )
        rows = await cur.fetchall()
        return [_track_from_json(str(r["track_json"])) for r in rows]

    # --------------------------------------------------------------------------
    # Group: Playlists
    # --------------------------------------------------------------------------
    async def create_playlist(self, guild_id: int, owner_user_id: int, name: str) -> int:
        conn = self._require_conn()
        cur = await conn.execute(
            "INSERT INTO playlists (guild_id, owner_user_id, name, created_at) VALUES (?, ?, ?, ?)",
            (guild_id, owner_user_id, name, _now_ts()),
        )
        await conn.commit()
        if cur.lastrowid is None:
            raise RuntimeError("Failed to retrieve lastrowid for new playlist")
        return int(cur.lastrowid)

    async def delete_playlist(self, guild_id: int, owner_user_id: int, name: str) -> bool:
        conn = self._require_conn()
        cur = await conn.execute(
            "DELETE FROM playlists WHERE guild_id=? AND owner_user_id=? AND name=?",
            (guild_id, owner_user_id, name),
        )
        await conn.commit()
        return cur.rowcount > 0

    async def list_playlists(self, guild_id: int, owner_user_id: int) -> list[tuple[str, int]]:
        conn = self._require_conn()
        cur = await conn.execute(
            """
            SELECT p.name, COUNT(i.position) AS item_count
            FROM playlists p
            LEFT JOIN playlist_items i ON i.playlist_id = p.playlist_id
            WHERE p.guild_id=? AND p.owner_user_id=?
            GROUP BY p.playlist_id
            ORDER BY p.created_at DESC
            """,
            (guild_id, owner_user_id),
        )
        rows = await cur.fetchall()
        return [(str(r["name"]), int(r["item_count"])) for r in rows]

    async def _get_playlist_id(self, guild_id: int, owner_user_id: int, name: str) -> int | None:
        conn = self._require_conn()
        cur = await conn.execute(
            "SELECT playlist_id FROM playlists WHERE guild_id=? AND owner_user_id=? AND name=?",
            (guild_id, owner_user_id, name),
        )
        row = await cur.fetchone()
        if not row:
            return None
        return int(row["playlist_id"])

    async def playlist_tracks(self, guild_id: int, owner_user_id: int, name: str) -> list[wavelink.Playable] | None:
        pid = await self._get_playlist_id(guild_id, owner_user_id, name)
        if pid is None:
            return None

        conn = self._require_conn()
        cur = await conn.execute(
            "SELECT track_json FROM playlist_items WHERE playlist_id=? ORDER BY position ASC",
            (pid,),
        )
        rows = await cur.fetchall()
        return [_track_from_json(str(r["track_json"])) for r in rows]

    async def add_playlist_track(self, guild_id: int, owner_user_id: int, name: str, track: wavelink.Playable) -> bool:
        pid = await self._get_playlist_id(guild_id, owner_user_id, name)
        if pid is None:
            return False

        conn = self._require_conn()

        # Kiểm tra giới hạn trước khi thêm
        cur = await conn.execute(
            "SELECT COUNT(*) FROM playlist_items WHERE playlist_id=?",
            (pid,),
        )
        row = await cur.fetchone()
        if row and int(row[0]) >= MAX_PLAYLIST_ITEMS:
            return False  # Đã đạt giới hạn, không thêm được

        cur = await conn.execute(
            "SELECT COALESCE(MAX(position), 0) AS max_pos FROM playlist_items WHERE playlist_id=?",
            (pid,),
        )
        row = await cur.fetchone()
        next_pos = int(row["max_pos"]) + 1 if row else 1

        await conn.execute(
            """
            INSERT INTO playlist_items (
              playlist_id, position, identifier, title, author, uri, length_ms, source, track_json
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                pid,
                next_pos,
                track.identifier,
                track.title,
                track.author,
                track.uri,
                int(track.length),
                track.source,
                _track_to_json(track),
            ),
        )

        await conn.commit()
        return True

    async def remove_playlist_track(self, guild_id: int, owner_user_id: int, name: str, index: int) -> bool:
        pid = await self._get_playlist_id(guild_id, owner_user_id, name)
        if pid is None:
            return False

        conn = self._require_conn()
        pos = int(index)
        if pos <= 0:
            return False

        cur = await conn.execute(
            "DELETE FROM playlist_items WHERE playlist_id=? AND position=?",
            (pid, pos),
        )
        if cur.rowcount <= 0:
            await conn.commit()
            return False

        # Cập nhật lại thứ tự position cho các item phía sau
        await conn.execute(
            "UPDATE playlist_items SET position = position - 1 WHERE playlist_id=? AND position > ?",
            (pid, pos),
        )
        await conn.commit()
        return True

    async def clear_playlist(self, guild_id: int, owner_user_id: int, name: str) -> bool:
        pid = await self._get_playlist_id(guild_id, owner_user_id, name)
        if pid is None:
            return False

        conn = self._require_conn()
        await conn.execute("DELETE FROM playlist_items WHERE playlist_id=?", (pid,))
        await conn.commit()
        return True
