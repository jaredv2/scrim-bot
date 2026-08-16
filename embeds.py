from __future__ import annotations

import discord

COLOR_PRIMARY = 0x3498DB
COLOR_SUCCESS = 0x2ECC71
COLOR_ERROR = 0xE74C3C
COLOR_WARNING = 0xF39C12


def base(title: str, color: int = COLOR_PRIMARY) -> discord.Embed:
    return discord.Embed(title=title, color=color)


def success(message: str) -> discord.Embed:
    return discord.Embed(title="Success", description=message, color=COLOR_SUCCESS)


def error(message: str) -> discord.Embed:
    return discord.Embed(title="Error", description=message, color=COLOR_ERROR)


def warning(message: str) -> discord.Embed:
    return discord.Embed(title="Warning", description=message, color=COLOR_WARNING)
