"""Магазин POLARI: каталог, покупка через приватный тикет, ручное подтверждение оплаты сотрудником.

Платёжной интеграции нет: нажатие «Купить» или сообщение покупателя НЕ является доказательством оплаты.
Оплату подтверждает только уполномоченный сотрудник магазина кнопкой «Подтвердить оплату».
"""
from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone
from typing import TYPE_CHECKING

import discord
from discord import app_commands
from discord.ext import commands, tasks

from bot.cogs.tickets import AddStaffSelect, CloseModal
from bot.data.content import CURRENCY, SHOP_BY_ID, SHOP_ITEMS, ShopItem
from bot.database.db import now_iso
from bot.embeds import style
from bot.services.tickets import TicketError


class OrderError(Exception):
    """Заказ не создан — текст можно показать покупателю."""
from bot.utils.checks import ensure, require
from bot.utils.helpers import parse_color, redact

if TYPE_CHECKING:
    from bot.core import PolariBot

log = logging.getLogger("polari.shop")
STATUS = {"pending": "🕓 Ожидает оплаты", "paid": "✅ Оплачен", "rejected": "✖ Оплата отклонена",
          "cancelled": "🚫 Отменён"}


def price_text(item: ShopItem) -> str:
    period = f" / {item.period_days} дн." if item.period_days else " навсегда"
    return f"{item.price:,} {CURRENCY}{period}".replace(",", " ")


class ShopCog(commands.Cog):
    def __init__(self, bot: "PolariBot"):
        self.bot = bot

    async def cog_load(self) -> None:
        self.bot.add_view(ShopView(self))
        self.bot.add_view(OrderControlView(self))
        self.bot.get_cog("SetupCog").register_panel("shop", self.post_panel)  # type: ignore[union-attr]
        self.expire_loop.start()

    async def cog_unload(self) -> None:
        self.expire_loop.cancel()

    # ------------------------------------------------------------------ витрина
    async def post_panel(self, channel: discord.TextChannel) -> discord.Message:
        header = style.header("Магазин", (
            "Хотите поддержать **POLARI** и выделиться? Выберите товар и нажмите **«Купить»**.\n"
            f"{style.THIN}\n"
            "**`1`** Бот откроет приватный тикет заказа.\n"
            "**`2`** Сотрудник магазина согласует с вами способ оплаты.\n"
            "**`3`** После подтверждения оплаты роль выдаётся автоматически.\n"
            f"{style.THIN}\n> Никогда не платите людям вне тикета заказа — это мошенники."),
            style.Colors.PRIMARY)
        embeds = [header]
        for item in SHOP_ITEMS:
            card = discord.Embed(title=f"{item.emoji}  {item.name}", description=item.description,
                                 color=style.Colors.ACCENT if item.custom else style.Colors.SECONDARY)
            card.add_field(name="Стоимость", value=f"**{price_text(item)}**")
            if item.custom:
                card.add_field(name="Особенность", value="Название и цвет выбираете вы")
            embeds.append(card)
        return await style.send_panel(channel, "shop", embeds[:9], view=ShopView(self))

    # ------------------------------------------------------------------ заказы
    async def create_order(self, interaction: discord.Interaction, item: ShopItem,
                           custom_name: str | None = None, custom_color: str | None = None) -> None:
        member = interaction.user
        assert isinstance(member, discord.Member)
        try:
            order_id, channel = await self.place_order(member, item, custom_name, custom_color)
        except OrderError as exc:
            await interaction.followup.send(embed=style.error(str(exc)), ephemeral=True)
            return
        await interaction.followup.send(embed=style.success("Заказ оформлен", (
            f"Заказ **#{order_id}** создан. Перейдите в {channel.mention} — сотрудник магазина свяжется с вами.")),
            ephemeral=True)

    async def place_order(self, member: discord.Member, item: ShopItem, custom_name: str | None = None,
                          custom_color: str | None = None, extra: str | None = None,
                          source: str = "discord") -> tuple[int, discord.TextChannel]:
        """Создаёт заказ и приватный тикет. Используется и Discord-магазином, и сайтом."""
        guild = member.guild
        db = self.bot.db
        pending = await db.fetchone("SELECT id FROM orders WHERE guild_id=? AND user_id=? AND item_id=? AND status='pending'",
                                    (guild.id, member.id, item.id))
        if pending:
            raise OrderError(f"У вас уже есть незавершённый заказ #{pending['id']} на этот товар.")
        now = now_iso()
        order_id = await db.execute(
            "INSERT INTO orders (guild_id,user_id,item_id,item_name,price,custom_name,custom_color,extra,source,"
            "created_at,updated_at) VALUES (?,?,?,?,?,?,?,?,?,?,?)",
            (guild.id, member.id, item.id, item.name, item.price, custom_name, custom_color, extra, source, now, now))
        intro = self.order_embed(member, item, order_id, custom_name, custom_color)
        if extra:
            intro.add_field(name="SteamID / ник в игре", value=extra[:200], inline=False)
        if source == "site":
            intro.add_field(name="Источник", value="🌐 Заказ оформлен на сайте", inline=False)
        try:
            channel, ticket_id = await self.bot.tickets.create(member, "purchase", f"Заказ #{order_id}: {item.name}",
                                                               intro=intro, view=OrderControlView(self))
        except (TicketError, discord.HTTPException) as exc:
            await db.execute("UPDATE orders SET status='cancelled', updated_at=? WHERE id=?", (now_iso(), order_id))
            await db.execute("INSERT INTO payments (order_id,action,note,created_at) VALUES (?,?,?,?)",
                             (order_id, "ticket_failed", str(exc)[:200], now_iso()))
            raise OrderError(f"Не удалось открыть тикет заказа: {exc}") from exc
        await db.execute("UPDATE orders SET ticket_id=? WHERE id=?", (ticket_id, order_id))
        await db.execute("INSERT INTO payments (order_id,action,staff_id,note,created_at) VALUES (?,?,?,?,?)",
                         (order_id, "created", None, None, now_iso()))
        await self.bot.logs.send(guild, "shop", style.log_embed("Новый заказ", emoji="🛒", fields=[
            ("Заказ", f"#{order_id}"), ("Покупатель", member.mention), ("Товар", f"{item.emoji} {item.name}"),
            ("Стоимость", price_text(item)), ("Тикет", channel.mention),
        ]))
        orders_channel = await self.bot.registry.text(guild, "orders")
        if orders_channel:
            await orders_channel.send(embed=style.base("Новый заказ", (
                f"**#{order_id}** · {item.emoji} {item.name} · {price_text(item)}\n"
                f"Покупатель: {member.mention} · Тикет: {channel.mention}"), color=style.Colors.ACCENT, emoji="🛒"))
        return order_id, channel

    def order_embed(self, member: discord.Member, item: ShopItem, order_id: int, custom_name: str | None,
                    custom_color: str | None) -> discord.Embed:
        embed = style.base(f"Заказ #{order_id}", (
            f"{member.mention}, спасибо за интерес к магазину POLARI!\n"
            "Сотрудник магазина скоро свяжется с вами здесь и согласует оплату.\n"
            "> ⚠️ Оплата подтверждается **только** сотрудником магазина в этом тикете."),
            color=style.Colors.SECONDARY, emoji="💎")
        embed.add_field(name="Товар", value=f"{item.emoji} **{item.name}**")
        embed.add_field(name="Стоимость", value=price_text(item))
        embed.add_field(name="Статус", value=STATUS["pending"])
        if custom_name:
            embed.add_field(name="Название роли", value=custom_name)
            embed.add_field(name="Цвет роли", value=f"#{custom_color}")
        embed.add_field(name="Покупатель", inline=False, value=(
            f"{member.mention} · `{member.id}`\nАккаунт создан {style.ts(member.created_at, 'R')} · "
            f"на сервере с {style.ts(member.joined_at, 'R') if member.joined_at else '—'}"))
        embed.set_thumbnail(url=member.display_avatar.url)
        return embed

    async def order_for_channel(self, channel_id: int):
        return await self.bot.db.fetchone(
            "SELECT o.* FROM orders o JOIN tickets t ON t.id=o.ticket_id WHERE t.channel_id=? ORDER BY o.id DESC LIMIT 1",
            (channel_id,))

    async def confirm_payment(self, interaction: discord.Interaction, order, note: str) -> None:
        guild = interaction.guild
        assert guild
        db = self.bot.db
        item = SHOP_BY_ID.get(order["item_id"])
        member = guild.get_member(order["user_id"])
        if not item or not member:
            await interaction.followup.send(embed=style.error("Товар или покупатель больше не найдены."), ephemeral=True)
            return
        if item.kind == "cs2":
            await self._confirm_cs2(interaction, order, member, note)
            return
        created_role: discord.Role | None = None
        try:
            if item.custom:
                role = created_role = await guild.create_role(
                    name=order["custom_name"], colour=discord.Colour(int(order["custom_color"], 16)),
                    reason=f"POLARI: кастомная роль, заказ #{order['id']}")
                anchor = await self.bot.registry.role(guild, item.custom_above or "")
                if anchor and anchor.position + 1 < guild.me.top_role.position:
                    try:
                        await role.edit(position=anchor.position + 1)
                    except discord.HTTPException:
                        log.warning("Не удалось поднять кастомную роль заказа #%s", order["id"])
            else:
                role = await self.bot.registry.role(guild, item.role_key or "")
                if not role:
                    raise RuntimeError(f"Роль товара «{item.name}» не настроена (/setup или /config link).")
                if role >= guild.me.top_role:
                    raise RuntimeError("Роль товара выше роли бота. Поднимите роль POLARI.")
            await member.add_roles(role, reason=f"POLARI: оплачен заказ #{order['id']} ({interaction.user})")
        except (discord.HTTPException, RuntimeError) as exc:
            if created_role:
                try:  # откатываем только что созданную ботом роль, чтобы не оставлять мусор
                    await created_role.delete(reason="POLARI: откат неудачной выдачи")
                except discord.HTTPException:
                    pass
            await interaction.followup.send(embed=style.error(f"Роль не выдана, заказ остаётся неоплаченным: {exc}"),
                                            ephemeral=True)
            return
        expires = (datetime.now(timezone.utc) + timedelta(days=item.period_days)).isoformat(timespec="seconds") \
            if item.period_days else None
        await db.execute("UPDATE orders SET status='paid', handled_by=?, role_id=?, expires_at=?, updated_at=? WHERE id=?",
                         (interaction.user.id, role.id, expires, now_iso(), order["id"]))
        await db.execute("INSERT INTO payments (order_id,action,staff_id,note,created_at) VALUES (?,?,?,?,?)",
                         (order["id"], "confirmed", interaction.user.id, redact(note), now_iso()))
        await db.log_role_grant(guild.id, member.id, role.id, "add", f"order:{order['id']}", interaction.user.id, expires)
        await interaction.channel.send(embed=style.success("Оплата подтверждена", (  # type: ignore[union-attr]
            f"{member.mention}, спасибо за поддержку POLARI! 💙\nВыдана роль {role.mention}"
            + (f" до {style.ts(expires, 'D')}." if expires else " навсегда.")
            + "\nТикет можно закрыть кнопкой «Закрыть тикет».")))
        await self.bot.logs.send(guild, "payment", style.log_embed("Оплата подтверждена", color=style.Colors.SUCCESS,
                                                                   emoji="💳", fields=[
            ("Заказ", f"#{order['id']}"), ("Покупатель", member.mention), ("Товар", order["item_name"]),
            ("Сумма", f"{order['price']} {CURRENCY}"), ("Подтвердил", interaction.user.mention),
            ("Роль", role.mention), ("Действует до", style.ts(expires) if expires else "навсегда"),
            ("Комментарий", redact(note) or "—"),
        ]))
        try:
            await member.send(embed=style.success("Покупка активирована",
                                                  f"Заказ **#{order['id']}** ({order['item_name']}) на **{guild.name}** оплачен."))
        except discord.HTTPException:
            pass

    async def _confirm_cs2(self, interaction: discord.Interaction, order, member: discord.Member, note: str) -> None:
        """Привилегия CS 2 выдаётся на игровом сервере сотрудником; бот фиксирует оплату и срок."""
        item = SHOP_BY_ID[order["item_id"]]
        expires = (datetime.now(timezone.utc) + timedelta(days=item.period_days)).isoformat(timespec="seconds") \
            if item.period_days else None
        await self.bot.db.execute("UPDATE orders SET status='paid', handled_by=?, expires_at=?, updated_at=? WHERE id=?",
                                  (interaction.user.id, expires, now_iso(), order["id"]))
        await self.bot.db.execute("INSERT INTO payments (order_id,action,staff_id,note,created_at) VALUES (?,?,?,?,?)",
                                  (order["id"], "confirmed", interaction.user.id, redact(note), now_iso()))
        await interaction.channel.send(embed=style.success("Оплата подтверждена", (  # type: ignore[union-attr]
            f"{member.mention}, спасибо! Привилегия **{order['item_name']}** будет выдана на сервере CS 2 "
            f"для **{order['extra'] or 'указанного аккаунта'}**"
            + (f" до {style.ts(expires, 'D')}." if expires else ".")
            + f"\n{interaction.user.mention}, выдайте привилегию на игровом сервере и закройте тикет.")))
        await self.bot.logs.send(interaction.guild, "payment", style.log_embed(  # type: ignore[arg-type]
            "Оплата подтверждена (CS 2)", color=style.Colors.SUCCESS, emoji="🎮", fields=[
                ("Заказ", f"#{order['id']}"), ("Покупатель", member.mention), ("Товар", order["item_name"]),
                ("Сумма", f"{order['price']} {CURRENCY}"), ("SteamID", order["extra"] or "—"),
                ("Подтвердил", interaction.user.mention), ("Комментарий", redact(note) or "—")]))

    async def set_order_status(self, interaction: discord.Interaction, order, status: str, action: str,
                               reason: str) -> None:
        db = self.bot.db
        await db.execute("UPDATE orders SET status=?, handled_by=?, updated_at=? WHERE id=?",
                         (status, interaction.user.id, now_iso(), order["id"]))
        await db.execute("INSERT INTO payments (order_id,action,staff_id,note,created_at) VALUES (?,?,?,?,?)",
                         (order["id"], action, interaction.user.id, redact(reason), now_iso()))
        title = "Оплата отклонена" if status == "rejected" else "Заказ отменён"
        await interaction.channel.send(embed=style.base(title, (  # type: ignore[union-attr]
            f"Заказ **#{order['id']}** · {order['item_name']}\n**Причина:** {reason}\n"
            f"Действие выполнил {interaction.user.mention}."), color=style.Colors.WARNING, emoji="📝"))
        await self.bot.logs.send(interaction.guild, "payment", style.log_embed(title, color=style.Colors.DANGER,  # type: ignore[arg-type]
                                                                               emoji="💳", fields=[
            ("Заказ", f"#{order['id']}"), ("Покупатель", f"<@{order['user_id']}>"), ("Товар", order["item_name"]),
            ("Кто", interaction.user.mention), ("Причина", redact(reason)),
        ]))

    # ------------------------------------------------------------------ истечение
    @tasks.loop(minutes=10)
    async def expire_loop(self) -> None:
        now = now_iso()
        rows = await self.bot.db.fetchall(
            "SELECT * FROM role_grants WHERE active=1 AND expires_at IS NOT NULL AND expires_at <= ?", (now,))
        for grant in rows:
            guild = self.bot.get_guild(grant["guild_id"])
            await self.bot.db.execute("UPDATE role_grants SET active=0 WHERE id=?", (grant["id"],))
            if not guild:
                continue
            role = guild.get_role(grant["role_id"])
            member = guild.get_member(grant["user_id"])
            # Продлённая покупка: есть другая активная выдача этой роли с более поздним сроком
            newer = await self.bot.db.fetchval(
                "SELECT COUNT(*) FROM role_grants WHERE active=1 AND guild_id=? AND user_id=? AND role_id=? AND expires_at > ?",
                (grant["guild_id"], grant["user_id"], grant["role_id"], now), 0)
            if not role or newer:
                continue
            try:
                if member and role in member.roles:
                    await member.remove_roles(role, reason="POLARI: срок покупки истёк")
                order = await self.bot.db.fetchone("SELECT custom_name FROM orders WHERE role_id=? AND status='paid'",
                                                   (role.id,))
                if order and order["custom_name"] and not role.members:
                    await role.delete(reason="POLARI: срок кастомной роли истёк")
            except discord.HTTPException as exc:
                log.warning("Не удалось снять истёкшую роль %s: %s", grant["role_id"], exc)
                continue
            await self.bot.db.log_role_grant(guild.id, grant["user_id"], grant["role_id"], "remove", "expired", None)
            await self.bot.logs.send(guild, "role", style.log_embed("Срок покупки истёк", emoji="⌛", fields=[
                ("Участник", f"<@{grant['user_id']}>"), ("Роль", role.name)]))
            if member:
                try:
                    await member.send(embed=style.base("Срок роли истёк", (
                        f"Срок действия роли **{role.name}** на **{guild.name}** закончился. "
                        "Продлить можно в магазине сервера."), emoji="⌛"))
                except discord.HTTPException:
                    pass

    @expire_loop.before_loop
    async def _wait_ready(self) -> None:
        await self.bot.wait_until_ready()

    # ------------------------------------------------------------------ команды
    @app_commands.command(name="orders", description="История заказов магазина")
    @app_commands.describe(status="Фильтр по статусу", user="Заказы конкретного пользователя")
    @app_commands.choices(status=[app_commands.Choice(name=v, value=k) for k, v in STATUS.items()])
    @app_commands.guild_only()
    @require("shop")
    async def orders_cmd(self, interaction: discord.Interaction, status: app_commands.Choice[str] | None = None,
                         user: discord.User | None = None) -> None:
        sql, params = "SELECT * FROM orders WHERE guild_id=?", [interaction.guild_id]
        if status:
            sql += " AND status=?"
            params.append(status.value)
        if user:
            sql += " AND user_id=?"
            params.append(user.id)
        rows = await self.bot.db.fetchall(sql + " ORDER BY id DESC LIMIT 20", params)
        text = "\n".join(f"**#{r['id']}** · {r['item_name']} · <@{r['user_id']}> · {STATUS.get(r['status'])} · "
                         f"{style.ts(r['created_at'], 'd')}" for r in rows) or "Заказов нет."
        total = await self.bot.db.fetchval("SELECT SUM(price) FROM orders WHERE guild_id=? AND status='paid'",
                                           (interaction.guild_id,), 0)
        embed = style.base("История заказов", text, emoji="🧾")
        embed.add_field(name="Подтверждено оплат на сумму", value=f"{total} {CURRENCY}")
        await interaction.response.send_message(embed=embed, ephemeral=True)


class CustomRoleModal(discord.ui.Modal):
    role_name = discord.ui.TextInput(label="Название роли", max_length=32, placeholder="Например: Ледяной странник")
    color = discord.ui.TextInput(label="Цвет (HEX)", max_length=7, placeholder="#7FDBFF")

    def __init__(self, cog: ShopCog, item: ShopItem):
        super().__init__(title=f"{item.name} — POLARI"[:45])
        self.cog, self.item = cog, item

    async def on_submit(self, interaction: discord.Interaction) -> None:
        color = parse_color(self.color.value)
        if color is None:
            await interaction.response.send_message(embed=style.error("Цвет нужно указать в формате #RRGGBB."),
                                                    ephemeral=True)
            return
        name = self.role_name.value.strip()
        if "@" in name or not name:
            await interaction.response.send_message(embed=style.error("Недопустимое название роли."), ephemeral=True)
            return
        await interaction.response.defer(ephemeral=True, thinking=True)
        await self.cog.create_order(interaction, self.item, name, f"{color:06X}")


class ShopView(discord.ui.View):
    """Витрина: по кнопке на товар. custom_id статичны — кнопки работают после перезапуска."""

    def __init__(self, cog: ShopCog):
        super().__init__(timeout=None)
        self.cog = cog
        for item in SHOP_ITEMS:
            button = discord.ui.Button(label=f"Купить · {item.name}", emoji=item.emoji,
                                       style=discord.ButtonStyle.primary, custom_id=f"polari:shop:buy:{item.id}")
            button.callback = self._make_callback(item)
            self.add_item(button)
        mine = discord.ui.Button(label="Мои заказы", emoji="🧾", style=discord.ButtonStyle.secondary,
                                 custom_id="polari:shop:mine")
        mine.callback = self.my_orders
        self.add_item(mine)

    def _make_callback(self, item: ShopItem):
        async def callback(interaction: discord.Interaction) -> None:
            if item.custom:
                await interaction.response.send_modal(CustomRoleModal(self.cog, item))
                return
            await interaction.response.defer(ephemeral=True, thinking=True)
            await self.cog.create_order(interaction, item)
        return callback

    async def my_orders(self, interaction: discord.Interaction) -> None:
        rows = await self.cog.bot.db.fetchall(
            "SELECT * FROM orders WHERE guild_id=? AND user_id=? ORDER BY id DESC LIMIT 10",
            (interaction.guild_id, interaction.user.id))
        text = "\n".join(f"**#{r['id']}** · {r['item_name']} · {STATUS.get(r['status'])}"
                         + (f" · до {style.ts(r['expires_at'], 'd')}" if r["expires_at"] else "") for r in rows)
        await interaction.response.send_message(embed=style.base("Мои заказы", text or "У вас пока нет заказов.",
                                                                 emoji="🧾"), ephemeral=True)


class ReasonModal(discord.ui.Modal):
    reason = discord.ui.TextInput(label="Комментарий", style=discord.TextStyle.paragraph, max_length=300)

    def __init__(self, title: str, label: str, handler, required: bool = True):
        super().__init__(title=title)
        self.reason.label = label
        self.reason.required = required
        self.handler = handler

    async def on_submit(self, interaction: discord.Interaction) -> None:
        # Результат публикуется в тикете; ошибки — эфемерно через followup
        await interaction.response.defer()
        await self.handler(interaction, self.reason.value or "")


class OrderControlView(discord.ui.View):
    """Кнопки тикета заказа. Заказ определяется по каналу, поэтому кнопки переживают перезапуск."""

    def __init__(self, cog: ShopCog):
        super().__init__(timeout=None)
        self.cog = cog

    async def _pending(self, interaction: discord.Interaction):
        order = await self.cog.order_for_channel(interaction.channel_id)  # type: ignore[arg-type]
        if not order:
            await interaction.response.send_message(embed=style.error("Заказ для этого тикета не найден."),
                                                    ephemeral=True)
            return None
        if order["status"] != "pending":
            await interaction.response.send_message(embed=style.error(
                f"Заказ уже обработан: {STATUS.get(order['status'])}."), ephemeral=True)
            return None
        return order

    @discord.ui.button(label="Подтвердить оплату", emoji="💳", style=discord.ButtonStyle.success,
                       custom_id="polari:order:confirm", row=0)
    async def confirm(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        if not await ensure(interaction, "shop"):
            return
        order = await self._pending(interaction)
        if not order:
            return
        if order["user_id"] == interaction.user.id:
            await interaction.response.send_message(embed=style.error("Нельзя подтвердить оплату собственного заказа."),
                                                    ephemeral=True)
            return

        async def handler(inter: discord.Interaction, note: str) -> None:
            fresh = await self.cog.bot.db.fetchone("SELECT * FROM orders WHERE id=?", (order["id"],))
            if fresh["status"] != "pending":
                await inter.followup.send(embed=style.error("Заказ уже обработан другим сотрудником."), ephemeral=True)
                return
            await self.cog.confirm_payment(inter, fresh, note)

        await interaction.response.send_modal(ReasonModal(
            "Подтверждение оплаты", "Как проверена оплата (без реквизитов!)", handler))

    @discord.ui.button(label="Отклонить оплату", emoji="✖", style=discord.ButtonStyle.danger,
                       custom_id="polari:order:reject", row=0)
    async def reject(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        if not await ensure(interaction, "shop"):
            return
        order = await self._pending(interaction)
        if not order:
            return

        async def handler(inter: discord.Interaction, reason: str) -> None:
            await self.cog.set_order_status(inter, order, "rejected", "rejected", reason)

        await interaction.response.send_modal(ReasonModal("Отклонение оплаты", "Причина отклонения", handler))

    @discord.ui.button(label="Отменить заказ", emoji="🚫", style=discord.ButtonStyle.secondary,
                       custom_id="polari:order:cancel", row=1)
    async def cancel(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        order = await self._pending(interaction)
        if not order:
            return
        is_buyer = order["user_id"] == interaction.user.id
        if not is_buyer and not await ensure(interaction, "shop"):
            return

        async def handler(inter: discord.Interaction, reason: str) -> None:
            await self.cog.set_order_status(inter, order, "cancelled", "cancelled", reason or "Без причины")

        await interaction.response.send_modal(ReasonModal("Отмена заказа", "Причина отмены", handler, required=False))

    @discord.ui.button(label="Добавить сотрудника", emoji="➕", style=discord.ButtonStyle.secondary,
                       custom_id="polari:order:add", row=1)
    async def add_staff(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        if not await ensure(interaction, "shop"):
            return
        view = discord.ui.View(timeout=60)
        view.add_item(AddStaffSelect())
        await interaction.response.send_message(view=view, ephemeral=True)

    @discord.ui.button(label="Закрыть тикет", emoji="🔒", style=discord.ButtonStyle.danger,
                       custom_id="polari:order:close", row=1)
    async def close(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        ticket = await self.cog.bot.tickets.get_by_channel(interaction.channel_id)  # type: ignore[arg-type]
        if not ticket or ticket["status"] != "open":
            await interaction.response.send_message(embed=style.error("Тикет уже закрыт."), ephemeral=True)
            return
        if ticket["user_id"] != interaction.user.id and not await ensure(interaction, "shop"):
            return
        await interaction.response.send_modal(CloseModal())


async def setup(bot: "PolariBot") -> None:
    await bot.add_cog(ShopCog(bot))
