"""Асинхронная SQLite-база POLARI с версионными миграциями."""
from __future__ import annotations

import logging
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable

import aiosqlite

log = logging.getLogger("polari.db")


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


# Каждая миграция применяется один раз; новые добавляются в конец списка.
MIGRATIONS: list[str] = [
    # v1 — базовая схема
    """
    CREATE TABLE guild_settings (
        guild_id INTEGER NOT NULL, key TEXT NOT NULL, value TEXT NOT NULL,
        PRIMARY KEY (guild_id, key)
    );
    CREATE TABLE users (
        guild_id INTEGER NOT NULL, user_id INTEGER NOT NULL,
        reputation INTEGER NOT NULL DEFAULT 0, last_rep_given TEXT,
        first_joined TEXT, last_joined TEXT, left_at TEXT, messages INTEGER NOT NULL DEFAULT 0,
        PRIMARY KEY (guild_id, user_id)
    );
    CREATE TABLE interviews (
        guild_id INTEGER NOT NULL, user_id INTEGER NOT NULL,
        status TEXT NOT NULL,                  -- pending | passed | failed | left
        admin_id INTEGER, reason TEXT, attempts INTEGER NOT NULL DEFAULT 0,
        created_at TEXT NOT NULL, updated_at TEXT NOT NULL,
        PRIMARY KEY (guild_id, user_id)
    );
    CREATE TABLE interview_history (
        id INTEGER PRIMARY KEY AUTOINCREMENT, guild_id INTEGER NOT NULL, user_id INTEGER NOT NULL,
        admin_id INTEGER, action TEXT NOT NULL, reason TEXT, created_at TEXT NOT NULL
    );
    CREATE TABLE warnings (
        id INTEGER PRIMARY KEY AUTOINCREMENT, guild_id INTEGER NOT NULL, user_id INTEGER NOT NULL,
        moderator_id INTEGER NOT NULL, reason TEXT NOT NULL, active INTEGER NOT NULL DEFAULT 1,
        created_at TEXT NOT NULL
    );
    CREATE TABLE punishments (
        id INTEGER PRIMARY KEY AUTOINCREMENT, guild_id INTEGER NOT NULL, user_id INTEGER NOT NULL,
        moderator_id INTEGER NOT NULL, type TEXT NOT NULL, reason TEXT, duration_seconds INTEGER,
        expires_at TEXT, active INTEGER NOT NULL DEFAULT 1, created_at TEXT NOT NULL
    );
    CREATE TABLE tickets (
        id INTEGER PRIMARY KEY AUTOINCREMENT, guild_id INTEGER NOT NULL, number INTEGER NOT NULL,
        channel_id INTEGER UNIQUE, user_id INTEGER NOT NULL, type TEXT NOT NULL, subject TEXT,
        status TEXT NOT NULL DEFAULT 'open', claimed_by INTEGER, closed_by INTEGER,
        created_at TEXT NOT NULL, closed_at TEXT
    );
    CREATE TABLE orders (
        id INTEGER PRIMARY KEY AUTOINCREMENT, guild_id INTEGER NOT NULL, ticket_id INTEGER,
        user_id INTEGER NOT NULL, item_id TEXT NOT NULL, item_name TEXT NOT NULL, price INTEGER NOT NULL,
        status TEXT NOT NULL DEFAULT 'pending',   -- pending | paid | rejected | cancelled
        custom_name TEXT, custom_color TEXT, role_id INTEGER, handled_by INTEGER,
        expires_at TEXT, created_at TEXT NOT NULL, updated_at TEXT NOT NULL
    );
    CREATE TABLE payments (
        id INTEGER PRIMARY KEY AUTOINCREMENT, order_id INTEGER NOT NULL, action TEXT NOT NULL,
        staff_id INTEGER, note TEXT, created_at TEXT NOT NULL
    );
    CREATE TABLE role_grants (
        id INTEGER PRIMARY KEY AUTOINCREMENT, guild_id INTEGER NOT NULL, user_id INTEGER NOT NULL,
        role_id INTEGER NOT NULL, action TEXT NOT NULL, source TEXT NOT NULL, actor_id INTEGER,
        expires_at TEXT, active INTEGER NOT NULL DEFAULT 1, created_at TEXT NOT NULL
    );
    CREATE TABLE panels (
        guild_id INTEGER NOT NULL, key TEXT NOT NULL, channel_id INTEGER NOT NULL,
        message_id INTEGER NOT NULL, updated_at TEXT NOT NULL, PRIMARY KEY (guild_id, key)
    );
    CREATE TABLE stats (
        guild_id INTEGER NOT NULL, day TEXT NOT NULL, joins INTEGER NOT NULL DEFAULT 0,
        leaves INTEGER NOT NULL DEFAULT 0, messages INTEGER NOT NULL DEFAULT 0,
        PRIMARY KEY (guild_id, day)
    );
    CREATE TABLE events (
        id INTEGER PRIMARY KEY AUTOINCREMENT, guild_id INTEGER NOT NULL, channel_id INTEGER,
        message_id INTEGER, title TEXT NOT NULL, description TEXT, starts_at TEXT,
        created_by INTEGER NOT NULL, created_at TEXT NOT NULL
    );
    CREATE TABLE event_participants (
        event_id INTEGER NOT NULL, user_id INTEGER NOT NULL, PRIMARY KEY (event_id, user_id)
    );
    CREATE TABLE suggestions (
        id INTEGER PRIMARY KEY AUTOINCREMENT, guild_id INTEGER NOT NULL, user_id INTEGER NOT NULL,
        content TEXT NOT NULL, message_id INTEGER, status TEXT NOT NULL DEFAULT 'open', created_at TEXT NOT NULL
    );
    CREATE TABLE channel_locks (
        guild_id INTEGER NOT NULL, channel_id INTEGER NOT NULL, target_id INTEGER NOT NULL,
        previous TEXT, PRIMARY KEY (channel_id, target_id)
    );
    CREATE INDEX idx_tickets_user ON tickets(guild_id, user_id, status);
    CREATE INDEX idx_orders_status ON orders(guild_id, status);
    CREATE INDEX idx_grants_exp ON role_grants(active, expires_at);
    CREATE INDEX idx_pun_exp ON punishments(active, expires_at);
    """,
    # v2 — киновечера
    """
    CREATE TABLE movies (
        id INTEGER PRIMARY KEY AUTOINCREMENT, guild_id INTEGER NOT NULL, title TEXT NOT NULL,
        year INTEGER, genre TEXT, description TEXT, added_by INTEGER, watched_at TEXT, created_at TEXT NOT NULL
    );
    CREATE TABLE movie_polls (
        id INTEGER PRIMARY KEY AUTOINCREMENT, guild_id INTEGER NOT NULL, day TEXT NOT NULL,
        channel_id INTEGER, message_id INTEGER, options TEXT NOT NULL,
        status TEXT NOT NULL DEFAULT 'open',          -- open | closed | started | finished
        closes_at TEXT NOT NULL, starts_at TEXT NOT NULL, winner_id INTEGER, event_id INTEGER,
        created_at TEXT NOT NULL
    );
    CREATE TABLE movie_votes (
        poll_id INTEGER NOT NULL, user_id INTEGER NOT NULL, movie_id INTEGER NOT NULL,
        PRIMARY KEY (poll_id, user_id)
    );
    CREATE INDEX idx_movie_polls ON movie_polls(guild_id, day);
    """,
    # v3 — рейтинг: опыт, уровни, вафли, активность в чате и голосе
    """
    CREATE TABLE levels (
        guild_id INTEGER NOT NULL, user_id INTEGER NOT NULL,
        xp INTEGER NOT NULL DEFAULT 0, level INTEGER NOT NULL DEFAULT 0, waffles INTEGER NOT NULL DEFAULT 0,
        messages INTEGER NOT NULL DEFAULT 0, voice_seconds INTEGER NOT NULL DEFAULT 0,
        PRIMARY KEY (guild_id, user_id)
    );
    CREATE INDEX idx_levels_xp ON levels(guild_id, xp DESC);
    """,
]


class Database:
    def __init__(self, path: Path):
        self.path = path
        self.conn: aiosqlite.Connection | None = None

    async def connect(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.conn = await aiosqlite.connect(self.path)
        self.conn.row_factory = aiosqlite.Row
        await self.conn.execute("PRAGMA journal_mode=WAL")
        await self.conn.execute("PRAGMA foreign_keys=ON")
        await self.migrate()

    async def close(self) -> None:
        if self.conn:
            await self.conn.close()

    async def migrate(self) -> None:
        assert self.conn
        await self.conn.execute("CREATE TABLE IF NOT EXISTS schema_version (version INTEGER NOT NULL)")
        row = await (await self.conn.execute("SELECT MAX(version) FROM schema_version")).fetchone()
        current = row[0] or 0
        for version, script in enumerate(MIGRATIONS, start=1):
            if version <= current:
                continue
            try:
                await self.conn.executescript(f"BEGIN;\n{script}\nINSERT INTO schema_version VALUES ({version});\nCOMMIT;")
                log.info("Применена миграция БД v%s", version)
            except Exception:
                await self.conn.rollback()
                log.exception("Ошибка миграции v%s — бот остановлен, данные не изменены", version)
                raise

    # ----------------------------------------------------------- базовые операции
    async def execute(self, sql: str, params: Iterable[Any] = ()) -> int:
        """Выполняет запрос с коммитом. Возвращает lastrowid."""
        assert self.conn
        try:
            cur = await self.conn.execute(sql, tuple(params))
            await self.conn.commit()
            return cur.lastrowid or 0
        except Exception:
            await self.conn.rollback()
            log.exception("Ошибка запроса: %s", sql.split("\n")[0][:120])
            raise

    async def fetchone(self, sql: str, params: Iterable[Any] = ()) -> aiosqlite.Row | None:
        assert self.conn
        return await (await self.conn.execute(sql, tuple(params))).fetchone()

    async def fetchall(self, sql: str, params: Iterable[Any] = ()) -> list[aiosqlite.Row]:
        assert self.conn
        return list(await (await self.conn.execute(sql, tuple(params))).fetchall())

    async def fetchval(self, sql: str, params: Iterable[Any] = (), default: Any = None) -> Any:
        row = await self.fetchone(sql, params)
        return row[0] if row and row[0] is not None else default

    # ----------------------------------------------------------- настройки сервера
    async def get_setting(self, guild_id: int, key: str) -> str | None:
        return await self.fetchval("SELECT value FROM guild_settings WHERE guild_id=? AND key=?", (guild_id, key))

    async def set_setting(self, guild_id: int, key: str, value: Any) -> None:
        await self.execute(
            "INSERT INTO guild_settings VALUES (?,?,?) ON CONFLICT(guild_id,key) DO UPDATE SET value=excluded.value",
            (guild_id, key, str(value)),
        )

    async def all_settings(self, guild_id: int) -> dict[str, str]:
        rows = await self.fetchall("SELECT key, value FROM guild_settings WHERE guild_id=?", (guild_id,))
        return {r["key"]: r["value"] for r in rows}

    # ----------------------------------------------------------- панели
    async def save_panel(self, guild_id: int, key: str, channel_id: int, message_id: int) -> None:
        await self.execute(
            "INSERT INTO panels VALUES (?,?,?,?,?) ON CONFLICT(guild_id,key) DO UPDATE SET "
            "channel_id=excluded.channel_id, message_id=excluded.message_id, updated_at=excluded.updated_at",
            (guild_id, key, channel_id, message_id, now_iso()),
        )

    async def bump_stat(self, guild_id: int, column: str, amount: int = 1) -> None:
        if column not in {"joins", "leaves", "messages"}:
            raise ValueError(column)
        day = datetime.now(timezone.utc).date().isoformat()
        await self.execute(
            f"INSERT INTO stats (guild_id, day, {column}) VALUES (?,?,?) "
            f"ON CONFLICT(guild_id, day) DO UPDATE SET {column}={column}+excluded.{column}",
            (guild_id, day, amount),
        )

    async def log_role_grant(self, guild_id: int, user_id: int, role_id: int, action: str, source: str,
                             actor_id: int | None, expires_at: str | None = None) -> None:
        await self.execute(
            "INSERT INTO role_grants (guild_id,user_id,role_id,action,source,actor_id,expires_at,active,created_at) "
            "VALUES (?,?,?,?,?,?,?,?,?)",
            (guild_id, user_id, role_id, action, source, actor_id, expires_at,
             1 if expires_at and action == "add" else 0, now_iso()),
        )
