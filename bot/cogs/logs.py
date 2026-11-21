"""Журнал событий сервера: вход/выход, роли, сообщения, каналы, роли сервера."""
from __future__ import annotations

from typing import TYPE_CHECKING

import discord
from discord.ext import commands

from bot.embeds import style
from bot.utils.helpers import redact

if TYPE_CHECKING:
    from bot.core import PolariBot


class LogsCog(commands.Cog):
    def __init__(self, bot: "PolariBot"):
        self.bot = bot

    def _send(self, guild: discord.Guild, kind: str, embed: discord.Embed):
        return self.bot.logs.send(guild, kind, embed)

    async def _is_log_channel(self, channel_id: int, guild_id: int) -> bool:
        settings = await self.bot.registry.load(guild_id)
        return any(k.startswith("channel.log_") and v == str(channel_id) for k, v in settings.items())

    # ------------------------------------------------------------------ участники
    @commands.Cog.listener()
    async def on_member_join(self, member: discord.Member) -> None:
        await self.bot.db.bump_stat(member.guild.id, "joins")
        age = discord.utils.utcnow() - member.created_at
        embed = style.log_embed("Участник зашёл", color=style.Colors.SUCCESS, emoji="📥", fields=[
            ("Участник", f"{member.mention}\n`{member.id}`"),
            ("Аккаунт создан", style.ts(member.created_at, "R") + (" ⚠️ новый" if age.days < 7 else "")),
            ("Участников", str(member.guild.member_count)),
        ])
        embed.set_thumbnail(url=member.display_avatar.url)
        await self._send(member.guild, "join", embed)

    @commands.Cog.listener()
    async def on_member_remove(self, member: discord.Member) -> None:
        await self.bot.db.bump_stat(member.guild.id, "leaves")
        await self.bot.db.execute("UPDATE users SET left_at=datetime('now') WHERE guild_id=? AND user_id=?",
                                  (member.guild.id, member.id))
        roles = ", ".join(r.mention for r in member.roles[1:][-10:]) or "—"
        embed = style.log_embed("Участник вышел", color=style.Colors.DARK, emoji="📤", fields=[
            ("Участник", f"{member} \n`{member.id}`"),
            ("Был на сервере с", style.ts(member.joined_at, "R") if member.joined_at else "—"),
            ("Роли", roles),
        ])
        embed.set_thumbnail(url=member.display_avatar.url)
        await self._send(member.guild, "leave", embed)

    @commands.Cog.listener()
    async def on_member_update(self, before: discord.Member, after: discord.Member) -> None:
        added = [r for r in after.roles if r not in before.roles]
        removed = [r for r in before.roles if r not in after.roles]
        if added or removed:
            actor = await self._audit_actor(after.guild, discord.AuditLogAction.member_role_update, after.id)
            fields = [("Участник", after.mention)]
            if added:
                fields.append(("Выданы", ", ".join(r.mention for r in added)))
            if removed:
                fields.append(("Сняты", ", ".join(r.mention for r in removed)))
            fields.append(("Кем", actor or "—"))
            await self._send(after.guild, "role", style.log_embed(
                "Изменение ролей", color=style.Colors.SECONDARY, emoji="🎭", fields=fields))
        if before.nick != after.nick:
            await self._send(after.guild, "server", style.log_embed("Смена ника", emoji="🏷️", fields=[
                ("Участник", after.mention), ("Было", before.nick or "—"), ("Стало", after.nick or "—")]))

    async def _audit_actor(self, guild: discord.Guild, action: discord.AuditLogAction, target_id: int) -> str | None:
        if not guild.me.guild_permissions.view_audit_log:
            return None
        try:
            async for entry in guild.audit_logs(limit=5, action=action):
                if entry.target and entry.target.id == target_id and \
                        (discord.utils.utcnow() - entry.created_at).total_seconds() < 15:
                    return entry.user.mention if entry.user else None
        except discord.HTTPException:
            return None
        return None

    # ------------------------------------------------------------------ сообщения
    @commands.Cog.listener()
    async def on_message_delete(self, message: discord.Message) -> None:
        if not message.guild or message.author.bot or await self._is_log_channel(message.channel.id, message.guild.id):
            return
        content = redact(message.content)[:1000] or "*без текста*"
        files = "\n".join(a.filename for a in message.attachments)
        await self._send(message.guild, "message", style.log_embed("Сообщение удалено", color=style.Colors.DANGER,
                                                                   emoji="🗑️", inline=False, fields=[
            ("Автор / канал", f"{message.author.mention} · {message.channel.mention}"),  # type: ignore[union-attr]
            ("Текст", content), *([("Вложения", files)] if files else []),
        ]))

    @commands.Cog.listener()
    async def on_bulk_message_delete(self, messages: list[discord.Message]) -> None:
        if not messages or not messages[0].guild:
            return
        await self._send(messages[0].guild, "message", style.log_embed("Массовое удаление", emoji="🧹", fields=[
            ("Канал", messages[0].channel.mention), ("Сообщений", str(len(messages)))]))  # type: ignore[union-attr]

    @commands.Cog.listener()
    async def on_message_edit(self, before: discord.Message, after: discord.Message) -> None:
        if not after.guild or after.author.bot or before.content == after.content:
            return
        await self._send(after.guild, "message", style.log_embed("Сообщение изменено", color=style.Colors.WARNING,
                                                                 emoji="✏️", inline=False, fields=[
            ("Автор / канал", f"{after.author.mention} · {after.channel.mention} · [перейти]({after.jump_url})"),  # type: ignore[union-attr]
            ("Было", redact(before.content)[:1000] or "—"), ("Стало", redact(after.content)[:1000] or "—"),
        ]))

    # ------------------------------------------------------------------ каналы и роли сервера
    @commands.Cog.listener()
    async def on_guild_channel_create(self, channel: discord.abc.GuildChannel) -> None:
        if channel.category and str(channel.category.id) == await self.bot.registry.get(channel.guild.id, "channel.cat_tickets"):
            return  # тикеты логируются отдельно
        await self._send(channel.guild, "server", style.log_embed("Канал создан", color=style.Colors.SUCCESS, emoji="➕",
                                                                  fields=[("Канал", f"{channel.mention} ({channel.name})")]))

    @commands.Cog.listener()
    async def on_guild_channel_delete(self, channel: discord.abc.GuildChannel) -> None:
        if channel.category and str(channel.category.id) == await self.bot.registry.get(channel.guild.id, "channel.cat_tickets"):
            return
        await self._send(channel.guild, "server", style.log_embed("Канал удалён", color=style.Colors.DANGER, emoji="➖",
                                                                  fields=[("Канал", channel.name)]))

    @commands.Cog.listener()
    async def on_guild_channel_update(self, before: discord.abc.GuildChannel, after: discord.abc.GuildChannel) -> None:
        changes = []
        if before.name != after.name:
            changes.append(f"Название: `{before.name}` → `{after.name}`")
        if getattr(before, "topic", None) != getattr(after, "topic", None):
            changes.append("Изменено описание")
        if before.overwrites != after.overwrites:
            changes.append("Изменены права доступа")
        if getattr(before, "slowmode_delay", None) != getattr(after, "slowmode_delay", None):
            changes.append(f"Медленный режим: {getattr(after, 'slowmode_delay', 0)} сек.")
        if changes:
            await self._send(after.guild, "server", style.log_embed("Канал изменён", emoji="⚙️", inline=False, fields=[
                ("Канал", after.mention), ("Изменения", "\n".join(changes))]))

    @commands.Cog.listener()
    async def on_guild_role_create(self, role: discord.Role) -> None:
        await self._send(role.guild, "server", style.log_embed("Роль создана", color=style.Colors.SUCCESS, emoji="🎭",
                                                               fields=[("Роль", role.mention)]))

    @commands.Cog.listener()
    async def on_guild_role_delete(self, role: discord.Role) -> None:
        await self._send(role.guild, "server", style.log_embed("Роль удалена", color=style.Colors.DANGER, emoji="🎭",
                                                               fields=[("Роль", role.name)]))

    @commands.Cog.listener()
    async def on_guild_role_update(self, before: discord.Role, after: discord.Role) -> None:
        changes = []
        if before.name != after.name:
            changes.append(f"Название: `{before.name}` → `{after.name}`")
        if before.colour != after.colour:
            changes.append(f"Цвет: {before.colour} → {after.colour}")
        if before.permissions != after.permissions:
            gained = [p for p, v in after.permissions if v and not getattr(before.permissions, p)]
            lost = [p for p, v in before.permissions if v and not getattr(after.permissions, p)]
            changes.append("Права: " + " ".join([f"+{p}" for p in gained] + [f"-{p}" for p in lost]))
        if changes:
            await self._send(after.guild, "server", style.log_embed("Роль изменена", emoji="🎭", inline=False, fields=[
                ("Роль", after.mention), ("Изменения", "\n".join(changes)[:1000])]))


async def setup(bot: "PolariBot") -> None:
    await bot.add_cog(LogsCog(bot))
