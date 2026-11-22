"""Рейтинг POLARI: опыт за чат и голос, уровни, валюта «Вафли» и топ участников.

* Чат: 15–25 опыта за сообщение, не чаще раза в минуту (защита от фарма флудом).
* Голос: 10 опыта и 60 секунд за каждую минуту в канале, где есть хотя бы ещё один человек
  и участник не заглушил звук себе (AFK-фарм в одиночку не засчитывается).
* Новый уровень: +10 🧇 × номер уровня и поздравление.
"""
from __future__ import annotations

import logging
import random
import time
from typing import TYPE_CHECKING

import discord
from discord import app_commands
from discord.ext import commands, tasks

from bot.embeds import style

if TYPE_CHECKING:
    from bot.core import PolariBot

log = logging.getLogger("polari.levels")
WAFFLE = "🧇"
MSG_XP = (15, 25)
MSG_COOLDOWN = 60
VOICE_XP_PER_MIN = 10
PAGE_SIZE = 10
SORTS = {
    "xp": ("Опыт", "🏆", "xp DESC", "опыту"),
    "voice": ("Голос", "🎙️", "voice_seconds DESC", "голосовой активности"),
    "chat": ("Чат", "💬", "messages DESC", "активности в чате"),
    "waffles": ("Вафли", WAFFLE, "waffles DESC", "вафлям"),
}


def xp_for_level(level: int) -> int:
    """Опыт, нужный для перехода с уровня level на level+1."""
    return 5 * level * level + 50 * level + 100


def level_from_xp(xp: int) -> tuple[int, int, int]:
    """(уровень, опыт внутри уровня, опыт до следующего уровня)."""
    level = 0
    while xp >= xp_for_level(level):
        xp -= xp_for_level(level)
        level += 1
    return level, xp, xp_for_level(level)


def fmt_voice(seconds: int) -> str:
    hours, rest = divmod(int(seconds), 3600)
    return f"{hours}:{rest // 60:02d}:{rest % 60:02d}"


def progress_bar(current: int, total: int, size: int = 12) -> str:
    filled = round(size * current / total) if total else 0
    return "▰" * filled + "▱" * (size - filled)


class LevelsCog(commands.Cog):
    def __init__(self, bot: "PolariBot"):
        self.bot = bot
        self._last_msg: dict[tuple[int, int], float] = {}

    async def cog_load(self) -> None:
        self.bot.add_view(LeaderboardView(self))
        self.bot.get_cog("SetupCog").register_panel("leaderboard", self.post_leaderboard)  # type: ignore[union-attr]
        self.voice_loop.start()
        self.refresh_loop.start()

    async def cog_unload(self) -> None:
        self.voice_loop.cancel()
        self.refresh_loop.cancel()

    # ------------------------------------------------------------------ канал рейтинга
    async def board_embed(self, guild: discord.Guild) -> discord.Embed:
        db = self.bot.db
        top_xp = await db.fetchall("SELECT * FROM levels WHERE guild_id=? ORDER BY xp DESC LIMIT 10", (guild.id,))
        top_voice = await db.fetchall("SELECT * FROM levels WHERE guild_id=? AND voice_seconds>0 "
                                      "ORDER BY voice_seconds DESC LIMIT 5", (guild.id,))
        total = await db.fetchval("SELECT COUNT(*) FROM levels WHERE guild_id=?", (guild.id,), 0)
        medals = {1: "🥇", 2: "🥈", 3: "🥉"}
        xp_lines = [f"{medals.get(i, f'`#{i}`')} <@{r['user_id']}> — ур. **{r['level']}** · {r['xp']} опыта · "
                    f"{r['waffles']} {WAFFLE}" for i, r in enumerate(top_xp, 1)]
        voice_lines = [f"{medals.get(i, f'`#{i}`')} <@{r['user_id']}> — 🎙️ {fmt_voice(r['voice_seconds'])}"
                       for i, r in enumerate(top_voice, 1)]
        embed = style.header("Топ рейтинга участников", (
            "Опыт начисляется за общение в чатах и за время в голосовых каналах.\n"
            f"Каждый новый уровень — награда в вафлях {WAFFLE}. Свой профиль — `/rank` во флуде.\n\n"
            "**🏆 По опыту**\n" + ("\n".join(xp_lines) or "Пока пусто — начните общаться!") +
            "\n\n**🎙️ По голосу**\n" + ("\n".join(voice_lines) or "Пока пусто.")), style.Colors.SECONDARY)
        embed.set_footer(text=f"Участников в рейтинге: {total} · обновляется каждые 5 минут")
        embed.timestamp = discord.utils.utcnow()
        return embed

    async def post_leaderboard(self, channel: discord.TextChannel) -> discord.Message:
        return await style.send_panel(channel, "leaderboard", [await self.board_embed(channel.guild)],
                                      view=LeaderboardView(self))

    @tasks.loop(minutes=5)
    async def refresh_loop(self) -> None:
        for guild in self.bot.guilds:
            row = await self.bot.db.fetchone("SELECT channel_id, message_id FROM panels WHERE guild_id=? AND key=?",
                                             (guild.id, "leaderboard"))
            channel = guild.get_channel(row["channel_id"]) if row else None
            if not isinstance(channel, discord.TextChannel):
                continue
            try:
                message = channel.get_partial_message(row["message_id"])
                await message.edit(embeds=[await self.board_embed(guild)], view=LeaderboardView(self))
            except discord.NotFound:
                continue
            except discord.HTTPException as exc:
                log.warning("Топ не обновлён: %s", exc)

    @refresh_loop.before_loop
    async def _ready_refresh(self) -> None:
        await self.bot.wait_until_ready()

    async def in_flood(self, interaction: discord.Interaction) -> bool:
        """Команды рейтинга работают только во флуде (если канал флуда настроен)."""
        flood_id = await self.bot.registry.get(interaction.guild_id, "channel.flood")  # type: ignore[arg-type]
        if not flood_id or str(interaction.channel_id) == flood_id:
            return True
        await interaction.response.send_message(embed=style.error(
            f"Команды рейтинга работают только в <#{flood_id}>."), ephemeral=True)
        return False

    # ------------------------------------------------------------------ начисление
    async def add(self, member: discord.Member, *, xp: int = 0, messages: int = 0, voice: int = 0) -> None:
        db = self.bot.db
        await db.execute(
            "INSERT INTO levels (guild_id,user_id,xp,messages,voice_seconds) VALUES (?,?,?,?,?) "
            "ON CONFLICT(guild_id,user_id) DO UPDATE SET xp=xp+excluded.xp, messages=messages+excluded.messages, "
            "voice_seconds=voice_seconds+excluded.voice_seconds",
            (member.guild.id, member.id, xp, messages, voice))
        row = await db.fetchone("SELECT xp, level FROM levels WHERE guild_id=? AND user_id=?",
                                (member.guild.id, member.id))
        new_level = level_from_xp(row["xp"])[0]
        if new_level <= row["level"]:
            return
        reward = sum(10 * lvl for lvl in range(row["level"] + 1, new_level + 1))
        await db.execute("UPDATE levels SET level=?, waffles=waffles+? WHERE guild_id=? AND user_id=?",
                         (new_level, reward, member.guild.id, member.id))
        target = await self.bot.registry.text(member.guild, "leaderboard") \
            or await self.bot.registry.text(member.guild, "flood")
        if target is None:
            return
        embed = style.base("Новый уровень", (
            f"Ура! {member.mention} достиг **{new_level}** уровня! 🎉\nНаграда: **+{reward} {WAFFLE}**"),
            color=style.Colors.ACCENT, emoji="⬆️")
        embed.set_thumbnail(url=member.display_avatar.url)
        try:
            await target.send(embed=embed, allowed_mentions=discord.AllowedMentions(users=False))
        except discord.HTTPException:
            pass

    @commands.Cog.listener()
    async def on_message(self, message: discord.Message) -> None:
        if not message.guild or message.author.bot or not isinstance(message.author, discord.Member):
            return
        key = (message.guild.id, message.author.id)
        now = time.monotonic()
        give_xp = now - self._last_msg.get(key, 0) >= MSG_COOLDOWN
        if give_xp:
            self._last_msg[key] = now
        await self.add(message.author, xp=random.randint(*MSG_XP) if give_xp else 0, messages=1)

    @tasks.loop(minutes=1)
    async def voice_loop(self) -> None:
        for guild in self.bot.guilds:
            afk_id = guild.afk_channel.id if guild.afk_channel else None
            for channel in guild.voice_channels + guild.stage_channels:
                if channel.id == afk_id:
                    continue
                humans = [m for m in channel.members if not m.bot]
                if len(humans) < 2:
                    continue  # в одиночку опыт не фармится
                for member in humans:
                    vs = member.voice
                    if vs is None or vs.self_deaf or vs.deaf:
                        continue
                    try:
                        await self.add(member, xp=VOICE_XP_PER_MIN, voice=60)
                    except Exception:
                        log.exception("Не удалось начислить голосовой опыт")

    @voice_loop.before_loop
    async def _ready(self) -> None:
        await self.bot.wait_until_ready()

    # ------------------------------------------------------------------ профиль
    async def stats(self, guild_id: int, user_id: int):
        return await self.bot.db.fetchone("SELECT * FROM levels WHERE guild_id=? AND user_id=?", (guild_id, user_id))

    async def position(self, guild_id: int, column: str, value: int) -> int:
        return await self.bot.db.fetchval(f"SELECT COUNT(*) FROM levels WHERE guild_id=? AND {column} > ?",
                                          (guild_id, value), 0) + 1

    @app_commands.command(name="rank", description="Уровень, опыт, вафли и активность участника")
    @app_commands.guild_only()
    async def rank(self, interaction: discord.Interaction, user: discord.Member | None = None) -> None:
        if not await self.in_flood(interaction):
            return
        member = user or interaction.user
        assert isinstance(member, discord.Member)
        row = await self.stats(member.guild.id, member.id)
        xp = row["xp"] if row else 0
        level, inside, need = level_from_xp(xp)
        embed = style.base("Профиль", f"{member.mention}", color=style.Colors.SECONDARY, emoji="📊", timestamp=False)
        embed.set_thumbnail(url=member.display_avatar.url)
        embed.add_field(name="Уровень", value=f"**{level}**")
        embed.add_field(name="Место", value=f"#{await self.position(member.guild.id, 'xp', xp)}")
        embed.add_field(name="Вафли", value=f"**{row['waffles'] if row else 0}** {WAFFLE}")
        embed.add_field(name=f"Опыт · {inside}/{need}", value=f"`{progress_bar(inside, need)}`", inline=False)
        embed.add_field(name="🎙️ В голосе", value=fmt_voice(row["voice_seconds"] if row else 0))
        embed.add_field(name="💬 Сообщений", value=str(row["messages"] if row else 0))
        embed.add_field(name="⭐ Всего опыта", value=str(xp))
        await interaction.response.send_message(embed=embed)

    @app_commands.command(name="top", description="Топ рейтинга участников")
    @app_commands.choices(sort=[app_commands.Choice(name=v[0], value=k) for k, v in SORTS.items()])
    @app_commands.guild_only()
    async def top(self, interaction: discord.Interaction, sort: app_commands.Choice[str] | None = None) -> None:
        if not await self.in_flood(interaction):
            return
        view = TopView(self, interaction.user.id, sort.value if sort else "xp")
        await interaction.response.send_message(embed=await view.render(interaction.guild), view=view)  # type: ignore[arg-type]

    @app_commands.command(name="waffles_pay", description=f"Передать вафли {WAFFLE} другому участнику")
    @app_commands.guild_only()
    @app_commands.checks.cooldown(1, 30)
    async def waffles_pay(self, interaction: discord.Interaction, user: discord.Member,
                          amount: app_commands.Range[int, 1, 1_000_000]) -> None:
        if not await self.in_flood(interaction):
            return
        if user.bot or user.id == interaction.user.id:
            await interaction.response.send_message(embed=style.error("Так перевести нельзя."), ephemeral=True)
            return
        db = self.bot.db
        row = await self.stats(interaction.guild_id, interaction.user.id)  # type: ignore[arg-type]
        if not row or row["waffles"] < amount:
            await interaction.response.send_message(embed=style.error(
                f"Недостаточно вафель: у вас {row['waffles'] if row else 0} {WAFFLE}."), ephemeral=True)
            return
        await db.execute("UPDATE levels SET waffles=waffles-? WHERE guild_id=? AND user_id=? AND waffles>=?",
                         (amount, interaction.guild_id, interaction.user.id, amount))
        await db.execute("INSERT INTO levels (guild_id,user_id,waffles) VALUES (?,?,?) ON CONFLICT(guild_id,user_id) "
                         "DO UPDATE SET waffles=waffles+excluded.waffles", (interaction.guild_id, user.id, amount))
        await interaction.response.send_message(embed=style.success(
            "Перевод вафель", f"{interaction.user.mention} → {user.mention}: **{amount} {WAFFLE}**"))


class LeaderboardView(discord.ui.View):
    """Постоянные кнопки под топом: открыть свою таблицу (видна только вам) и свой профиль."""

    def __init__(self, cog: LevelsCog):
        super().__init__(timeout=None)
        self.cog = cog

    async def _open(self, interaction: discord.Interaction, sort: str) -> None:
        view = TopView(self.cog, interaction.user.id, sort)
        await interaction.response.send_message(embed=await view.render(interaction.guild), view=view,  # type: ignore[arg-type]
                                                ephemeral=True)

    @discord.ui.button(label="Опыт", emoji="🏆", style=discord.ButtonStyle.secondary, custom_id="polari:lb:xp")
    async def by_xp(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        await self._open(interaction, "xp")

    @discord.ui.button(label="Голос", emoji="🎙️", style=discord.ButtonStyle.secondary, custom_id="polari:lb:voice")
    async def by_voice(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        await self._open(interaction, "voice")

    @discord.ui.button(label="Чат", emoji="💬", style=discord.ButtonStyle.secondary, custom_id="polari:lb:chat")
    async def by_chat(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        await self._open(interaction, "chat")

    @discord.ui.button(label="Вафли", emoji=WAFFLE, style=discord.ButtonStyle.secondary, custom_id="polari:lb:waffles")
    async def by_waffles(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        await self._open(interaction, "waffles")

    @discord.ui.button(label="Мой профиль", emoji="📊", style=discord.ButtonStyle.primary, custom_id="polari:lb:me")
    async def me(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        member = interaction.user
        row = await self.cog.stats(interaction.guild_id, member.id)  # type: ignore[arg-type]
        xp = row["xp"] if row else 0
        level, inside, need = level_from_xp(xp)
        embed = style.base("Мой профиль", member.mention, color=style.Colors.SECONDARY, emoji="📊", timestamp=False)
        embed.set_thumbnail(url=member.display_avatar.url)
        embed.add_field(name="Уровень", value=f"**{level}**")
        embed.add_field(name="Место", value=f"#{await self.cog.position(interaction.guild_id, 'xp', xp)}")  # type: ignore[arg-type]
        embed.add_field(name="Вафли", value=f"**{row['waffles'] if row else 0}** {WAFFLE}")
        embed.add_field(name=f"Опыт · {inside}/{need}", value=f"`{progress_bar(inside, need)}`", inline=False)
        embed.add_field(name="🎙️ В голосе", value=fmt_voice(row["voice_seconds"] if row else 0))
        embed.add_field(name="💬 Сообщений", value=str(row["messages"] if row else 0))
        await interaction.response.send_message(embed=embed, ephemeral=True)


class TopView(discord.ui.View):
    """Таблица лидеров: вкладки Опыт / Голос / Чат / Вафли и листание страниц."""

    def __init__(self, cog: LevelsCog, author_id: int, sort: str):
        super().__init__(timeout=600)
        self.cog, self.author_id, self.sort, self.page = cog, author_id, sort, 0
        self.pages = 1
        for key, (label, emoji, _, _) in SORTS.items():
            button = discord.ui.Button(label=label, emoji=emoji, row=0,
                                       style=discord.ButtonStyle.primary if key == sort else discord.ButtonStyle.secondary)
            button.callback = self._make_sort(key)
            self.add_item(button)
        for label, delta in (("«", "first"), ("‹", -1), ("›", 1), ("»", "last")):
            button = discord.ui.Button(label=label, style=discord.ButtonStyle.secondary, row=1)
            button.callback = self._make_page(delta)
            self.add_item(button)
        close = discord.ui.Button(emoji="✖️", style=discord.ButtonStyle.danger, row=1)
        close.callback = self._close
        self.add_item(close)

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if interaction.user.id != self.author_id:
            await interaction.response.send_message(embed=style.error("Откройте свой топ командой /top."),
                                                    ephemeral=True)
            return False
        return True

    async def render(self, guild: discord.Guild) -> discord.Embed:
        label, emoji, order, by = SORTS[self.sort]
        db = self.cog.bot.db
        total = await db.fetchval("SELECT COUNT(*) FROM levels WHERE guild_id=?", (guild.id,), 0)
        self.pages = max(1, -(-total // PAGE_SIZE))
        self.page = max(0, min(self.page, self.pages - 1))
        rows = await db.fetchall(f"SELECT * FROM levels WHERE guild_id=? ORDER BY {order}, user_id LIMIT ? OFFSET ?",
                                 (guild.id, PAGE_SIZE, self.page * PAGE_SIZE))
        medals = {1: "⭐", 2: "🌟", 3: "✨"}
        lines = []
        for i, r in enumerate(rows, start=self.page * PAGE_SIZE + 1):
            member = guild.get_member(r["user_id"])
            name = member.mention if member else f"<@{r['user_id']}>"
            lines.append(f"{medals.get(i, '')} **#{i}.** {name}\n"
                         f"Уровень: **{r['level']}** | Опыт: {r['xp']} | {WAFFLE} {r['waffles']} | "
                         f"🎙️ {fmt_voice(r['voice_seconds'])} | 💬 {r['messages']}")
        embed = style.base("Топ рейтинга участников", f"Отсортировано по {by} {emoji}\n\n" +
                           ("\n\n".join(lines) or "Пока никто не набрал опыт — общайтесь в чатах и голосовых!"),
                           color=style.Colors.SECONDARY, emoji="🏆", timestamp=False)
        if guild.icon:
            embed.set_thumbnail(url=guild.icon.url)
        embed.set_footer(text=f"Страница {self.page + 1} из {self.pages} — всего участников: {total}")
        return embed

    async def _refresh(self, interaction: discord.Interaction) -> None:
        for item in self.children:
            if isinstance(item, discord.ui.Button) and item.row == 0:
                item.style = discord.ButtonStyle.primary if item.label == SORTS[self.sort][0] \
                    else discord.ButtonStyle.secondary
        await interaction.response.edit_message(embed=await self.render(interaction.guild), view=self)  # type: ignore[arg-type]

    def _make_sort(self, key: str):
        async def callback(interaction: discord.Interaction) -> None:
            self.sort, self.page = key, 0
            await self._refresh(interaction)
        return callback

    def _make_page(self, delta):
        async def callback(interaction: discord.Interaction) -> None:
            if delta == "first":
                self.page = 0
            elif delta == "last":
                self.page = self.pages - 1
            else:
                self.page = (self.page + delta) % self.pages
            await self._refresh(interaction)
        return callback

    async def _close(self, interaction: discord.Interaction) -> None:
        self.stop()
        await interaction.response.edit_message(view=None)


async def setup(bot: "PolariBot") -> None:
    await bot.add_cog(LevelsCog(bot))
