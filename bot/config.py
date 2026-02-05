# ##############################################################################
# MODULE: CONFIG
# DESCRIPTION: Quản lý cấu hình ứng dụng từ biến môi trường (Environment Variables).
#              Sử dụng python-dotenv để load file .env.
# ##############################################################################

from __future__ import annotations

from dataclasses import dataclass
import json
import os
from urllib.parse import urlparse

from dotenv import load_dotenv


_ERR_LAVALINK_NODES_JSON_INVALID = (
    "LAVALINK_NODES_JSON không hợp lệ. Giá trị phải là JSON array. "
    "Gợi ý: bọc toàn bộ bằng dấu nháy đơn trong file .env."
)


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
class LavalinkNodeConfig:
    identifier: str
    host: str
    port: int
    password: str
    secure: bool

    @property
    def uri(self) -> str:
        scheme = "https" if self.secure else "http"
        return f"{scheme}://{self.host}:{self.port}"


@dataclass(frozen=True)
class Config:
    discord_token: str

    # Cấu hình Lavalink (Music Server)
    lavalink_nodes: tuple[LavalinkNodeConfig, ...]
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
        return self.lavalink_nodes[0].uri

    @property
    def lavalink_identifier(self) -> str:
        return self.lavalink_nodes[0].identifier

    @property
    def lavalink_password(self) -> str:
        return self.lavalink_nodes[0].password

    @property
    def lavalink_secure(self) -> bool:
        return self.lavalink_nodes[0].secure

    @property
    def lavalink_host(self) -> str:
        return self.lavalink_nodes[0].host

    @property
    def lavalink_port(self) -> int:
        return self.lavalink_nodes[0].port


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
    # Hỗ trợ multi-node qua JSON để dễ fallback khi public node chết.
    raw_nodes_json = os.getenv("LAVALINK_NODES_JSON")
    raw_nodes_json = raw_nodes_json.strip() if raw_nodes_json and raw_nodes_json.strip() else ""

    lavalink_nodes: list[LavalinkNodeConfig] = []
    if raw_nodes_json:
        try:
            data = json.loads(raw_nodes_json)
        except json.JSONDecodeError as e:
            raise ValueError(_ERR_LAVALINK_NODES_JSON_INVALID) from e

        if not isinstance(data, list) or not data:
            raise ValueError("LAVALINK_NODES_JSON phải là JSON array không rỗng")

        seen: set[str] = set()
        for i, item in enumerate(data, start=1):
            if not isinstance(item, dict):
                raise ValueError(f"LAVALINK_NODES_JSON[{i}] phải là object")

            identifier = str(item.get("identifier") or item.get("id") or f"node{i}").strip()
            if not identifier:
                identifier = f"node{i}"

            if identifier in seen:
                raise ValueError(f"Trùng identifier trong LAVALINK_NODES_JSON: {identifier!r}")
            seen.add(identifier)

            password = str(item.get("password") or "").strip()
            if not password:
                raise ValueError(f"Thiếu password cho node {identifier!r} trong LAVALINK_NODES_JSON")

            # Ưu tiên đọc uri nếu có (đỡ phải tách host/port/secure).
            host = str(item.get("host") or "").strip()
            port_raw = item.get("port")
            secure_raw = item.get("secure")

            uri_raw = item.get("uri") or item.get("url")
            if uri_raw:
                u = urlparse(str(uri_raw).strip())
                scheme = (u.scheme or "").lower()
                if scheme not in {"http", "https"}:
                    raise ValueError(
                        f"Node {identifier!r} có uri scheme không hợp lệ: {scheme!r} (chỉ hỗ trợ http/https)"
                    )
                secure = scheme == "https"
                host = u.hostname or ""
                port = u.port or (443 if secure else 80)
            else:
                secure = bool(secure_raw) if secure_raw is not None else False
                try:
                    port = int(port_raw) if port_raw is not None else 0
                except Exception as e:
                    raise ValueError(f"Port không hợp lệ cho node {identifier!r}: {port_raw!r}") from e

            if not host:
                raise ValueError(f"Thiếu host/uri cho node {identifier!r} trong LAVALINK_NODES_JSON")
            if not (1 <= port <= 65535):
                raise ValueError(f"Port không hợp lệ cho node {identifier!r}: {port}")

            lavalink_nodes.append(
                LavalinkNodeConfig(
                    identifier=identifier,
                    host=host,
                    port=port,
                    password=password,
                    secure=secure,
                )
            )

    else:
        lavalink_host = os.getenv("LAVALINK_HOST", "127.0.0.1").strip() or "127.0.0.1"
        lavalink_port = _get_int("LAVALINK_PORT", 2333)
        lavalink_password = os.getenv("LAVALINK_PASSWORD", "").strip()
        if not lavalink_password:
            raise ValueError("Missing LAVALINK_PASSWORD in environment")

        lavalink_secure = _get_bool("LAVALINK_SECURE", False)
        lavalink_identifier = os.getenv("LAVALINK_IDENTIFIER", "main").strip() or "main"

        lavalink_nodes.append(
            LavalinkNodeConfig(
                identifier=lavalink_identifier,
                host=lavalink_host,
                port=lavalink_port,
                password=lavalink_password,
                secure=lavalink_secure,
            )
        )

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
        lavalink_nodes=tuple(lavalink_nodes),
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
