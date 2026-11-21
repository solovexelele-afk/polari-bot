"""Поддержка хостинга (Render и аналоги).

* HTTP health-check на $PORT — Render Web Service требует открытый порт.
* Самопинг $RENDER_EXTERNAL_URL каждые 10 минут — бесплатный сервис не «засыпает».
* Бэкап SQLite в приватный Discord-канал ($BACKUP_CHANNEL_ID) и восстановление при старте:
  на бесплатном Render диск очищается при каждом деплое/перезапуске.
"""
from __future__ import annotations

import asyncio
import logging
import os
import sqlite3
import tempfile
from pathlib import Path
from typing import TYPE_CHECKING

import aiohttp
import discord
from aiohttp import web

if TYPE_CHECKING:
    from bot.core import PolariBot

log = logging.getLogger("polari.hosting")
BACKUP_PREFIX = "polari-backup"


class HostingService:
    def __init__(self, bot: "PolariBot"):
        self.bot = bot
        self.port = os.getenv("PORT")
        self.external_url = os.getenv("RENDER_EXTERNAL_URL") or os.getenv("KEEPALIVE_URL")
        backup = os.getenv("BACKUP_CHANNEL_ID", "")
        self.backup_channel_id = int(backup) if backup.isdigit() else 0
        self.backup_minutes = int(os.getenv("BACKUP_INTERVAL_MINUTES", "30") or 30)
        self._runner: web.AppRunner | None = None
        self._tasks: list[asyncio.Task] = []

    # ------------------------------------------------------------------ HTTP
    async def start_web(self) -> None:
        if not self.port:
            return
        app = web.Application()

        async def health(_: web.Request) -> web.Response:
            ready = self.bot.is_ready()
            return web.json_response({"status": "ok" if ready else "starting", "bot": str(self.bot.user or "")})

        app.router.add_get("/", health)
        app.router.add_get("/health", health)
        self._runner = web.AppRunner(app, access_log=None)
        await self._runner.setup()
        await web.TCPSite(self._runner, "0.0.0.0", int(self.port)).start()
        log.info("Health-check слушает порт %s", self.port)

    async def _keepalive(self) -> None:
        url = self.external_url.rstrip("/") + "/health"  # type: ignore[union-attr]
        async with aiohttp.ClientSession() as session:
            while True:
                await asyncio.sleep(600)
                try:
                    async with session.get(url, timeout=aiohttp.ClientTimeout(total=20)) as resp:
                        log.debug("Самопинг %s: %s", url, resp.status)
                except Exception as exc:  # сеть может моргнуть — просто пробуем позже
                    log.warning("Самопинг не удался: %s", exc)

    # ------------------------------------------------------------------ бэкап
    async def _backup_channel(self) -> discord.TextChannel | None:
        if not self.backup_channel_id:
            return None
        try:
            channel = self.bot.get_channel(self.backup_channel_id) or await self.bot.fetch_channel(self.backup_channel_id)
        except discord.HTTPException as exc:
            log.error("Канал бэкапа %s недоступен: %s", self.backup_channel_id, exc)
            return None
        return channel if isinstance(channel, discord.TextChannel) else None

    async def restore_if_missing(self, db_path: Path) -> None:
        """Вызывается до подключения БД: если файла нет — скачивает последний бэкап."""
        if db_path.exists() or not self.backup_channel_id:
            if not self.backup_channel_id:
                log.warning("BACKUP_CHANNEL_ID не задан: на бесплатном Render данные пропадут при перезапуске.")
            return
        channel = await self._backup_channel()
        if not channel:
            return
        async for message in channel.history(limit=50):
            if message.author.id != self.bot.user.id:  # type: ignore[union-attr]
                continue
            for att in message.attachments:
                if att.filename.startswith(BACKUP_PREFIX):
                    db_path.parent.mkdir(parents=True, exist_ok=True)
                    await att.save(db_path)
                    log.info("База восстановлена из бэкапа %s (%s)", att.filename, message.created_at)
                    return
        log.info("Бэкапов не найдено — создаётся новая база.")

    async def backup_now(self, reason: str = "по расписанию") -> bool:
        channel = await self._backup_channel()
        if not channel or not self.bot.db.conn:
            return False
        tmp = Path(tempfile.gettempdir()) / f"{BACKUP_PREFIX}-{os.getpid()}.db"

        def _copy() -> None:  # консистентная копия через SQLite backup API
            src = sqlite3.connect(self.bot.db.path)
            dst = sqlite3.connect(tmp)
            try:
                src.backup(dst)
            finally:
                dst.close()
                src.close()

        try:
            await asyncio.to_thread(_copy)
            stamp = discord.utils.utcnow().strftime("%Y%m%d-%H%M")
            await channel.send(content=f"🗄️ Бэкап POLARI · {reason}",
                               file=discord.File(tmp, filename=f"{BACKUP_PREFIX}-{stamp}.db"))
            return True
        except (discord.HTTPException, sqlite3.Error, OSError) as exc:
            log.error("Бэкап не выполнен: %s", exc)
            return False
        finally:
            tmp.unlink(missing_ok=True)

    async def _backup_loop(self) -> None:
        await self.bot.wait_until_ready()
        while True:
            await asyncio.sleep(self.backup_minutes * 60)
            await self.backup_now()

    # ------------------------------------------------------------------ жизненный цикл
    async def start_background(self) -> None:
        if self.external_url and self.port:
            self._tasks.append(asyncio.create_task(self._keepalive()))
        if self.backup_channel_id:
            self._tasks.append(asyncio.create_task(self._backup_loop()))

    async def stop(self) -> None:
        if self.backup_channel_id and self.bot.db.conn and not self.bot.is_closed():
            await self.backup_now("остановка бота")
        for task in self._tasks:
            task.cancel()
        if self._runner:
            await self._runner.cleanup()
