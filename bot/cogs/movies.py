"""Киновечера POLARI.

По выбранным дням (по умолчанию суббота и воскресенье) бот сам:
  1) открывает голосование за фильм из списка сервера (выбор в меню, один голос, можно переголосовать);
  2) закрывает голосование, объявляет победителя и создаёт событие сервера в кинозале;
  3) в назначенное время запускает событие и зовёт всех в кинозал; через 3 часа завершает его.

Сам фильм показывает ведущий через демонстрацию экрана — боты Discord не умеют транслировать видео.
"""
from __future__ import annotations

import json
import logging
import random
from datetime import datetime, time, timedelta
from typing import TYPE_CHECKING
from zoneinfo import ZoneInfo

import discord
from discord import app_commands
from discord.ext import commands, tasks

from bot.database.db import now_iso
from bot.embeds import style
from bot.utils.checks import require

if TYPE_CHECKING:
    from bot.core import PolariBot

log = logging.getLogger("polari.movies")
DAY_NAMES = ["понедельник", "вторник", "среда", "четверг", "пятница", "суббота", "воскресенье"]
DEFAULTS = {"movie.days": "5,6", "movie.poll_open": "12:00", "movie.poll_close": "19:00",
            "movie.start": "20:00", "movie.tz": "Europe/Moscow", "movie.options": "5"}
SHOW_HOURS = 3
STARTER_MOVIES = [
    ("Интерстеллар", 2014, "Фантастика"), ("Начало", 2010, "Фантастика"), ("Зелёная миля", 1999, "Драма"),
    ("Остров проклятых", 2010, "Триллер"), ("Джентльмены", 2019, "Криминал"), ("Достать ножи", 2019, "Детектив"),
    ("Дюна", 2021, "Фантастика"), ("Бойцовский клуб", 1999, "Триллер"), ("1+1", 2011, "Комедия"),
    ("Головоломка", 2015, "Мультфильм"), ("Паразиты", 2019, "Триллер"), ("Брат", 1997, "Криминал"),
    ("Остров", 2006, "Драма"), ("Тихое место", 2018, "Ужасы"), ("Мальчишник в Вегасе", 2009, "Комедия"),
    ("Отступники", 2006, "Криминал"), ("Ла-Ла Ленд", 2016, "Мюзикл"), ("Пункт назначения", 2000, "Ужасы"),
]


def _hm(text: str) -> time:
    hours, minutes = text.split(":")
    return time(int(hours), int(minutes))


class MoviesCog(commands.Cog):
    def __init__(self, bot: "PolariBot"):
        self.bot = bot

    async def cog_load(self) -> None:
        self.bot.add_dynamic_items(MovieVoteSelect)
        self.bot.get_cog("SetupCog").register_panel("movies", self.post_panel)  # type: ignore[union-attr]
        self.scheduler.start()

    async def cog_unload(self) -> None:
        self.scheduler.cancel()

    # ------------------------------------------------------------------ настройки
    async def cfg(self, guild_id: int, key: str) -> str:
        return await self.bot.registry.get(guild_id, key) or DEFAULTS[key]

    async def tz(self, guild_id: int) -> ZoneInfo:
        try:
            return ZoneInfo(await self.cfg(guild_id, "movie.tz"))
        except Exception:
            return ZoneInfo("Europe/Moscow")

    async def ensure_pool(self, guild_id: int) -> None:
        if await self.bot.db.fetchval("SELECT COUNT(*) FROM movies WHERE guild_id=?", (guild_id,), 0):
            return
        for title, year, genre in STARTER_MOVIES:
            await self.bot.db.execute(
                "INSERT INTO movies (guild_id,title,year,genre,created_at) VALUES (?,?,?,?,?)",
                (guild_id, title, year, genre, now_iso()))

    # ------------------------------------------------------------------ голосование
    def poll_embed(self, poll, movies: list, counts: dict[int, int], tz: ZoneInfo) -> discord.Embed:
        closes = datetime.fromisoformat(poll["closes_at"])
        starts = datetime.fromisoformat(poll["starts_at"])
        total = sum(counts.values())
        lines = []
        for m in movies:
            votes = counts.get(m["id"], 0)
            bar = "▰" * round(10 * votes / total) + "▱" * (10 - round(10 * votes / total)) if total else "▱" * 10
            lines.append(f"🎬 **{m['title']}** ({m['year'] or '—'}, {m['genre'] or 'кино'})\n`{bar}` {votes}")
        status = {"open": f"Голосование до {style.ts(closes, 't')} ({style.ts(closes, 'R')})",
                  "closed": "Голосование завершено", "started": "Показ идёт 🍿", "finished": "Показ завершён"}
        embed = style.header("Киновечер — выбираем фильм", (
            f"**{DAY_NAMES[starts.astimezone(tz).weekday()].capitalize()}**, показ в {style.ts(starts, 't')} "
            f"в кинозале.\n{status[poll['status']]}\n\n" + "\n\n".join(lines)), style.Colors.ACCENT)
        embed.set_footer(text=f"Голосов: {total} · можно переголосовать")
        return embed

    async def poll_movies(self, poll) -> list:
        ids = json.loads(poll["options"])
        rows = await self.bot.db.fetchall(f"SELECT * FROM movies WHERE id IN ({','.join('?' * len(ids))})", ids)
        order = {mid: i for i, mid in enumerate(ids)}
        return sorted(rows, key=lambda r: order[r["id"]])

    async def counts(self, poll_id: int) -> dict[int, int]:
        rows = await self.bot.db.fetchall("SELECT movie_id, COUNT(*) c FROM movie_votes WHERE poll_id=? GROUP BY movie_id",
                                          (poll_id,))
        return {r["movie_id"]: r["c"] for r in rows}

    async def refresh_poll(self, guild: discord.Guild, poll_id: int, with_view: bool = True) -> None:
        poll = await self.bot.db.fetchone("SELECT * FROM movie_polls WHERE id=?", (poll_id,))
        channel = guild.get_channel(poll["channel_id"]) if poll else None
        if not poll or not isinstance(channel, discord.TextChannel) or not poll["message_id"]:
            return
        movies = await self.poll_movies(poll)
        embed = self.poll_embed(poll, movies, await self.counts(poll_id), await self.tz(guild.id))
        view = vote_view(poll_id, movies) if with_view and poll["status"] == "open" else None
        try:
            msg = await channel.fetch_message(poll["message_id"])
            await msg.edit(embed=embed, view=view)
        except discord.HTTPException as exc:
            log.warning("Не удалось обновить голосование %s: %s", poll_id, exc)

    async def open_poll(self, guild: discord.Guild, closes_at: datetime, starts_at: datetime, day: str) -> int | None:
        channel = await self.bot.registry.text(guild, "movies")
        if not channel:
            return None
        await self.ensure_pool(guild.id)
        count = int(await self.cfg(guild.id, "movie.options"))
        # сначала фильмы, которые ещё не смотрели
        pool = await self.bot.db.fetchall("SELECT * FROM movies WHERE guild_id=? ORDER BY watched_at IS NOT NULL, "
                                          "watched_at", (guild.id,))
        fresh = [m for m in pool if not m["watched_at"]] or pool
        options = random.sample(fresh, min(count, len(fresh)))
        poll_id = await self.bot.db.execute(
            "INSERT INTO movie_polls (guild_id,day,channel_id,options,closes_at,starts_at,created_at) VALUES (?,?,?,?,?,?,?)",
            (guild.id, day, channel.id, json.dumps([m["id"] for m in options]), closes_at.isoformat(),
             starts_at.isoformat(), now_iso()))
        poll = await self.bot.db.fetchone("SELECT * FROM movie_polls WHERE id=?", (poll_id,))
        member_role = await self.bot.registry.role(guild, "member")
        msg = await style.send_panel(
            channel, "movies", [self.poll_embed(poll, options, {}, await self.tz(guild.id))],
            view=vote_view(poll_id, options), content=member_role.mention if member_role else None,
            allowed_mentions=discord.AllowedMentions(roles=True))
        await self.bot.db.execute("UPDATE movie_polls SET message_id=? WHERE id=?", (msg.id, poll_id))
        return poll_id

    async def close_poll(self, guild: discord.Guild, poll) -> None:
        counts = await self.counts(poll["id"])
        movies = await self.poll_movies(poll)
        if not movies:
            await self.bot.db.execute("UPDATE movie_polls SET status='finished' WHERE id=?", (poll["id"],))
            return
        best = max(counts.values(), default=0)
        winner = random.choice([m for m in movies if counts.get(m["id"], 0) == best])
        event_id = None
        voice = await self.bot.registry.channel(guild, "cinema")
        starts = datetime.fromisoformat(poll["starts_at"])
        if isinstance(voice, (discord.VoiceChannel, discord.StageChannel)) and starts > discord.utils.utcnow():
            try:
                event = await guild.create_scheduled_event(
                    name=f"🎬 {winner['title']}"[:100], start_time=starts, end_time=starts + timedelta(hours=SHOW_HOURS),
                    channel=voice, entity_type=discord.EntityType.voice, privacy_level=discord.PrivacyLevel.guild_only,
                    description=f"Киновечер POLARI. Фильм выбран голосованием ({best} голосов).")
                event_id = event.id
            except discord.HTTPException as exc:
                log.warning("Событие киновечера не создано: %s", exc)
        await self.bot.db.execute("UPDATE movie_polls SET status='closed', winner_id=?, event_id=? WHERE id=?",
                                  (winner["id"], event_id, poll["id"]))
        await self.refresh_poll(guild, poll["id"], with_view=False)
        channel = guild.get_channel(poll["channel_id"])
        if isinstance(channel, discord.TextChannel):
            embed = style.header("Фильм выбран", (
                f"🏆 **{winner['title']}** ({winner['year'] or '—'}, {winner['genre'] or 'кино'}) — голосов: **{best}**\n\n"
                f"Показ {style.ts(starts, 'F')} ({style.ts(starts, 'R')})"
                + (f" в {voice.mention}" if voice else "") + ".\nНажмите «Интересно» в событии сервера, "
                "чтобы получить напоминание."), style.Colors.SUCCESS)
            await channel.send(embed=embed)

    async def start_show(self, guild: discord.Guild, poll) -> None:
        movie = await self.bot.db.fetchone("SELECT * FROM movies WHERE id=?", (poll["winner_id"],))
        await self.bot.db.execute("UPDATE movie_polls SET status='started' WHERE id=?", (poll["id"],))
        await self.bot.db.execute("UPDATE movies SET watched_at=? WHERE id=?", (now_iso(), poll["winner_id"]))
        if poll["event_id"]:
            try:
                event = await guild.fetch_scheduled_event(poll["event_id"])
                if event.status == discord.EventStatus.scheduled:
                    await event.start()
            except discord.HTTPException as exc:
                log.warning("Не удалось запустить событие: %s", exc)
        channel = guild.get_channel(poll["channel_id"])
        voice = await self.bot.registry.channel(guild, "cinema")
        member_role = await self.bot.registry.role(guild, "member")
        if isinstance(channel, discord.TextChannel) and movie:
            await style.send_panel(channel, "movies", [style.header("Киновечер начинается", (
                f"🍿 Смотрим **{movie['title']}**!\nЗаходите в {voice.mention if voice else 'кинозал'} — "
                "ведущий включает демонстрацию экрана."), style.Colors.ACCENT)],
                content=member_role.mention if member_role else None,
                allowed_mentions=discord.AllowedMentions(roles=True))

    async def finish_show(self, guild: discord.Guild, poll) -> None:
        await self.bot.db.execute("UPDATE movie_polls SET status='finished' WHERE id=?", (poll["id"],))
        if poll["event_id"]:
            try:
                event = await guild.fetch_scheduled_event(poll["event_id"])
                if event.status == discord.EventStatus.active:
                    await event.end()
            except discord.HTTPException:
                pass

    # ------------------------------------------------------------------ расписание
    @tasks.loop(minutes=1)
    async def scheduler(self) -> None:
        for guild in self.bot.guilds:
            try:
                await self._tick(guild)
            except Exception:
                log.exception("Ошибка расписания киновечеров на %s", guild.id)

    async def _tick(self, guild: discord.Guild) -> None:
        if not await self.bot.registry.get(guild.id, "channel.movies"):
            return
        tz = await self.tz(guild.id)
        now = datetime.now(tz)
        db = self.bot.db
        # продвигаем существующие голосования
        for poll in await db.fetchall("SELECT * FROM movie_polls WHERE guild_id=? AND status IN ('open','closed','started')",
                                      (guild.id,)):
            closes, starts = datetime.fromisoformat(poll["closes_at"]), datetime.fromisoformat(poll["starts_at"])
            if poll["status"] == "open" and now >= closes:
                await self.close_poll(guild, poll)
            elif poll["status"] == "closed" and now >= starts:
                await self.start_show(guild, poll)
            elif poll["status"] == "started" and now >= starts + timedelta(hours=SHOW_HOURS):
                await self.finish_show(guild, poll)
        # открываем новое по расписанию
        days = {int(d) for d in (await self.cfg(guild.id, "movie.days")).split(",") if d.strip().isdigit()}
        if now.weekday() not in days:
            return
        today = now.date().isoformat()
        if await db.fetchval("SELECT COUNT(*) FROM movie_polls WHERE guild_id=? AND day=?", (guild.id, today), 0):
            return
        open_at = datetime.combine(now.date(), _hm(await self.cfg(guild.id, "movie.poll_open")), tz)
        close_at = datetime.combine(now.date(), _hm(await self.cfg(guild.id, "movie.poll_close")), tz)
        start_at = datetime.combine(now.date(), _hm(await self.cfg(guild.id, "movie.start")), tz)
        if open_at <= now < close_at:
            await self.open_poll(guild, close_at, start_at, today)

    @scheduler.before_loop
    async def _ready(self) -> None:
        await self.bot.wait_until_ready()

    # ------------------------------------------------------------------ панель и команды
    async def post_panel(self, channel: discord.TextChannel) -> discord.Message:
        guild_id = channel.guild.id
        days = ", ".join(DAY_NAMES[int(d)] for d in (await self.cfg(guild_id, "movie.days")).split(","))
        embed = style.header("Киновечера", (
            f"Каждую **{days}** смотрим кино вместе в кинозале 🍿\n\n"
            f"**{await self.cfg(guild_id, 'movie.poll_open')}** — здесь открывается голосование за фильм\n"
            f"**{await self.cfg(guild_id, 'movie.poll_close')}** — голосование закрывается, бот объявляет победителя "
            "и создаёт событие сервера\n"
            f"**{await self.cfg(guild_id, 'movie.start')}** — начало показа в кинозале\n\n"
            "Время — московское. Хотите свой фильм в списке? Предложите его через `/suggest`."),
            style.Colors.SECONDARY)
        return await style.send_panel(channel, "movies", [embed])

    movie = app_commands.Group(name="movie", description="Киновечера", guild_only=True)

    @movie.command(name="add", description="Добавить фильм в список для голосований")
    @require("level:40")
    async def movie_add(self, interaction: discord.Interaction, title: app_commands.Range[str, 1, 100],
                        year: app_commands.Range[int, 1900, 2100] | None = None,
                        genre: app_commands.Range[str, 0, 40] = "") -> None:
        await self.ensure_pool(interaction.guild_id)  # type: ignore[arg-type]
        await self.bot.db.execute("INSERT INTO movies (guild_id,title,year,genre,added_by,created_at) VALUES (?,?,?,?,?,?)",
                                  (interaction.guild_id, title, year, genre or None, interaction.user.id, now_iso()))
        await interaction.response.send_message(embed=style.success("Фильм добавлен", f"🎬 **{title}**"), ephemeral=True)

    @movie.command(name="remove", description="Убрать фильм из списка")
    @require("level:40")
    async def movie_remove(self, interaction: discord.Interaction, movie_id: int) -> None:
        row = await self.bot.db.fetchone("SELECT title FROM movies WHERE id=? AND guild_id=?", (movie_id, interaction.guild_id))
        if not row:
            await interaction.response.send_message(embed=style.error("Фильм с таким номером не найден."), ephemeral=True)
            return
        await self.bot.db.execute("DELETE FROM movies WHERE id=?", (movie_id,))
        await interaction.response.send_message(embed=style.success("Фильм удалён", row["title"]), ephemeral=True)

    @movie.command(name="list", description="Список фильмов для голосований")
    async def movie_list(self, interaction: discord.Interaction) -> None:
        await self.ensure_pool(interaction.guild_id)  # type: ignore[arg-type]
        rows = await self.bot.db.fetchall("SELECT * FROM movies WHERE guild_id=? ORDER BY watched_at IS NOT NULL, title",
                                          (interaction.guild_id,))
        text = "\n".join(f"`#{r['id']}` {'✅' if r['watched_at'] else '🎬'} **{r['title']}**"
                         f" ({r['year'] or '—'}, {r['genre'] or 'кино'})" for r in rows[:60])
        await interaction.response.send_message(embed=style.base("Фильмы", text or "Список пуст.", emoji="🎬"),
                                                ephemeral=True)

    @movie.command(name="poll_now", description="Запустить голосование прямо сейчас")
    @app_commands.describe(vote_minutes="Сколько минут идёт голосование", start_after="Через сколько минут после "
                                                                                       "голосования показ")
    @require("level:40")
    async def movie_poll_now(self, interaction: discord.Interaction, vote_minutes: app_commands.Range[int, 1, 1440] = 60,
                             start_after: app_commands.Range[int, 1, 1440] = 30) -> None:
        guild = interaction.guild
        assert guild
        await interaction.response.defer(ephemeral=True)
        now = datetime.now(await self.tz(guild.id))
        close_at = now + timedelta(minutes=vote_minutes)
        poll_id = await self.open_poll(guild, close_at, close_at + timedelta(minutes=start_after),
                                       f"manual-{now:%Y%m%d%H%M}")
        await interaction.followup.send(embed=style.success("Голосование открыто", f"Номер #{poll_id}") if poll_id
                                        else style.error("Канал кино не настроен — выполните /setup."), ephemeral=True)

    @movie.command(name="schedule", description="Настроить дни и время киновечеров")
    @app_commands.describe(days="Дни через запятую: 0=пн … 5=сб, 6=вс", poll_open="Открытие голосования, ЧЧ:ММ",
                           poll_close="Закрытие голосования, ЧЧ:ММ", start="Начало фильма, ЧЧ:ММ",
                           options="Сколько фильмов в голосовании (2–10)")
    @require("admin")
    async def movie_schedule(self, interaction: discord.Interaction, days: str | None = None,
                             poll_open: str | None = None, poll_close: str | None = None, start: str | None = None,
                             options: app_commands.Range[int, 2, 10] | None = None) -> None:
        updates = {"movie.days": days, "movie.poll_open": poll_open, "movie.poll_close": poll_close,
                   "movie.start": start, "movie.options": str(options) if options else None}
        try:
            for key, value in updates.items():
                if value is None:
                    continue
                if key == "movie.days":
                    parts = [int(p) for p in value.split(",")]
                    assert all(0 <= p <= 6 for p in parts)
                elif key != "movie.options":
                    _hm(value)
        except (ValueError, AssertionError):
            await interaction.response.send_message(embed=style.error("Неверный формат. Дни: 5,6 · время: 19:30."),
                                                    ephemeral=True)
            return
        for key, value in updates.items():
            if value is not None:
                await self.bot.registry.set(interaction.guild_id, key, value)  # type: ignore[arg-type]
        gid = interaction.guild_id
        text = (f"Дни: **{', '.join(DAY_NAMES[int(d)] for d in (await self.cfg(gid, 'movie.days')).split(','))}**\n"
                f"Голосование: **{await self.cfg(gid, 'movie.poll_open')}–{await self.cfg(gid, 'movie.poll_close')}**\n"
                f"Показ: **{await self.cfg(gid, 'movie.start')}** · фильмов в голосовании: "
                f"**{await self.cfg(gid, 'movie.options')}**")
        await interaction.response.send_message(embed=style.success("Расписание киновечеров", text), ephemeral=True)


def vote_view(poll_id: int, movies: list) -> discord.ui.View:
    view = discord.ui.View(timeout=None)
    view.add_item(MovieVoteSelect(poll_id, [(m["id"], m["title"], m["genre"]) for m in movies]))
    return view


class MovieVoteSelect(discord.ui.DynamicItem[discord.ui.Select], template=r"polari:movie:(?P<poll>\d+)"):
    """Меню голосования. Варианты восстанавливаются из БД — работает после перезапуска."""

    def __init__(self, poll_id: int, movies: list[tuple[int, str, str | None]]):
        options = [discord.SelectOption(label=title[:100], value=str(mid), emoji="🎬", description=(genre or None))
                   for mid, title, genre in movies] or [discord.SelectOption(label="—", value="0")]
        super().__init__(discord.ui.Select(placeholder="🍿 Голосуйте за фильм", options=options,
                                           custom_id=f"polari:movie:{poll_id}"))
        self.poll_id = poll_id

    @classmethod
    async def from_custom_id(cls, interaction, item, match):  # type: ignore[override]
        poll_id = int(match["poll"])
        cog: MoviesCog = interaction.client.get_cog("MoviesCog")
        poll = await interaction.client.db.fetchone("SELECT * FROM movie_polls WHERE id=?", (poll_id,))
        movies = await cog.poll_movies(poll) if poll else []
        return cls(poll_id, [(m["id"], m["title"], m["genre"]) for m in movies])

    async def callback(self, interaction: discord.Interaction) -> None:  # type: ignore[override]
        bot = interaction.client
        poll = await bot.db.fetchone("SELECT * FROM movie_polls WHERE id=?", (self.poll_id,))  # type: ignore[attr-defined]
        if not poll or poll["status"] != "open":
            await interaction.response.send_message(embed=style.error("Голосование уже закрыто."), ephemeral=True)
            return
        movie_id = int(self.item.values[0])
        if movie_id not in json.loads(poll["options"]):
            await interaction.response.send_message(embed=style.error("Такого варианта нет."), ephemeral=True)
            return
        await bot.db.execute("INSERT INTO movie_votes VALUES (?,?,?) ON CONFLICT(poll_id,user_id) "  # type: ignore[attr-defined]
                             "DO UPDATE SET movie_id=excluded.movie_id", (self.poll_id, interaction.user.id, movie_id))
        title = next((o.label for o in self.item.options if o.value == str(movie_id)), "фильм")
        await interaction.response.send_message(embed=style.success("Голос учтён", f"Вы выбрали **{title}** 🍿"),
                                                ephemeral=True)
        await bot.get_cog("MoviesCog").refresh_poll(interaction.guild, self.poll_id)  # type: ignore[union-attr]


async def setup(bot: "PolariBot") -> None:
    await bot.add_cog(MoviesCog(bot))
