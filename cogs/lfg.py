from __future__ import annotations

import time

import discord
from config import digits_only, settings
from discord import app_commands
from discord.ext import commands
from embeds import base, error, success

LFG_COOLDOWN_SECONDS = 3600

LFG_TARGETS = {
    "duo": ("Looking for a Duo partner", "Duo"),
    "trio": ("Looking for a Trio partner", "Trio"),
    "squad": ("Looking for a Squad", "Squad"),
}


class LFGCog(commands.Cog):
    """Find teammates: /lfd (duo), /lft (trio), /fls (squad)."""

    def __init__(self, bot: commands.Bot) -> None:
        self.bot = bot
        self._last_post: dict[int, float] = {}

    def _cooldown_ok(self, user_id: int) -> tuple[bool, float]:
        last = self._last_post.get(user_id, 0.0)
        remaining = LFG_COOLDOWN_SECONDS - (time.time() - last)
        return (last == 0 or remaining <= 0), max(0.0, remaining)

    async def _post_lfg(
        self,
        ctx: commands.Context,
        key: str,
        detail: str = "",
    ) -> None:
        channel_id = digits_only(settings.discord_lfg_channel_id)
        if not channel_id:
            await ctx.send(
                embed=error("No LFG channel configured (`DISCORD_LFG_CHANNEL_ID`).")
            )
            return
        channel = ctx.guild.get_channel(int(channel_id)) if ctx.guild else None
        if not channel or not isinstance(channel, discord.TextChannel):
            await ctx.send(embed=error("Configured LFG channel not found."))
            return

        ok, remaining = self._cooldown_ok(ctx.author.id)
        if not ok:
            await ctx.send(
                embed=error(
                    "You can only post one LFG per hour. "
                    f"Try again in {int(remaining // 60)}m."
                ),
                ephemeral=True,
            )
            return

        title, label = LFG_TARGETS[key]
        embed = base(f"🔍 {title}!", 0x1ABC9C)
        embed.description = (
            f"{ctx.author.mention} is looking for a **{label}**"
            + (f"\n{detail}" if detail else "")
            + "\n\nDM them to team up!"
        )
        embed.set_footer(text="One LFG post per hour • React 🔔 to join them")
        await channel.send(embed=embed)
        self._last_post[ctx.author.id] = time.time()

        await ctx.send(
            embed=success(f"Posted your LFG in {channel.mention}."),
            ephemeral=True,
        )

    @commands.hybrid_command(
        name="lfd",
        description="Looking For Duo — post a teammate request in the LFG channel",
    )
    @app_commands.describe(detail="Anything extra (region, style, etc.)")
    async def lfd(self, ctx: commands.Context, detail: str = "") -> None:
        await self._post_lfg(ctx, "duo", detail.strip())

    @commands.hybrid_command(
        name="lft",
        description="Looking For Trio — post a teammate request in the LFG channel",
    )
    @app_commands.describe(detail="Anything extra (region, style, etc.)")
    async def lft(self, ctx: commands.Context, detail: str = "") -> None:
        await self._post_lfg(ctx, "trio", detail.strip())

    @commands.hybrid_command(
        name="fls",
        description="Looking For Squad — post a teammate request in the LFG channel",
    )
    @app_commands.describe(detail="Anything extra (region, style, etc.)")
    async def fls(self, ctx: commands.Context, detail: str = "") -> None:
        await self._post_lfg(ctx, "squad", detail.strip())


async def setup(bot: commands.Bot) -> None:
    await bot.add_cog(LFGCog(bot))
