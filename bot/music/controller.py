# ##############################################################################
# MODULE: MUSIC CONTROLLER
# DESCRIPTION: Module quản lý giao diện điều khiển (UI) của trình phát nhạc.
#              Bao gồm Embed hiển thị thông tin và các Buttons/Select Menus.
# ##############################################################################

from __future__ import annotations

import asyncio
import logging
from typing import Any, cast

import discord
from discord.ext import commands
import wavelink

from bot.utils.constants import PLAYER_OP_TIMEOUT
from bot.utils.helpers import rebuild_player_session
from bot.utils.locks import guild_lock
from bot.utils.time import format_ms


logger = logging.getLogger(__name__)


# ------------------------------------------------------------------------------
# Helper: _queue_mode_text
# Purpose: Chuyển đổi trạng thái Loop thành text hiển thị.
# ------------------------------------------------------------------------------
def _queue_mode_text(mode: wavelink.QueueMode) -> str:
    return {
        wavelink.QueueMode.normal: "Tắt",
        wavelink.QueueMode.loop: "Bài hiện tại",
        wavelink.QueueMode.loop_all: "Toàn bộ",
    }.get(mode, "Tắt")


# ------------------------------------------------------------------------------
# Helper: _autoplay_text
# Purpose: Chuyển đổi trạng thái Autoplay thành text hiển thị.
# ------------------------------------------------------------------------------
def _autoplay_text(mode: wavelink.AutoPlayMode) -> str:
    # partial = chỉ tự phát bài kế tiếp trong queue, không gợi ý thêm bài
    return {
        wavelink.AutoPlayMode.enabled: "Bật",
        wavelink.AutoPlayMode.partial: "Tắt",
        wavelink.AutoPlayMode.disabled: "Tắt",
    }.get(mode, "Tắt")


# ------------------------------------------------------------------------------
# Helper: _filters_preset_text
# Purpose: Hiển thị tên preset filter đang áp dụng.
# ------------------------------------------------------------------------------
def _filters_preset_text(value: str | None) -> str:
    if not value:
        return "Tắt"
    return value


# ------------------------------------------------------------------------------
# Function: build_controller_embed
# Purpose: Tạo Embed chứa thông tin bài hát đang phát, thanh thời gian, và các trạng thái.
# ------------------------------------------------------------------------------
def build_controller_embed(
    bot: commands.Bot,
    player: wavelink.Player,
    *,
    notice: str | None = None,
) -> discord.Embed:
    guild_id = player.guild.id if player.guild else None
    settings = getattr(bot, "settings").get(guild_id) if guild_id else None

    current = player.current
    embed = discord.Embed(title="Trình phát nhạc")

    if notice:
        embed.add_field(name="Trạng thái", value=notice, inline=False)

    if current:
        title = current.title
        if current.uri:
            embed.description = f"[{title}]({current.uri})\n{current.author}"
        else:
            embed.description = f"{title}\n{current.author}"

        extras = dict(current.extras)
        requester_name = extras.get("requester_name")
        if requester_name:
            embed.add_field(name="Người yêu cầu", value=str(requester_name), inline=True)

        embed.add_field(
            name="Thời gian",
            value=f"{format_ms(player.position)} / {format_ms(current.length)}",
            inline=True,
        )
    else:
        embed.description = "Không có bài đang phát."

    embed.add_field(name="Âm lượng", value=str(player.volume), inline=True)
    embed.add_field(name="Lặp lại", value=_queue_mode_text(player.queue.mode), inline=True)
    embed.add_field(name="Tự động phát", value=_autoplay_text(player.autoplay), inline=True)
    embed.add_field(name="Hàng đợi", value=str(len(player.queue)), inline=True)

    if settings is not None:
        embed.add_field(name="Chế độ 24/7", value="Bật" if settings.stay_247 else "Tắt", inline=True)
        embed.add_field(
            name="Bộ lọc",
            value=_filters_preset_text(getattr(settings, "filters_preset", None)),
            inline=True,
        )

    embed.set_footer(text="Dùng các nút bên dưới để điều khiển")
    return embed


# ------------------------------------------------------------------------------
# Helpers: Permission Checks & Utils
# ------------------------------------------------------------------------------
def _is_dj_or_admin(bot: commands.Bot, interaction: discord.Interaction) -> bool:
    if not interaction.guild_id:
        return False

    member = interaction.user if isinstance(interaction.user, discord.Member) else None
    if not member:
        return False

    if member.guild_permissions.administrator or member.guild_permissions.manage_guild:
        return True

    settings = getattr(bot, "settings").get(interaction.guild_id)
    if not settings.dj_role_id:
        return True

    return any(r.id == settings.dj_role_id for r in member.roles)


def _is_admin(bot: commands.Bot, interaction: discord.Interaction) -> bool:
    member = interaction.user if isinstance(interaction.user, discord.Member) else None
    if not member:
        return False
    return member.guild_permissions.administrator or member.guild_permissions.manage_guild


def _author_voice_channel(
    interaction: discord.Interaction,
) -> discord.VoiceChannel | discord.StageChannel | None:
    member = interaction.user if isinstance(interaction.user, discord.Member) else None
    if not member or not member.voice or not member.voice.channel:
        return None
    return member.voice.channel


async def _get_player(interaction: discord.Interaction) -> wavelink.Player | None:
    if not interaction.guild:
        return None

    vc = interaction.guild.voice_client
    if isinstance(vc, wavelink.Player):
        return vc
    return None


async def _ensure_same_channel(interaction: discord.Interaction, player: wavelink.Player) -> bool:
    channel = _author_voice_channel(interaction)
    if not channel:
        await interaction.response.send_message("Bạn cần vào voice channel trước.", ephemeral=True)
        return False

    if player.channel != channel:
        await interaction.response.send_message(
            f"Bot đang ở voice channel khác: {player.channel.mention}.",
            ephemeral=True,
        )
        return False

    return True


async def _send_queue_ephemeral(bot: commands.Bot, interaction: discord.Interaction, player: wavelink.Player) -> None:
    embed = discord.Embed(title="Hàng đợi")

    if player.current:
        embed.add_field(
            name="Đang phát",
            value=f"{player.current.title} ({format_ms(player.current.length)})",
            inline=False,
        )

    if player.queue:
        lines: list[str] = []
        for i, t in enumerate(list(player.queue)[:10], start=1):
            lines.append(f"{i}. {t.title} ({format_ms(t.length)})")
        embed.add_field(name="Tiếp theo", value="\n".join(lines), inline=False)
    else:
        embed.add_field(name="Tiếp theo", value="(trống)", inline=False)

    await interaction.response.send_message(embed=embed, ephemeral=True)


# ------------------------------------------------------------------------------
# FILTER PRESETS REGISTRY
# Định nghĩa tất cả filter presets với cấu hình chi tiết
# ------------------------------------------------------------------------------
FILTER_PRESETS: dict[str, dict[str, Any]] = {
    # === OFF/RESET ===
    "off": {"description": "Tắt tất cả filter"},
    "reset": {"description": "Reset về mặc định"},

    # === QUALITY / CLARITY ===
    "balanced": {
        "description": "Cân bằng nhẹ, nghe lâu không mệt",
        "equalizer": [
            {"band": 0, "gain": 0.08},
            {"band": 1, "gain": 0.06},
            {"band": 2, "gain": 0.05},
            {"band": 5, "gain": -0.03},
            {"band": 6, "gain": -0.02},
            {"band": 10, "gain": 0.05},
            {"band": 11, "gain": 0.08},
            {"band": 12, "gain": 0.10},
            {"band": 13, "gain": 0.08},
        ],
    },
    "studio": {
        "description": "EQ nhẹ kiểu studio, rõ mà không gắt",
        "equalizer": [
            {"band": 0, "gain": 0.02},
            {"band": 1, "gain": 0.03},
            {"band": 2, "gain": 0.02},
            {"band": 3, "gain": -0.02},
            {"band": 4, "gain": -0.03},
            {"band": 5, "gain": -0.04},
            {"band": 7, "gain": 0.05},
            {"band": 8, "gain": 0.05},
            {"band": 11, "gain": 0.06},
            {"band": 12, "gain": 0.06},
        ],
    },
    "clarity": {
        "description": "Tăng độ rõ, giảm đục",
        "equalizer": [
            {"band": 0, "gain": -0.10},
            {"band": 1, "gain": -0.08},
            {"band": 2, "gain": -0.05},
            {"band": 3, "gain": -0.03},
            {"band": 5, "gain": -0.05},
            {"band": 6, "gain": 0.10},
            {"band": 7, "gain": 0.15},
            {"band": 8, "gain": 0.12},
            {"band": 9, "gain": 0.10},
            {"band": 11, "gain": 0.08},
            {"band": 12, "gain": 0.10},
        ],
    },
    "presence": {
        "description": "Nhấn mid để giọng nổi bật",
        "equalizer": [
            {"band": 0, "gain": -0.05},
            {"band": 1, "gain": -0.05},
            {"band": 2, "gain": -0.03},
            {"band": 5, "gain": 0.08},
            {"band": 6, "gain": 0.15},
            {"band": 7, "gain": 0.18},
            {"band": 8, "gain": 0.16},
            {"band": 9, "gain": 0.12},
            {"band": 10, "gain": 0.08},
            {"band": 11, "gain": 0.05},
        ],
    },
    "vocalclear": {
        "description": "Giọng rõ, bớt ù và chói",
        "equalizer": [
            {"band": 0, "gain": -0.20},
            {"band": 1, "gain": -0.15},
            {"band": 2, "gain": -0.10},
            {"band": 3, "gain": -0.05},
            {"band": 5, "gain": 0.10},
            {"band": 6, "gain": 0.18},
            {"band": 7, "gain": 0.22},
            {"band": 8, "gain": 0.18},
            {"band": 9, "gain": 0.12},
            {"band": 11, "gain": -0.05},
            {"band": 12, "gain": -0.08},
            {"band": 13, "gain": -0.10},
        ],
    },
    "vocalair": {
        "description": "Giọng sáng, thêm không khí",
        "equalizer": [
            {"band": 0, "gain": -0.10},
            {"band": 1, "gain": -0.08},
            {"band": 2, "gain": -0.05},
            {"band": 5, "gain": 0.08},
            {"band": 6, "gain": 0.12},
            {"band": 7, "gain": 0.15},
            {"band": 8, "gain": 0.12},
            {"band": 11, "gain": 0.12},
            {"band": 12, "gain": 0.15},
            {"band": 13, "gain": 0.12},
            {"band": 14, "gain": 0.08},
        ],
    },
    "warm": {
        "description": "Ấm, dịu tai",
        "equalizer": [
            {"band": 0, "gain": 0.12},
            {"band": 1, "gain": 0.10},
            {"band": 2, "gain": 0.08},
            {"band": 3, "gain": 0.05},
            {"band": 10, "gain": -0.05},
            {"band": 11, "gain": -0.08},
            {"band": 12, "gain": -0.10},
            {"band": 13, "gain": -0.12},
            {"band": 14, "gain": -0.12},
        ],
    },
    "bright": {
        "description": "Sáng, rõ chi tiết",
        "equalizer": [
            {"band": 0, "gain": -0.10},
            {"band": 1, "gain": -0.08},
            {"band": 2, "gain": -0.05},
            {"band": 10, "gain": 0.10},
            {"band": 11, "gain": 0.15},
            {"band": 12, "gain": 0.18},
            {"band": 13, "gain": 0.16},
            {"band": 14, "gain": 0.12},
        ],
    },
    "smooth": {
        "description": "Mượt, giảm gắt",
        "low_pass": {"smoothing": 12.0},
        "equalizer": [
            {"band": 0, "gain": 0.05},
            {"band": 1, "gain": 0.05},
            {"band": 2, "gain": 0.03},
            {"band": 10, "gain": -0.05},
            {"band": 11, "gain": -0.10},
            {"band": 12, "gain": -0.15},
            {"band": 13, "gain": -0.18},
            {"band": 14, "gain": -0.20},
        ],
    },
    "basscut": {
        "description": "Giảm bass, bớt ù",
        "equalizer": [
            {"band": 0, "gain": -0.30},
            {"band": 1, "gain": -0.25},
            {"band": 2, "gain": -0.20},
            {"band": 3, "gain": -0.15},
            {"band": 4, "gain": -0.10},
        ],
    },
    "trebleboost": {
        "description": "Tăng treble, sáng tiếng",
        "equalizer": [
            {"band": 10, "gain": 0.12},
            {"band": 11, "gain": 0.18},
            {"band": 12, "gain": 0.22},
            {"band": 13, "gain": 0.20},
            {"band": 14, "gain": 0.15},
        ],
    },
    "tightbass": {
        "description": "Bass gọn, bớt dày",
        "equalizer": [
            {"band": 0, "gain": 0.18},
            {"band": 1, "gain": 0.15},
            {"band": 2, "gain": 0.10},
            {"band": 3, "gain": -0.10},
            {"band": 4, "gain": -0.08},
            {"band": 5, "gain": -0.05},
        ],
    },
    "stage": {
        "description": "Không gian rộng, giọng tách lớp",
        "channel_mix": {
            "left_to_left": 1.0,
            "left_to_right": 0.18,
            "right_to_left": 0.18,
            "right_to_right": 1.0,
        },
        "equalizer": [
            {"band": 0, "gain": 0.04},
            {"band": 1, "gain": 0.03},
            {"band": 7, "gain": 0.06},
            {"band": 8, "gain": 0.06},
            {"band": 11, "gain": 0.06},
            {"band": 12, "gain": 0.08},
        ],
    },

    # === BASS MIX ===
    "bassclarity": {
        "description": "Bass dày nhưng vẫn rõ",
        "equalizer": [
            {"band": 0, "gain": 0.18},
            {"band": 1, "gain": 0.15},
            {"band": 2, "gain": 0.10},
            {"band": 3, "gain": -0.05},
            {"band": 4, "gain": -0.08},
            {"band": 5, "gain": -0.05},
            {"band": 6, "gain": 0.10},
            {"band": 7, "gain": 0.12},
            {"band": 8, "gain": 0.10},
            {"band": 11, "gain": 0.05},
            {"band": 12, "gain": 0.08},
        ],
    },
    "bassvocal": {
        "description": "Bass rõ, giọng nổi",
        "equalizer": [
            {"band": 0, "gain": 0.15},
            {"band": 1, "gain": 0.12},
            {"band": 2, "gain": 0.08},
            {"band": 5, "gain": 0.08},
            {"band": 6, "gain": 0.12},
            {"band": 7, "gain": 0.16},
            {"band": 8, "gain": 0.12},
            {"band": 10, "gain": 0.05},
            {"band": 11, "gain": 0.03},
        ],
    },
    "basswide": {
        "description": "Bass + sân khấu rộng",
        "channel_mix": {
            "left_to_left": 1.0,
            "left_to_right": 0.20,
            "right_to_left": 0.20,
            "right_to_right": 1.0,
        },
        "equalizer": [
            {"band": 0, "gain": 0.16},
            {"band": 1, "gain": 0.14},
            {"band": 2, "gain": 0.10},
            {"band": 11, "gain": 0.05},
            {"band": 12, "gain": 0.06},
        ],
    },
    "basssmooth": {
        "description": "Bass ấm, giảm gắt",
        "low_pass": {"smoothing": 12.0},
        "equalizer": [
            {"band": 0, "gain": 0.20},
            {"band": 1, "gain": 0.16},
            {"band": 2, "gain": 0.12},
            {"band": 10, "gain": -0.06},
            {"band": 11, "gain": -0.10},
            {"band": 12, "gain": -0.12},
            {"band": 13, "gain": -0.12},
        ],
    },

    # === PITCH/KEY ===
    "pitchup": {
        "description": "Tăng tông +1 (pitch)",
        "timescale": {"pitch": 1.05946, "speed": 1.0, "rate": 1.0},
    },
    "pitchdown": {
        "description": "Giảm tông -1 (pitch)",
        "timescale": {"pitch": 0.94387, "speed": 1.0, "rate": 1.0},
    },
    "pitchup2": {
        "description": "Tăng tông +2 (pitch)",
        "timescale": {"pitch": 1.12246, "speed": 1.0, "rate": 1.0},
    },
    "pitchdown2": {
        "description": "Giảm tông -2 (pitch)",
        "timescale": {"pitch": 0.89090, "speed": 1.0, "rate": 1.0},
    },
    
    # === BASS CATEGORY ===
    "bassboost": {
        "description": "Tăng bass mạnh",
        "equalizer": [
            {"band": 0, "gain": 0.35},  # Sub-bass 25Hz - tăng mạnh
            {"band": 1, "gain": 0.30},  # Bass 40Hz
            {"band": 2, "gain": 0.25},  # Bass 63Hz
            {"band": 3, "gain": 0.20},  # Low-mid 100Hz
            {"band": 4, "gain": 0.10},  # Low-mid 160Hz
            {"band": 5, "gain": 0.05},  # Mid 250Hz - fade out
        ],
    },
    "deepbass": {
        "description": "Bass sâu và nặng",
        "equalizer": [
            {"band": 0, "gain": 0.50},  # Sub-bass cực mạnh
            {"band": 1, "gain": 0.45},
            {"band": 2, "gain": 0.35},
            {"band": 3, "gain": 0.25},
            {"band": 4, "gain": 0.15},
            {"band": 5, "gain": 0.05},
        ],
    },
    "softbass": {
        "description": "Bass nhẹ nhàng, warm",
        "equalizer": [
            {"band": 0, "gain": 0.15},
            {"band": 1, "gain": 0.12},
            {"band": 2, "gain": 0.10},
            {"band": 3, "gain": 0.08},
            {"band": 4, "gain": 0.05},
        ],
    },
    "megabass": {
        "description": "Bass cực mạnh (cẩn thận tai!)",
        "equalizer": [
            {"band": 0, "gain": 0.75},  # Max sub-bass
            {"band": 1, "gain": 0.65},
            {"band": 2, "gain": 0.55},
            {"band": 3, "gain": 0.45},
            {"band": 4, "gain": 0.30},
            {"band": 5, "gain": 0.15},
        ],
    },
    "heavybass": {
        "description": "Bass nặng + treble boost",
        "equalizer": [
            {"band": 0, "gain": 0.45},
            {"band": 1, "gain": 0.40},
            {"band": 2, "gain": 0.30},
            {"band": 3, "gain": 0.15},
            {"band": 10, "gain": 0.10},  # Treble boost
            {"band": 11, "gain": 0.15},
            {"band": 12, "gain": 0.18},
            {"band": 13, "gain": 0.20},
        ],
    },
    
    # === SPEED/PITCH CATEGORY ===
    "nightcore": {
        "description": "Anime style - nhanh + cao",
        "timescale": {"pitch": 1.25, "speed": 1.20, "rate": 1.0},
    },
    "daycore": {
        "description": "Chậm + trầm",
        "timescale": {"pitch": 0.80, "speed": 0.80, "rate": 1.0},
    },
    "slowed": {
        "description": "Slowed nhẹ nhàng",
        "timescale": {"pitch": 0.92, "speed": 0.88, "rate": 1.0},
    },
    "superslow": {
        "description": "Slowed cực chậm + reverb feel",
        "timescale": {"pitch": 0.75, "speed": 0.70, "rate": 1.0},
        "tremolo": {"frequency": 0.5, "depth": 0.15},  # Tạo hiệu ứng reverb
    },
    "doubletime": {
        "description": "Gấp đôi tốc độ",
        "timescale": {"pitch": 1.0, "speed": 2.0, "rate": 1.0},
    },
    "chipmunk": {
        "description": "Giọng sóc cao vút",
        "timescale": {"pitch": 1.50, "speed": 1.0, "rate": 1.0},
    },
    "darthvader": {
        "description": "Giọng trầm như Darth Vader",
        "timescale": {"pitch": 0.60, "speed": 1.0, "rate": 1.0},
    },
    
    # === AESTHETIC CATEGORY ===
    "lofi": {
        "description": "Lo-fi chill vibes",
        "timescale": {"pitch": 0.95, "speed": 0.92, "rate": 1.0},
        "low_pass": {"smoothing": 15.0},  # Giảm smoothing để giữ detail
        "equalizer": [
            {"band": 0, "gain": 0.12},  # Warm bass
            {"band": 1, "gain": 0.10},
            {"band": 2, "gain": 0.08},
            {"band": 11, "gain": -0.10},  # Giảm treble harsh
            {"band": 12, "gain": -0.15},
            {"band": 13, "gain": -0.20},
            {"band": 14, "gain": -0.25},
        ],
    },
    "vaporwave": {
        "description": "Aesthetic 80s vibes",
        "timescale": {"pitch": 0.85, "speed": 0.85, "rate": 1.0},
        "tremolo": {"frequency": 2.0, "depth": 0.15},  # VHS wobble effect
        "equalizer": [
            {"band": 0, "gain": 0.15},
            {"band": 1, "gain": 0.12},
            {"band": 11, "gain": -0.08},
            {"band": 12, "gain": -0.12},
            {"band": 13, "gain": -0.15},
        ],
    },
    
    # === 3D/SPATIAL CATEGORY ===
    "8d": {
        "description": "8D audio xoay quanh đầu",
        "rotation": {"rotation_hz": 0.15},  # Chậm hơn để mượt
    },
    "reverse8d": {
        "description": "8D xoay ngược chiều",
        "rotation": {"rotation_hz": -0.15},
    },
    "stereowide": {
        "description": "Mở rộng stereo field",
        "channel_mix": {
            "left_to_left": 1.0,
            "left_to_right": 0.35,
            "right_to_left": 0.35,
            "right_to_right": 1.0,
        },
    },
    "mono": {
        "description": "Chuyển sang mono",
        "channel_mix": {
            "left_to_left": 0.5,
            "left_to_right": 0.5,
            "right_to_left": 0.5,
            "right_to_right": 0.5,
        },
    },
    
    # === MODULATION CATEGORY ===
    "vibrato": {
        "description": "Hiệu ứng vibrato mạnh",
        "vibrato": {"frequency": 8.0, "depth": 0.75},  # Tăng cường
    },
    "tremolo": {
        "description": "Hiệu ứng tremolo mạnh",
        "tremolo": {"frequency": 6.0, "depth": 0.80},  # Tăng cường
    },
    
    # === VOCAL CATEGORY ===
    "karaoke": {
        "description": "Giảm vocal (karaoke)",
        "karaoke": {
            "level": 1.0,
            "mono_level": 1.0,
            "filter_band": 200.0,
            "filter_width": 150.0,  # Rộng hơn để lọc tốt hơn
        },
    },
    "vocal": {
        "description": "Tăng vocal rõ ràng",
        "equalizer": [
            {"band": 0, "gain": -0.15},   # Giảm sub-bass
            {"band": 1, "gain": -0.10},
            {"band": 5, "gain": 0.15},    # Boost vocal range
            {"band": 6, "gain": 0.20},    # 400-630Hz
            {"band": 7, "gain": 0.20},    # 630-1000Hz
            {"band": 8, "gain": 0.15},    # 1-1.6kHz presence
            {"band": 9, "gain": 0.10},    # 1.6-2.5kHz clarity
            {"band": 10, "gain": 0.08},
        ],
    },
    
    # === GENRE EQ CATEGORY ===
    "rock": {
        "description": "EQ cho Rock/Metal",
        "equalizer": [
            {"band": 0, "gain": 0.25},   # Bass punch
            {"band": 1, "gain": 0.20},
            {"band": 2, "gain": 0.15},
            {"band": 5, "gain": -0.10},  # Scoop mids
            {"band": 6, "gain": -0.08},
            {"band": 10, "gain": 0.18},  # Treble attack
            {"band": 11, "gain": 0.22},
            {"band": 12, "gain": 0.20},
            {"band": 13, "gain": 0.15},
        ],
    },
    "pop": {
        "description": "EQ cân bằng cho Pop",
        "equalizer": [
            {"band": 0, "gain": 0.10},
            {"band": 1, "gain": 0.08},
            {"band": 6, "gain": 0.12},   # Vocal presence
            {"band": 7, "gain": 0.15},
            {"band": 8, "gain": 0.12},
            {"band": 11, "gain": 0.08},  # Sparkle
            {"band": 12, "gain": 0.10},
        ],
    },
    "electronic": {
        "description": "EQ cho EDM/Electronic",
        "equalizer": [
            {"band": 0, "gain": 0.40},   # Heavy sub-bass
            {"band": 1, "gain": 0.35},
            {"band": 2, "gain": 0.25},
            {"band": 3, "gain": 0.15},
            {"band": 10, "gain": 0.20},  # Crisp highs
            {"band": 11, "gain": 0.25},
            {"band": 12, "gain": 0.28},
            {"band": 13, "gain": 0.25},
            {"band": 14, "gain": 0.20},
        ],
    },
    "cinema": {
        "description": "Âm thanh cinematic rộng",
        "equalizer": [
            {"band": 0, "gain": 0.20},   # Deep bass
            {"band": 1, "gain": 0.18},
            {"band": 2, "gain": 0.12},
            {"band": 6, "gain": 0.10},   # Dialog clarity
            {"band": 7, "gain": 0.12},
            {"band": 11, "gain": 0.08},
            {"band": 12, "gain": 0.10},
        ],
        "channel_mix": {  # Slight stereo widening
            "left_to_left": 1.0,
            "left_to_right": 0.15,
            "right_to_left": 0.15,
            "right_to_right": 1.0,
        },
    },
    "party": {
        "description": "Bass + speed cho party",
        "timescale": {"pitch": 1.05, "speed": 1.08, "rate": 1.0},
        "equalizer": [
            {"band": 0, "gain": 0.35},
            {"band": 1, "gain": 0.30},
            {"band": 2, "gain": 0.22},
            {"band": 3, "gain": 0.15},
            {"band": 12, "gain": 0.12},
            {"band": 13, "gain": 0.15},
        ],
    },
    
    # === FUN/EFFECT CATEGORY ===
    "underwater": {
        "description": "Âm thanh như dưới nước",
        "low_pass": {"smoothing": 30.0},  # Heavy low pass
        "timescale": {"pitch": 0.92, "speed": 0.95, "rate": 1.0},
        "equalizer": [
            {"band": 10, "gain": -0.30},
            {"band": 11, "gain": -0.40},
            {"band": 12, "gain": -0.50},
            {"band": 13, "gain": -0.60},
            {"band": 14, "gain": -0.70},
        ],
    },
    "phone": {
        "description": "Âm thanh qua điện thoại cũ",
        "equalizer": [
            {"band": 0, "gain": -0.50},   # Cut bass
            {"band": 1, "gain": -0.45},
            {"band": 2, "gain": -0.35},
            {"band": 3, "gain": -0.20},
            {"band": 6, "gain": 0.25},    # Boost mid
            {"band": 7, "gain": 0.30},
            {"band": 8, "gain": 0.25},
            {"band": 11, "gain": -0.30},  # Cut treble
            {"band": 12, "gain": -0.40},
            {"band": 13, "gain": -0.50},
            {"band": 14, "gain": -0.60},
        ],
    },
    "radio": {
        "description": "Âm thanh radio vintage",
        "equalizer": [
            {"band": 0, "gain": -0.35},
            {"band": 1, "gain": -0.25},
            {"band": 2, "gain": -0.15},
            {"band": 5, "gain": 0.15},
            {"band": 6, "gain": 0.20},
            {"band": 7, "gain": 0.22},
            {"band": 8, "gain": 0.18},
            {"band": 12, "gain": -0.20},
            {"band": 13, "gain": -0.30},
            {"band": 14, "gain": -0.40},
        ],
        "tremolo": {"frequency": 0.8, "depth": 0.08},  # Slight AM radio effect
    },
    "distorted": {
        "description": "Âm thanh distortion nhẹ",
        "distortion": {
            "sin_offset": 0.0,
            "sin_scale": 1.0,
            "cos_offset": 0.0,
            "cos_scale": 1.0,
            "tan_offset": 0.0,
            "tan_scale": 1.0,
            "offset": 0.05,
            "scale": 1.05,
        },
        "equalizer": [
            {"band": 7, "gain": 0.15},
            {"band": 8, "gain": 0.20},
            {"band": 9, "gain": 0.15},
        ],
    },
}


# ------------------------------------------------------------------------------
# Function: apply_filter_preset
# Purpose: Áp dụng các bộ lọc âm thanh (Filters) dựa trên preset name.
# ------------------------------------------------------------------------------
async def apply_filter_preset(bot: commands.Bot, player: wavelink.Player, preset: str) -> None:
    preset = preset.strip().lower()
    
    if preset not in FILTER_PRESETS:
        raise ValueError(f"Unknown preset: {preset}")
    
    config = FILTER_PRESETS[preset]
    filters = wavelink.Filters()
    
    # Reset nếu là off/reset
    if preset in {"off", "reset"}:
        await player.set_filters(seek=True)
    else:
        # Áp dụng Equalizer
        if "equalizer" in config:
            filters.equalizer.set(bands=cast(Any, config["equalizer"]))
        
        # Áp dụng Timescale (pitch/speed/rate)
        if "timescale" in config:
            ts = config["timescale"]
            filters.timescale.set(
                pitch=ts.get("pitch", 1.0),
                speed=ts.get("speed", 1.0),
                rate=ts.get("rate", 1.0),
            )
        
        # Áp dụng Rotation (8D)
        if "rotation" in config:
            filters.rotation.set(rotation_hz=config["rotation"]["rotation_hz"])
        
        # Áp dụng Vibrato
        if "vibrato" in config:
            vib = config["vibrato"]
            filters.vibrato.set(
                frequency=vib.get("frequency", 2.0),
                depth=vib.get("depth", 0.5),
            )
        
        # Áp dụng Tremolo
        if "tremolo" in config:
            trem = config["tremolo"]
            filters.tremolo.set(
                frequency=trem.get("frequency", 2.0),
                depth=trem.get("depth", 0.5),
            )
        
        # Áp dụng Karaoke
        if "karaoke" in config:
            kar = config["karaoke"]
            filters.karaoke.set(
                level=kar.get("level", 1.0),
                mono_level=kar.get("mono_level", 1.0),
                filter_band=kar.get("filter_band", 220.0),
                filter_width=kar.get("filter_width", 100.0),
            )
        
        # Áp dụng Low Pass
        if "low_pass" in config:
            filters.low_pass.set(smoothing=config["low_pass"]["smoothing"])
        
        # Áp dụng Channel Mix
        if "channel_mix" in config:
            cm = config["channel_mix"]
            filters.channel_mix.set(
                left_to_left=cm.get("left_to_left", 1.0),
                left_to_right=cm.get("left_to_right", 0.0),
                right_to_left=cm.get("right_to_left", 0.0),
                right_to_right=cm.get("right_to_right", 1.0),
            )
        
        # Áp dụng Distortion
        if "distortion" in config:
            dist = config["distortion"]
            filters.distortion.set(
                sin_offset=dist.get("sin_offset", 0.0),
                sin_scale=dist.get("sin_scale", 1.0),
                cos_offset=dist.get("cos_offset", 0.0),
                cos_scale=dist.get("cos_scale", 1.0),
                tan_offset=dist.get("tan_offset", 0.0),
                tan_scale=dist.get("tan_scale", 1.0),
                offset=dist.get("offset", 0.0),
                scale=dist.get("scale", 1.0),
            )
        
        await player.set_filters(filters, seek=True)
    
    # Lưu preset vào settings
    if player.guild:
        settings = getattr(bot, "settings").get(player.guild.id)
        if hasattr(settings, "filters_preset"):
            settings.filters_preset = "off" if preset in {"off", "reset"} else preset

        if hasattr(bot, "storage"):
            try:
                await getattr(bot, "storage").upsert_guild_settings(player.guild.id, settings)
            except Exception:
                logger.exception("Failed to persist filters_preset guild=%s", player.guild.id)


# ------------------------------------------------------------------------------
# Function: get_filter_options
# Purpose: Tạo danh sách options cho Select Menu, chia thành nhiều trang nếu cần.
# ------------------------------------------------------------------------------
def get_filter_options(page: int = 0, page_size: int = 25) -> list[discord.SelectOption]:
    # Lấy danh sách filter options cho Select Menu.
    # Discord giới hạn 25 options, nên cần phân trang.
    # Danh sách filter theo category để dễ tìm
    filter_order = [
        # Cơ bản
        "off",
        # Quality/Clarity
        "balanced", "studio", "clarity", "presence", "vocalclear", "vocalair",
        "warm", "bright", "smooth", "basscut", "trebleboost", "tightbass", "stage",
        # Bass Mix
        "bassclarity", "bassvocal", "basswide", "basssmooth",
        # Bass
        "bassboost", "deepbass", "softbass", "megabass", "heavybass",
        # Pitch/Key
        "pitchup", "pitchdown", "pitchup2", "pitchdown2",
        # Speed/Pitch
        "nightcore", "daycore", "slowed", "superslow", "doubletime", 
        "chipmunk", "darthvader",
        # Aesthetic
        "lofi", "vaporwave",
        # 3D/Spatial
        "8d", "reverse8d", "stereowide", "mono",
        # Modulation
        "vibrato", "tremolo",
        # Vocal
        "karaoke", "vocal",
        # Genre EQ
        "rock", "pop", "electronic", "cinema", "party",
        # Fun/Effects
        "underwater", "phone", "radio", "distorted",
    ]
    
    # Lọc chỉ lấy các filter có trong FILTER_PRESETS (trừ reset)
    available = [f for f in filter_order if f in FILTER_PRESETS and f != "reset"]
    
    start = page * page_size
    end = start + page_size
    page_filters = available[start:end]
    
    options = []
    for name in page_filters:
        config = FILTER_PRESETS[name]
        label = f"{name.capitalize()}"
        description = config.get("description", "")[:50]  # Giới hạn 50 ký tự
        options.append(discord.SelectOption(
            label=label, 
            value=name, 
            description=description
        ))
    
    return options


# ------------------------------------------------------------------------------
# Class: FilterPresetSelect
# Purpose: Menu chọn filter preset trong giao diện điều khiển.
# ------------------------------------------------------------------------------
class FilterPresetSelect(discord.ui.Select):
    def __init__(self, bot: commands.Bot, page: int = 0) -> None:
        self._bot = bot
        self._page = page
        
        options = get_filter_options(page=page, page_size=25)
        
        super().__init__(
            placeholder=f"Chọn bộ lọc (trang {page + 1})",
            min_values=1,
            max_values=1,
            options=options,
            custom_id=f"music:filter_preset:{page}",
            row=4,
        )

    async def callback(self, interaction: discord.Interaction) -> None:
        if not interaction.guild_id:
            await interaction.response.send_message("Lệnh này chỉ dùng trong server.", ephemeral=True)
            return

        if not _is_dj_or_admin(self._bot, interaction):
            await interaction.response.send_message("Bạn không có quyền dùng filter.", ephemeral=True)
            return

        player = await _get_player(interaction)
        if not player:
            await interaction.response.send_message("Bot chưa ở trong voice channel.", ephemeral=True)
            return

        if not await _ensure_same_channel(interaction, player):
            return

        await interaction.response.defer()

        preset = self.values[0]
        async with guild_lock(interaction.guild_id):
            try:
                await asyncio.wait_for(
                    apply_filter_preset(self._bot, player, preset),
                    timeout=PLAYER_OP_TIMEOUT,
                )
            except asyncio.TimeoutError:
                logger.warning("Apply filter timeout guild=%s preset=%r", interaction.guild_id, preset)
                new_player = await rebuild_player_session(self._bot, interaction, old=player)
                if not new_player:
                    await interaction.followup.send("Không thể áp dụng filter do phiên phát bị treo.", ephemeral=True)
                    return
                player = new_player
            except wavelink.exceptions.LavalinkException:
                # Player không tồn tại trên Lavalink (404) hoặc session hết hạn -> rebuild
                logger.warning("LavalinkException applying filter, rebuilding guild=%s preset=%r", interaction.guild_id, preset)
                new_player = await rebuild_player_session(self._bot, interaction, old=player)
                if not new_player:
                    await interaction.followup.send("Phiên phát bị mất. Vui lòng thử lại.", ephemeral=True)
                    return
                player = new_player
                # Retry filter trên player mới
                try:
                    await asyncio.wait_for(
                        apply_filter_preset(self._bot, player, preset),
                        timeout=PLAYER_OP_TIMEOUT,
                    )
                except Exception:
                    logger.exception("Retry filter failed after rebuild guild=%s preset=%r", interaction.guild_id, preset)
                    await interaction.followup.send("Không thể áp dụng filter sau khi khôi phục phiên.", ephemeral=True)
                    return
            except ValueError:
                await interaction.followup.send("Preset không hợp lệ.", ephemeral=True)
                return
            except Exception:
                logger.exception("Failed to apply filter guild=%s preset=%r", interaction.guild_id, preset)
                await interaction.followup.send("Không thể áp dụng filter.", ephemeral=True)
                return

        embed = build_controller_embed(self._bot, player)
        await interaction.edit_original_response(embed=embed)


# ------------------------------------------------------------------------------
# Function: get_total_filter_pages
# Purpose: Tính tổng số trang filter dựa trên số lượng presets.
# ------------------------------------------------------------------------------
def get_total_filter_pages(page_size: int = 25) -> int:
    # Tính tổng số trang cho filter menu.
    # Lọc bỏ reset
    available = [k for k in FILTER_PRESETS.keys() if k != "reset"]
    return max(1, (len(available) + page_size - 1) // page_size)


# ------------------------------------------------------------------------------
# Class: PlayerControlView
# Purpose: View chứa các nút điều khiển (Pause, Skip, Stop, v.v.).
# ------------------------------------------------------------------------------
class PlayerControlView(discord.ui.View):
    def __init__(self, bot: commands.Bot, filter_page: int = 0) -> None:
        super().__init__(timeout=None)
        self._bot = bot
        self._filter_page = filter_page
        self._total_filter_pages = get_total_filter_pages()

        # Thêm filter select menu với trang hiện tại
        self.add_item(FilterPresetSelect(bot, page=filter_page))
        
        # Cập nhật label của nút Filter Page để hiển thị trang hiện tại
        for item in self.children:
            if isinstance(item, discord.ui.Button) and item.custom_id == "music:filter_page":
                item.label = f"Bộ lọc {filter_page + 1}/{self._total_filter_pages}"
                break

    async def _edit_message(
        self,
        interaction: discord.Interaction,
        player: wavelink.Player,
        *,
        notice: str | None = None,
    ) -> None:
        embed = build_controller_embed(self._bot, player, notice=notice)
        await interaction.response.edit_message(embed=embed)

    @discord.ui.button(
        label="Dừng/Phát",
        style=discord.ButtonStyle.secondary,
        custom_id="music:pause_resume",
        row=0,
    )
    async def pause_resume(self, interaction: discord.Interaction, _: discord.ui.Button) -> None:
        if not interaction.guild_id:
            await interaction.response.send_message("Lệnh này chỉ dùng trong server.", ephemeral=True)
            return

        player = await _get_player(interaction)
        if not player or not player.playing:
            await interaction.response.send_message("Không có bài đang phát.", ephemeral=True)
            return

        if not await _ensure_same_channel(interaction, player):
            return

        async with guild_lock(interaction.guild_id):
            try:
                await player.pause(not player.paused)
            except Exception:
                logger.exception("Failed pause/resume guild=%s", interaction.guild_id)
                await interaction.response.send_message("Không thể pause/resume.", ephemeral=True)
                return

        await self._edit_message(interaction, player)

    @discord.ui.button(label="Qua bài", style=discord.ButtonStyle.primary, custom_id="music:skip", row=0)
    async def skip(self, interaction: discord.Interaction, _: discord.ui.Button) -> None:
        if not interaction.guild_id:
            await interaction.response.send_message("Lệnh này chỉ dùng trong server.", ephemeral=True)
            return

        if not _is_dj_or_admin(self._bot, interaction):
            await interaction.response.send_message("Bạn không có quyền skip.", ephemeral=True)
            return

        player = await _get_player(interaction)
        if not player or not player.playing:
            await interaction.response.send_message("Không có bài đang phát.", ephemeral=True)
            return

        if not await _ensure_same_channel(interaction, player):
            return

        async with guild_lock(interaction.guild_id):
            try:
                old = player.current
                await player.skip(force=True)
            except Exception:
                logger.exception("Failed skip guild=%s", interaction.guild_id)
                await interaction.response.send_message("Không thể skip.", ephemeral=True)
                return

        notice = f"Đã skip '{old.title}'." if old else None
        await self._edit_message(interaction, player, notice=notice)

    @discord.ui.button(label="Dừng phát", style=discord.ButtonStyle.danger, custom_id="music:stop", row=0)
    async def stop_playback(self, interaction: discord.Interaction, _: discord.ui.Button) -> None:
        if not interaction.guild_id:
            await interaction.response.send_message("Lệnh này chỉ dùng trong server.", ephemeral=True)
            return

        if not _is_dj_or_admin(self._bot, interaction):
            await interaction.response.send_message("Bạn không có quyền stop.", ephemeral=True)
            return

        player = await _get_player(interaction)
        if not player:
            await interaction.response.send_message("Bot chưa ở trong voice channel.", ephemeral=True)
            return

        if not await _ensure_same_channel(interaction, player):
            return

        async with guild_lock(interaction.guild_id):
            try:
                player.queue.reset()
                await player.skip(force=True)
            except Exception:
                logger.exception("Failed stop guild=%s", interaction.guild_id)
                await interaction.response.send_message("Không thể stop.", ephemeral=True)
                return

        await self._edit_message(interaction, player, notice="Đã dừng phát và xóa hàng đợi.")

    @discord.ui.button(label="Thoát", style=discord.ButtonStyle.danger, custom_id="music:leave", row=0)
    async def leave(self, interaction: discord.Interaction, _: discord.ui.Button) -> None:
        if not interaction.guild_id:
            await interaction.response.send_message("Lệnh này chỉ dùng trong server.", ephemeral=True)
            return

        if not _is_dj_or_admin(self._bot, interaction):
            await interaction.response.send_message("Bạn không có quyền disconnect.", ephemeral=True)
            return

        player = await _get_player(interaction)
        if not player:
            await interaction.response.send_message("Bot chưa ở trong voice channel.", ephemeral=True)
            return

        if not await _ensure_same_channel(interaction, player):
            return

        await interaction.response.defer()

        async with guild_lock(interaction.guild_id):
            try:
                await asyncio.wait_for(player.disconnect(), timeout=PLAYER_OP_TIMEOUT)
            except asyncio.TimeoutError:
                logger.warning("Disconnect timeout guild=%s", interaction.guild_id)
                await interaction.followup.send("Disconnect bị timeout. Vui lòng thử lại.", ephemeral=True)
                return
            except Exception:
                logger.exception("Failed disconnect guild=%s", interaction.guild_id)
                await interaction.followup.send("Không thể disconnect.", ephemeral=True)
                return

        payload = {
            "embed": discord.Embed(title="Trình phát nhạc", description="Đã rời kênh thoại."),
            "view": None,
        }

        try:
            if interaction.message:
                await interaction.message.edit(**payload)
            else:
                await interaction.edit_original_response(**payload)
        except discord.HTTPException:
            pass

    @discord.ui.button(label="Hàng đợi", style=discord.ButtonStyle.secondary, custom_id="music:queue", row=0)
    async def queue(self, interaction: discord.Interaction, _: discord.ui.Button) -> None:
        if not interaction.guild_id:
            await interaction.response.send_message("Lệnh này chỉ dùng trong server.", ephemeral=True)
            return

        player = await _get_player(interaction)
        if not player:
            await interaction.response.send_message("Bot chưa ở trong voice channel.", ephemeral=True)
            return

        await _send_queue_ephemeral(self._bot, interaction, player)

    @discord.ui.button(label="Vol -", style=discord.ButtonStyle.secondary, custom_id="music:vol_down", row=1)
    async def vol_down(self, interaction: discord.Interaction, _: discord.ui.Button) -> None:
        if not interaction.guild_id:
            await interaction.response.send_message("Lệnh này chỉ dùng trong server.", ephemeral=True)
            return

        if not _is_dj_or_admin(self._bot, interaction):
            await interaction.response.send_message("Bạn không có quyền chỉnh volume.", ephemeral=True)
            return

        player = await _get_player(interaction)
        if not player:
            await interaction.response.send_message("Bot chưa ở trong voice channel.", ephemeral=True)
            return

        if not await _ensure_same_channel(interaction, player):
            return

        async with guild_lock(interaction.guild_id):
            try:
                new = max(0, min(int(player.volume) - 5, 100))
                await player.set_volume(new)
            except Exception:
                logger.exception("Failed vol down guild=%s", interaction.guild_id)
                await interaction.response.send_message("Không thể chỉnh volume.", ephemeral=True)
                return

        await self._edit_message(interaction, player)

    @discord.ui.button(label="Vol +", style=discord.ButtonStyle.secondary, custom_id="music:vol_up", row=1)
    async def vol_up(self, interaction: discord.Interaction, _: discord.ui.Button) -> None:
        if not interaction.guild_id:
            await interaction.response.send_message("Lệnh này chỉ dùng trong server.", ephemeral=True)
            return

        if not _is_dj_or_admin(self._bot, interaction):
            await interaction.response.send_message("Bạn không có quyền chỉnh volume.", ephemeral=True)
            return

        player = await _get_player(interaction)
        if not player:
            await interaction.response.send_message("Bot chưa ở trong voice channel.", ephemeral=True)
            return

        if not await _ensure_same_channel(interaction, player):
            return

        async with guild_lock(interaction.guild_id):
            try:
                new = max(0, min(int(player.volume) + 5, 100))
                await player.set_volume(new)
            except Exception:
                logger.exception("Failed vol up guild=%s", interaction.guild_id)
                await interaction.response.send_message("Không thể chỉnh volume.", ephemeral=True)
                return

        await self._edit_message(interaction, player)

    @discord.ui.button(label="-10s", style=discord.ButtonStyle.secondary, custom_id="music:seek_back", row=1)
    async def seek_back(self, interaction: discord.Interaction, _: discord.ui.Button) -> None:
        if not interaction.guild_id:
            await interaction.response.send_message("Lệnh này chỉ dùng trong server.", ephemeral=True)
            return

        if not _is_dj_or_admin(self._bot, interaction):
            await interaction.response.send_message("Bạn không có quyền seek.", ephemeral=True)
            return

        player = await _get_player(interaction)
        if not player or not player.current:
            await interaction.response.send_message("Không có bài đang phát.", ephemeral=True)
            return

        if not player.current.is_seekable:
            await interaction.response.send_message("Bài này không hỗ trợ seek.", ephemeral=True)
            return

        if not await _ensure_same_channel(interaction, player):
            return

        async with guild_lock(interaction.guild_id):
            try:
                ms = max(0, player.position - 10_000)
                await player.seek(ms)
            except Exception:
                logger.exception("Failed seek back guild=%s", interaction.guild_id)
                await interaction.response.send_message("Không thể seek.", ephemeral=True)
                return

        await self._edit_message(interaction, player)

    @discord.ui.button(label="+10s", style=discord.ButtonStyle.secondary, custom_id="music:seek_fwd", row=1)
    async def seek_fwd(self, interaction: discord.Interaction, _: discord.ui.Button) -> None:
        if not interaction.guild_id:
            await interaction.response.send_message("Lệnh này chỉ dùng trong server.", ephemeral=True)
            return

        if not _is_dj_or_admin(self._bot, interaction):
            await interaction.response.send_message("Bạn không có quyền seek.", ephemeral=True)
            return

        player = await _get_player(interaction)
        if not player or not player.current:
            await interaction.response.send_message("Không có bài đang phát.", ephemeral=True)
            return

        if not player.current.is_seekable:
            await interaction.response.send_message("Bài này không hỗ trợ seek.", ephemeral=True)
            return

        if not await _ensure_same_channel(interaction, player):
            return

        async with guild_lock(interaction.guild_id):
            try:
                ms = min(player.current.length, player.position + 10_000)
                await player.seek(ms)
            except Exception:
                logger.exception("Failed seek fwd guild=%s", interaction.guild_id)
                await interaction.response.send_message("Không thể seek.", ephemeral=True)
                return

        await self._edit_message(interaction, player)

    @discord.ui.button(label="Lặp lại", style=discord.ButtonStyle.secondary, custom_id="music:loop", row=1)
    async def loop(self, interaction: discord.Interaction, _: discord.ui.Button) -> None:
        if not interaction.guild_id:
            await interaction.response.send_message("Lệnh này chỉ dùng trong server.", ephemeral=True)
            return

        if not _is_dj_or_admin(self._bot, interaction):
            await interaction.response.send_message("Bạn không có quyền chỉnh loop.", ephemeral=True)
            return

        player = await _get_player(interaction)
        if not player:
            await interaction.response.send_message("Bot chưa ở trong voice channel.", ephemeral=True)
            return

        if not await _ensure_same_channel(interaction, player):
            return

        async with guild_lock(interaction.guild_id):
            try:
                current = player.queue.mode
                next_mode = {
                    wavelink.QueueMode.normal: wavelink.QueueMode.loop,
                    wavelink.QueueMode.loop: wavelink.QueueMode.loop_all,
                    wavelink.QueueMode.loop_all: wavelink.QueueMode.normal,
                }[current]
                player.queue.mode = next_mode
            except Exception:
                logger.exception("Failed loop guild=%s", interaction.guild_id)
                await interaction.response.send_message("Không thể chỉnh loop.", ephemeral=True)
                return

        await self._edit_message(interaction, player, notice=f"Chế độ lặp: {_queue_mode_text(player.queue.mode)}")

    @discord.ui.button(label="Trộn bài", style=discord.ButtonStyle.secondary, custom_id="music:shuffle", row=2)
    async def shuffle(self, interaction: discord.Interaction, _: discord.ui.Button) -> None:
        if not interaction.guild_id:
            await interaction.response.send_message("Lệnh này chỉ dùng trong server.", ephemeral=True)
            return

        if not _is_dj_or_admin(self._bot, interaction):
            await interaction.response.send_message("Bạn không có quyền shuffle.", ephemeral=True)
            return

        player = await _get_player(interaction)
        if not player:
            await interaction.response.send_message("Bot chưa ở trong voice channel.", ephemeral=True)
            return

        if not player.queue:
            await interaction.response.send_message("Hàng đợi đang trống.", ephemeral=True)
            return

        if not await _ensure_same_channel(interaction, player):
            return

        async with guild_lock(interaction.guild_id):
            try:
                player.queue.shuffle()
            except Exception:
                logger.exception("Failed shuffle guild=%s", interaction.guild_id)
                await interaction.response.send_message("Không thể shuffle.", ephemeral=True)
                return

        await self._edit_message(interaction, player, notice="Đã trộn hàng đợi.")

    @discord.ui.button(label="Tự động", style=discord.ButtonStyle.secondary, custom_id="music:autoplay", row=2)
    async def autoplay(self, interaction: discord.Interaction, _: discord.ui.Button) -> None:
        if not interaction.guild_id:
            await interaction.response.send_message("Lệnh này chỉ dùng trong server.", ephemeral=True)
            return

        if not _is_dj_or_admin(self._bot, interaction):
            await interaction.response.send_message("Bạn không có quyền chỉnh autoplay.", ephemeral=True)
            return

        player = await _get_player(interaction)
        if not player:
            await interaction.response.send_message("Bot chưa ở trong voice channel.", ephemeral=True)
            return

        if not await _ensure_same_channel(interaction, player):
            return

        async with guild_lock(interaction.guild_id):
            try:
                player.autoplay = (
                    wavelink.AutoPlayMode.partial
                    if player.autoplay is wavelink.AutoPlayMode.enabled
                    else wavelink.AutoPlayMode.enabled
                )
            except Exception:
                logger.exception("Failed autoplay toggle guild=%s", interaction.guild_id)
                await interaction.response.send_message("Không thể chỉnh autoplay.", ephemeral=True)
                return

        await self._edit_message(interaction, player, notice=f"Tự động phát: {_autoplay_text(player.autoplay)}")

    @discord.ui.button(label="24/7", style=discord.ButtonStyle.secondary, custom_id="music:247", row=2)
    async def stay_247(self, interaction: discord.Interaction, _: discord.ui.Button) -> None:
        if not interaction.guild_id:
            await interaction.response.send_message("Lệnh này chỉ dùng trong server.", ephemeral=True)
            return

        if not _is_admin(self._bot, interaction):
            await interaction.response.send_message("Chỉ admin mới dùng được 24/7.", ephemeral=True)
            return

        player = await _get_player(interaction)
        if not player:
            await interaction.response.send_message("Bot chưa ở trong voice channel.", ephemeral=True)
            return

        settings = getattr(self._bot, "settings").get(interaction.guild_id)
        settings.stay_247 = not settings.stay_247

        if hasattr(self._bot, "storage"):
            try:
                await getattr(self._bot, "storage").upsert_guild_settings(interaction.guild_id, settings)
            except Exception:
                logger.exception("Failed to persist stay_247 guild=%s", interaction.guild_id)

        await self._edit_message(interaction, player, notice=f"Chế độ 24/7: {'Bật' if settings.stay_247 else 'Tắt'}")

    @discord.ui.button(label="Làm mới", style=discord.ButtonStyle.secondary, custom_id="music:refresh", row=2)
    async def refresh(self, interaction: discord.Interaction, _: discord.ui.Button) -> None:
        if not interaction.guild_id:
            await interaction.response.send_message("Lệnh này chỉ dùng trong server.", ephemeral=True)
            return

        player = await _get_player(interaction)
        if not player:
            await interaction.response.send_message("Bot chưa ở trong voice channel.", ephemeral=True)
            return

        embed = build_controller_embed(self._bot, player)
        await interaction.response.edit_message(embed=embed)

    @discord.ui.button(label="Bộ lọc 1/2", style=discord.ButtonStyle.secondary, custom_id="music:filter_page", row=3)
    async def filter_page_btn(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        # Chuyển trang filter menu.
        if not interaction.guild_id:
            await interaction.response.send_message("Lệnh này chỉ dùng trong server.", ephemeral=True)
            return

        player = await _get_player(interaction)
        if not player:
            await interaction.response.send_message("Bot chưa ở trong voice channel.", ephemeral=True)
            return

        # Chuyển sang trang tiếp theo (vòng tròn)
        next_page = (self._filter_page + 1) % self._total_filter_pages
        
        # Tạo View mới với trang filter mới
        new_view = PlayerControlView(self._bot, filter_page=next_page)
        
        embed = build_controller_embed(self._bot, player)
        await interaction.response.edit_message(embed=embed, view=new_view)
