from __future__ import annotations

import discord
from config import settings
from discord import app_commands
from discord.ext import commands
from embeds import base, error, success

from database import (
    advance_bracket_winner,
    calc_event_pr,
    execute,
    finalize_bracket,
    get_bracket_matches,
    get_event,
    get_event_players,
    query_one,
    seed_bracket,
)
from ranks import sync_rank_role


def bracket_embed(ev: dict, matches: list[dict]) -> discord.Embed:
    """Render the bracket tree grouped by round."""
    rounds: dict[int, list[dict]] = {}
    for m in matches:
        rounds.setdefault(m["round"], []).append(m)

    embed = base(f"🏆 {ev['name']} — Bracket", 0xF1C40F)
    for r in sorted(rounds):
        lines = []
        for m in rounds[r]:
            p1 = m.get("player1_name") or "TBD"
            p2 = m.get("player2_name") or "TBD"
            if m["winner_id"]:
                w = m.get("winner_name") or "TBD"
                lines.append(f"✅ **{w}** beat {p2 if m['winner_id'] == m['player1_id'] else p1}")
            else:
                lines.append(f"{p1} vs {p2} — `Match {m['id']}`")
        embed.add_field(name=f"Round {r}", value="\n".join(lines) or "—", inline=False)
    return embed


class BracketsCog(commands.Cog):
    def __init__(self, bot: commands.Bot) -> None:
        self.bot = bot

    @commands.hybrid_command(
        name="seed-bracket",
        description="Seed a 1v1 single-elimination bracket for a bracket event (ordered by PR)",
    )
    @app_commands.describe(event_id="Event ID")
    async def seed_bracket_cmd(
        self, ctx: commands.Context, event_id: int
    ) -> None:
        if not await self._check_admin(ctx):
            return
        if ctx.interaction:
            await ctx.interaction.response.defer(ephemeral=True)

        ev = get_event(event_id)
        if not ev:
            await ctx.send(embed=error("Event not found."))
            return

        result = seed_bracket(event_id)
        if not result["ok"]:
            await ctx.send(embed=error(result.get("error", "Couldn't seed the bracket.")))
            return
        if result.get("champion") is not None:
            p = query_one(
                "SELECT COALESCE(game_username, username) AS name FROM vtx_players WHERE id = %s",
                (result["champion"],),
            )
            await ctx.send(
                embed=success(
                    f"Only one player is registered — **{p['name']}** is the champion by default. "
                    "Run `/end-bracket` to finalize."
                )
            )
            return

        matches = get_bracket_matches(event_id)
        await ctx.send(
            embed=success(
                f"Bracket seeded for **{ev['name']}** — {result['rounds']} round(s)."
            )
        )
        await ctx.send(embed=bracket_embed(ev, matches))

    @commands.hybrid_command(
        name="bracket",
        description="Show the current bracket state for an event",
    )
    @app_commands.describe(event_id="Event ID")
    async def bracket_view(self, ctx: commands.Context, event_id: int) -> None:
        ev = get_event(event_id)
        if not ev:
            await ctx.send(embed=error("Event not found."))
            return

        matches = get_bracket_matches(event_id)
        if not matches:
            await ctx.send(
                embed=base(f"🏆 {ev['name']} — No bracket yet. Use `/seed-bracket`.")
            )
            return
        await ctx.send(embed=bracket_embed(ev, matches))

    @commands.hybrid_command(
        name="advance-bracket",
        description="Record a bracket match winner and advance them to the next round",
    )
    @app_commands.describe(match_id="Bracket match ID", winner="The player who won")
    async def advance_bracket_cmd(
        self,
        ctx: commands.Context,
        match_id: int,
        winner: discord.Member,
    ) -> None:
        if not await self._check_admin(ctx):
            return
        if ctx.interaction:
            await ctx.interaction.response.defer(ephemeral=True)

        result = advance_bracket_winner(match_id, str(winner.id))
        if not result["ok"]:
            await ctx.send(embed=error(result.get("error", "Couldn't advance that match.")))
            return

        if result.get("finished"):
            await ctx.send(
                embed=success(
                    f"{winner.mention} wins the bracket! Run `/end-bracket` to "
                    "finalize standings and award PR."
                )
            )
            return

        await ctx.send(
            embed=success(
                f"{winner.mention} wins **Match {match_id}** and advances. "
                f"Next match ID: `{result['next_match']}`."
            )
        )

    @commands.hybrid_command(
        name="end-bracket",
        description="Finalize a bracket: record standings, apply placement points, award PR/coins",
    )
    @app_commands.describe(event_id="Event ID")
    async def end_bracket_cmd(
        self, ctx: commands.Context, event_id: int
    ) -> None:
        if not await self._check_admin(ctx):
            return
        if ctx.interaction:
            await ctx.interaction.response.defer(ephemeral=True)

        ev = get_event(event_id)
        if not ev:
            await ctx.send(embed=error("Event not found."))
            return

        result = finalize_bracket(event_id)
        if not result["ok"]:
            await ctx.send(embed=error(result.get("error", "Couldn't finalize the bracket.")))
            return

        standings = result["standings"]
        medals = ["🥇", "🥈", "🥉"]
        lines = [f"🏆 **{ev['name']} — Final Standings**\n"]
        names = {
            p["id"]: (p.get("game_username") or p.get("username"))
            for p in get_event_players(event_id)
        }
        for i, s in enumerate(standings):
            medal = medals[i] if i < 3 else f"{i+1}."
            name = names.get(s["player_id"], f"Player {s['player_id']}")
            lines.append(f"{medal} **{name}** — #{s['placement']}")

        pr_map = calc_event_pr(event_id)
        for did, pr_val in pr_map.items():
            execute("UPDATE vtx_players SET pr = %s WHERE discord_id = %s", (pr_val, did))
        await self._sync_ranks(ctx, pr_map)

        if standings:
            winner_row = query_one(
                "SELECT discord_id FROM vtx_players WHERE id = %s",
                (standings[0]["player_id"],),
            )
            if winner_row:
                from ranks import sync_crown_role

                await sync_crown_role(ctx.guild, winner_row["discord_id"])

        channel = ctx.guild.get_channel(
            int(ev["dispatch_channel_id"] or ev["channel_id"] or 0)
        )
        if channel:
            try:
                await channel.send("\n".join(lines))
            except Exception:
                pass

        await ctx.send(
            embed=success(
                f"Bracket **{ev['name']}** finalized — {len(standings)} placements "
                "recorded."
            )
        )

    async def _sync_ranks(self, ctx: commands.Context, pr_map: dict) -> None:
        for did in pr_map:
            member = ctx.guild.get_member(int(did))
            if not member:
                continue
            try:
                p = query_one("SELECT pr FROM vtx_players WHERE discord_id = %s", (did,))
                await sync_rank_role(ctx.guild, member, (p["pr"] if p else 0) or 0)
            except Exception:
                pass

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
    await bot.add_cog(BracketsCog(bot))