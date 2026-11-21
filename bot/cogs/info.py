"""Памятка (Select Menu), правила (Embed на каждое правило + навигация), /userinfo, /serverinfo."""
from __future__ import annotations

from typing import TYPE_CHECKING

import discord
from discord import app_commands
from discord.ext import commands

from bot.data.blueprint import ROLES
from bot.data.content import (MEMO_COMMANDS, MEMO_EVENTS_TEXT, MEMO_ROLES_TEXT, MEMO_SECTIONS, MEMO_SUPPORT_TEXT,
                              RULES, RULES_INTRO, RULES_TERMS, SHOP_ITEMS, Rule)
from bot.embeds import style
from config import settings

if TYPE_CHECKING:
    from bot.core import PolariBot


def rule_embed(rule: Rule) -> discord.Embed:
    embed = discord.Embed(title=f"📘  {rule.number}  ┃  {rule.title}", description=rule.description,
                          color=style.Colors.SECONDARY)
    embed.add_field(name="⚖️ Наказание", value=rule.punishment)
    if rule.warning:
        embed.add_field(name="⚠️ Важно", value=rule.warning)
    return embed


def terms_embed() -> discord.Embed:
    return style.base("Термины", RULES_TERMS.format(warns=settings.warns_to_ban, days=settings.warn_ban_days),
                      color=style.Colors.PRIMARY, emoji="📖", timestamp=False)


class MemoSelect(discord.ui.Select):
    def __init__(self, cog: "InfoCog"):
        options = [discord.SelectOption(label=name, value=key, emoji=emoji, description=desc)
                   for key, (emoji, name, desc) in MEMO_SECTIONS.items()]
        super().__init__(placeholder="❄️ Выберите интересующий раздел", options=options, custom_id="polari:memo")
        self.cog = cog

    async def callback(self, interaction: discord.Interaction) -> None:
        embed = await self.cog.memo_section(interaction.guild, self.values[0])  # type: ignore[arg-type]
        await interaction.response.send_message(embed=embed, ephemeral=True)


class MemoView(discord.ui.View):
    def __init__(self, cog: "InfoCog"):
        super().__init__(timeout=None)
        self.add_item(MemoSelect(cog))


class RulesSelect(discord.ui.Select):
    def __init__(self):
        options = [discord.SelectOption(label=f"{r.number} · {r.title}"[:100], value=r.number,
                                        description=r.punishment[:100]) for r in RULES[:24]]
        options.append(discord.SelectOption(label="Термины и наказания", value="terms", emoji="📖"))
        super().__init__(placeholder="📘 Найти правило", options=options, custom_id="polari:rules")

    async def callback(self, interaction: discord.Interaction) -> None:
        value = self.values[0]
        if value == "terms":
            await interaction.response.send_message(embed=terms_embed(), ephemeral=True)
            return
        index = next(i for i, r in enumerate(RULES) if r.number == value)
        await interaction.response.send_message(embed=rule_embed(RULES[index]), view=RulePager(index), ephemeral=True)


class RulesView(discord.ui.View):
    def __init__(self):
        super().__init__(timeout=None)
        self.add_item(RulesSelect())


class RulePager(discord.ui.View):
    """Листалка правил в эфемерном сообщении."""

    def __init__(self, index: int):
        super().__init__(timeout=300)
        self.index = index
        self._sync()

    def _sync(self) -> None:
        self.prev.disabled = self.index == 0
        self.next.disabled = self.index >= len(RULES) - 1
        self.counter.label = f"{self.index + 1} / {len(RULES)}"

    @discord.ui.button(emoji="◀️", style=discord.ButtonStyle.secondary)
    async def prev(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        self.index -= 1
        self._sync()
        await interaction.response.edit_message(embed=rule_embed(RULES[self.index]), view=self)

    @discord.ui.button(label="1", style=discord.ButtonStyle.primary, disabled=True)
    async def counter(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        await interaction.response.defer()

    @discord.ui.button(emoji="▶️", style=discord.ButtonStyle.secondary)
    async def next(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        self.index += 1
        self._sync()
        await interaction.response.edit_message(embed=rule_embed(RULES[self.index]), view=self)


class InfoCog(commands.Cog):
    def __init__(self, bot: "PolariBot"):
        self.bot = bot

    async def cog_load(self) -> None:
        self.bot.add_view(MemoView(self))
        self.bot.add_view(RulesView())
        setup_cog = self.bot.get_cog("SetupCog")
        setup_cog.register_panel("memo", self.post_memo)    # type: ignore[union-attr]
        setup_cog.register_panel("rules", self.post_rules)  # type: ignore[union-attr]

    # ------------------------------------------------------------------ памятка
    async def post_memo(self, channel: discord.TextChannel) -> discord.Message:
        lines = "\n".join(f"{emoji} **{name}** — {desc.lower()}." for emoji, name, desc in MEMO_SECTIONS.values())
        embed = style.base("Памятка по серверу", (
            f"Меню ниже поможет быстро познакомиться с **POLARI**.\n{style.DIVIDER}\n{lines}\n{style.DIVIDER}"),
            color=style.Colors.PRIMARY, emoji="📑")
        embed, file = style.with_banner(embed)
        kwargs = {"embed": embed, "view": MemoView(self)}
        if file:
            kwargs["file"] = file
        return await channel.send(**kwargs)

    async def memo_section(self, guild: discord.Guild, key: str) -> discord.Embed:
        emoji, name, _ = MEMO_SECTIONS[key]
        embed = style.base(name, "", color=style.Colors.SECONDARY, emoji=emoji)
        if key == "staff":
            blocks = []
            for spec in ROLES:
                if not spec.staff or spec.key == "staff":
                    continue
                role = await self.bot.registry.role(guild, spec.key)
                if role and role.members:
                    names = ", ".join(m.mention for m in role.members[:15])
                    blocks.append(f"**{role.name}**\n{names}")
            embed.description = "\n\n".join(blocks) or "Состав команды скоро появится здесь."
        elif key == "commands":
            embed.description = "\n".join(f"`{cmd}` — {desc}" for cmd, desc in MEMO_COMMANDS)
        elif key == "roles":
            embed.description = MEMO_ROLES_TEXT
        elif key == "events":
            embed.description = MEMO_EVENTS_TEXT
        elif key == "shop":
            shop = await self.bot.registry.channel(guild, "shop")
            embed.description = "\n".join(f"{i.emoji} **{i.name}** — {i.price} руб." +
                                          (f" / {i.period_days} дн." if i.period_days else "") for i in SHOP_ITEMS)
            embed.description += f"\n\nОформление — кнопкой «Купить» в {shop.mention if shop else 'магазине'}."
        elif key == "rules":
            rules = await self.bot.registry.channel(guild, "rules")
            embed.description = "\n".join(f"`{r.number}` {r.title} — *{r.punishment}*" for r in RULES)
            embed.description += f"\n\nПолная версия — {rules.mention if rules else 'в канале правил'}."
        elif key == "support":
            embed.description = MEMO_SUPPORT_TEXT
        return embed

    # ------------------------------------------------------------------ правила
    async def post_rules(self, channel: discord.TextChannel) -> discord.Message:
        intro = style.base("Правила сервера", RULES_INTRO, color=style.Colors.PRIMARY, emoji="📘")
        intro, file = style.with_banner(intro)
        await (channel.send(embed=intro, file=file) if file else channel.send(embed=intro))
        rules = [rule_embed(r) for r in RULES]
        for i in range(0, len(rules), 10):
            await channel.send(embeds=rules[i:i + 10])
        return await channel.send(embed=terms_embed(), view=RulesView())

    # ------------------------------------------------------------------ информация
    @app_commands.command(name="userinfo", description="Информация об участнике")
    @app_commands.guild_only()
    async def userinfo(self, interaction: discord.Interaction, user: discord.Member | None = None) -> None:
        member = user or interaction.user
        assert isinstance(member, discord.Member)
        db = self.bot.db
        row = await db.fetchone("SELECT reputation, messages FROM users WHERE guild_id=? AND user_id=?",
                                (member.guild.id, member.id))
        iv = await db.fetchone("SELECT status FROM interviews WHERE guild_id=? AND user_id=?", (member.guild.id, member.id))
        embed = style.base("Профиль участника", f"{member.mention} · `{member.id}`", emoji="👤")
        embed.set_thumbnail(url=member.display_avatar.url)
        embed.add_field(name="Аккаунт создан", value=style.ts(member.created_at, "D"))
        embed.add_field(name="На сервере с", value=style.ts(member.joined_at, "D") if member.joined_at else "—")
        embed.add_field(name="Репутация", value=f"⭐ {row['reputation'] if row else 0}")
        embed.add_field(name="Сообщений", value=str(row["messages"] if row else 0))
        embed.add_field(name="Собеседование", value={"passed": "✅ пройдено", "pending": "⏳ ожидает",
                                                     "failed": "✖ отказ"}.get(iv["status"] if iv else "", "—"))
        roles = [r.mention for r in reversed(member.roles[1:])]
        embed.add_field(name=f"Роли ({len(roles)})", value=" ".join(roles[:20]) or "—", inline=False)
        if await self.bot.registry.can(interaction.user, "level:40"):  # type: ignore[arg-type]
            warns = await db.fetchval("SELECT COUNT(*) FROM warnings WHERE guild_id=? AND user_id=? AND active=1",
                                      (member.guild.id, member.id), 0)
            embed.add_field(name="Активных варнов", value=str(warns))
        await interaction.response.send_message(embed=embed, ephemeral=True)

    @app_commands.command(name="serverinfo", description="Информация о сервере")
    @app_commands.guild_only()
    async def serverinfo(self, interaction: discord.Interaction) -> None:
        guild = interaction.guild
        assert guild
        embed = style.base(guild.name, guild.description or "Сервер с характером ❄️", emoji="🏔️")
        if guild.icon:
            embed.set_thumbnail(url=guild.icon.url)
        humans = sum(1 for m in guild.members if not m.bot)
        embed.add_field(name="Владелец", value=f"<@{guild.owner_id}>")
        embed.add_field(name="Создан", value=style.ts(guild.created_at, "D"))
        embed.add_field(name="Участники", value=f"👥 {humans} · 🤖 {guild.member_count - humans}")
        embed.add_field(name="Каналы", value=f"💬 {len(guild.text_channels)} · 🔊 {len(guild.voice_channels)}")
        embed.add_field(name="Роли", value=str(len(guild.roles) - 1))
        embed.add_field(name="Бусты", value=f"💎 {guild.premium_subscription_count} · уровень {guild.premium_tier}")
        await interaction.response.send_message(embed=embed)


async def setup(bot: "PolariBot") -> None:
    await bot.add_cog(InfoCog(bot))
