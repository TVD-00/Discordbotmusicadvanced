# ##############################################################################
# MODULE: META COG
# DESCRIPTION: Cog chứa các lệnh thông tin chung (Help, Ping, Statistics).
# ##############################################################################

from __future__ import annotations

import io
import platform
import time

import discord
from discord import app_commands
from discord.ext import commands
import wavelink


# ------------------------------------------------------------------------------
# Class: MetaCog
# Purpose: Quản lý các lệnh meta không liên quan trực tiếp đến nhạc.
# ------------------------------------------------------------------------------
class MetaCog(commands.Cog):
    def __init__(self, bot: commands.Bot) -> None:
        self.bot = bot

    async def _send(self, interaction: discord.Interaction, content: str, *, ephemeral: bool = False) -> None:
        if interaction.response.is_done():
            await interaction.followup.send(content, ephemeral=ephemeral)
        else:
            await interaction.response.send_message(content, ephemeral=ephemeral)

    def _is_admin(self, interaction: discord.Interaction) -> bool:
        member = interaction.user if isinstance(interaction.user, discord.Member) else None
        if not member:
            return False
        perms = member.guild_permissions
        return perms.administrator or perms.manage_guild

    @app_commands.command(name="help", description="Xem hướng dẫn sử dụng các lệnh")
    @app_commands.guild_only()
    async def help(self, interaction: discord.Interaction) -> None:
        embed = discord.Embed(title="Help")
        embed.description = (
            "Các lệnh chính:\n"
            "- /play, /pause, /resume, /skip, /stop\n"
            "- /queue, /nowplaying, /seek, /volume\n"
            "- /like, /showliked, /playliked, /playlist ...\n"
            "- /announce, /dj, /restrict ...\n"
            "\nDùng /settings để xem cấu hình server."
        )

        if interaction.response.is_done():
            await interaction.followup.send(embed=embed, ephemeral=True)
        else:
            await interaction.response.send_message(embed=embed, ephemeral=True)

    @app_commands.command(name="invite", description="Lấy link mời bot vào server")
    @app_commands.guild_only()
    async def invite(self, interaction: discord.Interaction) -> None:
        if not self.bot.user:
            await self._send(interaction, "Bot chưa sẵn sàng.", ephemeral=True)
            return

        perms = discord.Permissions(
            send_messages=True,
            embed_links=True,
            read_message_history=True,
            connect=True,
            speak=True,
            use_voice_activation=True,
        )

        url = discord.utils.oauth_url(
            self.bot.user.id,
            permissions=perms,
            scopes=("bot", "applications.commands"),
        )

        await self._send(interaction, url, ephemeral=True)

    @app_commands.command(name="support", description="Lấy link tham gia server hỗ trợ")
    @app_commands.guild_only()
    async def support(self, interaction: discord.Interaction) -> None:
        config = getattr(self.bot, "config", None)
        url = getattr(config, "support_invite_url", None) if config else None
        if not url:
            await self._send(interaction, "Chưa cấu hình SUPPORT_INVITE_URL.", ephemeral=True)
            return
        await self._send(interaction, url, ephemeral=True)

    @app_commands.command(name="vote", description="Lấy link bình chọn cho bot")
    @app_commands.guild_only()
    async def vote(self, interaction: discord.Interaction) -> None:
        config = getattr(self.bot, "config", None)
        url = getattr(config, "vote_url", None) if config else None
        if not url:
            await self._send(interaction, "Chưa cấu hình VOTE_URL.", ephemeral=True)
            return
        await self._send(interaction, url, ephemeral=True)

    @app_commands.command(name="statistics", description="Xem thống kê hoạt động của bot")
    @app_commands.guild_only()
    async def statistics(self, interaction: discord.Interaction) -> None:
        embed = discord.Embed(title="Statistics")

        uptime_s = 0
        if hasattr(self.bot, "started_at"):
            uptime_s = int(time.monotonic() - getattr(self.bot, "started_at"))

        embed.add_field(name="Uptime", value=f"{uptime_s}s", inline=True)
        embed.add_field(name="Guilds", value=str(len(getattr(self.bot, "guilds", []))), inline=True)
        embed.add_field(name="WS", value=f"{int(getattr(self.bot, 'latency', 0) * 1000)}ms", inline=True)

        try:
            node = wavelink.Pool.get_node()
            stats = await node.fetch_stats()
            embed.add_field(name="Node", value=str(node.identifier), inline=True)
            embed.add_field(name="Players", value=str(len(node.players)), inline=True)
            embed.add_field(name="Playing", value=str(stats.playing), inline=True)
            embed.add_field(name="Node Uptime", value=f"{int(stats.uptime/1000)}s", inline=True)
        except Exception:
            embed.add_field(name="Node", value="(unavailable)", inline=True)

        if interaction.response.is_done():
            await interaction.followup.send(embed=embed, ephemeral=True)
        else:
            await interaction.response.send_message(embed=embed, ephemeral=True)

    @app_commands.command(name="debug", description="Thu thập dữ liệu lỗi để báo cáo")
    @app_commands.guild_only()
    async def debug(self, interaction: discord.Interaction) -> None:
        if not self._is_admin(interaction):
            await self._send(interaction, "Bạn không có quyền dùng lệnh này.", ephemeral=True)
            return

        lines: list[str] = []
        lines.append(f"platform={platform.platform()}")
        lines.append(f"python={platform.python_version()}")
        lines.append(f"discord_py={discord.__version__}")
        try:
            lines.append(f"wavelink={wavelink.__version__}")
        except Exception:
            pass

        config = getattr(self.bot, "config", None)
        if config:
            lines.append("\n[config]")
            lines.append(f"DEV_GUILD_ID={getattr(config, 'dev_guild_id', None)}")
            lines.append(f"LAVALINK_URI={getattr(config, 'lavalink_uri', None)}")
            lines.append(f"LAVALINK_IDENTIFIER={getattr(config, 'lavalink_identifier', None)}")
            lines.append(f"WAVELINK_CACHE_CAPACITY={getattr(config, 'wavelink_cache_capacity', None)}")
            lines.append(f"DB_PATH={getattr(config, 'db_path', None)}")
            lines.append(f"LOG_LEVEL={getattr(config, 'log_level', None)}")

            nodes = getattr(config, "lavalink_nodes", None)
            if nodes:
                try:
                    lines.append(f"LAVALINK_NODES={len(nodes)}")
                    for n in nodes:
                        # Không log password để tránh lộ thông tin nhạy cảm.
                        lines.append(f"- {getattr(n, 'identifier', None)} {getattr(n, 'uri', None)}")
                except Exception:
                    pass

        if interaction.guild_id:
            lines.append("\n[context]")
            lines.append(f"guild_id={interaction.guild_id}")
            lines.append(f"channel_id={interaction.channel_id}")
            lines.append(f"user_id={interaction.user.id}")

        try:
            node = wavelink.Pool.get_node()
            info = await node.fetch_info()
            stats = await node.fetch_stats()
            lines.append("\n[lavalink]")
            lines.append(f"node_id={node.identifier}")
            lines.append(f"node_uri={node.uri}")
            lines.append(f"version={info.version.semver}")
            lines.append(f"jvm={info.jvm}")
            lines.append(f"source_managers={','.join(info.source_managers)}")
            lines.append(f"players={stats.players}")
            lines.append(f"playing={stats.playing}")
            lines.append(f"uptime_ms={stats.uptime}")
        except Exception as e:
            lines.append("\n[lavalink]")
            lines.append(f"error={e!r}")

        data = "\n".join(lines).encode("utf-8")
        fp = io.BytesIO(data)
        fp.seek(0)
        file = discord.File(fp, filename="debug.txt")

        if interaction.response.is_done():
            await interaction.followup.send(file=file, ephemeral=True)
        else:
            await interaction.response.send_message(file=file, ephemeral=True)


async def setup(bot: commands.Bot) -> None:
    await bot.add_cog(MetaCog(bot))
