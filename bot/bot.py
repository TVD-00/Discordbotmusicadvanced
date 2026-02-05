# ##############################################################################
# MODULE: BOT CORE
# DESCRIPTION: Định nghĩa class MusicBot kế thừa từ commands.Bot.
#              Quản lý Lifecycle, Extensions, và Global Error Handling.
# ##############################################################################

from __future__ import annotations

import asyncio
import logging
import time
from typing import Any

import discord
from discord import app_commands
from discord.ext import commands
import wavelink

from bot.config import Config
from bot.music.controller import PlayerControlView, build_controller_embed
from bot.storage.memory import GuildSettingsStore
from bot.storage.sqlite_storage import SQLiteStorage
from bot.utils.errors import ChannelRestrictedError

logger = logging.getLogger(__name__)


# ------------------------------------------------------------------------------
# Class: BotCommandTree
# Purpose: Custom CommandTree để inject logic kiểm tra global (Global Interaction Check).
# ------------------------------------------------------------------------------
class BotCommandTree(app_commands.CommandTree):
    async def interaction_check(self, interaction: discord.Interaction, /) -> bool:
        client = self.client
        if not isinstance(client, MusicBot):
            return True

        return await client.global_interaction_check(interaction)


# ------------------------------------------------------------------------------
# Class: MusicBot
# Purpose: Class bot chính.
# ------------------------------------------------------------------------------
class MusicBot(commands.Bot):
    def __init__(self, config: Config) -> None:
        intents = discord.Intents.default()
        super().__init__(command_prefix="!", intents=intents, tree_cls=BotCommandTree)

        self.config = config
        self.started_at = time.monotonic()

        # In-memory store cho setting để truy xuất nhanh
        self.settings = GuildSettingsStore(
            default_volume=config.default_volume,
            default_announce_enabled=config.announce_nowplaying,
        )

        # Persistent storage (SQLite)
        self.storage = SQLiteStorage(config.db_path)

        # Cache cho allowed channels và overrides
        self.allowed_channels: dict[int, set[int]] = {}
        self.command_channel_overrides: dict[int, dict[str, int]] = {}

        # Danh sách các lệnh không bị giới hạn bởi whitelist channel
        self.unrestricted_commands: set[str] = {
            "help",
            "invite",
            "support",
            "vote",
            "ping",
            "statistics",
            "debug",
            "settings",
            "dj",
            "announce",
            "buttons",
            "restrict channel",
            "restrict list",
            "restrict clear",
            "unrestrict channel",
            "restrictcommand",
            "unrestrictcommand",
        }

        # Lưu reference message controller để update realtime
        self.controller_messages: dict[int, tuple[int, int]] = {}
        self._current_track: dict[int, wavelink.Playable] = {}
        self._previous_track: dict[int, wavelink.Playable] = {}

    # --------------------------------------------------------------------------
    # Method: setup_hook
    # Purpose: Khởi chạy khi bot bắt đầu. Kết nối DB, Lavalink, Load Cogs.
    # --------------------------------------------------------------------------
    # --------------------------------------------------------------------------
    # Method: setup_hook
    # Purpose: Khởi chạy khi bot bắt đầu. Kết nối DB, Lavalink, Load Cogs.
    # --------------------------------------------------------------------------
    async def setup_hook(self) -> None:
        # 1. Kết nối Database
        await self.storage.connect()

        try:
            # 2. Load settings từ DB vào Memory
            loaded = await self.storage.load_guild_settings_all()
            for gid, s in loaded.items():
                self.settings.set(gid, s)

            self.allowed_channels = await self.storage.load_allowed_channels_all()
            self.command_channel_overrides = await self.storage.load_command_restrictions_all()
        except Exception:
            logger.exception("Failed to load settings/restrictions from DB")

        # 2b. DB Maintenance - chạy khi startup
        try:
            # Prune liked tracks quá cũ (mặc định 365 ngày)
            pruned = await self.storage.prune_old_liked()
            if pruned > 0:
                logger.info("DB maintenance: pruned %d old liked tracks", pruned)

            # Log thống kê DB để theo dõi
            stats = await self.storage.get_db_stats()
            logger.info(
                "DB stats: settings=%d, channels=%d, restrictions=%d, liked=%d, playlists=%d, items=%d",
                stats.get("guild_settings", 0),
                stats.get("allowed_channels", 0),
                stats.get("command_restrictions", 0),
                stats.get("liked_tracks", 0),
                stats.get("playlists", 0),
                stats.get("playlist_items", 0),
            )
        except Exception:
            logger.exception("DB maintenance failed (non-critical)")

        # 3. Kết nối Lavalink Node
        nodes: list[wavelink.Node] = []
        for n in getattr(self.config, "lavalink_nodes", ()):  # type: ignore[attr-defined]
            nodes.append(
                wavelink.Node(
                    uri=n.uri,
                    password=n.password,
                    identifier=n.identifier,
                )
            )

        if not nodes:
            # Fallback an toàn (không nên xảy ra vì config đã validate)
            nodes.append(
                wavelink.Node(
                    uri=self.config.lavalink_uri,
                    password=self.config.lavalink_password,
                    identifier=self.config.lavalink_identifier,
                )
            )

        await wavelink.Pool.connect(nodes=nodes, client=self, cache_capacity=self.config.wavelink_cache_capacity)

        # 4. Load Extensions (Cogs)
        await self.load_extension("bot.cogs.music")
        await self.load_extension("bot.cogs.library")
        await self.load_extension("bot.cogs.meta")
        await self.load_extension("bot.cogs.restrict")

        # 5. Restore View (để nút bấm cũ vẫn hoạt động)
        self.add_view(PlayerControlView(self))

        # 6. Sync Slash Commands
        if self.config.dev_guild_id:
            guild = discord.Object(id=self.config.dev_guild_id)
            self.tree.copy_global_to(guild=guild)
            await self.tree.sync(guild=guild)
        else:
            await self.tree.sync()


    # --------------------------------------------------------------------------
    # Method: on_ready
    # Purpose: Event khi bot đã đăng nhập thành công vào Discord Gateway.
    # --------------------------------------------------------------------------
    async def on_ready(self) -> None:
        if not self.user:
            return
        logger.info("Logged in as %s (%s)", self.user, self.user.id)

        # Cleanup dữ liệu của guild đã rời bot
        try:
            active_guilds = {g.id for g in self.guilds}
            deleted = await self.storage.cleanup_orphaned_guilds(active_guilds)
            total = sum(deleted.values())
            if total > 0:
                logger.info("DB cleanup orphaned guilds: %s", deleted)
        except Exception:
            logger.exception("Failed to cleanup orphaned guilds (non-critical)")

    # --------------------------------------------------------------------------
    # Method: on_app_command_error
    # Purpose: Xử lý lỗi toàn cục cho Slash Commands.
    # --------------------------------------------------------------------------
    # --------------------------------------------------------------------------
    # Method: on_app_command_error
    # Purpose: Xử lý lỗi toàn cục cho Slash Commands.
    # --------------------------------------------------------------------------
    async def on_app_command_error(self, interaction: discord.Interaction, error: app_commands.AppCommandError) -> None:
        original = getattr(error, "original", error)

        if isinstance(error, app_commands.CheckFailure):
            message = str(error) or "Bạn không thể dùng lệnh này ở đây."
        else:
            logger.exception("App command error: %r", original)
            message = "Đã xảy ra lỗi khi xử lý lệnh. Vui lòng thử lại sau."

        if isinstance(error, app_commands.CommandOnCooldown):
            message = f"Lệnh đang cooldown. Thử lại sau {error.retry_after:.1f}s."

        try:
            if interaction.response.is_done():
                await interaction.followup.send(message, ephemeral=True)
            else:
                await interaction.response.send_message(message, ephemeral=True)
        except discord.HTTPException:
            pass


    # --------------------------------------------------------------------------
    # Method: global_interaction_check
    # Purpose: Logic kiểm tra quyền (Whitelist Channel / Command Restriction).
    # --------------------------------------------------------------------------
    # --------------------------------------------------------------------------
    # Method: global_interaction_check
    # Purpose: Logic kiểm tra quyền (Whitelist Channel / Command Restriction).
    # --------------------------------------------------------------------------
    async def global_interaction_check(self, interaction: discord.Interaction) -> bool:
        if interaction.guild_id is None or interaction.channel_id is None:
            return True

        cmd = interaction.command
        if cmd is None:
            return True

        cmd_name = cmd.qualified_name
        # Bỏ qua check cho các lệnh cơ bản
        if cmd_name in self.unrestricted_commands:
            return True

        # Check: Command Restriction (Cấm lệnh cụ thể ở channel khác)
        overrides = self.command_channel_overrides.get(interaction.guild_id, {})
        forced = overrides.get(cmd_name)
        if forced is not None and interaction.channel_id != forced:
            raise ChannelRestrictedError(f"Lệnh `{cmd_name}` chỉ dùng trong <#{forced}>.")

        # Check: Whitelist Channel (Chỉ cho phép dùng bot ở channel quy định)
        allowed = self.allowed_channels.get(interaction.guild_id, set())
        if allowed and interaction.channel_id not in allowed:
            channels = " ".join(f"<#{cid}>" for cid in sorted(allowed))
            raise ChannelRestrictedError(f"Server đang restrict. Dùng lệnh trong: {channels}")

        return True

    # --------------------------------------------------------------------------
    # Method: close
    # Purpose: Dọn dẹp tài nguyên khi tắt bot.
    #          - Disconnect tất cả voice clients
    #          - Đóng kết nối Database
    # --------------------------------------------------------------------------
    async def close(self) -> None:
        logger.info("Shutting down bot - cleaning up resources...")
        
        # 1. Disconnect tất cả players để tránh orphan connections
        disconnect_count = 0
        for vc in list(self.voice_clients):
            try:
                await vc.disconnect(force=True)
                disconnect_count += 1
            except Exception:
                logger.exception("Failed to disconnect voice client during shutdown")
        
        if disconnect_count > 0:
            logger.info("Disconnected %d voice clients", disconnect_count)
        
        # 2. Đóng kết nối Lavalink Pool
        try:
            await wavelink.Pool.close()
        except Exception:
            logger.exception("Failed to close wavelink pool")
        
        # 3. Đóng kết nối Database
        try:
            await self.storage.close()
            logger.info("Database connection closed")
        except Exception:
            logger.exception("Failed to close DB")

        await super().close()
        logger.info("Bot shutdown complete")

    # --------------------------------------------------------------------------
    # Events: Wavelink (Lavalink)
    # --------------------------------------------------------------------------
    async def on_wavelink_node_ready(self, payload: wavelink.NodeReadyEventPayload) -> None:
        logger.info("Wavelink node ready: %r resumed=%s", payload.node, payload.resumed)

    async def on_wavelink_node_disconnected(self, payload: wavelink.NodeDisconnectedEventPayload) -> None:
        logger.warning("Wavelink node disconnected: %r", payload.node)

    async def on_wavelink_track_exception(self, payload: wavelink.TrackExceptionEventPayload) -> None:
        player = payload.player
        if player and player.guild:
            logger.warning(
                "Track exception guild=%s title=%r error=%r",
                player.guild.id,
                payload.track.title,
                payload.exception,
            )

            # Best-effort: cập nhật panel để user thấy trạng thái mới.
            await self.refresh_controller_message(player)

    async def on_wavelink_track_stuck(self, payload: wavelink.TrackStuckEventPayload) -> None:
        player = payload.player
        if not player or not player.guild:
            return

        logger.warning(
            "Track stuck guild=%s title=%r threshold=%s",
            player.guild.id,
            payload.track.title,
            payload.threshold,
        )

        # Best-effort: thử skip để tránh kẹt.
        try:
            await player.skip(force=True)
        except Exception:
            logger.exception("Failed to skip stuck track guild=%s", player.guild.id)

        await self.refresh_controller_message(player)

    async def on_wavelink_track_end(self, payload: wavelink.TrackEndEventPayload) -> None:
        player = payload.player
        if not player or not player.guild:
            return

        # Wavelink có AutoPlay/Queue internal. Chờ ngắn để nó kịp start bài tiếp theo.
        async def _delayed_refresh() -> None:
            await asyncio.sleep(0.7)
            if player.playing:
                return
            await self.refresh_controller_message(player)

        asyncio.create_task(_delayed_refresh())

    async def on_wavelink_track_start(self, payload: wavelink.TrackStartEventPayload) -> None:
        player = payload.player
        if not player or not player.guild:
            return

        # Cache lại bài hiện tại và bài trước đó để dùng cho tính năng /back hoặc /dislike
        current = payload.original or payload.track
        old_current = self._current_track.get(player.guild.id)
        if old_current and old_current.identifier != current.identifier:
            self._previous_track[player.guild.id] = old_current
        self._current_track[player.guild.id] = current

        # Cập nhật giao diện (Player Controller)
        await self.refresh_controller_message(player)

        # Thông báo bài đang phát (nếu bật setting)
        settings = self.settings.get(player.guild.id)
        if not settings.announce_enabled:
            return

        channel: discord.abc.Messageable | None = None
        if settings.announce_channel_id:
            maybe = self.get_channel(settings.announce_channel_id)
            if maybe is not None and hasattr(maybe, "send"):
                channel = maybe  # type: ignore[assignment]

        if channel is None and hasattr(player, "home"):
            maybe = getattr(player, "home")
            if maybe is not None and hasattr(maybe, "send"):
                channel = maybe  # type: ignore[assignment]

        if channel is None:
            return

        track = payload.track
        embed = discord.Embed(title="Now Playing")
        embed.description = f"{track.title} - {track.author}"
        if track.uri:
            embed.url = track.uri
        if track.artwork:
            embed.set_thumbnail(url=track.artwork)

        try:
            await channel.send(embed=embed)
        except discord.HTTPException:
            pass

    def get_previous_track(self, guild_id: int) -> wavelink.Playable | None:
        return self._previous_track.get(guild_id)

    # --------------------------------------------------------------------------
    # Method: refresh_controller_message
    # Purpose: Cập nhật nội dung embed/buttons của trình phát nhạc (Realtime UI).
    # --------------------------------------------------------------------------
    async def refresh_controller_message(self, player: wavelink.Player) -> None:
        if not player.guild:
            return

        ref = self.controller_messages.get(player.guild.id)
        if not ref:
            return

        channel_id, message_id = ref
        channel = self.get_channel(channel_id)

        if channel is None or not hasattr(channel, "fetch_message"):
            return

        try:
            message = await channel.fetch_message(message_id)  # type: ignore[attr-defined]
        except discord.NotFound:
            self.controller_messages.pop(player.guild.id, None)
            return
        except discord.HTTPException:
            return

        try:
            embed = build_controller_embed(self, player)
            settings = self.settings.get(player.guild.id)
            view = PlayerControlView(self) if settings.buttons_enabled else None
            await message.edit(embed=embed, view=view)
        except discord.HTTPException:
            return

    async def mark_controller_message(self, guild_id: int, *, notice: str) -> None:
        ref = self.controller_messages.get(guild_id)
        if not ref:
            return

        channel_id, message_id = ref
        channel = self.get_channel(channel_id)
        if channel is None or not hasattr(channel, "fetch_message"):
            return

        try:
            message = await channel.fetch_message(message_id)  # type: ignore[attr-defined]
        except discord.NotFound:
            self.controller_messages.pop(guild_id, None)
            return
        except discord.HTTPException:
            return

        embed = discord.Embed(title="Music Player")
        embed.description = notice

        try:
            await message.edit(embed=embed, view=None)
        except discord.HTTPException:
            return

    # --------------------------------------------------------------------------
    # Event: on_wavelink_inactive_player
    # Purpose: Tự động ngắt kết nối khi bot không phát nhạc quá lâu.
    # --------------------------------------------------------------------------
    async def on_wavelink_inactive_player(self, player: wavelink.Player) -> None:
        if not player.guild:
            return

        settings = self.settings.get(player.guild.id)
        if settings.stay_247:
            return

        try:
            await player.disconnect()
        except Exception:
            logger.exception("Failed to disconnect inactive player guild=%s", player.guild.id)
            return

        await self.mark_controller_message(
            player.guild.id,
            notice="Đã rời voice channel do không hoạt động. Dùng /play để phát lại.",
        )

    # --------------------------------------------------------------------------
    # Event: on_voice_state_update
    # Purpose: Tự động ngắt kết nối khi mọi người rời khỏi kênh thoại.
    # --------------------------------------------------------------------------
    async def on_voice_state_update(
        self,
        member: discord.Member,
        before: discord.VoiceState,
        after: discord.VoiceState,
    ) -> None:
        if not before.channel:
            return

        guild = member.guild
        vc = guild.voice_client
        if not isinstance(vc, wavelink.Player):
            return

        if vc.channel != before.channel:
            return

        humans_left = any(not m.bot for m in before.channel.members)
        if humans_left:
            return

        settings = self.settings.get(guild.id)
        if settings.stay_247:
            return

        try:
            await vc.disconnect()
        except Exception:
            logger.exception("Failed to disconnect empty channel player guild=%s", guild.id)
            return

        await self.mark_controller_message(
            guild.id,
            notice="Đã rời voice channel vì không còn ai trong kênh. Dùng /play để phát lại.",
        )
