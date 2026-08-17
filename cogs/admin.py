from __future__ import annotations

import re
from datetime import datetime, timedelta

import discord
from config import digits_only, settings
from discord import app_commands
from discord.ext import commands
from embeds import base, error, success

from database import (
    add_event_qualifier,
    add_player_pr,
    add_player_to_event,
    ban_player,
    execute,
    get_event,
    get_event_games,
    get_event_qualifiers,
    get_event_registrations,
    get_game_players,
    get_leaderboard,
    get_player_ban,
    get_rank_for_pr,
    get_rank_tiers,
    get_season,
    get_server_legend,
    get_team_leaderboard,
    log_bot_action,
    move_qualifiers,
    query,
    query_one,
    remove_event_qualifier,
    remove_player_from_event,
    season_reset,
    set_player_pr,
    start_season as db_start_season,
    unban_player,
)
from ranks import get_player_rank, sync_legend_role, sync_rank_role

KILL_PATTERN = re.compile(
    r"(.+?)\s+(?:killed|eliminated|knocked)\s+(.+?)(?:\s+with\s+(.+))?$",
    re.IGNORECASE,
)


def parse_duration(duration: str) -> timedelta | None:
    """Parse a duration like '2h', '3d', '1w', '90m' into a timedelta."""
    match = re.match(r"^(\d+)\s*([smhdw])$", duration.strip().lower())
    if not match:
        return None
    amount = int(match.group(1))
    unit = match.group(2)
    if unit == "s":
        return timedelta(seconds=amount)
    if unit == "m":
        return timedelta(minutes=amount)
    if unit == "h":
        return timedelta(hours=amount)
    if unit == "d":
        return timedelta(days=amount)
    if unit == "w":
        return timedelta(weeks=amount)
    return None


class AdminCog(commands.Cog):
    def __init__(self, bot: commands.Bot) -> None:
        self.bot = bot

    @commands.hybrid_command(
        name="assign-points",
        description="Assign extra points to a player in a specific game",
    )
    @app_commands.describe(
        event_id="Event ID",
        game_number="Game number",
        player="Player",
        points="Points to assign",
    )
    async def assign_points(
        self,
        ctx: commands.Context,
        event_id: int,
        game_number: int,
        player: discord.Member,
        points: int,
    ) -> None:
        if not await self._check_admin(ctx):
            return

        game = query_one(
            "SELECT * FROM games WHERE event_id = ? AND game_number = ?",
            (event_id, game_number),
        )
        if not game:
            await ctx.send(embed=error("Game not found."))
            return

        p = query_one("SELECT id FROM players WHERE discord_id = ?", (str(player.id),))
        if not p:
            await ctx.send(embed=error("Player not registered."))
            return

        execute(
            "UPDATE game_players SET points = points + ? "
            "WHERE game_id = ? AND player_id = ?",
            (points, game["id"], p["id"]),
        )

        await ctx.send(
            embed=success(
                f"Assigned **{points}** pts to {player.mention} in Game {game_number}"
            ),
        )

    @commands.hybrid_command(
        name="dq-player",
        description="Disqualify a player: sets points to 0, marks DQ, notifies via DM",
    )
    @app_commands.describe(
        event_id="Event ID",
        game_number="Game number",
        player="Player to DQ",
        reason="Reason for DQ",
    )
    async def dq_player(
        self,
        ctx: commands.Context,
        event_id: int,
        game_number: int,
        player: discord.Member,
        reason: str = "No reason given",
    ) -> None:
        if not await self._check_admin(ctx):
            return

        game = query_one(
            "SELECT * FROM games WHERE event_id = ? AND game_number = ?",
            (event_id, game_number),
        )
        if not game:
            await ctx.send(embed=error("Game not found."))
            return

        p = query_one("SELECT id FROM players WHERE discord_id = ?", (str(player.id),))
        if not p:
            await ctx.send(embed=error("Player not registered."))
            return

        execute(
            "UPDATE game_players SET is_disqualified = 1, points = 0 "
            "WHERE game_id = ? AND player_id = ?",
            (game["id"], p["id"]),
        )

        embed = base("🚫 Player Disqualified", 0xE74C3C)
        embed.add_field(name="Player", value=player.mention, inline=True)
        embed.add_field(name="Game", value=str(game_number), inline=True)
        embed.add_field(name="Reason", value=reason, inline=False)

        await ctx.send(embed=embed)

    @commands.hybrid_command(
        name="add-kills",
        description="Set the total kills for a player in a specific game",
    )
    @app_commands.describe(
        event_id="Event ID",
        game_number="Game number",
        player="Player",
        kills="Number of kills",
    )
    async def add_kills(
        self,
        ctx: commands.Context,
        event_id: int,
        game_number: int,
        player: discord.Member,
        kills: int,
    ) -> None:
        if not await self._check_admin(ctx):
            return

        game = query_one(
            "SELECT * FROM games WHERE event_id = ? AND game_number = ?",
            (event_id, game_number),
        )
        if not game:
            await ctx.send(embed=error("Game not found."))
            return

        p = query_one("SELECT id FROM players WHERE discord_id = ?", (str(player.id),))
        if not p:
            await ctx.send(embed=error("Player not registered."))
            return

        execute(
            "UPDATE game_players SET kills = ? WHERE game_id = ? AND player_id = ?",
            (kills, game["id"], p["id"]),
        )

        await ctx.send(
            embed=success(
                f"Set **{kills}** kills for {player.mention} in Game {game_number}"
            ),
        )

    @commands.hybrid_command(
        name="game-stats",
        description="Show detailed stats for a specific game: players, kills, points, placement",
    )
    @app_commands.describe(event_id="Event ID", game_number="Game number")
    async def game_stats(
        self, ctx: commands.Context, event_id: int, game_number: int
    ) -> None:
        game = query_one(
            "SELECT * FROM games WHERE event_id = ? AND game_number = ?",
            (event_id, game_number),
        )
        if not game:
            await ctx.send(embed=error("Game not found."))
            return

        players = get_game_players(game["id"])
        if not players:
            await ctx.send(
                embed=base(f"📊 Game {game_number} — No player data"),
            )
            return

        lines = []
        for i, p in enumerate(players, 1):
            dq = " 🚫" if p["is_disqualified"] else ""
            placement = f"#{p['placement']}" if p.get("placement") else "-"
            lines.append(
                f"{i}. **{p['username']}** — {p['points']} pts, "
                f"{p['kills']} kills, {placement}{dq}"
            )

        embed = base(f"📊 Game {game_number} Stats")
        embed.description = "\n".join(lines)
        embed.set_footer(text=f"Status: {game['status']}")
        await ctx.send(embed=embed)

    @commands.hybrid_command(
        name="log-leaderboard",
        description="Post the event leaderboard to the dispatch channel and log it",
    )
    @app_commands.describe(
        event_id="Event ID",
        limit="Number of entries to show (default 15)",
        channel="Leaderboard channel to post to (optional, defaults to the event dispatch channel)",
    )
    async def log_leaderboard(
        self,
        ctx: commands.Context,
        event_id: int,
        limit: int = 15,
        channel: discord.TextChannel | None = None,
    ) -> None:
        if not await self._check_admin(ctx):
            return
        if ctx.interaction:
            await ctx.interaction.response.defer(ephemeral=True)

        ev = get_event(event_id)
        if not ev:
            await ctx.send(embed=error("Event not found."))
            return

        from database import get_leaderboard_full

        board = get_leaderboard_full(event_id)

        if not board:
            await ctx.send(embed=base(f"🏆 {ev['name']} — No scores yet."))
            return

        medals = ["🥇", "🥈", "🥉"]
        lines = []
        for i, row in enumerate(board[:limit]):
            medal = medals[i] if i < 3 else f"{i+1}."
            name = row.get("username") or row.get("team_name", "Unknown")
            placements = row.get("placements") or []
            pl_str = ", ".join(f"#{p}" for p in placements) if placements else "—"
            if row.get("is_dq"):
                lines.append(f"{medal} ~~{name}~~ — **DQ**")
            else:
                lines.append(
                    f"{medal} **{name}** — {row['total_points']} pts ({row['total_kills']} kills) "
                    f"| {row.get('wins', 0)}W | avg #{row.get('avg_placement') or '—'} "
                    f"| {pl_str} | {row.get('placement_points', 0)} pp"
                )

        embed = base(f"🏆 {ev['name']} — Leaderboard")
        embed.description = "\n".join(lines)

        from templates_fmt import role_ping

        ping = role_ping(settings.discord_tournament_role_id)

        target = channel or ctx.guild.get_channel(
            int(ev["dispatch_channel_id"] or ev["channel_id"] or 0)
        )
        if target:
            try:
                await target.send(ping, embed=embed)
            except Exception:
                pass

        log_channel_id = digits_only(settings.discord_leaderboard_log_channel_id)
        if log_channel_id:
            log_channel = ctx.guild.get_channel(int(log_channel_id))
            if log_channel:
                try:
                    await log_channel.send(embed=embed)
                except Exception:
                    pass

        log_bot_action(event_id, "log_leaderboard", f"Posted leaderboard ({len(lines)} entries)", str(ctx.author.id))
        await ctx.send(
            embed=success(f"Leaderboard for **{ev['name']}** posted."),
        )

    @commands.Cog.listener()
    async def on_message(self, message: discord.Message) -> None:
        if message.author.bot:
            return
        if not message.guild:
            return

        match = KILL_PATTERN.match(message.content)
        if not match:
            return

        killer_name = match.group(1).strip()
        victim_name = match.group(2).strip()
        weapon = match.group(3).strip() if match.group(3) else None

        events = get_active_games_for_channel(str(message.channel.id))
        if not events:
            return

        for ev in events:
            game = query_one(
                "SELECT * FROM games WHERE event_id = ? AND status = 'in_progress' "
                "ORDER BY game_number DESC LIMIT 1",
                (ev["id"],),
            )
            if not game:
                continue

            killer = query_one(
                "SELECT id FROM players WHERE username = ?", (killer_name,)
            )
            victim = query_one(
                "SELECT id FROM players WHERE username = ?", (victim_name,)
            )

            if killer and victim:
                execute(
                    "INSERT INTO kills (game_id, killer_id, victim_id, weapon) "
                    "VALUES (?, ?, ?, ?)",
                    (game["id"], killer["id"], victim["id"], weapon),
                )
                execute(
                    "UPDATE game_players SET kills = kills + 1 "
                    "WHERE game_id = ? AND player_id = ?",
                    (game["id"], killer["id"]),
                )
                try:
                    await message.add_reaction("💀")
                except Exception:
                    pass

    admin_group = app_commands.Group(name="admin", description="Admin commands")
    admin_add_group = app_commands.Group(name="add", description="Add points, kills, or PR", parent=admin_group)
    admin_set_group = app_commands.Group(name="set", description="Set a player's PR", parent=admin_group)

    @admin_group.command(name="events", description="List all events with status, format, and region")
    async def admin_events(self, interaction: discord.Interaction) -> None:
        if not await self._check_admin_interaction(interaction):
            return
        events = query("SELECT * FROM events ORDER BY created_at DESC LIMIT 20")
        if not events:
            await interaction.response.send_message(
                embed=base("📋 No events found."), ephemeral=True
            )
            return
        lines = []
        for ev in events:
            status_badge = {"setup": "🟡", "registration": "🟢", "in_progress": "🔵", "completed": "⚪"}.get(ev["status"], "❓")
            games_label = f"{ev['total_games']} games"
            if not (ev.get("total_games") or 0):
                games_label = "∞ games"
            lines.append(
                f"**{ev['name']}** (ID: {ev['id']}) {status_badge} {ev['status']}\n"
                f"  {ev['event_format']} | {ev['region']} | {games_label}"
            )
        embed = base("📋 Events")
        embed.description = "\n\n".join(lines)
        await interaction.response.send_message(embed=embed, ephemeral=True)

    @admin_group.command(name="listplayers", description="List all registered players for an event with team info")
    @app_commands.describe(event_id="Event ID")
    async def admin_listplayers(self, interaction: discord.Interaction, event_id: int) -> None:
        if not await self._check_admin_interaction(interaction):
            return
        ev = get_event(event_id)
        if not ev:
            await interaction.response.send_message(
                embed=error("Event not found."), ephemeral=True
            )
            return
        regs = get_event_registrations(event_id)
        if not regs:
            await interaction.response.send_message(
                embed=base(f"📋 {ev['name']} — No players registered."), ephemeral=True
            )
            return
        lines = []
        for i, r in enumerate(regs, 1):
            team = ""
            if r.get("team_members"):
                members = r["team_members"].split(",")
                names = []
                for mid in members:
                    p = query_one("SELECT username FROM players WHERE discord_id = ?", (mid.strip(),))
                    names.append(p["username"] if p else mid)
                team = f" → {' + '.join(names)}"
            lines.append(f"{i}. {r['username']} ({r['discord_id']}){team}")
        embed = base(f"📋 {ev['name']} — Players ({len(regs)} registrations)")
        embed.description = "\n".join(lines)
        await interaction.response.send_message(embed=embed, ephemeral=True)

    @admin_group.command(name="matches", description="List all matches for an event with status, players, and room code")
    @app_commands.describe(event_id="Event ID")
    async def admin_matches(self, interaction: discord.Interaction, event_id: int) -> None:
        if not await self._check_admin_interaction(interaction):
            return
        ev = get_event(event_id)
        if not ev:
            await interaction.response.send_message(
                embed=error("Event not found."), ephemeral=True
            )
            return
        games = get_event_games(event_id)
        if not games:
            await interaction.response.send_message(
                embed=base(f"📋 {ev['name']} — No matches yet."), ephemeral=True
            )
            return
        lines = []
        for g in games:
            status_icon = {"waiting": "⏳", "in_progress": "🔵", "completed": "✅"}.get(g["status"], "❓")
            players = get_game_players(g["id"])
            lines.append(
                f"Game {g['game_number']} {status_icon} {g['status']}\n"
                f"  {len(players)} players | Code: {g.get('room_code', 'N/A')}"
            )
        embed = base(f"📋 {ev['name']} — Matches ({len(games)})")
        embed.description = "\n\n".join(lines)
        await interaction.response.send_message(embed=embed, ephemeral=True)

    @admin_group.command(name="leaderboard", description="Show the event leaderboard with points, kills, and DQ status")
    @app_commands.describe(event_id="Event ID")
    async def admin_leaderboard(self, interaction: discord.Interaction, event_id: int) -> None:
        if not await self._check_admin_interaction(interaction):
            return
        ev = get_event(event_id)
        if not ev:
            await interaction.response.send_message(
                embed=error("Event not found."), ephemeral=True
            )
            return
        if ev.get("team_size", 1) >= 2:
            board = get_team_leaderboard(event_id)
        else:
            board = get_leaderboard(event_id)
        if not board:
            await interaction.response.send_message(
                embed=base(f"🏆 {ev['name']} — No scores yet."), ephemeral=True
            )
            return
        medals = ["🥇", "🥈", "🥉"]
        lines = []
        for i, row in enumerate(board[:15]):
            medal = medals[i] if i < 3 else f"{i+1}."
            name = row.get("username") or row.get("team_name", "Unknown")
            if row.get("is_dq"):
                lines.append(f"{medal} ~~{name}~~ — **DQ**")
            else:
                lines.append(f"{medal} **{name}** — {row['total_points']} pts ({row['total_kills']} kills)")
        embed = base(f"🏆 {ev['name']} — Leaderboard")
        embed.description = "\n".join(lines)
        await interaction.response.send_message(embed=embed, ephemeral=True)

    @admin_group.command(name="info", description="Show detailed event info: status, format, points, room code, registrations")
    @app_commands.describe(event_id="Event ID")
    async def admin_info(self, interaction: discord.Interaction, event_id: int) -> None:
        if not await self._check_admin_interaction(interaction):
            return
        ev = get_event(event_id)
        if not ev:
            await interaction.response.send_message(
                embed=error("Event not found."), ephemeral=True
            )
            return
        team_label = {1: "Solo", 2: "Duo", 3: "Trio"}.get(ev["team_size"], "Solo")
        embed = base(f"ℹ️ {ev['name']}")
        embed.add_field(name="ID", value=str(ev["id"]), inline=True)
        embed.add_field(name="Status", value=ev["status"], inline=True)
        embed.add_field(name="Format", value=ev["event_format"], inline=True)
        embed.add_field(name="Region", value=ev["region"], inline=True)
        embed.add_field(name="Team", value=team_label, inline=True)
        games_display = f"{ev['current_game']}/∞"
        if (ev.get("total_games") or 0) > 0:
            games_display = f"{ev['current_game']}/{ev['total_games']}"
        embed.add_field(name="Games", value=games_display, inline=True)
        embed.add_field(name="Points", value=f"Kill: {ev['point_kill']} | Win: {ev['point_win']}", inline=True)
        embed.add_field(name="Room Code", value=ev.get("room_code") or "N/A", inline=True)
        regs = get_event_registrations(event_id)
        embed.add_field(name="Registered", value=str(len(regs)), inline=True)
        games = get_event_games(event_id)
        embed.add_field(name="Matches Played", value=str(len(games)), inline=True)
        await interaction.response.send_message(embed=embed, ephemeral=True)

    @admin_add_group.command(name="points", description="Add points to a player in a specific game")
    @app_commands.describe(
        event_id="Event ID",
        game_number="Game number",
        player="Player",
        points="Points to add",
    )
    async def admin_add_points(
        self,
        interaction: discord.Interaction,
        event_id: int,
        game_number: int,
        player: discord.Member,
        points: int,
    ) -> None:
        if not await self._check_admin_interaction(interaction):
            return
        game = query_one(
            "SELECT * FROM games WHERE event_id = ? AND game_number = ?",
            (event_id, game_number),
        )
        if not game:
            await interaction.response.send_message(embed=error("Game not found."), ephemeral=True)
            return
        p = query_one("SELECT id FROM players WHERE discord_id = ?", (str(player.id),))
        if not p:
            await interaction.response.send_message(embed=error("Player not registered."), ephemeral=True)
            return
        execute(
            "UPDATE game_players SET points = points + ? WHERE game_id = ? AND player_id = ?",
            (points, game["id"], p["id"]),
        )
        log_bot_action(event_id, "admin_add_points", f"+{points} pts to {player.name} in Game {game_number}", str(interaction.user.id))
        await interaction.response.send_message(
            embed=success(f"Added **{points}** pts to {player.mention} in Game {game_number}"),
            ephemeral=True,
        )

    @admin_add_group.command(name="kill", description="Add kills to a player in a specific game")
    @app_commands.describe(
        event_id="Event ID",
        game_number="Game number",
        player="Player",
        kills="Kills to add",
    )
    async def admin_add_kill(
        self,
        interaction: discord.Interaction,
        event_id: int,
        game_number: int,
        player: discord.Member,
        kills: int,
    ) -> None:
        if not await self._check_admin_interaction(interaction):
            return
        game = query_one(
            "SELECT * FROM games WHERE event_id = ? AND game_number = ?",
            (event_id, game_number),
        )
        if not game:
            await interaction.response.send_message(embed=error("Game not found."), ephemeral=True)
            return
        p = query_one("SELECT id FROM players WHERE discord_id = ?", (str(player.id),))
        if not p:
            await interaction.response.send_message(embed=error("Player not registered."), ephemeral=True)
            return
        execute(
            "UPDATE game_players SET kills = kills + ? WHERE game_id = ? AND player_id = ?",
            (kills, game["id"], p["id"]),
        )
        log_bot_action(event_id, "admin_add_kill", f"+{kills} kills to {player.name} in Game {game_number}", str(interaction.user.id))
        await interaction.response.send_message(
            embed=success(f"Added **{kills}** kills to {player.mention} in Game {game_number}"),
            ephemeral=True,
        )

    @admin_add_group.command(name="pr", description="Add PR to a player's overall rating and update their rank role")
    @app_commands.describe(player="Player", amount="PR amount to add")
    async def admin_add_pr(
        self,
        interaction: discord.Interaction,
        player: discord.Member,
        amount: int,
    ) -> None:
        if not await self._check_admin_interaction(interaction):
            return
        new_pr = add_player_pr(str(player.id), amount)
        rank = await self._apply_rank(interaction, player, new_pr)
        log_bot_action(None, "admin_add_pr", f"+{amount} PR to {player.name} (now {new_pr})", str(interaction.user.id))
        await interaction.response.send_message(
            embed=success(f"Added **{amount}** PR to {player.mention} — now **{new_pr}** ({rank})"),
            ephemeral=True,
        )

    @admin_set_group.command(name="pr", description="Set a player's PR to an exact value and update their rank role")
    @app_commands.describe(player="Player", pr="New PR value")
    async def admin_set_pr(
        self,
        interaction: discord.Interaction,
        player: discord.Member,
        pr: int,
    ) -> None:
        if not await self._check_admin_interaction(interaction):
            return
        if pr < 0:
            await interaction.response.send_message(embed=error("PR cannot be negative."), ephemeral=True)
            return
        new_pr = set_player_pr(str(player.id), pr)
        rank = await self._apply_rank(interaction, player, new_pr)
        log_bot_action(None, "admin_set_pr", f"Set {player.name} PR to {new_pr}", str(interaction.user.id))
        await interaction.response.send_message(
            embed=success(f"Set {player.mention} PR to **{new_pr}** ({rank})"),
            ephemeral=True,
        )

    @admin_group.command(name="ban", description="Temporarily ban a player from registering")
    @app_commands.describe(
        player="Player to ban",
        duration="Ban duration, e.g. 1h, 2d, 1w (use 0 for permanent)",
        reason="Reason for the ban",
    )
    async def admin_ban(
        self,
        interaction: discord.Interaction,
        player: discord.Member,
        duration: str,
        reason: str = "",
    ) -> None:
        if not await self._check_admin_interaction(interaction):
            return
        if duration.strip() == "0":
            banned_until = None
        else:
            delta = parse_duration(duration)
            if not delta:
                await interaction.response.send_message(
                    embed=error("Invalid duration. Use formats like `2h`, `3d`, `1w`, or `0` for permanent."),
                    ephemeral=True,
                )
                return
            banned_until = (datetime.utcnow() + delta).isoformat()

        ban_player(str(player.id), banned_until or "", reason=reason, created_by=str(interaction.user.id))
        log_bot_action(None, "admin_ban", f"Banned {player.name}: {reason or 'no reason'} until {banned_until or 'permanent'}", str(interaction.user.id))
        until_text = "permanently" if not banned_until else f"until `{banned_until}`"
        await interaction.response.send_message(
            embed=success(f"Banned {player.mention} {until_text}" + (f"\nReason: {reason}" if reason else "")),
            ephemeral=True,
        )

    @admin_group.command(name="unban", description="Remove a ban from a player")
    @app_commands.describe(player="Player to unban")
    async def admin_unban(
        self,
        interaction: discord.Interaction,
        player: discord.Member,
    ) -> None:
        if not await self._check_admin_interaction(interaction):
            return
        ban = get_player_ban(str(player.id))
        if not ban:
            await interaction.response.send_message(embed=error(f"{player.mention} is not banned."), ephemeral=True)
            return
        unban_player(str(player.id))
        log_bot_action(None, "admin_unban", f"Unbanned {player.name}", str(interaction.user.id))
        await interaction.response.send_message(
            embed=success(f"Unbanned {player.mention}."),
            ephemeral=True,
        )

    @admin_group.command(name="rank", description="Show a player's current PR and rank")
    @app_commands.describe(player="Player to check")
    async def admin_rank(
        self,
        interaction: discord.Interaction,
        player: discord.Member,
    ) -> None:
        if not await self._check_admin_interaction(interaction):
            return
        rank_info = get_player_rank(str(player.id))
        if not rank_info:
            await interaction.response.send_message(embed=error(f"{player.mention} is not registered."), ephemeral=True)
            return
        embed = base(f"🏅 {player.display_name}'s Rank")
        embed.add_field(name="PR", value=str(rank_info["pr"]), inline=True)
        embed.add_field(name="Rank", value=rank_info["rank"], inline=True)
        next_tiers = [t for t in get_rank_tiers() if t["pr_min"] > rank_info["pr"]]
        if next_tiers:
            next_tier = next_tiers[-1]
            embed.add_field(
                name="Next Rank",
                value=f"{next_tier['name']} ({next_tier['pr_min']} PR)",
                inline=True,
            )
        await interaction.response.send_message(embed=embed, ephemeral=True)

    @admin_group.command(name="ranks", description="Show the PR rank ladder")
    async def admin_ranks(self, interaction: discord.Interaction) -> None:
        if not await self._check_admin_interaction(interaction):
            return
        tiers = get_rank_tiers()
        lines = []
        for i, tier in enumerate(tiers, 1):
            next_min = tiers[i - 2]["pr_min"] if i - 2 >= 0 else None
            range_text = f"{tier['pr_min']}+ PR" if next_min is None else f"{tier['pr_min']}–{next_min - 1} PR"
            lines.append(f"{i}. **{tier['name']}** — {range_text}")
        embed = base("📈 PR Rank Ladder")
        embed.description = "\n".join(lines)
        await interaction.response.send_message(embed=embed, ephemeral=True)

    @admin_group.command(name="reset-pr", description="Reset a player's PR back to 0 (Unranked)")
    @app_commands.describe(player="Player to reset")
    async def admin_reset_pr(
        self,
        interaction: discord.Interaction,
        player: discord.Member,
    ) -> None:
        if not await self._check_admin_interaction(interaction):
            return
        new_pr = set_player_pr(str(player.id), 0)
        rank = await self._apply_rank(interaction, player, new_pr)
        log_bot_action(None, "admin_reset_pr", f"Reset {player.name} PR to 0", str(interaction.user.id))
        await interaction.response.send_message(
            embed=success(f"Reset {player.mention} PR to **0** ({rank})"),
            ephemeral=True,
        )

    @admin_group.command(name="addplayer", description="Add a player to an event's confirmed registration")
    @app_commands.describe(event_id="Event ID", player="Player to add")
    async def admin_add_player(
        self,
        interaction: discord.Interaction,
        event_id: int,
        player: discord.Member,
    ) -> None:
        if not await self._check_admin_interaction(interaction):
            return
        ev = get_event(event_id)
        if not ev:
            await interaction.response.send_message(embed=error("Event not found."), ephemeral=True)
            return
        from database import check_event_entry

        entry = check_event_entry(event_id, str(player.id))
        if not entry["ok"]:
            await interaction.response.send_message(
                embed=error(entry["reason"]), ephemeral=True
            )
            return
        add_player_to_event(event_id, str(player.id), player.display_name)
        log_bot_action(event_id, "admin_add_player", f"Added {player.name} to event", str(interaction.user.id))
        await interaction.response.send_message(
            embed=success(f"Added {player.mention} to **{ev['name']}**."),
            ephemeral=True,
        )

    @admin_group.command(name="removeplayer", description="Remove a player from an event (registration, matches, lobbies)")
    @app_commands.describe(event_id="Event ID", player="Player to remove")
    async def admin_remove_player(
        self,
        interaction: discord.Interaction,
        event_id: int,
        player: discord.Member,
    ) -> None:
        if not await self._check_admin_interaction(interaction):
            return
        ev = get_event(event_id)
        if not ev:
            await interaction.response.send_message(embed=error("Event not found."), ephemeral=True)
            return
        remove_player_from_event(event_id, str(player.id))
        log_bot_action(event_id, "admin_remove_player", f"Removed {player.name} from event", str(interaction.user.id))
        await interaction.response.send_message(
            embed=success(f"Removed {player.mention} from **{ev['name']}**."),
            ephemeral=True,
        )

    @admin_group.command(name="sync-ranks", description="Re-sync rank roles for all registered players + legend")
    async def admin_sync_ranks(self, interaction: discord.Interaction) -> None:
        if not await self._check_admin_interaction(interaction):
            return
        await interaction.response.defer(ephemeral=True)

        players = query("SELECT discord_id, pr FROM players")
        updated = 0
        for p in players:
            member = interaction.guild.get_member(int(p["discord_id"]))
            if not member:
                continue
            try:
                await sync_rank_role(interaction.guild, member, p["pr"] or 0)
                updated += 1
            except Exception:
                pass

        legend_name = "none"
        try:
            legend_member = await sync_legend_role(interaction.guild)
            if legend_member:
                legend_name = legend_member.display_name
        except Exception:
            pass

        log_bot_action(None, "admin_sync_ranks", f"Synced {updated} players, legend: {legend_name}", str(interaction.user.id))
        await interaction.followup.send(
            embed=success(f"Synced **{updated}** rank roles.\nUnreal Legend: **{legend_name}**"),
            ephemeral=True,
        )

    @admin_group.command(name="legend", description="Show the current Unreal Legend (best PR/wins/kills on the server)")
    async def admin_legend(self, interaction: discord.Interaction) -> None:
        if not await self._check_admin_interaction(interaction):
            return
        await interaction.response.defer(ephemeral=True)

        legend = get_server_legend()
        try:
            await sync_legend_role(interaction.guild)
        except Exception:
            pass

        if not legend:
            await interaction.followup.send(
                embed=base("👑 No Unreal Legend yet — no players with stats."),
                ephemeral=True,
            )
            return

        embed = base("👑 Unreal Legend", 0x9B59B6)
        embed.add_field(name="Player", value=legend["username"], inline=True)
        embed.add_field(name="PR", value=str(legend["pr"] or 0), inline=True)
        embed.add_field(name="Wins", value=str(legend["total_wins"] or 0), inline=True)
        embed.add_field(name="Kills", value=str(legend["total_kills"] or 0), inline=True)
        embed.add_field(name="Games", value=str(legend["total_games"] or 0), inline=True)
        await interaction.followup.send(embed=embed, ephemeral=True)

    @admin_group.command(name="season-reset", description="Reset every player's PR to 0 for a new season (confirm with confirm:yes)")
    @app_commands.describe(confirm="Type 'yes' to confirm the reset")
    async def admin_season_reset(
        self,
        interaction: discord.Interaction,
        confirm: str = "",
    ) -> None:
        if not await self._check_admin_interaction(interaction):
            return
        if confirm.strip().lower() != "yes":
            await interaction.response.send_message(
                embed=error("This resets ALL players' PR to 0. Run with `confirm: yes` to proceed."),
                ephemeral=True,
            )
            return

        count = season_reset()
        new_season = get_season()

        removed = 0
        for member in interaction.guild.members:
            if member.bot:
                continue
            rank_roles = [r for r in member.roles if r.name.startswith("Rank ") or r.name == "Unreal Legend"]
            if rank_roles:
                try:
                    await member.remove_roles(*rank_roles, reason="Season reset")
                    removed += 1
                except Exception:
                    pass

        log_bot_action(None, "season_reset", f"Reset PR for {count} players, removed roles from {removed}", str(interaction.user.id))
        await interaction.response.send_message(
            embed=success(
                f"Season reset complete.\n"
                f"PR reset for **{count}** players.\n"
                f"Removed rank roles from **{removed}** members.\n"
                f"Welcome to **Season {new_season}**!"
            ),
            ephemeral=True,
        )

    @admin_group.command(name="start-season", description="Start a new season (announcement dispatch not wired up yet)")
    @app_commands.describe(confirm="Type 'yes' to confirm")
    async def admin_start_season(
        self,
        interaction: discord.Interaction,
        confirm: str = "",
    ) -> None:
        if not await self._check_admin_interaction(interaction):
            return
        if confirm.strip().lower() != "yes":
            await interaction.response.send_message(
                embed=error("Run with `confirm: yes` to start the new season."),
                ephemeral=True,
            )
            return

        new_season = db_start_season()
        log_bot_action(None, "start_season", f"Season {new_season} started (no dispatch yet)", str(interaction.user.id))
        await interaction.response.send_message(
            embed=success(
                f"**Season {new_season}** has started.\n"
                f"Dispatch announcement is not configured yet."
            ),
            ephemeral=True,
        )

    @admin_group.command(name="qualify", description="Add a player to an event's qualified list")
    @app_commands.describe(event_id="Event ID", user="Player to qualify")
    async def admin_qualify(
        self,
        interaction: discord.Interaction,
        event_id: int,
        user: discord.Member,
    ) -> None:
        if not await self._check_admin_interaction(interaction):
            return
        ev = get_event(event_id)
        if not ev:
            await interaction.response.send_message(embed=error("Event not found."), ephemeral=True)
            return
        q = add_event_qualifier(event_id, str(user.id), user.display_name)
        log_bot_action(event_id, "qualify", f"Qualified {user.display_name}", str(interaction.user.id))
        await interaction.response.send_message(
            embed=success(f"⭐ {user.mention} qualified for **{ev['name']}**."),
            ephemeral=True,
        )

    @admin_group.command(name="qualified", description="List the qualified players of an event")
    @app_commands.describe(event_id="Event ID")
    async def admin_qualified(
        self,
        interaction: discord.Interaction,
        event_id: int,
    ) -> None:
        if not await self._check_admin_interaction(interaction):
            return
        ev = get_event(event_id)
        if not ev:
            await interaction.response.send_message(embed=error("Event not found."), ephemeral=True)
            return
        qualifiers = get_event_qualifiers(event_id)
        embed = base(f"⭐ {ev['name']} — Qualified Players", 0x9B59B6)
        if not qualifiers:
            embed.description = "No qualified players yet. Use the dashboard's Players tab or `/admin qualify`."
        else:
            embed.description = "\n".join(
                f"{i}. **{q['username']}**" + (" (team)" if q.get("team_members") else "")
                for i, q in enumerate(qualifiers, 1)
            )
        await interaction.response.send_message(embed=embed, ephemeral=True)

    @admin_group.command(name="remove-qualified", description="Remove a player from an event's qualified list")
    @app_commands.describe(event_id="Event ID", user="Player to remove")
    async def admin_remove_qualified(
        self,
        interaction: discord.Interaction,
        event_id: int,
        user: discord.Member,
    ) -> None:
        if not await self._check_admin_interaction(interaction):
            return
        if not remove_event_qualifier(event_id, str(user.id)):
            await interaction.response.send_message(
                embed=error(f"{user.display_name} is not qualified for this event."),
                ephemeral=True,
            )
            return
        await interaction.response.send_message(
            embed=success(f"{user.mention} removed from the qualified list."),
            ephemeral=True,
        )

    @admin_group.command(
        name="move-qualified",
        description="Move all qualified players from one event to another (no re-registration needed)",
    )
    @app_commands.describe(
        source_event="Event to take qualified players from",
        target_event="Event to register them into",
        confirm="Type 'yes' to confirm",
    )
    async def admin_move_qualified(
        self,
        interaction: discord.Interaction,
        source_event: int,
        target_event: int,
        confirm: str = "",
    ) -> None:
        if not await self._check_admin_interaction(interaction):
            return
        if confirm.strip().lower() != "yes":
            await interaction.response.send_message(
                embed=error("Run with `confirm: yes` to move the qualified players."),
                ephemeral=True,
            )
            return
        src = get_event(source_event)
        dst = get_event(target_event)
        if not src or not dst:
            await interaction.response.send_message(embed=error("Event not found."), ephemeral=True)
            return
        result = move_qualifiers(source_event, target_event)
        embed = base("⭐ Qualified Players Moved", 0x2ECC71)
        embed.description = (
            f"From **{src['name']}** to **{dst['name']}**\n"
            f"Moved: **{result['moved']}** players\n"
            f"Skipped (already registered): **{result['skipped']}**"
        )
        log_bot_action(target_event, "move_qualified", f"Moved {result['moved']} from event {source_event}", str(interaction.user.id))
        await interaction.response.send_message(embed=embed, ephemeral=True)

    @commands.hybrid_command(
        name="removeteam",
        description="Remove a whole team (leader + members) from an event",
    )
    @app_commands.describe(event_id="Event ID", leader="The team leader")
    async def removeteam(
        self,
        ctx: commands.Context,
        event_id: int,
        leader: discord.Member,
    ) -> None:
        if not await self._check_admin(ctx):
            return
        if ctx.interaction:
            await ctx.interaction.response.defer(ephemeral=True)

        from database import remove_team_from_event

        result = remove_team_from_event(event_id, str(leader.id))
        if not result["ok"]:
            await ctx.send(embed=error("No registration found for that team leader."))
            return
        log_bot_action(event_id, "remove_team", f"Leader {leader.id}, {result['removed_members']} members", str(ctx.author.id))
        await ctx.send(
            embed=success(
                f"Removed team led by {leader.mention} "
                f"({result['removed_members']} player(s)) from event **{event_id}**."
            ),
        )

    @commands.hybrid_command(
        name="undq",
        description="Undo a disqualification for a player in a specific game",
    )
    @app_commands.describe(
        event_id="Event ID",
        game_number="Game number",
        player="Player to un-DQ",
    )
    async def undq(
        self,
        ctx: commands.Context,
        event_id: int,
        game_number: int,
        player: discord.Member,
    ) -> None:
        if not await self._check_admin(ctx):
            return
        if ctx.interaction:
            await ctx.interaction.response.defer(ephemeral=True)

        game = query_one(
            "SELECT * FROM games WHERE event_id = ? AND game_number = ?",
            (event_id, game_number),
        )
        if not game:
            await ctx.send(embed=error("Game not found."))
            return

        p = query_one("SELECT id FROM players WHERE discord_id = ?", (str(player.id),))
        if not p:
            await ctx.send(embed=error("Player not registered."))
            return

        gp = query_one(
            "SELECT 1 FROM game_players WHERE game_id = ? AND player_id = ?",
            (game["id"], p["id"]),
        )
        if not gp:
            await ctx.send(embed=error("Player has no record in this game."))
            return

        execute(
            "UPDATE game_players SET is_disqualified = 0 "
            "WHERE game_id = ? AND player_id = ?",
            (game["id"], p["id"]),
        )

        log_bot_action(event_id, "undq", f"Game {game_number}, player {player.id}", str(ctx.author.id))
        await ctx.send(
            embed=success(f"Removed DQ for {player.mention} in Game {game_number}."),
        )

    @commands.hybrid_command(
        name="reset-score",
        description="Delete all games/scores for an event and reopen registration",
    )
    @app_commands.describe(event_id="Event ID", confirm="Type yes to confirm")
    async def reset_score(
        self,
        ctx: commands.Context,
        event_id: int,
        confirm: str = "",
    ) -> None:
        if not await self._check_admin(ctx):
            return
        if ctx.interaction:
            await ctx.interaction.response.defer(ephemeral=True)

        if confirm.strip().lower() != "yes":
            await ctx.send(
                embed=error(
                    "This deletes every game and score for the event. "
                    "Run with `confirm: yes` to proceed."
                )
            )
            return

        from database import reset_event_scores

        ev = get_event(event_id)
        if not ev:
            await ctx.send(embed=error("Event not found."))
            return

        deleted = reset_event_scores(event_id)
        log_bot_action(event_id, "reset_score", f"Deleted {deleted} games", str(ctx.author.id))
        await ctx.send(
            embed=success(
                f"Reset **{ev['name']}**: {deleted} game(s) deleted, "
                "registration reopened."
            ),
        )

    @commands.hybrid_command(
        name="add-coins",
        description="Credit coins to a player",
    )
    @app_commands.describe(player="Player", amount="Amount of coins")
    async def add_coins(
        self,
        ctx: commands.Context,
        player: discord.Member,
        amount: int,
    ) -> None:
        if not await self._check_admin(ctx):
            return
        if amount <= 0:
            await ctx.send(embed=error("Amount must be positive."))
            return

        from database import award_coins, get_coins

        balance = award_coins(str(player.id), amount)
        log_bot_action(None, "add_coins", f"{player.id} +{amount}", str(ctx.author.id))
        await ctx.send(
            embed=success(
                f"Added **{amount}** coins to {player.mention} "
                f"(new balance: **{balance}**)."
            ),
        )

    @commands.hybrid_command(
        name="remove-coins",
        description="Take coins from a player (floors at 0)",
    )
    @app_commands.describe(player="Player", amount="Amount of coins")
    async def remove_coins(
        self,
        ctx: commands.Context,
        player: discord.Member,
        amount: int,
    ) -> None:
        if not await self._check_admin(ctx):
            return
        if amount <= 0:
            await ctx.send(embed=error("Amount must be positive."))
            return

        from database import get_coins, remove_coins

        balance = remove_coins(str(player.id), amount)
        log_bot_action(None, "remove_coins", f"{player.id} -{amount}", str(ctx.author.id))
        await ctx.send(
            embed=success(
                f"Removed **{amount}** coins from {player.mention} "
                f"(new balance: **{balance}**)."
            ),
        )

    @commands.hybrid_command(
        name="reset-coins",
        description="Zero a player's coin balance",
    )
    @app_commands.describe(player="Player")
    async def reset_coins(
        self,
        ctx: commands.Context,
        player: discord.Member,
    ) -> None:
        if not await self._check_admin(ctx):
            return

        from database import reset_coins

        old = reset_coins(str(player.id))
        log_bot_action(None, "reset_coins", f"{player.id} (was {old})", str(ctx.author.id))
        await ctx.send(
            embed=success(
                f"Reset {player.mention}'s coins (previous balance: **{old}**)."
            ),
        )

    async def _apply_rank(self, interaction: discord.Interaction, player: discord.Member, pr: int) -> str:
        try:
            return await sync_rank_role(interaction.guild, player, pr)
        except Exception:
            tier = get_rank_for_pr(pr)
            return tier["name"] if tier else "Unranked"

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

    async def _check_admin_interaction(self, interaction: discord.Interaction) -> bool:
        if not interaction.guild:
            await interaction.response.send_message(
                embed=error("Server only."), ephemeral=True
            )
            return False
        member = interaction.guild.get_member(interaction.user.id)
        if not member:
            return False
        if member.guild_permissions.administrator:
            return True
        from config import settings

        admin_role_id = settings.discord_admin_role_id
        if admin_role_id:
            role = interaction.guild.get_role(int(admin_role_id))
            if role and role in member.roles:
                return True
        await interaction.response.send_message(
            embed=error("You need admin permission."), ephemeral=True
        )
        return False


def get_active_games_for_channel(channel_id: str) -> list[dict]:
    from database import query

    return query(
        "SELECT * FROM events WHERE channel_id = ? AND status = 'in_progress'",
        (channel_id,),
    )


async def setup(bot: commands.Bot) -> None:
    await bot.add_cog(AdminCog(bot))
