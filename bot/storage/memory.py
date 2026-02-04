# ##############################################################################
# MODULE: MEMORY STORAGE
# DESCRIPTION: Quản lý dữ liệu tạm thời (In-Memory) để truy xuất nhanh.
#              Dùng cho Settings của các Guild để tránh query DB liên tục.
# ##############################################################################

from __future__ import annotations

from dataclasses import dataclass


# ------------------------------------------------------------------------------
# Class: GuildSettings
# Purpose: Cấu trúc dữ liệu chứa cấu hình của một server.
# ------------------------------------------------------------------------------
@dataclass
class GuildSettings:
    volume_default: int
    stay_247: bool = False
    announce_enabled: bool = False
    announce_channel_id: int | None = None
    dj_role_id: int | None = None
    filters_preset: str = "off"
    buttons_enabled: bool = True


# ------------------------------------------------------------------------------
# Class: GuildSettingsStore
# Purpose: Kho chứa GuildSettings trong RAM (Dictionary).
# ------------------------------------------------------------------------------
class GuildSettingsStore:
    def __init__(self, *, default_volume: int, default_announce_enabled: bool = False) -> None:
        self._default_volume = default_volume
        self._default_announce_enabled = default_announce_enabled
        self._data: dict[int, GuildSettings] = {}

    # --------------------------------------------------------------------------
    # Method: get
    # Purpose: Lấy setting của guild. Nếu chưa có, tạo mới với giá trị mặc định.
    # --------------------------------------------------------------------------
    def get(self, guild_id: int) -> GuildSettings:
        existing = self._data.get(guild_id)
        if existing:
            return existing

        created = GuildSettings(
            volume_default=self._default_volume,
            announce_enabled=self._default_announce_enabled,
        )
        self._data[guild_id] = created
        return created

    # --------------------------------------------------------------------------
    # Method: set
    # Purpose: Lưu/Ghi đè setting của guild.
    # --------------------------------------------------------------------------
    def set(self, guild_id: int, settings: GuildSettings) -> None:
        self._data[guild_id] = settings

    # --------------------------------------------------------------------------
    # Method: all
    # Purpose: Trả về bản sao của toàn bộ dữ liệu (để debug/save DB).
    # --------------------------------------------------------------------------
    def all(self) -> dict[int, GuildSettings]:
        return self._data.copy()
