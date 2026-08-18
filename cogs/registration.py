from __future__ import annotations

import re

import discord
from config import settings
from discord import app_commands
from discord.ext import commands
from embeds import base, error, success

from database import (
    check_event_entry,
    count_event_players,
    execute,
    get_event,
    get_event_registrations,
    get_player_stats,
    is_player_banned,
    log_bot_action,
    query_one,
    update_player_fields,
    upsert_player,
)
from views.registration import RegisterView, register_team, team_label, team_signup_format


class RegistrationCog(commands.Cog):
    def __init__(self, bot: commands.Bot) -> None:
        self.bot = bot

    @commands.hybrid_command(
        name="open-registration",
        description="Open signups for an event, post the register button in the signup channel",
    )
    @app_commands.describe(event_id="Event ID")
    async def open_registration(
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
        if ev["status"] != "setup":
            await ctx.send(embed=error("Event must be in 'setup' status."))
            return

        channel = ctx.guild.get_channel(
            int(ev["signup_channel_id"] or ev["channel_id"] or 0)
        )
        if not channel or not isinstance(channel, discord.TextChannel):
            await ctx.send(embed=error("Signup channel not found."))
            return

        await channel.set_permissions(
            ctx.guild.default_role,
            send_messages=True,
            reason="Registration opened",
        )
        execute("UPDATE vtx_events SET status = 'registration' WHERE id = %s", (event_id,))
        await channel.send(f"**{ev['name']} Signups Opened**")

        view = RegisterView(event_id=event_id, team_size=ev["team_size"], qualification_enabled=bool(ev.get("qualification_enabled")))
        button_msg = await channel.send("Click below to register:", view=view)
        execute(
            "UPDATE vtx_events SET register_button_message_id = %s WHERE id = %s",
            (str(button_msg.id), event_id),
        )

        fmt = team_signup_format(ev["team_size"])
        if fmt:
            await channel.send(
                f"📝 **{team_label(ev['team_size'])} event** — type your registration right here:\n"
                f"`{fmt}`\n"
                "Start with your own mention, then your teammate(s), then the skin."
            )

        await ctx.send(
            embed=success(f"Registration opened for **{ev['name']}** in {channel.mention}"),
        )

    @commands.hybrid_command(
        name="close-registration",
        description="Close signups for an event, disable the register button",
    )
    @app_commands.describe(event_id="Event ID")
    async def close_registration(
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

        channel = ctx.guild.get_channel(
            int(ev["signup_channel_id"] or ev["channel_id"] or 0)
        )
        if not channel or not isinstance(channel, discord.TextChannel):
            await ctx.send(embed=error("Signup channel not found."))
            return

        button_msg_id = ev.get("register_button_message_id")
        if button_msg_id:
            try:
                button_msg = await channel.fetch_message(int(button_msg_id))
                disabled_view = RegisterView(event_id=event_id, team_size=ev["team_size"], qualification_enabled=bool(ev.get("qualification_enabled")))
                for item in disabled_view.children:
                    item.disabled = True
                await button_msg.edit(content="**Registration Closed**", view=disabled_view)
            except Exception:
                pass

        await channel.set_permissions(
            ctx.guild.default_role,
            send_messages=False,
            reason="Registration closed",
        )
        execute(
            "UPDATE vtx_events SET status = 'setup' WHERE id = %s AND status = 'registration'",
            (event_id,),
        )
        regs = get_event_registrations(event_id)
        await channel.send(f"**{ev['name']} Signups Closed** — {len(regs)} teams registered")
        await ctx.send(
            embed=success(
                f"Registration closed for **{ev['name']}**. "
                f"{len(regs)} teams registered."
            ),
        )

    @commands.hybrid_command(
        name="reopen-registration",
        description="Reopen signups for an event and re-post the register button",
    )
    @app_commands.describe(event_id="Event ID")
    async def reopen_registration(
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
        if ev["status"] == "completed":
            await ctx.send(embed=error("Event is already completed."))
            return

        channel = ctx.guild.get_channel(
            int(ev["signup_channel_id"] or ev["channel_id"] or 0)
        )
        if not channel or not isinstance(channel, discord.TextChannel):
            await ctx.send(embed=error("Signup channel not found."))
            return

        await channel.set_permissions(
            ctx.guild.default_role,
            send_messages=True,
            reason="Registration reopened",
        )
        execute("UPDATE vtx_events SET status = 'registration' WHERE id = %s", (event_id,))
        await channel.send(f"**{ev['name']} Signups Reopened**")

        view = RegisterView(event_id=event_id, team_size=ev["team_size"], qualification_enabled=bool(ev.get("qualification_enabled")))
        button_msg = await channel.send("Click below to register:", view=view)
        execute(
            "UPDATE vtx_events SET register_button_message_id = %s WHERE id = %s",
            (str(button_msg.id), event_id),
        )

        fmt = team_signup_format(ev["team_size"])
        if fmt:
            await channel.send(
                f"📝 **{team_label(ev['team_size'])} event** — type your registration right here:\n"
                f"`{fmt}`\n"
                "Start with your own mention, then your teammate(s), then the skin."
            )

        await ctx.send(
            embed=success(f"Registration reopened for **{ev['name']}** in {channel.mention}"),
        )

    @commands.hybrid_command(
        name="event-status",
        description="ADMIN: change an event's state (setup, registration, in_progress, completed)",
    )
    @app_commands.describe(event_id="Event ID", status="New status: setup, registration, in_progress, completed")
    async def event_status(
        self, ctx: commands.Context, event_id: int, status: str
    ) -> None:
        if not await self._check_admin(ctx):
            return
        allowed = {"setup", "registration", "in_progress", "completed"}
        status = status.strip().lower()
        if status not in allowed:
            await ctx.send(embed=error(f"Invalid status. Allowed: {', '.join(sorted(allowed))}"))
            return
        ev = get_event(event_id)
        if not ev:
            await ctx.send(embed=error("Event not found."))
            return

        execute("UPDATE vtx_events SET status = %s WHERE id = %s", (status, event_id))
        log_bot_action(event_id, "event_status", f"Status → {status}", str(ctx.author.id))
        await ctx.send(embed=success(f"**{ev['name']}** status → `{status}`"))

    @commands.hybrid_command(
        name="event-settings",
        description="ADMIN: change an event's settings (team size, capacity, games, points, region, format)",
    )
    @app_commands.describe(
        event_id="Event ID",
        team_size="Team size (1=solo, 2=duo, 3=trio, 4=squad)",
        max_players="Max players",
        total_games="Number of games",
        point_kill="Points per elimination",
        point_win="Points for victory",
        region="Region (EU, NA, etc.)",
        event_format="Format (ZoneWars, BoxFights, etc.)",
    )
    async def event_settings(
        self,
        ctx: commands.Context,
        event_id: int,
        team_size: int | None = None,
        max_players: int | None = None,
        total_games: int | None = None,
        point_kill: int | None = None,
        point_win: int | None = None,
        region: str | None = None,
        event_format: str | None = None,
    ) -> None:
        if not await self._check_admin(ctx):
            return
        ev = get_event(event_id)
        if not ev:
            await ctx.send(embed=error("Event not found."))
            return

        changes = {}
        if team_size is not None:
            if team_size < 1 or team_size > 4:
                await ctx.send(embed=error("Team size must be between 1 and 4."))
                return
            changes["team_size"] = team_size
        if max_players is not None:
            if max_players < 1:
                await ctx.send(embed=error("Max players must be at least 1."))
                return
            changes["max_players"] = max_players
        if total_games is not None:
            if total_games < 0:
                await ctx.send(embed=error("Total games can't be negative."))
                return
            changes["total_games"] = total_games
        if point_kill is not None:
            changes["point_kill"] = point_kill
        if point_win is not None:
            changes["point_win"] = point_win
        if region is not None and region.strip():
            changes["region"] = region.strip()
        if event_format is not None and event_format.strip():
            changes["event_format"] = event_format.strip()

        if not changes:
            await ctx.send(embed=error("Provide at least one setting to change."))
            return

        sets = ", ".join(f"{k} = %s" for k in changes)
        execute(f"UPDATE vtx_events SET {sets} WHERE id = %s", (*changes.values(), event_id))
        log_bot_action(event_id, "event_settings", str(changes), str(ctx.author.id))

        lines = [f"{k.replace('_', ' ').title()}: **{v}**" for k, v in changes.items()]
        embed = base("⚙️ Event Settings Updated", 0x2ECC71)
        embed.description = f"**{ev['name']}**\n" + "\n".join(lines)
        await ctx.send(embed=embed)

    @commands.hybrid_command(
        name="add-team",
        description="ADMIN: register a team for an event (first mention = leader)",
    )
    @app_commands.describe(
        event_id="Event ID",
        players="The full team as mentions (first = team leader)",
        skin="Team skin (optional, defaults to 'Default')",
    )
    async def add_team(
        self,
        ctx: commands.Context,
        event_id: int,
        players: str,
        skin: str | None = None,
    ) -> None:
        if not await self._check_admin(ctx):
            return
        ev = get_event(event_id)
        if not ev:
            await ctx.send(embed=error("Event not found."))
            return
        if ev["team_size"] < 2:
            await ctx.send(embed=error("This is a solo event — use `;admin addplayer` instead."))
            return

        ids = re.findall(r"<@!?(\d+)>", players)
        members = []
        for uid in ids:
            member = ctx.guild.get_member(int(uid)) if ctx.guild else None
            if member:
                members.append((str(member.id), member.display_name))
        if not members:
            await ctx.send(embed=error("Mention the players you want in the team."))
            return

        for _, uid in members:
            entry = check_event_entry(event_id, str(uid))
            if not entry["ok"]:
                await ctx.send(embed=error(f"<@{uid}>: {entry['reason']}"))
                return

        result = register_team(ev, members, skin or "Default")
        if not result["ok"]:
            reason = {
                "WRONG_MEMBER_COUNT": (
                    f"{team_label(ev['team_size'])} event needs exactly {result['need']} "
                    f"players, got {result['got']}."
                ),
                "DUPLICATE": "You mentioned the same player more than once.",
                "BANNED": "One or more players are banned from registering.",
                "FULL": "**The event is full!** This team can't be added.",
                "ALREADY_REGISTERED": "Already registered: " + ", ".join(result.get("overlap", [])) + ".",
                "SKIN_REQUIRED": "A team skin is required.",
                "ENTRY_BLOCKED": result.get("reason") or "Entry requirements not met.",
            }.get(result["code"], "Couldn't add that team.")
            await ctx.send(embed=error(reason))
            return

        log_bot_action(event_id, "admin_add_team", result["text"], str(ctx.author.id))
        await ctx.send(embed=success(result["text"].lstrip("✅ ")))

    @commands.hybrid_command(
        name="assign-player",
        description="ADMIN: assign a player to an existing team in a team event",
    )
    @app_commands.describe(event_id="Event ID", player="Player to assign", leader="Team leader whose team gets the player")
    async def assign_player(
        self,
        ctx: commands.Context,
        event_id: int,
        player: discord.Member,
        leader: discord.Member,
    ) -> None:
        if not await self._check_admin(ctx):
            return
        ev = get_event(event_id)
        if not ev:
            await ctx.send(embed=error("Event not found."))
            return
        if ev["team_size"] < 2:
            await ctx.send(embed=error("This is a solo event — use `;admin addplayer` instead."))
            return
        if player.id == leader.id:
            await ctx.send(embed=error("Player and team leader can't be the same."))
            return
        if is_player_banned(str(player.id)):
            await ctx.send(embed=error("That player is banned from registering."))
            return

        entry = check_event_entry(event_id, str(player.id))
        if not entry["ok"]:
            await ctx.send(embed=error(entry["reason"]))
            return

        reg = query_one(
            "SELECT * FROM vtx_registrations WHERE event_id = %s AND discord_id = %s AND status = 'confirmed'",
            (event_id, str(leader.id)),
        )
        if not reg:
            await ctx.send(embed=error(f"{leader.mention} has no team in **{ev['name']}**."))
            return

        team_members = [m for m in (reg["team_members"] or "").split(",") if m]
        if len(team_members) + 1 >= ev["team_size"]:
            await ctx.send(embed=error(f"{leader.mention}'s team is already full ({team_label(ev['team_size'])} event)."))
            return

        pid = str(player.id)
        if pid in team_members:
            await ctx.send(embed=error(f"{player.mention} is already in {leader.mention}'s team."))
            return

        for other in get_event_registrations(event_id):
            other_ids = {other["discord_id"]}
            if other.get("team_members"):
                other_ids.update(other["team_members"].split(","))
            if pid in other_ids:
                await ctx.send(embed=error(f"{player.mention} is already registered in **{ev['name']}**."))
                return

        max_players = ev.get("max_players") or 0
        if max_players > 0 and count_event_players(ev["id"]) >= max_players:
            await ctx.send(embed=error("**The event is full!**"))
            return

        upsert_player(pid, player.display_name)
        team_members.append(pid)
        execute(
            "UPDATE vtx_registrations SET team_members = %s WHERE id = %s",
            (",".join(team_members), reg["id"]),
        )
        log_bot_action(event_id, "admin_assign_player", f"{player.name} → team of {leader.name}", str(ctx.author.id))
        await ctx.send(
            embed=success(f"{player.mention} assigned to {leader.mention}'s team in **{ev['name']}**.")
        )

    @commands.hybrid_command(
        name="remove-from-team",
        description="ADMIN: remove a player from their team in an event (removes the whole team if they're the leader)",
    )
    @app_commands.describe(event_id="Event ID", player="Player to remove from their team")
    async def remove_from_team(
        self,
        ctx: commands.Context,
        event_id: int,
        player: discord.Member,
    ) -> None:
        if not await self._check_admin(ctx):
            return
        ev = get_event(event_id)
        if not ev:
            await ctx.send(embed=error("Event not found."))
            return

        pid = str(player.id)
        reg = query_one(
            "SELECT * FROM vtx_registrations WHERE event_id = %s AND discord_id = %s AND status = 'confirmed'",
            (event_id, pid),
        )
        if reg:
            execute("DELETE FROM vtx_registrations WHERE id = %s", (reg["id"],))
            log_bot_action(event_id, "admin_remove_team", f"Removed {player.name}'s team", str(ctx.author.id))
            await ctx.send(
                embed=success(f"Removed {player.mention}'s whole team from **{ev['name']}**.")
            )
            return

        in_team = query_one(
            "SELECT * FROM vtx_registrations WHERE event_id = %s AND team_members LIKE %s AND status = 'confirmed'",
            (event_id, f"%{pid}%"),
        )
        if not in_team:
            await ctx.send(embed=error(f"{player.mention} is not registered in **{ev['name']}**."))
            return

        from database import remove_player_from_event

        remove_player_from_event(event_id, pid)
        log_bot_action(event_id, "admin_remove_from_team", f"Removed {player.name} from their team", str(ctx.author.id))
        await ctx.send(
            embed=success(f"Removed {player.mention} from their team in **{ev['name']}**.")
        )

    @commands.hybrid_command(
        name="change-ign",
        description="Change your in-game name (IGN) in the system",
    )
    @app_commands.describe(new_ign="Your new in-game name")
    async def change_ign(
        self, ctx: commands.Context, new_ign: str
    ) -> None:
        discord_id = str(ctx.author.id)
        player = query_one("SELECT * FROM vtx_players WHERE discord_id = %s", (discord_id,))
        if not player:
            await ctx.send(embed=error("You are not registered in the system."))
            return

        old_ign = player["game_username"] or "Not set"
        execute(
            "UPDATE vtx_players SET game_username = %s WHERE discord_id = %s",
            (new_ign.strip(), discord_id),
        )
        await ctx.send(
            embed=success(f"IGN changed from **{old_ign}** to **{new_ign.strip()}**"),
        )

    @commands.hybrid_command(
        name="change-data",
        description="Change your profile data: discord name, in-game name, game id, country, region",
    )
    @app_commands.describe(
        username="Your new discord name shown in the system",
        game_name="Your new in-game name (IGN)",
        game_id="Your new Fortnite game id",
        country="Your new country",
        region="Your new region (e.g. EU, NA, ME)",
    )
    async def change_data(
        self,
        ctx: commands.Context,
        username: str | None = None,
        game_name: str | None = None,
        game_id: str | None = None,
        country: str | None = None,
        region: str | None = None,
    ) -> None:
        discord_id = str(ctx.author.id)
        player = query_one("SELECT * FROM vtx_players WHERE discord_id = %s", (discord_id,))
        if not player:
            await ctx.send(embed=error("You are not registered in the system."))
            return

        changes = {
            "username": username,
            "game_username": game_name,
            "game_id": game_id,
            "country": country,
            "region": region,
        }
        changes = {k: v.strip() for k, v in changes.items() if v is not None and v.strip()}
        if not changes:
            await ctx.send(embed=error("Provide at least one field to change."))
            return

        updated = update_player_fields(discord_id, changes)

        from database import PLAYER_EDITABLE_FIELDS

        lines = []
        for key, new_value in changes.items():
            old_value = player.get(key) or "Not set"
            lines.append(f"{PLAYER_EDITABLE_FIELDS[key].title()} `{old_value}` → `{new_value}`")

        embed = base("✏️ Profile Updated", 0x2ECC71)
        embed.description = "\n".join(lines)
        await ctx.send(embed=embed)

    @commands.hybrid_command(
        name="stats",
        description="View your or another player's stats: IGN, Game ID, PR, wins, kills, games",
    )
    @app_commands.describe(user="User to check stats for (optional)")
    async def stats(
        self, ctx: commands.Context, user: discord.Member | None = None
    ) -> None:
        target = user or ctx.author
        discord_id = str(target.id)

        stats_data = get_player_stats(discord_id)
        if not stats_data:
            await ctx.send(embed=error("Player not found in the system."))
            return

        player = stats_data["player"]
        embed = base(f"📊 {target.display_name}'s Stats", 0x3498DB)
        embed.add_field(
            name="In-Game Name",
            value=player.get("game_username") or "Not set",
            inline=True,
        )
        embed.add_field(
            name="Game ID",
            value=player.get("game_id") or "Not set",
            inline=True,
        )
        embed.add_field(
            name="PR (Season)",
            value=str(player.get("pr", 0)),
            inline=True,
        )
        embed.add_field(
            name="Total PR (Lifetime)",
            value=str(player.get("total_pr", 0) + int(player.get("pr", 0) or 0)),
            inline=True,
        )
        embed.add_field(
            name="Total Wins",
            value=str(stats_data["total_wins"]),
            inline=True,
        )
        embed.add_field(
            name="Total Kills",
            value=str(stats_data["total_kills"]),
            inline=True,
        )
        embed.add_field(
            name="Total Games",
            value=str(stats_data["total_games"]),
            inline=True,
        )
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
    await bot.add_cog(RegistrationCog(bot))
