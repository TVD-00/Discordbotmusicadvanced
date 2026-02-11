# ##############################################################################
# MODULE: LIBRARY COG
# DESCRIPTION: Cog quản lý thư viện nhạc cá nhân của người dùng.
#              Bao gồm:
#              - Liked Tracks: Bài hát yêu thích (lưu theo user/guild).
#              - Playlists: Danh sách phát do user tự tạo.
# ##############################################################################

from __future__ import annotations

import asyncio
import logging
import time

import discord
from discord import app_commands
from discord.ext import commands
import wavelink

from bot.music.controller import build_controller_embed
from bot.storage.sqlite_storage import SQLiteStorage
from bot.utils.constants import (
    MAX_LIKED_DISPLAY,
    MAX_PLAYLIST_ADD,
    MAX_PLAYLIST_LIST,
    MAX_PLAYLIST_VIEW,
    MAX_SAVE_QUEUE,
    PLAYLIST_CACHE_TTL_SECONDS,
    PLAYER_OP_TIMEOUT,
    SEARCH_TIMEOUT,
)
from bot.utils.helpers import (
    author_voice_channel,
    ensure_same_channel,
    get_player,
    rebuild_player_session,
    send_response,
)
from bot.utils.locks import guild_lock
from bot.utils.time import format_ms


logger = logging.getLogger(__name__)

# Cache đơn giản cho playlist: {(guild_id, user_id, name): (tracks, timestamp)}
_PLAYLIST_CACHE: dict[tuple[int, int, str], tuple[list[wavelink.Playable], float]] = {}


def _get_cached_playlist(guild_id: int, user_id: int, name: str) -> list[wavelink.Playable] | None:
    # Lấy playlist từ cache nếu còn hợp lệ + dọn dẹp entry hết hạn.
    now = time.time()
    key = (guild_id, user_id, name)

    # Dọn dẹp toàn bộ entry hết hạn để tránh memory leak
    expired = [k for k, (_, ts) in _PLAYLIST_CACHE.items() if now - ts >= PLAYLIST_CACHE_TTL_SECONDS]
    for k in expired:
        del _PLAYLIST_CACHE[k]

    cached = _PLAYLIST_CACHE.get(key)
    if cached is not None:
        return cached[0]
    return None


def _set_cached_playlist(guild_id: int, user_id: int, name: str, tracks: list[wavelink.Playable]) -> None:
    # Lưu playlist vào cache.
    key = (guild_id, user_id, name)
    _PLAYLIST_CACHE[key] = (tracks, time.time())


def _clear_playlist_cache(guild_id: int, user_id: int, name: str) -> None:
    # Xóa cache của playlist (dùng khi modify).
    key = (guild_id, user_id, name)
    _PLAYLIST_CACHE.pop(key, None)


# ------------------------------------------------------------------------------
# Helper: _refresh_controller
# Purpose: Cập nhật giao diện player chính.
# ------------------------------------------------------------------------------
async def _refresh_controller(bot: commands.Bot, player: wavelink.Player) -> None:
    if hasattr(bot, "refresh_controller_message"):
        try:
            await getattr(bot, "refresh_controller_message")(player)
        except Exception:
            if player.guild:
                logger.exception("Failed to refresh controller message guild=%s", player.guild.id)


# ------------------------------------------------------------------------------
# Class: LibraryCog
# Purpose: Quản lý Liked Tracks (Thích/Bỏ thích/Xem danh sách).
# ------------------------------------------------------------------------------
class LibraryCog(commands.Cog):
    def __init__(self, bot: commands.Bot) -> None:
        self.bot = bot

    def _storage(self) -> SQLiteStorage:
        return getattr(self.bot, "storage")

    @app_commands.command(name="like", description="Thêm bài đang phát vào Yêu thích")
    @app_commands.guild_only()
    async def like(self, interaction: discord.Interaction) -> None:
        if not interaction.guild_id:
            await send_response(interaction, "Lệnh này chỉ dùng trong server.", ephemeral=True)
            return

        player = await get_player(interaction, connect=False)
        if not player or not player.current:
            await send_response(interaction, "Không có bài đang phát.", ephemeral=True)
            return

        added = await self._storage().like_track(interaction.guild_id, interaction.user.id, player.current)
        await send_response(interaction, "Đã like." if added else "Bài này đã có trong liked.", ephemeral=True)

    @app_commands.command(name="dislike", description="Xóa bài khỏi Yêu thích")
    @app_commands.describe(target="current | previous")
    @app_commands.guild_only()
    async def dislike(self, interaction: discord.Interaction, target: str = "current") -> None:
        if not interaction.guild_id:
            await send_response(interaction, "Lệnh này chỉ dùng trong server.", ephemeral=True)
            return

        target = target.strip().lower()
        if target not in {"current", "previous"}:
            await send_response(interaction, "Target không hợp lệ: current | previous", ephemeral=True)
            return

        track: wavelink.Playable | None = None
        if target == "current":
            player = await get_player(interaction, connect=False)
            track = player.current if player else None
        else:
            if hasattr(self.bot, "get_previous_track"):
                track = getattr(self.bot, "get_previous_track")(interaction.guild_id)

        if not track:
            await send_response(interaction, "Không có track để dislike.", ephemeral=True)
            return

        removed = await self._storage().unlike_track(interaction.guild_id, interaction.user.id, track.identifier)
        await send_response(interaction, "Đã dislike." if removed else "Track không có trong liked.", ephemeral=True)

    @app_commands.command(name="showliked", description="Xem danh sách bài Yêu thích")
    @app_commands.guild_only()
    async def showliked(self, interaction: discord.Interaction) -> None:
        if not interaction.guild_id:
            await send_response(interaction, "Lệnh này chỉ dùng trong server.", ephemeral=True)
            return

        tracks = await self._storage().list_liked(interaction.guild_id, interaction.user.id)
        if not tracks:
            await send_response(interaction, "Liked list đang trống.", ephemeral=True)
            return

        embed = discord.Embed(title=f"Liked Songs ({len(tracks)})")
        lines: list[str] = []
        for i, t in enumerate(tracks[:MAX_LIKED_DISPLAY], start=1):
            lines.append(f"{i}. {t.title} ({format_ms(t.length)})")
        embed.description = "\n".join(lines)

        if interaction.response.is_done():
            await interaction.followup.send(embed=embed, ephemeral=True)
        else:
            await interaction.response.send_message(embed=embed, ephemeral=True)

    @app_commands.command(name="playliked", description="Phát các bài trong Yêu thích")
    @app_commands.describe(order="oldest | newest")
    @app_commands.guild_only()
    async def playliked(self, interaction: discord.Interaction, order: str = "oldest") -> None:
        if not interaction.guild_id:
            await send_response(interaction, "Lệnh này chỉ dùng trong server.", ephemeral=True)
            return

        order = order.strip().lower()
        if order not in {"oldest", "newest"}:
            await send_response(interaction, "Order không hợp lệ: oldest | newest", ephemeral=True)
            return

        tracks = await self._storage().list_liked(interaction.guild_id, interaction.user.id)
        if not tracks:
            await send_response(interaction, "Liked list đang trống.", ephemeral=True)
            return

        if order == "oldest":
            tracks = list(reversed(tracks))

        await interaction.response.defer(thinking=True)

        async with guild_lock(interaction.guild_id):
            player = await get_player(interaction, connect=True)
            if not player:
                await send_response(interaction, "Bạn cần vào voice channel trước.", ephemeral=True)
                return

            if not ensure_same_channel(interaction, player):
                if getattr(player, "channel", None) is not None:
                    await send_response(
                        interaction,
                        f"Bot đang ở voice channel khác: {player.channel.mention}.",
                        ephemeral=True,
                    )
                else:
                    await send_response(interaction, "Bạn cần vào voice channel trước.", ephemeral=True)
                return

            settings = getattr(self.bot, "settings").get(interaction.guild_id)
            for t in tracks:
                t.extras = {
                    "requester_id": interaction.user.id,
                    "requester_name": getattr(interaction.user, "display_name", str(interaction.user)),
                }

            await player.queue.put_wait(tracks)

            if not player.playing:
                try:
                    nxt = player.queue.get()
                except wavelink.QueueEmpty:
                    await send_response(interaction, "Queue rỗng.", ephemeral=True)
                    return
                try:
                    await asyncio.wait_for(
                        player.play(nxt, volume=settings.volume_default),
                        timeout=PLAYER_OP_TIMEOUT,
                    )
                except asyncio.TimeoutError:
                    logger.warning("Play timeout guild=%s", interaction.guild_id)
                    new_player = await rebuild_player_session(self.bot, interaction, old=player)
                    if not new_player:
                        await send_response(interaction, "Không thể phát nhạc do phiên phát bị treo.", ephemeral=True)
                        return
                    player = new_player
                except Exception:
                    logger.exception("Failed to start playback guild=%s", interaction.guild_id)
                    await send_response(interaction, "Không thể phát nhạc. Vui lòng thử lại.", ephemeral=True)
                    return

            await _refresh_controller(self.bot, player)

        embed = build_controller_embed(self.bot, player, notice=f"Đã queue {len(tracks)} liked tracks.")
        await interaction.edit_original_response(embed=embed)

    @app_commands.command(name="clearliked", description="Xóa toàn bộ danh sách Yêu thích")
    @app_commands.guild_only()
    async def clearliked(self, interaction: discord.Interaction) -> None:
        if not interaction.guild_id:
            await send_response(interaction, "Lệnh này chỉ dùng trong server.", ephemeral=True)
            return

        deleted = await self._storage().clear_liked(interaction.guild_id, interaction.user.id)
        await send_response(interaction, f"Đã xóa {deleted} liked tracks.", ephemeral=True)

    @app_commands.command(name="sortliked", description="Sắp xếp danh sách Yêu thích")
    @app_commands.describe(key="title | author")
    @app_commands.guild_only()
    async def sortliked(self, interaction: discord.Interaction, key: str = "title") -> None:
        if not interaction.guild_id:
            await send_response(interaction, "Lệnh này chỉ dùng trong server.", ephemeral=True)
            return

        key = key.strip().lower()
        if key not in {"title", "author"}:
            await send_response(interaction, "Key không hợp lệ: title | author", ephemeral=True)
            return

        tracks = await self._storage().list_liked(interaction.guild_id, interaction.user.id)
        if not tracks:
            await send_response(interaction, "Liked list đang trống.", ephemeral=True)
            return

        if key == "title":
            tracks.sort(key=lambda t: (t.title or "").lower())
        else:
            tracks.sort(key=lambda t: (t.author or "").lower())

        embed = discord.Embed(title=f"Liked Songs (sorted by {key})")
        lines: list[str] = []
        for i, t in enumerate(tracks[:MAX_LIKED_DISPLAY], start=1):
            lines.append(f"{i}. {t.title} ({format_ms(t.length)})")
        embed.description = "\n".join(lines)

        if interaction.response.is_done():
            await interaction.followup.send(embed=embed, ephemeral=True)
        else:
            await interaction.response.send_message(embed=embed, ephemeral=True)


# ------------------------------------------------------------------------------
# Class: PlaylistCog
# Purpose: Quản lý Playlist (CRUD và phát nhạc từ playlist).
# ------------------------------------------------------------------------------
@app_commands.guild_only()
class PlaylistCog(commands.GroupCog, group_name="playlist"):
    def __init__(self, bot: commands.Bot) -> None:
        self.bot = bot

    def _storage(self) -> SQLiteStorage:
        return getattr(self.bot, "storage")

    @app_commands.command(name="create", description="Tạo playlist mới")
    @app_commands.describe(name="Tên playlist")
    async def create(self, interaction: discord.Interaction, name: str) -> None:
        if not interaction.guild_id:
            await send_response(interaction, "Lệnh này chỉ dùng trong server.", ephemeral=True)
            return

        name = name.strip()
        if not name:
            await send_response(interaction, "Tên playlist không hợp lệ.", ephemeral=True)
            return

        try:
            await self._storage().create_playlist(interaction.guild_id, interaction.user.id, name)
        except Exception:
            await send_response(interaction, "Không thể tạo playlist (có thể đã tồn tại).", ephemeral=True)
            return

        await send_response(interaction, f"Đã tạo playlist '{name}'.", ephemeral=True)

    @app_commands.command(name="delete", description="Xóa playlist")
    @app_commands.describe(name="Tên playlist")
    async def delete(self, interaction: discord.Interaction, name: str) -> None:
        if not interaction.guild_id:
            await send_response(interaction, "Lệnh này chỉ dùng trong server.", ephemeral=True)
            return

        ok = await self._storage().delete_playlist(interaction.guild_id, interaction.user.id, name.strip())
        await send_response(interaction, "Đã xóa playlist." if ok else "Không tìm thấy playlist.", ephemeral=True)

    @app_commands.command(name="list", description="Xem danh sách playlist")
    async def list(self, interaction: discord.Interaction) -> None:
        if not interaction.guild_id:
            await send_response(interaction, "Lệnh này chỉ dùng trong server.", ephemeral=True)
            return

        items = await self._storage().list_playlists(interaction.guild_id, interaction.user.id)
        if not items:
            await send_response(interaction, "Bạn chưa có playlist nào.", ephemeral=True)
            return

        embed = discord.Embed(title="Playlists")
        lines = [f"- {name} ({count})" for name, count in items[:MAX_PLAYLIST_LIST]]
        embed.description = "\n".join(lines)

        if interaction.response.is_done():
            await interaction.followup.send(embed=embed, ephemeral=True)
        else:
            await interaction.response.send_message(embed=embed, ephemeral=True)

    @app_commands.command(name="view", description="Xem chi tiết playlist")
    @app_commands.describe(name="Tên playlist")
    async def view(self, interaction: discord.Interaction, name: str) -> None:
        if not interaction.guild_id:
            await send_response(interaction, "Lệnh này chỉ dùng trong server.", ephemeral=True)
            return

        # Thử lấy từ cache trước
        tracks = _get_cached_playlist(interaction.guild_id, interaction.user.id, name.strip())
        if tracks is None:
            tracks = await self._storage().playlist_tracks(interaction.guild_id, interaction.user.id, name.strip())
            if tracks is not None:
                _set_cached_playlist(interaction.guild_id, interaction.user.id, name.strip(), tracks)
        
        if tracks is None:
            await send_response(interaction, "Không tìm thấy playlist.", ephemeral=True)
            return
        if not tracks:
            await send_response(interaction, "Playlist trống.", ephemeral=True)
            return

        embed = discord.Embed(title=f"Playlist: {name} ({len(tracks)})")
        lines: list[str] = []
        for i, t in enumerate(tracks[:MAX_PLAYLIST_VIEW], start=1):
            lines.append(f"{i}. {t.title} ({format_ms(t.length)})")
        embed.description = "\n".join(lines)

        if interaction.response.is_done():
            await interaction.followup.send(embed=embed, ephemeral=True)
        else:
            await interaction.response.send_message(embed=embed, ephemeral=True)

    @app_commands.command(name="addtrack", description="Thêm bài vào playlist")
    @app_commands.describe(name="Tên playlist", query="Query hoặc URL (bỏ trống để lấy bài đang phát)")
    async def addtrack(self, interaction: discord.Interaction, name: str, query: str | None = None) -> None:
        if not interaction.guild_id:
            await send_response(interaction, "Lệnh này chỉ dùng trong server.", ephemeral=True)
            return

        name = name.strip()
        if not name:
            await send_response(interaction, "Tên playlist không hợp lệ.", ephemeral=True)
            return

        track: wavelink.Playable | None = None
        if query is None or not query.strip():
            player = await get_player(interaction, connect=False)
            track = player.current if player else None
            if track is None:
                await send_response(interaction, "Bạn cần nhập query hoặc phải có bài đang phát.", ephemeral=True)
                return
        else:
            await interaction.response.defer(thinking=True)
            try:
                results = await asyncio.wait_for(
                    wavelink.Playable.search(query.strip()),
                    timeout=SEARCH_TIMEOUT,
                )
            except asyncio.TimeoutError:
                await send_response(interaction, "Tìm kiếm quá lâu, vui lòng thử lại.", ephemeral=True)
                return
            except Exception:
                logger.exception("Playlist addtrack search failed guild=%s query=%r", interaction.guild_id, query)
                await send_response(interaction, "Không thể tìm track (lỗi tìm kiếm).", ephemeral=True)
                return
            if not results:
                await send_response(interaction, "Không tìm thấy track.", ephemeral=True)
                return

            if isinstance(results, wavelink.Playlist):
                added = 0
                for t in results.tracks[:MAX_PLAYLIST_ADD]:
                    ok = await self._storage().add_playlist_track(interaction.guild_id, interaction.user.id, name, t)
                    if ok:
                        added += 1

                _clear_playlist_cache(interaction.guild_id, interaction.user.id, name)
                await send_response(interaction, f"Đã thêm {added} track từ playlist vào '{name}'.", ephemeral=True)
                return

            track = results[0]

        ok = await self._storage().add_playlist_track(interaction.guild_id, interaction.user.id, name, track)
        if ok:
            _clear_playlist_cache(interaction.guild_id, interaction.user.id, name)
        await send_response(interaction, "Đã thêm track." if ok else "Không tìm thấy playlist.", ephemeral=True)

    @app_commands.command(name="removetrack", description="Xóa bài khỏi playlist")
    @app_commands.describe(name="Tên playlist", index="Index trong playlist (1..)")
    async def removetrack(self, interaction: discord.Interaction, name: str, index: int) -> None:
        if not interaction.guild_id:
            await send_response(interaction, "Lệnh này chỉ dùng trong server.", ephemeral=True)
            return

        ok = await self._storage().remove_playlist_track(interaction.guild_id, interaction.user.id, name.strip(), int(index))
        if ok:
            _clear_playlist_cache(interaction.guild_id, interaction.user.id, name.strip())
        await send_response(interaction, "Đã xóa track." if ok else "Không thể xóa (playlist/index không hợp lệ).", ephemeral=True)

    @app_commands.command(name="clear", description="Xóa toàn bộ bài trong playlist")
    @app_commands.describe(name="Tên playlist")
    async def clear(self, interaction: discord.Interaction, name: str) -> None:
        if not interaction.guild_id:
            await send_response(interaction, "Lệnh này chỉ dùng trong server.", ephemeral=True)
            return

        ok = await self._storage().clear_playlist(interaction.guild_id, interaction.user.id, name.strip())
        if ok:
            _clear_playlist_cache(interaction.guild_id, interaction.user.id, name.strip())
        await send_response(interaction, "Đã clear playlist." if ok else "Không tìm thấy playlist.", ephemeral=True)

    @app_commands.command(name="play", description="Phát playlist")
    @app_commands.describe(name="Tên playlist")
    async def play(self, interaction: discord.Interaction, name: str) -> None:
        if not interaction.guild_id:
            await send_response(interaction, "Lệnh này chỉ dùng trong server.", ephemeral=True)
            return

        # Thử lấy từ cache trước
        tracks = _get_cached_playlist(interaction.guild_id, interaction.user.id, name.strip())
        if tracks is None:
            tracks = await self._storage().playlist_tracks(interaction.guild_id, interaction.user.id, name.strip())
            if tracks is not None:
                _set_cached_playlist(interaction.guild_id, interaction.user.id, name.strip(), tracks)
        
        if tracks is None:
            await send_response(interaction, "Không tìm thấy playlist.", ephemeral=True)
            return
        if not tracks:
            await send_response(interaction, "Playlist trống.", ephemeral=True)
            return

        await interaction.response.defer(thinking=True)

        async with guild_lock(interaction.guild_id):
            player = await get_player(interaction, connect=True)
            if not player:
                await send_response(interaction, "Bạn cần vào voice channel trước.", ephemeral=True)
                return

            if not ensure_same_channel(interaction, player):
                if getattr(player, "channel", None) is not None:
                    await send_response(
                        interaction,
                        f"Bot đang ở voice channel khác: {player.channel.mention}.",
                        ephemeral=True,
                    )
                else:
                    await send_response(interaction, "Bạn cần vào voice channel trước.", ephemeral=True)
                return

            settings = getattr(self.bot, "settings").get(interaction.guild_id)
            for t in tracks:
                t.extras = {
                    "requester_id": interaction.user.id,
                    "requester_name": getattr(interaction.user, "display_name", str(interaction.user)),
                }

            await player.queue.put_wait(tracks)
            if not player.playing:
                try:
                    nxt = player.queue.get()
                except wavelink.QueueEmpty:
                    await send_response(interaction, "Queue rỗng.", ephemeral=True)
                    return
                try:
                    await asyncio.wait_for(
                        player.play(nxt, volume=settings.volume_default),
                        timeout=PLAYER_OP_TIMEOUT,
                    )
                except asyncio.TimeoutError:
                    logger.warning("Play timeout guild=%s", interaction.guild_id)
                    new_player = await rebuild_player_session(self.bot, interaction, old=player)
                    if not new_player:
                        await send_response(interaction, "Không thể phát nhạc do phiên phát bị treo.", ephemeral=True)
                        return
                    player = new_player
                except Exception:
                    logger.exception("Failed to start playback guild=%s", interaction.guild_id)
                    await send_response(interaction, "Không thể phát nhạc. Vui lòng thử lại.", ephemeral=True)
                    return

            await _refresh_controller(self.bot, player)

        embed = build_controller_embed(self.bot, player, notice=f"Đã queue playlist '{name}' ({len(tracks)} bài).")
        await interaction.edit_original_response(embed=embed)

    @app_commands.command(name="savequeue", description="Lưu queue hiện tại thành playlist mới")
    @app_commands.describe(name="Tên playlist mới")
    async def savequeue(self, interaction: discord.Interaction, name: str) -> None:
        if not interaction.guild_id:
            await send_response(interaction, "Lệnh này chỉ dùng trong server.", ephemeral=True)
            return

        name = name.strip()
        if not name:
            await send_response(interaction, "Tên playlist không hợp lệ.", ephemeral=True)
            return

        player = await get_player(interaction, connect=False)
        if not player:
            await send_response(interaction, "Không có player đang hoạt động.", ephemeral=True)
            return

        # Thu thập tracks từ queue và current track
        tracks_to_save: list[wavelink.Playable] = []
        if player.current:
            tracks_to_save.append(player.current)
        tracks_to_save.extend(list(player.queue))

        if not tracks_to_save:
            await send_response(interaction, "Queue đang trống, không có gì để lưu.", ephemeral=True)
            return

        await interaction.response.defer(thinking=True)

        # Tạo playlist mới
        try:
            await self._storage().create_playlist(interaction.guild_id, interaction.user.id, name)
        except Exception:
            await send_response(interaction, f"Không thể tạo playlist '{name}' (có thể đã tồn tại).", ephemeral=True)
            return

        # Thêm các tracks vào playlist
        added = 0
        for track in tracks_to_save[:MAX_SAVE_QUEUE]:  # Giới hạn số bài tối đa
            ok = await self._storage().add_playlist_track(
                interaction.guild_id, interaction.user.id, name, track
            )
            if ok:
                added += 1

        await send_response(
            interaction,
            f"Đã lưu {added} bài vào playlist '{name}'.",
            ephemeral=True
        )


async def setup(bot: commands.Bot) -> None:
    await bot.add_cog(LibraryCog(bot))
    await bot.add_cog(PlaylistCog(bot))
