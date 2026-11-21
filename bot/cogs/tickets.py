"""Тикет-система POLARI: панель выбора типа, модальное окно, управление тикетом."""
from __future__ import annotations

from typing import TYPE_CHECKING

import discord
from discord import app_commands
from discord.ext import commands

from bot.data.content import TICKET_BY_ID, TICKET_TYPES
from bot.database.db import now_iso
from bot.embeds import style
from bot.services.tickets import TicketError

if TYPE_CHECKING:
    from bot.core import PolariBot


def _ticket_options() -> list[discord.SelectOption]:
    return [discord.SelectOption(label=t.label, value=t.id, emoji=t.emoji, description=t.description)
            for t in TICKET_TYPES if t.id != "purchase"] + [
        discord.SelectOption(label="Покупка", value="purchase", emoji="💎",
                             description="Товары оформляются кнопкой «Купить» в магазине")]


class TicketModal(discord.ui.Modal):
    subject = discord.ui.TextInput(label="Тема обращения", max_length=100, placeholder="Коротко о проблеме")
    details = discord.ui.TextInput(label="Подробности", style=discord.TextStyle.paragraph, max_length=1000,
                                   required=False, placeholder="Опишите ситуацию: что, где, когда, кто участвовал")

    def __init__(self, type_id: str):
        t = TICKET_BY_ID[type_id]
        super().__init__(title=f"{t.label} — POLARI"[:45])
        self.type_id = type_id

    async def on_submit(self, interaction: discord.Interaction) -> None:
        await interaction.response.defer(ephemeral=True, thinking=True)
        subject = self.subject.value + (f"\n\n{self.details.value}" if self.details.value else "")
        try:
            channel, _ = await interaction.client.tickets.create(interaction.user, self.type_id, subject)  # type: ignore[attr-defined]
        except TicketError as exc:
            await interaction.followup.send(embed=style.error(str(exc)), ephemeral=True)
            return
        await interaction.followup.send(embed=style.success("Тикет создан", f"Перейдите в {channel.mention} ❄️"),
                                        ephemeral=True)


class TicketTypeSelect(discord.ui.Select):
    def __init__(self, custom_id: str | None = "polari:tk:open"):
        kwargs = {"custom_id": custom_id} if custom_id else {}
        super().__init__(placeholder="🎫 Выберите тип обращения", options=_ticket_options(), **kwargs)

    async def callback(self, interaction: discord.Interaction) -> None:
        type_id = self.values[0]
        if type_id == "purchase":
            shop = await interaction.client.registry.channel(interaction.guild, "shop")  # type: ignore[attr-defined]
            where = shop.mention if shop else "канале магазина"
            await interaction.response.send_message(embed=style.base("Покупка", (
                f"Выберите товар в {where} и нажмите **«Купить»** — тикет заказа откроется автоматически.\n"
                "Если вопрос о покупке без заказа, выберите «Вопрос администрации».")), ephemeral=True)
            return
        await interaction.response.send_modal(TicketModal(type_id))


class TicketPanelView(discord.ui.View):
    def __init__(self):
        super().__init__(timeout=None)
        self.add_item(TicketTypeSelect())


class AddStaffSelect(discord.ui.UserSelect):
    def __init__(self):
        super().__init__(placeholder="Выберите сотрудника", min_values=1, max_values=1)

    async def callback(self, interaction: discord.Interaction) -> None:
        target = self.values[0]
        reg = interaction.client.registry  # type: ignore[attr-defined]
        is_staff = isinstance(target, discord.Member) and (
            await reg.member_level(target) > 0
            or any([await reg.has_role_key(target, k) for k in ("support", "shop_staff", "checker", "staff")]))
        if not is_staff:
            await interaction.response.send_message(embed=style.error("Добавлять можно только сотрудников POLARI."),
                                                    ephemeral=True)
            return
        await interaction.channel.set_permissions(target, view_channel=True, send_messages=True,  # type: ignore[union-attr]
                                                  read_message_history=True, reason=f"POLARI: добавлен {interaction.user}")
        await interaction.response.edit_message(content=None, view=None, embed=style.success(
            "Сотрудник добавлен", f"{target.mention} теперь видит этот тикет."))
        await interaction.channel.send(embed=style.base("В тикет добавлен сотрудник",  # type: ignore[union-attr]
                                                        f"{target.mention} подключён {interaction.user.mention}.",
                                                        emoji="➕"))


class TicketControlView(discord.ui.View):
    """Кнопки внутри обычного тикета. Тикет определяется по каналу — работает после перезапуска."""

    def __init__(self):
        super().__init__(timeout=None)

    async def _ticket(self, interaction: discord.Interaction):
        ticket = await interaction.client.tickets.get_by_channel(interaction.channel_id)  # type: ignore[attr-defined]
        if not ticket or ticket["status"] != "open":
            await interaction.response.send_message(embed=style.error("Тикет уже закрыт или не найден."), ephemeral=True)
            return None
        return ticket

    @discord.ui.button(label="Принять", emoji="✅", style=discord.ButtonStyle.success, custom_id="polari:tk:claim")
    async def claim(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        ticket = await self._ticket(interaction)
        if not ticket:
            return
        if not await interaction.client.tickets.can_manage(interaction.user, ticket):  # type: ignore[attr-defined]
            await interaction.response.send_message(embed=style.error("Принять тикет может только персонал."),
                                                    ephemeral=True)
            return
        if ticket["claimed_by"]:
            await interaction.response.send_message(embed=style.error(f"Тикет уже принял <@{ticket['claimed_by']}>."),
                                                    ephemeral=True)
            return
        await interaction.client.db.execute("UPDATE tickets SET claimed_by=? WHERE id=?",  # type: ignore[attr-defined]
                                            (interaction.user.id, ticket["id"]))
        await interaction.response.send_message(embed=style.base(
            "Тикет принят", f"{interaction.user.mention} занимается вашим обращением.", color=style.Colors.SUCCESS,
            emoji="✅"))

    @discord.ui.button(label="Добавить сотрудника", emoji="➕", style=discord.ButtonStyle.secondary,
                       custom_id="polari:tk:add")
    async def add_staff(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        ticket = await self._ticket(interaction)
        if not ticket:
            return
        if not await interaction.client.tickets.can_manage(interaction.user, ticket):  # type: ignore[attr-defined]
            await interaction.response.send_message(embed=style.error("Только персонал может добавлять сотрудников."),
                                                    ephemeral=True)
            return
        view = discord.ui.View(timeout=60)
        view.add_item(AddStaffSelect())
        await interaction.response.send_message(view=view, ephemeral=True)

    @discord.ui.button(label="Закрыть", emoji="🔒", style=discord.ButtonStyle.danger, custom_id="polari:tk:close")
    async def close(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        ticket = await self._ticket(interaction)
        if not ticket:
            return
        is_author = ticket["user_id"] == interaction.user.id
        if not is_author and not await interaction.client.tickets.can_manage(interaction.user, ticket):  # type: ignore[attr-defined]
            await interaction.response.send_message(embed=style.error("Закрыть тикет может автор или персонал."),
                                                    ephemeral=True)
            return
        await interaction.response.send_modal(CloseModal())


class CloseModal(discord.ui.Modal, title="Закрытие тикета"):
    reason = discord.ui.TextInput(label="Причина закрытия", default="Вопрос решён", max_length=200)

    async def on_submit(self, interaction: discord.Interaction) -> None:
        await interaction.response.send_message(embed=style.base(
            "Тикет закрывается", "Транскрипт сохранён в журнале. Канал будет удалён через 5 секунд.",
            color=style.Colors.DARK, emoji="🔒"))
        await interaction.client.tickets.close(interaction.channel, interaction.user, self.reason.value)  # type: ignore[attr-defined]


class TicketsCog(commands.Cog):
    def __init__(self, bot: "PolariBot"):
        self.bot = bot

    async def cog_load(self) -> None:
        self.bot.add_view(TicketPanelView())
        self.bot.add_view(TicketControlView())
        self.bot.get_cog("SetupCog").register_panel("tickets", self.post_panel)  # type: ignore[union-attr]

    async def post_panel(self, channel: discord.TextChannel) -> discord.Message:
        lines = "\n".join(f"{t.emoji} **{t.label}** — {t.description}" for t in TICKET_TYPES)
        embed = style.header("Поддержка сервера", (
            "Нужна помощь? Откройте приватный тикет — его увидите только вы и ответственная команда.\n"
            f"{style.DIVIDER}\n{lines}\n{style.DIVIDER}\n"
            "> Не создавайте тикеты без причины: за спам тикетами выдаётся наказание."),
            style.Colors.PRIMARY)
        return await style.send_panel(channel, "tickets", [embed], view=TicketPanelView())

    @app_commands.command(name="ticket", description="Открыть обращение в поддержку POLARI")
    @app_commands.guild_only()
    async def ticket_cmd(self, interaction: discord.Interaction) -> None:
        view = discord.ui.View(timeout=120)
        view.add_item(TicketTypeSelect(custom_id=None))
        await interaction.response.send_message(embed=style.base("Новое обращение", "Выберите тип обращения ниже.",
                                                                 emoji="🎫"), view=view, ephemeral=True)

    @commands.Cog.listener()
    async def on_guild_channel_delete(self, channel: discord.abc.GuildChannel) -> None:
        """Если канал тикета удалили вручную — закрываем запись, чтобы не блокировать лимит."""
        await self.bot.db.execute("UPDATE tickets SET status='closed', closed_at=? WHERE channel_id=? AND status='open'",
                                  (now_iso(), channel.id))


async def setup(bot: "PolariBot") -> None:
    await bot.add_cog(TicketsCog(bot))
