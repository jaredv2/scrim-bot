from __future__ import annotations

import json
import logging

import discord
from config import settings
from discord.ext import commands, tasks
from templates_fmt import (
    cup_announcement,
    dm_message,
    end_tournament,
    role_ping,
    signup_announcement,
    team_size_label,
)

logger = logging.getLogger("scrim-bot")

from database import (
    complete_command,
    execute,
    fail_command,
    get_event,
    get_event_registrations,
    get_session,
    log_bot_action,
    pop_command,
    query_one,
    reload_db_if_needed,
)


class CommandQueueCog(commands.Cog):
    def __init__(self, bot: commands.Bot) -> None:
        self.bot = bot
        self.process_queue.start()

    def cog_unload(self) -> None:
        self.process_queue.cancel()

    @tasks.loop(seconds=3)
    async def process_queue(self) -> None:
        if reload_db_if_needed():
            logger.info("Database restored by dashboard — connections reloaded")
        cmd = pop_command()
        if not cmd:
            return

        try:
            params = json.loads(cmd["params"])
            await self._execute(cmd["command"], params)
            complete_command(cmd["id"])
        except Exception as e:
            fail_command(cmd["id"], str(e))

    async def _execute(self, command: str, params: dict) -> None:
        guild = self.bot.guilds[0] if self.bot.guilds else None
        if not guild:
            return

        if command == "open_registration":
            await self._open_registration(guild, params)
        elif command == "close_registration":
            await self._close_registration(guild, params)
        elif command == "reopen_registration":
            await self._reopen_registration(guild, params)
        elif command == "dispatch":
            await self._dispatch(guild, params)
        elif command == "announce":
            await self._announce(guild, params)
        elif command == "announce_signups":
            await self._announce_signups(guild, params)
        elif command == "announce_end":
            await self._announce_end(guild, params)
        elif command == "dm_players":
            await self._dm_players(guild, params)
        elif command == "start_game":
            await self._start_game(guild, params)
        elif command == "end_game":
            await self._end_game(guild, params)
        elif command == "start_live_feed":
            await self._start_live_feed(guild, params)
        elif command == "log_kill":
            await self._log_kill(guild, params)
        elif command == "end_match":
            await self._end_match(guild, params)
        elif command == "start_session":
            await self._start_session(guild, params)
        elif command == "end_session":
            await self._end_session(guild, params)
        elif command == "end_event":
            await self._end_event(guild, params)
        elif command == "dq_notify":
            await self._dq_notify(guild, params)

    async def _open_registration(self, guild: discord.Guild, params: dict) -> None:
        ev = get_event(params["event_id"])
        if not ev:
            return
        channel = guild.get_channel(int(ev["signup_channel_id"] or ev["channel_id"] or 0))
        if channel and isinstance(channel, discord.TextChannel):
            await channel.set_permissions(
                guild.default_role, send_messages=True, reason="Dashboard: open registration"
            )
            execute("UPDATE vtx_events SET status = 'registration' WHERE id = %s", (ev["id"],))
            log_bot_action(ev["id"], "open_registration", f"Opened in {channel.name}")

    async def _close_registration(self, guild: discord.Guild, params: dict) -> None:
        ev = get_event(params["event_id"])
        if not ev:
            return
        channel = guild.get_channel(int(ev["signup_channel_id"] or ev["channel_id"] or 0))
        if channel and isinstance(channel, discord.TextChannel):
            await channel.set_permissions(
                guild.default_role, send_messages=False, reason="Dashboard: close registration"
            )
            execute(
                "UPDATE vtx_events SET status = 'setup' WHERE id = %s AND status = 'registration'",
                (ev["id"],),
            )
            log_bot_action(ev["id"], "close_registration", f"Closed in {channel.name}")

    async def _reopen_registration(self, guild: discord.Guild, params: dict) -> None:
        ev = get_event(params["event_id"])
        if not ev or ev["status"] == "completed":
            return
        channel = guild.get_channel(int(ev["signup_channel_id"] or ev["channel_id"] or 0))
        if channel and isinstance(channel, discord.TextChannel):
            await channel.set_permissions(
                guild.default_role, send_messages=True, reason="Dashboard: reopen registration"
            )
            execute("UPDATE vtx_events SET status = 'registration' WHERE id = %s", (ev["id"],))
            log_bot_action(ev["id"], "reopen_registration", f"Reopened in {channel.name}")

    async def _dispatch(self, guild: discord.Guild, params: dict) -> None:
        ev = get_event(params["event_id"])
        if not ev:
            return
        channel = guild.get_channel(
            int(ev["dispatch_channel_id"] or ev["channel_id"] or 0)
        )
        if not channel:
            return

        code = params.get("code", "")
        game_number = params.get("game_number")
        session_number = params.get("session_number")
        team_label = team_size_label(ev["team_size"])
        if session_number:
            game_label = f" — Session {session_number} · Match {game_number}" if game_number else f" — Session {session_number}"
        else:
            game_label = f" — Game {game_number}" if game_number else ""
        placement = self._placement_scale_text(ev)
        lines = [
            f"Scrim {team_label}{game_label}",
            f"Format : {ev.get('event_format', 'ZoneWars')}",
            f"Region : {ev.get('region', 'EU')}",
            f"Placement : {placement}" if placement else None,
            f"Code : {code}",
            role_ping(settings.discord_scrim_role_id),
        ]
        await channel.send("\n".join(line for line in lines if line))
        log_bot_action(ev["id"], "dispatch", f"Code: {code}, Game: {game_number}, Session: {session_number}")

        if params.get("dm_players"):
            await self._dm_players(guild, params)

    def _placement_scale_text(self, ev: dict) -> str:
        import json as _json

        try:
            scale = _json.loads(ev.get("placement_scale") or "[]")
            return ", ".join(str(int(x)) for x in scale) if scale else ""
        except (ValueError, TypeError):
            return ""

    async def _announce(self, guild: discord.Guild, params: dict) -> None:
        ev = get_event(params["event_id"])
        if not ev:
            return
        channel = guild.get_channel(
            int(ev["updates_channel_id"] or ev["channel_id"] or 0)
        )
        if not channel:
            return

        team_label = team_size_label(ev["team_size"])
        text = cup_announcement(
            name=ev["name"],
            format_label=team_label,
            region=ev.get("region", "EU"),
            start_time=params.get("start_time", "TBD"),
            point_kill=params.get("point_kill", 1),
            point_win=params.get("point_win", 5),
            ping_role=role_ping(settings.discord_tournament_role_id),
        )
        await channel.send(text)
        log_bot_action(ev["id"], "announce", "Cup announcement posted")

    async def _announce_signups(self, guild: discord.Guild, params: dict) -> None:
        ev = get_event(params["event_id"])
        if not ev:
            return
        channel = guild.get_channel(
            int(ev["updates_channel_id"] or ev["channel_id"] or 0)
        )
        if not channel:
            return

        team_label = team_size_label(ev["team_size"])
        text = signup_announcement(
            name=ev["name"],
            format_label=team_label,
            region=ev.get("region", "EU"),
            start_time=params.get("start_time", "TBD"),
            signup_channel=params.get("signup_channel", "sign-up"),
            ping_role=role_ping(settings.discord_tournament_role_id),
        )
        await channel.send(text)
        log_bot_action(ev["id"], "announce_signups", "Signup announcement posted")

    async def _announce_end(self, guild: discord.Guild, params: dict) -> None:
        ev = get_event(params["event_id"])
        if not ev:
            return
        channel = guild.get_channel(
            int(ev["updates_channel_id"] or ev["channel_id"] or 0)
        )
        if not channel:
            return

        from database import get_leaderboard

        board = get_leaderboard(ev["id"])
        winner_mention = params.get("winner", "")
        runner_up_mention = params.get("runner_up", "")
        winner_stats = ""
        runner_up_stats = ""
        if board and len(board) > 0:
            pts = board[0]["total_points"]
            kills = board[0]["total_kills"]
            winner_stats = f"with {pts} points and {kills} kills!"
        if board and len(board) > 1:
            pts = board[1]["total_points"]
            kills = board[1]["total_kills"]
            runner_up_stats = f"with {pts} points and {kills} kills!"

        text = end_tournament(
            name=ev["name"],
            winner_mention=winner_mention or "TBD",
            winner_stats=winner_stats,
            runner_up_mention=runner_up_mention,
            runner_up_stats=runner_up_stats,
            next_event=params.get("next_event", ""),
            ping_role=role_ping(settings.discord_tournament_role_id),
        )
        await channel.send(text)

        from database import execute
        execute("UPDATE vtx_events SET status = 'completed' WHERE id = %s", (ev["id"],))

    async def _dm_players(self, guild: discord.Guild, params: dict) -> None:
        ev = get_event(params["event_id"])
        if not ev:
            return

        regs = get_event_registrations(ev["id"])
        if not regs:
            return

        code = params.get("code", "")
        team_label = team_size_label(ev["team_size"])
        msg = dm_message(
            event_name=ev["name"],
            format_label=team_label,
            region=ev.get("region", "EU"),
            start_time=params.get("start_time", "TBD"),
            room_code=code,
            game_number=params.get("game_number", 1),
            placement_scale=params.get("placement_scale")
            or self._placement_scale_text(ev),
        )

        all_ids = set()
        for reg in regs:
            all_ids.add(reg["discord_id"])
            if reg.get("team_members"):
                for mid in reg["team_members"].split(","):
                    all_ids.add(mid)

        sent = 0
        for did in all_ids:
            try:
                member = guild.get_member(int(did))
                if member:
                    await member.send(msg)
                    sent += 1
            except Exception:
                pass
        log_bot_action(ev["id"], "dm_players", f"DM'd {sent}/{len(all_ids)} players")

    async def _start_game(self, guild: discord.Guild, params: dict) -> None:
        from database import create_game_record, execute, get_event_players, query_one

        ev = get_event(params["event_id"])
        if not ev:
            return

        game_number = params["game_number"]
        room_code = params.get("room_code") or ev.get("room_code") or ""

        game = query_one(
            "SELECT * FROM vtx_games WHERE event_id = %s AND game_number = %s",
            (ev["id"], game_number),
        )
        if not game:
            game_id = create_game_record(ev["id"], game_number, room_code)
        else:
            game_id = game["id"]
            execute(
                "UPDATE vtx_games SET status = 'in_progress', "
                "started_at = CURRENT_TIMESTAMP WHERE id = %s",
                (game_id,),
            )

        execute(
            "UPDATE vtx_events SET status = 'in_progress', current_game = %s WHERE id = %s",
            (game_number, ev["id"]),
        )

        players = get_event_players(ev["id"])
        for p in players:
            execute(
                "INSERT INTO vtx_game_players (game_id, player_id) VALUES (%s, %s) ON CONFLICT DO NOTHING",
                (game_id, p["id"]),
            )

    async def _end_game(self, guild: discord.Guild, params: dict) -> None:
        from database import apply_placement_points, award_coins_for_placements, execute, query_one

        ev = get_event(params["event_id"])
        if not ev:
            return

        game_number = params["game_number"]
        game = query_one(
            "SELECT * FROM vtx_games WHERE event_id = %s AND game_number = %s",
            (ev["id"], game_number),
        )
        if not game:
            return

        execute(
            "UPDATE vtx_games SET status = 'completed', ended_at = CURRENT_TIMESTAMP WHERE id = %s",
            (game["id"],),
        )

        placements = params.get("placements", {})
        for discord_id, data in placements.items():
            p = query_one("SELECT id FROM vtx_players WHERE discord_id = %s", (discord_id,))
            if p:
                execute(
                    "UPDATE vtx_game_players SET placement = %s, points = %s "
                    "WHERE game_id = %s AND player_id = %s",
                    (data["placement"], data["points"], game["id"], p["id"]),
                )

        apply_placement_points(game["id"], ev["id"])
        award_coins_for_placements(game["id"], ev["id"])

        await self._post_game_results(guild, ev, game)

        remaining = (ev.get("total_games") or 0) > 0 and game_number >= ev["total_games"]
        if remaining:
            execute("UPDATE vtx_events SET status = 'completed' WHERE id = %s", (ev["id"],))
        else:
            execute("UPDATE vtx_events SET status = 'setup' WHERE id = %s", (ev["id"],))

        await self._post_leaderboard_log(guild, ev)

    async def _start_live_feed(self, guild: discord.Guild, params: dict) -> None:
        from embeds import base as embed_base

        ev = get_event(params["event_id"])
        if not ev:
            return
        channel = guild.get_channel(
            int(ev["dispatch_channel_id"] or ev["channel_id"] or 0)
        )
        if not channel:
            return

        game_number = params.get("game_number", 1)
        embed = embed_base(
            f"📡 Live Feed — {ev['name']} Game {game_number}", 0xE74C3C
        )
        embed.description = "Waiting for kills..."
        embed.set_footer(text="Use /log-kill to add kills")

        msg = await channel.send(embed=embed)
        execute(
            "UPDATE vtx_events SET live_feed_message_id = %s WHERE id = %s",
            (str(msg.id), ev["id"]),
        )

    async def _log_kill(self, guild: discord.Guild, params: dict) -> None:
        from embeds import base as embed_base

        from database import upsert_player

        ev = get_event(params["event_id"])
        if not ev:
            return

        killer_id = params.get("killer_id", "")
        victim_id = params.get("victim_id", "")
        weapon = params.get("weapon", "")

        k_member = guild.get_member(int(killer_id))
        v_member = guild.get_member(int(victim_id))
        if not k_member or not v_member:
            return

        k_player = upsert_player(killer_id, k_member.display_name)
        v_player = upsert_player(victim_id, v_member.display_name)

        game = query_one(
            "SELECT * FROM vtx_games WHERE event_id = %s AND status = 'in_progress' "
            "ORDER BY game_number DESC LIMIT 1",
            (ev["id"],),
        )
        if game:
            execute(
                "INSERT INTO vtx_kills (game_id, killer_id, victim_id, weapon) "
                "VALUES (%s, %s, %s, %s)",
                (game["id"], k_player["id"], v_player["id"], weapon or None),
            )
            execute(
                "UPDATE vtx_game_players SET kills = kills + 1 "
                "WHERE game_id = %s AND player_id = %s",
                (game["id"], k_player["id"]),
            )

        msg_id = ev.get("live_feed_message_id")
        if msg_id:
            try:
                channel = guild.get_channel(
                    int(ev["dispatch_channel_id"] or ev["channel_id"] or 0)
                )
                msg = await channel.fetch_message(int(msg_id))
                embed = msg.embeds[0] if msg.embeds else embed_base(
                    f"📡 Live Feed — {ev['name']}", 0xE74C3C
                )
                current = embed.description or ""
                if current == "Waiting for kills...":
                    current = ""

                weapon_text = f" [{weapon}]" if weapon else ""
                kill_line = (
                    f"💀 **{k_member.display_name}** eliminated "
                    f"**{v_member.display_name}**{weapon_text}"
                )

                lines = current.split("\n") if current else []
                lines.append(kill_line)
                if len(lines) > 20:
                    lines = lines[-20:]

                embed.description = "\n".join(lines)
                embed.set_footer(
                    text=f"{len(lines)} kill(s) | Use /log-kill to add more"
                )
                await msg.edit(embed=embed)
            except Exception:
                pass

    async def _start_session(self, guild: discord.Guild, params: dict) -> None:
        from database import (
            create_game_record,
            create_session,
            execute,
            get_event_active_session,
            get_latest_session,
            get_lobby_active_session,
            get_lobby_latest_session,
            query_one,
            register_match_players,
        )

        ev = get_event(params["event_id"])
        if not ev:
            return

        lobby_id = params.get("lobby_id")
        if lobby_id:
            session = get_lobby_active_session(lobby_id)
            if not session:
                session = get_lobby_latest_session(lobby_id)
                if not session or session["status"] != "pending":
                    session = create_session(ev["id"], lobby_id)
        else:
            session = get_event_active_session(ev["id"])
            if not session:
                session = get_latest_session(ev["id"])
                if not session or session["status"] != "pending":
                    session = create_session(ev["id"])

        sid = session["id"]
        room_code = params.get("room_code") or ev.get("room_code") or ""
        next_match = (session["current_match"] or 0) + 1

        execute(
            "UPDATE vtx_sessions SET status = 'in_progress', room_code = %s, "
            "started_at = COALESCE(started_at, CURRENT_TIMESTAMP) WHERE id = %s",
            (room_code, sid),
        )
        execute(
            "UPDATE vtx_events SET status = 'in_progress', current_game = %s WHERE id = %s",
            (next_match, ev["id"]),
        )

        game_number = (
            query_one(
                "SELECT COALESCE(MAX(game_number), 0) AS n FROM vtx_games WHERE event_id = %s",
                (ev["id"],),
            )["n"]
            or 0
        ) + 1
        game_id = create_game_record(
            ev["id"], game_number, room_code, session_id=sid
        )
        register_match_players(game_id, ev["id"], lobby_id)

        execute(
            "UPDATE vtx_sessions SET current_match = %s WHERE id = %s",
            (next_match, sid),
        )
        log_bot_action(
            ev["id"],
            "start_session",
            f"Session {session['session_number']} match {next_match} started",
        )

        await self._dispatch(
            guild,
            {
                "event_id": ev["id"],
                "code": room_code,
                "game_number": next_match,
                "session_number": session["session_number"],
                "dm_players": True,
            },
        )

    async def _end_session(self, guild: discord.Guild, params: dict) -> None:
        from database import get_lobby_active_session, get_lobby_latest_session

        ev = get_event(params["event_id"])
        if not ev:
            return
        lobby_id = params.get("lobby_id")
        if lobby_id:
            session = get_lobby_active_session(lobby_id)
            if not session:
                session = get_lobby_latest_session(lobby_id)
                if not session or session["status"] == "completed":
                    return
        else:
            session = get_event_active_session(ev["id"])
            if not session:
                session = get_latest_session(ev["id"])
                if not session or session["status"] == "completed":
                    return

        sid = session["id"]
        cur = query_one(
            "SELECT * FROM vtx_games WHERE session_id = %s "
            "ORDER BY game_number DESC LIMIT 1",
            (sid,),
        )
        if cur and cur["status"] != "completed":
            execute(
                "UPDATE vtx_games SET status = 'completed', "
                "ended_at = CURRENT_TIMESTAMP WHERE id = %s",
                (cur["id"],),
            )
            await self._post_game_results(guild, ev, cur)

        execute(
            "UPDATE vtx_sessions SET status = 'completed', "
            "ended_at = CURRENT_TIMESTAMP WHERE id = %s",
            (sid,),
        )
        execute(
            "UPDATE vtx_events SET status = 'setup' WHERE id = %s",
            (ev["id"],),
        )
        log_bot_action(
            ev["id"], "end_session", f"Session {session['session_number']} ended"
        )
        await self._post_session_leaderboard(guild, ev, session)

    async def _post_session_leaderboard(
        self, guild: discord.Guild, ev: dict, session: dict
    ) -> None:
        from embeds import base as embed_base

        from database import get_session_leaderboard

        channel = guild.get_channel(
            int(ev["dispatch_channel_id"] or ev["channel_id"] or 0)
        )
        if not channel:
            return
        board = get_session_leaderboard(session["id"])
        if not board:
            return

        medals = ["🥇", "🥈", "🥉"]
        lines = []
        for i, row in enumerate(board):
            medal = medals[i] if i < 3 else f"{i+1}."
            if row.get("discord_id"):
                name = f"<@{row['discord_id']}>"
            else:
                name = row.get("username") or row.get("team_name", "Unknown")
            dq = " 🚫" if row.get("is_dq") else ""
            lines.append(
                f"{medal} **{name}** — {row['total_points']} pts ({row['total_kills']} kills) "
                f"| {row.get('wins', 0)}W | avg #{row.get('avg_placement') or '—'} "
                f"| {row.get('placement_points', 0)} pp{dq}"
            )

        embed = embed_base(
            f"📊 Session {session['session_number']} Leaderboard — {ev['name']}",
            0x3498DB,
        )
        embed.description = "\n".join(lines)
        embed.set_footer(text=f"Event ID: {ev['id']}")
        try:
            await channel.send(embed=embed)
        except Exception:
            pass

    async def _end_match(self, guild: discord.Guild, params: dict) -> None:
        ev = get_event(params["event_id"])
        if not ev:
            return

        game_number = params["game_number"]
        game = query_one(
            "SELECT * FROM vtx_games WHERE event_id = %s AND game_number = %s",
            (ev["id"], game_number),
        )
        if not game:
            return

        execute(
            "UPDATE vtx_games SET status = 'completed', "
            "ended_at = CURRENT_TIMESTAMP WHERE id = %s",
            (game["id"],),
        )

        from database import award_coins_for_placements

        award_coins_for_placements(game["id"], ev["id"])

        await self._post_game_results(guild, ev, game)

        if game.get("session_id"):
            session = get_session(game["session_id"])
            if not session:
                return
            await self._post_session_leaderboard(guild, ev, session)
            log_bot_action(
                ev["id"],
                "end_match",
                f"Session {session['session_number']} match {game_number} ended",
            )
            if params.get("end_session"):
                execute(
                    "UPDATE vtx_sessions SET status = 'completed', "
                    "ended_at = CURRENT_TIMESTAMP WHERE id = %s",
                    (session["id"],),
                )
                await self._post_session_leaderboard(guild, ev, session)
                log_bot_action(
                    ev["id"],
                    "end_session",
                    f"Session {session['session_number']} ended after final match",
                )
                return
            await self._advance_session_match(guild, ev, session)
            return

        remaining = (ev.get("total_games") or 0) > 0 and game_number >= ev["total_games"]
        if remaining:
            execute("UPDATE vtx_events SET status = 'completed' WHERE id = %s", (ev["id"],))
        else:
            execute("UPDATE vtx_events SET status = 'setup' WHERE id = %s", (ev["id"],))
        log_bot_action(ev["id"], "end_match", f"Game {game_number} ended")

        await self._post_leaderboard_log(guild, ev)

    async def _advance_session_match(
        self, guild: discord.Guild, ev: dict, session: dict
    ) -> None:
        from database import (
            create_game_record,
            execute,
            query_one,
            register_match_players,
        )

        sid = session["id"]
        next_match = (session["current_match"] or 0) + 1
        room_code = session.get("room_code") or ev.get("room_code") or ""

        game_number = (
            query_one(
                "SELECT COALESCE(MAX(game_number), 0) AS n FROM vtx_games WHERE event_id = %s",
                (ev["id"],),
            )["n"]
            or 0
        ) + 1
        game_id = create_game_record(
            ev["id"], game_number, room_code, session_id=sid
        )
        register_match_players(game_id, ev["id"], session.get("lobby_id"))

        execute(
            "UPDATE vtx_sessions SET current_match = %s WHERE id = %s",
            (next_match, sid),
        )
        execute(
            "UPDATE vtx_events SET status = 'in_progress', current_game = %s WHERE id = %s",
            (next_match, ev["id"]),
        )
        log_bot_action(
            ev["id"],
            "start_session",
            f"Session {session['session_number']} match {next_match} started (auto)",
        )

        await self._dispatch(
            guild,
            {
                "event_id": ev["id"],
                "code": room_code,
                "game_number": next_match,
                "session_number": session["session_number"],
                "dm_players": True,
            },
        )

    async def _game_results_embed(
        self, guild: discord.Guild, ev: dict, game: dict
    ) -> discord.Embed | None:
        from embeds import base as embed_base

        from database import get_game_players, get_game_team_leaderboard

        channel = guild.get_channel(
            int(ev["dispatch_channel_id"] or ev["channel_id"] or 0)
        )
        if not channel:
            return None

        game_number = game["game_number"]
        team_size = ev.get("team_size", 1)
        medals = ["🥇", "🥈", "🥉"]
        lines = [f"📊 **Game {game_number} Results — {ev['name']}**\n"]

        if team_size >= 2:
            board = get_game_team_leaderboard(game["id"], ev["id"])
            if board:
                for i, row in enumerate(board):
                    medal = medals[i] if i < 3 else f"{i+1}."
                    dq = " 🚫" if row["is_dq"] else ""
                    lead_id = row.get("lead_id")
                    team_members = row.get("team_members", "")
                    mentions = []
                    if lead_id:
                        member = guild.get_member(int(lead_id))
                        mentions.append(member.mention if member else f"<@{lead_id}>")
                    if team_members:
                        for mid in team_members.split(","):
                            mid = mid.strip()
                            if mid:
                                member = guild.get_member(int(mid))
                                mentions.append(member.mention if member else f"<@{mid}>")
                    name = " x ".join(mentions) if mentions else row["team_name"]
                    lines.append(
                        f"{medal} {name} — "
                        f"{row['total_points']} pts, {row['total_kills']} kills{dq}"
                    )
            else:
                lines.append("No team data.")
        else:
            players = get_game_players(game["id"])
            if players:
                for i, p in enumerate(players):
                    medal = medals[i] if i < 3 else f"{i+1}."
                    dq = " 🚫" if p["is_disqualified"] else ""
                    if p.get("discord_id"):
                        name = f"<@{p['discord_id']}>"
                    else:
                        name = p.get("username") or "Unknown"
                    lines.append(
                        f"{medal} **{name}** — "
                        f"{p['points']} pts, {p['kills']} kills{dq}"
                    )
            else:
                lines.append("No player data.")

        embed = embed_base(
            f"📊 Game {game_number} Results — {ev['name']}", 0xF39C12
        )
        embed.description = "\n".join(lines)
        return embed

    async def _post_game_results(
        self, guild: discord.Guild, ev: dict, game: dict
    ) -> None:
        channel = guild.get_channel(
            int(ev["dispatch_channel_id"] or ev["channel_id"] or 0)
        )
        if not channel:
            return
        embed = await self._game_results_embed(guild, ev, game)
        if embed:
            try:
                await channel.send(embed=embed)
            except Exception:
                pass

    async def _post_leaderboard_log(self, guild: discord.Guild, ev: dict) -> None:
        from config import digits_only, settings

        from embeds import base as embed_base

        from database import get_leaderboard_full

        log_channel_id = digits_only(settings.discord_leaderboard_log_channel_id)
        if not log_channel_id:
            return
        channel = guild.get_channel(int(log_channel_id))
        if not channel:
            return

        board = get_leaderboard_full(ev["id"])
        if not board:
            return

        medals = ["🥇", "🥈", "🥉"]
        lines = []
        for i, row in enumerate(board[:15]):
            medal = medals[i] if i < 3 else f"{i+1}."
            if row.get("discord_id"):
                name = f"<@{row['discord_id']}>"
            else:
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

        embed = embed_base(f"📈 {ev['name']} — Leaderboard Update", 0x3498DB)
        embed.description = "\n".join(lines)
        embed.set_footer(text=f"Event ID: {ev['id']}")
        try:
            await channel.send(embed=embed)
        except Exception:
            pass

    async def _end_event(self, guild: discord.Guild, params: dict) -> None:
        from embeds import base as embed_base

        from database import calc_event_pr, get_leaderboard, get_team_leaderboard

        ev = get_event(params["event_id"])
        if not ev:
            return

        execute("UPDATE vtx_events SET status = 'completed' WHERE id = %s", (ev["id"],))

        pr_map = calc_event_pr(ev["id"])
        for did, pr_val in pr_map.items():
            execute("UPDATE vtx_players SET pr = %s WHERE discord_id = %s", (pr_val, did))

        from database import record_event_wins

        record_event_wins(ev["id"])

        from database import grant_event_coin_rewards

        grant_event_coin_rewards(ev["id"])

        team_size = ev.get("team_size", 1)
        if team_size >= 2:
            board = get_team_leaderboard(ev["id"])
        else:
            board = get_leaderboard(ev["id"])

        channel = guild.get_channel(
            int(ev["dispatch_channel_id"] or ev["channel_id"] or 0)
        )
        if channel:
            embed = embed_base(f"🏆 {ev['name']} — Final Results", 0xF1C40F)
            if board:
                medals = ["🥇", "🥈", "🥉"]
                lines = []
                for i, row in enumerate(board):
                    medal = medals[i] if i < 3 else f"{i+1}."
                    if team_size >= 2:
                        lead_id = row.get("lead_id")
                        team_members = row.get("team_members", "")
                        mentions = []
                        if lead_id:
                            member = guild.get_member(int(lead_id))
                            mentions.append(member.mention if member else f"<@{lead_id}>")
                        if team_members:
                            for mid in team_members.split(","):
                                mid = mid.strip()
                                if mid:
                                    member = guild.get_member(int(mid))
                                    mentions.append(member.mention if member else f"<@{mid}>")
                        name = " x ".join(mentions) if mentions else row.get("team_name", "Unknown")
                    else:
                        if row.get("discord_id"):
                            name = f"<@{row['discord_id']}>"
                        else:
                            name = row.get("username", "Unknown")
                    placements = row.get("placements") or []
                    pl_str = ", ".join(f"#{p}" for p in placements) if placements else "—"
                    lines.append(
                        f"{medal} {name} — "
                        f"{row['total_points']} pts ({row['total_kills']} kills) "
                        f"| {row.get('wins', 0)}W | avg #{row.get('avg_placement') or '—'} "
                        f"| {pl_str} | {row.get('placement_points', 0)} pp"
                    )
                embed.description = "\n".join(lines)
            else:
                embed.description = "No scores recorded."
            await channel.send(embed=embed)

        await self._post_leaderboard_log(guild, ev)

        from ranks import sync_legend_role, sync_rank_role

        for row in board:
            did = row.get("discord_id") or row.get("lead_id")
            if not did:
                continue
            member = guild.get_member(int(did))
            if not member:
                continue
            try:
                p = query_one("SELECT pr FROM vtx_players WHERE discord_id = %s", (did,))
                await sync_rank_role(guild, member, (p["pr"] if p else 0) or 0)
            except Exception:
                pass
        try:
            await sync_legend_role(guild)
        except Exception:
            pass

        log_bot_action(ev["id"], "end_event", "Event ended, final results posted")

    async def _dq_notify(self, guild: discord.Guild, params: dict) -> None:
        ev = get_event(params["event_id"])
        if not ev:
            return

        discord_id = params["discord_id"]
        reason = params.get("reason", "No reason given")

        reg = query_one(
            "SELECT * FROM vtx_registrations WHERE event_id = %s AND discord_id = %s",
            (ev["id"], discord_id),
        )
        if not reg:
            reg = query_one(
                "SELECT * FROM vtx_registrations WHERE event_id = %s AND (discord_id = %s OR team_members LIKE %s)",
                (ev["id"], discord_id, f"%{discord_id}%"),
            )

        all_ids = set()
        if reg:
            all_ids.add(reg["discord_id"])
            if reg.get("team_members"):
                for mid in reg["team_members"].split(","):
                    all_ids.add(mid.strip())

        if not all_ids:
            all_ids.add(discord_id)

        for did in all_ids:
            try:
                member = guild.get_member(int(did))
                if member:
                    await member.send(
                        f"🚫 You have been disqualified from **{ev['name']}**\n"
                        f"Reason: {reason}\n"
                        f"If you believe this is a mistake, contact staff."
                    )
            except Exception:
                pass

        log_bot_action(ev["id"], "dq_notify", f"DQ'd player {discord_id}: {reason}")


async def setup(bot: commands.Bot) -> None:
    await bot.add_cog(CommandQueueCog(bot))
