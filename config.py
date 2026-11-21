"""Глобальная конфигурация POLARI. Секреты читаются только из .env."""
from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

from dotenv import load_dotenv

BASE_DIR = Path(__file__).resolve().parent
load_dotenv(BASE_DIR / ".env")


def _int(name: str, default: int) -> int:
    try:
        return int(os.getenv(name, default))
    except (TypeError, ValueError):
        return default


@dataclass(frozen=True)
class Settings:
    token: str = os.getenv("DISCORD_TOKEN", "")
    guild_id: int = _int("GUILD_ID", 0)               # сервер POLARI (для быстрой синхронизации команд)
    db_path: Path = BASE_DIR / os.getenv("DB_PATH", "data/polari.db")
    log_dir: Path = BASE_DIR / "data" / "logs"
    banner_path: Path = BASE_DIR / "assets" / "banners" / "polari_banner.png"
    brand: str = "POLARI"

    # Лимиты и антиспам
    max_open_tickets: int = _int("MAX_OPEN_TICKETS", 2)
    ticket_cooldown: int = _int("TICKET_COOLDOWN", 60)          # сек. между созданием тикетов
    waiting_rooms: int = _int("WAITING_ROOMS", 5)               # голосовых комнат ожидания
    interview_rooms: int = _int("INTERVIEW_ROOMS", 3)           # голосовых комнат собеседования
    warns_to_ban: int = _int("WARNS_TO_BAN", 3)                 # TENDERLE: 3 варна -> бан на месяц
    warn_ban_days: int = _int("WARN_BAN_DAYS", 30)
    mod_action_limit: int = _int("MOD_ACTION_LIMIT", 5)         # банов/киков за окно
    mod_action_window: int = _int("MOD_ACTION_WINDOW", 600)     # сек.
    rep_cooldown_hours: int = _int("REP_COOLDOWN_HOURS", 12)

    # Автомодерация
    flood_messages: int = _int("FLOOD_MESSAGES", 6)
    flood_seconds: int = _int("FLOOD_SECONDS", 8)
    caps_min_length: int = _int("CAPS_MIN_LENGTH", 12)
    caps_ratio: float = float(os.getenv("CAPS_RATIO", "0.75"))
    mass_mention_limit: int = _int("MASS_MENTION_LIMIT", 5)
    automod_timeout_minutes: int = _int("AUTOMOD_TIMEOUT_MINUTES", 5)


settings = Settings()
