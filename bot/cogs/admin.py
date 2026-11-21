"""Панель настройки бота: /config, /panel, /announce и прощальные сообщения."""
from __future__ import annotations

from typing import TYPE_CHECKING

import discord
from discord import app_commands
from discord.ext import commands

from bot.cogs.setup import PANEL_CHANNELS
from bot.data.blueprint import ALL_CHANNEL_KEYS, CATEGORIES, ROLE_BY_KEY
from bot.embeds import style
from bot.utils.checks import require

if TYPE_CHECKING:
    from bot.core import PolariBot

# Настройки, которые можно менять командой /config set
OPTIONS = {
    "interview.give_member": ("Выдавать роль «Участник» после обзвона", {"1", "0"}),
    "welcome.goodbye": ("Прощальные сообщения в канале новичков", {"1", "0"}),
}
CHANNEL_KEYS = ALL_CHANNEL_KEYS + [c.key for c in CATEGORIES]


async def _ac(current: str, values: list[str]) -> list[app_commands.Choice[str]]:
    return [app_commands.Choice(name=v, value=v) for v in values if current.lower() in v.lower()][:25]


class AnnounceModal(discord.ui.Modal, title="Объявление POLARI"):
    heading = discord.ui.TextInput(label="Заголовок", max_length=80)
    body = discord.ui.TextInput(label="Текст", style=discord.TextStyle.paragraph, max_length=3500)

    def __init__(self, channel: discord.TextChannel, ping: bool):
        super().__init__()
        self.channel, self.ping = channel, ping

    async def on_submit(self, interaction: discord.Interaction) -> None:
        embed = style.base(self.heading.value, self.body.value, color=style.Colors.PRIMARY, emoji="📢")
        embed.set_author(name=interaction.user.display_name, icon_url=interaction.user.display_avatar.url)
        msg = await self.channel.send(content="@everyone" if self.ping else None, embed=embed,
                                      allowed_mentions=discord.AllowedMentions(everyone=self.ping))
        await interaction.response.send_message(embed=style.success("Объявление опубликовано", msg.jump_url),
                                                ephemeral=True)


class AdminCog(commands.Cog):
    def __init__(self, bot: "PolariBot"):
        self.bot = bot

    config = app_commands.Group(name="config", description="Настройки POLARI", guild_only=True)

    @config.command(name="show", description="Показать текущую конфигурацию")
    @require("admin")
    async def config_show(self, interaction: discord.Interaction) -> None:
        guild = interaction.guild
        assert guild
        data = await self.bot.registry.load(guild.id)
        roles = [f"`{k}` → <@&{data[f'role.{k}']}>" if f"role.{k}" in data else f"`{k}` → ❌" for k in ROLE_BY_KEY]
        chans = [f"`{k}` → <#{data[f'channel.{k}']}>" if f"channel.{k}" in data else f"`{k}` → ❌" for k in CHANNEL_KEYS]
        opts = [f"`{k}` = **{data.get(k, '1')}** — {desc}" for k, (desc, _) in OPTIONS.items()]
        embeds = [style.base("Конфигурация", "\n".join(opts) + (
            f"\n\nНастройка применена: {style.ts(data['setup.applied_at'])}" if "setup.applied_at" in data else
            "\n\n⚠️ `/setup` ещё не применялся."), emoji="⚙️"),
            discord.Embed(title="🎭 Роли", description="\n".join(roles)[:4000], color=style.Colors.SECONDARY),
            discord.Embed(title="# Каналы", description="\n".join(chans)[:4000], color=style.Colors.SECONDARY)]
        await interaction.response.send_message(embeds=embeds, ephemeral=True)

    @config.command(name="set", description="Изменить параметр")
    @require("config")
    async def config_set(self, interaction: discord.Interaction, key: str, value: str) -> None:
        if key not in OPTIONS or value not in OPTIONS[key][1]:
            allowed = "; ".join(f"{k}: {'/'.join(sorted(v))}" for k, (_, v) in OPTIONS.items())
            await interaction.response.send_message(embed=style.error(f"Допустимо: {allowed}"), ephemeral=True)
            return
        await self.bot.registry.set(interaction.guild_id, key, value)  # type: ignore[arg-type]
        await self.bot.logs.send(interaction.guild, "server", style.log_embed("Настройка изменена", emoji="⚙️", fields=[  # type: ignore[arg-type]
            ("Параметр", key), ("Значение", value), ("Кто", interaction.user.mention)]))
        await interaction.response.send_message(embed=style.success("Сохранено", f"`{key}` = **{value}**"), ephemeral=True)

    @config_set.autocomplete("key")
    async def _set_key_ac(self, interaction: discord.Interaction, current: str):
        return await _ac(current, list(OPTIONS))

    @config.command(name="link_role", description="Привязать существующую роль к роли POLARI (без изменений роли)")
    @require("config")
    async def link_role(self, interaction: discord.Interaction, key: str, role: discord.Role) -> None:
        if key not in ROLE_BY_KEY:
            await interaction.response.send_message(embed=style.error("Неизвестный ключ роли."), ephemeral=True)
            return
        await self.bot.registry.set(interaction.guild_id, f"role.{key}", role.id)  # type: ignore[arg-type]
        await interaction.response.send_message(embed=style.success("Роль привязана", f"`{key}` → {role.mention}"),
                                                ephemeral=True)

    @link_role.autocomplete("key")
    async def _role_key_ac(self, interaction: discord.Interaction, current: str):
        return await _ac(current, list(ROLE_BY_KEY))

    @config.command(name="link_channel", description="Привязать существующий канал к каналу POLARI (без изменений)")
    @require("config")
    async def link_channel(self, interaction: discord.Interaction, key: str,
                           channel: discord.TextChannel | discord.VoiceChannel | discord.CategoryChannel) -> None:
        if key not in CHANNEL_KEYS:
            await interaction.response.send_message(embed=style.error("Неизвестный ключ канала."), ephemeral=True)
            return
        await self.bot.registry.set(interaction.guild_id, f"channel.{key}", channel.id)  # type: ignore[arg-type]
        await interaction.response.send_message(embed=style.success("Канал привязан", f"`{key}` → {channel.mention}"),
                                                ephemeral=True)

    @link_channel.autocomplete("key")
    async def _channel_key_ac(self, interaction: discord.Interaction, current: str):
        return await _ac(current, CHANNEL_KEYS)

    @app_commands.command(name="panel", description="Опубликовать панель заново (старая панель бота заменяется)")
    @app_commands.choices(panel=[app_commands.Choice(name=k, value=k) for k in PANEL_CHANNELS])
    @app_commands.guild_only()
    @require("config")
    async def panel(self, interaction: discord.Interaction, panel: app_commands.Choice[str],
                    channel: discord.TextChannel | None = None) -> None:
        await interaction.response.defer(ephemeral=True)
        setup_cog = self.bot.get_cog("SetupCog")
        builder = setup_cog.panel_builders.get(panel.value)  # type: ignore[union-attr]
        target = channel or await self.bot.registry.text(interaction.guild, PANEL_CHANNELS[panel.value])  # type: ignore[arg-type]
        if not builder or not target:
            await interaction.followup.send(embed=style.error("Канал панели не найден. Укажите channel."), ephemeral=True)
            return
        old = await self.bot.db.fetchone("SELECT channel_id, message_id FROM panels WHERE guild_id=? AND key=?",
                                         (interaction.guild_id, panel.value))
        if old:
            old_channel = interaction.guild.get_channel(old["channel_id"])  # type: ignore[union-attr]
            if isinstance(old_channel, discord.TextChannel):
                try:
                    old_msg = await old_channel.fetch_message(old["message_id"])
                    if old_msg.author.id == self.bot.user.id:  # type: ignore[union-attr]
                        await old_msg.delete()
                except discord.HTTPException:
                    pass  # старое сообщение уже удалено
        msg = await builder(target)
        await self.bot.db.save_panel(interaction.guild_id, panel.value, target.id, msg.id)  # type: ignore[arg-type]
        await interaction.followup.send(embed=style.success("Панель опубликована", msg.jump_url), ephemeral=True)

    @app_commands.command(name="backup", description="Сделать бэкап базы данных прямо сейчас")
    @app_commands.guild_only()
    @require("config")
    async def backup(self, interaction: discord.Interaction) -> None:
        await interaction.response.defer(ephemeral=True)
        ok = await self.bot.hosting.backup_now(f"вручную, {interaction.user}")
        await interaction.followup.send(embed=style.success("Бэкап сохранён", "Файл отправлен в канал бэкапов.") if ok
                                        else style.error("Бэкап не выполнен: проверьте BACKUP_CHANNEL_ID."), ephemeral=True)

    @app_commands.command(name="announce", description="Опубликовать оформленное объявление")
    @app_commands.guild_only()
    @require("admin")
    async def announce(self, interaction: discord.Interaction, channel: discord.TextChannel, ping_everyone: bool = False) -> None:
        if ping_everyone and not await self.bot.registry.can(interaction.user, "level:80"):  # type: ignore[arg-type]
            await interaction.response.send_message(embed=style.error("Пинг @everyone доступен с уровня главного администратора."),
                                                    ephemeral=True)
            return
        await interaction.response.send_modal(AnnounceModal(channel, ping_everyone))

    @commands.Cog.listener()
    async def on_member_remove(self, member: discord.Member) -> None:
        if member.bot or (await self.bot.registry.get(member.guild.id, "welcome.goodbye") or "1") != "1":
            return
        channel = await self.bot.registry.text(member.guild, "newbies")
        if channel:
            try:
                await channel.send(embed=style.base("До встречи", f"**{member.display_name}** покинул POLARI. "
                                                                  "Будем рады увидеть снова ❄️",
                                                    color=style.Colors.DARK, emoji="👋"))
            except discord.HTTPException:
                pass


async def setup(bot: "PolariBot") -> None:
    await bot.add_cog(AdminCog(bot))
