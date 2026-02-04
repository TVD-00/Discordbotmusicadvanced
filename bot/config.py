# ##############################################################################
# MODULE: CONFIG
# DESCRIPTION: Quản lý cấu hình ứng dụng từ biến môi trường (Environment Variables).
#              Sử dụng python-dotenv để load file .env.
# ##############################################################################

from __future__ import annotations

from dataclasses import dataclass
import os

from dotenv import load_dotenv


# ------------------------------------------------------------------------------
# Helper: _get_bool
# Purpose: Chuyển đổi giá trị string từ env thành boolean an toàn.
# ------------------------------------------------------------------------------
def _get_bool(name: str, default: bool = False) -> bool:
    raw = os.getenv(name)
    if raw is None:
        return default
    return raw.strip().lower() in {"1", "true", "yes", "y", "on"}


# ------------------------------------------------------------------------------
# Helper: _get_int
# Purpose: Chuyển đổi giá trị string từ env thành int, có giá trị mặc định.
# ------------------------------------------------------------------------------
def _get_int(name: str, default: int) -> int:
    raw = os.getenv(name)
    if raw is None or not raw.strip():
        return default
    return int(raw)


# ------------------------------------------------------------------------------
# Helper: _get_optional_int
# Purpose: Chuyển đổi thành int nhưng cho phép trả về None nếu không có giá trị.
# ------------------------------------------------------------------------------
def _get_optional_int(name: str) -> int | None:
    raw = os.getenv(name)
    if raw is None or not raw.strip():
        return None
    return int(raw)


# ------------------------------------------------------------------------------
# Class: Config
# Purpose: Dataclass chứa toàn bộ thông tin cấu hình (immutable).
# ------------------------------------------------------------------------------
@dataclass(frozen=True)
class Config:
    discord_token: str

    # Cấu hình Lavalink (Music Server)
    lavalink_host: str
    lavalink_port: int
    lavalink_password: str
    lavalink_secure: bool
    lavalink_identifier: str
    wavelink_cache_capacity: int | None

    # Cấu hình Bot
    dev_guild_id: int | None
    default_volume: int
    idle_timeout_seconds: int
    announce_nowplaying: bool
    db_path: str

    # Cấu hình Logging
    log_level: str
    log_dir: str
    log_file: str
    log_max_bytes: int
    log_backup_count: int

    # Cấu hình Meta
    support_invite_url: str | None
    vote_url: str | None

    @property
    def lavalink_uri(self) -> str:
        scheme = "https" if self.lavalink_secure else "http"
        return f"{scheme}://{self.lavalink_host}:{self.lavalink_port}"


# ------------------------------------------------------------------------------
# Function: load_config
# Purpose: Đọc file .env và validate các giá trị bắt buộc.
#          Trả về đối tượng Config hoàn chỉnh.
# ------------------------------------------------------------------------------
def load_config() -> Config:
    load_dotenv(override=False)

    # 1. Discord Token (Bắt buộc)
    discord_token = os.getenv("DISCORD_TOKEN", "").strip()
    if not discord_token:
        raise ValueError("Missing DISCORD_TOKEN in environment")

    # 2. Lavalink Config
    lavalink_host = os.getenv("LAVALINK_HOST", "127.0.0.1").strip() or "127.0.0.1"
    lavalink_port = _get_int("LAVALINK_PORT", 2333)
    lavalink_password = os.getenv("LAVALINK_PASSWORD", "").strip()
    if not lavalink_password:
        raise ValueError("Missing LAVALINK_PASSWORD in environment")

    lavalink_secure = _get_bool("LAVALINK_SECURE", False)
    lavalink_identifier = os.getenv("LAVALINK_IDENTIFIER", "main").strip() or "main"

    raw_cache = os.getenv("WAVELINK_CACHE_CAPACITY")
    wavelink_cache_capacity = int(raw_cache) if raw_cache and raw_cache.strip() else None

    # 3. Bot General Config
    dev_guild_id = _get_optional_int("DEV_GUILD_ID")

    default_volume = _get_int("DEFAULT_VOLUME", 30)
    if not 0 <= default_volume <= 100:
        raise ValueError("DEFAULT_VOLUME must be between 0 and 100")

    idle_timeout_seconds = _get_int("IDLE_TIMEOUT_SECONDS", 300)
    if idle_timeout_seconds < 0:
        raise ValueError("IDLE_TIMEOUT_SECONDS must be >= 0")

    announce_nowplaying = _get_bool("ANNOUNCE_NOWPLAYING", False)

    db_path = os.getenv("DB_PATH", "bot.db").strip() or "bot.db"

    # 4. Logging Config
    log_level = os.getenv("LOG_LEVEL", "INFO").strip() or "INFO"
    log_dir = os.getenv("LOG_DIR", "logs").strip() or "logs"
    log_file = os.getenv("LOG_FILE", "bot.log").strip() or "bot.log"
    log_max_bytes = _get_int("LOG_MAX_BYTES", 5 * 1024 * 1024)
    log_backup_count = _get_int("LOG_BACKUP_COUNT", 5)

    # 5. External Links
    support_invite_url = os.getenv("SUPPORT_INVITE_URL")
    support_invite_url = support_invite_url.strip() if support_invite_url and support_invite_url.strip() else None

    vote_url = os.getenv("VOTE_URL")
    vote_url = vote_url.strip() if vote_url and vote_url.strip() else None

    return Config(
        discord_token=discord_token,
        lavalink_host=lavalink_host,
        lavalink_port=lavalink_port,
        lavalink_password=lavalink_password,
        lavalink_secure=lavalink_secure,
        lavalink_identifier=lavalink_identifier,
        wavelink_cache_capacity=wavelink_cache_capacity,
        dev_guild_id=dev_guild_id,
        default_volume=default_volume,
        idle_timeout_seconds=idle_timeout_seconds,
        announce_nowplaying=announce_nowplaying,
        db_path=db_path,
        log_level=log_level,
        log_dir=log_dir,
        log_file=log_file,
        log_max_bytes=log_max_bytes,
        log_backup_count=log_backup_count,
        support_invite_url=support_invite_url,
        vote_url=vote_url,
    )

