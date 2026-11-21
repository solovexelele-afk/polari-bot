"""Вступление на сервер: приветствие, роль «На проверке», комнаты ожидания и голосовое собеседование.

Логика: вход -> приветствие -> правила -> комната ожидания -> собеседование -> /interview_pass -> доступ.
Автоматической кнопочной верификации нет: доступ выдаёт только уполномоченный сотрудник.
"""
from __future__ import annotations

import logging
import time
from typing import TYPE_CHECKING

import discord
from discord import app_commands
from discord.ext import commands

from bot.data.blueprint import INTERVIEW_KEYS, WAITING_KEYS
from bot.database.db import now_iso
from bot.embeds import style
from bot.utils.checks import ensure, require

if TYPE_CHECKING:
    from bot.core import PolariBot

log = logging.getLogger("polari.interview")
NOTIFY_COOLDOWN = 300  # сек. между уведомлениями об одном и том же ожидающем


class InterviewCog(commands.Cog):
    def __init__(self, bot: "PolariBot"):
        self.bot = bot
        self._notified: dict[int, float] = {}

    async def cog_load(self) -> None:
        self.bot.add_view(InterviewPanelView(self))
        self.bot.add_dynamic_items(MoveToInterviewButton)
        setup_cog = self.bot.get_cog("SetupCog")
        setup_cog.register_panel("info", self.post_info_panel)          # type: ignore[union-attr]
        setup_cog.register_panel("interview", self.post_admin_panel)    # type: ignore[union-attr]

    # ------------------------------------------------------------------ данные
    async def get_record(self, guild_id: int, user_id: int):
        return await self.bot.db.fetchone("SELECT * FROM interviews WHERE guild_id=? AND user_id=?", (guild_id, user_id))

    async def set_status(self, guild_id: int, user_id: int, status: str, admin_id: int | None,
                         reason: str | None, action: str) -> None:
        now = now_iso()
        await self.bot.db.execute(
            "INSERT INTO interviews (guild_id,user_id,status,admin_id,reason,attempts,created_at,updated_at) "
            "VALUES (?,?,?,?,?,?,?,?) ON CONFLICT(guild_id,user_id) DO UPDATE SET status=excluded.status, "
            "admin_id=excluded.admin_id, reason=excluded.reason, updated_at=excluded.updated_at, "
            "attempts=interviews.attempts + (CASE WHEN excluded.status IN ('passed','failed') THEN 1 ELSE 0 END)",
            (guild_id, user_id, status, admin_id, reason, 1 if status in ("passed", "failed") else 0, now, now),
        )
        await self.bot.db.execute(
            "INSERT INTO interview_history (guild_id,user_id,admin_id,action,reason,created_at) VALUES (?,?,?,?,?,?)",
            (guild_id, user_id, admin_id, action, reason, now),
        )

    # ------------------------------------------------------------------ панели
    async def _links(self, guild: discord.Guild) -> discord.ui.View:
        view = discord.ui.View()
        rules = await self.bot.registry.channel(guild, "rules")
        waiting = await self.bot.registry.channel(guild, WAITING_KEYS[0]) if WAITING_KEYS else None
        if rules:
            view.add_item(discord.ui.Button(label="Правила", emoji="📘", url=rules.jump_url))
        if waiting:
            view.add_item(discord.ui.Button(label="Комната ожидания", emoji="🎧", url=waiting.jump_url))
        return view

    def _instruction(self) -> str:
        return (
            f"{style.THIN}\n"
            "**`1`** Прочитайте правила сервера — это обязательно.\n"
            "**`2`** Зайдите в любую голосовую комнату **Ожидание**.\n"
            "**`3`** Дождитесь проверяющего: он проведёт короткое голосовое собеседование.\n"
            "**`4`** После собеседования вы получите роль участника и полный доступ.\n"
            f"{style.THIN}\n"
            "> Пока проверка не пройдена, вам доступны только информационные каналы и комнаты ожидания."
        )

    async def post_info_panel(self, channel: discord.TextChannel) -> discord.Message:
        embed = style.header("Как получить доступ", (
            f"Добро пожаловать на **POLARI** ❄️\nДоступ к серверу выдаётся после короткого голосового "
            f"собеседования с проверяющим.\n\n{self._instruction()}"), style.Colors.PRIMARY)
        return await style.send_panel(channel, "info", [embed], view=await self._links(channel.guild))

    async def admin_panel_embed(self, guild: discord.Guild) -> discord.Embed:
        db = self.bot.db
        counts = {r["status"]: r["c"] for r in await db.fetchall(
            "SELECT status, COUNT(*) c FROM interviews WHERE guild_id=? GROUP BY status", (guild.id,))}
        embed = style.base("Панель собеседований", (
            "Здесь проверяющие видят очередь, историю и статистику обзвонов.\n"
            f"{style.THIN}\n"
            "`/interview_pass` — пропустить · `/interview_fail` — отказать\n"
            "`/interview_reset` — повторная проверка · `/interview_status` — статус\n"
            "`/interview_queue` — очередь"), color=style.Colors.PRIMARY, emoji="🎙️")
        embed.add_field(name="⏳ Ожидают", value=str(counts.get("pending", 0)))
        embed.add_field(name="✅ Пройдено", value=str(counts.get("passed", 0)))
        embed.add_field(name="✖ Отказано", value=str(counts.get("failed", 0)))
        return embed

    async def post_admin_panel(self, channel: discord.TextChannel) -> discord.Message:
        return await style.send_panel(channel, "interview", [await self.admin_panel_embed(channel.guild)],
                                      view=InterviewPanelView(self))

    # ------------------------------------------------------------------ события
    @commands.Cog.listener()
    async def on_member_join(self, member: discord.Member) -> None:
        if member.bot:
            return
        guild = member.guild
        now = now_iso()
        await self.bot.db.execute(
            "INSERT INTO users (guild_id,user_id,first_joined,last_joined) VALUES (?,?,?,?) "
            "ON CONFLICT(guild_id,user_id) DO UPDATE SET last_joined=excluded.last_joined, left_at=NULL",
            (guild.id, member.id, now, now))
        previous = await self.get_record(guild.id, member.id)
        onhold = await self.bot.registry.role(guild, "onhold")
        if onhold and onhold < guild.me.top_role:
            try:
                await member.add_roles(onhold, reason="POLARI: новый участник, ожидает собеседования")
                await self.bot.db.log_role_grant(guild.id, member.id, onhold.id, "add", "join", None)
            except discord.HTTPException as exc:
                log.warning("Не удалось выдать роль «На проверке» %s: %s", member, exc)
        await self.set_status(guild.id, member.id, "pending", None, None, "join")

        channel = await self.bot.registry.text(guild, "newbies")
        if channel:
            embed = style.base("Новый участник", (
                f"Привет, {member.mention}! Рады видеть тебя в **POLARI** ❄️\n"
                f"Ты наш **{guild.member_count}**-й участник.\n\n"
                "Перед получением доступа нужно ознакомиться с правилами и пройти "
                f"короткое **голосовое собеседование** с проверяющим.\n\n{self._instruction()}"),
                color=style.Colors.SECONDARY, emoji="👋")
            embed.set_thumbnail(url=member.display_avatar.url)
            try:
                await style.send_panel(channel, "welcome", [embed], view=await self._links(guild),
                                       content=member.mention)
            except discord.HTTPException as exc:
                log.warning("Приветствие не отправлено: %s", exc)

        note = ""
        if previous and previous["status"] == "passed":
            note = "Ранее уже проходил собеседование — решите, нужна ли повторная проверка."
        await self.bot.logs.send(guild, "interview", style.log_embed("Ожидает собеседования", emoji="⏳", fields=[
            ("Пользователь", f"{member.mention}\n`{member.id}`"),
            ("Аккаунт создан", style.ts(member.created_at, "R")),
            ("Примечание", note or "—"),
        ]))

    @commands.Cog.listener()
    async def on_member_remove(self, member: discord.Member) -> None:
        if member.bot:
            return
        record = await self.get_record(member.guild.id, member.id)
        if record and record["status"] == "pending":
            await self.set_status(member.guild.id, member.id, "left", None, "Покинул сервер до собеседования", "left")

    @commands.Cog.listener()
    async def on_voice_state_update(self, member: discord.Member, before: discord.VoiceState,
                                    after: discord.VoiceState) -> None:
        if member.bot or not after.channel or before.channel == after.channel:
            return
        guild = member.guild
        waiting_ids = {int(v) for k in WAITING_KEYS if (v := await self.bot.registry.get(guild.id, f"channel.{k}"))}
        if after.channel.id not in waiting_ids:
            return
        record = await self.get_record(guild.id, member.id)
        if not record or record["status"] not in ("pending", "failed"):
            return
        if time.monotonic() - self._notified.get(member.id, 0) < NOTIFY_COOLDOWN:
            return
        self._notified[member.id] = time.monotonic()
        panel = await self.bot.registry.text(guild, "interview_panel")
        if not panel:
            return
        embed = style.base("Ожидает собеседования", (
            f"{member.mention} зашёл в {after.channel.mention}.\n"
            f"Аккаунт создан {style.ts(member.created_at, 'R')} · попыток: **{record['attempts']}**"),
            color=style.Colors.ACCENT, emoji="🎧")
        embed.set_thumbnail(url=member.display_avatar.url)
        view = discord.ui.View(timeout=None)
        view.add_item(MoveToInterviewButton(member.id))
        await panel.send(embed=embed, view=view)

    # ------------------------------------------------------------------ команды
    async def _resolve(self, interaction: discord.Interaction, user: discord.User) -> discord.Member | None:
        guild = interaction.guild
        assert guild
        member = guild.get_member(user.id)
        if member is None:
            try:
                member = await guild.fetch_member(user.id)
            except discord.NotFound:
                await interaction.followup.send(embed=style.error("Пользователь не находится на сервере."),
                                                ephemeral=True)
                return None
        if member.bot:
            await interaction.followup.send(embed=style.error("Ботов не нужно проверять."), ephemeral=True)
            return None
        return member

    async def _notify(self, member: discord.Member, embed: discord.Embed) -> bool:
        try:
            await member.send(embed=embed)
            return True
        except discord.HTTPException:
            return False

    @app_commands.command(name="interview_pass", description="Подтвердить успешное прохождение собеседования")
    @app_commands.describe(user="Участник", reason="Причина / комментарий")
    @app_commands.guild_only()
    @require("interview")
    async def interview_pass(self, interaction: discord.Interaction, user: discord.User,
                             reason: app_commands.Range[str, 3, 300] = "Успешно прошёл голосовой обзвон.") -> None:
        await interaction.response.defer(ephemeral=True)
        member = await self._resolve(interaction, user)
        if not member:
            return
        guild, admin = member.guild, interaction.user
        if member.id == admin.id:
            await interaction.followup.send(embed=style.error("Нельзя проверить самого себя."), ephemeral=True)
            return
        record = await self.get_record(guild.id, member.id)
        passed = await self.bot.registry.role(guild, "passed")
        member_role = await self.bot.registry.role(guild, "member")
        onhold = await self.bot.registry.role(guild, "onhold")
        if record and record["status"] == "passed" and passed in member.roles:
            await interaction.followup.send(embed=style.error(f"{member.mention} уже прошёл собеседование."),
                                            ephemeral=True)
            return
        if not passed:
            await interaction.followup.send(embed=style.error("Роль «Прошёл обзвон» не настроена. Выполните /setup."),
                                            ephemeral=True)
            return
        give_member = (await self.bot.registry.get(guild.id, "interview.give_member") or "1") == "1"
        to_add = [r for r in (passed, member_role if give_member else None) if r and r not in member.roles]
        to_remove = [onhold] if onhold and onhold in member.roles else []
        too_high = [r.name for r in to_add + to_remove if r >= guild.me.top_role]
        if too_high:
            await interaction.followup.send(embed=style.error(
                f"Роли выше роли бота: {', '.join(too_high)}. Поднимите роль POLARI."), ephemeral=True)
            return
        new_roles = [r for r in member.roles if not r.is_default() and r not in to_remove] + to_add
        try:
            # Одна операция edit: роли меняются атомарно — либо все, либо ничего
            await member.edit(roles=new_roles, reason=f"POLARI: собеседование пройдено ({admin})")
        except discord.HTTPException as exc:
            await interaction.followup.send(embed=style.error(f"Discord не дал изменить роли: {exc}. "
                                                              "Роли участника не изменены."), ephemeral=True)
            return
        await self.set_status(guild.id, member.id, "passed", admin.id, reason, "pass")
        for r in to_add:
            await self.bot.db.log_role_grant(guild.id, member.id, r.id, "add", "interview", admin.id)
        for r in to_remove:
            await self.bot.db.log_role_grant(guild.id, member.id, r.id, "remove", "interview", admin.id)

        dm = await self._notify(member, style.success("Добро пожаловать в POLARI", (
            f"Вы успешно прошли собеседование на сервере **{guild.name}**.\n"
            "Теперь вам доступны все каналы. Приятного общения! ❄️")))
        await self.bot.logs.send(guild, "interview", style.log_embed("Собеседование пройдено", color=style.Colors.SUCCESS,
                                                                     emoji="✅", fields=[
            ("Пользователь", member.mention), ("Администратор", admin.mention), ("Статус", "Успешно"),
            ("Причина", reason), ("Дата", style.ts(discord.utils.utcnow())),
            ("Выданы роли", ", ".join(r.mention for r in to_add) or "—"),
        ]))
        await interaction.followup.send(embed=style.success("Собеседование засчитано", (
            f"{member.mention} получил доступ к серверу.\n"
            f"Уведомление в ЛС: {'отправлено' if dm else 'закрыты ЛС'}")), ephemeral=True)

    @app_commands.command(name="interview_fail", description="Отказать в прохождении собеседования")
    @app_commands.describe(user="Участник", reason="Причина отказа")
    @app_commands.guild_only()
    @require("interview")
    async def interview_fail(self, interaction: discord.Interaction, user: discord.User,
                             reason: app_commands.Range[str, 3, 300]) -> None:
        await interaction.response.defer(ephemeral=True)
        member = await self._resolve(interaction, user)
        if not member:
            return
        record = await self.get_record(member.guild.id, member.id)
        if record and record["status"] == "passed":
            await interaction.followup.send(embed=style.error(
                "Участник уже прошёл собеседование. Сначала используйте /interview_reset."), ephemeral=True)
            return
        await self.set_status(member.guild.id, member.id, "failed", interaction.user.id, reason, "fail")
        dm = await self._notify(member, style.warning("Собеседование не пройдено", (
            f"К сожалению, собеседование на **{member.guild.name}** не пройдено.\n**Причина:** {reason}\n\n"
            "Вы можете попробовать снова позже — зайдите в комнату ожидания.")))
        await self.bot.logs.send(member.guild, "interview", style.log_embed("Собеседование не пройдено",
                                                                            color=style.Colors.DANGER, emoji="✖", fields=[
            ("Пользователь", member.mention), ("Администратор", interaction.user.mention),
            ("Статус", "Отказ"), ("Причина", reason), ("Дата", style.ts(discord.utils.utcnow())),
        ]))
        await interaction.followup.send(embed=style.base("Отказ записан", (
            f"{member.mention} остаётся на проверке.\nУведомление в ЛС: {'отправлено' if dm else 'закрыты ЛС'}"),
            color=style.Colors.WARNING, emoji="📝"), ephemeral=True)

    @app_commands.command(name="interview_reset", description="Вернуть участника в статус ожидания собеседования")
    @app_commands.describe(user="Участник", reason="Причина")
    @app_commands.guild_only()
    @require("interview")
    async def interview_reset(self, interaction: discord.Interaction, user: discord.User,
                              reason: app_commands.Range[str, 3, 300]) -> None:
        await interaction.response.defer(ephemeral=True)
        member = await self._resolve(interaction, user)
        if not member:
            return
        guild = member.guild
        if await self.bot.registry.member_level(member) >= await self.bot.registry.member_level(interaction.user):  # type: ignore[arg-type]
            if interaction.user.id != guild.owner_id:
                await interaction.followup.send(embed=style.error("Нельзя сбросить проверку равному или старшему."),
                                                ephemeral=True)
                return
        passed = await self.bot.registry.role(guild, "passed")
        member_role = await self.bot.registry.role(guild, "member")
        onhold = await self.bot.registry.role(guild, "onhold")
        to_remove = [r for r in (passed, member_role) if r and r in member.roles]
        to_add = [onhold] if onhold and onhold not in member.roles else []
        if any(r >= guild.me.top_role for r in to_remove + to_add):
            await interaction.followup.send(embed=style.error("Роли выше роли бота. Поднимите роль POLARI."),
                                            ephemeral=True)
            return
        new_roles = [r for r in member.roles if not r.is_default() and r not in to_remove] + to_add
        try:
            await member.edit(roles=new_roles, reason=f"POLARI: повторная проверка ({interaction.user})")
        except discord.HTTPException as exc:
            await interaction.followup.send(embed=style.error(f"Роли не изменены: {exc}"), ephemeral=True)
            return
        await self.set_status(guild.id, member.id, "pending", interaction.user.id, reason, "reset")
        for r in to_remove:
            await self.bot.db.log_role_grant(guild.id, member.id, r.id, "remove", "interview_reset", interaction.user.id)
        await self.bot.logs.send(guild, "interview", style.log_embed("Проверка сброшена", color=style.Colors.WARNING,
                                                                     emoji="↺", fields=[
            ("Пользователь", member.mention), ("Администратор", interaction.user.mention), ("Причина", reason),
        ]))
        await interaction.followup.send(embed=style.base("Проверка сброшена",
                                                         f"{member.mention} снова ожидает собеседования.",
                                                         emoji="↺"), ephemeral=True)

    @app_commands.command(name="interview_status", description="Статус собеседования участника")
    @app_commands.guild_only()
    @require("interview")
    async def interview_status(self, interaction: discord.Interaction, user: discord.User) -> None:
        guild = interaction.guild
        assert guild
        record = await self.get_record(guild.id, user.id)
        labels = {"pending": "⏳ Ожидает", "passed": "✅ Пройдено", "failed": "✖ Отказ", "left": "🚪 Покинул сервер"}
        embed = style.base("Статус собеседования", f"Участник: {user.mention}", emoji="🔎")
        embed.set_thumbnail(url=user.display_avatar.url)
        if not record:
            embed.description += "\n\nЗаписей нет — участник ещё не заходил после запуска POLARI."
        else:
            embed.add_field(name="Статус", value=labels.get(record["status"], record["status"]))
            embed.add_field(name="Попыток", value=str(record["attempts"]))
            embed.add_field(name="Проверяющий", value=f"<@{record['admin_id']}>" if record["admin_id"] else "—")
            embed.add_field(name="Причина", value=record["reason"] or "—", inline=False)
            embed.add_field(name="Обновлено", value=style.ts(record["updated_at"]))
        history = await self.bot.db.fetchall(
            "SELECT * FROM interview_history WHERE guild_id=? AND user_id=? ORDER BY id DESC LIMIT 5",
            (guild.id, user.id))
        if history:
            embed.add_field(name="Последние действия", inline=False, value="\n".join(
                f"{style.ts(h['created_at'], 'd')} · `{h['action']}`" + (f" · <@{h['admin_id']}>" if h["admin_id"] else "")
                for h in history))
        await interaction.response.send_message(embed=embed, ephemeral=True)

    async def queue_embed(self, guild: discord.Guild) -> discord.Embed:
        rows = await self.bot.db.fetchall(
            "SELECT user_id, updated_at, attempts FROM interviews WHERE guild_id=? AND status IN ('pending','failed') "
            "ORDER BY updated_at LIMIT 50", (guild.id,))
        waiting_ids = {int(v) for k in WAITING_KEYS if (v := await self.bot.registry.get(guild.id, f"channel.{k}"))}
        lines = []
        for r in rows:
            m = guild.get_member(r["user_id"])
            if not m:
                continue
            in_voice = m.voice and m.voice.channel and m.voice.channel.id in waiting_ids
            mark = f"🔊 {m.voice.channel.mention}" if in_voice else "💤 не в комнате"
            lines.append((not in_voice, f"{m.mention} · {mark} · с {style.ts(r['updated_at'], 'R')}"))
        lines.sort(key=lambda pair: pair[0])  # сначала те, кто уже сидит в комнате ожидания
        return style.base("Очередь собеседований", "\n".join(t for _, t in lines[:30]) or "Очередь пуста ✨",
                          color=style.Colors.ACCENT, emoji="⏳")

    @app_commands.command(name="interview_queue", description="Список ожидающих собеседования")
    @app_commands.guild_only()
    @require("interview")
    async def interview_queue(self, interaction: discord.Interaction) -> None:
        assert interaction.guild
        await interaction.response.send_message(embed=await self.queue_embed(interaction.guild), ephemeral=True)


class InterviewPanelView(discord.ui.View):
    """Постоянная панель в канале обзвонов."""

    def __init__(self, cog: InterviewCog):
        super().__init__(timeout=None)
        self.cog = cog

    @discord.ui.button(label="Очередь", emoji="⏳", style=discord.ButtonStyle.primary, custom_id="polari:iv:queue")
    async def queue(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        if await ensure(interaction, "interview"):
            await interaction.response.send_message(embed=await self.cog.queue_embed(interaction.guild), ephemeral=True)

    @discord.ui.button(label="История", emoji="📜", style=discord.ButtonStyle.secondary, custom_id="polari:iv:history")
    async def history(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        if not await ensure(interaction, "interview"):
            return
        rows = await self.cog.bot.db.fetchall(
            "SELECT * FROM interview_history WHERE guild_id=? AND action IN ('pass','fail','reset') "
            "ORDER BY id DESC LIMIT 20", (interaction.guild_id,))
        icons = {"pass": "✅", "fail": "✖", "reset": "↺"}
        text = "\n".join(f"{icons[r['action']]} <@{r['user_id']}> · <@{r['admin_id']}> · {style.ts(r['created_at'], 'R')}"
                         for r in rows) or "Пока пусто."
        await interaction.response.send_message(embed=style.base("История собеседований", text, emoji="📜"),
                                                ephemeral=True)

    @discord.ui.button(label="Статистика", emoji="📊", style=discord.ButtonStyle.secondary, custom_id="polari:iv:stats")
    async def stats(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        if not await ensure(interaction, "interview"):
            return
        db = self.cog.bot.db
        embed = await self.cog.admin_panel_embed(interaction.guild)
        embed.description = "Статистика проверок POLARI"
        top = await db.fetchall(
            "SELECT admin_id, SUM(action='pass') p, SUM(action='fail') f FROM interview_history "
            "WHERE guild_id=? AND admin_id IS NOT NULL AND action IN ('pass','fail') "
            "GROUP BY admin_id ORDER BY p DESC LIMIT 10", (interaction.guild_id,))
        embed.add_field(name="Проверяющие", inline=False, value="\n".join(
            f"<@{r['admin_id']}> — ✅ {r['p']} · ✖ {r['f']}" for r in top) or "Пока нет данных.")
        week = await db.fetchval(
            "SELECT COUNT(*) FROM interview_history WHERE guild_id=? AND action='pass' AND created_at >= datetime('now','-7 days')",
            (interaction.guild_id,), 0)
        embed.add_field(name="Пропущено за 7 дней", value=str(week))
        await interaction.response.send_message(embed=embed, ephemeral=True)


class MoveToInterviewButton(discord.ui.DynamicItem[discord.ui.Button], template=r"polari:iv:move:(?P<uid>\d+)"):
    """Перемещает ожидающего (и проверяющего, если он в голосе) в свободную комнату собеседования."""

    def __init__(self, user_id: int):
        super().__init__(discord.ui.Button(label="Взять на собеседование", emoji="🎙️",
                                           style=discord.ButtonStyle.success, custom_id=f"polari:iv:move:{user_id}"))
        self.user_id = user_id

    @classmethod
    async def from_custom_id(cls, interaction, item, match):  # type: ignore[override]
        return cls(int(match["uid"]))

    async def callback(self, interaction: discord.Interaction) -> None:  # type: ignore[override]
        if not await ensure(interaction, "interview"):
            return
        guild = interaction.guild
        bot = interaction.client
        member = guild.get_member(self.user_id)
        if not member or not member.voice or not member.voice.channel:
            await interaction.response.send_message(embed=style.error("Участник сейчас не в голосовом канале."),
                                                    ephemeral=True)
            return
        room = None
        for key in INTERVIEW_KEYS:
            ch = await bot.registry.channel(guild, key)  # type: ignore[attr-defined]
            if isinstance(ch, discord.VoiceChannel) and not [m for m in ch.members if not m.bot]:
                room = ch
                break
        if room is None:
            await interaction.response.send_message(embed=style.error("Все комнаты собеседования заняты."),
                                                    ephemeral=True)
            return
        try:
            await member.move_to(room, reason=f"POLARI: собеседование ({interaction.user})")
            staff = interaction.user
            if isinstance(staff, discord.Member) and staff.voice and staff.voice.channel:
                await staff.move_to(room, reason="POLARI: собеседование")
        except discord.HTTPException as exc:
            await interaction.response.send_message(embed=style.error(f"Не удалось переместить: {exc}"), ephemeral=True)
            return
        await interaction.response.edit_message(view=None, embed=style.base(
            "Собеседование начато", f"{member.mention} → {room.mention}\nПроверяющий: {interaction.user.mention}",
            color=style.Colors.SUCCESS, emoji="🎙️"))


async def setup(bot: "PolariBot") -> None:
    await bot.add_cog(InterviewCog(bot))
