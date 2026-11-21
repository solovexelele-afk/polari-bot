"""Шаблон сервера POLARI: роли, категории, каналы и права.

Структура повторяет обнаруженную на TENDERLE (Знакомство -> Прихожая с комнатами ожидания),
дополнена каналами, которые были закрыты для просмотра, но нужны по ТЗ.
Ключи (key) — внутренние идентификаторы; ID реальных объектов сохраняются в БД.
"""
from __future__ import annotations

from dataclasses import dataclass, field

from config import settings

# --------------------------------------------------------------------------- роли
P = dict  # короткий алиас для наборов прав


@dataclass(frozen=True)
class RoleSpec:
    key: str
    name: str
    color: int
    level: int = 0                     # уровень персонала (0 — не персонал)
    hoist: bool = False
    perms: dict = field(default_factory=dict)
    aliases: tuple[str, ...] = ()
    staff: bool = False                # входит в «Персонал» (доступ к категории персонала)


# От высшей к низшей. В этом порядке роли создаются, поэтому новые роли выстраиваются корректно.
ROLES: list[RoleSpec] = [
    RoleSpec("founder", "👑 Founder", 0x0B1F4B, 100, True, P(administrator=True), ("основатель", "owner", "владелец"), True),
    RoleSpec("deputy", "💠 Co-Founder", 0x123A7A, 90, True,
             P(manage_guild=True, manage_roles=True, manage_channels=True, ban_members=True, kick_members=True,
               moderate_members=True, manage_messages=True, view_audit_log=True, move_members=True,
               mute_members=True, manage_nicknames=True),
             ("co-founder", "заместитель основателя", "зам основателя", "зам. основателя", "deputy"), True),
    RoleSpec("head_admin", "🔷 Head Admin", 0x1C4FA8, 80, True,
             P(manage_roles=True, manage_channels=True, ban_members=True, kick_members=True, moderate_members=True,
               manage_messages=True, view_audit_log=True, move_members=True, mute_members=True, manage_nicknames=True),
             ("head admin", "главный администратор", "гл. администратор", "главный админ", "head admin"), True),
    RoleSpec("admin", "🔹 Admin", 0x2563C9, 70, True,
             P(ban_members=True, kick_members=True, moderate_members=True, manage_messages=True,
               view_audit_log=True, move_members=True, mute_members=True, manage_nicknames=True),
             ("администратор", "администрация", "админ", "admin"), True),
    RoleSpec("senior_mod", "🌀 Senior Moderator", 0x2F7FE0, 60, True,
             P(kick_members=True, moderate_members=True, manage_messages=True, move_members=True,
               mute_members=True, manage_nicknames=True),
             ("senior moderator", "старший модератор", "ст. модератор", "senior moderator"), True),
    RoleSpec("mod", "🛡️ Moderator", 0x3A93F0, 50, True,
             P(moderate_members=True, manage_messages=True, move_members=True, mute_members=True),
             ("модератор", "модерация", "moderator"), True),
    RoleSpec("junior_mod", "🔰 Junior Moderator", 0x5AA8F5, 40, True,
             P(moderate_members=True, manage_messages=True),
             ("junior moderator", "младший модератор", "мл. модератор", "junior moderator", "хелпер", "helper"), True),
    RoleSpec("checker", "🎧 Verifier", 0x4FB3E8, 0, False, P(move_members=True, mute_members=True),
             ("verifier", "проверяющий", "обзвонщик", "верификатор", "саппорт обзвона"), True),
    RoleSpec("support", "💬 Support", 0x67C1EE, 0, False, P(), ("поддержка", "саппорт", "support"), True),
    RoleSpec("shop_staff", "🛒 Shop Manager", 0x7ACCF2, 0, False, P(),
             ("shop manager", "сотрудник магазина", "менеджер магазина", "магазин"), True),
    RoleSpec("staff", "🧊 Staff", 0x8FD6F5, 0, True, P(), ("персонал", "стафф", "staff"), True),
    # Спонсорские роли — товары магазина (обнаружены на TENDERLE)
    RoleSpec("custom_sponsor", "✦ Custom Sponsor", 0xA8E4FF, 0, True, P(), ("custom sponsor", "кастом спонсор")),
    RoleSpec("diamond", "💎 Diamond", 0x9BE7FF, 0, True, P(), ("diamond", "алмаз")),
    RoleSpec("diamond_sponsor", "💠 Diamond Sponsor", 0x7FDBFF, 0, True, P(), ("даймонд спонсор", "diamond sponsor")),
    RoleSpec("sponsor", "❄️ Sponsor", 0x6CCFF6, 0, True, P(), ("спонсор", "sponsor")),
    RoleSpec("partner", "🤝 Partner", 0x5BC0EB, 0, True, P(), ("партнёр", "партнер", "partner")),
    RoleSpec("vip", "⭐ VIP", 0x4AA3DF, 0, True, P(), ("vip", "вип")),
    RoleSpec("passed", "✅ Verified", 0x3D7FBF, 0, False, P(), ("verified", "прошёл обзвон", "прошел обзвон", "верифицирован")),
    RoleSpec("member", "🌊 Member", 0x35618F, 0, False, P(), ("участник", "пользователь", "member")),
    RoleSpec("onhold", "⏳ Unverified", 0x2B3A55, 0, False, P(),
             ("на проверке", "новичок", "непроверенный", "unverified")),
]
ROLE_BY_KEY = {r.key: r for r in ROLES}
STAFF_KEYS = [r.key for r in ROLES if r.staff]
LEVEL_KEYS = [r.key for r in ROLES if r.level > 0]


def keys_with_level(min_level: int) -> list[str]:
    return [r.key for r in ROLES if r.level >= min_level]


# --------------------------------------------------------------------------- каналы
# Права записываются как {цель: {право: bool}}. Цели: "@everyone", ключ роли, "@staff" (все STAFF_KEYS),
# "@level:N" (роли персонала с уровнем >= N).
READ_ONLY = P(view_channel=True, send_messages=False, add_reactions=True, read_message_history=True)
HIDDEN = P(view_channel=False)
CHAT = P(view_channel=True, send_messages=True, read_message_history=True, attach_files=True, embed_links=True)
VOICE = P(view_channel=True, connect=True, speak=True)
STAFF_FULL = P(view_channel=True, send_messages=True, read_message_history=True, connect=True, speak=True,
               move_members=True, mute_members=True, manage_messages=True)


@dataclass(frozen=True)
class ChannelSpec:
    key: str
    name: str
    kind: str = "text"                # text | voice
    topic: str = ""
    aliases: tuple[str, ...] = ()
    overwrites: dict = field(default_factory=dict)  # дополнительно к правам категории
    user_limit: int = 0
    slowmode: int = 0


@dataclass(frozen=True)
class CategorySpec:
    key: str
    name: str
    aliases: tuple[str, ...]
    overwrites: dict
    channels: list[ChannelSpec]


def _rooms(prefix: str, key: str, emoji: list[str], count: int, overwrites: dict | None = None,
           limit: int = 0, aliases_extra: tuple[str, ...] = ()) -> list[ChannelSpec]:
    rooms = []
    for i in range(1, count + 1):
        e = emoji[(i - 1) % len(emoji)]
        rooms.append(ChannelSpec(f"{key}_{i}", f"{e}・{prefix} {i}", "voice",
                                 aliases=(f"{prefix} {i}".lower(),) + tuple(f"{a} {i}" for a in aliases_extra),
                                 overwrites=overwrites or {}, user_limit=limit))
    return rooms


CATEGORIES: list[CategorySpec] = [
    # Новичок (Unverified) видит здесь только памятку, правила и магазин; новости — после обзвона
    CategorySpec("cat_intro", "⎯⎯ 💙・Знакомство", ("знакомство",),
                 {"@everyone": HIDDEN, "onhold": READ_ONLY, "member": READ_ONLY,
                  "@staff": P(view_channel=True, send_messages=True)},
                 [
                     ChannelSpec("news", "💙・polari-news", topic="Новости и анонсы POLARI",
                                 aliases=("polari-news", "новости", "news", "анонсы"),
                                 overwrites={"onhold": HIDDEN}),
                     ChannelSpec("memo", "├📑・памятка", topic="Памятка по серверу POLARI", aliases=("памятка",)),
                     ChannelSpec("rules", "├📘・правила", topic="Правила сервера POLARI", aliases=("правила", "rules")),
                     ChannelSpec("shop", "├💎・магазин", topic="Магазин POLARI: спонсорские и кастомные роли",
                                 aliases=("магазин", "shop", "донат")),
                 ]),
    # Поддержка сразу под «Знакомством»: видна и новичкам, и участникам
    CategorySpec("cat_support", "⎯⎯ 🎫・Поддержка", ("поддержка", "support"),
                 {"@everyone": HIDDEN, "member": READ_ONLY, "@staff": STAFF_FULL},
                 [
                     ChannelSpec("ticket_panel", "🎫・создать-тикет", topic="Обращения в поддержку POLARI",
                                 aliases=("создать-тикет", "тикеты", "тикет", "support")),
                     ChannelSpec("tech_support", "🛠️・тех-поддержка", topic="Проблемы с Discord, ботом и доступом",
                                 aliases=("тех-поддержка", "техподдержка", "tech-support")),
                     ChannelSpec("faq", "❓・частые-вопросы", topic="Ответы на частые вопросы",
                                 aliases=("частые-вопросы", "faq", "вопросы")),
                 ]),
    CategorySpec("cat_tickets", "⎯⎯ 📨・Тикеты", ("тикеты", "обращения", "tickets"),
                 {"@everyone": HIDDEN, "support": STAFF_FULL, "shop_staff": STAFF_FULL, "@level:40": STAFF_FULL},
                 []),
    CategorySpec("cat_lobby", "⎯⎯ 🌸・Прихожая", ("прихожая", "ожидание", "верификация"),
                 {"@everyone": HIDDEN,
                  "onhold": P(view_channel=True, send_messages=False, read_message_history=True),
                  "@staff": STAFF_FULL},
                 [
                     ChannelSpec("info", "📋・информация", topic="Как получить доступ к серверу",
                                 aliases=("информация", "info", "инфо")),
                     ChannelSpec("newbies", "💌・новички", topic="Приветствие новых участников",
                                 aliases=("новички", "приветствие", "welcome")),
                     *_rooms("Ожидание", "waiting", ["🧊", "❄️", "🌊", "💧", "🫧", "🌀", "💠", "🔹", "🌌", "🐋"],
                             settings.waiting_rooms,
                             {"onhold": P(view_channel=True, connect=True, speak=True)}),
                     *_rooms("Собеседование", "interview", ["🎙️"], settings.interview_rooms,
                             {"onhold": P(view_channel=True, connect=False, speak=True)}, limit=4,
                             aliases_extra=("обзвон",)),
                 ]),
    CategorySpec("cat_community", "⎯⎯ 💬・Общение", ("общение", "общий", "чаты"),
                 {"@everyone": HIDDEN, "member": CHAT | P(connect=True, speak=True), "@staff": STAFF_FULL},
                 [
                     ChannelSpec("chat", "💬・чат", topic="Общий чат POLARI", aliases=("чат", "общий", "general", "общение")),
                     ChannelSpec("suggestions", "💡・предложения", topic="Используйте /suggest",
                                 aliases=("предложения", "идеи", "suggestions"),
                                 overwrites={"member": P(send_messages=False, send_messages_in_threads=True)}),
                     ChannelSpec("polls", "📊・голосования", topic="Голосования сервера",
                                 aliases=("голосования", "опросы", "polls"), overwrites={"member": P(send_messages=False)}),
                     ChannelSpec("events", "🎉・мероприятия", topic="Ивенты и турниры POLARI",
                                 aliases=("мероприятия", "ивенты", "события", "events"),
                                 overwrites={"member": P(send_messages=False)}),
                     ChannelSpec("music", "🎵・музыка", topic="Музыка через бота CHIP: /play название трека или ссылка",
                                 aliases=("музыка", "music")),
                     ChannelSpec("voice_1", "🔊・Общий", "voice", aliases=("общий", "general", "общий 1")),
                 ]),
    CategorySpec("cat_staff", "⎯⎯ 🛡️・Персонал", ("персонал", "стафф", "staff", "администрация"),
                 {"@everyone": HIDDEN, "@staff": STAFF_FULL},
                 [
                     ChannelSpec("staff_memo", "📘・памятка-персонала", topic="Памятка для персонала POLARI",
                                 aliases=("памятка-персонала", "памятка-стафф", "staff-memo"),
                                 overwrites={"@staff": P(view_channel=True, send_messages=False)}),
                     ChannelSpec("staff_chat", "💬・чат-стафф", aliases=("чат-стафф", "staff-chat", "стафф-чат")),
                     ChannelSpec("interview_panel", "🎙️・обзвон-панель",
                                 topic="Очередь и команды /interview_*",
                                 aliases=("обзвон-панель", "обзвон", "собеседования")),
                     ChannelSpec("orders", "🛒・заказы", topic="Журнал заказов магазина",
                                 aliases=("заказы", "orders"),
                                 overwrites={"@staff": HIDDEN, "shop_staff": STAFF_FULL, "@level:80": STAFF_FULL}),
                 ]),
    CategorySpec("cat_logs", "⎯⎯ 📜・Логи", ("логи", "logs", "журнал"),
                 {"@everyone": HIDDEN, "@level:70": P(view_channel=True, send_messages=False, read_message_history=True)},
                 [
                     ChannelSpec("log_joins", "📥・вход-выход", aliases=("вход-выход", "joins")),
                     ChannelSpec("log_verify", "✅・собеседования-лог", aliases=("верификация-лог", "собеседования-лог")),
                     ChannelSpec("log_roles", "🎭・роли-лог", aliases=("роли-лог", "roles-log")),
                     ChannelSpec("log_tickets", "🎫・тикеты-лог", aliases=("тикеты-лог", "tickets-log")),
                     ChannelSpec("log_shop", "💳・покупки-лог", aliases=("покупки-лог", "оплаты-лог", "shop-log")),
                     ChannelSpec("log_mod", "🔨・наказания-лог", aliases=("наказания-лог", "mod-log", "модерация-лог")),
                     ChannelSpec("log_messages", "✏️・сообщения-лог", aliases=("сообщения-лог", "message-log")),
                     ChannelSpec("log_server", "⚙️・сервер-лог", aliases=("сервер-лог", "server-log")),
                 ]),
]

ALL_CHANNEL_KEYS = [c.key for cat in CATEGORIES for c in cat.channels]
WAITING_KEYS = [f"waiting_{i}" for i in range(1, settings.waiting_rooms + 1)]
INTERVIEW_KEYS = [f"interview_{i}" for i in range(1, settings.interview_rooms + 1)]

# Куда отправляются логи каждого типа
LOG_ROUTES = {
    "join": "log_joins", "leave": "log_joins",
    "interview": "log_verify",
    "role": "log_roles",
    "ticket": "log_tickets",
    "shop": "log_shop", "payment": "log_shop",
    "mod": "log_mod", "warn": "log_mod", "automod": "log_mod",
    "message": "log_messages",
    "server": "log_server", "setup": "log_server",
}
