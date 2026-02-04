from __future__ import annotations

from discord import app_commands


class ChannelRestrictedError(app_commands.CheckFailure):
    pass
