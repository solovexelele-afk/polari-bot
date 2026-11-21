"""Вспомогательные функции: нормализация названий, длительности, маскирование секретов."""
from __future__ import annotations

import re
import unicodedata
from datetime import timedelta
from difflib import SequenceMatcher

_TOKEN_RE = re.compile(r"[A-Za-z\d_-]{23,28}\.[A-Za-z\d_-]{6,7}\.[A-Za-z\d_-]{27,40}")   # Discord-токены
_CARD_RE = re.compile(r"\b(?:\d[ -]?){13,19}\b")                                         # номера карт
_SECRET_RE = re.compile(r"(?i)\b(pass(word)?|пароль|token|токен|secret|api[_-]?key|cvv|cvc)\b\s*[:=]\s*\S+")
_LONG_KEY_RE = re.compile(r"\b(sk|pk|rk)_(live|test)_[A-Za-z0-9]{10,}\b")


def redact(text: str | None) -> str:
    """Убирает из текста токены, пароли, номера карт и ключи API перед записью в логи."""
    if not text:
        return ""
    text = _TOKEN_RE.sub("[скрыт токен]", text)
    text = _LONG_KEY_RE.sub("[скрыт ключ]", text)
    text = _SECRET_RE.sub(lambda m: f"{m.group(1)}: [скрыто]", text)
    text = _CARD_RE.sub("[скрыт номер]", text)
    return text


def normalize(name: str) -> str:
    """Название без эмодзи, разделителей и регистра: '├📘・Правила' -> 'правила'."""
    name = name.lower().replace("ё", "е")
    kept = []
    for ch in name:
        cat = unicodedata.category(ch)
        if cat[0] in ("L", "N"):
            kept.append(ch)
        elif ch in " -_." or cat[0] == "Z":
            kept.append(" ")
    return re.sub(r"\s+", " ", "".join(kept)).strip()


def similarity(a: str, b: str) -> float:
    a, b = normalize(a), normalize(b)
    if not a or not b:
        return 0.0
    if a == b:
        return 1.0
    return SequenceMatcher(None, a, b).ratio()


_DUR_RE = re.compile(r"(\d+)\s*(д|d|ч|h|м|m|мин|min|с|s)", re.I)
_UNITS = {"д": 86400, "d": 86400, "ч": 3600, "h": 3600, "м": 60, "m": 60, "мин": 60, "min": 60, "с": 1, "s": 1}


def parse_duration(text: str) -> timedelta | None:
    """'1д 2ч', '30m', '45мин' -> timedelta. Без единиц — минуты."""
    text = text.strip().lower()
    if text.isdigit():
        return timedelta(minutes=int(text))
    total = sum(int(n) * _UNITS[u.lower()] for n, u in _DUR_RE.findall(text))
    return timedelta(seconds=total) if total > 0 else None


def human_delta(delta: timedelta) -> str:
    seconds = int(delta.total_seconds())
    parts = []
    for size, label in ((86400, "д"), (3600, "ч"), (60, "мин"), (1, "с")):
        if seconds >= size:
            parts.append(f"{seconds // size}{label}")
            seconds %= size
    return " ".join(parts) or "0с"


def parse_color(text: str) -> int | None:
    text = text.strip().lstrip("#")
    if re.fullmatch(r"[0-9a-fA-F]{6}", text):
        return int(text, 16)
    return None
