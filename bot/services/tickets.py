"""Сервис тикетов: создание приватного канала, транскрипт, закрытие. Используется тикетами и магазином."""
from __future__ import annotations

import asyncio
import io
import logging
import time
from typing import TYPE_CHECKING

import discord

from bot.data.blueprint import keys_with_level
from bot.data.content import TICKET_BY_ID
from bot.database.db import now_iso
from bot.embeds import style
from bot.utils.helpers import redact
from config import settings

if TYPE_CHECKING:
    from bot.core import PolariBot

log = logging.getLogger("polari.tickets")
CAPABILITY_ROLE_KEYS = {
    "tickets": ["support", *keys_with_level(40)],
    "shop": ["shop_staff", *keys_with_level(80)],
    "admin": keys_with_level(70),
}


class TicketError(Exception):
    """Понятная пользователю причина, по которой тикет не создан."""


class TicketService:
    def __init__(self, bot: "PolariBot"):
        self.bot = bot
        self._cooldowns: dict[tuple[int, int], float] = {}
        self._locks: dict[int, asyncio.Lock] = {}

    async def staff_roles(self, guild: discord.Guild, capability: str) -> list[discord.Role]:
        return await self.bot.registry.roles(guild, CAPABILITY_ROLE_KEYS.get(capability, CAPABILITY_ROLE_KEYS["tickets"]))

    async def get_by_channel(self, channel_id: int):
        return await self.bot.db.fetchone("SELECT * FROM tickets WHERE channel_id=?", (channel_id,))

    async def can_manage(self, member: discord.Member, ticket) -> bool:
        ttype = TICKET_BY_ID.get(ticket["type"])
        cap = ttype.capability if ttype else "tickets"
        reg = self.bot.registry
        return await reg.can(member, cap) or (cap != "admin" and await reg.can(member, "tickets"))

    async def create(self, member: discord.Member, type_id: str, subject: str,
                     intro: discord.Embed | None = None, view: discord.ui.View | None = None
                     ) -> tuple[discord.TextChannel, int]:
        guild = member.guild
        ttype = TICKET_BY_ID[type_id]
        lock = self._locks.setdefault(member.id, asyncio.Lock())
        if lock.locked():
            raise TicketError("Тикет уже создаётся, подождите секунду.")
        async with lock:
            key = (guild.id, member.id)
            left = settings.ticket_cooldown - (time.monotonic() - self._cooldowns.get(key, 0))
            if left > 0:
                raise TicketError(f"Новый тикет можно открыть через {left:.0f} сек.")
            open_count = await self.bot.db.fetchval(
                "SELECT COUNT(*) FROM tickets WHERE guild_id=? AND user_id=? AND status='open'", (guild.id, member.id), 0)
            if open_count >= settings.max_open_tickets:
                raise TicketError(f"У вас уже {open_count} открытых тикета. Закройте один из них, чтобы создать новый.")
            category = await self.bot.registry.channel(guild, "cat_tickets")
            if not isinstance(category, discord.CategoryChannel):
                raise TicketError("Категория тикетов не настроена. Сообщите администрации.")

            number = await self.bot.db.fetchval("SELECT MAX(number) FROM tickets WHERE guild_id=?", (guild.id,), 0) + 1
            staff = await self.staff_roles(guild, ttype.capability)
            overwrites: dict = {
                guild.default_role: discord.PermissionOverwrite(view_channel=False),
                member: discord.PermissionOverwrite(view_channel=True, send_messages=True, read_message_history=True,
                                                    attach_files=True, embed_links=True),
                guild.me: discord.PermissionOverwrite(view_channel=True, send_messages=True, manage_channels=True,
                                                      manage_messages=True, read_message_history=True, embed_links=True,
                                                      attach_files=True, manage_permissions=True),
            }
            for role in staff:
                overwrites[role] = discord.PermissionOverwrite(view_channel=True, send_messages=True,
                                                               read_message_history=True, attach_files=True,
                                                               manage_messages=True)
            channel = await guild.create_text_channel(
                f"{ttype.emoji}・{ttype.id}-{number:04d}", category=category, overwrites=overwrites,
                topic=f"Тикет #{number:04d} · {ttype.label} · {member} ({member.id})",
                reason=f"POLARI: тикет #{number} от {member}")
            ticket_id = await self.bot.db.execute(
                "INSERT INTO tickets (guild_id,number,channel_id,user_id,type,subject,created_at) VALUES (?,?,?,?,?,?,?)",
                (guild.id, number, channel.id, member.id, type_id, subject[:300], now_iso()))
            self._cooldowns[key] = time.monotonic()

        from bot.cogs.tickets import TicketControlView  # локальный импорт: избегаем цикла

        embed = intro or style.base(f"Тикет #{number:04d}", (
            f"{member.mention}, спасибо за обращение! Команда POLARI скоро ответит.\n{style.THIN}\n"
            f"**Тип:** {ttype.emoji} {ttype.label}\n**Тема:** {subject}"), emoji="🎫")
        mentions = " ".join(r.mention for r in staff[:3])
        await channel.send(content=f"{member.mention} {mentions}", embed=embed, view=view or TicketControlView(),
                           allowed_mentions=discord.AllowedMentions(users=True, roles=True))
        await self.bot.logs.send(guild, "ticket", style.log_embed("Тикет создан", emoji="🎫", fields=[
            ("Номер", f"#{number:04d}"), ("Тип", ttype.label), ("Автор", member.mention),
            ("Канал", channel.mention), ("Тема", redact(subject)[:200]),
        ]))
        return channel, ticket_id

    async def transcript(self, channel: discord.TextChannel, ticket) -> discord.File:
        lines = [f"POLARI — транскрипт тикета #{ticket['number']:04d} ({ticket['type']})",
                 f"Канал: #{channel.name} | Автор: {ticket['user_id']} | Создан: {ticket['created_at']}", "=" * 60]
        async for msg in channel.history(limit=None, oldest_first=True):
            content = redact(msg.content)
            for e in msg.embeds:
                content += f"\n  [embed] {e.title or ''} {redact(e.description or '')[:500]}"
            for a in msg.attachments:
                content += f"\n  [файл] {a.filename} {a.url}"
            lines.append(f"[{msg.created_at:%Y-%m-%d %H:%M}] {msg.author} ({msg.author.id}): {content}")
        data = "\n".join(lines).encode("utf-8")
        folder = settings.db_path.parent / "transcripts"
        folder.mkdir(parents=True, exist_ok=True)
        name = f"ticket-{channel.guild.id}-{ticket['number']:04d}.txt"
        (folder / name).write_bytes(data)
        return discord.File(io.BytesIO(data), filename=name)

    async def close(self, channel: discord.TextChannel, closer: discord.Member, reason: str) -> None:
        ticket = await self.get_by_channel(channel.id)
        if not ticket or ticket["status"] != "open":
            return
        await self.bot.db.execute("UPDATE tickets SET status='closed', closed_by=?, closed_at=? WHERE id=?",
                                  (closer.id, now_iso(), ticket["id"]))
        # Незавершённый заказ в закрываемом тикете отменяется
        order = await self.bot.db.fetchone("SELECT * FROM orders WHERE ticket_id=? AND status='pending'", (ticket["id"],))
        if order:
            await self.bot.db.execute("UPDATE orders SET status='cancelled', handled_by=?, updated_at=? WHERE id=?",
                                      (closer.id, now_iso(), order["id"]))
            await self.bot.db.execute("INSERT INTO payments (order_id,action,staff_id,note,created_at) VALUES (?,?,?,?,?)",
                                      (order["id"], "cancelled_on_close", closer.id, reason, now_iso()))
        transcript = await self.transcript(channel, ticket)
        guild = channel.guild
        await self.bot.logs.send(guild, "ticket", style.log_embed("Тикет закрыт", color=style.Colors.DARK, emoji="🔒", fields=[
            ("Номер", f"#{ticket['number']:04d}"), ("Автор", f"<@{ticket['user_id']}>"),
            ("Закрыл", closer.mention), ("Причина", reason),
            ("Заказ", f"#{order['id']} отменён" if order else "—"),
        ]), file=transcript)
        author = guild.get_member(ticket["user_id"])
        if author:
            try:
                await author.send(embed=style.base("Тикет закрыт", (
                    f"Ваш тикет **#{ticket['number']:04d}** на **{guild.name}** закрыт.\n**Причина:** {reason}")),
                    file=await self.transcript(channel, ticket))
            except discord.HTTPException:
                pass
        await asyncio.sleep(5)
        try:
            await channel.delete(reason=f"POLARI: тикет закрыт ({closer})")
        except discord.HTTPException as exc:
            log.warning("Не удалось удалить канал тикета %s: %s", channel.id, exc)
