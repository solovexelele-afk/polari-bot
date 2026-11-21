"""Сообщество: автомодерация, статистика, репутация, предложения, голосования, мероприятия."""
from __future__ import annotations

import logging
import time
from collections import defaultdict, deque
from datetime import datetime, timedelta, timezone
from typing import TYPE_CHECKING

import discord
from discord import app_commands
from discord.ext import commands

from bot.database.db import now_iso
from bot.embeds import style
from bot.utils.checks import require
from config import settings

if TYPE_CHECKING:
    from bot.core import PolariBot

log = logging.getLogger("polari.community")
NUMBERS = ["1️⃣", "2️⃣", "3️⃣", "4️⃣", "5️⃣", "6️⃣", "7️⃣", "8️⃣", "9️⃣"]


class CommunityCog(commands.Cog):
    def __init__(self, bot: "PolariBot"):
        self.bot = bot
        self._recent: dict[int, deque[tuple[float, str]]] = defaultdict(lambda: deque(maxlen=20))
        self._pending_msgs: dict[tuple[int, int], int] = defaultdict(int)
        self._last_flush = time.monotonic()

    async def cog_load(self) -> None:
        self.bot.add_dynamic_items(EventJoinButton)

    # ------------------------------------------------------------------ автомодерация
    async def _punish(self, message: discord.Message, reason: str, timeout: bool) -> None:
        member = message.author
        assert isinstance(member, discord.Member)
        try:
            await message.delete()
        except discord.HTTPException:
            pass
        if timeout and member.top_role < member.guild.me.top_role:
            try:
                await member.timeout(timedelta(minutes=settings.automod_timeout_minutes), reason=f"POLARI автомод: {reason}")
            except discord.HTTPException:
                timeout = False
        try:
            await message.channel.send(embed=style.warning("Автомодерация", (
                f"{member.mention}, {reason.lower()}."
                + (f" Тайм-аут на **{settings.automod_timeout_minutes} мин.**" if timeout else ""))), delete_after=8)
        except discord.HTTPException:
            pass
        await self.bot.logs.send(member.guild, "automod", style.log_embed("Автомодерация", emoji="🤖", fields=[
            ("Участник", member.mention), ("Канал", message.channel.mention),  # type: ignore[union-attr]
            ("Нарушение", reason), ("Тайм-аут", "да" if timeout else "нет")]))

    async def _exempt(self, member: discord.Member) -> bool:
        return member.guild_permissions.manage_messages or await self.bot.registry.member_level(member) > 0

    @commands.Cog.listener()
    async def on_message(self, message: discord.Message) -> None:
        if not message.guild or message.author.bot or not isinstance(message.author, discord.Member):
            return
        await self._count_message(message)
        if await self._exempt(message.author):
            return
        content = message.content
        # Массовые упоминания
        mentions = len(message.raw_mentions) + len(message.raw_role_mentions) + (5 if message.mention_everyone else 0)
        if mentions >= settings.mass_mention_limit:
            await self._punish(message, "Массовые упоминания запрещены", True)
            return
        # Флуд и повтор одинаковых сообщений
        now = time.monotonic()
        history = self._recent[message.author.id]
        history.append((now, content.lower().strip()))
        window = [c for t, c in history if now - t <= settings.flood_seconds]
        if len(window) >= settings.flood_messages:
            history.clear()
            await self._punish(message, "Флуд запрещён", True)
            return
        if content and len(window) >= 3 and window[-1] == window[-2] == window[-3]:
            history.clear()
            await self._punish(message, "Спам одинаковыми сообщениями запрещён", True)
            return
        # Капс
        letters = [c for c in content if c.isalpha()]
        if len(letters) >= settings.caps_min_length and \
                sum(c.isupper() for c in letters) / len(letters) >= settings.caps_ratio:
            await self._punish(message, "Сообщения капсом запрещены", False)

    async def _count_message(self, message: discord.Message) -> None:
        """Счётчики сообщений копятся в памяти и пишутся в БД пачкой раз в минуту — без лишней нагрузки."""
        self._pending_msgs[(message.guild.id, message.author.id)] += 1  # type: ignore[union-attr]
        if time.monotonic() - self._last_flush < 60:
            return
        self._last_flush = time.monotonic()
        pending, self._pending_msgs = self._pending_msgs, defaultdict(int)
        totals: dict[int, int] = defaultdict(int)
        for (guild_id, user_id), count in pending.items():
            totals[guild_id] += count
            await self.bot.db.execute(
                "INSERT INTO users (guild_id,user_id,messages) VALUES (?,?,?) "
                "ON CONFLICT(guild_id,user_id) DO UPDATE SET messages=messages+excluded.messages",
                (guild_id, user_id, count))
        for guild_id, total in totals.items():
            await self.bot.db.bump_stat(guild_id, "messages", total)

    # ------------------------------------------------------------------ статистика
    @app_commands.command(name="stats", description="Статистика сервера POLARI за 7 дней")
    @app_commands.guild_only()
    async def stats(self, interaction: discord.Interaction) -> None:
        guild = interaction.guild
        assert guild
        db = self.bot.db
        rows = await db.fetchall("SELECT * FROM stats WHERE guild_id=? ORDER BY day DESC LIMIT 7", (guild.id,))
        joins = sum(r["joins"] for r in rows)
        leaves = sum(r["leaves"] for r in rows)
        msgs = sum(r["messages"] for r in rows)
        passed = await db.fetchval("SELECT COUNT(*) FROM interviews WHERE guild_id=? AND status='passed'", (guild.id,), 0)
        top = await db.fetchall("SELECT user_id, reputation FROM users WHERE guild_id=? AND reputation>0 "
                                "ORDER BY reputation DESC LIMIT 5", (guild.id,))
        in_voice = sum(len(c.members) for c in guild.voice_channels)
        embed = style.base("Статистика", f"Последние 7 дней на **{guild.name}**", emoji="📊")
        embed.add_field(name="Участников", value=str(guild.member_count))
        embed.add_field(name="В голосе сейчас", value=str(in_voice))
        embed.add_field(name="Прошли обзвон", value=str(passed))
        embed.add_field(name="Пришло / ушло", value=f"📥 {joins} · 📤 {leaves}")
        embed.add_field(name="Сообщений", value=str(msgs))
        embed.add_field(name="Топ репутации", inline=False,
                        value="\n".join(f"`{i + 1}.` <@{r['user_id']}> — ⭐ {r['reputation']}" for i, r in enumerate(top))
                        or "Пока пусто")
        await interaction.response.send_message(embed=embed)

    # ------------------------------------------------------------------ репутация
    rep = app_commands.Group(name="rep", description="Репутация участников", guild_only=True)

    @rep.command(name="give", description="Поблагодарить участника (+1 репутации)")
    async def rep_give(self, interaction: discord.Interaction, user: discord.Member,
                       reason: app_commands.Range[str, 0, 200] = "") -> None:
        if user.id == interaction.user.id or user.bot:
            await interaction.response.send_message(embed=style.error("Себе и ботам репутацию дать нельзя."), ephemeral=True)
            return
        guild_id = interaction.guild_id
        db = self.bot.db
        last = await db.fetchval("SELECT last_rep_given FROM users WHERE guild_id=? AND user_id=?",
                                 (guild_id, interaction.user.id))
        if last:
            next_time = datetime.fromisoformat(last) + timedelta(hours=settings.rep_cooldown_hours)
            if next_time > datetime.now(timezone.utc):
                await interaction.response.send_message(embed=style.error(
                    f"Следующую репутацию можно дать {style.ts(next_time, 'R')}."), ephemeral=True)
                return
        await db.execute("INSERT INTO users (guild_id,user_id,last_rep_given) VALUES (?,?,?) "
                         "ON CONFLICT(guild_id,user_id) DO UPDATE SET last_rep_given=excluded.last_rep_given",
                         (guild_id, interaction.user.id, now_iso()))
        await db.execute("INSERT INTO users (guild_id,user_id,reputation) VALUES (?,?,1) "
                         "ON CONFLICT(guild_id,user_id) DO UPDATE SET reputation=reputation+1", (guild_id, user.id))
        total = await db.fetchval("SELECT reputation FROM users WHERE guild_id=? AND user_id=?", (guild_id, user.id), 1)
        await interaction.response.send_message(embed=style.base("Репутация", (
            f"{interaction.user.mention} благодарит {user.mention}! ⭐ **{total}**"
            + (f"\n> {reason}" if reason else "")), color=style.Colors.ACCENT, emoji="⭐"))

    @rep.command(name="show", description="Посмотреть репутацию")
    async def rep_show(self, interaction: discord.Interaction, user: discord.Member | None = None) -> None:
        target = user or interaction.user
        total = await self.bot.db.fetchval("SELECT reputation FROM users WHERE guild_id=? AND user_id=?",
                                           (interaction.guild_id, target.id), 0)
        await interaction.response.send_message(embed=style.base("Репутация", f"{target.mention} — ⭐ **{total}**",
                                                                 emoji="⭐"), ephemeral=True)

    # ------------------------------------------------------------------ предложения
    @app_commands.command(name="suggest", description="Предложить идею для сервера")
    @app_commands.guild_only()
    @app_commands.checks.cooldown(1, 300)
    async def suggest(self, interaction: discord.Interaction, idea: app_commands.Range[str, 10, 1000]) -> None:
        channel = await self.bot.registry.text(interaction.guild, "suggestions")  # type: ignore[arg-type]
        if not channel:
            await interaction.response.send_message(embed=style.error("Канал предложений не настроен."), ephemeral=True)
            return
        sid = await self.bot.db.execute("INSERT INTO suggestions (guild_id,user_id,content,created_at) VALUES (?,?,?,?)",
                                        (interaction.guild_id, interaction.user.id, idea, now_iso()))
        embed = style.base(f"Предложение #{sid}", idea, color=style.Colors.SECONDARY, emoji="💡")
        embed.set_author(name=interaction.user.display_name, icon_url=interaction.user.display_avatar.url)
        msg = await channel.send(embed=embed)
        for emoji in ("👍", "👎"):
            await msg.add_reaction(emoji)
        try:
            await msg.create_thread(name=f"Обсуждение #{sid}", auto_archive_duration=4320)
        except discord.HTTPException:
            pass
        await self.bot.db.execute("UPDATE suggestions SET message_id=? WHERE id=?", (msg.id, sid))
        await interaction.response.send_message(embed=style.success("Предложение отправлено", msg.jump_url),
                                                ephemeral=True)

    @app_commands.command(name="suggestion_status", description="Принять или отклонить предложение")
    @app_commands.choices(status=[app_commands.Choice(name="Принято", value="accepted"),
                                  app_commands.Choice(name="Отклонено", value="rejected"),
                                  app_commands.Choice(name="На рассмотрении", value="review")])
    @app_commands.guild_only()
    @require("admin")
    async def suggestion_status(self, interaction: discord.Interaction, suggestion_id: int,
                                status: app_commands.Choice[str], comment: str = "") -> None:
        row = await self.bot.db.fetchone("SELECT * FROM suggestions WHERE id=? AND guild_id=?",
                                         (suggestion_id, interaction.guild_id))
        channel = await self.bot.registry.text(interaction.guild, "suggestions")  # type: ignore[arg-type]
        if not row or not channel or not row["message_id"]:
            await interaction.response.send_message(embed=style.error("Предложение не найдено."), ephemeral=True)
            return
        try:
            msg = await channel.fetch_message(row["message_id"])
        except discord.NotFound:
            await interaction.response.send_message(embed=style.error("Сообщение предложения удалено."), ephemeral=True)
            return
        color = {"accepted": style.Colors.SUCCESS, "rejected": style.Colors.DANGER, "review": style.Colors.WARNING}
        embed = msg.embeds[0]
        embed.color = color[status.value]
        embed.clear_fields()
        embed.add_field(name=f"Статус: {status.name}", value=f"{comment or '—'}\n— {interaction.user.mention}")
        await msg.edit(embed=embed)
        await self.bot.db.execute("UPDATE suggestions SET status=? WHERE id=?", (status.value, suggestion_id))
        await interaction.response.send_message(embed=style.success("Статус обновлён", msg.jump_url), ephemeral=True)

    # ------------------------------------------------------------------ голосования
    @app_commands.command(name="poll", description="Создать голосование")
    @app_commands.describe(question="Вопрос", options="Варианты через | (2-9). Пусто — да/нет")
    @app_commands.guild_only()
    @require("level:40")
    async def poll(self, interaction: discord.Interaction, question: app_commands.Range[str, 5, 250],
                   options: str = "") -> None:
        variants = [o.strip() for o in options.split("|") if o.strip()] if options else []
        if options and not 2 <= len(variants) <= 9:
            await interaction.response.send_message(embed=style.error("Нужно от 2 до 9 вариантов через |."), ephemeral=True)
            return
        channel = await self.bot.registry.text(interaction.guild, "polls") or interaction.channel  # type: ignore[arg-type]
        text = "\n".join(f"{NUMBERS[i]}  {v}" for i, v in enumerate(variants)) or "👍 — за  ·  👎 — против"
        embed = style.base("Голосование", f"**{question}**\n{style.THIN}\n{text}", color=style.Colors.ACCENT, emoji="📊")
        embed.set_author(name=interaction.user.display_name, icon_url=interaction.user.display_avatar.url)
        msg = await channel.send(embed=embed)  # type: ignore[union-attr]
        for emoji in (NUMBERS[:len(variants)] if variants else ["👍", "👎"]):
            await msg.add_reaction(emoji)
        await interaction.response.send_message(embed=style.success("Голосование создано", msg.jump_url), ephemeral=True)

    # ------------------------------------------------------------------ мероприятия
    @app_commands.command(name="event", description="Анонсировать мероприятие")
    @app_commands.describe(title="Название", when="Когда (например: 25.11 в 20:00 МСК)", description="Описание")
    @app_commands.guild_only()
    @require("level:40")
    async def event(self, interaction: discord.Interaction, title: app_commands.Range[str, 3, 100],
                    when: app_commands.Range[str, 3, 100], description: app_commands.Range[str, 0, 1500] = "") -> None:
        channel = await self.bot.registry.text(interaction.guild, "events")  # type: ignore[arg-type]
        if not channel:
            await interaction.response.send_message(embed=style.error("Канал мероприятий не настроен."), ephemeral=True)
            return
        await interaction.response.defer(ephemeral=True)
        event_id = await self.bot.db.execute(
            "INSERT INTO events (guild_id,channel_id,title,description,starts_at,created_by,created_at) VALUES (?,?,?,?,?,?,?)",
            (interaction.guild_id, channel.id, title, description, when, interaction.user.id, now_iso()))
        view = discord.ui.View(timeout=None)
        view.add_item(EventJoinButton(event_id))
        msg = await channel.send(embed=event_embed(event_id, title, description, when, interaction.user.id, 0), view=view)
        await self.bot.db.execute("UPDATE events SET message_id=? WHERE id=?", (msg.id, event_id))
        await interaction.followup.send(embed=style.success("Мероприятие опубликовано", msg.jump_url), ephemeral=True)


def event_embed(event_id: int, title: str, description: str, when: str, host_id: int, count: int) -> discord.Embed:
    embed = style.base(title, description or "Подробности — у организатора.", color=style.Colors.ACCENT, emoji="🎉")
    embed.add_field(name="🕒 Когда", value=when)
    embed.add_field(name="🎙️ Организатор", value=f"<@{host_id}>")
    embed.add_field(name="👥 Участвуют", value=str(count))
    embed.set_footer(text=f"POLARI • мероприятие #{event_id}")
    return embed


class EventJoinButton(discord.ui.DynamicItem[discord.ui.Button], template=r"polari:event:(?P<id>\d+)"):
    """Кнопка «Участвую» — повторное нажатие отменяет запись. Работает после перезапуска."""

    def __init__(self, event_id: int):
        super().__init__(discord.ui.Button(label="Участвую", emoji="🙋", style=discord.ButtonStyle.primary,
                                           custom_id=f"polari:event:{event_id}"))
        self.event_id = event_id

    @classmethod
    async def from_custom_id(cls, interaction, item, match):  # type: ignore[override]
        return cls(int(match["id"]))

    async def callback(self, interaction: discord.Interaction) -> None:  # type: ignore[override]
        db = interaction.client.db  # type: ignore[attr-defined]
        event = await db.fetchone("SELECT * FROM events WHERE id=?", (self.event_id,))
        if not event:
            await interaction.response.send_message(embed=style.error("Мероприятие не найдено."), ephemeral=True)
            return
        exists = await db.fetchval("SELECT COUNT(*) FROM event_participants WHERE event_id=? AND user_id=?",
                                   (self.event_id, interaction.user.id), 0)
        if exists:
            await db.execute("DELETE FROM event_participants WHERE event_id=? AND user_id=?",
                             (self.event_id, interaction.user.id))
        else:
            await db.execute("INSERT INTO event_participants VALUES (?,?)", (self.event_id, interaction.user.id))
        count = await db.fetchval("SELECT COUNT(*) FROM event_participants WHERE event_id=?", (self.event_id,), 0)
        await interaction.response.edit_message(embed=event_embed(
            self.event_id, event["title"], event["description"], event["starts_at"], event["created_by"], count))
        await interaction.followup.send(embed=style.base(
            "Мероприятие", "Запись отменена." if exists else "Вы записаны! Ждём вас ❄️", emoji="🎉"), ephemeral=True)


async def setup(bot: "PolariBot") -> None:
    await bot.add_cog(CommunityCog(bot))
