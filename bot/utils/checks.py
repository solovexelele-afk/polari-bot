"""Проверки прав для slash-команд и кнопок."""
from __future__ import annotations

import discord
from discord import app_commands

CAPABILITY_NAMES = {
    "interview": "проверяющих и администрации",
    "tickets": "поддержки и модерации",
    "shop": "сотрудников магазина",
    "admin": "администрации",
    "logs": "администрации",
    "config": "основателя и заместителя",
}


class PolariCheckFailure(app_commands.CheckFailure):
    """Ошибка проверки с готовым текстом для пользователя."""


def require(capability: str):
    """Декоратор: команда доступна только обладателям полномочия."""

    async def predicate(interaction: discord.Interaction) -> bool:
        if not isinstance(interaction.user, discord.Member):
            raise PolariCheckFailure("Команда работает только на сервере.")
        if await interaction.client.registry.can(interaction.user, capability):  # type: ignore[attr-defined]
            return True
        who = CAPABILITY_NAMES.get(capability, f"персонала уровня {capability.split(':')[-1]}+")
        raise PolariCheckFailure(f"Эта команда доступна только для {who}.")

    return app_commands.check(predicate)


async def ensure(interaction: discord.Interaction, capability: str) -> bool:
    """То же для кнопок: при отказе отвечает пользователю и возвращает False."""
    from bot.embeds import style

    member = interaction.user
    if isinstance(member, discord.Member) and await interaction.client.registry.can(member, capability):  # type: ignore[attr-defined]
        return True
    who = CAPABILITY_NAMES.get(capability, "персонала")
    msg = style.error(f"Это действие доступно только для {who}.", "Нет доступа")
    if interaction.response.is_done():
        await interaction.followup.send(embed=msg, ephemeral=True)
    else:
        await interaction.response.send_message(embed=msg, ephemeral=True)
    return False


def hierarchy_error(actor: discord.Member, target: discord.Member, me: discord.Member) -> str | None:
    """Проверка иерархии ролей перед модерацией. Возвращает текст ошибки или None."""
    guild = actor.guild
    if target.id == actor.id:
        return "Нельзя применить это действие к себе."
    if target.id == guild.owner_id:
        return "Нельзя модерировать владельца сервера."
    if target.bot and target.id == me.id:
        return "Я не могу модерировать сам себя."
    if actor.id != guild.owner_id and target.top_role >= actor.top_role:
        return "Роль участника выше или равна вашей."
    if target.top_role >= me.top_role:
        return "Роль участника выше или равна роли бота. Поднимите роль POLARI в настройках."
    return None
