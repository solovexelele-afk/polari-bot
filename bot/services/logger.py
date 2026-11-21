"""Отправка событий в каналы логов POLARI."""
from __future__ import annotations

import logging
from typing import TYPE_CHECKING

import discord

from bot.data.blueprint import LOG_ROUTES

if TYPE_CHECKING:
    from bot.core import PolariBot

log = logging.getLogger("polari.logs")


class LogService:
    def __init__(self, bot: "PolariBot"):
        self.bot = bot

    async def send(self, guild: discord.Guild, kind: str, embed: discord.Embed,
                   file: discord.File | None = None) -> None:
        """Отправляет Embed в канал, назначенный для типа события. Ошибки только логируются."""
        key = LOG_ROUTES.get(kind, "log_server")
        channel = await self.bot.registry.text(guild, key)
        if channel is None:
            log.debug("Канал логов %s не настроен (%s)", key, kind)
            return
        try:
            if file:
                await channel.send(embed=embed, file=file, allowed_mentions=discord.AllowedMentions.none())
            else:
                await channel.send(embed=embed, allowed_mentions=discord.AllowedMentions.none())
        except discord.HTTPException as exc:
            log.warning("Не удалось отправить лог %s в #%s: %s", kind, channel, exc)
