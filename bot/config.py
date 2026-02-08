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


def _coerce_bool(raw: object, *, field_name: str) -> bool:
    if isinstance(raw, bool):
        return raw

    if isinstance(raw, (int, float)):
        return bool(raw)

    text = str(raw).strip().lower()
    if text in {"1", "true", "yes", "y", "on"}:
        return True
    if text in {"0", "false", "no", "n", "off", ""}:
        return False

    raise ValueError(f"Giá trị boolean không hợp lệ cho {field_name}: {raw!r}")


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
    # primary_lavalink_node: Node đơn lẻ từ LAVALINK_HOST/PORT/... (ưu tiên dùng trước)
    # fallback_lavalink_nodes: Danh sách nodes từ LAVALINK_NODES_JSON (dùng khi primary fail)
    # lavalink_nodes: Tất cả nodes (primary + fallback) - giữ để backward compatible
    primary_lavalink_node: LavalinkNodeConfig | None
    fallback_lavalink_nodes: tuple[LavalinkNodeConfig, ...]
    lavalink_nodes: tuple[LavalinkNodeConfig, ...]
    wavelink_cache_capacity: int | None
    lavalink_node_retries: int
    # Thời gian (giây) kiểm tra lại primary node để chuyển về khi ổn định
    lavalink_primary_health_interval: int

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
    # Chiến lược mới:
    # - primary_node: Từ LAVALINK_HOST/PORT/PASSWORD (ưu tiên dùng trước)
    # - fallback_nodes: Từ LAVALINK_NODES_JSON (dùng khi primary fail)
    # - Nếu chỉ có 1 trong 2, bot vẫn hoạt động bình thường

    # 2a. Load primary node từ LAVALINK_HOST/PORT/... (nếu có)
    primary_node: LavalinkNodeConfig | None = None
    lavalink_host = os.getenv("LAVALINK_HOST", "").strip()
    lavalink_password = os.getenv("LAVALINK_PASSWORD", "").strip()

    if lavalink_host and lavalink_password:
        lavalink_port = _get_int("LAVALINK_PORT", 2333)
        lavalink_secure = _get_bool("LAVALINK_SECURE", False)
        lavalink_identifier = os.getenv("LAVALINK_IDENTIFIER", "primary").strip() or "primary"

        primary_node = LavalinkNodeConfig(
            identifier=lavalink_identifier,
            host=lavalink_host,
            port=lavalink_port,
            password=lavalink_password,
            secure=lavalink_secure,
        )

    # 2b. Load fallback nodes từ LAVALINK_NODES_JSON (nếu có)
    fallback_nodes: list[LavalinkNodeConfig] = []
    raw_nodes_json = os.getenv("LAVALINK_NODES_JSON")
    raw_nodes_json = raw_nodes_json.strip() if raw_nodes_json and raw_nodes_json.strip() else ""

    if raw_nodes_json:
        try:
            data = json.loads(raw_nodes_json)
        except json.JSONDecodeError as e:
            raise ValueError(_ERR_LAVALINK_NODES_JSON_INVALID) from e

        if not isinstance(data, list) or not data:
            raise ValueError("LAVALINK_NODES_JSON phải là JSON array không rỗng")

        seen: set[str] = set()
        # Nếu có primary node, thêm identifier vào seen để tránh trùng
        if primary_node:
            seen.add(primary_node.identifier)

        for i, item in enumerate(data, start=1):
            if not isinstance(item, dict):
                raise ValueError(f"LAVALINK_NODES_JSON[{i}] phải là object")

            identifier = str(item.get("identifier") or item.get("id") or f"fallback{i}").strip()
            if not identifier:
                identifier = f"fallback{i}"

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
                secure = _coerce_bool(secure_raw, field_name=f"{identifier}.secure") if secure_raw is not None else False
                try:
                    port = int(port_raw) if port_raw is not None else 0
                except Exception as e:
                    raise ValueError(f"Port không hợp lệ cho node {identifier!r}: {port_raw!r}") from e

            if not host:
                raise ValueError(f"Thiếu host/uri cho node {identifier!r} trong LAVALINK_NODES_JSON")
            if not (1 <= port <= 65535):
                raise ValueError(f"Port không hợp lệ cho node {identifier!r}: {port}")

            fallback_nodes.append(
                LavalinkNodeConfig(
                    identifier=identifier,
                    host=host,
                    port=port,
                    password=password,
                    secure=secure,
                )
            )

    # 2c. Validate: phải có ít nhất 1 node (primary hoặc fallback)
    if not primary_node and not fallback_nodes:
        raise ValueError(
            "Thiếu cấu hình Lavalink. Cần ít nhất 1 trong 2:\n"
            "- LAVALINK_HOST + LAVALINK_PASSWORD (node đơn lẻ)\n"
            "- LAVALINK_NODES_JSON (multi-node)"
        )

    # 2d. Tạo danh sách tất cả nodes (primary đứng đầu để backward compatible)
    all_nodes: list[LavalinkNodeConfig] = []
    if primary_node:
        all_nodes.append(primary_node)
    all_nodes.extend(fallback_nodes)

    raw_cache = os.getenv("WAVELINK_CACHE_CAPACITY")
    wavelink_cache_capacity = int(raw_cache) if raw_cache and raw_cache.strip() else None

    # Số lần retry khi node Lavalink không kết nối được.
    # Public node hay chết; nếu để None (mặc định của wavelink) có thể treo startup rất lâu.
    lavalink_node_retries = _get_int("LAVALINK_NODE_RETRIES", 2)
    if lavalink_node_retries < 0:
        raise ValueError("LAVALINK_NODE_RETRIES must be >= 0")

    # Thời gian (giây) kiểm tra lại primary node để chuyển về khi ổn định
    # Mặc định 120 giây (2 phút). Set 0 để tắt tính năng này.
    lavalink_primary_health_interval = _get_int("LAVALINK_PRIMARY_HEALTH_INTERVAL", 120)
    if lavalink_primary_health_interval < 0:
        raise ValueError("LAVALINK_PRIMARY_HEALTH_INTERVAL must be >= 0")

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
        primary_lavalink_node=primary_node,
        fallback_lavalink_nodes=tuple(fallback_nodes),
        lavalink_nodes=tuple(all_nodes),
        wavelink_cache_capacity=wavelink_cache_capacity,
        lavalink_node_retries=lavalink_node_retries,
        lavalink_primary_health_interval=lavalink_primary_health_interval,
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
