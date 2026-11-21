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
from collections import deque
from dataclasses import dataclass, field
from pathlib import Path

import discord

log = logging.getLogger("polari.music")
BASE_DIR = Path(__file__).resolve().parents[2]
BEFORE_OPTIONS = "-reconnect 1 -reconnect_streamed 1 -reconnect_delay_max 5 -nostdin"
IDLE_LEAVE_SECONDS = 60


def ffmpeg_path() -> str | None:
    """ffmpeg: переменная FFMPEG_PATH, локальный bin/ffmpeg (ставится на Render при сборке) или системный."""
    for candidate in (os.getenv("FFMPEG_PATH"), str(BASE_DIR / "bin" / "ffmpeg"), shutil.which("ffmpeg")):
        if candidate and Path(candidate).exists():
            return candidate
    return None


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

    def _source(self, track: Track, seek: float = 0.0) -> discord.FFmpegOpusAudio:
        exe = ffmpeg_path()
        if not exe:
            raise RuntimeError("ffmpeg не установлен на сервере бота.")
        before = BEFORE_OPTIONS + (f" -ss {seek:.1f}" if seek and not track.live else "")
        return discord.FFmpegOpusAudio(track.url, executable=exe, before_options=before,
                                       options=f"-vn -filter:a volume={self.volume / 100:.2f}")

    def _start(self, track: Track, seek: float = 0.0) -> None:
        vc = self.voice
        if not vc:
            return
        loop = self.client.loop
        source = self._source(track, seek)
        self.current, self.offset, self.started_at = track, seek, time.monotonic()
        vc.play(source, after=lambda err: loop.call_soon_threadsafe(self._after, err))

    def _after(self, error: Exception | None) -> None:
        if error:
            log.warning("Ошибка воспроизведения: %s", error)
        if self._skip_after > 0:
            self._skip_after -= 1
            return
        self.play_next()

    def play_next(self) -> None:
        if not self.queue:
            self.current, self.started_at, self.offset = None, 0.0, 0.0
            self.client.loop.create_task(self.refresh_presence())
            return
        try:
            self._start(self.queue.popleft())
        except Exception:
            log.exception("Не удалось запустить трек")
            self.play_next()
            return
        self.client.loop.create_task(self.refresh_presence())

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
            self.play_next()
            return True
        return now

    def set_volume(self, percent: int) -> None:
        self.volume = max(5, min(150, percent))
        vc, track = self.voice, self.current
        if vc and track and (vc.is_playing() or vc.is_paused()):
            seek = self.elapsed()
            self._skip_after += 1
            vc.stop()
            self._start(track, seek)

    async def stop(self) -> None:
        self.queue.clear()
        vc = self.voice
        self.current = None
        if vc:
            if vc.is_playing() or vc.is_paused():
                self._skip_after += 1
                vc.stop()
            await vc.disconnect(force=True)
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
