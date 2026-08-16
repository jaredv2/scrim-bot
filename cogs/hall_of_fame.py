from __future__ import annotations

import datetime
import logging

import discord
from config import digits_only, settings
from discord import app_commands
from discord.ext import commands, tasks

from database import (
    get_players_leaderboard,
    get_season,
    get_server_legend,
    get_kv,
    set_kv,
)
from embeds import base, error

logger = logging.getLogger("scrim-bot")

POST_INTERVAL_HOURS = 72
KV_LAST_POSTED = "hof_last_posted"


class HallOfFameCog(commands.Cog):
    """Posts a Hall of Fame message to a configured channel every 3 days."""

    def __init__(self, bot: commands.Bot) -> None:
        self.bot = bot
        self.hof_loop.start()

    def cog_unload(self) -> None:
        self.hof_loop.cancel()

    @tasks.loop(hours=1)
    async def hof_loop(self) -> None:
        channel_id = digits_only(settings.discord_hall_of_fame_channel_id)
        if not channel_id:
            return
        for guild in self.bot.guilds:
            channel = guild.get_channel(int(channel_id))
            if not channel:
                continue

            last_raw = get_kv(KV_LAST_POSTED, "")
            last = None
            if last_raw:
                try:
                    last = datetime.datetime.fromisoformat(last_raw)
                except ValueError:
                    last = None

            if last is not None:
                elapsed = datetime.datetime.utcnow() - last
                if elapsed < datetime.timedelta(hours=POST_INTERVAL_HOURS):
                    continue

            try:
                await channel.send(embed=await self._hof_embed(guild))
                set_kv(KV_LAST_POSTED, datetime.datetime.utcnow().isoformat())
                logger.info("hof_posted", extra={"channel": str(channel.id)})
            except Exception as e:
                logger.error("hof_post_failed: %s", e, exc_info=True)

    @hof_loop.before_loop
    async def before_hof_loop(self) -> None:
        await self.bot.wait_until_ready()

    async def _hof_embed(self, guild: discord.Guild) -> discord.Embed:
        players = get_players_leaderboard()
        active = [p for p in players if (p.get("total_games") or 0) > 0]

        embed = base(f"🏆 Hall of Fame — Season {get_season()}", 0xF1C40F)

        legend = get_server_legend()
        if legend:
            member = guild.get_member(int(legend["discord_id"]))
            name = member.mention if member else legend["username"]
            embed.add_field(
                name="👑 Unreal Legend",
                value=f"{name} — {legend['pr']} PR",
                inline=False,
            )

        if active:
            best_pr = max(active, key=lambda r: r.get("pr") or 0)
            best_kills = max(active, key=lambda r: r.get("total_kills") or 0)
            best_wins = max(active, key=lambda r: r.get("total_wins") or 0)
            best_pl = min(
                [r for r in active if r.get("avg_placement") is not None],
                key=lambda r: r["avg_placement"],
                default=None,
            )

            def name_of(row: dict) -> str:
                member = guild.get_member(int(row["discord_id"]))
                return member.mention if member else row["username"]

            embed.add_field(
                name="💎 Best PR",
                value=f"{name_of(best_pr)} — {best_pr['pr']}",
                inline=True,
            )
            embed.add_field(
                name="☠️ Most Kills",
                value=f"{name_of(best_kills)} — {best_kills['total_kills']}",
                inline=True,
            )
            embed.add_field(
                name="🏅 Most Wins",
                value=f"{name_of(best_wins)} — {best_wins['total_wins']}",
                inline=True,
            )
            if best_pl:
                embed.add_field(
                    name="🎯 Best Avg Placement",
                    value=f"{name_of(best_pl)} — #{best_pl['avg_placement']}",
                    inline=True,
                )

        embed.set_footer(text="Posted automatically every 3 days")
        return embed

    @app_commands.command(
        name="hall-of-fame",
        description="Post the Hall of Fame message to the configured channel now",
    )
    async def hall_of_fame(self, interaction: discord.Interaction) -> None:
        member = interaction.guild.get_member(interaction.user.id) if interaction.guild else None
        if not member:
            await interaction.response.send_message(embed=error("Server only."), ephemeral=True)
            return
        if member.guild_permissions.administrator:
            pass
        else:
            admin_role_id = settings.discord_admin_role_id
            if admin_role_id:
                role = interaction.guild.get_role(int(admin_role_id))
                if role and role in member.roles:
                    pass
                else:
                    await interaction.response.send_message(
                        embed=error("You need admin permission."), ephemeral=True
                    )
                    return
            else:
                await interaction.response.send_message(
                    embed=error("You need admin permission."), ephemeral=True
                )
                return

        channel_id = digits_only(settings.discord_hall_of_fame_channel_id)
        if not channel_id:
            await interaction.response.send_message(
                embed=error("Hall of Fame channel not configured."), ephemeral=True
            )
            return
        channel = interaction.guild.get_channel(int(channel_id))
        if not channel:
            await interaction.response.send_message(
                embed=error("Hall of Fame channel not found in this server."), ephemeral=True
            )
            return

        await channel.send(embed=await self._hof_embed(interaction.guild))
        set_kv(KV_LAST_POSTED, datetime.datetime.utcnow().isoformat())
        await interaction.response.send_message(
            embed=base(f"🏆 Hall of Fame posted to {channel.mention}."),
            ephemeral=True,
        )


async def setup(bot: commands.Bot) -> None:
    await bot.add_cog(HallOfFameCog(bot))
