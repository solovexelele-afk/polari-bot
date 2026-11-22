"""/setup — безопасная первоначальная настройка сервера POLARI.

Гарантии:
  * ничего не удаляется и не переименовывается;
  * дубликаты не создаются: точные совпадения и алиасы используются как есть,
    похожие названия помечаются как «сомнительные» и НЕ трогаются без решения владельца;
  * права выставляются только на новых объектах;
  * DRY RUN ничего не меняет, APPLY выполняется только после нажатия «Применить».
"""
from __future__ import annotations

import asyncio
import io
import json
import logging
import re
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Awaitable, Callable

import discord
from discord import app_commands
from discord.ext import commands

from bot.data.blueprint import CATEGORIES, ROLES, STAFF_KEYS, CategorySpec, ChannelSpec, keys_with_level
from bot.database.db import now_iso
from bot.embeds import style
from bot.utils.checks import require
from bot.utils.helpers import normalize, similarity

if TYPE_CHECKING:
    from bot.core import PolariBot

log = logging.getLogger("polari.setup")

EXACT, DOUBT = 1.0, 0.72
PanelBuilder = Callable[[discord.TextChannel], Awaitable[discord.Message]]
# Ключ панели -> ключ канала, в который она публикуется
PANEL_CHANNELS = {
    "info": "info", "memo": "memo", "rules": "rules", "shop": "shop",
    "tickets": "ticket_panel", "interview": "interview_panel", "staff_memo": "staff_memo",
    "news": "news", "tech": "tech_support", "faq": "faq", "music": "music", "leaderboard": "leaderboard",
}


@dataclass
class Item:
    kind: str                 # role | category | text | voice
    key: str
    name: str
    action: str               # link | create | doubt | saved
    target: object | None = None
    note: str = ""
    spec: object | None = None
    parent: str | None = None


@dataclass
class Plan:
    items: list[Item] = field(default_factory=list)
    panels: list[tuple[str, str]] = field(default_factory=list)  # (panel_key, channel_key)
    warnings: list[str] = field(default_factory=list)
    removals: list = field(default_factory=list)  # пустые лишние каналы и категории

    def by(self, action: str) -> list[Item]:
        return [i for i in self.items if i.action == action]


def _digits(text: str) -> str:
    return "".join(re.findall(r"\d+", text))


def _score(spec_name: str, aliases: tuple[str, ...], actual: str) -> float:
    actual_n = normalize(actual)
    names = [spec_name, *aliases]
    if any(normalize(n) == actual_n for n in names):
        return EXACT
    # «Ожидание 1» и «Ожидание 2» — разные объекты, а не похожие
    if _digits(spec_name) != _digits(actual):
        return 0.0
    return max(similarity(n, actual) for n in names)


class SetupCog(commands.Cog):
    def __init__(self, bot: "PolariBot"):
        self.bot = bot
        self.panel_builders: dict[str, PanelBuilder] = {}
        self._lock = asyncio.Lock()

    def register_panel(self, key: str, builder: PanelBuilder) -> None:
        """Модули регистрируют функции публикации своих панелей."""
        self.panel_builders[key] = builder

    # ------------------------------------------------------------------ планирование
    def _match(self, spec_name: str, aliases: tuple[str, ...], pool: list, used: set[int],
               exact_only: bool) -> tuple[object | None, float]:
        best, best_score = None, 0.0
        for obj in pool:
            if obj.id in used:
                continue
            score = _score(spec_name, aliases, obj.name)
            if exact_only and score < EXACT:
                continue
            if score > best_score:
                best, best_score = obj, score
        return best, best_score

    async def build_plan(self, guild: discord.Guild) -> Plan:
        plan = Plan()
        saved = await self.bot.registry.load(guild.id)
        roles_pool = [r for r in guild.roles if not r.is_default() and not r.managed]
        cats_pool = list(guild.categories)
        text_pool = [c for c in guild.channels if isinstance(c, (discord.TextChannel, discord.ForumChannel))]
        voice_pool = [c for c in guild.channels if isinstance(c, (discord.VoiceChannel, discord.StageChannel))]

        specs: list[tuple[str, str, str, tuple, list, object, str | None]] = []
        for r in ROLES:
            specs.append(("role", r.key, r.name, r.aliases, roles_pool, r, None))
        for cat in CATEGORIES:
            specs.append(("category", cat.key, cat.name, cat.aliases, cats_pool, cat, None))
            for ch in cat.channels:
                pool = voice_pool if ch.kind == "voice" else text_pool
                specs.append((ch.kind, ch.key, ch.name, ch.aliases, pool, ch, cat.key))

        used: set[int] = set()
        resolved: dict[str, Item] = {}
        # 1) объекты, уже сохранённые ранее
        for kind, key, name, aliases, pool, spec, parent in specs:
            prefix = "role" if kind == "role" else "channel"
            sid = saved.get(f"{prefix}.{key}")
            obj = next((o for o in pool if sid and o.id == int(sid)), None)
            if obj:
                used.add(obj.id)
                resolved[key] = Item(kind, key, name, "saved", obj, spec=spec, parent=parent)
        # 2) точные совпадения и алиасы, 3) похожие названия
        for exact_only in (True, False):
            for kind, key, name, aliases, pool, spec, parent in specs:
                if key in resolved:
                    continue
                obj, score = self._match(name, aliases, pool, used, exact_only)
                if obj is None:
                    continue
                if score >= EXACT:
                    used.add(obj.id)
                    resolved[key] = Item(kind, key, name, "link", obj, spec=spec, parent=parent)
                elif score >= DOUBT:
                    used.add(obj.id)
                    resolved[key] = Item(kind, key, name, "doubt", obj,
                                         f"похоже на «{obj.name}» ({score:.0%})", spec=spec, parent=parent)
        for kind, key, name, aliases, pool, spec, parent in specs:
            item = resolved.get(key) or Item(kind, key, name, "create", spec=spec, parent=parent)
            # Категория под вопросом — не создаём её каналы «в никуда», ждём решения владельца
            if item.action == "create" and parent and resolved.get(parent) and resolved[parent].action == "doubt":
                item.action, item.note = "doubt", "категория под вопросом — сначала привяжите её"
            plan.items.append(item)

        # Автоудаление пустых лишних каналов отключено по просьбе владельца: чужие каналы не трогаем.
        # Включить обратно: plan.removals = await self._find_unused(guild, plan)

        # Панели: публикуем только те, которых ещё нет
        for panel_key, channel_key in PANEL_CHANNELS.items():
            row = await self.bot.db.fetchone("SELECT channel_id, message_id FROM panels WHERE guild_id=? AND key=?",
                                             (guild.id, panel_key))
            if row and guild.get_channel(row["channel_id"]):
                continue
            plan.panels.append((panel_key, channel_key))

        me = guild.me
        if not me.guild_permissions.manage_roles:
            plan.warnings.append("У бота нет права **Управлять ролями**.")
        if not me.guild_permissions.manage_channels:
            plan.warnings.append("У бота нет права **Управлять каналами**.")
        if not me.guild_permissions.administrator:
            plan.warnings.append("У бота нет Administrator: роль «Основатель» будет создана без прав, "
                                 "права ей выдаст владелец вручную.")
        return plan

    async def _find_unused(self, guild: discord.Guild, plan: Plan) -> list[discord.abc.GuildChannel]:
        """Лишние каналы вне шаблона: пустые текстовые, голосовые без людей и опустевшие категории.

        Не трогаем: каналы шаблона (в т.ч. сомнительные), тикеты, системные каналы сервера.
        """
        mapped = {getattr(i.target, "id", None) for i in plan.items if i.kind != "role"}
        tickets_id = await self.bot.registry.get(guild.id, "channel.cat_tickets")
        protected = {c.id for c in (guild.rules_channel, guild.public_updates_channel, guild.safety_alerts_channel)
                     if c is not None}
        result: list[discord.abc.GuildChannel] = []
        for ch in guild.channels:
            if ch.id in mapped or ch.id in protected or isinstance(ch, discord.CategoryChannel):
                continue
            if ch.category_id and str(ch.category_id) == tickets_id:
                continue
            if isinstance(ch, discord.TextChannel):
                if not ch.permissions_for(guild.me).read_message_history:
                    continue
                try:
                    if [m async for m in ch.history(limit=1)]:
                        continue  # в канале есть сообщения — не трогаем
                except discord.HTTPException:
                    continue
                result.append(ch)
            elif isinstance(ch, (discord.VoiceChannel, discord.StageChannel)) and not ch.members:
                result.append(ch)
        removing = {c.id for c in result}
        for cat in guild.categories:
            if cat.id in mapped:
                continue
            if all(c.id in removing for c in cat.channels):
                result.append(cat)
        return result

    def render_plan(self, plan: Plan, applied: bool = False) -> list[discord.Embed]:
        icons = {"role": "🎭", "category": "📁", "text": "#", "voice": "🔊"}

        def lines(items: list[Item]) -> list[str]:
            out = []
            for i in items:
                where = f" → `{i.target.name}`" if i.target is not None and i.action != "create" else ""
                note = f" — {i.note}" if i.note else ""
                out.append(f"{icons[i.kind]} **{i.name}**{where}{note}")
            return out

        sections = [
            ("🆕 Будет создано" if not applied else "🆕 Создано", lines(plan.by("create")), style.Colors.SECONDARY),
            ("🔗 Используются существующие (без изменений)", lines(plan.by("link") + plan.by("saved")),
             style.Colors.PRIMARY),
            ("❓ Сомнительные совпадения — НЕ трогаются", lines(plan.by("doubt")), style.Colors.WARNING),
            ("🗑️ Пустые лишние каналы — будут удалены" if not applied else "🗑️ Удалены пустые лишние каналы",
             [f"{'📁' if isinstance(c, discord.CategoryChannel) else '#'} **{c.name}**" for c in plan.removals],
             style.Colors.DANGER),
        ]
        embeds = []
        head = style.base("План настройки" if not applied else "Настройка применена",
                          ("Режим **DRY RUN**: ничего не изменено.\n" if not applied else "") +
                          "Каналы раскладываются по категориям в порядке шаблона. Каналы вне шаблона "
                          "не удаляются и не изменяются. Ваши роли не переименовываются.\n"
                          "Сомнительные совпадения можно привязать командой `/config link`.",
                          color=style.Colors.PRIMARY, emoji="🛠️")
        head.add_field(name="Удалить пустых", value=str(len(plan.removals)))
        head.add_field(name="Создать", value=str(len(plan.by("create"))))
        head.add_field(name="Использовать", value=str(len(plan.by("link")) + len(plan.by("saved"))))
        head.add_field(name="Сомнительно", value=str(len(plan.by("doubt"))))
        if plan.panels:
            head.add_field(name="Панели к публикации",
                           value=", ".join(f"`{p}` → {c}" for p, c in plan.panels), inline=False)
        if plan.warnings:
            head.add_field(name="⚠️ Предупреждения", value="\n".join(plan.warnings), inline=False)
        embeds.append(head)
        for heading, rows, color in sections:
            if not rows:
                continue
            chunk = ""
            for row in rows:
                if len(chunk) + len(row) > 3900:
                    embeds.append(discord.Embed(title=heading, description=chunk, color=color))
                    chunk = ""
                chunk += row + "\n"
            embeds.append(discord.Embed(title=heading, description=chunk, color=color))
        return embeds

    # ------------------------------------------------------------------ применение
    def _overwrites(self, guild: discord.Guild, spec_map: dict, roles: dict[str, discord.Role]
                    ) -> dict[discord.abc.Snowflake, discord.PermissionOverwrite]:
        result: dict[discord.abc.Snowflake, discord.PermissionOverwrite] = {}
        for target, perms in spec_map.items():
            if target == "@everyone":
                targets = [guild.default_role]
            elif target == "@staff":
                targets = [roles[k] for k in STAFF_KEYS if k in roles]
            elif target.startswith("@level:"):
                targets = [roles[k] for k in keys_with_level(int(target.split(":")[1])) if k in roles]
            else:
                targets = [roles[target]] if target in roles else []
            for t in targets:
                ow = result.get(t, discord.PermissionOverwrite())
                ow.update(**perms)
                result[t] = ow
        # Бот всегда должен видеть свои каналы
        result[guild.me] = discord.PermissionOverwrite(
            view_channel=True, send_messages=True, manage_channels=True, manage_messages=True, embed_links=True,
            attach_files=True, read_message_history=True, connect=True, move_members=True, manage_permissions=True)
        return result

    async def apply_plan(self, guild: discord.Guild, plan: Plan, actor: discord.Member) -> list[str]:
        errors: list[str] = []
        reg = self.bot.registry
        reason = f"POLARI /setup by {actor} ({actor.id})"
        roles: dict[str, discord.Role] = {}
        created_ids = {int(k.split(".", 1)[1]) for k in await reg.load(guild.id) if k.startswith("created.")}

        # Пустые лишние каналы (сначала каналы, потом категории)
        for ch in sorted(plan.removals, key=lambda c: isinstance(c, discord.CategoryChannel)):
            try:
                await ch.delete(reason=reason)
            except discord.HTTPException as exc:
                errors.append(f"Удаление {ch.name}: {exc}")
            await asyncio.sleep(0.35)

        # Роли
        for item in [i for i in plan.items if i.kind == "role"]:
            if item.action in ("link", "saved"):
                roles[item.key] = item.target  # type: ignore[assignment]
                await reg.set(guild.id, f"role.{item.key}", item.target.id)  # type: ignore[union-attr]
            elif item.action == "create":
                spec = item.spec
                try:
                    role = await guild.create_role(name=spec.name, colour=discord.Colour(spec.color), hoist=spec.hoist,
                                                   permissions=discord.Permissions(**spec.perms), reason=reason)
                except discord.Forbidden:
                    try:
                        role = await guild.create_role(name=spec.name, colour=discord.Colour(spec.color),
                                                       hoist=spec.hoist, reason=reason)
                        errors.append(f"Роль {spec.name} создана без прав (бот не может выдать эти права).")
                    except discord.HTTPException as exc:
                        errors.append(f"Роль {spec.name}: {exc}")
                        continue
                except discord.HTTPException as exc:
                    errors.append(f"Роль {spec.name}: {exc}")
                    continue
                roles[item.key] = role
                item.target = role
                await reg.set(guild.id, f"role.{item.key}", role.id)
                await reg.set(guild.id, f"created.{role.id}", "role")
                await asyncio.sleep(0.4)

        # Категории и каналы
        categories: dict[str, discord.CategoryChannel] = {}
        cat_created: set[str] = set()
        for item in [i for i in plan.items if i.kind == "category"]:
            spec: CategorySpec = item.spec  # type: ignore[assignment]
            if item.action in ("link", "saved"):
                categories[item.key] = item.target  # type: ignore[assignment]
                await reg.set(guild.id, f"channel.{item.key}", item.target.id)  # type: ignore[union-attr]
                if item.target.id in created_ids:  # type: ignore[union-attr]
                    # категория создана ботом ранее — приводим права к актуальному шаблону
                    try:
                        await item.target.edit(overwrites=self._overwrites(guild, spec.overwrites, roles),  # type: ignore[union-attr]
                                               reason=reason)
                    except discord.HTTPException as exc:
                        errors.append(f"Права {spec.name}: {exc}")
            elif item.action == "create":
                try:
                    cat = await guild.create_category(spec.name, overwrites=self._overwrites(guild, spec.overwrites, roles),
                                                      reason=reason)
                except discord.HTTPException as exc:
                    errors.append(f"Категория {spec.name}: {exc}")
                    continue
                categories[item.key] = cat
                cat_created.add(item.key)
                item.target = cat
                await reg.set(guild.id, f"channel.{item.key}", cat.id)
                await reg.set(guild.id, f"created.{cat.id}", "channel")
                await asyncio.sleep(0.4)

        cat_specs = {c.key: c for c in CATEGORIES}
        for item in [i for i in plan.items if i.kind in ("text", "voice")]:
            spec: ChannelSpec = item.spec  # type: ignore[no-redef]
            parent = categories.get(item.parent or "")
            cat_spec = cat_specs[item.parent] if item.parent else None
            # Права: категорийные + канальные; в существующей категории канал получает полный набор явно
            merged = dict(cat_spec.overwrites) if cat_spec else {}
            for target, perms in spec.overwrites.items():
                merged[target] = {**merged.get(target, {}), **perms}
            overwrites = self._overwrites(guild, merged, roles)
            if item.action in ("link", "saved"):
                await reg.set(guild.id, f"channel.{item.key}", item.target.id)  # type: ignore[union-attr]
                changes: dict = {}
                if parent and item.target.category_id != parent.id:  # type: ignore[union-attr]
                    changes["category"] = parent  # раскладываем по нужной категории
                if item.target.id in created_ids:  # type: ignore[union-attr]
                    changes["overwrites"] = overwrites  # свой канал — права по шаблону
                if changes:
                    try:
                        await item.target.edit(reason=reason, **changes)  # type: ignore[union-attr]
                    except discord.HTTPException as exc:
                        errors.append(f"Канал {spec.name}: {exc}")
                    await asyncio.sleep(0.4)
                continue
            if item.action != "create":
                continue
            try:
                if spec.kind == "voice":
                    ch = await guild.create_voice_channel(spec.name, category=parent, overwrites=overwrites,
                                                          user_limit=spec.user_limit, reason=reason)
                else:
                    ch = await guild.create_text_channel(spec.name, category=parent, overwrites=overwrites,
                                                         topic=spec.topic or None, slowmode_delay=spec.slowmode,
                                                         reason=reason)
            except discord.HTTPException as exc:
                errors.append(f"Канал {spec.name}: {exc}")
                continue
            item.target = ch
            await reg.set(guild.id, f"channel.{item.key}", ch.id)
            await reg.set(guild.id, f"created.{ch.id}", "channel")
            await asyncio.sleep(0.4)

        # Порядок категорий как в шаблоне: Знакомство, Поддержка, Тикеты, Прихожая, Общение, Персонал, Логи
        for index, cat_spec in enumerate(CATEGORIES):
            cat = categories.get(cat_spec.key)
            if cat is None:
                continue
            try:
                await cat.edit(position=index, reason=reason)
            except discord.HTTPException as exc:
                errors.append(f"Порядок категории {cat.name}: {exc}")
            await asyncio.sleep(0.4)

        # Порядок каналов внутри категорий как в шаблоне (текстовые сверху, голосовые ниже)
        for cat_spec in CATEGORIES:
            parent = categories.get(cat_spec.key)
            if parent is None:
                continue
            wanted = [await reg.channel(guild, c.key) for c in cat_spec.channels]
            for is_voice in (False, True):
                order = [c for c in wanted if c is not None and c.category_id == parent.id
                         and isinstance(c, (discord.VoiceChannel, discord.StageChannel)) == is_voice]
                current = [c for c in (parent.voice_channels if is_voice else parent.text_channels) if c in order]
                if [c.id for c in current] == [c.id for c in order]:
                    continue
                previous = None
                for ch in order:
                    try:
                        if previous is None:
                            await ch.move(beginning=True, category=parent, reason=reason)
                        else:
                            await ch.move(after=previous, category=parent, reason=reason)
                    except (discord.HTTPException, ValueError) as exc:
                        errors.append(f"Порядок канала {ch.name}: {exc}")
                    previous = ch
                    await asyncio.sleep(0.3)

        # Панели
        for panel_key, channel_key in plan.panels:
            builder = self.panel_builders.get(panel_key)
            channel = await reg.text(guild, channel_key)
            if not builder or not channel:
                errors.append(f"Панель {panel_key}: канал {channel_key} не найден.")
                continue
            try:
                msg = await builder(channel)
                await self.bot.db.save_panel(guild.id, panel_key, channel.id, msg.id)
            except discord.HTTPException as exc:
                errors.append(f"Панель {panel_key}: {exc}")
            await asyncio.sleep(0.4)

        await reg.set(guild.id, "setup.applied_at", now_iso())
        await reg.set(guild.id, "setup.applied_by", actor.id)
        embed = style.log_embed("Настройка сервера применена", emoji="🛠️", fields=[
            ("Администратор", actor.mention),
            ("Создано", str(len(plan.by("create")))),
            ("Привязано", str(len(plan.by("link")))),
            ("Ошибки", "\n".join(errors)[:1000] or "нет"),
        ])
        await self.bot.logs.send(guild, "setup", embed)
        return errors

    # ------------------------------------------------------------------ бэкап структуры
    async def snapshot(self, guild: discord.Guild, reason: str) -> bool:
        """Сохраняет все категории, каналы и роли (названия, порядок, права) в JSON.

        Файл кладётся в data/structure/ и отправляется в канал бэкапов и в ⚙️・сервер-лог.
        """
        def overwrites(ch: discord.abc.GuildChannel) -> list[dict]:
            out = []
            for target, ow in ch.overwrites.items():
                allow, deny = ow.pair()
                out.append({"id": target.id, "type": "role" if isinstance(target, discord.Role) else "member",
                            "name": getattr(target, "name", ""), "allow": allow.value, "deny": deny.value})
            return out

        data = {
            "guild": {"id": guild.id, "name": guild.name}, "reason": reason, "created_at": now_iso(),
            "roles": [{"id": r.id, "name": r.name, "color": r.colour.value, "position": r.position,
                       "permissions": r.permissions.value, "hoist": r.hoist, "mentionable": r.mentionable}
                      for r in guild.roles],
            "channels": [{"id": c.id, "name": c.name, "type": str(c.type), "position": c.position,
                          "category_id": c.category_id, "topic": getattr(c, "topic", None),
                          "slowmode": getattr(c, "slowmode_delay", None), "user_limit": getattr(c, "user_limit", None),
                          "overwrites": overwrites(c)} for c in guild.channels],
        }
        payload = json.dumps(data, ensure_ascii=False, indent=1).encode("utf-8")
        stamp = discord.utils.utcnow().strftime("%Y%m%d-%H%M%S")
        name = f"structure-{guild.id}-{stamp}.json"
        folder = self.bot.db.path.parent / "structure"
        folder.mkdir(parents=True, exist_ok=True)
        (folder / name).write_bytes(payload)
        sent = False
        targets = [await self.bot.registry.text(guild, "log_server")]
        backup_channel = await self.bot.hosting._backup_channel()
        if backup_channel and backup_channel.guild.id == guild.id:
            targets.append(backup_channel)
        for channel in {t.id: t for t in targets if t}.values():
            try:
                await channel.send(content=f"🗂️ Бэкап структуры сервера · {reason} · каналов: {len(guild.channels)}, "
                                           f"ролей: {len(guild.roles)}",
                                   file=discord.File(io.BytesIO(payload), filename=name))
                sent = True
            except discord.HTTPException as exc:
                log.warning("Бэкап структуры не отправлен в #%s: %s", channel, exc)
        log.info("Бэкап структуры %s сохранён (%s)", name, reason)
        return sent

    @app_commands.command(name="structure_backup", description="Сохранить бэкап всех каналов и ролей сервера")
    @app_commands.guild_only()
    @require("config")
    async def structure_backup(self, interaction: discord.Interaction) -> None:
        await interaction.response.defer(ephemeral=True)
        sent = await self.snapshot(interaction.guild, f"вручную, {interaction.user}")  # type: ignore[arg-type]
        await interaction.followup.send(embed=style.success("Бэкап структуры", (
            "Все категории, каналы, роли и права сохранены в JSON" +
            (" и отправлены в канал логов/бэкапов." if sent else " на сервере бота (канал логов не найден)."))),
            ephemeral=True)

    # ------------------------------------------------------------------ сброс
    async def collect_created(self, guild: discord.Guild) -> tuple[list[discord.Role], list[discord.abc.GuildChannel]]:
        """Всё, что создал бот: записи в БД + аудит-лог (создано пользователем-ботом)."""
        ids: set[int] = {int(k.split(".", 1)[1]) for k in (await self.bot.registry.load(guild.id))
                         if k.startswith("created.")}
        if guild.me.guild_permissions.view_audit_log:
            for action in (discord.AuditLogAction.channel_create, discord.AuditLogAction.role_create):
                try:
                    async for entry in guild.audit_logs(limit=500, action=action, user=guild.me):
                        if entry.target:
                            ids.add(entry.target.id)
                except discord.HTTPException as exc:
                    log.warning("Аудит-лог недоступен: %s", exc)
        roles = [r for r in guild.roles if r.id in ids and not r.managed and r < guild.me.top_role]
        channels = [c for c in guild.channels if c.id in ids]
        return roles, channels

    async def show_reset(self, interaction: discord.Interaction) -> None:
        guild = interaction.guild
        assert guild
        roles, channels = await self.collect_created(guild)
        cats = [c for c in channels if isinstance(c, discord.CategoryChannel)]
        others = [c for c in channels if not isinstance(c, discord.CategoryChannel)]
        text = (f"**Роли ({len(roles)}):** " + (", ".join(r.name for r in roles) or "—")
                + f"\n\n**Категории ({len(cats)}):** " + (", ".join(c.name for c in cats) or "—")
                + f"\n\n**Каналы ({len(others)}):** " + (", ".join(c.name for c in others) or "—")
                + "\n\nТакже будут удалены сообщения-панели бота в остальных каналах и сброшены привязки. "
                "Заказы, наказания и история собеседований в базе **сохранятся**.\n"
                "> ⚠️ Удаление необратимо. Объекты, созданные не ботом, не затрагиваются.")
        await interaction.followup.send(embed=style.warning("Сброс POLARI", text[:4000]),
                                        view=ConfirmResetView(self, interaction.user.id), ephemeral=True)

    async def apply_reset(self, guild: discord.Guild, actor: discord.Member) -> tuple[int, int, list[str]]:
        roles, channels = await self.collect_created(guild)
        created_ids = {c.id for c in channels}
        errors: list[str] = []
        reason = f"POLARI /setup reset by {actor} ({actor.id})"
        rows = await self.bot.db.fetchall("SELECT channel_id FROM panels WHERE guild_id=?", (guild.id,))
        for cid in {r["channel_id"] for r in rows} - created_ids:
            ch = guild.get_channel(cid)
            if isinstance(ch, discord.TextChannel):
                try:
                    await ch.purge(limit=100, check=lambda m: m.author.id == guild.me.id, reason=reason)
                except discord.HTTPException as exc:
                    errors.append(f"Панели в #{ch.name}: {exc}")
        channels.sort(key=lambda c: isinstance(c, discord.CategoryChannel))  # сначала каналы, потом категории
        deleted_ch = 0
        for ch in channels:
            try:
                await ch.delete(reason=reason)
                deleted_ch += 1
            except discord.HTTPException as exc:
                errors.append(f"Канал {ch.name}: {exc}")
            await asyncio.sleep(0.35)
        deleted_roles = 0
        for role in roles:
            try:
                await role.delete(reason=reason)
                deleted_roles += 1
            except discord.HTTPException as exc:
                errors.append(f"Роль {role.name}: {exc}")
            await asyncio.sleep(0.35)
        await self.bot.db.execute("DELETE FROM guild_settings WHERE guild_id=? AND (key LIKE 'role.%' OR "
                                  "key LIKE 'channel.%' OR key LIKE 'created.%' OR key LIKE 'setup.%')", (guild.id,))
        await self.bot.db.execute("DELETE FROM panels WHERE guild_id=?", (guild.id,))
        self.bot.registry._cache.pop(guild.id, None)
        return deleted_ch, deleted_roles, errors

    # ------------------------------------------------------------------ команда
    @app_commands.command(name="setup", description="Настройка сервера POLARI: план (DRY RUN) или применение")
    @app_commands.describe(mode="dry_run — только показать план; apply — показать план и применить после подтверждения")
    @app_commands.choices(mode=[app_commands.Choice(name="DRY RUN — показать план", value="dry_run"),
                                app_commands.Choice(name="APPLY — применить после подтверждения", value="apply"),
                                app_commands.Choice(name="RESET — удалить всё, что создал POLARI", value="reset")])
    @app_commands.guild_only()
    @require("config")
    async def setup_cmd(self, interaction: discord.Interaction, mode: app_commands.Choice[str]) -> None:
        await interaction.response.defer(ephemeral=True, thinking=True)
        guild = interaction.guild
        assert guild
        if mode.value == "reset":
            await self.show_reset(interaction)
            return
        plan = await self.build_plan(guild)
        embeds = self.render_plan(plan)
        if mode.value == "dry_run":
            for i in range(0, len(embeds), 10):
                await interaction.followup.send(embeds=embeds[i:i + 10], ephemeral=True)
            return
        view = ConfirmSetupView(self, plan, interaction.user.id)  # type: ignore[arg-type]
        for i in range(0, len(embeds), 10):
            last = i + 10 >= len(embeds)
            if last:
                await interaction.followup.send(embeds=embeds[i:i + 10], view=view, ephemeral=True)
            else:
                await interaction.followup.send(embeds=embeds[i:i + 10], ephemeral=True)


def _signature(plan: Plan) -> tuple:
    return (tuple((i.key, i.action, getattr(i.target, "id", None)) for i in plan.items) + tuple(plan.panels)
            + tuple(c.id for c in plan.removals))


class ConfirmResetView(discord.ui.View):
    def __init__(self, cog: SetupCog, author_id: int):
        super().__init__(timeout=180)
        self.cog, self.author_id = cog, author_id

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if interaction.user.id != self.author_id:
            await interaction.response.send_message(embed=style.error("Подтвердить может только автор команды."),
                                                    ephemeral=True)
            return False
        return True

    @discord.ui.button(label="Удалить созданное POLARI", emoji="🗑️", style=discord.ButtonStyle.danger)
    async def confirm(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        if self.cog._lock.locked():
            await interaction.response.send_message(embed=style.error("Настройка уже выполняется."), ephemeral=True)
            return
        self.stop()
        await interaction.response.edit_message(embed=style.base("Сброс", "Удаляю созданное ботом…", emoji="⏳"),
                                                view=None)
        async with self.cog._lock:
            await self.cog.snapshot(interaction.guild, f"перед RESET, {interaction.user}")  # type: ignore[arg-type]
            ch, roles, errors = await self.cog.apply_reset(interaction.guild, interaction.user)  # type: ignore[arg-type]
        text = f"Удалено каналов: **{ch}**, ролей: **{roles}**.\nТеперь выполните `/setup` → APPLY."
        if errors:
            text += "\n\n**Ошибки:**\n" + "\n".join(f"• {e}" for e in errors)[:3000]
        try:
            await interaction.followup.send(embed=style.success("Сброс выполнен", text), ephemeral=True)
        except discord.HTTPException:
            pass  # канал, где вызвали команду, мог быть удалён

    @discord.ui.button(label="Отмена", emoji="✖", style=discord.ButtonStyle.secondary)
    async def cancel(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        self.stop()
        await interaction.response.edit_message(embed=style.base("Сброс отменён", "Ничего не удалено."), view=None)


class ConfirmSetupView(discord.ui.View):
    def __init__(self, cog: SetupCog, plan: Plan, author_id: int):
        super().__init__(timeout=300)
        self.cog, self.plan, self.author_id = cog, plan, author_id

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if interaction.user.id != self.author_id:
            await interaction.response.send_message(embed=style.error("Подтвердить может только автор команды."),
                                                    ephemeral=True)
            return False
        return True

    @discord.ui.button(label="Применить", emoji="✅", style=discord.ButtonStyle.success)
    async def confirm(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        if self.cog._lock.locked():
            await interaction.response.send_message(embed=style.error("Настройка уже выполняется."), ephemeral=True)
            return
        self.stop()
        await interaction.response.edit_message(
            embeds=[style.base("Применяю настройку", "Создаю роли, каналы и панели. Это займёт до пары минут…",
                               emoji="⏳")], view=None)
        async with self.cog._lock:
            guild = interaction.guild
            assert guild and isinstance(interaction.user, discord.Member)
            # Пересчёт плана прямо перед применением: если сервер изменился — не применяем
            plan = await self.cog.build_plan(guild)
            if _signature(plan) != _signature(self.plan):
                await interaction.followup.send(embed=style.warning(
                    "Сервер изменился", "С момента показа плана на сервере что-то изменилось. "
                    "Ничего не применено — запустите `/setup` ещё раз и проверьте новый план."), ephemeral=True)
                return
            await self.cog.snapshot(guild, f"перед APPLY, {interaction.user}")
            errors = await self.cog.apply_plan(guild, plan, interaction.user)
        embeds = self.cog.render_plan(plan, applied=True)
        if errors:
            embeds.insert(1, style.warning("Ошибки при применении", "\n".join(f"• {e}" for e in errors)[:4000]))
        try:
            for i in range(0, len(embeds), 10):
                await interaction.followup.send(embeds=embeds[i:i + 10], ephemeral=True)
        except discord.HTTPException:
            log.info("Отчёт /setup не доставлен (канал команды мог быть удалён). Ошибки: %s", errors)
        if errors:
            await self.cog.bot.logs.send(interaction.guild, "setup", style.warning(  # type: ignore[arg-type]
                "Ошибки /setup", "\n".join(f"• {e}" for e in errors)[:4000]))

    @discord.ui.button(label="Отмена", emoji="✖", style=discord.ButtonStyle.secondary)
    async def cancel(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        self.stop()
        await interaction.response.edit_message(embeds=[style.base("Настройка отменена", "Ничего не изменено.")],
                                                view=None)


async def setup(bot: "PolariBot") -> None:
    await bot.add_cog(SetupCog(bot))
