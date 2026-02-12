# ##############################################################################
# MODULE: BOT CORE
# DESCRIPTION: Định nghĩa class MusicBot kế thừa từ commands.Bot.
#              Quản lý Lifecycle, Extensions, và Global Error Handling.
# ##############################################################################

from __future__ import annotations

import asyncio
import logging
import time
from typing import Any, cast

import discord
from discord import app_commands
from discord.ext import commands
import wavelink

from bot.config import Config
from bot.music.controller import PlayerControlView, build_controller_embed
from bot.storage.memory import GuildSettingsStore
from bot.storage.sqlite_storage import SQLiteStorage
from bot.utils import constants
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
        intents.message_content = True
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

        # Tracking lỗi theo node để tự động chuyển node khi có vấn đề
        # Key: node identifier, Value: số lỗi trong khoảng thời gian gần đây
        self._node_error_counts: dict[str, int] = {}
        self._node_error_threshold = 3  # Số lỗi tối đa trước khi chuyển node
        self._node_error_window = 60  # Giây - reset error count sau khoảng này
        self._node_last_error_time: dict[str, float] = {}
        self._using_primary_node = False
        self._primary_node_identifier: str | None = None
        self._primary_health_task: asyncio.Task[None] | None = None

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

        # 3. Kết nối Lavalink Node với chiến lược fallback
        await self._connect_lavalink_with_fallback()

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
    # Method: _connect_lavalink_with_fallback
    # Purpose: Kết nối Lavalink theo chiến lược: primary trước, fallback sau.
    # --------------------------------------------------------------------------
    def _connected_node_identifiers(self) -> set[str]:
        try:
            pool_nodes = wavelink.Pool.nodes
        except Exception:
            return set()

        return {
            identifier
            for identifier, node in pool_nodes.items()
            if node.status == wavelink.NodeStatus.CONNECTED
        }

    async def _connect_node_configs(
        self,
        node_configs: list[Any],
        *,
        reason: str,
        retries: int | None = None,
    ) -> set[str]:
        if not node_configs:
            return self._connected_node_identifiers()

        pool_nodes = wavelink.Pool.nodes
        node_retries = self.config.lavalink_node_retries if retries is None else retries

        nodes_to_add: list[wavelink.Node] = []
        has_disconnected = False

        for cfg in node_configs:
            existing = pool_nodes.get(cfg.identifier)
            if existing is None:
                nodes_to_add.append(
                    wavelink.Node(
                        uri=cfg.uri,
                        password=cfg.password,
                        identifier=cfg.identifier,
                        retries=node_retries,
                    )
                )
                continue

            if existing.status != wavelink.NodeStatus.CONNECTED:
                has_disconnected = True

        if nodes_to_add:
            try:
                logger.info("Connecting %d Lavalink node(s) (%s)", len(nodes_to_add), reason)
                await wavelink.Pool.connect(
                    nodes=nodes_to_add,
                    client=self,
                    cache_capacity=self.config.wavelink_cache_capacity,
                )
            except Exception:
                logger.exception("Failed to connect Lavalink node(s) (%s)", reason)

        if has_disconnected:
            try:
                logger.info("Reconnecting disconnected Lavalink node(s) (%s)", reason)
                await wavelink.Pool.reconnect()
            except Exception:
                logger.exception("Failed to reconnect Lavalink node(s) (%s)", reason)

        if nodes_to_add or has_disconnected:
            await asyncio.sleep(1)

        return self._connected_node_identifiers()

    async def _switch_players_to_node(self, target_node_id: str) -> int:
        target = wavelink.Pool.nodes.get(target_node_id)
        if target is None or target.status != wavelink.NodeStatus.CONNECTED:
            return 0

        switched = 0
        for vc in list(self.voice_clients):
            if not isinstance(vc, wavelink.Player):
                continue

            node = getattr(vc, "node", None)
            if node is None or node.identifier == target_node_id:
                continue

            switch_node = getattr(vc, "switch_node", None)
            if switch_node is None:
                continue

            try:
                await asyncio.wait_for(switch_node(target), timeout=constants.PLAYER_OP_TIMEOUT)
                switched += 1
            except Exception:
                guild_id = vc.guild.id if vc.guild else "unknown"
                logger.exception(
                    "Failed to switch player guild=%s to node=%s",
                    guild_id,
                    target_node_id,
                )

        return switched

    async def _connect_lavalink_with_fallback(self) -> None:
        primary = self.config.primary_lavalink_node
        fallback_configs = self.config.fallback_lavalink_nodes

        # Biến theo dõi trạng thái
        self._using_primary_node = False
        self._primary_node_identifier: str | None = primary.identifier if primary else None

        connected_ids: set[str] = set()

        if primary:
            logger.info("Trying primary Lavalink node: %s (%s)", primary.identifier, primary.uri)
            connected_ids = await self._connect_node_configs([primary], reason="primary")
            self._using_primary_node = primary.identifier in connected_ids

            if self._using_primary_node:
                logger.info("Primary Lavalink node connected successfully: %s", primary.identifier)
            else:
                logger.warning("Primary Lavalink node is unavailable: %s", primary.identifier)

        if fallback_configs:
            fallback_reason = "fallback (primary unavailable)" if not self._using_primary_node else "fallback warmup"
            connected_ids = await self._connect_node_configs(list(fallback_configs), reason=fallback_reason)

        if not connected_ids:
            raise RuntimeError("Failed to connect any Lavalink node")

        if primary and primary.identifier in connected_ids:
            self._using_primary_node = True

        logger.info("Connected Lavalink nodes: %s", ", ".join(sorted(connected_ids)))

        if primary and fallback_configs:
            self._start_primary_health_check()

    def _start_primary_health_check(self) -> None:
        """Khởi động background task kiểm tra primary node định kỳ."""
        interval = self.config.lavalink_primary_health_interval
        if interval <= 0:
            logger.info("Primary health check disabled (interval=0)")
            return

        existing_task = getattr(self, "_primary_health_task", None)
        if existing_task and not existing_task.done():
            return

        async def _health_check_loop() -> None:
            await self.wait_until_ready()
            while not self.is_closed():
                await asyncio.sleep(interval)

                primary = self.config.primary_lavalink_node
                if not primary:
                    continue

                connected = self._connected_node_identifiers()
                if primary.identifier not in connected:
                    await self._connect_node_configs([primary], reason="primary health-check", retries=1)
                    connected = self._connected_node_identifiers()

                if primary.identifier not in connected:
                    self._using_primary_node = False
                    logger.debug("Health check: primary node %s vẫn chưa sẵn sàng", primary.identifier)
                    continue

                switched = await self._switch_players_to_node(primary.identifier)
                self._using_primary_node = True

                if switched > 0:
                    logger.info(
                        "Primary node %s online, switched %d active player(s)",
                        primary.identifier,
                        switched,
                    )

        self._primary_health_task = asyncio.create_task(
            _health_check_loop(),
            name="primary-node-health-check",
        )


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

        # 1. Dừng health-check task để tránh task nền còn chạy khi bot tắt.
        health_task = self._primary_health_task
        if health_task and not health_task.done():
            health_task.cancel()
            try:
                await health_task
            except asyncio.CancelledError:
                pass
            except Exception:
                logger.exception("Failed to cancel primary health-check task")

        self._primary_health_task = None
        
        # 2. Disconnect tất cả players để tránh orphan connections
        disconnect_count = 0
        for vc in list(self.voice_clients):
            try:
                await asyncio.wait_for(vc.disconnect(force=True), timeout=constants.PLAYER_OP_TIMEOUT)
                disconnect_count += 1
            except asyncio.TimeoutError:
                logger.warning("Timeout disconnect voice client during shutdown")
            except Exception:
                logger.exception("Failed to disconnect voice client during shutdown")
        
        if disconnect_count > 0:
            logger.info("Disconnected %d voice clients", disconnect_count)
        
        # 3. Đóng kết nối Lavalink Pool
        try:
            await wavelink.Pool.close()
        except Exception:
            logger.exception("Failed to close wavelink pool")
        
        # 4. Đóng kết nối Database
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

        if self._primary_node_identifier and payload.node.identifier == self._primary_node_identifier:
            self._using_primary_node = True

    async def on_wavelink_node_disconnected(self, payload: Any) -> None:
        logger.warning("Wavelink node disconnected: %r", payload.node)

        if self._primary_node_identifier and payload.node.identifier == self._primary_node_identifier:
            self._using_primary_node = False

    async def on_wavelink_track_exception(self, payload: wavelink.TrackExceptionEventPayload) -> None:
        player = payload.player
        if player and player.guild:
            logger.warning(
                "Track exception guild=%s title=%r error=%r",
                player.guild.id,
                payload.track.title,
                payload.exception,
            )

            # Tracking lỗi node để xem xét chuyển node
            await self._record_node_error(player)

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

        # Tracking lỗi node để xem xét chuyển node
        await self._record_node_error(player)

        # Best-effort: thử skip để tránh kẹt.
        try:
            await player.skip(force=True)
        except Exception:
            logger.exception("Failed to skip stuck track guild=%s", player.guild.id)

        await self.refresh_controller_message(player)

    async def _record_node_error(self, player: wavelink.Player) -> None:
        """Ghi nhận lỗi từ node và xem xét chuyển node nếu lỗi quá nhiều."""
        if not player.node:
            return

        node_id = player.node.identifier
        now = time.monotonic()

        # Reset error count nếu đã qua thời gian window
        last_error = self._node_last_error_time.get(node_id, 0)
        if now - last_error > self._node_error_window:
            self._node_error_counts[node_id] = 0

        # Tăng error count
        self._node_error_counts[node_id] = self._node_error_counts.get(node_id, 0) + 1
        self._node_last_error_time[node_id] = now

        error_count = self._node_error_counts[node_id]
        logger.debug("Node %s error count: %d/%d", node_id, error_count, self._node_error_threshold)

        # Nếu vượt ngưỡng, thử chuyển sang node khác
        if error_count >= self._node_error_threshold:
            await self._try_switch_to_better_node(player, node_id)

    async def _try_switch_to_better_node(self, player: wavelink.Player, bad_node_id: str) -> None:
        """Thử chuyển player sang node khác tốt hơn, restore filter và queue."""
        pool_nodes = wavelink.Pool.nodes

        # Tìm node khác đang connected và có ít lỗi hơn
        best_node: wavelink.Node | None = None
        best_error_count = float("inf")

        for node_id, node in pool_nodes.items():
            if node_id == bad_node_id:
                continue
            if node.status != wavelink.NodeStatus.CONNECTED:
                continue

            node_errors = self._node_error_counts.get(node_id, 0)
            if node_errors < best_error_count:
                best_error_count = node_errors
                best_node = node

        if not best_node:
            logger.warning(
                "Node %s has too many errors but no alternative node available",
                bad_node_id
            )
            return

        # Reset error count của node cũ (cho lần sau)
        self._node_error_counts[bad_node_id] = 0

        guild = player.guild
        if not guild:
            return

        channel = player.channel
        if not channel:
            return

        logger.info(
            "Switching player guild=%s from node %s to %s",
            guild.id,
            bad_node_id,
            best_node.identifier,
        )

        switch_node = getattr(player, "switch_node", None)
        if switch_node is not None:
            try:
                await asyncio.wait_for(switch_node(best_node), timeout=constants.PLAYER_OP_TIMEOUT)
                await self.refresh_controller_message(player)
                logger.info(
                    "Switched player guild=%s to node %s via switch_node",
                    guild.id,
                    best_node.identifier,
                )
                return
            except Exception:
                logger.exception(
                    "switch_node failed guild=%s -> %s, fallback to rebuild",
                    guild.id,
                    best_node.identifier,
                )

        # Lưu state hiện tại để restore sau khi chuyển node
        saved_queue = list(player.queue)
        history_queue = player.queue.history if hasattr(player.queue, "history") else None
        saved_history: list[wavelink.Playable] = []
        if history_queue is not None:
            try:
                saved_history = list(cast(Any, history_queue))
            except Exception:
                saved_history = []
        saved_current = player.current
        saved_position = player.position  # ms
        saved_volume = player.volume
        saved_paused = player.paused
        saved_filters = player.filters
        saved_autoplay = player.autoplay
        saved_queue_mode = player.queue.mode if hasattr(player.queue, "mode") else None

        try:
            # Disconnect player cũ
            await asyncio.wait_for(player.disconnect(), timeout=constants.PLAYER_OP_TIMEOUT)

            # Đợi một chút để cleanup
            await asyncio.sleep(0.5)

            # Reconnect với node mới
            # Wavelink tự chọn node tốt nhất từ pool, nhưng vì bad_node đã bị đánh dấu
            # nhiều lỗi, các lần connect sau sẽ ưu tiên node khác
            new_player: wavelink.Player = await asyncio.wait_for(
                channel.connect(cls=wavelink.Player, self_deaf=True),  # type: ignore[arg-type]
                timeout=constants.VOICE_CONNECT_TIMEOUT,
            )

            # Restore volume
            await new_player.set_volume(saved_volume)

            # Restore filters - Quan trọng: phải re-apply filters
            if saved_filters:
                try:
                    await new_player.set_filters(saved_filters)
                    logger.debug("Restored filters for guild=%s after node switch", guild.id)
                except Exception:
                    logger.exception("Failed to restore filters for guild=%s", guild.id)

            # Restore autoplay mode
            new_player.autoplay = saved_autoplay

            # Restore queue mode
            if saved_queue_mode is not None and hasattr(new_player.queue, "mode"):
                new_player.queue.mode = saved_queue_mode

            # Restore queue
            for track in saved_queue:
                new_player.queue.put(track)

            # Restore history nếu có
            restored_history = new_player.queue.history if hasattr(new_player.queue, "history") else None
            if saved_history and restored_history is not None:
                for track in saved_history:
                    cast(Any, restored_history).put(track)

            # Resume playing từ vị trí cũ
            if saved_current:
                await new_player.play(saved_current, start=saved_position)
                if saved_paused:
                    await new_player.pause(True)

            # Cập nhật panel
            await self.refresh_controller_message(new_player)

            logger.info(
                "Successfully switched player guild=%s to node %s. Queue=%d, Filters=%s",
                guild.id,
                best_node.identifier,
                len(saved_queue),
                "restored" if saved_filters else "none",
            )

        except Exception:
            logger.exception(
                "Failed to switch player guild=%s to new node. Attempting recovery...",
                guild.id
            )
            # Thử reconnect lại với bất kỳ node nào
            try:
                recovery_player: wavelink.Player = await asyncio.wait_for(
                    channel.connect(cls=wavelink.Player, self_deaf=True),  # type: ignore[arg-type]
                    timeout=constants.VOICE_CONNECT_TIMEOUT,
                )
                await recovery_player.set_volume(saved_volume)
                if saved_current:
                    await asyncio.wait_for(
                        recovery_player.play(saved_current),
                        timeout=constants.PLAYER_OP_TIMEOUT,
                    )
                await self.refresh_controller_message(recovery_player)
            except Exception:
                logger.exception("Recovery failed for guild=%s", guild.id)

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
