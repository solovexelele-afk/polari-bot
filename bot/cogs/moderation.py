"""Модерация POLARI: наказания с причинами, историей, логами, проверкой иерархии и защитой от злоупотреблений."""
from __future__ import annotations

import json
import logging
import time
from collections import defaultdict, deque
from datetime import datetime, timedelta, timezone
from typing import TYPE_CHECKING

import discord
from discord import app_commands
from discord.ext import commands, tasks

from bot.database.db import now_iso
from bot.embeds import style
from bot.utils.checks import hierarchy_error, require
from bot.utils.helpers import human_delta, parse_duration
from config import settings

if TYPE_CHECKING:
    from bot.core import PolariBot

log = logging.getLogger("polari.mod")
MAX_TIMEOUT = timedelta(days=28)
ICONS = {"ban": "🔨", "tempban": "🔨", "unban": "🕊️", "kick": "👢", "mute": "🔇", "unmute": "🔊",
         "timeout": "⏱️", "untimeout": "⏱️", "warn": "⚠️", "unwarn": "♻️", "clear": "🧹"}


class ModerationCog(commands.Cog):
    def __init__(self, bot: "PolariBot"):
        self.bot = bot
        self._actions: dict[int, deque[float]] = defaultdict(deque)

    async def cog_load(self) -> None:
        self.unban_loop.start()

    async def cog_unload(self) -> None:
        self.unban_loop.cancel()

    # ------------------------------------------------------------------ общее
    def _rate_ok(self, moderator: discord.Member) -> bool:
        """Защита от массовых банов/киков: не больше N за окно (кроме владельца)."""
        if moderator.id == moderator.guild.owner_id:
            return True
        q = self._actions[moderator.id]
        now = time.monotonic()
        while q and now - q[0] > settings.mod_action_window:
            q.popleft()
        if len(q) >= settings.mod_action_limit:
            return False
        q.append(now)
        return True

    async def _abuse_alert(self, interaction: discord.Interaction, action: str) -> None:
        await self.bot.logs.send(interaction.guild, "mod", style.log_embed(  # type: ignore[arg-type]
            "Подозрение на злоупотребление", color=style.Colors.DANGER, emoji="🚨", fields=[
                ("Модератор", interaction.user.mention), ("Действие", action),
                ("Лимит", f"{settings.mod_action_limit} за {settings.mod_action_window // 60} мин."),
            ]))
        await interaction.followup.send(embed=style.error(
            "Слишком много наказаний за короткое время. Действие заблокировано, администрация уведомлена."),
            ephemeral=True)

    async def _target(self, interaction: discord.Interaction, user: discord.User | discord.Member
                      ) -> discord.Member | None:
        guild = interaction.guild
        assert guild
        member = guild.get_member(user.id)
        if member is None:
            await interaction.followup.send(embed=style.error("Пользователь не найден на сервере."), ephemeral=True)
            return None
        problem = hierarchy_error(interaction.user, member, guild.me)  # type: ignore[arg-type]
        if problem is None and await self.bot.registry.member_level(member) >= \
                await self.bot.registry.member_level(interaction.user) and interaction.user.id != guild.owner_id:  # type: ignore[arg-type]
            problem = "Нельзя наказать сотрудника вашего уровня или выше."
        if problem:
            await interaction.followup.send(embed=style.error(problem), ephemeral=True)
            return None
        return member

    async def _record(self, guild: discord.Guild, user_id: int, moderator: discord.abc.User, kind: str,
                      reason: str, duration: timedelta | None = None) -> int:
        expires = (datetime.now(timezone.utc) + duration).isoformat(timespec="seconds") if duration else None
        return await self.bot.db.execute(
            "INSERT INTO punishments (guild_id,user_id,moderator_id,type,reason,duration_seconds,expires_at,active,created_at) "
            "VALUES (?,?,?,?,?,?,?,?,?)",
            (guild.id, user_id, moderator.id, kind, reason, int(duration.total_seconds()) if duration else None,
             expires, 1 if kind in ("tempban",) else 0, now_iso()))

    async def _log(self, guild: discord.Guild, kind: str, target: discord.abc.User | str, moderator: discord.abc.User,
                   reason: str, extra: list[tuple[str, str]] | None = None, case: int | None = None) -> None:
        target_text = target if isinstance(target, str) else f"{target.mention}\n`{target.id}`"
        fields = [("Участник", target_text), ("Модератор", moderator.mention), ("Причина", reason)] + (extra or [])
        title = {"ban": "Бан", "tempban": "Временный бан", "unban": "Разбан", "kick": "Кик", "mute": "Мут",
                 "unmute": "Снятие мута", "timeout": "Тайм-аут", "untimeout": "Снятие тайм-аута",
                 "warn": "Предупреждение", "unwarn": "Снятие предупреждения", "clear": "Очистка сообщений"}[kind]
        embed = style.log_embed(f"{title}" + (f" · случай #{case}" if case else ""), emoji=ICONS.get(kind, "🔨"),
                                color=style.Colors.DANGER if kind in ("ban", "tempban", "kick") else style.Colors.WARNING,
                                fields=fields)
        await self.bot.logs.send(guild, "mod", embed)

    async def _dm(self, user: discord.abc.User, guild: discord.Guild, title: str, reason: str,
                  duration: str | None = None) -> None:
        text = f"**Сервер:** {guild.name}\n**Причина:** {reason}" + (f"\n**Срок:** {duration}" if duration else "")
        try:
            await user.send(embed=style.warning(title, text))
        except discord.HTTPException:
            pass

    def _done(self, title: str, text: str, emoji: str) -> discord.Embed:
        return style.base(title, text, color=style.Colors.PRIMARY, emoji=emoji)

    # ------------------------------------------------------------------ бан / кик
    @app_commands.command(name="ban", description="Забанить участника")
    @app_commands.describe(user="Кого", reason="Причина", duration="Срок (например 7д, 12ч). Пусто — навсегда",
                           delete_days="Удалить сообщения за N дней (0-7)")
    @app_commands.guild_only()
    @require("level:70")
    async def ban(self, interaction: discord.Interaction, user: discord.User, reason: app_commands.Range[str, 3, 400],
                  duration: str | None = None, delete_days: app_commands.Range[int, 0, 7] = 0) -> None:
        await interaction.response.defer(ephemeral=True)
        guild = interaction.guild
        assert guild
        delta = parse_duration(duration) if duration else None
        if duration and not delta:
            await interaction.followup.send(embed=style.error("Не понял срок. Примеры: 30м, 12ч, 7д."), ephemeral=True)
            return
        if guild.get_member(user.id) and not await self._target(interaction, user):
            return
        if not self._rate_ok(interaction.user):  # type: ignore[arg-type]
            await self._abuse_alert(interaction, "ban")
            return
        await self._dm(user, guild, "Вы заблокированы", reason, human_delta(delta) if delta else "навсегда")
        await guild.ban(user, reason=f"{interaction.user}: {reason}", delete_message_seconds=delete_days * 86400)
        kind = "tempban" if delta else "ban"
        case = await self._record(guild, user.id, interaction.user, kind, reason, delta)
        await self._log(guild, kind, user, interaction.user, reason,
                        [("Срок", human_delta(delta) if delta else "навсегда")], case)
        await interaction.followup.send(embed=self._done("Участник заблокирован",
                                                         f"{user.mention} · случай **#{case}**", "🔨"), ephemeral=True)

    @app_commands.command(name="unban", description="Разбанить пользователя по ID")
    @app_commands.guild_only()
    @require("level:70")
    async def unban(self, interaction: discord.Interaction, user_id: str, reason: app_commands.Range[str, 3, 400]) -> None:
        await interaction.response.defer(ephemeral=True)
        guild = interaction.guild
        assert guild
        if not user_id.isdigit():
            await interaction.followup.send(embed=style.error("Укажите числовой ID пользователя."), ephemeral=True)
            return
        try:
            user = await self.bot.fetch_user(int(user_id))
            await guild.unban(user, reason=f"{interaction.user}: {reason}")
        except discord.NotFound:
            await interaction.followup.send(embed=style.error("Этот пользователь не забанен."), ephemeral=True)
            return
        await self.bot.db.execute("UPDATE punishments SET active=0 WHERE guild_id=? AND user_id=? AND type='tempban'",
                                  (guild.id, user.id))
        case = await self._record(guild, user.id, interaction.user, "unban", reason)
        await self._log(guild, "unban", user, interaction.user, reason, case=case)
        await interaction.followup.send(embed=self._done("Разбан выполнен", f"{user.mention} снова может зайти.", "🕊️"),
                                        ephemeral=True)

    @app_commands.command(name="kick", description="Выгнать участника")
    @app_commands.guild_only()
    @require("level:60")
    async def kick(self, interaction: discord.Interaction, user: discord.Member,
                   reason: app_commands.Range[str, 3, 400]) -> None:
        await interaction.response.defer(ephemeral=True)
        member = await self._target(interaction, user)
        if not member:
            return
        if not self._rate_ok(interaction.user):  # type: ignore[arg-type]
            await self._abuse_alert(interaction, "kick")
            return
        await self._dm(member, member.guild, "Вы исключены с сервера", reason)
        await member.kick(reason=f"{interaction.user}: {reason}")
        case = await self._record(member.guild, member.id, interaction.user, "kick", reason)
        await self._log(member.guild, "kick", member, interaction.user, reason, case=case)
        await interaction.followup.send(embed=self._done("Участник исключён", f"{member.mention} · случай **#{case}**",
                                                         "👢"), ephemeral=True)

    # ------------------------------------------------------------------ мут / тайм-аут
    async def _timeout(self, interaction: discord.Interaction, user: discord.Member, delta: timedelta | None,
                       reason: str, kind: str) -> None:
        await interaction.response.defer(ephemeral=True)
        if not delta or delta > MAX_TIMEOUT:
            await interaction.followup.send(embed=style.error("Срок от 1 минуты до 28 дней. Примеры: 30м, 2ч, 1д."),
                                            ephemeral=True)
            return
        member = await self._target(interaction, user)
        if not member:
            return
        await member.timeout(delta, reason=f"{interaction.user}: {reason}")
        case = await self._record(member.guild, member.id, interaction.user, kind, reason, delta)
        await self._dm(member, member.guild, "Вам ограничен чат и голос" if kind == "mute" else "Вы получили тайм-аут",
                       reason, human_delta(delta))
        await self._log(member.guild, kind, member, interaction.user, reason,
                        [("Срок", human_delta(delta)), ("До", style.ts(discord.utils.utcnow() + delta))], case)
        await interaction.followup.send(embed=self._done("Ограничение выдано",
                                                         f"{member.mention} на **{human_delta(delta)}** · случай **#{case}**",
                                                         ICONS[kind]), ephemeral=True)

    async def _untimeout(self, interaction: discord.Interaction, user: discord.Member, reason: str, kind: str) -> None:
        await interaction.response.defer(ephemeral=True)
        member = await self._target(interaction, user)
        if not member:
            return
        if not member.is_timed_out():
            await interaction.followup.send(embed=style.error("У участника нет активного ограничения."), ephemeral=True)
            return
        await member.timeout(None, reason=f"{interaction.user}: {reason}")
        case = await self._record(member.guild, member.id, interaction.user, kind, reason)
        await self._log(member.guild, kind, member, interaction.user, reason, case=case)
        await interaction.followup.send(embed=self._done("Ограничение снято", member.mention, "🔊"), ephemeral=True)

    @app_commands.command(name="mute", description="Замутить участника (текст и голос) на срок")
    @app_commands.describe(duration="Срок: 30м, 2ч, 1д")
    @app_commands.guild_only()
    @require("level:40")
    async def mute(self, interaction: discord.Interaction, user: discord.Member, duration: str,
                   reason: app_commands.Range[str, 3, 400]) -> None:
        await self._timeout(interaction, user, parse_duration(duration), reason, "mute")

    @app_commands.command(name="unmute", description="Снять мут")
    @app_commands.guild_only()
    @require("level:40")
    async def unmute(self, interaction: discord.Interaction, user: discord.Member,
                     reason: app_commands.Range[str, 3, 400] = "Досрочно") -> None:
        await self._untimeout(interaction, user, reason, "unmute")

    @app_commands.command(name="timeout", description="Тайм-аут в минутах")
    @app_commands.guild_only()
    @require("level:40")
    async def timeout_cmd(self, interaction: discord.Interaction, user: discord.Member,
                          minutes: app_commands.Range[int, 1, 40320], reason: app_commands.Range[str, 3, 400]) -> None:
        await self._timeout(interaction, user, timedelta(minutes=minutes), reason, "timeout")

    @app_commands.command(name="untimeout", description="Снять тайм-аут")
    @app_commands.guild_only()
    @require("level:40")
    async def untimeout(self, interaction: discord.Interaction, user: discord.Member,
                        reason: app_commands.Range[str, 3, 400] = "Досрочно") -> None:
        await self._untimeout(interaction, user, reason, "untimeout")

    # ------------------------------------------------------------------ предупреждения
    @app_commands.command(name="warn", description="Выдать предупреждение")
    @app_commands.guild_only()
    @require("level:40")
    async def warn(self, interaction: discord.Interaction, user: discord.Member,
                   reason: app_commands.Range[str, 3, 400]) -> None:
        await interaction.response.defer(ephemeral=True)
        member = await self._target(interaction, user)
        if not member:
            return
        guild = member.guild
        await self.bot.db.execute("INSERT INTO warnings (guild_id,user_id,moderator_id,reason,created_at) VALUES (?,?,?,?,?)",
                                  (guild.id, member.id, interaction.user.id, reason, now_iso()))
        case = await self._record(guild, member.id, interaction.user, "warn", reason)
        count = await self.bot.db.fetchval("SELECT COUNT(*) FROM warnings WHERE guild_id=? AND user_id=? AND active=1",
                                           (guild.id, member.id), 0)
        await self._dm(member, guild, f"Предупреждение {count}/{settings.warns_to_ban}", reason)
        await self._log(guild, "warn", member, interaction.user, reason, [("Варнов", f"{count}/{settings.warns_to_ban}")],
                        case)
        text = f"{member.mention} · варн **{count}/{settings.warns_to_ban}** · случай **#{case}**"
        if count >= settings.warns_to_ban:
            delta = timedelta(days=settings.warn_ban_days)
            auto_reason = f"Накоплено {count} предупреждений"
            await self._dm(member, guild, "Вы заблокированы", auto_reason, human_delta(delta))
            try:
                await guild.ban(member, reason=f"POLARI: {auto_reason}")
                await self.bot.db.execute("UPDATE warnings SET active=0 WHERE guild_id=? AND user_id=?",
                                          (guild.id, member.id))
                ban_case = await self._record(guild, member.id, self.bot.user, "tempban", auto_reason, delta)  # type: ignore[arg-type]
                await self._log(guild, "tempban", member, self.bot.user, auto_reason,  # type: ignore[arg-type]
                                [("Срок", human_delta(delta))], ban_case)
                text += f"\n🔨 Автобан на **{settings.warn_ban_days} дн.**"
            except discord.HTTPException as exc:
                text += f"\n⚠️ Автобан не выполнен: {exc}"
        await interaction.followup.send(embed=self._done("Предупреждение выдано", text, "⚠️"), ephemeral=True)

    @app_commands.command(name="unwarn", description="Снять предупреждение по номеру")
    @app_commands.guild_only()
    @require("level:60")
    async def unwarn(self, interaction: discord.Interaction, warn_id: int, reason: app_commands.Range[str, 3, 400]) -> None:
        row = await self.bot.db.fetchone("SELECT * FROM warnings WHERE id=? AND guild_id=? AND active=1",
                                         (warn_id, interaction.guild_id))
        if not row:
            await interaction.response.send_message(embed=style.error("Активное предупреждение не найдено."), ephemeral=True)
            return
        await self.bot.db.execute("UPDATE warnings SET active=0 WHERE id=?", (warn_id,))
        await self._log(interaction.guild, "unwarn", f"<@{row['user_id']}>", interaction.user, reason,  # type: ignore[arg-type]
                        [("Варн", f"#{warn_id}")])
        await interaction.response.send_message(embed=self._done("Предупреждение снято", f"Варн **#{warn_id}**", "♻️"),
                                                ephemeral=True)

    @app_commands.command(name="warnings", description="Предупреждения и история наказаний участника")
    @app_commands.guild_only()
    @require("level:40")
    async def warnings(self, interaction: discord.Interaction, user: discord.User) -> None:
        db = self.bot.db
        warns = await db.fetchall("SELECT * FROM warnings WHERE guild_id=? AND user_id=? ORDER BY id DESC LIMIT 10",
                                  (interaction.guild_id, user.id))
        history = await db.fetchall("SELECT * FROM punishments WHERE guild_id=? AND user_id=? ORDER BY id DESC LIMIT 10",
                                    (interaction.guild_id, user.id))
        embed = style.base("История наказаний", f"Участник: {user.mention}", emoji="📒")
        embed.set_thumbnail(url=user.display_avatar.url)
        active = sum(1 for w in warns if w["active"])
        embed.add_field(name=f"Предупреждения · активных {active}/{settings.warns_to_ban}", inline=False, value="\n".join(
            f"{'🟦' if w['active'] else '⬜'} **#{w['id']}** · {w['reason'][:80]} · <@{w['moderator_id']}> · "
            f"{style.ts(w['created_at'], 'd')}" for w in warns) or "Нет")
        embed.add_field(name="Последние наказания", inline=False, value="\n".join(
            f"{ICONS.get(p['type'], '•')} **#{p['id']}** `{p['type']}` · {(p['reason'] or '')[:60]} · "
            f"{style.ts(p['created_at'], 'd')}" for p in history) or "Нет")
        await interaction.response.send_message(embed=embed, ephemeral=True)

    # ------------------------------------------------------------------ каналы
    @app_commands.command(name="clear", description="Удалить последние сообщения")
    @app_commands.describe(amount="Сколько (1-100)", user="Только сообщения этого участника")
    @app_commands.guild_only()
    @require("level:40")
    async def clear(self, interaction: discord.Interaction, amount: app_commands.Range[int, 1, 100],
                    user: discord.User | None = None) -> None:
        await interaction.response.defer(ephemeral=True)
        channel = interaction.channel
        if not isinstance(channel, (discord.TextChannel, discord.Thread, discord.VoiceChannel)):
            await interaction.followup.send(embed=style.error("Здесь нельзя очищать сообщения."), ephemeral=True)
            return
        matched = 0

        def check(message: discord.Message) -> bool:
            nonlocal matched
            if message.pinned or (user and message.author.id != user.id) or matched >= amount:
                return False
            matched += 1
            return True

        deleted = await channel.purge(limit=amount if not user else min(amount * 5, 500), check=check,
                                      reason=f"POLARI /clear {interaction.user}", bulk=True)
        await self._log(interaction.guild, "clear", channel.mention, interaction.user,  # type: ignore[arg-type]
                        f"Удалено {len(deleted)} сообщений" + (f" от {user}" if user else ""))
        await interaction.followup.send(embed=self._done("Чат очищен", f"Удалено сообщений: **{len(deleted)}**", "🧹"),
                                        ephemeral=True)

    @app_commands.command(name="slowmode", description="Медленный режим в канале (0 — выключить)")
    @app_commands.guild_only()
    @require("level:50")
    async def slowmode(self, interaction: discord.Interaction, seconds: app_commands.Range[int, 0, 21600]) -> None:
        channel = interaction.channel
        if not isinstance(channel, discord.TextChannel):
            await interaction.response.send_message(embed=style.error("Только для текстовых каналов."), ephemeral=True)
            return
        await channel.edit(slowmode_delay=seconds, reason=f"POLARI /slowmode {interaction.user}")
        await self.bot.logs.send(channel.guild, "mod", style.log_embed("Медленный режим", emoji="🐢", fields=[
            ("Канал", channel.mention), ("Интервал", f"{seconds} сек."), ("Модератор", interaction.user.mention)]))
        await interaction.response.send_message(embed=self._done(
            "Медленный режим", "выключен" if seconds == 0 else f"{seconds} сек. между сообщениями", "🐢"))

    async def _lock_targets(self, guild: discord.Guild) -> list[discord.Role]:
        targets = [guild.default_role]
        for key in ("member", "onhold"):
            role = await self.bot.registry.role(guild, key)
            if role:
                targets.append(role)
        return targets

    @app_commands.command(name="lock", description="Закрыть канал для сообщений участников")
    @app_commands.guild_only()
    @require("level:50")
    async def lock(self, interaction: discord.Interaction, reason: app_commands.Range[str, 3, 300] = "Временно") -> None:
        channel = interaction.channel
        if not isinstance(channel, discord.TextChannel):
            await interaction.response.send_message(embed=style.error("Только для текстовых каналов."), ephemeral=True)
            return
        exists = await self.bot.db.fetchval("SELECT COUNT(*) FROM channel_locks WHERE channel_id=?", (channel.id,), 0)
        if exists:
            await interaction.response.send_message(embed=style.error("Канал уже закрыт."), ephemeral=True)
            return
        await interaction.response.defer()
        for role in await self._lock_targets(channel.guild):
            ow = channel.overwrites_for(role)
            # Запоминаем прежнее значение, чтобы /unlock вернул всё как было
            await self.bot.db.execute("INSERT OR REPLACE INTO channel_locks VALUES (?,?,?,?)",
                                      (channel.guild.id, channel.id, role.id,
                                       json.dumps({"send_messages": ow.send_messages,
                                                   "send_messages_in_threads": ow.send_messages_in_threads})))
            ow.update(send_messages=False, send_messages_in_threads=False)
            await channel.set_permissions(role, overwrite=ow, reason=f"POLARI /lock {interaction.user}")
        await self.bot.logs.send(channel.guild, "mod", style.log_embed("Канал закрыт", emoji="🔒", fields=[
            ("Канал", channel.mention), ("Модератор", interaction.user.mention), ("Причина", reason)]))
        await interaction.followup.send(embed=self._done("Канал закрыт", f"**Причина:** {reason}", "🔒"))

    @app_commands.command(name="unlock", description="Открыть канал, закрытый через /lock")
    @app_commands.guild_only()
    @require("level:50")
    async def unlock(self, interaction: discord.Interaction) -> None:
        channel = interaction.channel
        rows = await self.bot.db.fetchall("SELECT * FROM channel_locks WHERE channel_id=?", (interaction.channel_id,))
        if not isinstance(channel, discord.TextChannel) or not rows:
            await interaction.response.send_message(embed=style.error("Канал не был закрыт через /lock."), ephemeral=True)
            return
        await interaction.response.defer()
        for row in rows:
            role = channel.guild.get_role(row["target_id"])
            if role:
                ow = channel.overwrites_for(role)
                ow.update(**json.loads(row["previous"]))
                await channel.set_permissions(role, overwrite=None if ow.is_empty() else ow,
                                              reason=f"POLARI /unlock {interaction.user}")
        await self.bot.db.execute("DELETE FROM channel_locks WHERE channel_id=?", (channel.id,))
        await self.bot.logs.send(channel.guild, "mod", style.log_embed("Канал открыт", emoji="🔓", fields=[
            ("Канал", channel.mention), ("Модератор", interaction.user.mention)]))
        await interaction.followup.send(embed=self._done("Канал открыт", "Можно снова общаться ❄️", "🔓"))

    # ------------------------------------------------------------------ жалобы
    @app_commands.command(name="report", description="Пожаловаться на участника")
    @app_commands.guild_only()
    @app_commands.checks.cooldown(1, 120)
    async def report(self, interaction: discord.Interaction, user: discord.Member,
                     reason: app_commands.Range[str, 5, 500]) -> None:
        if user.id == interaction.user.id or user.bot:
            await interaction.response.send_message(embed=style.error("Так пожаловаться нельзя."), ephemeral=True)
            return
        await self.bot.logs.send(interaction.guild, "mod", style.log_embed(  # type: ignore[arg-type]
            "Новая жалоба", color=style.Colors.WARNING, emoji="📣", fields=[
                ("На кого", user.mention), ("От кого", interaction.user.mention),
                ("Канал", interaction.channel.mention if interaction.channel else "—"),  # type: ignore[union-attr]
                ("Суть", reason)]))
        await interaction.response.send_message(embed=style.success(
            "Жалоба отправлена", "Модерация рассмотрит её в ближайшее время. Спасибо, что помогаете POLARI!"),
            ephemeral=True)

    # ------------------------------------------------------------------ авто-разбан
    @tasks.loop(minutes=5)
    async def unban_loop(self) -> None:
        rows = await self.bot.db.fetchall(
            "SELECT * FROM punishments WHERE type='tempban' AND active=1 AND expires_at <= ?", (now_iso(),))
        for row in rows:
            await self.bot.db.execute("UPDATE punishments SET active=0 WHERE id=?", (row["id"],))
            guild = self.bot.get_guild(row["guild_id"])
            if not guild:
                continue
            try:
                await guild.unban(discord.Object(id=row["user_id"]), reason="POLARI: срок бана истёк")
            except discord.NotFound:
                continue
            except discord.HTTPException as exc:
                log.warning("Авто-разбан %s не выполнен: %s", row["user_id"], exc)
                continue
            await self._log(guild, "unban", f"<@{row['user_id']}>", self.bot.user, "Срок бана истёк")  # type: ignore[arg-type]

    @unban_loop.before_loop
    async def _ready(self) -> None:
        await self.bot.wait_until_ready()


async def setup(bot: "PolariBot") -> None:
    await bot.add_cog(ModerationCog(bot))
