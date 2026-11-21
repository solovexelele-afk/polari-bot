"""Музыкальные воркеры и плеер.

Discord разрешает одному боту быть только в одном голосовом канале сервера. Поэтому каждая
одновременная «комната с музыкой» обслуживается отдельным ботом-воркером (MUSIC_TOKENS).
Главный бот POLARI тоже может играть — как последний свободный воркер.
"""
from __future__ import annotations

import asyncio
import logging
import os
import shutil
import time
import urllib.request
from collections import deque
from dataclasses import dataclass, field
from pathlib import Path

import discord

log = logging.getLogger("polari.music")
BASE_DIR = Path(__file__).resolve().parents[2]
USER_AGENT = "Mozilla/5.0 (POLARI Discord bot)"
IDLE_LEAVE_SECONDS = 60


def ffmpeg_path() -> str | None:
    """ffmpeg: переменная FFMPEG_PATH, локальный bin/ffmpeg (ставится на Render при сборке) или системный."""
    for candidate in (os.getenv("FFMPEG_PATH"), str(BASE_DIR / "bin" / "ffmpeg"), shutil.which("ffmpeg")):
        if candidate and Path(candidate).exists():
            return candidate
    return None


class _FFmpegLog:
    """Перенаправляет stderr ffmpeg в лог бота (видно в логах Render)."""

    def write(self, data: bytes) -> int:
        text = data.decode(errors="ignore").strip() if isinstance(data, bytes) else str(data).strip()
        if text:
            log.warning("ffmpeg: %s", text[:300])
        return len(data)

    def flush(self) -> None:
        pass

    def fileno(self) -> int:
        raise OSError("not a real file")


@dataclass
class Track:
    title: str
    url: str
    requester_id: int
    live: bool = False          # радио: нельзя перематывать, не кончается само


@dataclass
class GuildPlayer:
    client: discord.Client
    guild_id: int
    queue: deque[Track] = field(default_factory=deque)
    current: Track | None = None
    volume: int = 60            # проценты
    started_at: float = 0.0
    offset: float = 0.0         # для перезапуска с места при смене громкости
    _stream: object | None = None
    _skip_after: int = 0         # сколько ближайших after-колбэков игнорировать (ручной stop)

    @property
    def voice(self) -> discord.VoiceClient | None:
        guild = self.client.get_guild(self.guild_id)
        vc = guild.voice_client if guild else None
        return vc if isinstance(vc, discord.VoiceClient) else None

    @property
    def channel_id(self) -> int | None:
        vc = self.voice
        return vc.channel.id if vc and vc.channel else None

    def elapsed(self) -> float:
        return self.offset + (time.monotonic() - self.started_at if self.started_at else 0)

    async def connect(self, channel_id: int) -> discord.VoiceClient:
        channel = self.client.get_channel(channel_id)
        if not isinstance(channel, (discord.VoiceChannel, discord.StageChannel)):
            raise RuntimeError("Музыкальный бот не видит этот голосовой канал — проверьте его права.")
        vc = self.voice
        if vc and vc.is_connected():
            if vc.channel.id != channel_id:
                await vc.move_to(channel)
            return vc
        return await channel.connect(self_deaf=True, timeout=20, reconnect=True)

    @staticmethod
    def _open(url: str):
        """Открывает поток в Python. ffmpeg получает данные через stdin, а не по сети:
        статическая сборка ffmpeg на Render падает (SIGSEGV) при сетевых запросах."""
        request = urllib.request.Request(url, headers={"User-Agent": USER_AGENT, "Icy-MetaData": "0"})
        return urllib.request.urlopen(request, timeout=15)

    def _close_stream(self) -> None:
        stream, self._stream = self._stream, None
        if stream is not None:
            try:
                stream.close()  # type: ignore[attr-defined]
            except Exception:
                pass

    async def _start(self, track: Track, seek: float = 0.0) -> None:
        vc = self.voice
        if not vc:
            return
        exe = ffmpeg_path()
        if not exe:
            raise RuntimeError("ffmpeg не установлен на сервере бота.")
        stream = await asyncio.to_thread(self._open, track.url)
        self._close_stream()
        self._stream = stream
        before = "-hide_banner -loglevel warning" + (f" -ss {seek:.1f}" if seek and not track.live else "")
        source = discord.FFmpegOpusAudio(stream, pipe=True, executable=exe, before_options=before or None,
                                         options=f"-vn -filter:a volume={self.volume / 100:.2f}",
                                         stderr=_FFmpegLog())
        loop = self.client.loop
        self.current, self.offset, self.started_at = track, seek, time.monotonic()
        vc.play(source, after=lambda err: loop.call_soon_threadsafe(self._after, err))
        log.info("▶ %s · канал %s · бот %s", track.title, vc.channel.id if vc.channel else "?", self.client.user)
        loop.create_task(self._watch(vc, track))

    async def _watch(self, vc: discord.VoiceClient, track: Track) -> None:
        """Диагностика: через 6 секунд пишет в лог, отправляются ли аудиопакеты."""
        await asyncio.sleep(6)
        player = getattr(vc, "_player", None)
        log.info("Диагностика «%s»: playing=%s, пакетов=%s, connected=%s, self_mute=%s",
                 track.title, vc.is_playing(), getattr(player, "loops", "?"), vc.is_connected(),
                 vc.guild.me.voice.self_mute if vc.guild.me.voice else "?")

    def _after(self, error: Exception | None) -> None:
        if error:
            log.warning("Ошибка воспроизведения: %s", error)
        if self._skip_after > 0:
            self._skip_after -= 1
            return
        self.client.loop.create_task(self.play_next())

    async def play_next(self) -> None:
        while self.queue:
            track = self.queue.popleft()
            try:
                await self._start(track)
            except Exception as exc:
                log.warning("Не удалось запустить «%s»: %s", track.title, exc)
                continue
            await self.refresh_presence()
            return
        self._close_stream()
        self.current, self.started_at, self.offset = None, 0.0, 0.0
        await self.refresh_presence()

    async def enqueue(self, track: Track, *, now: bool = False) -> bool:
        """Добавляет трек. now=True — заменить текущий (радио). Возвращает True, если заиграл сразу."""
        vc = self.voice
        if now:
            self.queue.clear()
            self.queue.append(track)
            if vc and (vc.is_playing() or vc.is_paused()):
                vc.stop()  # after -> play_next возьмёт новый трек
                return True
        else:
            self.queue.append(track)
        if vc and not vc.is_playing() and not vc.is_paused():
            await self.play_next()
            return self.current is track
        return now

    async def set_volume(self, percent: int) -> None:
        self.volume = max(5, min(150, percent))
        vc, track = self.voice, self.current
        if vc and track and (vc.is_playing() or vc.is_paused()):
            seek = self.elapsed()
            self._skip_after += 1
            vc.stop()
            try:
                await self._start(track, seek)
            except Exception as exc:
                log.warning("Не удалось сменить громкость: %s", exc)
                await self.play_next()

    async def stop(self) -> None:
        self.queue.clear()
        vc = self.voice
        self.current = None
        if vc:
            if vc.is_playing() or vc.is_paused():
                self._skip_after += 1
                vc.stop()
            await vc.disconnect(force=True)
        self._close_stream()
        await self.refresh_presence()

    async def refresh_presence(self) -> None:
        busy = [p.current.title for p in getattr(self.client, "players", {}).values() if p.current]
        name = f"🎵 {busy[0]}"[:120] if busy else "❄️ свободен · /play"
        try:
            await self.client.change_presence(activity=discord.Activity(type=discord.ActivityType.listening,
                                                                        name=name))
        except Exception:  # презенс — косметика, не должен ломать плеер
            pass


class MusicWorker(discord.Client):
    """Отдельный бот-музыкант. Слушает только серверы и голосовые состояния."""

    def __init__(self, index: int):
        intents = discord.Intents.none()
        intents.guilds = True
        intents.voice_states = True
        super().__init__(intents=intents)
        self.index = index
        self.players: dict[int, GuildPlayer] = {}

    async def on_ready(self) -> None:
        log.info("Музыкальный бот #%s готов: %s", self.index, self.user)
        await self.change_presence(activity=discord.Activity(type=discord.ActivityType.listening,
                                                             name="❄️ свободен · /play"))

    async def on_voice_state_update(self, member: discord.Member, before: discord.VoiceState,
                                    after: discord.VoiceState) -> None:
        await leave_if_alone(self, member.guild)


def player_for(client: discord.Client, guild_id: int) -> GuildPlayer:
    players: dict[int, GuildPlayer] = client.__dict__.setdefault("players", {})
    if guild_id not in players:
        players[guild_id] = GuildPlayer(client, guild_id)
    return players[guild_id]


async def leave_if_alone(client: discord.Client, guild: discord.Guild) -> None:
    """Если в канале с ботом не осталось людей — ждём минуту и уходим."""
    vc = guild.voice_client
    if not isinstance(vc, discord.VoiceClient) or not vc.channel:
        return
    if any(not m.bot for m in vc.channel.members):
        return
    await asyncio.sleep(IDLE_LEAVE_SECONDS)
    vc = guild.voice_client
    if isinstance(vc, discord.VoiceClient) and vc.channel and not any(not m.bot for m in vc.channel.members):
        await player_for(client, guild.id).stop()
