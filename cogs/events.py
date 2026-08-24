from __future__ import annotations

import json

import discord
from config import settings
from discord import app_commands
from discord.ext import commands
from embeds import base, error, success
from templates_fmt import (
    cup_announcement,
    dm_message,
    dynamic_time,
    end_tournament,
    role_ping,
    team_size_label,
    to_unix_ts,
)

from database import (
    award_coins_for_placements,
    create_event_record,
    create_game_record,
    event_awards_pr,
    execute,
    get_divisions,
    get_event,
    get_event_players,
    get_event_registrations,
    get_game_players,
    get_game_team_leaderboard,
    get_leaderboard,
    get_team_leaderboard,
    query_one,
    set_event_qualifier_requirements,
    update_player_pr,
)
from ranks import sync_crown_role, sync_rank_role


class EventsCog(commands.Cog):
    def __init__(self, bot: commands.Bot) -> None:
        self.bot = bot

    @commands.hybrid_command(
        name="create-event",
        description="Create a cup/scrim/bracket/qualifier event with format, entry rules, and scoring",
    )
    @app_commands.describe(
        name="Event name",
        channel="Channel for announcement",
        signup_channel="Channel for player registrations",
        event_type="Event type",
        entry_mode="Who can enter (open / PR cap / division)",
        pr_cap="Max PR allowed when entry_mode=pr_limited",
        division="Division name required when entry_mode=division",
        scoring_mode="Scoring system (coins cups pay out placement scale as coins)",
        awards_pr="Whether this event awards PR (coins cups never do)",
        qualifier_top="Qualifier: how many top finishers qualify",
        qualifier_target="Qualifier: event id that qualified players may join",
        qualifier_division="Qualifier: division auto-granted to qualified players",
        team_size="Team size (1=solo, 2=duo, 3=trio, 4=squad)",
        total_games="Number of games (0 = unlimited session)",
        max_players="Max players",
        event_format="Format (ZoneWars, BoxFights, BattleRoyale, etc.)",
        point_kill="Points per elimination",
        point_win="Points for victory",
        placement_scale="Placement points (comma-separated, e.g. 10,8,6,4,2,1)",
        qualification="Enable the qualified-players system (players can qualify, move to other events without re-registering)",
        pr_multiplier="PR multiplier override (0 = auto based on player count)",
        shoot_timer="Shoot timer as string (e.g. '02:30', '5m', '0' = none), shown in game DMs",
        dispatch="Post a dispatch message (with room code) immediately after creation",
        room_code="Room code used when dispatch is enabled",
        start_time="Start time (e.g. '2026-08-24 18:00 UTC' or unix '1787584200') — shown as <t:...:R>",
    )
    @app_commands.rename(event_type="type")
    @app_commands.choices(
        event_type=[
            app_commands.Choice(name="Cup", value="cup"),
            app_commands.Choice(name="Scrim", value="scrim"),
            app_commands.Choice(name="Bracket", value="bracket"),
            app_commands.Choice(name="Qualifier", value="qualifier"),
        ],
        entry_mode=[
            app_commands.Choice(name="Open", value="open"),
            app_commands.Choice(name="PR limited", value="pr_limited"),
            app_commands.Choice(name="Division only", value="division"),
        ],
        scoring_mode=[
            app_commands.Choice(name="Normal", value="normal"),
            app_commands.Choice(name="Placement only", value="placement_only"),
            app_commands.Choice(name="Coins", value="coins"),
        ],
        team_size=[
            app_commands.Choice(name="Solo (1)", value=1),
            app_commands.Choice(name="Duo (2)", value=2),
            app_commands.Choice(name="Trio (3)", value=3),
            app_commands.Choice(name="Squad (4)", value=4),
        ],
        event_format=[
            app_commands.Choice(name="Zone Wars", value="ZoneWars"),
            app_commands.Choice(name="Box Fights", value="BoxFights"),
            app_commands.Choice(name="Battle Royale", value="BattleRoyale"),
            app_commands.Choice(name="Realistic", value="Realistic"),
        ],
    )
    async def create_event(
        self,
        ctx: commands.Context,
        name: str,
        channel: discord.TextChannel,
        signup_channel: discord.TextChannel,
        event_type: str = "cup",
        entry_mode: str = "open",
        pr_cap: int = 0,
        division: str = "",
        scoring_mode: str = "normal",
        awards_pr: bool = True,
        qualifier_top: int = 0,
        qualifier_target: int = 0,
        qualifier_division: str = "",
        event_format: str = "ZoneWars",
        team_size: int = 1,
        total_games: int = 0,
        max_players: int = 100,
        point_kill: int = 1,
        point_win: int = 5,
        placement_scale: str = "10,8,6,4,2,1",
        qualification: bool = False,
        pr_multiplier: float = 0.0,
        shoot_timer: str = "0",
        dispatch: bool = False,
        room_code: str = "",
        start_time: str = "TBD",
    ) -> None:
        if not await self._check_admin(ctx):
            return
        if ctx.interaction:
            await ctx.interaction.response.defer(ephemeral=True)

        event_type = (event_type or "cup").strip().lower()
        if event_type not in ("cup", "scrim", "bracket", "qualifier"):
            await ctx.send(embed=error("Invalid event type."))
            return
        entry_mode = (entry_mode or "open").strip().lower()
        if entry_mode not in ("open", "pr_limited", "division"):
            await ctx.send(embed=error("Invalid entry mode."))
            return
        scoring_mode = (scoring_mode or "normal").strip().lower()
        if scoring_mode not in ("normal", "placement_only", "coins"):
            await ctx.send(embed=error("Invalid scoring mode."))
            return
        if event_type == "bracket" and team_size != 1:
            await ctx.send(embed=error("Brackets are 1v1 — team_size must be Solo (1)."))
            return

        required_division_id = None
        if entry_mode == "division":
            if not division.strip():
                await ctx.send(
                    embed=error("Pick a division name for division-gated entry.")
                )
                return
            match = next(
                (
                    d
                    for d in get_divisions()
                    if d["name"].lower() == division.strip().lower()
                ),
                None,
            )
            if not match:
                await ctx.send(
                    embed=error(
                        f"Division **{division.strip()}** not found. "
                        "Create it first with `/create-division`."
                    )
                )
                return
            required_division_id = match["id"]

        qualifier_division_id = None
        if qualifier_division.strip():
            match = next(
                (
                    d
                    for d in get_divisions()
                    if d["name"].lower() == qualifier_division.strip().lower()
                ),
                None,
            )
            if not match:
                await ctx.send(
                    embed=error(
                        f"Division **{qualifier_division.strip()}** not found. "
                        "Create it first with `/create-division`."
                    )
                )
                return
            qualifier_division_id = match["id"]

        if event_type == "qualifier":
            if qualifier_top <= 0:
                await ctx.send(
                    embed=error("Qualifiers need `qualifier_top` (how many qualify).")
                )
                return
            if qualifier_target <= 0 and qualifier_division_id is None:
                await ctx.send(
                    embed=error(
                        "Qualifiers need a `qualifier_target` event id or a "
                        "`qualifier_division` to grant."
                    )
                )
                return

        coins_cup = scoring_mode == "coins"
        ps_json = json.dumps(
            [int(x.strip()) for x in placement_scale.split(",") if x.strip()]
        )

        # start_time → scheduled_at (unix) + discord relative tag
        raw_start = (start_time or "TBD").strip() or "TBD"
        scheduled_at_val = None
        if raw_start.lower() != "tbd":
            scheduled_at_val = to_unix_ts(raw_start)
            # if parsing failed but raw looks like unix, keep raw for display
            # cup_announcement will handle fallback to raw string
        shoot_timer_str = (shoot_timer or "0").strip() or "0"

        event_id = create_event_record(
            name=name,
            status="setup",
            channel_id=str(channel.id),
            signup_channel_id=str(signup_channel.id),
            team_size=team_size,
            total_games=total_games,
            max_players=max_players,
            region="EU",
            event_format=event_format,
            point_kill=point_kill,
            point_win=point_win,
            placement_scale=ps_json,
            qualification_enabled=1 if qualification else 0,
            pr_multiplier=pr_multiplier if pr_multiplier > 0 else None,
            shoot_timer=shoot_timer_str,
            scheduled_at=scheduled_at_val,
            event_type=event_type,
            entry_mode=entry_mode,
            pr_cap=pr_cap if pr_cap > 0 else None,
            required_division_id=required_division_id,
            scoring_mode=scoring_mode,
            awards_pr=1 if (awards_pr and not coins_cup) else 0,
            coins_enabled=1 if coins_cup else 0,
        )

        if event_type == "qualifier":
            set_event_qualifier_requirements(
                event_id,
                {
                    "top": qualifier_top,
                    "target_event_id": qualifier_target if qualifier_target > 0 else None,
                    "target_division_id": qualifier_division_id,
                },
            )

        if room_code.strip():
            execute(
                "UPDATE vtx_events SET room_code = %s WHERE id = %s",
                (room_code.strip(), event_id),
            )

        await signup_channel.set_permissions(
            ctx.guild.default_role,
            send_messages=False,
            reason="Event created, registration closed",
        )

        if event_type != "scrim":
            team_label = team_size_label(team_size)
            # Use provided start_time so announcement shows <t:unix:R> relative tag
            announce_start = raw_start if raw_start.lower() != "tbd" else "TBD"
            text = cup_announcement(
                name=name,
                format_label=team_label,
                region="EU",
                start_time=announce_start,
                point_kill=point_kill,
                point_win=point_win,
                ping_role=role_ping(settings.discord_tournament_role_id),
            )
            await channel.send(text)

        entry_note = {
            "open": "Open entry",
            "pr_limited": f"PR cap: {pr_cap}",
            "division": f"Division: {division.strip()}",
        }[entry_mode]
        scoring_note = {
            "normal": "Normal scoring",
            "placement_only": "Placement-only scoring",
            "coins": "Coins cup (placement scale pays out coins)",
        }[scoring_mode]
        qualifier_note = ""
        if event_type == "qualifier":
            qualifier_note = (
                f"\nQualifier: top **{qualifier_top}** qualify"
                + (f" → Event {qualifier_target}" if qualifier_target > 0 else "")
                + (
                    f" → division **{qualifier_division.strip()}**"
                    if qualifier_division_id
                    else ""
                )
            )

        # Build relative timestamp for the success message if start_time was given
        start_display = raw_start
        if scheduled_at_val:
            start_display = f"{raw_start} (<t:{scheduled_at_val}:R>)"
        shoot_display = shoot_timer_str if shoot_timer_str and shoot_timer_str != "0" else ""
        await ctx.send(
            embed=success(
                f"Event **{name}** created (ID: {event_id}).\n"
                f"Type: **{event_type.title()}** | {entry_note} | {scoring_note}"
                + ("" if awards_pr or coins_cup else "\nNo PR awarded")
                + qualifier_note
                + f"\nAnnouncement in {channel.mention}\n"
                f"Registrations in {signup_channel.mention}"
                + (f"\nStart: **{start_display}**" if raw_start.lower() != "tbd" else "")
                + (f"\nPR multiplier: **{pr_multiplier}x**" if pr_multiplier > 0 else "")
                + (f"\nShoot timer: **{shoot_display}**" if shoot_display else "")
                + (
                    f"\nDispatch posted in {channel.mention}"
                    if dispatch
                    else ""
                )
            ),
        )

        if dispatch:
            scale_str = ", ".join(
                x.strip() for x in placement_scale.split(",") if x.strip()
            )
            dispatch_msg = (
                f"**{team_size_label(team_size)} Scrim**\n"
                f"Format: {event_format}\n"
                f"Placements: **{scale_str}**\n"
                f"Region: EU\n"
                f"Code: **{room_code.strip() or 'TBD'}**\n"
                + (
                    role_ping(settings.discord_scrim_role_id)
                    if event_type == "scrim"
                    else role_ping(settings.discord_tournament_role_id)
                )
            )
            try:
                await channel.send(dispatch_msg)
            except Exception:
                pass

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
            "UPDATE vtx_events SET room_code = %s, status = 'in_progress' WHERE id = %s",
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
                        query_one("SELECT * FROM vtx_players WHERE discord_id = %s", (mid,))
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
                    hoist=True,
                    reason=f"Event {ev['name']} - Team {i + 1}",
                )
                team_roles.append(role.id)

                for player in team:
                    if player:
                        member = ctx.guild.get_member(int(player["discord_id"]))
                        if member:
                            await member.add_roles(role)

            execute(
                "UPDATE vtx_events SET team_roles = %s WHERE id = %s",
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
            "UPDATE vtx_events SET dispatch_channel_id = %s WHERE id = %s",
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
            "SELECT * FROM vtx_games WHERE event_id = %s AND game_number = %s",
            (event_id, game_number),
        )
        if not game:
            game_id = create_game_record(event_id, game_number, rc)
        else:
            game_id = game["id"]
            execute(
                "UPDATE vtx_games SET status = 'in_progress', room_code = %s, "
                "started_at = CURRENT_TIMESTAMP WHERE id = %s",
                (rc, game_id),
            )

        execute(
            "UPDATE vtx_events SET status = 'in_progress', current_game = %s WHERE id = %s",
            (game_number, event_id),
        )

        players = get_event_players(event_id)
        for p in players:
            execute(
                "INSERT INTO vtx_game_players (game_id, player_id) VALUES (%s, %s) ON CONFLICT DO NOTHING",
                (game_id, p["id"]),
            )

        regs = get_event_registrations(event_id)
        team_label = team_size_label(ev["team_size"])

        sent = 0
        failed = 0
        for reg in regs:
            try:
                member = ctx.guild.get_member(int(reg["discord_id"]))
                if member:
                    _st = str(ev.get('shoot_timer') or "").strip()
                    _st_display = _st if _st and _st != "0" else ""
                    dm_text = (
                        f"🎮 **{ev['name']}** — Game {game_number}\n"
                        f"Room Code: **{rc}**\n"
                        f"Format: {team_label} | {ev.get('region', 'EU')}"
                        + (f"\n⏱️ Shoot Timer: **{_st_display}**" if _st_display else "")
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
            _st2 = str(ev.get("shoot_timer") or "").strip()
            if _st2 and _st2 != "0":
                embed.add_field(name="Shoot Timer", value=_st2)
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
            "SELECT * FROM vtx_games WHERE event_id = %s AND game_number = %s",
            (event_id, game_number),
        )
        if not game:
            await ctx.send(embed=error("Game not found."))
            return

        execute(
            "UPDATE vtx_games SET status = 'completed', ended_at = CURRENT_TIMESTAMP WHERE id = %s",
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
            p = query_one("SELECT id FROM vtx_players WHERE discord_id = %s", (did,))
            if p:
                execute(
                    "UPDATE vtx_game_players SET placement = %s, points = %s "
                    "WHERE game_id = %s AND player_id = %s",
                    (placement, pts, game["id"], p["id"]),
                )

        award_coins_for_placements(game["id"], event_id)

        game_players = get_game_players(game["id"])

        remaining = (ev["total_games"] or 0) > 0 and game_number >= ev["total_games"]

        if event_awards_pr(ev):
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
                    if p.get("discord_id"):
                        name = f"<@{p['discord_id']}>"
                    else:
                        name = p.get("username") or p.get("team_name", "Unknown")
                    lines.append(
                        f"{medal} {name} — "
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

        execute("UPDATE vtx_events SET status = 'completed' WHERE id = %s", (event_id,))

        if ev.get("team_size", 1) >= 2:
            board = get_team_leaderboard(event_id)
        else:
            board = get_leaderboard(event_id)

        if event_awards_pr(ev):
            for row in board:
                did = row.get("discord_id") or row.get("lead_id")
                if did:
                    update_player_pr(did, event_id=event_id)
            await self._sync_ranks(ctx, board)

        if board:
            winner_did = board[0].get("discord_id") or board[0].get("lead_id")
            await sync_crown_role(ctx.guild, winner_did)

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
                    elif row.get("discord_id"):
                        name = f"<@{row['discord_id']}>"
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

        team_label = team_size_label(ev["team_size"])
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
            "UPDATE vtx_events SET room_code = %s, status = 'in_progress' WHERE id = %s",
            (room_code, event_id),
        )

        team_label = team_size_label(ev["team_size"])
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

        execute("UPDATE vtx_events SET status = 'completed' WHERE id = %s", (event_id,))

        if ev.get("team_size", 1) >= 2:
            board = get_team_leaderboard(event_id)
        else:
            board = get_leaderboard(event_id)

        if event_awards_pr(ev):
            for row in board:
                did = row.get("discord_id") or row.get("lead_id")
                if did:
                    update_player_pr(did, event_id=event_id)
            await self._sync_ranks(ctx, board)

        from database import grant_event_coin_rewards

        import asyncio

        await asyncio.to_thread(grant_event_coin_rewards, event_id)

        channel = ctx.guild.get_channel(
            int(ev["dispatch_channel_id"] or ev["channel_id"] or 0)
        )
        if channel:
            medals = ["🥇", "🥈", "🥉"]
            lb_lines = [f"📊 **{ev['name']} — Final Leaderboard**\n"]
            if board:
                for i, row in enumerate(board[:10]):
                    medal = medals[i] if i < 3 else f"{i+1}."
                    if row.get("discord_id"):
                        name = f"<@{row['discord_id']}>"
                    else:
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
    @app_commands.describe(event_id="Event ID", lobby_id="Lobby ID (optional, for lobby-scoped sessions)")
    async def create_session_cmd(
        self, ctx: commands.Context, event_id: int, lobby_id: int | None = None
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

        if lobby_id is not None:
            from database import get_lobby

            lobby = get_lobby(lobby_id)
            if not lobby:
                await ctx.send(embed=error("Lobby not found."))
                return

        session = create_session(event_id, lobby_id)
        scope = f" for lobby **{lobby['name']}**" if lobby_id is not None else ""
        await ctx.send(
            embed=success(
                f"Session **#{session['session_number']}** created{scope} for **{ev['name']}**. "
                "Ready to start with `;start-session`."
            )
        )

    @commands.hybrid_command(
        name="start-session",
        description="Start a session: create its first match, dispatch code, DM players",
    )
    @app_commands.describe(
        event_id="Event ID",
        lobby_id="Lobby ID (optional, for lobby-scoped sessions)",
        room_code="Room code to dispatch (optional, uses event room code if empty)",
    )
    async def start_session_cmd(
        self,
        ctx: commands.Context,
        event_id: int,
        lobby_id: int | None = None,
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
            "INSERT INTO vtx_command_queue (command, params) VALUES ('start_session', %s)",
            (
                json.dumps(
                    {
                        "event_id": event_id,
                        "lobby_id": lobby_id,
                        "room_code": room_code.strip(),
                    }
                ),
            ),
        )
        scope = f" (lobby {lobby_id})" if lobby_id is not None else ""
        await ctx.send(
            embed=success(f"Session start queued for **{ev['name']}**{scope}.")
        )

    @commands.hybrid_command(
        name="end-session",
        description="End a session: complete its last match, post session leaderboard",
    )
    @app_commands.describe(event_id="Event ID", lobby_id="Lobby ID (optional, for lobby-scoped sessions)")
    async def end_session_cmd(self, ctx: commands.Context, event_id: int, lobby_id: int | None = None) -> None:
        if not await self._check_admin(ctx):
            return
        if ctx.interaction:
            await ctx.interaction.response.defer(ephemeral=True)

        ev = get_event(event_id)
        if not ev:
            await ctx.send(embed=error("Event not found."))
            return

        execute(
            "INSERT INTO vtx_command_queue (command, params) VALUES ('end_session', %s)",
            (json.dumps({"event_id": event_id, "lobby_id": lobby_id}),),
        )
        scope = f" (lobby {lobby_id})" if lobby_id is not None else ""
        await ctx.send(
            embed=success(f"Session end queued for **{ev['name']}**{scope}.")
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
    await bot.add_cog(EventsCog(bot))