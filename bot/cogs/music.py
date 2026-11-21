"""Музыка POLARI: много голосовых каналов одновременно через ботов-воркеров.

MUSIC_TOKENS — токены дополнительных ботов через запятую. Каждый бот = одна комната с музыкой.
Главный бот POLARI используется как запасной плеер, когда все воркеры заняты.
Источники: радиостанции, прямые ссылки на аудио и загруженные файлы.
"""
from __future__ import annotations

import asyncio
import ipaddress
import logging
import os
from typing import TYPE_CHECKING
from urllib.parse import urlparse

import discord
from discord import app_commands
from discord.ext import commands

from bot.embeds import style
from bot.music.player import GuildPlayer, MusicWorker, Track, ffmpeg_path, leave_if_alone, player_for
from bot.music.stations import STATION_BY_ID, STATIONS

if TYPE_CHECKING:
    from bot.core import PolariBot

log = logging.getLogger("polari.music")
WORKER_PERMISSIONS = 3146752  # Просмотр каналов, Подключаться, Говорить


def _safe_url(url: str) -> bool:
    """Только http(s) и не внутренние адреса сервера."""
    parsed = urlparse(url)
    if parsed.scheme not in ("http", "https") or not parsed.hostname:
        return False
    host = parsed.hostname.lower()
    if host in ("localhost",) or host.endswith(".local") or host.endswith(".internal"):
        return False
    try:
        ip = ipaddress.ip_address(host)
        return not (ip.is_private or ip.is_loopback or ip.is_link_local or ip.is_reserved)
    except ValueError:
        return True


class MusicCog(commands.Cog):
    def __init__(self, bot: "PolariBot"):
        self.bot = bot
        tokens = [t.strip() for t in os.getenv("MUSIC_TOKENS", "").split(",") if t.strip()]
        self.workers: list[MusicWorker] = [MusicWorker(i + 1) for i in range(len(tokens))]
        self._tokens = tokens
        self._tasks: list[asyncio.Task] = []

    async def cog_load(self) -> None:
        self.bot.add_dynamic_items(MusicButton)
        self.bot.add_view(RadioView(self))
        self.bot.get_cog("SetupCog").register_panel("music", self.post_panel)  # type: ignore[union-attr]
        for worker, token in zip(self.workers, self._tokens):
            self._tasks.append(asyncio.create_task(self._run_worker(worker, token)))
        if not ffmpeg_path():
            log.warning("ffmpeg не найден — музыка работать не будет.")

    async def _run_worker(self, worker: MusicWorker, token: str) -> None:
        try:
            await worker.start(token)
        except discord.LoginFailure:
            log.error("Музыкальный бот #%s: неверный токен в MUSIC_TOKENS.", worker.index)
        except Exception:
            log.exception("Музыкальный бот #%s остановился", worker.index)

    async def cog_unload(self) -> None:
        for worker in self.workers:
            await worker.close()
        for task in self._tasks:
            task.cancel()

    # ------------------------------------------------------------------ выбор бота
    def _all_clients(self) -> list[discord.Client]:
        return [w for w in self.workers if w.is_ready()] + [self.bot]

    def find_player(self, guild_id: int, channel_id: int) -> GuildPlayer | None:
        """Бот, который уже играет в этом канале."""
        for client in self._all_clients():
            player = player_for(client, guild_id)
            if player.channel_id == channel_id:
                return player
        return None

    def free_player(self, guild_id: int) -> GuildPlayer | None:
        """Свободный бот на этом сервере (не сидит ни в одном голосовом канале)."""
        for client in self._all_clients():
            if client.get_guild(guild_id) is None:
                continue
            player = player_for(client, guild_id)
            if player.voice is None or not player.voice.is_connected():
                return player
        return None

    async def _resolve(self, interaction: discord.Interaction, create: bool) -> GuildPlayer | None:
        member = interaction.user
        if not isinstance(member, discord.Member) or not member.voice or not member.voice.channel:
            await self._reply(interaction, style.error("Сначала зайдите в голосовой канал."))
            return None
        channel = member.voice.channel
        player = self.find_player(member.guild.id, channel.id)
        if player or not create:
            if not player:
                await self._reply(interaction, style.error("В вашем канале сейчас ничего не играет."))
            return player
        player = self.free_player(member.guild.id)
        if not player:
            await self._reply(interaction, style.error(
                "Все музыкальные боты заняты в других каналах. Добавьте ещё ботов (см. `/music_bots`)."))
            return None
        if not ffmpeg_path():
            await self._reply(interaction, style.error("На сервере бота не установлен ffmpeg."))
            return None
        try:
            await player.connect(channel.id)
        except (discord.HTTPException, discord.ClientException, asyncio.TimeoutError, RuntimeError) as exc:
            await self._reply(interaction, style.error(f"Не удалось подключиться к каналу: {exc}"))
            return None
        return player

    async def _reply(self, interaction: discord.Interaction, embed: discord.Embed,
                     view: discord.ui.View | None = None, ephemeral: bool = True) -> None:
        kwargs = {"embed": embed, "ephemeral": ephemeral}
        if view is not None:
            kwargs["view"] = view
        if interaction.response.is_done():
            await interaction.followup.send(**kwargs)
        else:
            await interaction.response.send_message(**kwargs)

    def now_playing(self, player: GuildPlayer) -> discord.Embed:
        track = player.current
        bot_name = player.client.user.name if player.client.user else "POLARI"
        if not track:
            return style.base("Музыка", "Сейчас ничего не играет.", emoji="🎵")
        embed = style.base("Сейчас играет", f"**{track.title}**", color=style.Colors.ACCENT, emoji="🎵")
        embed.add_field(name="Канал", value=f"<#{player.channel_id}>")
        embed.add_field(name="Бот", value=bot_name)
        embed.add_field(name="Громкость", value=f"{player.volume}%")
        embed.add_field(name="Включил", value=f"<@{track.requester_id}>")
        if player.queue:
            embed.add_field(name=f"Далее ({len(player.queue)})", inline=False,
                            value="\n".join(f"`{i + 1}.` {t.title}" for i, t in enumerate(list(player.queue)[:5])))
        return embed

    def controls(self, channel_id: int) -> discord.ui.View:
        view = discord.ui.View(timeout=None)
        for action, emoji, btn_style in (("pause", "⏯️", discord.ButtonStyle.secondary),
                                         ("skip", "⏭️", discord.ButtonStyle.secondary),
                                         ("voldown", "🔉", discord.ButtonStyle.secondary),
                                         ("volup", "🔊", discord.ButtonStyle.secondary),
                                         ("stop", "⏹️", discord.ButtonStyle.danger)):
            view.add_item(MusicButton(action, channel_id, emoji, btn_style))
        return view

    async def start_track(self, interaction: discord.Interaction, track: Track, *, replace: bool) -> None:
        player = await self._resolve(interaction, create=True)
        if not player:
            return
        try:
            started = await player.enqueue(track, now=replace)
        except Exception as exc:
            log.exception("Ошибка запуска музыки")
            await self._reply(interaction, style.error(f"Не удалось включить: {exc}"))
            return
        await asyncio.sleep(2)
        if started and not (player.voice and player.voice.is_playing() and player.current):
            await self._reply(interaction, style.error(
                "Источник не воспроизводится: ссылка недоступна или это не аудио. Попробуйте другую станцию или файл."))
            return
        if started:
            await self._reply(interaction, self.now_playing(player), view=self.controls(player.channel_id or 0),
                              ephemeral=False)
        else:
            await self._reply(interaction, style.base("Добавлено в очередь", (
                f"**{track.title}** · позиция **{len(player.queue)}**"), emoji="📥"), ephemeral=False)

    # ------------------------------------------------------------------ панель
    async def post_panel(self, channel: discord.TextChannel) -> discord.Message:
        stations = "\n".join(f"{s.emoji} **{s.name}** — {s.genre}" for s in STATIONS)
        embed = style.header("Музыка", (
            "Зайдите в любой голосовой канал и выберите радио ниже — бот придёт и включит музыку.\n"
            "В разных каналах одновременно может играть разная музыка: каждый канал занимает свой бот.\n\n"
            f"{stations}\n\n"
            "`/play` — радио или ссылка на аудио · `/play_file` — свой mp3\n"
            "`/skip` `/pause` `/resume` `/volume` `/queue` `/nowplaying` `/stop`"), style.Colors.SECONDARY)
        return await style.send_panel(channel, "music", [embed], view=RadioView(self))

    # ------------------------------------------------------------------ команды
    @app_commands.command(name="play", description="Включить радио или аудио по ссылке в вашем голосовом канале")
    @app_commands.describe(source="Радиостанция из списка или прямая ссылка на mp3/поток")
    @app_commands.guild_only()
    @app_commands.checks.cooldown(1, 3)
    async def play(self, interaction: discord.Interaction, source: str) -> None:
        await interaction.response.defer(thinking=True)
        station = STATION_BY_ID.get(source)
        if station:
            await self.start_track(interaction, Track(f"{station.emoji} {station.name}", station.url,
                                                      interaction.user.id, live=True), replace=True)
            return
        if not _safe_url(source):
            await self._reply(interaction, style.error("Укажите станцию из списка или ссылку http(s) на аудио."))
            return
        title = urlparse(source).path.rsplit("/", 1)[-1] or source
        await self.start_track(interaction, Track(f"🎶 {title[:80]}", source, interaction.user.id), replace=False)

    @play.autocomplete("source")
    async def _play_ac(self, interaction: discord.Interaction, current: str):
        return [app_commands.Choice(name=f"{s.emoji} {s.name} — {s.genre}", value=s.id) for s in STATIONS
                if current.lower() in f"{s.name} {s.genre}".lower()][:25]

    @app_commands.command(name="play_file", description="Включить свой аудиофайл (mp3, ogg, wav, m4a)")
    @app_commands.guild_only()
    async def play_file(self, interaction: discord.Interaction, file: discord.Attachment) -> None:
        if not (file.content_type or "").startswith("audio") and not file.filename.lower().endswith(
                (".mp3", ".ogg", ".wav", ".m4a", ".flac", ".opus")):
            await interaction.response.send_message(embed=style.error("Это не аудиофайл."), ephemeral=True)
            return
        await interaction.response.defer(thinking=True)
        await self.start_track(interaction, Track(f"🎶 {file.filename[:80]}", file.url, interaction.user.id),
                               replace=False)

    async def _control(self, interaction: discord.Interaction, action: str, value: int | None = None) -> None:
        player = await self._resolve(interaction, create=False)
        if not player:
            return
        vc = player.voice
        text = ""
        if action == "skip":
            if player.current and player.current.live and not player.queue:
                text = "Радио нельзя пропустить — выберите другую станцию или нажмите стоп."
            elif vc:
                vc.stop()
                text = "Трек пропущен ⏭️"
        elif action == "pause" and vc:
            if vc.is_paused():
                vc.resume()
                text = "Продолжаем ▶️"
            else:
                vc.pause()
                text = "Пауза ⏸️"
        elif action == "resume" and vc:
            vc.resume()
            text = "Продолжаем ▶️"
        elif action == "stop":
            await player.stop()
            text = "Музыка остановлена, бот вышел из канала ⏹️"
        elif action in ("volume", "volup", "voldown"):
            target = value if action == "volume" else player.volume + (15 if action == "volup" else -15)
            await player.set_volume(int(target or 60))
            text = f"Громкость: **{player.volume}%**"
        await self._reply(interaction, style.base("Музыка", text or "Готово", emoji="🎵"),
                          ephemeral=action not in ("stop",))

    @app_commands.command(name="skip", description="Пропустить трек")
    @app_commands.guild_only()
    async def skip(self, interaction: discord.Interaction) -> None:
        await self._control(interaction, "skip")

    @app_commands.command(name="pause", description="Пауза / продолжить")
    @app_commands.guild_only()
    async def pause(self, interaction: discord.Interaction) -> None:
        await self._control(interaction, "pause")

    @app_commands.command(name="resume", description="Продолжить воспроизведение")
    @app_commands.guild_only()
    async def resume(self, interaction: discord.Interaction) -> None:
        await self._control(interaction, "resume")

    @app_commands.command(name="stop", description="Остановить музыку в вашем канале")
    @app_commands.guild_only()
    async def stop(self, interaction: discord.Interaction) -> None:
        await self._control(interaction, "stop")

    @app_commands.command(name="volume", description="Громкость 5–150%")
    @app_commands.guild_only()
    async def volume(self, interaction: discord.Interaction, percent: app_commands.Range[int, 5, 150]) -> None:
        await self._control(interaction, "volume", percent)

    @app_commands.command(name="nowplaying", description="Что сейчас играет в вашем канале")
    @app_commands.guild_only()
    async def nowplaying(self, interaction: discord.Interaction) -> None:
        player = await self._resolve(interaction, create=False)
        if player:
            await self._reply(interaction, self.now_playing(player), view=self.controls(player.channel_id or 0))

    @app_commands.command(name="queue", description="Очередь треков в вашем канале")
    @app_commands.guild_only()
    async def queue_cmd(self, interaction: discord.Interaction) -> None:
        player = await self._resolve(interaction, create=False)
        if player:
            await self._reply(interaction, self.now_playing(player))

    @app_commands.command(name="music_bots", description="Музыкальные боты: где играют и как добавить новых")
    @app_commands.guild_only()
    async def music_bots(self, interaction: discord.Interaction) -> None:
        guild = interaction.guild
        assert guild
        lines = []
        for client in [*self.workers, self.bot]:
            name = client.user.name if client.user else "не запущен"
            if client is not self.bot and not client.is_ready():
                lines.append(f"⚫ **{name}** — не подключён (проверьте токен)")
                continue
            if client.get_guild(guild.id) is None and client.user:
                url = (f"https://discord.com/oauth2/authorize?client_id={client.user.id}"
                       f"&permissions={WORKER_PERMISSIONS}&scope=bot")
                lines.append(f"🟡 **{name}** — не на сервере · [пригласить]({url})")
                continue
            player = player_for(client, guild.id)
            where = f"<#{player.channel_id}> · {player.current.title if player.current else 'тишина'}" \
                if player.channel_id else "свободен"
            lines.append(f"{'🟢' if not player.channel_id else '🎵'} **{name}** — {where}")
        embed = style.base("Музыкальные боты", "\n".join(lines), emoji="🎛️")
        embed.add_field(name="Одновременно каналов", value=str(len(self.workers) + 1))
        embed.add_field(name="ffmpeg", value="✅" if ffmpeg_path() else "❌ не установлен")
        await interaction.response.send_message(embed=embed, ephemeral=True)

    @commands.Cog.listener()
    async def on_voice_state_update(self, member: discord.Member, before: discord.VoiceState,
                                    after: discord.VoiceState) -> None:
        if member.guild.voice_client:
            await leave_if_alone(self.bot, member.guild)


class RadioSelect(discord.ui.Select):
    def __init__(self, cog: MusicCog):
        super().__init__(placeholder="📻 Выберите радиостанцию", custom_id="polari:mu:radio", options=[
            discord.SelectOption(label=s.name, value=s.id, emoji=s.emoji, description=s.genre) for s in STATIONS])
        self.cog = cog

    async def callback(self, interaction: discord.Interaction) -> None:
        station = STATION_BY_ID[self.values[0]]
        await interaction.response.defer(ephemeral=True, thinking=True)
        await self.cog.start_track(interaction, Track(f"{station.emoji} {station.name}", station.url,
                                                      interaction.user.id, live=True), replace=True)


class RadioView(discord.ui.View):
    def __init__(self, cog: MusicCog):
        super().__init__(timeout=None)
        self.add_item(RadioSelect(cog))


class MusicButton(discord.ui.DynamicItem[discord.ui.Button],
                  template=r"polari:mu:(?P<action>pause|skip|stop|volup|voldown):(?P<channel>\d+)"):
    """Кнопки управления под сообщением «Сейчас играет». Работают после перезапуска."""

    def __init__(self, action: str, channel_id: int, emoji: str = "🎵",
                 btn_style: discord.ButtonStyle = discord.ButtonStyle.secondary):
        super().__init__(discord.ui.Button(emoji=emoji, style=btn_style,
                                           custom_id=f"polari:mu:{action}:{channel_id}"))
        self.action, self.channel_id = action, channel_id

    @classmethod
    async def from_custom_id(cls, interaction, item, match):  # type: ignore[override]
        return cls(match["action"], int(match["channel"]))

    async def callback(self, interaction: discord.Interaction) -> None:  # type: ignore[override]
        cog: MusicCog = interaction.client.get_cog("MusicCog")  # type: ignore[assignment]
        member = interaction.user
        if not isinstance(member, discord.Member) or not member.voice or member.voice.channel is None \
                or member.voice.channel.id != self.channel_id:
            await interaction.response.send_message(embed=style.error("Управлять музыкой можно, находясь в этом "
                                                                      "голосовом канале."), ephemeral=True)
            return
        await cog._control(interaction, self.action)


async def setup(bot: "PolariBot") -> None:
    await bot.add_cog(MusicCog(bot))
