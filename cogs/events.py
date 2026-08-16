from __future__ import annotations

import json

import discord
from config import settings
from discord import app_commands
from discord.ext import commands
from embeds import base, error, success
from templates_fmt import cup_announcement, dm_message, end_tournament, role_ping, to_unix_ts

from database import (
    create_game_record,
    execute,
    get_event,
    get_event_players,
    get_event_registrations,
    get_game_players,
    get_game_team_leaderboard,
    get_leaderboard,
    get_team_leaderboard,
    query_one,
    update_player_pr,
)
from ranks import sync_rank_role


class EventsCog(commands.Cog):
    def __init__(self, bot: commands.Bot) -> None:
        self.bot = bot

    @commands.hybrid_command(
        name="create-event",
        description="Create a new scrim/cup event with format, points, and team settings",
    )
    @app_commands.describe(
        name="Event name",
        channel="Channel for announcement",
        signup_channel="Channel for player registrations",
        team_size="Team size (1=solo, 2=duo, 3=trio)",
        total_games="Number of games (0 = unlimited session)",
        max_players="Max players",
        region="Region (EU, NA, ASIA, etc.)",
        event_format="Format (ZoneWars, BoxFights, etc.)",
        start_time="Start time (e.g. 3:00 PM EST)",
        point_kill="Points per elimination",
        point_win="Points for victory",
        placement_scale="Placement points (comma-separated, e.g. 10,8,6,4,2,1)",
        qualification="Enable the qualified-players system (players can qualify, move to other events without re-registering)",
        place_1="1st place prize/label (optional, shown in announcement)",
        place_2="2nd place prize/label (optional, shown in announcement)",
        place_3="3rd place prize/label (optional, shown in announcement)",
        place_4plus="4th place+ prize/label (optional, shown in announcement)",
        pr_multiplier="PR multiplier override (0 = auto based on player count)",
        shoot_timer="Shoot timer in seconds (0 = none), shown in game DMs",
    )
    @app_commands.choices(
        team_size=[
            app_commands.Choice(name="Solo (1)", value=1),
            app_commands.Choice(name="Duo (2)", value=2),
            app_commands.Choice(name="Trio (3)", value=3),
        ]
    )
    async def create_event(
        self,
        ctx: commands.Context,
        name: str,
        channel: discord.TextChannel,
        signup_channel: discord.TextChannel,
        region: str = "EU",
        event_format: str = "ZoneWars",
        start_time: str = "TBD",
        team_size: int = 1,
        total_games: int = 0,
        max_players: int = 100,
        point_kill: int = 1,
        point_win: int = 5,
        placement_scale: str = "10,8,6,4,2,1",
        qualification: bool = False,
        place_1: str = "",
        place_2: str = "",
        place_3: str = "",
        place_4plus: str = "",
        pr_multiplier: float = 0.0,
        shoot_timer: int = 0,
    ) -> None:
        if not await self._check_admin(ctx):
            return
        if ctx.interaction:
            await ctx.interaction.response.defer(ephemeral=True)

        ps_json = json.dumps([int(x.strip()) for x in placement_scale.split(",") if x.strip()])

        event_id = execute(
            "INSERT INTO events "
            "(name, status, channel_id, signup_channel_id, team_size, total_games, "
            "max_players, region, event_format, point_kill, point_win, placement_scale, "
            "qualification_enabled, place_1, place_2, place_3, place_4plus, "
            "pr_multiplier, shoot_timer, scheduled_at) "
            "VALUES (?, 'setup', ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (name, str(channel.id), str(signup_channel.id), team_size, total_games, max_players, region, event_format, point_kill, point_win, ps_json,
             1 if qualification else 0, place_1.strip() or None, place_2.strip() or None, place_3.strip() or None, place_4plus.strip() or None,
             pr_multiplier if pr_multiplier > 0 else None, shoot_timer if shoot_timer > 0 else 0,
             to_unix_ts(start_time) or None),
        )

        await signup_channel.set_permissions(
            ctx.guild.default_role,
            send_messages=False,
            reason="Event created, registration closed",
        )

        team_label = {1: "Solo", 2: "Duo", 3: "Trio"}.get(team_size, "Solo")
        text = cup_announcement(
            name=name,
            format_label=team_label,
            region=region,
            start_time=start_time,
            point_kill=point_kill,
            point_win=point_win,
            ping_role=role_ping(settings.discord_tournament_role_id),
            place_1=place_1.strip() or None,
            place_2=place_2.strip() or None,
            place_3=place_3.strip() or None,
            place_4plus=place_4plus.strip() or None,
        )
        await channel.send(text)

        await ctx.send(
            embed=success(
                f"Event **{name}** created (ID: {event_id}).\n"
                f"Announcement in {channel.mention}\n"
                f"Registrations in {signup_channel.mention}"
                + (f"\nPR multiplier: **{pr_multiplier}x**" if pr_multiplier > 0 else "")
                + (f"\nShoot timer: **{shoot_timer}s**" if shoot_timer > 0 else "")
            ),
        )

    @commands.hybrid_command(
        name="start-event",
        description="Start an event: create a temp channel, assign team roles, set room code",
    )
    @app_commands.describe(event_id="Event ID", room_code="Room code")
    async def start_event(
        self,
        ctx: commands.Context,
        event_id: int,
        room_code: str,
    ) -> None:
        if not await self._check_admin(ctx):
            return
        if ctx.interaction:
            await ctx.interaction.response.defer(ephemeral=True)

        ev = get_event(event_id)
        if not ev:
            await ctx.send(embed=error("Event not found."))
            return

        regs = get_event_registrations(event_id)
        if not regs:
            await ctx.send(embed=error("No registered players."))
            return

        execute(
            "UPDATE events SET room_code = ?, status = 'in_progress' WHERE id = ?",
            (room_code, event_id),
        )

        team_size = ev.get("team_size", 1)
        team_colors = [
            0xE74C3C, 0x3498DB, 0x2ECC71, 0xF1C40F,
            0x9B59B6, 0xE67E22, 0x1ABC9C, 0xE91E63,
        ]

        team_roles = []
        if team_size >= 2:
            teams = []
            current_team = []
            for reg in regs:
                current_team.append(reg)
                if reg.get("team_members"):
                    members = reg["team_members"].split(",")
                    current_team.extend([
                        query_one("SELECT * FROM players WHERE discord_id = ?", (mid,))
                        for mid in members
                    ])
                if len(current_team) >= team_size:
                    teams.append(current_team)
                    current_team = []
            if current_team:
                teams.append(current_team)

            for i, team in enumerate(teams):
                color = team_colors[i % len(team_colors)]
                role = await ctx.guild.create_role(
                    name=f"Team {i + 1}",
                    color=discord.Color(color),
                    reason=f"Event {ev['name']} - Team {i + 1}",
                )
                team_roles.append(role.id)

                for player in team:
                    if player:
                        member = ctx.guild.get_member(int(player["discord_id"]))
                        if member:
                            await member.add_roles(role)

            execute(
                "UPDATE events SET team_roles = ? WHERE id = ?",
                (",".join(str(r) for r in team_roles), event_id),
            )

        category = ctx.guild.get_channel(int(ev["channel_id"] or 0))
        if not category or not isinstance(category, discord.CategoryChannel):
            category = None

        overwrites = {
            ctx.guild.default_role: discord.PermissionOverwrite(read_messages=False),
            ctx.guild.me: discord.PermissionOverwrite(read_messages=True),
        }

        for reg in regs:
            member = ctx.guild.get_member(int(reg["discord_id"]))
            if member:
                overwrites[member] = discord.PermissionOverwrite(read_messages=True)
            if reg.get("team_members"):
                for mid in reg["team_members"].split(","):
                    mid = mid.strip()
                    if mid:
                        tm = ctx.guild.get_member(int(mid))
                        if tm:
                            overwrites[tm] = discord.PermissionOverwrite(read_messages=True)

        temp_channel = await ctx.guild.create_text_channel(
            name=f"event-{ev['name'].lower().replace(' ', '-')}",
            category=category,
            overwrites=overwrites,
            reason=f"Event {ev['name']} - Temporary channel",
        )

        execute(
            "UPDATE events SET dispatch_channel_id = ? WHERE id = ?",
            (str(temp_channel.id), event_id),
        )

        await ctx.send(
            embed=success(
                f"Event **{ev['name']}** started!\n"
                f"Channel: {temp_channel.mention}\n"
                f"Roles created: {len(team_roles)}\n\n"
                f"Use `/start-game` to dispatch room code."
            ),
        )

    @commands.hybrid_command(
        name="start-game",
        description="Start a game: create game record, DM all players with room code",
    )
    @app_commands.describe(
        event_id="Event ID",
        game_number="Game number",
        room_code="Room code to dispatch to players (optional, uses event room code if empty)",
    )
    async def start_game(
        self,
        ctx: commands.Context,
        event_id: int,
        game_number: int,
        room_code: str = "",
    ) -> None:
        if not await self._check_admin(ctx):
            return
        if ctx.interaction:
            await ctx.interaction.response.defer(ephemeral=True)

        ev = get_event(event_id)
        if not ev:
            await ctx.send(embed=error("Event not found."))
            return

        rc = room_code.strip() or ev.get("room_code") or ""

        game = query_one(
            "SELECT * FROM games WHERE event_id = ? AND game_number = ?",
            (event_id, game_number),
        )
        if not game:
            game_id = create_game_record(event_id, game_number, rc)
        else:
            game_id = game["id"]
            execute(
                "UPDATE games SET status = 'in_progress', room_code = ?, "
                "started_at = CURRENT_TIMESTAMP WHERE id = ?",
                (rc, game_id),
            )

        execute(
            "UPDATE events SET status = 'in_progress', current_game = ? WHERE id = ?",
            (game_number, event_id),
        )

        players = get_event_players(event_id)
        for p in players:
            execute(
                "INSERT OR IGNORE INTO game_players (game_id, player_id) VALUES (?, ?)",
                (game_id, p["id"]),
            )

        regs = get_event_registrations(event_id)
        team_label = {1: "Solo", 2: "Duo", 3: "Trio"}.get(ev["team_size"], "Solo")

        sent = 0
        failed = 0
        for reg in regs:
            try:
                member = ctx.guild.get_member(int(reg["discord_id"]))
                if member:
                    dm_text = (
                        f"🎮 **{ev['name']}** — Game {game_number}\n"
                        f"Room Code: **{rc}**\n"
                        f"Format: {team_label} | {ev.get('region', 'EU')}"
                        + (f"\n⏱️ Shoot Timer: **{ev.get('shoot_timer') or 0}s**" if (ev.get('shoot_timer') or 0) > 0 else "")
                    )
                    await member.send(dm_text)
                    sent += 1
                else:
                    failed += 1
            except Exception:
                failed += 1

        channel = ctx.guild.get_channel(
            int(ev["dispatch_channel_id"] or ev["channel_id"] or 0)
        )
        if channel:
            embed = base(f"🏁 Game {game_number} Started", 0xF39C12)
            embed.description = f"**{ev['name']}** — Game {game_number}"
            embed.add_field(name="Room Code", value=rc)
            if (ev.get("shoot_timer") or 0) > 0:
                embed.add_field(name="Shoot Timer", value=f"{ev['shoot_timer']}s")
            if ev.get("scheduled_at"):
                embed.add_field(
                    name="Scheduled",
                    value=f"<t:{int(ev['scheduled_at'])}:F> (<t:{int(ev['scheduled_at'])}:R>)",
                )
            embed.set_footer(text=f"{len(players)} players in game | {sent} DM'd")
            try:
                await channel.send(embed=embed)
            except Exception:
                pass

        await ctx.send(
            embed=success(
                f"Game {game_number} started for **{ev['name']}**. "
                f"{sent} players DM'd with room code."
            ),
        )

    @commands.hybrid_command(
        name="end-game",
        description="End a game: mark completed, set placements, show results & leaderboard",
    )
    @app_commands.describe(
        event_id="Event ID",
        game_number="Game number",
        first="1st place player (optional)",
        second="2nd place player (optional)",
        third="3rd place player (optional)",
        leaderboard_channel="Channel to post results to (optional, defaults to the event dispatch channel)",
        tournament_channel="Tournament channel to also post results (optional)",
    )
    async def end_game(
        self,
        ctx: commands.Context,
        event_id: int,
        game_number: int,
        first: discord.Member | None = None,
        second: discord.Member | None = None,
        third: discord.Member | None = None,
        leaderboard_channel: discord.TextChannel | None = None,
        tournament_channel: discord.TextChannel | None = None,
    ) -> None:
        if not await self._check_admin(ctx):
            return
        if ctx.interaction:
            await ctx.interaction.response.defer(ephemeral=True)

        ev = get_event(event_id)
        if not ev:
            await ctx.send(embed=error("Event not found."))
            return

        game = query_one(
            "SELECT * FROM games WHERE event_id = ? AND game_number = ?",
            (event_id, game_number),
        )
        if not game:
            await ctx.send(embed=error("Game not found."))
            return

        execute(
            "UPDATE games SET status = 'completed', ended_at = CURRENT_TIMESTAMP WHERE id = ?",
            (game["id"],),
        )

        placements = []
        if first:
            placements.append((str(first.id), 1, ev.get("point_win", 50)))
        if second:
            placements.append((str(second.id), 2, 30))
        if third:
            placements.append((str(third.id), 3, 15))

        for did, placement, pts in placements:
            p = query_one("SELECT id FROM players WHERE discord_id = ?", (did,))
            if p:
                execute(
                    "UPDATE game_players SET placement = ?, points = ? "
                    "WHERE game_id = ? AND player_id = ?",
                    (placement, pts, game["id"], p["id"]),
                )

        game_players = get_game_players(game["id"])

        remaining = (ev["total_games"] or 0) > 0 and game_number >= ev["total_games"]

        for p in game_players:
            update_player_pr(p["discord_id"], event_id=event_id)
        await self._sync_ranks(ctx, game_players)

        team_size = ev.get("team_size", 1)
        medals = ["🥇", "🥈", "🥉"]
        lines = [f"📊 **Game {game_number} Results — {ev['name']}**\n"]

        if team_size >= 2:
            board = get_game_team_leaderboard(game["id"], event_id)
            if board:
                for i, row in enumerate(board[:10]):
                    medal = medals[i] if i < 3 else f"{i+1}."
                    dq = " 🚫" if row["is_dq"] else ""
                    lead_id = row.get("lead_id")
                    team_members = row.get("team_members", "")
                    mentions = []
                    if lead_id:
                        member = ctx.guild.get_member(int(lead_id))
                        mentions.append(member.mention if member else f"<@{lead_id}>")
                    if team_members:
                        for mid in team_members.split(","):
                            mid = mid.strip()
                            if mid:
                                member = ctx.guild.get_member(int(mid))
                                mentions.append(member.mention if member else f"<@{mid}>")
                    name = " x ".join(mentions) if mentions else row["team_name"]
                    lines.append(
                        f"{medal} {name} — "
                        f"{row['total_points']} pts, {row['total_kills']} kills{dq}"
                    )
            else:
                lines.append("No team data.")
        else:
            if game_players:
                for i, p in enumerate(game_players[:10]):
                    medal = medals[i] if i < 3 else f"{i+1}."
                    dq = " 🚫" if p["is_disqualified"] else ""
                    name = p.get("username") or p.get("team_name", "Unknown")
                    lines.append(
                        f"{medal} **{name}** — "
                        f"{p['points']} pts, {p['kills']} kills{dq}"
                    )
            else:
                lines.append("No player data.")

        if remaining:
            lines.append("\nEvent completed. Use `/end-event` to see final results.")
        else:
            lines.append(f"\nNext game: `/start-game` (Game {game_number + 1})")

        msg = "\n".join(lines)

        target_channels = []
        default_channel = ctx.guild.get_channel(
            int(ev["dispatch_channel_id"] or ev["channel_id"] or 0)
        )
        if leaderboard_channel:
            target_channels.append(leaderboard_channel)
        elif default_channel:
            target_channels.append(default_channel)
        if tournament_channel and tournament_channel not in target_channels:
            target_channels.append(tournament_channel)

        for ch in target_channels:
            try:
                await ch.send(msg)
            except Exception:
                pass

        await ctx.send(
            embed=success(f"Game {game_number} ended."),
        )

    @commands.hybrid_command(
        name="end-event",
        description="End event: post final results, delete temp channel and team roles",
    )
    @app_commands.describe(event_id="Event ID")
    async def end_event(
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

        execute("UPDATE events SET status = 'completed' WHERE id = ?", (event_id,))

        if ev.get("team_size", 1) >= 2:
            board = get_team_leaderboard(event_id)
        else:
            board = get_leaderboard(event_id)

        for row in board:
            did = row.get("discord_id") or row.get("lead_id")
            if did:
                update_player_pr(did, event_id=event_id)
        await self._sync_ranks(ctx, board)

        channel = ctx.guild.get_channel(
            int(ev["dispatch_channel_id"] or 0)
        )
        if channel:
            team_size = ev.get("team_size", 1)
            winner_mention = ""
            runner_up_mention = ""

            if board:
                winner = board[0]
                if team_size >= 2:
                    winner_mention = self._get_team_mention(ctx.guild, winner)
                else:
                    winner_id = winner.get("discord_id")
                    winner_mention = f"<@{winner_id}>" if winner_id else winner.get("username", "Unknown")

                if len(board) >= 2:
                    runner = board[1]
                    if team_size >= 2:
                        runner_up_mention = self._get_team_mention(ctx.guild, runner)
                    else:
                        runner_id = runner.get("discord_id")
                        runner_up_mention = f"<@{runner_id}>" if runner_id else runner.get("username", "Unknown")

            msg = end_tournament(
                name=ev["name"],
                winner_mention=winner_mention,
                runner_up_mention=runner_up_mention,
                ping_role=role_ping(settings.discord_tournament_role_id),
            )

            try:
                await channel.send(msg)
            except Exception:
                pass

            medals = ["🥇", "🥈", "🥉"]
            lb_lines = [f"📊 **{ev['name']} — Final Leaderboard**\n"]
            if board:
                for i, row in enumerate(board[:10]):
                    medal = medals[i] if i < 3 else f"{i+1}."
                    if team_size >= 2:
                        name = self._get_team_mention(ctx.guild, row)
                    else:
                        name = row.get("username", "Unknown")
                    lb_lines.append(
                        f"{medal} {name} — "
                        f"{row['total_points']} pts ({row['total_kills']} kills)"
                    )
            else:
                lb_lines.append("No scores recorded.")

            lb_msg = "\n".join(lb_lines)
            try:
                await channel.send(lb_msg)
            except Exception:
                pass

        team_roles_str = ev.get("team_roles", "")
        if team_roles_str:
            for role_id in team_roles_str.split(","):
                try:
                    role = ctx.guild.get_role(int(role_id.strip()))
                    if role:
                        await role.delete(reason=f"Event {ev['name']} ended")
                except Exception:
                    pass

        if channel:
            try:
                await channel.delete(reason=f"Event {ev['name']} ended")
            except Exception:
                pass

        await ctx.send(
            embed=success(
                f"Event **{ev['name']}** ended!\n"
                f"Channel and roles cleaned up."
            ),
        )

    @commands.hybrid_command(
        name="dm-players",
        description="DM all registered players with event info, room code, and start time",
    )
    @app_commands.describe(
        event_id="Event ID",
        room_code="Room code to send",
        game_number="Game number",
        start_time="Start time to include in DM",
    )
    async def dm_players(
        self,
        ctx: commands.Context,
        event_id: int,
        room_code: str,
        game_number: int = 1,
        start_time: str = "TBD",
    ) -> None:
        if not await self._check_admin(ctx):
            return

        ev = get_event(event_id)
        if not ev:
            await ctx.send(embed=error("Event not found."))
            return

        regs = get_event_registrations(event_id)
        if not regs:
            await ctx.send(embed=error("No registered players."))
            return

        if ctx.interaction:
            await ctx.interaction.response.defer(ephemeral=True)

        team_label = {1: "Solo", 2: "Duo", 3: "Trio"}.get(ev["team_size"], "Solo")
        msg = dm_message(
            event_name=ev["name"],
            format_label=team_label,
            region=ev.get("region", "EU"),
            start_time=start_time,
            room_code=room_code,
            game_number=game_number,
        )

        sent = 0
        failed = 0
        for reg in regs:
            try:
                member = ctx.guild.get_member(int(reg["discord_id"]))
                if member:
                    await member.send(msg)
                    sent += 1
                else:
                    failed += 1
            except Exception:
                failed += 1

        embed = base("📨 DM Results", 0x3498DB)
        embed.add_field(name="Sent", value=str(sent), inline=True)
        embed.add_field(name="Failed", value=str(failed), inline=True)
        embed.add_field(name="Total", value=str(len(regs)), inline=True)
        await ctx.send(embed=embed)

    @commands.hybrid_command(
        name="create-scrim",
        description="Create a scrim: auto-generates SCRIM-XXXX ID, sets up channels and PR points",
    )
    @app_commands.describe(
        channel="Channel for scrim announcements",
        signup_channel="Channel for player registrations",
        team_size="Team size (1=solo, 2=duo, 3=trio)",
        match_count="Number of matches",
        base_pr_kill="Points per elimination",
        base_pr_win="Points for win",
        region="Region (EU, NA, ASIA, etc.)",
        event_format="Format (ZoneWars, BoxFights, etc.)",
        placement_scale="Placement points (comma-separated, e.g. 10,8,6,4,2,1)",
        pr_multiplier="PR multiplier override (0 = auto based on player count)",
        shoot_timer="Shoot timer in seconds (0 = none), shown in game DMs",
    )
    @app_commands.choices(
        team_size=[
            app_commands.Choice(name="Solo (1)", value=1),
            app_commands.Choice(name="Duo (2)", value=2),
            app_commands.Choice(name="Trio (3)", value=3),
        ]
    )
    async def create_scrim(
        self,
        ctx: commands.Context,
        channel: discord.TextChannel,
        signup_channel: discord.TextChannel,
        region: str = "EU",
        event_format: str = "ZoneWars",
        team_size: int = 1,
        match_count: int = 3,
        base_pr_kill: int = 5,
        base_pr_win: int = 25,
        placement_scale: str = "10,8,6,4,2,1",
        pr_multiplier: float = 0.0,
        shoot_timer: int = 0,
    ) -> None:
        if not await self._check_admin(ctx):
            return
        if ctx.interaction:
            await ctx.interaction.response.defer(ephemeral=True)

        ps_json = json.dumps([int(x.strip()) for x in placement_scale.split(",") if x.strip()])

        import random
        scrim_id = f"SCRIM-{random.randint(1000, 9999)}"
        name = f"Scrim #{scrim_id}"

        event_id = execute(
            "INSERT INTO events "
            "(name, status, channel_id, signup_channel_id, team_size, total_games, "
            "max_players, region, event_format, point_kill, point_win, placement_scale, "
            "pr_multiplier, shoot_timer) "
            "VALUES (?, 'setup', ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (name, str(channel.id), str(signup_channel.id), team_size, match_count, 100, region, event_format, base_pr_kill, base_pr_win, ps_json,
             pr_multiplier if pr_multiplier > 0 else None, shoot_timer if shoot_timer > 0 else 0),
        )

        await signup_channel.set_permissions(
            ctx.guild.default_role,
            send_messages=False,
            reason="Scrim created, registration closed",
        )

        await ctx.send(
            embed=success(f"Scrim **{name}** created (ID: {event_id}).\nScrim code: `{scrim_id}`"),
        )

    @commands.hybrid_command(
        name="start-scrim",
        description="Start a scrim: dispatch room code to channel and DM all registered players",
    )
    @app_commands.describe(
        event_id="Event ID",
        room_code="Room code",
    )
    async def start_scrim(
        self,
        ctx: commands.Context,
        event_id: int,
        room_code: str,
    ) -> None:
        if not await self._check_admin(ctx):
            return
        if ctx.interaction:
            await ctx.interaction.response.defer(ephemeral=True)

        ev = get_event(event_id)
        if not ev:
            await ctx.send(embed=error("Event not found."))
            return

        execute(
            "UPDATE events SET room_code = ?, status = 'in_progress' WHERE id = ?",
            (room_code, event_id),
        )

        team_label = {1: "Solo", 2: "Duo", 3: "Trio"}.get(ev["team_size"], "Solo")
        msg = (
            f"**{team_label} Scrim**\n"
            f"Format: {ev.get('event_format', 'ZoneWars')}\n"
            f"Region: {ev.get('region', 'EU')}\n"
            f"Code: **{room_code}**\n"
            f"{role_ping(settings.discord_scrim_role_id)}"
        )

        channel = ctx.guild.get_channel(
            int(ev["dispatch_channel_id"] or ev["channel_id"] or 0)
        )
        if channel:
            try:
                await channel.send(msg)
            except Exception:
                pass

        regs = get_event_registrations(event_id)
        sent = 0
        failed = 0
        for reg in regs:
            try:
                member = ctx.guild.get_member(int(reg["discord_id"]))
                if member:
                    await member.send(
                        f"🎮 **{ev['name']}** started!\n"
                        f"Room Code: `{room_code}`\n"
                        f"Format: {team_label} | {ev.get('region', 'EU')}"
                    )
                    sent += 1
                else:
                    failed += 1
            except Exception:
                failed += 1

        await ctx.send(
            f"Scrim **{ev['name']}** started.\n"
            f"Code: `{room_code}`\n"
            f"{sent} players DM'd.",
        )

    @commands.hybrid_command(
        name="end-scrim",
        description="End a scrim: mark completed, post final leaderboard, calculate PR",
    )
    @app_commands.describe(event_id="Event ID")
    async def end_scrim(
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

        execute("UPDATE events SET status = 'completed' WHERE id = ?", (event_id,))

        if ev.get("team_size", 1) >= 2:
            board = get_team_leaderboard(event_id)
        else:
            board = get_leaderboard(event_id)

        for row in board:
            did = row.get("discord_id") or row.get("lead_id")
            if did:
                update_player_pr(did, event_id=event_id)
        await self._sync_ranks(ctx, board)

        channel = ctx.guild.get_channel(
            int(ev["dispatch_channel_id"] or ev["channel_id"] or 0)
        )
        if channel:
            medals = ["🥇", "🥈", "🥉"]
            lb_lines = [f"📊 **{ev['name']} — Final Leaderboard**\n"]
            if board:
                for i, row in enumerate(board[:10]):
                    medal = medals[i] if i < 3 else f"{i+1}."
                    name = row.get("username") or row.get("team_name", "Unknown")
                    lb_lines.append(
                        f"{medal} **{name}** — "
                        f"{row['total_points']} pts ({row['total_kills']} kills)"
                    )
            else:
                lb_lines.append("No scores recorded.")

            lb_msg = "\n".join(lb_lines)
            try:
                await channel.send(lb_msg)
            except Exception:
                pass

            end_msg = (
                f"🏆 **{ev['name']}** has ended!\n\n"
                f"GGs to everyone who competed!\n"
                f"Keep grinding, keep improving!"
            )
            try:
                await channel.send(end_msg)
            except Exception:
                pass

        await ctx.send(
            f"Scrim **{ev['name']}** ended. Final leaderboard posted.",
        )

    @commands.hybrid_command(
        name="create-session",
        description="Create a pending session for an event (sessions hold multiple matches)",
    )
    @app_commands.describe(event_id="Event ID")
    async def create_session_cmd(
        self, ctx: commands.Context, event_id: int
    ) -> None:
        if not await self._check_admin(ctx):
            return
        if ctx.interaction:
            await ctx.interaction.response.defer(ephemeral=True)

        from database import create_session

        ev = get_event(event_id)
        if not ev:
            await ctx.send(embed=error("Event not found."))
            return

        session = create_session(event_id)
        await ctx.send(
            embed=success(
                f"Session **#{session['session_number']}** created for **{ev['name']}**. "
                "Ready to start with `;start-session`."
            )
        )

    @commands.hybrid_command(
        name="start-session",
        description="Start a session: create its first match, dispatch code, DM players",
    )
    @app_commands.describe(
        event_id="Event ID",
        room_code="Room code to dispatch (optional, uses event room code if empty)",
    )
    async def start_session_cmd(
        self,
        ctx: commands.Context,
        event_id: int,
        room_code: str = "",
    ) -> None:
        if not await self._check_admin(ctx):
            return
        if ctx.interaction:
            await ctx.interaction.response.defer(ephemeral=True)

        ev = get_event(event_id)
        if not ev:
            await ctx.send(embed=error("Event not found."))
            return

        execute(
            "INSERT INTO command_queue (command, params) VALUES ('start_session', ?)",
            (
                json.dumps(
                    {"event_id": event_id, "room_code": room_code.strip()}
                ),
            ),
        )
        await ctx.send(
            embed=success(f"Session start queued for **{ev['name']}**.")
        )

    @commands.hybrid_command(
        name="end-session",
        description="End a session: complete its last match, post session leaderboard",
    )
    @app_commands.describe(event_id="Event ID")
    async def end_session_cmd(self, ctx: commands.Context, event_id: int) -> None:
        if not await self._check_admin(ctx):
            return
        if ctx.interaction:
            await ctx.interaction.response.defer(ephemeral=True)

        ev = get_event(event_id)
        if not ev:
            await ctx.send(embed=error("Event not found."))
            return

        execute(
            "INSERT INTO command_queue (command, params) VALUES ('end_session', ?)",
            (json.dumps({"event_id": event_id}),),
        )
        await ctx.send(
            embed=success(f"Session end queued for **{ev['name']}**.")
        )

    def _get_team_mention(self, guild: discord.Guild, row: dict) -> str:
        lead_id = row.get("lead_id") or row.get("discord_id")
        team_members = row.get("team_members", "")

        mentions = []
        if lead_id:
            member = guild.get_member(int(lead_id))
            if member:
                mentions.append(member.mention)
            else:
                mentions.append(f"<@{lead_id}>")

        if team_members:
            for mid in team_members.split(","):
                mid = mid.strip()
                if mid:
                    member = guild.get_member(int(mid))
                    if member:
                        mentions.append(member.mention)
                    else:
                        mentions.append(f"<@{mid}>")

        return " x ".join(mentions) if mentions else row.get("team_name", "Unknown")

    async def _sync_ranks(self, ctx: commands.Context, board: list[dict]) -> None:
        for row in board:
            did = row.get("discord_id") or row.get("lead_id")
            if not did:
                continue
            member = ctx.guild.get_member(int(did))
            if not member:
                continue
            try:
                p = query_one("SELECT pr FROM players WHERE discord_id = ?", (did,))
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
    await bot.add_cog(EventsCog(bot))
