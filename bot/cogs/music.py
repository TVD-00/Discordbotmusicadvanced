# ##############################################################################
# MODULE: MUSIC COG
# DESCRIPTION: Cog xử lý toàn bộ logic phát nhạc chính của bot.
#              Bao gồm: join, play, skip, stop, queue management, volume, seeking.
#              Sử dụng thư viện Wavelink để giao tiếp với Lavalink Server.
# ##############################################################################

from __future__ import annotations

import asyncio
import logging
import math
import urllib.parse
from typing import cast

import aiohttp
import discord
from discord import app_commands
from discord.ext import commands
import wavelink

from bot.music.controller import (
    FILTER_PRESETS,
    PlayerControlView,
    apply_filter_preset,
    build_controller_embed,
)
from bot.utils.constants import (
    LYRICS_API_TIMEOUT,
    MAX_LIKED_DISPLAY,
    MAX_LYRICS_LENGTH,
    MAX_PLAYLIST_ADD,
    MAX_PLAYLIST_VIEW,
    MAX_QUEUE_DISPLAY,
    MAX_SAVE_QUEUE,
    MAX_SEARCH_RESULTS,
    PLAYER_OP_TIMEOUT,
    SEARCH_TIMEOUT,
    SEEK_STEP_MS,
    VOLUME_STEP,
    VOICE_CONNECT_TIMEOUT,
    SEARCH_RATE_LIMIT_COUNT,
    SEARCH_RATE_LIMIT_WINDOW,
)
from bot.utils.locks import guild_lock
from bot.utils.helpers import rebuild_player_session
from bot.utils.time import format_ms, parse_time_to_ms

logger = logging.getLogger(__name__)

# Cache lưu trạng thái vote skip: {guild_id: (track_identifier, set_of_user_ids)}
_VOTESKIP: dict[int, tuple[str, set[int]]] = {}

# Rate limiting đơn giản cho search: {user_id: [(timestamp)]}
_SEARCH_RATE_LIMIT: dict[int, list[float]] = {}


def _check_rate_limit(user_id: int) -> tuple[bool, int]:
    """Kiểm tra rate limit cho search. Trả về (allowed, remaining)."""
    import time
    now = time.time()
    
    if user_id not in _SEARCH_RATE_LIMIT:
        _SEARCH_RATE_LIMIT[user_id] = []
    
    # Xóa các request cũ hơn window
    window_start = now - SEARCH_RATE_LIMIT_WINDOW
    _SEARCH_RATE_LIMIT[user_id] = [t for t in _SEARCH_RATE_LIMIT[user_id] if t > window_start]
    
    current_count = len(_SEARCH_RATE_LIMIT[user_id])
    remaining = max(0, SEARCH_RATE_LIMIT_COUNT - current_count)
    
    if current_count >= SEARCH_RATE_LIMIT_COUNT:
        return False, 0
    
    # Thêm request hiện tại
    _SEARCH_RATE_LIMIT[user_id].append(now)
    return True, remaining - 1


# ------------------------------------------------------------------------------
# Helper: _as_member
# Purpose: Chuyển đổi an toàn từ discord.User/abc.User sang discord.Member.
# ------------------------------------------------------------------------------
def _as_member(user: discord.abc.User) -> discord.Member | None:
    return user if isinstance(user, discord.Member) else None


# ------------------------------------------------------------------------------
# Class: MusicCog
# Purpose: Class chứa các lệnh Slash Commands liên quan đến âm nhạc.
# ------------------------------------------------------------------------------
class MusicCog(commands.Cog):
    def __init__(self, bot: commands.Bot) -> None:
        self.bot = bot

    def _settings(self, guild_id: int):
        return getattr(self.bot, "settings").get(guild_id)

    def _config(self):
        return getattr(self.bot, "config")

    # --------------------------------------------------------------------------
    # Helper: _refresh_controller
    # Purpose: Gọi hàm cập nhật giao diện Player (Embed) từ Bot Core.
    # --------------------------------------------------------------------------
    async def _refresh_controller(self, player: wavelink.Player) -> None:
        bot = self.bot
        if not hasattr(bot, "refresh_controller_message"):
            return
        try:
            await getattr(bot, "refresh_controller_message")(player)
        except Exception:
            if player.guild:
                logger.exception("Failed to refresh controller message guild=%s", player.guild.id)

    # --------------------------------------------------------------------------
    # Helper: _send
    # Purpose: Gửi phản hồi message an toàn (check deferred, interaction done).
    # --------------------------------------------------------------------------
    async def _send(
        self,
        interaction: discord.Interaction,
        content: str | None = None,
        *,
        embed: discord.Embed | None = None,
        ephemeral: bool = False,
    ) -> None:
        if content is None and embed is None:
            raise ValueError("content and embed cannot both be None")

        if interaction.response.is_done():
            # Nếu interaction đã được trả lời (deferred hoặc sent), dùng followup hoặc edit
            if interaction.response.type is discord.InteractionResponseType.deferred_channel_message:
                if content is None:
                    assert embed is not None
                    await interaction.edit_original_response(embed=embed, view=None)
                elif embed is None:
                    await interaction.edit_original_response(content=content, embed=None, view=None)
                else:
                    await interaction.edit_original_response(content=content, embed=embed, view=None)
                return

            if content is None:
                assert embed is not None
                await interaction.followup.send(embed=embed, ephemeral=ephemeral)
            elif embed is None:
                await interaction.followup.send(content=content, ephemeral=ephemeral)
            else:
                await interaction.followup.send(content=content, embed=embed, ephemeral=ephemeral)
            return

        # Nếu chưa trả lời, dùng response.send_message
        if content is None:
            assert embed is not None
            await interaction.response.send_message(embed=embed, ephemeral=ephemeral)
        elif embed is None:
            await interaction.response.send_message(content, ephemeral=ephemeral)
        else:
            await interaction.response.send_message(content, embed=embed, ephemeral=ephemeral)

    # --------------------------------------------------------------------------
    # Helper: _is_dj_or_admin
    # Purpose: Kiểm tra quyền điều khiển nhạc (Role DJ hoặc Admin).
    # --------------------------------------------------------------------------
    def _is_dj_or_admin(self, interaction: discord.Interaction) -> bool:
        if not interaction.guild_id:
            return False

        member = _as_member(interaction.user)
        if not member:
            return False

        if member.guild_permissions.administrator or member.guild_permissions.manage_guild:
            return True

        settings = self._settings(interaction.guild_id)
        if not settings.dj_role_id:
            return True

        return any(r.id == settings.dj_role_id for r in member.roles)

    # --------------------------------------------------------------------------
    # Helper: _is_admin
    # Purpose: Kiểm tra quyền quản trị server.
    # --------------------------------------------------------------------------
    def _is_admin(self, interaction: discord.Interaction) -> bool:
        member = _as_member(interaction.user)
        if not member:
            return False
        return member.guild_permissions.administrator or member.guild_permissions.manage_guild

    # --------------------------------------------------------------------------
    # Helper: _author_voice_channel
    # Purpose: Lấy voice channel mà user đang tham gia.
    # --------------------------------------------------------------------------
    def _author_voice_channel(self, interaction: discord.Interaction) -> discord.VoiceChannel | discord.StageChannel | None:
        member = _as_member(interaction.user)
        if not member or not member.voice or not member.voice.channel:
            return None
        return member.voice.channel

    # --------------------------------------------------------------------------
    # Helper: _get_player
    # Purpose: Lấy hoặc khởi tạo Wavelink Player.
    # --------------------------------------------------------------------------
    async def _get_player(self, interaction: discord.Interaction, *, connect: bool) -> wavelink.Player | None:
        guild = interaction.guild
        if not guild:
            return None

        vc = guild.voice_client
        if isinstance(vc, wavelink.Player):
            return vc

        if not connect:
            return None

        channel = self._author_voice_channel(interaction)
        if not channel:
            await self._send(interaction, "Bạn cần vào voice channel trước.", ephemeral=True)
            return None

        try:
            player = await asyncio.wait_for(
                channel.connect(cls=wavelink.Player, self_deaf=True),
                timeout=VOICE_CONNECT_TIMEOUT,
            )
        except (asyncio.TimeoutError, wavelink.exceptions.ChannelTimeoutException):
            logger.warning("Voice connect timeout guild=%s channel=%s", guild.id, channel.id)

            vc = guild.voice_client
            if vc:
                try:
                    await asyncio.wait_for(vc.disconnect(force=True), timeout=PLAYER_OP_TIMEOUT)
                except Exception:
                    logger.exception("Failed to disconnect stale voice client guild=%s", guild.id)

            try:
                player = await asyncio.wait_for(
                    channel.connect(cls=wavelink.Player, self_deaf=True),
                    timeout=VOICE_CONNECT_TIMEOUT,
                )
            except (asyncio.TimeoutError, wavelink.exceptions.ChannelTimeoutException):
                await self._send(
                    interaction,
                    f"Không thể tham gia voice channel sau {VOICE_CONNECT_TIMEOUT}s.",
                    ephemeral=True,
                )
                return None
            except (discord.ClientException, discord.HTTPException):
                await self._send(interaction, "Không thể tham gia voice channel. Vui lòng thử lại.", ephemeral=True)
                return None
        except (discord.ClientException, discord.HTTPException):
            await self._send(interaction, "Không thể tham gia voice channel. Vui lòng thử lại.", ephemeral=True)
            return None

        config = self._config()
        settings = self._settings(guild.id)

        # Cấu hình mặc định cho player mới
        player.autoplay = wavelink.AutoPlayMode.partial
        player.inactive_timeout = config.idle_timeout_seconds

        try:
            await player.set_volume(settings.volume_default)
        except Exception:
            logger.exception("Failed to set initial volume guild=%s", guild.id)

        return player

    # --------------------------------------------------------------------------
    # Helper: _ensure_same_channel
    # Purpose: Đảm bảo user và bot đang ở cùng kênh thoại.
    # --------------------------------------------------------------------------
    async def _ensure_same_channel(self, interaction: discord.Interaction, player: wavelink.Player) -> bool:
        channel = self._author_voice_channel(interaction)
        if not channel:
            await self._send(interaction, "Bạn cần vào voice channel trước.", ephemeral=True)
            return False

        if player.channel != channel:
            await self._send(
                interaction,
                f"Bot đang ở voice channel khác: {player.channel.mention}.",
                ephemeral=True,
            )
            return False

        return True

    # --------------------------------------------------------------------------
    # Helper: _search_select
    # Purpose: Logic tìm kiếm nhạc chung (Spotify/Youtube).
    # --------------------------------------------------------------------------
    async def _search_select(
        self,
        interaction: discord.Interaction,
        query: str,
        *,
        source: str | None = None,
        title: str = "Search",
    ) -> None:
        if not interaction.guild_id:
            await self._send(interaction, "Lệnh này chỉ dùng trong server.", ephemeral=True)
            return

        # Rate limiting check
        allowed, remaining = _check_rate_limit(interaction.user.id)
        if not allowed:
            await self._send(
                interaction, 
                f"Bạn đã search quá nhiều lần. Vui lòng đợi {SEARCH_RATE_LIMIT_WINDOW}s.", 
                ephemeral=True
            )
            return

        await interaction.response.defer(thinking=True, ephemeral=True)

        try:
            if source:
                results: wavelink.Search = await asyncio.wait_for(
                    wavelink.Playable.search(query, source=source),
                    timeout=SEARCH_TIMEOUT,
                )
            else:
                results = await asyncio.wait_for(
                    wavelink.Playable.search(query),
                    timeout=SEARCH_TIMEOUT,
                )
        except asyncio.TimeoutError:
            logger.warning("Search timeout guild=%s query=%r source=%r", interaction.guild_id, query, source)
            await interaction.edit_original_response(
                content="Tìm kiếm quá lâu, vui lòng thử lại.",
                embed=None,
                view=None,
            )
            return
        except Exception:
            logger.exception(
                "Search failed guild=%s query=%r source=%r",
                interaction.guild_id,
                query,
                source,
            )
            await interaction.edit_original_response(content="Không tìm thấy kết quả.", embed=None, view=None)
            return

        if not results:
            await interaction.edit_original_response(content="Không tìm thấy kết quả.", embed=None, view=None)
            return

        requester = _as_member(interaction.user)
        if not requester:
            await interaction.edit_original_response(content="Không xác định được user.", embed=None, view=None)
            return

        # Case 1: Playlist
        if isinstance(results, wavelink.Playlist):
            recovered = False
            async with guild_lock(interaction.guild_id):
                player = await self._get_player(interaction, connect=True)
                if not player:
                    return

                if not await self._ensure_same_channel(interaction, player):
                    return

                extras = {
                    "requester_id": requester.id,
                    "requester_name": requester.display_name,
                }

                results.extras = extras
                added = await player.queue.put_wait(results)
                notice = f"Đã thêm playlist '{results.name}' ({added} bài) vào hàng đợi."

                if not player.playing:
                    try:
                        nxt = player.queue.get()
                    except wavelink.QueueEmpty:
                        await interaction.edit_original_response(content="Hàng đợi trống.", embed=None, view=None)
                        return

                    settings = self._settings(interaction.guild_id)
                    try:
                        await asyncio.wait_for(
                            player.play(nxt, volume=settings.volume_default),
                            timeout=PLAYER_OP_TIMEOUT,
                        )
                    except asyncio.TimeoutError:
                        logger.warning("Play timeout guild=%s", interaction.guild_id)
                        new_player = await rebuild_player_session(self.bot, interaction, old=player)
                        if not new_player:
                            await interaction.edit_original_response(
                                content="Không thể phát nhạc do phiên phát bị treo.",
                                embed=None,
                                view=None,
                            )
                            return
                        player = new_player
                        recovered = True
                    except Exception:
                        logger.exception("Failed to start playback guild=%s", interaction.guild_id)
                        await interaction.edit_original_response(
                            content="Không thể phát nhạc. Vui lòng thử lại.",
                            embed=None,
                            view=None,
                        )
                        return

                await self._refresh_controller(player)

            if recovered:
                extra = "Đã khởi tạo lại phiên phát nhạc do treo."
                notice = f"{notice}\n{extra}" if notice else extra

            embed = build_controller_embed(self.bot, player, notice=notice)
            await interaction.edit_original_response(embed=embed, view=None, content=None)
            return

        # Case 2: Danh sách bài hát (Single Track Search) -> Chọn bài
        tracks = list(results)[:MAX_SEARCH_RESULTS]
        if not tracks:
            await interaction.edit_original_response(content="Không tìm thấy kết quả.", embed=None, view=None)
            return

        embed = discord.Embed(title=title)
        lines: list[str] = []
        for i, t in enumerate(tracks, start=1):
            lines.append(f"{i}. {t.title} - {t.author} ({format_ms(t.length)})")
        embed.description = "\n".join(lines)

        view = SearchResultView(self.bot, tracks, requester_id=requester.id)
        await interaction.edit_original_response(content="Chọn một bài hát:", embed=embed, view=view)

    # --------------------------------------------------------------------------
    # Internal: _do_join
    # Purpose: Logic thực hiện việc join kênh.
    # --------------------------------------------------------------------------
    async def _do_join(self, interaction: discord.Interaction) -> None:
        if not interaction.guild_id:
            await self._send(interaction, "Lệnh này chỉ dùng trong server.", ephemeral=True)
            return

        async with guild_lock(interaction.guild_id):
            channel = self._author_voice_channel(interaction)
            if not channel:
                await self._send(interaction, "Bạn cần vào voice channel trước.", ephemeral=True)
                return

            player = await self._get_player(interaction, connect=False)
            
            # Nếu bot đang ở channel khác, move sang
            if player and player.channel != channel:
                try:
                    await player.move_to(channel)
                except Exception:
                    logger.exception("Failed to move player guild=%s", interaction.guild_id)
                    await self._send(interaction, "Không thể chuyển voice channel.", ephemeral=True)
                    return

                await self._send(interaction, f"Đã chuyển sang {channel.mention}.")
                return

            if player:
                await self._send(interaction, f"Đang ở {channel.mention}.")
                return

            # Nếu bot chưa ở channel nào, connect mới
            player = await self._get_player(interaction, connect=True)
            if not player:
                return

            await self._send(interaction, f"Đã vào {channel.mention}.")

    # --------------------------------------------------------------------------
    # Internal: _do_leave
    # Purpose: Logic thực hiện việc leave kênh.
    # --------------------------------------------------------------------------
    async def _do_leave(self, interaction: discord.Interaction) -> None:
        if not interaction.guild_id:
            await self._send(interaction, "Lệnh này chỉ dùng trong server.", ephemeral=True)
            return

        async with guild_lock(interaction.guild_id):
            player = await self._get_player(interaction, connect=False)
            if not player:
                await self._send(interaction, "Bot chưa ở trong voice channel.", ephemeral=True)
                return

            await player.disconnect()

            if hasattr(self.bot, "mark_controller_message"):
                try:
                    await getattr(self.bot, "mark_controller_message")(
                        interaction.guild_id,
                        notice="Đã rời voice channel. Dùng /play để phát lại.",
                    )
                except Exception:
                    logger.exception("Failed to mark controller message on leave guild=%s", interaction.guild_id)

            await self._send(interaction, "Đã rời voice channel.")

    # ==========================================================================
    # COMMANDS: Connection (Join/Leave)
    # ==========================================================================
    @app_commands.command(name="join", description="Join (hoặc chuyển) vào voice channel của bạn")
    @app_commands.guild_only()
    async def join(self, interaction: discord.Interaction) -> None:
        await self._do_join(interaction)

    @app_commands.command(name="leave", description="Rời voice channel")
    @app_commands.guild_only()
    async def leave(self, interaction: discord.Interaction) -> None:
        await self._do_leave(interaction)

    @app_commands.command(name="connect", description="Connect bot vào voice channel")
    @app_commands.guild_only()
    async def connect(self, interaction: discord.Interaction) -> None:
        await self._do_join(interaction)

    @app_commands.command(name="disconnect", description="Disconnect bot khỏi voice channel")
    @app_commands.guild_only()
    async def disconnect(self, interaction: discord.Interaction) -> None:
        await self._do_leave(interaction)

    # ==========================================================================
    # COMMANDS: Playback (Play/Pause/Stop/Skip)
    # ==========================================================================
    @app_commands.command(name="play", description="Phát nhạc theo tên bài hát hoặc đường dẫn (URL)")
    @app_commands.describe(query="Từ khóa hoặc URL")
    @app_commands.guild_only()
    async def play(self, interaction: discord.Interaction, query: str) -> None:
        if not interaction.guild_id:
            await self._send(interaction, "Lệnh này chỉ dùng trong server.", ephemeral=True)
            return

        # Rate limiting check cho play (vì có thể trigger search)
        allowed, remaining = _check_rate_limit(interaction.user.id)
        if not allowed:
            await self._send(
                interaction, 
                f"Bạn đã search quá nhiều lần. Vui lòng đợi {SEARCH_RATE_LIMIT_WINDOW}s.", 
                ephemeral=True
            )
            return

        await interaction.response.defer(thinking=True)

        requester = _as_member(interaction.user)
        if not requester:
            await self._send(interaction, "Không xác định được user.", ephemeral=True)
            return

        try:
            results: wavelink.Search = await asyncio.wait_for(
                wavelink.Playable.search(query),
                timeout=SEARCH_TIMEOUT,
            )
        except asyncio.TimeoutError:
            logger.warning("Search timeout guild=%s query=%r", interaction.guild_id, query)
            await self._send(interaction, "Tìm kiếm quá lâu, vui lòng thử lại.")
            return
        except Exception:
            logger.exception("Search failed guild=%s query=%r", interaction.guild_id, query)
            await self._send(interaction, "Không tìm được bài hát với query này.", ephemeral=True)
            return

        if not results:
            await self._send(interaction, "Không tìm thấy kết quả.", ephemeral=True)
            return

        notice: str | None = None
        recovered = False

        async with guild_lock(interaction.guild_id):
            player = await self._get_player(interaction, connect=True)
            if not player:
                return

            if not await self._ensure_same_channel(interaction, player):
                return

            if interaction.channel:
                setattr(player, "home", interaction.channel)

            extras = {
                "requester_id": requester.id,
                "requester_name": requester.display_name,
            }

            if isinstance(results, wavelink.Playlist):
                results.extras = extras
                added = await player.queue.put_wait(results)
                notice = f"Đã thêm playlist '{results.name}' ({added} bài) vào hàng đợi."
            else:
                track = results[0]
                track.extras = extras
                await player.queue.put_wait(track)
                notice = f"Đã thêm '{track.title}' vào hàng đợi."

            if not player.playing:
                try:
                    next_track = player.queue.get()
                except wavelink.QueueEmpty:
                    return

                settings = self._settings(interaction.guild_id)
                try:
                    await asyncio.wait_for(
                        player.play(next_track, volume=settings.volume_default),
                        timeout=PLAYER_OP_TIMEOUT,
                    )
                except asyncio.TimeoutError:
                    logger.warning("Play timeout guild=%s", interaction.guild_id)
                    new_player = await rebuild_player_session(self.bot, interaction, old=player)
                    if not new_player:
                        await self._send(interaction, "Không thể phát nhạc do phiên phát bị treo.")
                        return
                    player = new_player
                    recovered = True
                except Exception:
                    logger.exception("Failed to start playback guild=%s", interaction.guild_id)
                    await self._send(interaction, "Không thể phát nhạc. Vui lòng thử lại.")
                    return

        if recovered:
            extra = "Đã khởi tạo lại phiên phát nhạc do treo."
            notice = f"{notice}\n{extra}" if notice else extra

        embed = build_controller_embed(self.bot, player, notice=notice)
        settings = self._settings(interaction.guild_id)
        view = PlayerControlView(self.bot) if settings.buttons_enabled else None
        message = await interaction.edit_original_response(embed=embed, view=view)
        try:
            channel_id = interaction.channel_id
            if channel_id is not None:
                getattr(self.bot, "controller_messages")[interaction.guild_id] = (channel_id, message.id)
        except Exception:
            pass

    @app_commands.command(name="playfile", description="Phát nhạc từ tệp đính kèm")
    @app_commands.describe(file="File đính kèm")
    @app_commands.guild_only()
    async def playfile(self, interaction: discord.Interaction, file: discord.Attachment) -> None:
        if not interaction.guild_id:
            await self._send(interaction, "Lệnh này chỉ dùng trong server.", ephemeral=True)
            return

        await interaction.response.defer(thinking=True)

        query = file.url

        requester = _as_member(interaction.user)
        if not requester:
            await self._send(interaction, "Không xác định được user.", ephemeral=True)
            return

        try:
            results: wavelink.Search = await asyncio.wait_for(
                wavelink.Playable.search(query),
                timeout=SEARCH_TIMEOUT,
            )
        except asyncio.TimeoutError:
            logger.warning("Search timeout guild=%s query=%r", interaction.guild_id, query)
            await self._send(interaction, "Tìm kiếm quá lâu, vui lòng thử lại.")
            return
        except Exception:
            logger.exception("Search failed guild=%s query=%r", interaction.guild_id, query)
            await self._send(interaction, "Không tìm được file để phát.", ephemeral=True)
            return

        if not results:
            await self._send(interaction, "Không tìm thấy kết quả.", ephemeral=True)
            return

        notice: str | None = None
        recovered = False

        async with guild_lock(interaction.guild_id):
            player = await self._get_player(interaction, connect=True)
            if not player:
                return

            if not await self._ensure_same_channel(interaction, player):
                return

            if interaction.channel:
                setattr(player, "home", interaction.channel)

            extras = {
                "requester_id": requester.id,
                "requester_name": requester.display_name,
            }

            if isinstance(results, wavelink.Playlist):
                results.extras = extras
                added = await player.queue.put_wait(results)
                notice = f"Đã thêm playlist '{results.name}' ({added} bài) vào hàng đợi."
            else:
                track = results[0]
                track.extras = extras
                await player.queue.put_wait(track)
                notice = f"Đã thêm '{track.title}' vào hàng đợi."

            if not player.playing:
                try:
                    next_track = player.queue.get()
                except wavelink.QueueEmpty:
                    return

                settings = self._settings(interaction.guild_id)
                try:
                    await asyncio.wait_for(
                        player.play(next_track, volume=settings.volume_default),
                        timeout=PLAYER_OP_TIMEOUT,
                    )
                except asyncio.TimeoutError:
                    logger.warning("Play timeout guild=%s", interaction.guild_id)
                    new_player = await rebuild_player_session(self.bot, interaction, old=player)
                    if not new_player:
                        await self._send(interaction, "Không thể phát nhạc do phiên phát bị treo.")
                        return
                    player = new_player
                    recovered = True
                except Exception:
                    logger.exception("Failed to start playback guild=%s", interaction.guild_id)
                    await self._send(interaction, "Không thể phát nhạc. Vui lòng thử lại.")
                    return

        if recovered:
            extra = "Đã khởi tạo lại phiên phát nhạc do treo."
            notice = f"{notice}\n{extra}" if notice else extra

        embed = build_controller_embed(self.bot, player, notice=notice)
        settings = self._settings(interaction.guild_id)
        view = PlayerControlView(self.bot) if settings.buttons_enabled else None
        message = await interaction.edit_original_response(embed=embed, view=view)
        try:
            channel_id = interaction.channel_id
            if channel_id is not None:
                getattr(self.bot, "controller_messages")[interaction.guild_id] = (channel_id, message.id)
        except Exception:
            pass

    @app_commands.command(name="search", description="Tìm kiếm bài hát")
    @app_commands.describe(query="Từ khóa hoặc URL")
    @app_commands.guild_only()
    async def search(self, interaction: discord.Interaction, query: str) -> None:
        await self._search_select(interaction, query, title="Tìm kiếm")

    @app_commands.command(name="spotify", description="Tìm và phát từ Spotify (cần LavaSrc)")
    @app_commands.describe(query="Spotify query hoặc URL")
    @app_commands.guild_only()
    async def spotify(self, interaction: discord.Interaction, query: str) -> None:
        await self._search_select(interaction, query, source="spsearch", title="Tìm kiếm Spotify")

    @app_commands.command(name="searchalbum", description="Tìm kiếm album từ Spotify")
    @app_commands.describe(query="Album query")
    @app_commands.guild_only()
    async def searchalbum(self, interaction: discord.Interaction, query: str) -> None:
        await self._search_select(interaction, query, source="spsearch", title="Tìm kiếm Album Spotify")

    @app_commands.command(name="searchartist", description="Tìm và phát nhạc của nghệ sĩ")
    @app_commands.describe(query="Artist query")
    @app_commands.guild_only()
    async def searchartist(self, interaction: discord.Interaction, query: str) -> None:
        await self._search_select(interaction, query, source="spsearch", title="Tìm kiếm Nghệ sĩ Spotify")

    @app_commands.command(name="searchplaylist", description="Tìm playlist công khai từ Spotify")
    @app_commands.describe(query="Playlist query hoặc URL")
    @app_commands.guild_only()
    async def searchplaylist(self, interaction: discord.Interaction, query: str) -> None:
        await self._search_select(interaction, query, source="spsearch", title="Tìm kiếm Playlist Spotify")

    @app_commands.command(name="pause", description="Tạm dừng bài đang phát")
    @app_commands.guild_only()
    async def pause(self, interaction: discord.Interaction) -> None:
        if not interaction.guild_id:
            await self._send(interaction, "Lệnh này chỉ dùng trong server.", ephemeral=True)
            return

        async with guild_lock(interaction.guild_id):
            player = await self._get_player(interaction, connect=False)
            if not player or not player.playing:
                await self._send(interaction, "Không có bài đang phát.", ephemeral=True)
                return

            await player.pause(True)
            await self._refresh_controller(player)
            await self._send(interaction, "Đã tạm dừng.")

    @app_commands.command(name="resume", description="Tiếp tục phát nhạc")
    @app_commands.guild_only()
    async def resume(self, interaction: discord.Interaction) -> None:
        if not interaction.guild_id:
            await self._send(interaction, "Lệnh này chỉ dùng trong server.", ephemeral=True)
            return

        async with guild_lock(interaction.guild_id):
            player = await self._get_player(interaction, connect=False)
            if not player or not player.playing:
                await self._send(interaction, "Không có bài đang phát.", ephemeral=True)
                return

            await player.pause(False)
            await self._refresh_controller(player)
            await self._send(interaction, "Đã tiếp tục.")

    @app_commands.command(name="stop", description="Dừng nhạc và xóa hàng đợi")
    @app_commands.guild_only()
    async def stop(self, interaction: discord.Interaction) -> None:
        if not interaction.guild_id:
            await self._send(interaction, "Lệnh này chỉ dùng trong server.", ephemeral=True)
            return

        if not self._is_dj_or_admin(interaction):
            await self._send(interaction, "Bạn không có quyền dùng lệnh này.", ephemeral=True)
            return

        async with guild_lock(interaction.guild_id):
            player = await self._get_player(interaction, connect=False)
            if not player:
                await self._send(interaction, "Bot chưa ở trong voice channel.", ephemeral=True)
                return

            player.queue.reset()
            player.autoplay = wavelink.AutoPlayMode.partial
            await player.skip(force=True)
            await self._refresh_controller(player)
            await self._send(interaction, "Đã dừng phát và xóa hàng đợi.")

    @app_commands.command(name="skip", description="Bỏ qua bài hiện tại")
    @app_commands.guild_only()
    async def skip(self, interaction: discord.Interaction) -> None:
        if not interaction.guild_id:
            await self._send(interaction, "Lệnh này chỉ dùng trong server.", ephemeral=True)
            return

        if not self._is_dj_or_admin(interaction):
            await self._send(interaction, "Bạn không có quyền dùng lệnh này.", ephemeral=True)
            return

        async with guild_lock(interaction.guild_id):
            player = await self._get_player(interaction, connect=False)
            if not player or not player.playing or not player.current:
                await self._send(interaction, "Không có bài đang phát.", ephemeral=True)
                return

            old = player.current
            await player.skip(force=True)
            await self._refresh_controller(player)
            await self._send(interaction, f"Đã skip '{old.title}'.")

    @app_commands.command(name="voteskip", description="Bỏ phiếu để bỏ qua bài hát")
    @app_commands.describe(index="(Tuỳ chọn) Skip tới bài số N trong queue")
    @app_commands.guild_only()
    async def voteskip(self, interaction: discord.Interaction, index: int | None = None) -> None:
        if not interaction.guild_id:
            await self._send(interaction, "Lệnh này chỉ dùng trong server.", ephemeral=True)
            return

        async with guild_lock(interaction.guild_id):
            player = await self._get_player(interaction, connect=False)
            if not player or not player.current:
                await self._send(interaction, "Không có bài đang phát.", ephemeral=True)
                return

            if not await self._ensure_same_channel(interaction, player):
                return

            humans = [m for m in player.channel.members if not m.bot]
            needed = max(1, math.ceil(len(humans) / 2))

            key = f"{player.current.identifier}:{int(index or 0)}"
            state = _VOTESKIP.get(interaction.guild_id)
            if not state or state[0] != key:
                state = (key, set())
                _VOTESKIP[interaction.guild_id] = state

            votes = state[1]
            votes.add(interaction.user.id)

            if len(votes) < needed:
                await self._send(interaction, f"Đã vote skip: {len(votes)}/{needed}", ephemeral=True)
                return

            _VOTESKIP.pop(interaction.guild_id, None)

            if index is not None:
                if not player.queue:
                    await self._send(interaction, "Hàng đợi đang trống.", ephemeral=True)
                    return

                idx = int(index) - 1
                if idx < 0 or idx >= len(player.queue):
                    await self._send(interaction, "Index không hợp lệ.", ephemeral=True)
                    return

                upcoming = list(player.queue)
                target = upcoming[idx]
                keep = upcoming[idx + 1 :]

                player.queue.clear()
                if keep:
                    await player.queue.put_wait(keep)

                try:
                    await asyncio.wait_for(
                        player.play(target, replace=True, volume=player.volume),
                        timeout=PLAYER_OP_TIMEOUT,
                    )
                except asyncio.TimeoutError:
                    logger.warning("Play timeout guild=%s", interaction.guild_id)
                    new_player = await rebuild_player_session(self.bot, interaction, old=player)
                    if not new_player:
                        await self._send(interaction, "Không thể phát nhạc do phiên phát bị treo.")
                        return
                    player = new_player
                    try:
                        await asyncio.wait_for(
                            player.play(target, replace=True, volume=player.volume),
                            timeout=PLAYER_OP_TIMEOUT,
                        )
                    except Exception:
                        logger.exception("Failed to start playback guild=%s", interaction.guild_id)
                        await self._send(interaction, "Không thể phát nhạc. Vui lòng thử lại.")
                        return
                except Exception:
                    logger.exception("Failed to start playback guild=%s", interaction.guild_id)
                    await self._send(interaction, "Không thể phát nhạc. Vui lòng thử lại.")
                    return

                await self._refresh_controller(player)
                await self._send(interaction, f"Bỏ phiếu thành công. Đã chuyển tới '{target.title}'.")
                return

            old = player.current
            await player.skip(force=True)
            await self._refresh_controller(player)
            await self._send(interaction, f"Bỏ phiếu thành công. Đã bỏ qua '{old.title}'.")

    @app_commands.command(name="seek", description="Tua đến vị trí chỉ định (vd: 1:23 hoặc 90s)")
    @app_commands.describe(time="mm:ss | hh:mm:ss | seconds")
    @app_commands.guild_only()
    async def seek(self, interaction: discord.Interaction, time: str) -> None:
        if not interaction.guild_id:
            await self._send(interaction, "Lệnh này chỉ dùng trong server.", ephemeral=True)
            return

        if not self._is_dj_or_admin(interaction):
            await self._send(interaction, "Bạn không có quyền dùng lệnh này.", ephemeral=True)
            return

        async with guild_lock(interaction.guild_id):
            player = await self._get_player(interaction, connect=False)
            if not player or not player.current:
                await self._send(interaction, "Không có bài đang phát.", ephemeral=True)
                return

            if not player.current.is_seekable:
                await self._send(interaction, "Bài này không hỗ trợ seek.", ephemeral=True)
                return

            try:
                ms = parse_time_to_ms(time)
            except ValueError:
                await self._send(interaction, "Định dạng thời gian không hợp lệ.", ephemeral=True)
                return

            ms = max(0, min(ms, player.current.length))
            await player.seek(ms)
            await self._refresh_controller(player)
            await self._send(interaction, f"Đã seek tới {format_ms(ms)}.")

    @app_commands.command(name="volume", description="Điều chỉnh âm lượng (0-100)")
    @app_commands.describe(value="0-100")
    @app_commands.guild_only()
    async def volume(self, interaction: discord.Interaction, value: int) -> None:
        if not interaction.guild_id:
            await self._send(interaction, "Lệnh này chỉ dùng trong server.", ephemeral=True)
            return

        if not self._is_dj_or_admin(interaction):
            await self._send(interaction, "Bạn không có quyền dùng lệnh này.", ephemeral=True)
            return

        value = max(0, min(int(value), 100))

        async with guild_lock(interaction.guild_id):
            player = await self._get_player(interaction, connect=False)
            if not player:
                await self._send(interaction, "Bot chưa ở trong voice channel.", ephemeral=True)
                return

            await player.set_volume(value)
            await self._refresh_controller(player)
            await self._send(interaction, f"Đã chỉnh âm lượng: {value}.")

    @app_commands.command(name="nowplaying", description="Xem thông tin bài đang phát")
    @app_commands.guild_only()
    async def nowplaying(self, interaction: discord.Interaction) -> None:
        if not interaction.guild_id:
            await self._send(interaction, "Lệnh này chỉ dùng trong server.", ephemeral=True)
            return

        async with guild_lock(interaction.guild_id):
            player = await self._get_player(interaction, connect=False)
            if not player or not player.current:
                await self._send(interaction, "Không có bài đang phát.", ephemeral=True)
                return

            track = player.current
            pos = player.position
            embed = discord.Embed(title="Đang phát")
            embed.description = f"{track.title} - {track.author}"
            if track.uri:
                embed.url = track.uri
            embed.add_field(name="Thời gian", value=f"{format_ms(pos)} / {format_ms(track.length)}", inline=False)
            await self._send(interaction, embed=embed)

    @app_commands.command(name="lyrics", description="Xem lời bài hát đang phát")
    @app_commands.guild_only()
    async def lyrics(self, interaction: discord.Interaction) -> None:
        if not interaction.guild_id:
            await self._send(interaction, "Lệnh này chỉ dùng trong server.", ephemeral=True)
            return

        await interaction.response.defer(thinking=True, ephemeral=True)

        async with guild_lock(interaction.guild_id):
            player = await self._get_player(interaction, connect=False)
            if not player or not player.current:
                await interaction.edit_original_response(content="Không có bài đang phát.")
                return

            track = player.current
            artist = (track.author or "").strip()
            title = (track.title or "").strip()

        if not artist or not title:
            await interaction.edit_original_response(content="Không đủ thông tin để lấy lyrics.")
            return

        url = f"https://api.lyrics.ovh/v1/{urllib.parse.quote(artist)}/{urllib.parse.quote(title)}"

        try:
            timeout = aiohttp.ClientTimeout(total=LYRICS_API_TIMEOUT)
            async with aiohttp.ClientSession(timeout=timeout) as session:
                async with session.get(url) as resp:
                    if resp.status != 200:
                        await interaction.edit_original_response(content="Không tìm thấy lyrics.")
                        return
                    data = await resp.json()
        except Exception:
            await interaction.edit_original_response(content="Không thể lấy lyrics (lỗi mạng).")
            return

        text = str(data.get("lyrics", "") or "").strip()
        if not text:
            await interaction.edit_original_response(content="Không tìm thấy lyrics.")
            return

        if len(text) <= MAX_LYRICS_LENGTH:
            await interaction.edit_original_response(content=f"```\n{text}\n```")
            return

        import io

        fp = io.BytesIO(text.encode("utf-8"))
        fp.seek(0)
        file = discord.File(fp, filename="lyrics.txt")
        await interaction.edit_original_response(content="Lyrics dài, gửi kèm file.")
        await interaction.followup.send(file=file, ephemeral=True)

    @app_commands.command(name="queue", description="Xem danh sách hàng đợi")
    @app_commands.guild_only()
    async def queue(self, interaction: discord.Interaction) -> None:
        if not interaction.guild_id:
            await self._send(interaction, "Lệnh này chỉ dùng trong server.", ephemeral=True)
            return

        async with guild_lock(interaction.guild_id):
            player = await self._get_player(interaction, connect=False)
            if not player:
                await self._send(interaction, "Bot chưa ở trong voice channel.", ephemeral=True)
                return

            mode = cast(wavelink.QueueMode, player.queue.mode)
            loop_text = {
                wavelink.QueueMode.normal: "Tắt",
                wavelink.QueueMode.loop: "Bài hiện tại",
                wavelink.QueueMode.loop_all: "Toàn bộ",
            }.get(mode, "Tắt")

            embed = discord.Embed(title="Hàng đợi")
            embed.add_field(name="Lặp lại", value=loop_text, inline=True)
            embed.add_field(name="Số lượng", value=str(len(player.queue)), inline=True)

            if player.current:
                embed.add_field(
                    name="Đang phát",
                    value=f"{player.current.title} ({format_ms(player.current.length)})",
                    inline=False,
                )

            if player.queue:
                lines: list[str] = []
                for i, t in enumerate(list(player.queue)[:MAX_QUEUE_DISPLAY], start=1):
                    lines.append(f"{i}. {t.title} ({format_ms(t.length)})")
                embed.add_field(name="Tiếp theo", value="\n".join(lines), inline=False)
            else:
                embed.add_field(name="Tiếp theo", value="(trống)", inline=False)

            await self._send(interaction, embed=embed)

    @app_commands.command(name="remove", description="Xóa bài khỏi hàng đợi theo số thứ tự")
    @app_commands.describe(index="1 là bài kế tiếp")
    @app_commands.guild_only()
    async def remove(self, interaction: discord.Interaction, index: int) -> None:
        if not interaction.guild_id:
            await self._send(interaction, "Lệnh này chỉ dùng trong server.", ephemeral=True)
            return

        if not self._is_dj_or_admin(interaction):
            await self._send(interaction, "Bạn không có quyền dùng lệnh này.", ephemeral=True)
            return

        async with guild_lock(interaction.guild_id):
            player = await self._get_player(interaction, connect=False)
            if not player:
                await self._send(interaction, "Bot chưa ở trong voice channel.", ephemeral=True)
                return

            if not player.queue:
                await self._send(interaction, "Hàng đợi đang trống.", ephemeral=True)
                return

            idx = int(index) - 1
            if idx < 0 or idx >= len(player.queue):
                await self._send(interaction, "Index không hợp lệ.", ephemeral=True)
                return

            track = player.queue[idx]
            player.queue.delete(idx)
            await self._refresh_controller(player)
            await self._send(interaction, f"Đã xóa '{track.title}' khỏi hàng đợi.")

    @app_commands.command(name="move", description="Di chuyển vị trí bài trong hàng đợi")
    @app_commands.describe(from_index="Index nguồn (1 là bài kế tiếp)", to_index="Index đích (1 là bài kế tiếp)")
    @app_commands.guild_only()
    async def move(self, interaction: discord.Interaction, from_index: int, to_index: int) -> None:
        if not interaction.guild_id:
            await self._send(interaction, "Lệnh này chỉ dùng trong server.", ephemeral=True)
            return

        if not self._is_dj_or_admin(interaction):
            await self._send(interaction, "Bạn không có quyền dùng lệnh này.", ephemeral=True)
            return

        async with guild_lock(interaction.guild_id):
            player = await self._get_player(interaction, connect=False)
            if not player:
                await self._send(interaction, "Bot chưa ở trong voice channel.", ephemeral=True)
                return

            if not player.queue:
                await self._send(interaction, "Hàng đợi đang trống.", ephemeral=True)
                return

            src = int(from_index) - 1
            dst = int(to_index) - 1

            if src < 0 or src >= len(player.queue) or dst < 0 or dst >= len(player.queue):
                await self._send(interaction, "Index không hợp lệ.", ephemeral=True)
                return

            track = player.queue[src]
            player.queue.delete(src)
            player.queue.put_at(dst, track)
            await self._refresh_controller(player)
            await self._send(interaction, f"Đã chuyển '{track.title}' tới vị trí {to_index}.")

    @app_commands.command(name="clear", description="Xóa toàn bộ bài trong hàng đợi")
    @app_commands.guild_only()
    async def clear(self, interaction: discord.Interaction) -> None:
        if not interaction.guild_id:
            await self._send(interaction, "Lệnh này chỉ dùng trong server.", ephemeral=True)
            return

        if not self._is_dj_or_admin(interaction):
            await self._send(interaction, "Bạn không có quyền dùng lệnh này.", ephemeral=True)
            return

        async with guild_lock(interaction.guild_id):
            player = await self._get_player(interaction, connect=False)
            if not player:
                await self._send(interaction, "Bot chưa ở trong voice channel.", ephemeral=True)
                return

            player.queue.clear()
            await self._refresh_controller(player)
            await self._send(interaction, "Đã xóa hàng đợi.")

    @app_commands.command(name="shuffle", description="Trộn ngẫu nhiên hàng đợi")
    @app_commands.guild_only()
    async def shuffle(self, interaction: discord.Interaction) -> None:
        if not interaction.guild_id:
            await self._send(interaction, "Lệnh này chỉ dùng trong server.", ephemeral=True)
            return

        async with guild_lock(interaction.guild_id):
            player = await self._get_player(interaction, connect=False)
            if not player:
                await self._send(interaction, "Bot chưa ở trong voice channel.", ephemeral=True)
                return

            if not player.queue:
                await self._send(interaction, "Hàng đợi đang trống.", ephemeral=True)
                return

            player.queue.shuffle()
            await self._refresh_controller(player)
            await self._send(interaction, "Đã trộn hàng đợi.")

    @app_commands.command(name="loop", description="Chỉnh chế độ lặp lại (bài/hàng đợi)")
    @app_commands.describe(mode="off | track | queue")
    @app_commands.guild_only()
    async def loop(self, interaction: discord.Interaction, mode: str) -> None:
        if not interaction.guild_id:
            await self._send(interaction, "Lệnh này chỉ dùng trong server.", ephemeral=True)
            return

        mode = mode.strip().lower()
        mapping = {
            "off": wavelink.QueueMode.normal,
            "track": wavelink.QueueMode.loop,
            "queue": wavelink.QueueMode.loop_all,
        }
        if mode not in mapping:
            await self._send(interaction, "Mode không hợp lệ: off | track | queue", ephemeral=True)
            return

        async with guild_lock(interaction.guild_id):
            player = await self._get_player(interaction, connect=False)
            if not player:
                await self._send(interaction, "Bot chưa ở trong voice channel.", ephemeral=True)
                return

            player.queue.mode = mapping[mode]
            await self._refresh_controller(player)
            
            mode_vn = {
                "off": "Tắt",
                "track": "Bài hiện tại",
                "queue": "Toàn bộ"
            }.get(mode, mode)
            
            await self._send(interaction, f"Đã chỉnh lặp lại: {mode_vn}.")

    @app_commands.command(name="247", description="Bật/tắt chế độ 24/7 (không tự rời kênh thoại)")
    @app_commands.describe(mode="on | off")
    @app_commands.guild_only()
    async def stay_247(self, interaction: discord.Interaction, mode: str) -> None:
        if not interaction.guild_id:
            await self._send(interaction, "Lệnh này chỉ dùng trong server.", ephemeral=True)
            return

        member = _as_member(interaction.user)
        if not member or not (member.guild_permissions.administrator or member.guild_permissions.manage_guild):
            await self._send(interaction, "Bạn không có quyền dùng lệnh này.", ephemeral=True)
            return

        mode = mode.strip().lower()
        if mode not in {"on", "off"}:
            await self._send(interaction, "Mode không hợp lệ: on | off", ephemeral=True)
            return

        settings = self._settings(interaction.guild_id)
        settings.stay_247 = mode == "on"

        if hasattr(self.bot, "storage"):
            try:
                await getattr(self.bot, "storage").upsert_guild_settings(interaction.guild_id, settings)
            except Exception:
                logger.exception("Failed to persist stay_247 guild=%s", interaction.guild_id)

        player = await self._get_player(interaction, connect=False)
        if player:
            await self._refresh_controller(player)
        await self._send(interaction, f"Chế độ 24/7 đã {'bật' if mode == 'on' else 'tắt'}.")

    @app_commands.command(name="forcefix", description="Sửa lỗi player (tham gia lại kênh)")
    @app_commands.guild_only()
    async def forcefix(self, interaction: discord.Interaction) -> None:
        if not interaction.guild_id:
            await self._send(interaction, "Lệnh này chỉ dùng trong server.", ephemeral=True)
            return

        member = _as_member(interaction.user)
        if not member or not (member.guild_permissions.administrator or member.guild_permissions.manage_guild):
            await self._send(interaction, "Bạn không có quyền dùng lệnh này.", ephemeral=True)
            return

        await interaction.response.defer(thinking=True)

        async with guild_lock(interaction.guild_id):
            channel = self._author_voice_channel(interaction)
            if not channel:
                await self._send(interaction, "Bạn cần vào voice channel trước.", ephemeral=True)
                return

            player = await rebuild_player_session(self.bot, interaction, channel=channel)
            if not player:
                await self._send(
                    interaction,
                    f"Không thể tham gia voice channel sau {VOICE_CONNECT_TIMEOUT}s.",
                    ephemeral=True,
                )
                return

            settings = self._settings(interaction.guild_id)
            embed = build_controller_embed(self.bot, player, notice="Đã sửa lỗi trình phát.")
            view = PlayerControlView(self.bot) if settings.buttons_enabled else None
            message = await interaction.edit_original_response(embed=embed, view=view)

            try:
                channel_id = interaction.channel_id
                if channel_id is not None:
                    getattr(self.bot, "controller_messages")[interaction.guild_id] = (channel_id, message.id)
            except Exception:
                pass

    @app_commands.command(name="switchaudionode", description="Chuyển đổi máy chủ phát nhạc (multi-node)")
    @app_commands.describe(identifier="Node identifier")
    @app_commands.guild_only()
    async def switchaudionode(self, interaction: discord.Interaction, identifier: str) -> None:
        if not interaction.guild_id:
            await self._send(interaction, "Lệnh này chỉ dùng trong server.", ephemeral=True)
            return

        member = _as_member(interaction.user)
        if not member or not (member.guild_permissions.administrator or member.guild_permissions.manage_guild):
            await self._send(interaction, "Bạn không có quyền dùng lệnh này.", ephemeral=True)
            return

        identifier = identifier.strip()
        nodes = wavelink.Pool.nodes
        if len(nodes) < 2:
            await self._send(interaction, "Hiện chỉ có 1 node.", ephemeral=True)
            return

        if identifier not in nodes:
            available = ", ".join(sorted(nodes.keys()))
            await self._send(interaction, f"Node không hợp lệ. Available: {available}", ephemeral=True)
            return

        async with guild_lock(interaction.guild_id):
            player = await self._get_player(interaction, connect=False)
            if not player:
                await self._send(interaction, "Bot chưa ở trong voice channel.", ephemeral=True)
                return

            if not await self._ensure_same_channel(interaction, player):
                return

            switch_node = getattr(player, "switch_node", None)
            # wavelink < 3.5 không có API switch_node
            if switch_node is None:
                await self._send(
                    interaction,
                    "Phiên bản wavelink hiện tại không hỗ trợ chuyển node. Hãy nâng wavelink lên >= 3.5.",
                    ephemeral=True,
                )
                return

            try:
                await switch_node(nodes[identifier])  # type: ignore[misc]
            except Exception:
                logger.exception("Failed to switch node guild=%s node=%r", interaction.guild_id, identifier)
                await self._send(interaction, "Không thể switch node.", ephemeral=True)
                return

            await self._refresh_controller(player)
            await self._send(interaction, f"Đã switch node -> {identifier}.")

    @app_commands.command(name="autoplay", description="Tự động phát bài liên quan khi hết hàng đợi")
    @app_commands.describe(mode="on | off")
    @app_commands.guild_only()
    async def autoplay(self, interaction: discord.Interaction, mode: str) -> None:
        if not interaction.guild_id:
            await self._send(interaction, "Lệnh này chỉ dùng trong server.", ephemeral=True)
            return

        if not self._is_dj_or_admin(interaction):
            await self._send(interaction, "Bạn không có quyền dùng lệnh này.", ephemeral=True)
            return

        mode = mode.strip().lower()
        if mode not in {"on", "off"}:
            await self._send(interaction, "Mode không hợp lệ: on | off", ephemeral=True)
            return

        async with guild_lock(interaction.guild_id):
            player = await self._get_player(interaction, connect=False)
            if not player:
                await self._send(interaction, "Bot chưa ở trong voice channel.", ephemeral=True)
                return

            player.autoplay = wavelink.AutoPlayMode.enabled if mode == "on" else wavelink.AutoPlayMode.partial
            await self._refresh_controller(player)
            await self._send(interaction, f"Tự động phát: {'Bật' if mode == 'on' else 'Tắt'}.")

    @app_commands.command(name="resetfilter", description="Đặt lại bộ lọc âm thanh")
    @app_commands.guild_only()
    async def resetfilter(self, interaction: discord.Interaction) -> None:
        if not interaction.guild_id:
            await self._send(interaction, "Lệnh này chỉ dùng trong server.", ephemeral=True)
            return

        if not self._is_dj_or_admin(interaction):
            await self._send(interaction, "Bạn không có quyền dùng lệnh này.", ephemeral=True)
            return

        async with guild_lock(interaction.guild_id):
            player = await self._get_player(interaction, connect=False)
            if not player:
                await self._send(interaction, "Bot chưa ở trong voice channel.", ephemeral=True)
                return

            try:
                await asyncio.wait_for(
                    apply_filter_preset(self.bot, player, "off"),
                    timeout=PLAYER_OP_TIMEOUT,
                )
            except asyncio.TimeoutError:
                logger.warning("Reset filter timeout guild=%s", interaction.guild_id)
                new_player = await rebuild_player_session(self.bot, interaction, old=player)
                if not new_player:
                    await self._send(interaction, "Không thể đặt lại bộ lọc do phiên phát bị treo.")
                    return
                player = new_player
            except Exception:
                logger.exception("Failed to reset filter guild=%s", interaction.guild_id)
                await self._send(interaction, "Không thể đặt lại bộ lọc.", ephemeral=True)
                return

            await self._refresh_controller(player)
            await self._send(interaction, "Đã đặt lại bộ lọc.")

    async def _preset(self, interaction: discord.Interaction, preset: str) -> None:
        if not interaction.guild_id:
            await self._send(interaction, "Lệnh này chỉ dùng trong server.", ephemeral=True)
            return

        if not self._is_dj_or_admin(interaction):
            await self._send(interaction, "Bạn không có quyền dùng lệnh này.", ephemeral=True)
            return

        async with guild_lock(interaction.guild_id):
            player = await self._get_player(interaction, connect=False)
            if not player:
                await self._send(interaction, "Bot chưa ở trong voice channel.", ephemeral=True)
                return

            if not await self._ensure_same_channel(interaction, player):
                return

            try:
                await asyncio.wait_for(
                    apply_filter_preset(self.bot, player, preset),
                    timeout=PLAYER_OP_TIMEOUT,
                )
            except asyncio.TimeoutError:
                logger.warning("Apply preset timeout guild=%s preset=%r", interaction.guild_id, preset)
                new_player = await rebuild_player_session(self.bot, interaction, old=player)
                if not new_player:
                    await self._send(interaction, "Không thể áp dụng filter do phiên phát bị treo.")
                    return
                player = new_player
            except ValueError:
                await self._send(interaction, "Preset không hợp lệ.", ephemeral=True)
                return
            except Exception:
                logger.exception("Failed to apply preset guild=%s preset=%r", interaction.guild_id, preset)
                await self._send(interaction, "Không thể áp dụng filter.", ephemeral=True)
                return

            await self._refresh_controller(player)
            await self._send(interaction, f"Đã bật bộ lọc: {preset}.")

    @app_commands.command(name="8d", description="Bật bộ lọc 8D")
    @app_commands.guild_only()
    async def filter_8d(self, interaction: discord.Interaction) -> None:
        await self._preset(interaction, "8d")

    @app_commands.command(name="bassboost", description="Bật bộ lọc BassBoost")
    @app_commands.guild_only()
    async def bassboost(self, interaction: discord.Interaction) -> None:
        await self._preset(interaction, "bassboost")

    @app_commands.command(name="deepbass", description="Bật bộ lọc DeepBass")
    @app_commands.guild_only()
    async def deepbass(self, interaction: discord.Interaction) -> None:
        await self._preset(interaction, "deepbass")

    @app_commands.command(name="nightcore", description="Bật bộ lọc NightCore")
    @app_commands.guild_only()
    async def nightcore(self, interaction: discord.Interaction) -> None:
        await self._preset(interaction, "nightcore")

    @app_commands.command(name="chipmunk", description="Bật bộ lọc Chipmunk")
    @app_commands.guild_only()
    async def chipmunk(self, interaction: discord.Interaction) -> None:
        await self._preset(interaction, "chipmunk")

    @app_commands.command(name="darthvader", description="Bật bộ lọc DarthVader")
    @app_commands.guild_only()
    async def darthvader(self, interaction: discord.Interaction) -> None:
        await self._preset(interaction, "darthvader")

    @app_commands.command(name="daycore", description="Bật bộ lọc DayCore")
    @app_commands.guild_only()
    async def daycore(self, interaction: discord.Interaction) -> None:
        await self._preset(interaction, "daycore")

    @app_commands.command(name="slowed", description="Bật bộ lọc Slowed")
    @app_commands.guild_only()
    async def slowed(self, interaction: discord.Interaction) -> None:
        await self._preset(interaction, "slowed")

    @app_commands.command(name="lofi", description="Bật bộ lọc Lofi")
    @app_commands.guild_only()
    async def lofi(self, interaction: discord.Interaction) -> None:
        await self._preset(interaction, "lofi")

    @app_commands.command(name="vibrato", description="Bật bộ lọc Vibrato")
    @app_commands.guild_only()
    async def vibrato(self, interaction: discord.Interaction) -> None:
        await self._preset(interaction, "vibrato")

    @app_commands.command(name="vibration", description="Bật bộ lọc Vibration")
    @app_commands.guild_only()
    async def vibration(self, interaction: discord.Interaction) -> None:
        await self._preset(interaction, "vibrato")

    @app_commands.command(name="tremolo", description="Bật bộ lọc Tremolo")
    @app_commands.guild_only()
    async def tremolo(self, interaction: discord.Interaction) -> None:
        await self._preset(interaction, "tremolo")

    @app_commands.command(name="karaoke", description="Bật bộ lọc Karaoke")
    @app_commands.guild_only()
    async def karaoke(self, interaction: discord.Interaction) -> None:
        await self._preset(interaction, "karaoke")

    # ------------------------------------------------------------------------------
    # Command: /filter - Chọn filter bằng autocomplete (hỗ trợ tất cả 32 presets)
    # ------------------------------------------------------------------------------
    async def filter_autocomplete(
        self,
        interaction: discord.Interaction,
        current: str,
    ) -> list[app_commands.Choice[str]]:
        """Autocomplete cho command /filter - lọc theo từ khóa người dùng nhập."""
        choices: list[app_commands.Choice[str]] = []
        current_lower = current.lower()
        
        for name, config in FILTER_PRESETS.items():
            if name == "reset":
                continue  # Bỏ qua reset, đã có off
            
            description = config.get("description", "")
            # Tìm theo tên hoặc mô tả
            if current_lower in name.lower() or current_lower in description.lower():
                label = f"{name.capitalize()} - {description}"[:100]
                choices.append(app_commands.Choice(name=label, value=name))
        
        # Discord giới hạn 25 choices
        return choices[:25]

    @app_commands.command(name="filter", description="Chọn bộ lọc âm thanh")
    @app_commands.describe(preset="Chọn filter preset")
    @app_commands.autocomplete(preset=filter_autocomplete)
    @app_commands.guild_only()
    async def filter_cmd(self, interaction: discord.Interaction, preset: str) -> None:
        await self._preset(interaction, preset)

    @app_commands.command(name="filters", description="Xem danh sách tất cả filter có sẵn")
    @app_commands.guild_only()
    async def filters_list(self, interaction: discord.Interaction) -> None:
        """Hiển thị danh sách tất cả filter presets."""
        categories = {
            "Quality": [
                "balanced", "studio", "clarity", "presence", "warm", "bright",
                "smooth", "basscut", "trebleboost", "tightbass", "stage",
            ],
            "Bass Mix": ["bassclarity", "bassvocal", "basswide", "basssmooth"],
            "Bass": ["bassboost", "deepbass", "softbass", "megabass", "heavybass"],
            "Pitch/Key": ["pitchup", "pitchdown", "pitchup2", "pitchdown2"],
            "Speed/Pitch": ["nightcore", "daycore", "slowed", "superslow", "doubletime", "chipmunk", "darthvader"],
            "Aesthetic": ["lofi", "vaporwave"],
            "3D/Spatial": ["8d", "reverse8d", "stereowide", "mono"],
            "Modulation": ["vibrato", "tremolo"],
            "Vocal": ["vocal", "vocalclear", "vocalair", "karaoke"],
            "Genre EQ": ["rock", "pop", "electronic", "cinema", "party"],
            "Effects": ["underwater", "phone", "radio", "distorted"],
        }
        
        embed = discord.Embed(title="Danh sách bộ lọc", color=0x7289DA)
        embed.description = "Dùng `/filter <tên>` hoặc lệnh riêng để bật bộ lọc.\nDùng `/resetfilter` hoặc `/filter off` để tắt."
        
        for cat_name, filter_names in categories.items():
            values = []
            for name in filter_names:
                if name in FILTER_PRESETS:
                    desc = FILTER_PRESETS[name].get("description", "")
                    values.append(f"`{name}` - {desc}")
            if values:
                embed.add_field(name=cat_name, value="\n".join(values), inline=False)
        
        await self._send(interaction, embed=embed, ephemeral=True)

    # === Các command filter mới ===
    @app_commands.command(name="softbass", description="Bật bộ lọc Softbass (bass nhẹ)")
    @app_commands.guild_only()
    async def softbass(self, interaction: discord.Interaction) -> None:
        await self._preset(interaction, "softbass")

    @app_commands.command(name="megabass", description="Bật bộ lọc Megabass (cực mạnh!)")
    @app_commands.guild_only()
    async def megabass(self, interaction: discord.Interaction) -> None:
        await self._preset(interaction, "megabass")

    @app_commands.command(name="heavybass", description="Bật bộ lọc Heavybass (bass + treble)")
    @app_commands.guild_only()
    async def heavybass(self, interaction: discord.Interaction) -> None:
        await self._preset(interaction, "heavybass")

    @app_commands.command(name="superslow", description="Bật bộ lọc Superslow (cực chậm)")
    @app_commands.guild_only()
    async def superslow(self, interaction: discord.Interaction) -> None:
        await self._preset(interaction, "superslow")

    @app_commands.command(name="doubletime", description="Bật bộ lọc Doubletime (gấp đôi tốc độ)")
    @app_commands.guild_only()
    async def doubletime(self, interaction: discord.Interaction) -> None:
        await self._preset(interaction, "doubletime")

    @app_commands.command(name="vaporwave", description="Bật bộ lọc Vaporwave (aesthetic 80s)")
    @app_commands.guild_only()
    async def vaporwave(self, interaction: discord.Interaction) -> None:
        await self._preset(interaction, "vaporwave")

    @app_commands.command(name="reverse8d", description="Bật bộ lọc Reverse8D (xoay ngược)")
    @app_commands.guild_only()
    async def reverse8d(self, interaction: discord.Interaction) -> None:
        await self._preset(interaction, "reverse8d")

    @app_commands.command(name="stereowide", description="Bật bộ lọc Stereowide (mở rộng stereo)")
    @app_commands.guild_only()
    async def stereowide(self, interaction: discord.Interaction) -> None:
        await self._preset(interaction, "stereowide")

    @app_commands.command(name="mono", description="Bật bộ lọc Mono (chuyển sang mono)")
    @app_commands.guild_only()
    async def mono(self, interaction: discord.Interaction) -> None:
        await self._preset(interaction, "mono")

    @app_commands.command(name="vocal", description="Bật bộ lọc Vocal (tăng giọng hát)")
    @app_commands.guild_only()
    async def vocal(self, interaction: discord.Interaction) -> None:
        await self._preset(interaction, "vocal")

    @app_commands.command(name="rock", description="Bật bộ lọc Rock/Metal EQ")
    @app_commands.guild_only()
    async def rock(self, interaction: discord.Interaction) -> None:
        await self._preset(interaction, "rock")

    @app_commands.command(name="pop", description="Bật bộ lọc Pop EQ")
    @app_commands.guild_only()
    async def pop(self, interaction: discord.Interaction) -> None:
        await self._preset(interaction, "pop")

    @app_commands.command(name="electronic", description="Bật bộ lọc Electronic/EDM EQ")
    @app_commands.guild_only()
    async def electronic(self, interaction: discord.Interaction) -> None:
        await self._preset(interaction, "electronic")

    @app_commands.command(name="cinema", description="Bật bộ lọc Cinema (cinematic)")
    @app_commands.guild_only()
    async def cinema(self, interaction: discord.Interaction) -> None:
        await self._preset(interaction, "cinema")

    @app_commands.command(name="party", description="Bật bộ lọc Party (bass + speed)")
    @app_commands.guild_only()
    async def party(self, interaction: discord.Interaction) -> None:
        await self._preset(interaction, "party")

    @app_commands.command(name="underwater", description="Bật bộ lọc Underwater (dưới nước)")
    @app_commands.guild_only()
    async def underwater(self, interaction: discord.Interaction) -> None:
        await self._preset(interaction, "underwater")

    @app_commands.command(name="phone", description="Bật bộ lọc Phone (điện thoại cũ)")
    @app_commands.guild_only()
    async def phone(self, interaction: discord.Interaction) -> None:
        await self._preset(interaction, "phone")

    @app_commands.command(name="radio", description="Bật bộ lọc Radio (vintage)")
    @app_commands.guild_only()
    async def radio(self, interaction: discord.Interaction) -> None:
        await self._preset(interaction, "radio")

    @app_commands.command(name="distorted", description="Bật bộ lọc Distorted (méo tiếng)")
    @app_commands.guild_only()
    async def distorted(self, interaction: discord.Interaction) -> None:
        await self._preset(interaction, "distorted")


    @app_commands.command(name="forward", description="Tua tới (giây)")
    @app_commands.describe(seconds="Số giây")
    @app_commands.guild_only()
    async def forward(self, interaction: discord.Interaction, seconds: int) -> None:
        if not interaction.guild_id:
            await self._send(interaction, "Lệnh này chỉ dùng trong server.", ephemeral=True)
            return

        if not self._is_dj_or_admin(interaction):
            await self._send(interaction, "Bạn không có quyền dùng lệnh này.", ephemeral=True)
            return

        seconds = max(0, int(seconds))

        async with guild_lock(interaction.guild_id):
            player = await self._get_player(interaction, connect=False)
            if not player or not player.current:
                await self._send(interaction, "Không có bài đang phát.", ephemeral=True)
                return

            if not player.current.is_seekable:
                await self._send(interaction, "Bài này không hỗ trợ seek.", ephemeral=True)
                return

            if not await self._ensure_same_channel(interaction, player):
                return

            ms = min(player.current.length, player.position + (seconds * 1000))
            await player.seek(ms)
            await self._refresh_controller(player)
            await self._send(interaction, f"Đã forward tới {format_ms(ms)}.")

    @app_commands.command(name="rewind", description="Tua lui (giây)")
    @app_commands.describe(seconds="Số giây")
    @app_commands.guild_only()
    async def rewind(self, interaction: discord.Interaction, seconds: int) -> None:
        if not interaction.guild_id:
            await self._send(interaction, "Lệnh này chỉ dùng trong server.", ephemeral=True)
            return

        if not self._is_dj_or_admin(interaction):
            await self._send(interaction, "Bạn không có quyền dùng lệnh này.", ephemeral=True)
            return

        seconds = max(0, int(seconds))

        async with guild_lock(interaction.guild_id):
            player = await self._get_player(interaction, connect=False)
            if not player or not player.current:
                await self._send(interaction, "Không có bài đang phát.", ephemeral=True)
                return

            if not player.current.is_seekable:
                await self._send(interaction, "Bài này không hỗ trợ seek.", ephemeral=True)
                return

            if not await self._ensure_same_channel(interaction, player):
                return

            ms = max(0, player.position - (seconds * 1000))
            await player.seek(ms)
            await self._refresh_controller(player)
            await self._send(interaction, f"Đã rewind tới {format_ms(ms)}.")

    @app_commands.command(name="replay", description="Phát lại từ đầu")
    @app_commands.guild_only()
    async def replay(self, interaction: discord.Interaction) -> None:
        if not interaction.guild_id:
            await self._send(interaction, "Lệnh này chỉ dùng trong server.", ephemeral=True)
            return

        if not self._is_dj_or_admin(interaction):
            await self._send(interaction, "Bạn không có quyền dùng lệnh này.", ephemeral=True)
            return

        async with guild_lock(interaction.guild_id):
            player = await self._get_player(interaction, connect=False)
            if not player or not player.current:
                await self._send(interaction, "Không có bài đang phát.", ephemeral=True)
                return

            if not player.current.is_seekable:
                await self._send(interaction, "Bài này không hỗ trợ seek.", ephemeral=True)
                return

            await player.seek(0)
            await self._refresh_controller(player)
            await self._send(interaction, "Đã replay từ đầu.")

    @app_commands.command(name="skipto", description="Chuyển đến bài thứ N trong hàng đợi")
    @app_commands.describe(index="1 là bài kế tiếp")
    @app_commands.guild_only()
    async def skipto(self, interaction: discord.Interaction, index: int) -> None:
        if not interaction.guild_id:
            await self._send(interaction, "Lệnh này chỉ dùng trong server.", ephemeral=True)
            return

        if not self._is_dj_or_admin(interaction):
            await self._send(interaction, "Bạn không có quyền dùng lệnh này.", ephemeral=True)
            return

        async with guild_lock(interaction.guild_id):
            player = await self._get_player(interaction, connect=False)
            if not player:
                await self._send(interaction, "Bot chưa ở trong voice channel.", ephemeral=True)
                return

            if not player.queue:
                await self._send(interaction, "Hàng đợi đang trống.", ephemeral=True)
                return

            idx = int(index) - 1
            if idx < 0 or idx >= len(player.queue):
                await self._send(interaction, "Index không hợp lệ.", ephemeral=True)
                return

            if not await self._ensure_same_channel(interaction, player):
                return

            upcoming = list(player.queue)
            target = upcoming[idx]
            keep = upcoming[idx + 1 :]

            player.queue.clear()
            if keep:
                await player.queue.put_wait(keep)

            try:
                await asyncio.wait_for(
                    player.play(target, replace=True, volume=player.volume),
                    timeout=PLAYER_OP_TIMEOUT,
                )
            except asyncio.TimeoutError:
                logger.warning("Play timeout guild=%s", interaction.guild_id)
                new_player = await rebuild_player_session(self.bot, interaction, old=player)
                if not new_player:
                    await self._send(interaction, "Không thể phát nhạc do phiên phát bị treo.")
                    return
                player = new_player
                try:
                    await asyncio.wait_for(
                        player.play(target, replace=True, volume=player.volume),
                        timeout=PLAYER_OP_TIMEOUT,
                    )
                except Exception:
                    logger.exception("Failed to start playback guild=%s", interaction.guild_id)
                    await self._send(interaction, "Không thể phát nhạc. Vui lòng thử lại.")
                    return
            except Exception:
                logger.exception("Failed to start playback guild=%s", interaction.guild_id)
                await self._send(interaction, "Không thể phát nhạc. Vui lòng thử lại.")
                return

            await self._refresh_controller(player)
            await self._send(interaction, f"Đã skipto '{target.title}'.")

    @app_commands.command(name="bump", description="Đưa bài lên đầu hàng đợi")
    @app_commands.describe(index="1 là bài kế tiếp")
    @app_commands.guild_only()
    async def bump(self, interaction: discord.Interaction, index: int) -> None:
        if not interaction.guild_id:
            await self._send(interaction, "Lệnh này chỉ dùng trong server.", ephemeral=True)
            return

        if not self._is_dj_or_admin(interaction):
            await self._send(interaction, "Bạn không có quyền dùng lệnh này.", ephemeral=True)
            return

        async with guild_lock(interaction.guild_id):
            player = await self._get_player(interaction, connect=False)
            if not player:
                await self._send(interaction, "Bot chưa ở trong voice channel.", ephemeral=True)
                return

            if not player.queue:
                await self._send(interaction, "Hàng đợi đang trống.", ephemeral=True)
                return

            idx = int(index) - 1
            if idx < 0 or idx >= len(player.queue):
                await self._send(interaction, "Index không hợp lệ.", ephemeral=True)
                return

            track = player.queue[idx]
            player.queue.delete(idx)
            player.queue.put_at(0, track)
            await self._refresh_controller(player)
            await self._send(interaction, f"Đã bump '{track.title}' lên #1.")

    @app_commands.command(name="history", description="Xem lịch sử các bài đã phát")
    @app_commands.guild_only()
    async def history(self, interaction: discord.Interaction) -> None:
        if not interaction.guild_id:
            await self._send(interaction, "Lệnh này chỉ dùng trong server.", ephemeral=True)
            return

        async with guild_lock(interaction.guild_id):
            player = await self._get_player(interaction, connect=False)
            if not player or not player.queue.history:
                await self._send(interaction, "Chưa có lịch sử phát.", ephemeral=True)
                return

            items = list(player.queue.history)
            if not items:
                await self._send(interaction, "Chưa có lịch sử phát.", ephemeral=True)
                return

            last = items[-10:]
            lines: list[str] = []
            for i, t in enumerate(reversed(last), start=1):
                lines.append(f"{i}. {t.title} ({format_ms(t.length)})")

            embed = discord.Embed(title="Lịch sử phát")
            embed.description = "\n".join(lines)
            await self._send(interaction, embed=embed)

    @app_commands.command(name="previous", description="Phát lại bài trước đó")
    @app_commands.guild_only()
    async def previous(self, interaction: discord.Interaction) -> None:
        if not interaction.guild_id:
            await self._send(interaction, "Lệnh này chỉ dùng trong server.", ephemeral=True)
            return

        if not self._is_dj_or_admin(interaction):
            await self._send(interaction, "Bạn không có quyền dùng lệnh này.", ephemeral=True)
            return

        async with guild_lock(interaction.guild_id):
            player = await self._get_player(interaction, connect=False)
            if not player:
                await self._send(interaction, "Bot chưa ở trong voice channel.", ephemeral=True)
                return

            if not await self._ensure_same_channel(interaction, player):
                return

            prev = None
            if hasattr(self.bot, "get_previous_track"):
                prev = getattr(self.bot, "get_previous_track")(interaction.guild_id)

            if prev is None:
                await self._send(interaction, "Không có bài trước đó.", ephemeral=True)
                return

            try:
                await asyncio.wait_for(
                    player.play(prev, replace=True, start=0, volume=player.volume),
                    timeout=PLAYER_OP_TIMEOUT,
                )
            except asyncio.TimeoutError:
                logger.warning("Play timeout guild=%s", interaction.guild_id)
                new_player = await rebuild_player_session(self.bot, interaction, old=player)
                if not new_player:
                    await self._send(interaction, "Không thể phát nhạc do phiên phát bị treo.")
                    return
                player = new_player
                try:
                    await asyncio.wait_for(
                        player.play(prev, replace=True, start=0, volume=player.volume),
                        timeout=PLAYER_OP_TIMEOUT,
                    )
                except Exception:
                    logger.exception("Failed to start playback guild=%s", interaction.guild_id)
                    await self._send(interaction, "Không thể phát nhạc. Vui lòng thử lại.")
                    return
            except Exception:
                logger.exception("Failed to start playback guild=%s", interaction.guild_id)
                await self._send(interaction, "Không thể phát nhạc. Vui lòng thử lại.")
                return

            await self._refresh_controller(player)
            await self._send(interaction, f"Đang phát lại bài trước: '{prev.title}'.")

    @app_commands.command(name="grab", description="Gửi bài đang phát vào tin nhắn riêng (DM)")
    @app_commands.guild_only()
    async def grab(self, interaction: discord.Interaction) -> None:
        if not interaction.guild_id:
            await self._send(interaction, "Lệnh này chỉ dùng trong server.", ephemeral=True)
            return

        player = await self._get_player(interaction, connect=False)
        if not player or not player.current:
            await self._send(interaction, "Không có bài đang phát.", ephemeral=True)
            return

        track = player.current
        embed = discord.Embed(title="Bài hát đã lưu")
        embed.description = f"{track.title} - {track.author}"
        if track.uri:
            embed.url = track.uri
        if track.artwork:
            embed.set_thumbnail(url=track.artwork)

        try:
            await interaction.user.send(embed=embed)
        except discord.Forbidden:
            await self._send(interaction, "Không thể DM bạn (bạn đang tắt DM).", ephemeral=True)
            return
        except discord.HTTPException:
            await self._send(interaction, "Không thể gửi DM.", ephemeral=True)
            return

        await self._send(interaction, "Đã gửi bài hiện tại vào DM của bạn.", ephemeral=True)

    @app_commands.command(name="leavecleanup", description="Xóa bài của những người đã rời kênh thoại")
    @app_commands.guild_only()
    async def leavecleanup(self, interaction: discord.Interaction) -> None:
        if not interaction.guild_id:
            await self._send(interaction, "Lệnh này chỉ dùng trong server.", ephemeral=True)
            return

        if not self._is_dj_or_admin(interaction):
            await self._send(interaction, "Bạn không có quyền dùng lệnh này.", ephemeral=True)
            return

        async with guild_lock(interaction.guild_id):
            player = await self._get_player(interaction, connect=False)
            if not player or not player.channel:
                await self._send(interaction, "Bot chưa ở trong voice channel.", ephemeral=True)
                return

            present_ids = {m.id for m in player.channel.members if not m.bot}
            upcoming = list(player.queue)

            keep: list[wavelink.Playable] = []
            removed = 0
            for t in upcoming:
                extras = dict(t.extras)
                rid = extras.get("requester_id")
                if rid is None:
                    keep.append(t)
                    continue

                if int(rid) in present_ids:
                    keep.append(t)
                else:
                    removed += 1

            player.queue.clear()
            if keep:
                await player.queue.put_wait(keep)

            await self._refresh_controller(player)
            await self._send(interaction, f"Đã xóa {removed} bài khỏi hàng đợi.")

    @app_commands.command(name="dj", description="Cấu hình chế độ DJ")
    @app_commands.describe(action="set | clear | view", role="Role DJ (chỉ dùng với action=set)")
    @app_commands.guild_only()
    async def dj(self, interaction: discord.Interaction, action: str, role: discord.Role | None = None) -> None:
        if not interaction.guild_id:
            await self._send(interaction, "Lệnh này chỉ dùng trong server.", ephemeral=True)
            return

        action = action.strip().lower()
        if action not in {"set", "clear", "view"}:
            await self._send(interaction, "Action không hợp lệ: set | clear | view", ephemeral=True)
            return

        if action in {"set", "clear"} and not self._is_admin(interaction):
            await self._send(interaction, "Bạn không có quyền dùng lệnh này.", ephemeral=True)
            return

        settings = self._settings(interaction.guild_id)

        if action == "view":
            if settings.dj_role_id:
                await self._send(interaction, f"DJ role hiện tại: <@&{settings.dj_role_id}>.")
            else:
                await self._send(interaction, "DJ role chưa được set (mọi người đều dùng được).")
            return

        if action == "clear":
            settings.dj_role_id = None

            if hasattr(self.bot, "storage"):
                try:
                    await getattr(self.bot, "storage").upsert_guild_settings(interaction.guild_id, settings)
                except Exception:
                    logger.exception("Failed to persist dj_role_id guild=%s", interaction.guild_id)

            await self._send(interaction, "Đã clear DJ role.")
            return

        if role is None:
            await self._send(interaction, "Bạn cần chọn role.", ephemeral=True)
            return

        settings.dj_role_id = role.id

        if hasattr(self.bot, "storage"):
            try:
                await getattr(self.bot, "storage").upsert_guild_settings(interaction.guild_id, settings)
            except Exception:
                logger.exception("Failed to persist dj_role_id guild=%s", interaction.guild_id)

        await self._send(interaction, f"Đã set DJ role = {role.mention}.")

    @app_commands.command(name="announce", description="Cấu hình thông báo bài hát")
    @app_commands.describe(mode="on | off", channel="Kênh để announce (tuỳ chọn)")
    @app_commands.guild_only()
    async def announce(
        self,
        interaction: discord.Interaction,
        mode: str,
        channel: discord.TextChannel | None = None,
    ) -> None:
        if not interaction.guild_id:
            await self._send(interaction, "Lệnh này chỉ dùng trong server.", ephemeral=True)
            return

        if not self._is_admin(interaction):
            await self._send(interaction, "Bạn không có quyền dùng lệnh này.", ephemeral=True)
            return

        mode = mode.strip().lower()
        if mode not in {"on", "off"}:
            await self._send(interaction, "Mode không hợp lệ: on | off", ephemeral=True)
            return

        settings = self._settings(interaction.guild_id)
        settings.announce_enabled = mode == "on"

        if channel is not None:
            settings.announce_channel_id = channel.id
        elif mode == "on" and settings.announce_channel_id is None:
            if interaction.channel_id is not None:
                settings.announce_channel_id = interaction.channel_id

        if hasattr(self.bot, "storage"):
            try:
                await getattr(self.bot, "storage").upsert_guild_settings(interaction.guild_id, settings)
            except Exception:
                logger.exception("Failed to persist announce settings guild=%s", interaction.guild_id)

        await self._send(interaction, f"Announce = {mode}.")

    @app_commands.command(name="buttons", description="Cấu hình nút điều khiển")
    @app_commands.describe(mode="on | off")
    @app_commands.guild_only()
    async def buttons(self, interaction: discord.Interaction, mode: str) -> None:
        if not interaction.guild_id:
            await self._send(interaction, "Lệnh này chỉ dùng trong server.", ephemeral=True)
            return

        if not self._is_admin(interaction):
            await self._send(interaction, "Bạn không có quyền dùng lệnh này.", ephemeral=True)
            return

        mode = mode.strip().lower()
        if mode not in {"on", "off"}:
            await self._send(interaction, "Mode không hợp lệ: on | off", ephemeral=True)
            return

        settings = self._settings(interaction.guild_id)
        settings.buttons_enabled = mode == "on"

        if hasattr(self.bot, "storage"):
            try:
                await getattr(self.bot, "storage").upsert_guild_settings(interaction.guild_id, settings)
            except Exception:
                logger.exception("Failed to persist buttons_enabled guild=%s", interaction.guild_id)

        player = await self._get_player(interaction, connect=False)
        if player:
            await self._refresh_controller(player)

        await self._send(interaction, f"Buttons = {mode}.")

    @app_commands.command(name="settings", description="Xem cài đặt hiện tại")
    @app_commands.guild_only()
    async def settings(self, interaction: discord.Interaction) -> None:
        if not interaction.guild_id:
            await self._send(interaction, "Lệnh này chỉ dùng trong server.", ephemeral=True)
            return

        s = self._settings(interaction.guild_id)
        embed = discord.Embed(title="Cài đặt")
        embed.add_field(name="Âm lượng mặc định", value=str(s.volume_default), inline=True)
        embed.add_field(name="Chế độ 24/7", value="Bật" if s.stay_247 else "Tắt", inline=True)
        embed.add_field(name="Thông báo", value="Bật" if s.announce_enabled else "Tắt", inline=True)
        embed.add_field(name="Kênh thông báo", value=str(s.announce_channel_id or "(tự động)"), inline=True)
        embed.add_field(name="DJ Role", value=str(s.dj_role_id or "(không)"), inline=True)
        embed.add_field(name="Bộ lọc", value=str(s.filters_preset), inline=True)
        embed.add_field(name="Nút điều khiển", value="Bật" if s.buttons_enabled else "Tắt", inline=True)
        await self._send(interaction, embed=embed)

    @app_commands.command(name="ping", description="Kiểm tra độ trễ (ping)")
    @app_commands.guild_only()
    async def ping(self, interaction: discord.Interaction) -> None:
        ws_ms = int(getattr(self.bot, "latency", 0) * 1000)

        player = await self._get_player(interaction, connect=False)
        ll_ms = None
        if player:
            ll_ms = player.ping

        msg = f"WS: {ws_ms}ms"
        if ll_ms is not None and ll_ms >= 0:
            msg += f" | Lavalink: {ll_ms}ms"

        await self._send(interaction, msg, ephemeral=True)


class SearchResultView(discord.ui.View):
    def __init__(self, bot: commands.Bot, tracks: list[wavelink.Playable], *, requester_id: int) -> None:
        super().__init__(timeout=60)
        self._bot = bot
        self._tracks = tracks
        self._requester_id = requester_id

        options: list[discord.SelectOption] = []
        for i, t in enumerate(tracks):
            label = (t.title or "(unknown)")[:100]
            desc = f"{t.author} ({format_ms(t.length)})"[:100]
            options.append(discord.SelectOption(label=label, value=str(i), description=desc))

        select = discord.ui.Select(placeholder="Chọn bài hát", min_values=1, max_values=1, options=options)
        select.callback = self._on_select  # type: ignore[assignment]
        self.add_item(select)

    async def _on_select(self, interaction: discord.Interaction) -> None:
        if interaction.user.id != self._requester_id:
            await interaction.response.send_message("Menu này không dành cho bạn.", ephemeral=True)
            return

        if interaction.guild_id is None or interaction.guild is None:
            await interaction.response.send_message("Lệnh này chỉ dùng trong server.", ephemeral=True)
            return

        try:
            idx = int(interaction.data.get("values", ["0"])[0])  # type: ignore[union-attr]
        except Exception:
            await interaction.response.send_message("Selection không hợp lệ.", ephemeral=True)
            return

        if idx < 0 or idx >= len(self._tracks):
            await interaction.response.send_message("Selection không hợp lệ.", ephemeral=True)
            return

        await interaction.response.defer(thinking=True)

        member = interaction.user if isinstance(interaction.user, discord.Member) else None
        if not member or not member.voice or not member.voice.channel:
            await interaction.followup.send("Bạn cần vào voice channel trước.", ephemeral=True)
            return

        async with guild_lock(interaction.guild_id):
            vc = interaction.guild.voice_client
            player: wavelink.Player | None = vc if isinstance(vc, wavelink.Player) else None

            if player is None:
                try:
                    player = await asyncio.wait_for(
                        member.voice.channel.connect(cls=wavelink.Player, self_deaf=True),
                        timeout=VOICE_CONNECT_TIMEOUT,
                    )
                except (asyncio.TimeoutError, wavelink.exceptions.ChannelTimeoutException):
                    logger.warning("Voice connect timeout guild=%s", interaction.guild_id)
                    player = await rebuild_player_session(
                        self._bot,
                        interaction,
                        channel=member.voice.channel,
                    )
                    if not player:
                        await interaction.followup.send(
                            f"Không thể tham gia voice channel sau {VOICE_CONNECT_TIMEOUT}s.",
                            ephemeral=True,
                        )
                        return
                except (discord.ClientException, discord.HTTPException):
                    await interaction.followup.send("Không thể tham gia voice channel.", ephemeral=True)
                    return

                config = getattr(self._bot, "config")
                settings = getattr(self._bot, "settings").get(interaction.guild_id)
                player.autoplay = wavelink.AutoPlayMode.partial
                player.inactive_timeout = config.idle_timeout_seconds
                try:
                    await player.set_volume(settings.volume_default)
                except Exception:
                    pass

            if player.channel != member.voice.channel:
                await interaction.followup.send(
                    f"Bot đang ở voice channel khác: {player.channel.mention}.",
                    ephemeral=True,
                )
                return

            track = self._tracks[idx]
            track.extras = {
                "requester_id": member.id,
                "requester_name": member.display_name,
            }

            await player.queue.put_wait(track)

            if not player.playing:
                try:
                    nxt = player.queue.get()
                except wavelink.QueueEmpty:
                    await interaction.followup.send("Hàng đợi trống.", ephemeral=True)
                    return

                settings = getattr(self._bot, "settings").get(interaction.guild_id)
                try:
                    await asyncio.wait_for(
                        player.play(nxt, volume=settings.volume_default),
                        timeout=PLAYER_OP_TIMEOUT,
                    )
                except asyncio.TimeoutError:
                    logger.warning("Play timeout guild=%s", interaction.guild_id)
                    new_player = await rebuild_player_session(self._bot, interaction, old=player)
                    if not new_player:
                        await interaction.followup.send("Không thể phát nhạc do phiên phát bị treo.", ephemeral=True)
                        return
                    player = new_player
                except Exception:
                    logger.exception("Failed to start playback guild=%s", interaction.guild_id)
                    await interaction.followup.send("Không thể phát nhạc. Vui lòng thử lại.", ephemeral=True)
                    return

            if hasattr(self._bot, "refresh_controller_message"):
                try:
                    await getattr(self._bot, "refresh_controller_message")(player)
                except Exception:
                    pass

        embed = discord.Embed(title="Đã thêm")
        embed.description = f"Đã thêm '{track.title}' vào hàng đợi."

        edited = False
        try:
            if interaction.message:
                await interaction.message.edit(content=None, embed=embed, view=None)
                edited = True
        except discord.HTTPException:
            edited = False

        if not edited:
            try:
                await interaction.followup.send(embed=embed, ephemeral=True)
            except discord.HTTPException:
                pass


async def setup(bot: commands.Bot) -> None:
    await bot.add_cog(MusicCog(bot))
