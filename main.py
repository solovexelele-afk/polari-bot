"""Точка входа POLARI."""
from __future__ import annotations

import asyncio
import logging
import logging.handlers
import sys

from config import settings


def setup_logging() -> None:
    settings.log_dir.mkdir(parents=True, exist_ok=True)
    fmt = logging.Formatter("%(asctime)s | %(levelname)-8s | %(name)s | %(message)s")
    root = logging.getLogger()
    root.setLevel(logging.INFO)
    console = logging.StreamHandler(sys.stdout)
    console.setFormatter(fmt)
    file = logging.handlers.RotatingFileHandler(
        settings.log_dir / "polari.log", maxBytes=5_000_000, backupCount=5, encoding="utf-8"
    )
    file.setFormatter(fmt)
    root.addHandler(console)
    root.addHandler(file)
    logging.getLogger("discord.http").setLevel(logging.WARNING)

    # Диагностика шифрования голоса (DAVE/MLS): только нужные debug-сообщения, без шума
    class _DaveOnly(logging.Filter):
        KEYS = ("MLS", "DAVE", "transition", "Transition", "op=2", "op=3")

        def filter(self, record: logging.LogRecord) -> bool:
            return record.levelno > logging.DEBUG or any(k in record.getMessage() for k in self.KEYS)

    for name in ("discord.gateway", "discord.voice_state"):
        logger = logging.getLogger(name)
        logger.setLevel(logging.DEBUG)
        logger.addFilter(_DaveOnly())


async def run() -> None:
    from bot.core import PolariBot

    if not settings.token:
        logging.critical("DISCORD_TOKEN не задан. Скопируйте .env.example в .env и заполните его.")
        return
    bot = PolariBot()
    if sys.platform != "win32":  # Render/Linux присылает SIGTERM при деплое — завершаемся корректно с бэкапом
        import signal

        loop = asyncio.get_running_loop()
        for sig in (signal.SIGTERM, signal.SIGINT):
            loop.add_signal_handler(sig, lambda: asyncio.create_task(bot.close()))
    async with bot:
        await bot.hosting.start_web()  # порт открывается сразу, до входа в Discord
        await bot.start(settings.token)


if __name__ == "__main__":
    setup_logging()
    try:
        asyncio.run(run())
    except KeyboardInterrupt:
        logging.info("POLARI остановлен.")
