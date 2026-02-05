# ##############################################################################
# MODULE: HELPERS
# DESCRIPTION: Các hàm tiện ích dùng chung cho toàn bộ codebase.
#              Tập trung các helper bị duplicate ở nhiều file.
# ##############################################################################

from __future__ import annotations

import asyncio
import logging
import time
from typing import TYPE_CHECKING, cast

import discord
from discord.ext import commands

from bot.utils import constants

if TYPE_CHECKING:
    import wavelink


logger = logging.getLogger(__name__)


_LAVALINK_RECONNECT_LOCK = asyncio.Lock()
_LAST_LAVALINK_RECONNECT_AT = 0.0


async def ensure_lavalink_connected(*, timeout_s: float = 8.0, min_interval_s: float = 15.0) -> bool:
    # Đảm bảo Lavalink có ít nhất 1 node CONNECTED.
    #
    # Lý do: Khi websocket Lavalink bị rớt (node CONNECTING/DISCONNECTED), thao tác join voice
    # bằng wavelink.Player thường bị treo và timeout, khiến bot "join rồi văng".
    #
    # Input: timeout_s (thời gian chờ cho thao tác reconnect), min_interval_s (chống spam reconnect)
    # Output: True nếu có node CONNECTED, False nếu chưa sẵn sàng.

    import wavelink

    def _has_connected_node() -> bool:
        try:
            nodes = wavelink.Pool.nodes
        except Exception:
            return False
        return any(n.status is wavelink.NodeStatus.CONNECTED for n in nodes.values())

    if _has_connected_node():
        return True

    # Thử trigger reconnect (nếu đủ thời gian giữa 2 lần thử), sau đó chờ ngắn để node lên CONNECTED.
    global _LAST_LAVALINK_RECONNECT_AT

    do_reconnect = False
    async with _LAVALINK_RECONNECT_LOCK:
        now = time.monotonic()
        if now - _LAST_LAVALINK_RECONNECT_AT >= min_interval_s:
            _LAST_LAVALINK_RECONNECT_AT = now
            do_reconnect = True

    if do_reconnect:
        try:
            await asyncio.wait_for(wavelink.Pool.reconnect(), timeout=timeout_s)
        except Exception:
            logger.exception("Failed to reconnect Lavalink pool")

    deadline = time.monotonic() + max(0.0, timeout_s)
    while time.monotonic() < deadline:
        if _has_connected_node():
            return True
        await asyncio.sleep(0.5)

    return _has_connected_node()


# ------------------------------------------------------------------------------
# Helper: as_member
# Purpose: Chuyển đổi an toàn từ discord.User/abc.User sang discord.Member.
#          Trả về None nếu không phải Member (ví dụ trong DM).
# ------------------------------------------------------------------------------
def as_member(user: discord.abc.User) -> discord.Member | None:
    return user if isinstance(user, discord.Member) else None


# ------------------------------------------------------------------------------
# Helper: author_voice_channel
# Purpose: Lấy voice channel mà user đang kết nối.
#          Trả về None nếu user không trong voice channel.
# ------------------------------------------------------------------------------
def author_voice_channel(
    interaction: discord.Interaction,
) -> discord.VoiceChannel | discord.StageChannel | None:
    member = as_member(interaction.user)
    if member and member.voice and member.voice.channel:
        return member.voice.channel
    return None


# ------------------------------------------------------------------------------
# Helper: is_admin
# Purpose: Kiểm tra user có quyền Administrator hoặc Manage Guild không.
# ------------------------------------------------------------------------------
def is_admin(interaction: discord.Interaction) -> bool:
    member = as_member(interaction.user)
    if not member:
        return False
    perms = member.guild_permissions
    return perms.administrator or perms.manage_guild


# ------------------------------------------------------------------------------
# Helper: is_dj_or_admin
# Purpose: Kiểm tra user có quyền DJ (có DJ role) hoặc Admin.
#          Cần truyền bot để lấy guild settings.
# ------------------------------------------------------------------------------
def is_dj_or_admin(bot: commands.Bot, interaction: discord.Interaction) -> bool:
    if is_admin(interaction):
        return True

    member = as_member(interaction.user)
    if not member or not interaction.guild_id:
        return False

    # Lấy DJ role từ guild settings
    settings_store = getattr(bot, "settings", None)
    if not settings_store:
        return False

    settings = settings_store.get(interaction.guild_id)
    dj_role_id = settings.dj_role_id
    if not dj_role_id:
        # Không có DJ role -> mọi người đều có quyền
        return True

    # Kiểm tra user có DJ role không
    return any(r.id == dj_role_id for r in member.roles)


# ------------------------------------------------------------------------------
# Helper: get_player
# Purpose: Lấy wavelink Player cho guild hiện tại.
#          Nếu connect=True, sẽ tự động kết nối voice nếu chưa có player.
# ------------------------------------------------------------------------------
async def get_player(
    interaction: discord.Interaction,
    *,
    connect: bool,
) -> "wavelink.Player | None":
    import wavelink

    guild = interaction.guild
    if not guild:
        return None

    # Kiểm tra đã có player chưa
    vc = guild.voice_client
    player: wavelink.Player | None = vc if isinstance(vc, wavelink.Player) else None

    if player is not None:
        return player

    if not connect:
        return None

    ok = await ensure_lavalink_connected()
    if not ok:
        logger.warning("Lavalink is not connected; abort voice connect guild=%s", guild.id)
        return None

    # Cần kết nối mới
    vc = author_voice_channel(interaction)
    if not vc:
        return None

    try:
        player = await asyncio.wait_for(
            vc.connect(cls=wavelink.Player, self_deaf=True),  # type: ignore
            timeout=constants.VOICE_CONNECT_TIMEOUT,
        )
    except (asyncio.TimeoutError, wavelink.exceptions.ChannelTimeoutException):
        logger.warning("Voice connect timeout guild=%s", guild.id)
        existing = guild.voice_client
        if existing:
            try:
                await asyncio.wait_for(existing.disconnect(force=True), timeout=constants.PLAYER_OP_TIMEOUT)
            except Exception:
                logger.exception("Failed to disconnect stale voice client guild=%s", guild.id)

            try:
                player = await asyncio.wait_for(
                    vc.connect(cls=wavelink.Player, self_deaf=True),  # type: ignore
                    timeout=constants.VOICE_CONNECT_TIMEOUT,
                )
            except Exception:
                logger.exception("Failed to reconnect voice client guild=%s", guild.id)
                return None
        else:
            return None
    except Exception:
        logger.exception("Failed to connect voice client guild=%s", guild.id)
        return None

    # Thiết lập mặc định tương tự MusicCog để tránh hành vi lệch giữa các lệnh
    bot = getattr(interaction, "client", None)
    config = getattr(bot, "config", None) if bot is not None else None
    settings_store = getattr(bot, "settings", None) if bot is not None else None

    if config is not None and settings_store is not None:
        settings = settings_store.get(guild.id)
        player.autoplay = wavelink.AutoPlayMode.partial
        player.inactive_timeout = config.idle_timeout_seconds
        if interaction.channel:
            setattr(player, "home", interaction.channel)
        try:
            await asyncio.wait_for(
                player.set_volume(settings.volume_default),
                timeout=constants.PLAYER_OP_TIMEOUT,
            )
        except Exception:
            logger.exception("Failed to set initial volume guild=%s", guild.id)

    return player


async def rebuild_player_session(
    bot: commands.Bot,
    interaction: discord.Interaction,
    *,
    channel: discord.VoiceChannel | discord.StageChannel | None = None,
    old: "wavelink.Player | None" = None,
    start_if_idle: bool = True,
) -> "wavelink.Player | None":
    import wavelink

    guild = interaction.guild
    if not guild:
        return None

    if old is None:
        vc = guild.voice_client
        old = vc if isinstance(vc, wavelink.Player) else None

    if channel is None:
        if old and old.channel:
            channel = old.channel
        else:
            member = as_member(interaction.user)
            if member and member.voice and member.voice.channel:
                channel = member.voice.channel

    if channel is None:
        return None

    ok = await ensure_lavalink_connected()
    if not ok:
        logger.warning("Lavalink is not connected; abort rebuild session guild=%s", guild.id)
        return None

    saved_queue: list[wavelink.Playable] = []
    saved_current: wavelink.Playable | None = None
    saved_pos: int = 0
    saved_paused: bool = False
    saved_volume: int | None = None
    saved_mode: wavelink.QueueMode | None = None
    saved_autoplay: wavelink.AutoPlayMode | None = None

    if old:
        saved_queue = list(old.queue)
        saved_current = old.current
        saved_pos = old.position
        saved_paused = old.paused
        saved_volume = old.volume
        saved_mode = cast(wavelink.QueueMode, old.queue.mode)
        saved_autoplay = old.autoplay

        try:
            await asyncio.wait_for(old.disconnect(force=True), timeout=constants.PLAYER_OP_TIMEOUT)
        except Exception:
            logger.exception("Failed to disconnect old player guild=%s", guild.id)

    try:
        player = await asyncio.wait_for(
            channel.connect(cls=wavelink.Player, self_deaf=True),
            timeout=constants.VOICE_CONNECT_TIMEOUT,
        )
    except (asyncio.TimeoutError, wavelink.exceptions.ChannelTimeoutException):
        logger.warning("Rebuild connect timeout guild=%s channel=%s", guild.id, channel.id)
        return None
    except (discord.ClientException, discord.HTTPException):
        logger.exception("Failed to rebuild player guild=%s", guild.id)
        return None

    config = getattr(bot, "config", None)
    settings_store = getattr(bot, "settings", None)
    settings = settings_store.get(guild.id) if settings_store else None

    player.autoplay = saved_autoplay if saved_autoplay is not None else wavelink.AutoPlayMode.partial
    if config is not None:
        player.inactive_timeout = config.idle_timeout_seconds
    if interaction.channel:
        setattr(player, "home", interaction.channel)

    if saved_mode is not None:
        player.queue.mode = saved_mode

    try:
        if saved_volume is not None:
            await asyncio.wait_for(player.set_volume(saved_volume), timeout=constants.PLAYER_OP_TIMEOUT)
        elif settings is not None:
            await asyncio.wait_for(player.set_volume(settings.volume_default), timeout=constants.PLAYER_OP_TIMEOUT)
    except Exception:
        logger.exception("Failed to set volume after rebuild guild=%s", guild.id)

    if saved_queue:
        await player.queue.put_wait(saved_queue)

    volume = saved_volume
    if volume is None and settings is not None:
        volume = settings.volume_default
    if volume is None:
        volume = int(getattr(player, "volume", 100))

    if saved_current is not None:
        try:
            await asyncio.wait_for(
                player.play(
                    saved_current,
                    start=max(0, saved_pos),
                    volume=volume,
                    paused=saved_paused,
                ),
                timeout=constants.PLAYER_OP_TIMEOUT,
            )
        except Exception:
            logger.exception("Failed to resume track after rebuild guild=%s", guild.id)
    elif start_if_idle and player.queue and not player.playing:
        try:
            nxt = player.queue.get()
        except wavelink.QueueEmpty:
            return player

        try:
            await asyncio.wait_for(player.play(nxt, volume=volume), timeout=constants.PLAYER_OP_TIMEOUT)
        except Exception:
            logger.exception("Failed to play after rebuild guild=%s", guild.id)

    return player


# ------------------------------------------------------------------------------
# Helper: ensure_same_channel
# Purpose: Đảm bảo user đang ở cùng voice channel với bot.
#          Trả về True nếu ok, False nếu khác channel.
# ------------------------------------------------------------------------------
def ensure_same_channel(
    interaction: discord.Interaction, player: "wavelink.Player"
) -> bool:
    user_vc = author_voice_channel(interaction)
    if not user_vc:
        return False
    if not player.channel:
        return False
    return user_vc.id == player.channel.id


# ------------------------------------------------------------------------------
# Helper: send_response
# Purpose: Gửi tin nhắn phản hồi, xử lý cả trường hợp đã response rồi.
# ------------------------------------------------------------------------------
async def send_response(
    interaction: discord.Interaction,
    content: str | None = None,
    *,
    embed: discord.Embed | None = None,
    ephemeral: bool = False,
) -> None:
    kwargs: dict = {"ephemeral": ephemeral}
    if content:
        kwargs["content"] = content
    if embed:
        kwargs["embed"] = embed

    if interaction.response.is_done():
        await interaction.followup.send(**kwargs)
    else:
        await interaction.response.send_message(**kwargs)


# Giữ tên _send để tương thích với code cũ
async def _send(interaction: discord.Interaction, content: str | None = None, *, embed: discord.Embed | None = None, ephemeral: bool = False) -> None:
    # Wrapper cho send_response.
    await send_response(interaction, content, embed=embed, ephemeral=ephemeral)


def _author_voice_channel(interaction: discord.Interaction) -> discord.VoiceChannel | discord.StageChannel | None:
    # Alias cho author_voice_channel để tương thích với code cũ.
    return author_voice_channel(interaction)
