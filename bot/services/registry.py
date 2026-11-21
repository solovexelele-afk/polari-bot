"""Доступ к сохранённым ID ролей и каналов POLARI (ключ blueprint -> объект Discord)."""
from __future__ import annotations

from typing import TYPE_CHECKING

import discord

from bot.data.blueprint import ROLE_BY_KEY

if TYPE_CHECKING:
    from bot.database.db import Database


class Registry:
    """Кэширует настройки сервера в памяти; источник истины — таблица guild_settings."""

    def __init__(self, db: "Database"):
        self.db = db
        self._cache: dict[int, dict[str, str]] = {}

    async def load(self, guild_id: int) -> dict[str, str]:
        if guild_id not in self._cache:
            self._cache[guild_id] = await self.db.all_settings(guild_id)
        return self._cache[guild_id]

    async def set(self, guild_id: int, key: str, value: int | str) -> None:
        await self.db.set_setting(guild_id, key, value)
        (await self.load(guild_id))[key] = str(value)

    async def get(self, guild_id: int, key: str) -> str | None:
        return (await self.load(guild_id)).get(key)

    async def role(self, guild: discord.Guild, key: str) -> discord.Role | None:
        value = await self.get(guild.id, f"role.{key}")
        return guild.get_role(int(value)) if value else None

    async def roles(self, guild: discord.Guild, keys: list[str]) -> list[discord.Role]:
        result = []
        for key in keys:
            role = await self.role(guild, key)
            if role:
                result.append(role)
        return result

    async def channel(self, guild: discord.Guild, key: str) -> discord.abc.GuildChannel | None:
        value = await self.get(guild.id, f"channel.{key}")
        return guild.get_channel(int(value)) if value else None

    async def text(self, guild: discord.Guild, key: str) -> discord.TextChannel | None:
        ch = await self.channel(guild, key)
        return ch if isinstance(ch, discord.TextChannel) else None

    async def member_level(self, member: discord.Member) -> int:
        """Уровень полномочий: владелец и Administrator = 100, иначе максимум по ролям персонала."""
        if member.guild.owner_id == member.id or member.guild_permissions.administrator:
            return 100
        settings = await self.load(member.guild.id)
        role_ids = {r.id for r in member.roles}
        best = 0
        for key, spec in ROLE_BY_KEY.items():
            rid = settings.get(f"role.{key}")
            if spec.level and rid and int(rid) in role_ids:
                best = max(best, spec.level)
        return best

    async def has_role_key(self, member: discord.Member, key: str) -> bool:
        rid = await self.get(member.guild.id, f"role.{key}")
        return bool(rid) and any(r.id == int(rid) for r in member.roles)

    async def can(self, member: discord.Member, capability: str) -> bool:
        """Проверка полномочий по функциональным группам."""
        level = await self.member_level(member)
        if capability == "interview":
            return level >= 70 or await self.has_role_key(member, "checker")
        if capability == "tickets":
            return level >= 40 or await self.has_role_key(member, "support")
        if capability == "shop":
            return level >= 80 or await self.has_role_key(member, "shop_staff")
        if capability in ("admin", "logs"):
            return level >= 70
        if capability == "config":
            return level >= 90
        if capability.startswith("level:"):
            return level >= int(capability.split(":", 1)[1])
        return False
