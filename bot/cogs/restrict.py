# ##############################################################################
# MODULE: RESTRICT COG
# DESCRIPTION: Cog quản lý quyền hạn của bot trong server.
#              Cho phép:
#              - Whitelist: Chỉ cho bot hoạt động ở các channel cụ thể.
#              - Command Restriction: Cấm lệnh cụ thể ở channel khác.
# ##############################################################################

from __future__ import annotations

import discord
from discord import app_commands
from discord.ext import commands

from bot.storage.sqlite_storage import SQLiteStorage
from bot.utils.helpers import is_admin, send_response


# ------------------------------------------------------------------------------
# Helper: _storage
# Purpose: Truy cập vào SQLiteStorage.
# ------------------------------------------------------------------------------
def _storage(bot: commands.Bot) -> SQLiteStorage:
    return getattr(bot, "storage")


# ------------------------------------------------------------------------------
# Helper: _resolve_command_qualified_name
# Purpose: Tìm tên lệnh đầy đủ (qualified name) từ tên người dùng nhập.
#          Ví dụ: "list" -> "playlist list".
# ------------------------------------------------------------------------------
def _resolve_command_qualified_name(bot: commands.Bot, raw: str) -> str | None:
    name = raw.strip()
    if not name:
        return None

    candidates = {c.qualified_name: c for c in bot.tree.walk_commands()}
    if name in candidates:
        return name

    lowered = name.lower()
    for q in candidates.keys():
        if q.lower() == lowered:
            return q

    return None


# ------------------------------------------------------------------------------
# Group: Restrict Channel (Whitelist)
# Purpose: Quản lý danh sách kênh được phép sử dụng bot.
# ------------------------------------------------------------------------------
@app_commands.guild_only()
class RestrictGroup(commands.GroupCog, group_name="restrict"):
    def __init__(self, bot: commands.Bot) -> None:
        self.bot = bot

    @app_commands.command(name="channel", description="Giới hạn lệnh nhạc trong kênh này")
    @app_commands.describe(channel="Kênh được phép")
    async def channel(self, interaction: discord.Interaction, channel: discord.TextChannel) -> None:
        if not interaction.guild_id:
            await send_response(interaction, "Lệnh này chỉ dùng trong server.", ephemeral=True)
            return

        if not is_admin(interaction):
            await send_response(interaction, "Bạn không có quyền dùng lệnh này.", ephemeral=True)
            return

        await _storage(self.bot).add_allowed_channel(interaction.guild_id, channel.id)
        allowed = getattr(self.bot, "allowed_channels")
        allowed.setdefault(interaction.guild_id, set()).add(channel.id)

        await send_response(interaction, f"Đã restrict channel: {channel.mention}", ephemeral=True)

    @app_commands.command(name="list", description="Xem danh sách hạn chế hiện tại")
    async def list(self, interaction: discord.Interaction) -> None:
        if not interaction.guild_id:
            await send_response(interaction, "Lệnh này chỉ dùng trong server.", ephemeral=True)
            return

        allowed = getattr(self.bot, "allowed_channels").get(interaction.guild_id, set())
        overrides = getattr(self.bot, "command_channel_overrides").get(interaction.guild_id, {})

        embed = discord.Embed(title="Restrictions")
        if allowed:
            embed.add_field(name="Allowed channels", value="\n".join(f"<#{cid}>" for cid in sorted(allowed)), inline=False)
        else:
            embed.add_field(name="Allowed channels", value="(none)", inline=False)

        if overrides:
            lines = [f"{cmd}: <#{cid}>" for cmd, cid in sorted(overrides.items())]
            embed.add_field(name="Command overrides", value="\n".join(lines[:20]), inline=False)
        else:
            embed.add_field(name="Command overrides", value="(none)", inline=False)

        if interaction.response.is_done():
            await interaction.followup.send(embed=embed, ephemeral=True)
        else:
            await interaction.response.send_message(embed=embed, ephemeral=True)

    @app_commands.command(name="clear", description="Xóa mọi hạn chế kênh")
    async def clear(self, interaction: discord.Interaction) -> None:
        if not interaction.guild_id:
            await send_response(interaction, "Lệnh này chỉ dùng trong server.", ephemeral=True)
            return

        if not is_admin(interaction):
            await send_response(interaction, "Bạn không có quyền dùng lệnh này.", ephemeral=True)
            return

        await _storage(self.bot).clear_allowed_channels(interaction.guild_id)
        getattr(self.bot, "allowed_channels").pop(interaction.guild_id, None)

        await send_response(interaction, "Đã clear restrict channel.", ephemeral=True)


# ------------------------------------------------------------------------------
# Group: Unrestrict Channel
# Purpose: Gỡ bỏ giới hạn kênh.
# ------------------------------------------------------------------------------
@app_commands.guild_only()
class UnrestrictGroup(commands.GroupCog, group_name="unrestrict"):
    def __init__(self, bot: commands.Bot) -> None:
        self.bot = bot

    @app_commands.command(name="channel", description="Gỡ hạn chế cho kênh này")
    @app_commands.describe(channel="Kênh cần gỡ")
    async def channel(self, interaction: discord.Interaction, channel: discord.TextChannel) -> None:
        if not interaction.guild_id:
            await send_response(interaction, "Lệnh này chỉ dùng trong server.", ephemeral=True)
            return

        if not is_admin(interaction):
            await send_response(interaction, "Bạn không có quyền dùng lệnh này.", ephemeral=True)
            return

        await _storage(self.bot).remove_allowed_channel(interaction.guild_id, channel.id)
        allowed = getattr(self.bot, "allowed_channels").get(interaction.guild_id)
        if allowed:
            allowed.discard(channel.id)
            if not allowed:
                getattr(self.bot, "allowed_channels").pop(interaction.guild_id, None)

        await send_response(interaction, f"Đã unrestrict channel: {channel.mention}", ephemeral=True)


# ------------------------------------------------------------------------------
# Class: RestrictCommandCog
# Purpose: Quản lý giới hạn lệnh cụ thể (Command Overrides).
# ------------------------------------------------------------------------------
class RestrictCommandCog(commands.Cog):
    def __init__(self, bot: commands.Bot) -> None:
        self.bot = bot

    @app_commands.command(name="restrictcommand", description="Cấm dùng lệnh cụ thể trong kênh")
    @app_commands.describe(command="Tên lệnh (vd: play, playlist play)", channel="Kênh được phép")
    @app_commands.guild_only()
    async def restrictcommand(self, interaction: discord.Interaction, command: str, channel: discord.TextChannel) -> None:
        if not interaction.guild_id:
            await send_response(interaction, "Lệnh này chỉ dùng trong server.", ephemeral=True)
            return

        if not is_admin(interaction):
            await send_response(interaction, "Bạn không có quyền dùng lệnh này.", ephemeral=True)
            return

        qualified = _resolve_command_qualified_name(self.bot, command)
        if not qualified:
            await send_response(interaction, "Không tìm thấy command name.", ephemeral=True)
            return

        await _storage(self.bot).set_command_restriction(interaction.guild_id, qualified, channel.id)
        overrides = getattr(self.bot, "command_channel_overrides")
        overrides.setdefault(interaction.guild_id, {})[qualified] = channel.id

        await send_response(interaction, f"Đã restrict `{qualified}` -> {channel.mention}", ephemeral=True)

    @app_commands.command(name="unrestrictcommand", description="Gỡ cấm dùng lệnh")
    @app_commands.describe(command="Tên lệnh (vd: play, playlist play)")
    @app_commands.guild_only()
    async def unrestrictcommand(self, interaction: discord.Interaction, command: str) -> None:
        if not interaction.guild_id:
            await send_response(interaction, "Lệnh này chỉ dùng trong server.", ephemeral=True)
            return

        if not is_admin(interaction):
            await send_response(interaction, "Bạn không có quyền dùng lệnh này.", ephemeral=True)
            return

        qualified = _resolve_command_qualified_name(self.bot, command)
        if not qualified:
            await send_response(interaction, "Không tìm thấy command name.", ephemeral=True)
            return

        await _storage(self.bot).clear_command_restriction(interaction.guild_id, qualified)
        overrides = getattr(self.bot, "command_channel_overrides").get(interaction.guild_id)
        if overrides:
            overrides.pop(qualified, None)

        await send_response(interaction, f"Đã gỡ restrict cho `{qualified}`", ephemeral=True)


async def setup(bot: commands.Bot) -> None:
    await bot.add_cog(RestrictGroup(bot))
    await bot.add_cog(UnrestrictGroup(bot))
    await bot.add_cog(RestrictCommandCog(bot))
