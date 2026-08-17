from __future__ import annotations

import discord
from config import settings
from discord import app_commands
from discord.ext import commands
from embeds import base, error, success

from database import (
    evaluate_qualifier,
    get_event,
    get_event_qualifier_requirements,
    get_event_qualifiers,
    grant_qualification,
    log_bot_action,
)


class QualifiersCog(commands.Cog):
    def __init__(self, bot: commands.Bot) -> None:
        self.bot = bot

    def _requirements_text(self, event_id: int) -> str:
        req = get_event_qualifier_requirements(event_id)
        if not req:
            return "No requirements set."
        parts = []
        top = int(req.get("top") or 0)
        if top:
            parts.append(f"Top **{top}** finishers")
        if int(req.get("min_kills") or 0):
            parts.append(f"min **{req['min_kills']}** kills")
        if int(req.get("min_wins") or 0):
            parts.append(f"min **{req['min_wins']}** win(s)")
        target = req.get("target_event_id")
        division = req.get("target_division_id")
        reward = []
        if target:
            ev = get_event(int(target))
            reward.append(f"entry to **{ev['name']}** (ID {target})" if ev else f"entry to event {target}")
        if division:
            reward.append(f"division membership (ID {division})")
        suffix = f"\nReward: {' + '.join(reward)}" if reward else ""
        return "; ".join(parts) + suffix

    @commands.hybrid_command(
        name="evaluate-qualifier",
        description="Evaluate a qualifier event: preview who meets the requirements (apply=True grants them)",
    )
    @app_commands.describe(
        event_id="Qualifier event ID",
        apply="Set True to actually grant qualifications (adds players to the qualified list)",
    )
    async def evaluate_qualifier_cmd(
        self,
        ctx: commands.Context,
        event_id: int,
        apply: bool = False,
    ) -> None:
        if not await self._check_admin(ctx):
            return
        if ctx.interaction:
            await ctx.interaction.response.defer(ephemeral=True)

        ev = get_event(event_id)
        if not ev:
            await ctx.send(embed=error("Event not found."))
            return
        if (ev.get("event_type") or "cup") != "qualifier":
            await ctx.send(
                embed=error(f"**{ev['name']}** is not a qualifier event.")
            )
            return

        result = evaluate_qualifier(event_id)
        qualified = result["qualified"]
        if not qualified:
            embed = base(f"🏅 {ev['name']} — Qualifier Evaluation")
            embed.description = "No players meet the requirements yet."
            await ctx.send(embed=embed)
            return

        already = {
            q["discord_id"] for q in get_event_qualifiers(event_id)
        }
        lines = []
        granted = 0
        for row in qualified:
            did = row["discord_id"]
            mark = "✅" if did in already else ("➕" if apply else "·")
            if apply and did not in already:
                grant_qualification(
                    event_id, did, row.get("username") or "", row.get("team_members")
                )
                granted += 1
            lines.append(
                f"{mark} **{row.get('username') or did}** — #{row['placement']} "
                f"({row.get('wins', 0)}W, {row.get('kills', 0)} kills)"
            )

        embed = base(f"🏅 {ev['name']} — Qualifier Evaluation", 0xF1C40F)
        embed.description = "\n".join(lines)
        embed.set_footer(
            text=f"Requirements: {self._requirements_text(event_id)}"
        )
        if apply:
            log_bot_action(
                event_id,
                "evaluate_qualifier",
                f"Granted qualification to {granted} player(s)",
                str(ctx.author.id),
            )
        await ctx.send(embed=embed)
        if apply:
            await ctx.send(
                embed=success(f"Qualification granted to {granted} new player(s).")
            )
        else:
            embed = base("🏅 Preview only — nothing changed.")
            embed.description = "Re-run with `apply:True` to grant these players."
            await ctx.send(embed=embed)

    @commands.hybrid_command(
        name="qualifier-status",
        description="Show a qualifier's requirements and who has qualified so far",
    )
    @app_commands.describe(event_id="Qualifier event ID")
    async def qualifier_status(
        self, ctx: commands.Context, event_id: int
    ) -> None:
        ev = get_event(event_id)
        if not ev:
            await ctx.send(embed=error("Event not found."))
            return

        qualifiers = get_event_qualifiers(event_id)
        lines = []
        for q in qualifiers:
            name = q.get("username") or q.get("discord_id", "?")
            extra = ""
            if q.get("team_members"):
                extra = f" + {len(q['team_members'].split(','))} teammate(s)"
            lines.append(f"✅ **{name}**{extra}")
        if not lines:
            lines.append("No one has qualified yet.")

        embed = base(f"🏅 {ev['name']} — Qualifier Status", 0x3498DB)
        embed.description = "\n".join(lines)
        embed.set_footer(text=self._requirements_text(event_id))
        await ctx.send(embed=embed)

    async def _check_admin(self, ctx: commands.Context) -> bool:
        if not ctx.guild:
            await ctx.send(embed=error("Server only."))
            return False
        member = ctx.guild.get_member(ctx.author.id)
        if not member:
            return False
        if member.guild_permissions.administrator:
            return True
        admin_role_id = settings.discord_admin_role_id
        if admin_role_id:
            role = ctx.guild.get_role(int(admin_role_id))
            if role and role in member.roles:
                return True
        await ctx.send(embed=error("You need admin permission."))
        return False


async def setup(bot: commands.Bot) -> None:
    await bot.add_cog(QualifiersCog(bot))