from __future__ import annotations

import discord
from discord import app_commands
from discord.ext import commands
from embeds import base, error, success

from database import (
    auto_split_lobbies,
    execute,
    get_event,
    get_event_lobbies,
    get_lobby,
    get_lobby_leaderboard,
    get_lobby_players,
    query_one,
)


class LobbiesCog(commands.Cog):
    def __init__(self, bot: commands.Bot) -> None:
        self.bot = bot

    @commands.hybrid_command(
        name="create-lobby",
        description="Create a lobby for an event to organize players into groups",
    )
    @app_commands.describe(event_id="Event ID", name="Lobby name")
    async def create_lobby(
        self,
        ctx: commands.Context,
        event_id: int,
        name: str,
    ) -> None:
        if not await self._check_admin(ctx):
            return

        ev = get_event(event_id)
        if not ev:
            await ctx.send(embed=error("Event not found."))
            return

        lobby_id = execute(
            "INSERT INTO lobbies (event_id, name) VALUES (?, ?)",
            (event_id, name),
        )

        embed = base(f"🏟️ Lobby Created: {name}", 0x2ECC71)
        embed.add_field(name="Lobby ID", value=str(lobby_id), inline=True)
        embed.add_field(name="Event", value=ev["name"], inline=True)
        embed.set_footer(text="Use /join-lobby to add players")

        await ctx.send(embed=embed)

    @commands.hybrid_command(
        name="join-lobby",
        description="Add a player to a lobby by lobby ID",
    )
    @app_commands.describe(lobby_id="Lobby ID", player="Player to add")
    async def join_lobby(
        self,
        ctx: commands.Context,
        lobby_id: int,
        player: discord.Member,
    ) -> None:
        if not await self._check_admin(ctx):
            return

        lobby = get_lobby(lobby_id)
        if not lobby:
            await ctx.send(embed=error("Lobby not found."))
            return

        existing = query_one(
            "SELECT * FROM lobby_players WHERE lobby_id = ? AND player_id = ?",
            (lobby_id, player.id),
        )
        if existing:
            await ctx.send(
                embed=error(f"{player.mention} is already in this lobby."),
            )
            return

        execute(
            "INSERT INTO lobby_players (lobby_id, player_id) VALUES (?, ?)",
            (lobby_id, player.id),
        )

        players = get_lobby_players(lobby_id)
        embed = base(f"🏟️ {lobby['name']}", 0x2ECC71)
        embed.description = "\n".join(
            f"• {p['username']}" for p in players
        )
        embed.set_footer(text=f"{len(players)} player(s) in lobby")

        await ctx.send(embed=embed)

    @commands.hybrid_command(
        name="remove-from-lobby",
        description="Remove a player from a lobby by lobby ID",
    )
    @app_commands.describe(lobby_id="Lobby ID", player="Player to remove")
    async def remove_from_lobby(
        self,
        ctx: commands.Context,
        lobby_id: int,
        player: discord.Member,
    ) -> None:
        if not await self._check_admin(ctx):
            return

        lobby = get_lobby(lobby_id)
        if not lobby:
            await ctx.send(embed=error("Lobby not found."))
            return

        execute(
            "DELETE FROM lobby_players WHERE lobby_id = ? AND player_id = ?",
            (lobby_id, player.id),
        )

        await ctx.send(
            embed=success(f"Removed {player.mention} from lobby **{lobby['name']}**"),
        )

    @commands.hybrid_command(
        name="lobby-info",
        description="Show lobby details: status, room code, and list of players",
    )
    @app_commands.describe(lobby_id="Lobby ID")
    async def lobby_info(
        self, ctx: commands.Context, lobby_id: int
    ) -> None:
        lobby = get_lobby(lobby_id)
        if not lobby:
            await ctx.send(embed=error("Lobby not found."))
            return

        players = get_lobby_players(lobby_id)
        embed = base(f"🏟️ {lobby['name']}", 0x3498DB)
        embed.add_field(name="Status", value=lobby["status"], inline=True)
        if lobby.get("room_code"):
            embed.add_field(name="Room Code", value=lobby["room_code"], inline=True)

        if players:
            embed.description = "\n".join(
                f"• {p['username']}" for p in players
            )
        else:
            embed.description = "No players in lobby."

        embed.set_footer(text=f"{len(players)} player(s)")
        await ctx.send(embed=embed)

    @commands.hybrid_command(
        name="lobbies",
        description="List all lobbies for an event with player count and status",
    )
    @app_commands.describe(event_id="Event ID")
    async def list_lobbies(
        self, ctx: commands.Context, event_id: int
    ) -> None:
        ev = get_event(event_id)
        if not ev:
            await ctx.send(embed=error("Event not found."))
            return

        lobbies = get_event_lobbies(event_id)
        if not lobbies:
            await ctx.send(
                embed=base(f"🏟️ {ev['name']} — No lobbies"),
            )
            return

        lines = []
        for lob in lobbies:
            players = get_lobby_players(lob["id"])
            lines.append(
                f"**{lob['name']}** (ID: {lob['id']}) — "
                f"{len(players)} players — {lob['status']}"
            )

        embed = base(f"🏟️ {ev['name']} — Lobbies")
        embed.description = "\n".join(lines)
        await ctx.send(embed=embed)

    @commands.hybrid_command(
        name="set-lobby-code",
        description="Set the room code for a specific lobby",
    )
    @app_commands.describe(lobby_id="Lobby ID", room_code="Room code")
    async def set_lobby_code(
        self,
        ctx: commands.Context,
        lobby_id: int,
        room_code: str,
    ) -> None:
        if not await self._check_admin(ctx):
            return

        lobby = get_lobby(lobby_id)
        if not lobby:
            await ctx.send(embed=error("Lobby not found."))
            return

        execute(
            "UPDATE lobbies SET room_code = ? WHERE id = ?",
            (room_code, lobby_id),
        )

        await ctx.send(
            embed=success(
                f"Room code for **{lobby['name']}** set to **{room_code}**"
            ),
        )

    @commands.hybrid_command(
        name="close-lobby",
        description="Close a lobby and prevent further joins",
    )
    @app_commands.describe(lobby_id="Lobby ID")
    async def close_lobby(
        self, ctx: commands.Context, lobby_id: int
    ) -> None:
        if not await self._check_admin(ctx):
            return

        lobby = get_lobby(lobby_id)
        if not lobby:
            await ctx.send(embed=error("Lobby not found."))
            return

        execute(
            "UPDATE lobbies SET status = 'closed' WHERE id = ?",
            (lobby_id,),
        )

        await ctx.send(
            embed=success(f"Lobby **{lobby['name']}** closed."),
        )

    @commands.hybrid_command(
        name="split-lobbies",
        description="Auto-split an event's registrations into lobbies (teams stay together)",
    )
    @app_commands.describe(event_id="Event ID", force="Re-split even if lobbies already exist")
    async def split_lobbies(
        self, ctx: commands.Context, event_id: int, force: bool = False
    ) -> None:
        if not await self._check_admin(ctx):
            return
        if ctx.interaction:
            await ctx.interaction.response.defer(ephemeral=True)

        ev = get_event(event_id)
        if not ev:
            await ctx.send(embed=error("Event not found."))
            return

        lobbies = auto_split_lobbies(event_id, force=force)
        if not lobbies:
            existing = get_event_lobbies(event_id)
            if existing:
                await ctx.send(
                    embed=base(
                        f"🏟️ {ev['name']} — {len(existing)} lobby(s) already exist"
                    )
                )
            else:
                await ctx.send(
                    embed=success(
                        f"No split needed for **{ev['name']}** — "
                        "everyone fits in one lobby. Use `/create-lobby` or "
                        "`/join-lobby` to organize manually."
                    )
                )
            return

        embed = base(f"🏟️ {ev['name']} — Split into {len(lobbies)} Lobbies", 0x2ECC71)
        lines = []
        for lob in lobbies:
            players = get_lobby_players(lob["id"])
            names = ", ".join(p["username"] for p in players)
            lines.append(f"**{lob['name']}** (ID: {lob['id']}) — {len(players)} players\n{names}")
        embed.description = "\n\n".join(lines)
        embed.set_footer(text="Use /start-session with the lobby ID for lobby-scoped matches")
        await ctx.send(embed=embed)

    @commands.hybrid_command(
        name="lobby-leaderboard",
        description="Show a lobby's leaderboard across all of its matches",
    )
    @app_commands.describe(lobby_id="Lobby ID")
    async def lobby_leaderboard(
        self, ctx: commands.Context, lobby_id: int
    ) -> None:
        lobby = get_lobby(lobby_id)
        if not lobby:
            await ctx.send(embed=error("Lobby not found."))
            return

        board = get_lobby_leaderboard(lobby_id)
        if not board:
            await ctx.send(
                embed=base(f"🏟️ {lobby['name']} — No scores yet"),
            )
            return

        medals = ["🥇", "🥈", "🥉"]
        lines = []
        for i, row in enumerate(board[:10]):
            medal = medals[i] if i < 3 else f"{i+1}."
            dq = " 🚫" if row.get("is_dq") else ""
            name = row.get("username") or row.get("team_name", "Unknown")
            lines.append(
                f"{medal} **{name}** — {row['total_points']} pts "
                f"({row['total_kills']} kills) | {row.get('wins', 0)}W"
                f" | avg {row.get('avg_points', 0)} pts | "
                f"{row.get('placement_points', 0)} pp{dq}"
            )

        embed = base(f"🏟️ {lobby['name']} — Leaderboard", 0x3498DB)
        embed.description = "\n".join(lines)
        embed.set_footer(text=f"{len(board)} player(s) | Lobby ID: {lobby_id}")
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
        from config import settings

        admin_role_id = settings.discord_admin_role_id
        if admin_role_id:
            role = ctx.guild.get_role(int(admin_role_id))
            if role and role in member.roles:
                return True
        await ctx.send(embed=error("You need admin permission."))
        return False


async def setup(bot: commands.Bot) -> None:
    await bot.add_cog(LobbiesCog(bot))
