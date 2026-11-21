"""Радиостанции POLARI (публичные потоки, проверены на доступность)."""
from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class Station:
    id: str
    name: str
    emoji: str
    url: str
    genre: str


STATIONS: list[Station] = [
    Station("record", "Radio Record", "🔴", "https://radiorecord.hostingradio.ru/rr_main96.aacp", "Танцевальная"),
    Station("phonk", "Record Phonk", "🖤", "https://radiorecord.hostingradio.ru/phonk96.aacp", "Phonk"),
    Station("lofi", "Record Lo-Fi", "☕", "https://radiorecord.hostingradio.ru/lofi96.aacp", "Lo-Fi"),
    Station("chill", "Record Chill-Out", "🌙", "https://radiorecord.hostingradio.ru/chil96.aacp", "Chill"),
    Station("deep", "Record Deep", "🌊", "https://radiorecord.hostingradio.ru/deep96.aacp", "Deep House"),
    Station("trap", "Record Trap", "🔥", "https://radiorecord.hostingradio.ru/trap96.aacp", "Trap"),
    Station("rusmix", "Record Russian Mix", "🎧", "https://radiorecord.hostingradio.ru/rus96.aacp", "Русский микс"),
    Station("rushits", "Record Russian Hits", "🇷🇺", "https://radiorecord.hostingradio.ru/russianhits96.aacp",
            "Русские хиты"),
    Station("europa", "Европа Плюс", "⭐", "https://ep256.hostingradio.ru:8052/europaplus256.mp3", "Поп"),
    Station("nashe", "Наше Радио", "🎸", "https://nashe1.hostingradio.ru/nashe-128.mp3", "Рок"),
]
STATION_BY_ID = {s.id: s for s in STATIONS}
