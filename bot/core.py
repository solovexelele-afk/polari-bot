"""Класс бота POLARI: загрузка модулей, БД, persistent views, глобальный обработчик ошибок."""
from __future__ import annotations

import logging

import discord
from discord import app_commands
from discord.ext import commands

from bot.database.db import Database
from bot.embeds import style
from bot.services.hosting import HostingService
from bot.services.logger import LogService
from bot.services.registry import Registry
from bot.services.tickets import TicketService
from bot.utils.checks import PolariCheckFailure
from config import settings

log = logging.getLogger("polari")

COGS = [
    "bot.cogs.setup",
    "bot.cogs.interview",
    "bot.cogs.tickets",
    "bot.cogs.shop",
    "bot.cogs.moderation",
    "bot.cogs.logs",
    "bot.cogs.info",
    "bot.cogs.community",
    "bot.cogs.admin",
]


class PolariTree(app_commands.CommandTree):
    async def on_error(self, interaction: discord.Interaction, error: app_commands.AppCommandError) -> None:
        if isinstance(error, PolariCheckFailure):
            text = str(error)
        elif isinstance(error, app_commands.CommandOnCooldown):
            text = f"Подождите ещё {error.retry_after:.0f} сек. перед повторным использованием."
        elif isinstance(error, app_commands.MissingPermissions):
            text = "У вас недостаточно прав для этой команды."
        elif isinstance(error, app_commands.BotMissingPermissions):
            text = "У бота недостаточно прав: " + ", ".join(error.missing_permissions)
        elif isinstance(error, app_commands.NoPrivateMessage):
            text = "Команда работает только на сервере."
        else:
            original = getattr(error, "original", error)
            if isinstance(original, discord.Forbidden):
                text = "Discord запретил действие: у бота не хватает прав или его роль ниже нужной."
            else:
                log.exception("Ошибка команды /%s", interaction.command.qualified_name if interaction.command else "?",
                              exc_info=original)
                text = "Произошла внутренняя ошибка. Она записана в журнал, персонал разберётся."
        embed = style.error(text)
        try:
            if interaction.response.is_done():
                await interaction.followup.send(embed=embed, ephemeral=True)
            else:
                await interaction.response.send_message(embed=embed, ephemeral=True)
        except discord.HTTPException:
            pass


class PolariBot(commands.Bot):
    def __init__(self) -> None:
        intents = discord.Intents.default()
        intents.members = True            # вход/выход, роли — включите Server Members Intent в портале
        intents.message_content = True    # автомодерация и логи изменений — включите Message Content Intent
        intents.voice_states = True
        super().__init__(command_prefix=commands.when_mentioned, intents=intents, tree_cls=PolariTree,
                         allowed_mentions=discord.AllowedMentions(everyone=False, roles=False, users=True),
                         activity=discord.Activity(type=discord.ActivityType.watching, name="за POLARI ❄️"))
        self.db = Database(settings.db_path)
        self.registry = Registry(self.db)
        self.logs = LogService(self)
        self.tickets = TicketService(self)
        self.hosting = HostingService(self)

    async def setup_hook(self) -> None:
        await self.hosting.restore_if_missing(settings.db_path)
        await self.db.connect()
        await self.hosting.start_background()
        for ext in COGS:
            await self.load_extension(ext)
            log.info("Модуль загружен: %s", ext)
        if settings.guild_id:
            guild = discord.Object(id=settings.guild_id)
            self.tree.copy_global_to(guild=guild)
            synced = await self.tree.sync(guild=guild)
            log.info("Синхронизировано %s команд для сервера %s", len(synced), settings.guild_id)
        else:
            synced = await self.tree.sync()
            log.info("Глобально синхронизировано %s команд (появятся в течение часа)", len(synced))

    async def on_ready(self) -> None:
        log.info("POLARI запущен как %s (%s). Серверов: %s", self.user, self.user.id if self.user else "?",
                 len(self.guilds))

    async def close(self) -> None:
        try:
            await self.hosting.stop()  # финальный бэкап, пока соединение с Discord живо
        except Exception:
            log.exception("Ошибка при остановке сервисов хостинга")
        await super().close()
        await self.db.close()
