"""Единый визуальный стиль POLARI для всех Embed."""
from __future__ import annotations

from datetime import datetime, timezone

import discord

from config import settings


class Colors:
    PRIMARY = 0x0F2A5C      # тёмно-синий
    SECONDARY = 0x2E86DE    # голубой
    ACCENT = 0x7FDBFF       # светло-голубой
    DARK = 0x0B1220         # фон / нейтральный
    SUCCESS = 0x3DD6C6      # бирюзовый — успех в палитре POLARI
    WARNING = 0xF5B841
    DANGER = 0xE5484D


DIVIDER = "▬▬▬▬▬▬▬▬▬▬▬▬▬▬▬▬▬▬▬▬"
THIN = "┈┈┈┈┈┈┈┈┈┈┈┈┈┈┈┈┈┈┈┈"
BANNER_NAME = "polari_banner.png"
BANNER_URL = f"attachment://{BANNER_NAME}"


def title(text: str, emoji: str = "❄️") -> str:
    return f"{emoji}  {settings.brand}  ┃  {text.upper()}"


def base(heading: str, description: str = "", *, color: int = Colors.SECONDARY, emoji: str = "❄️",
         footer: str | None = None, timestamp: bool = True) -> discord.Embed:
    embed = discord.Embed(title=title(heading, emoji), description=description, color=color)
    embed.set_footer(text=footer or f"{settings.brand} • сервер с характером")
    if timestamp:
        embed.timestamp = datetime.now(timezone.utc)
    return embed


def success(heading: str, description: str = "", **kw) -> discord.Embed:
    return base(heading, description, color=Colors.SUCCESS, emoji="✦", **kw)


def error(description: str, heading: str = "Не получилось") -> discord.Embed:
    return base(heading, f"> {description}", color=Colors.DANGER, emoji="✖", timestamp=False)


def warning(heading: str, description: str = "") -> discord.Embed:
    return base(heading, description, color=Colors.WARNING, emoji="⚠️")


def log_embed(heading: str, *, color: int = Colors.PRIMARY, emoji: str = "📜",
              fields: list[tuple[str, str]] | None = None, inline: bool = True) -> discord.Embed:
    embed = base(heading, "", color=color, emoji=emoji, footer=f"{settings.brand} • журнал событий")
    for name, value in fields or []:
        embed.add_field(name=name, value=(value or "—")[:1024], inline=inline)
    return embed


def banner_file() -> discord.File | None:
    """Файл баннера для вложения в Embed (None, если баннер не сгенерирован)."""
    if settings.banner_path.exists():
        return discord.File(settings.banner_path, filename=BANNER_NAME)
    return None


def with_banner(embed: discord.Embed) -> tuple[discord.Embed, discord.File | None]:
    file = banner_file()
    if file:
        embed.set_image(url=BANNER_URL)
    return embed, file


def ts(dt: datetime | str | None, style: str = "f") -> str:
    """Discord-таймстамп, отображаемый в часовом поясе читателя."""
    if dt is None:
        return "—"
    if isinstance(dt, str):
        dt = datetime.fromisoformat(dt)
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return f"<t:{int(dt.timestamp())}:{style}>"
