from __future__ import annotations

import random
import time

import discord
from discord import app_commands
from discord.ext import commands

from embeds import base, error, success
from config import settings
from templates_fmt import role_ping
from database import (
    count_event_players,
    execute,
    get_event,
    get_event_player_placement,
    get_event_player_stats,
    get_leaderboard,
    get_player_position,
    get_player_profile,
    get_players_leaderboard,
    get_rank_tiers,
    get_season,
    get_season_stats,
    get_team_leaderboard,
    is_player_banned,
    query,
    query_one,
    upsert_player,
)
from ranks import get_player_rank


class RegisterModal(discord.ui.Modal, title="Register Player"):
    ign = discord.ui.TextInput(
        label="In-Game Name (IGN)",
        placeholder="Your Buildnow.gg display name",
        required=True,
        max_length=32,
    )
    game_id = discord.ui.TextInput(
        label="Game ID",
        placeholder="Your Game ID",
        required=True,
        max_length=64,
    )

    def __init__(self, user: discord.Member) -> None:
        super().__init__()
        self.user = user

    async def on_submit(self, interaction: discord.Interaction) -> None:
        discord_id = str(self.user.id)
        if is_player_banned(discord_id):
            await interaction.response.send_message(
                embed=error("You are banned from registering."), ephemeral=True
            )
            return
        username = self.user.display_name
        ign_val = self.ign.value.strip()
        gid_val = self.game_id.value.strip()

        existing = query_one("SELECT * FROM vtx_players WHERE discord_id = %s", (discord_id,))
        if existing:
            execute(
                "UPDATE vtx_players SET username = %s, game_username = %s, game_id = %s WHERE discord_id = %s",
                (username, ign_val, gid_val, discord_id),
            )
            await interaction.response.send_message(
                embed=success(f"Profile updated!\nIGN: **{ign_val}**\nGame ID: **{gid_val}**"),
            )
        else:
            upsert_player(discord_id, username)
            execute(
                "UPDATE vtx_players SET game_username = %s, game_id = %s WHERE discord_id = %s",
                (ign_val, gid_val, discord_id),
            )
            await interaction.response.send_message(
                embed=success(f"Registered!\nIGN: **{ign_val}**\nGame ID: **{gid_val}**"),
            )


class RegisterButton(discord.ui.View):
    def __init__(self, user: discord.Member) -> None:
        super().__init__(timeout=120)
        self.user = user

    @discord.ui.button(label="Open Registration Form", style=discord.ButtonStyle.primary, emoji="📝")
    async def open_modal(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        if interaction.user.id != self.user.id:
            await interaction.response.send_message("This button isn't for you!", ephemeral=True)
            return
        modal = RegisterModal(user=self.user)
        await interaction.response.send_modal(modal)


class GeneralCog(commands.Cog):
    def __init__(self, bot: commands.Bot) -> None:
        self.bot = bot
        self.start_time = time.time()

    @commands.hybrid_command(name="ping", description="Check bot latency and uptime")
    async def ping(self, ctx: commands.Context) -> None:
        latency = round(self.bot.latency * 1000)
        embed = base("🏓 Pong!", 0x2ECC71)
        embed.add_field(name="Latency", value=f"{latency}ms", inline=True)
        embed.add_field(name="Uptime", value=self._get_uptime(), inline=True)
        await ctx.send(embed=embed)

    @commands.hybrid_command(
        name="create-scrim",
        description="Post a scrim lobby ad with room code, format, region, team size and shoot timer",
    )
    @app_commands.describe(
        code="Room code, e.g. OMNLK",
        format="Event format, e.g. Zonewars",
        region="Region, e.g. Eu",
        team_size="Solo / Duo / Trio / Squad",
        timer="Shoot timer as a string, e.g. 4:30",
    )
    async def create_scrim(
        self,
        ctx: commands.Context,
        code: str,
        format: str,
        region: str,
        team_size: str = "Solo",
        timer: str = "0:00",
    ) -> None:
        team_size = team_size.capitalize()
        text = (
            f"Scrim {team_size}Format : {format}\n"
            f"Region : {region}\n"
            f"Code : {code}\n"
            f"Shooting Timer : {timer}\n"
            f"{role_ping(settings.discord_scrim_role_id)}"
        )
        await ctx.send(text)

    @commands.hybrid_command(
        name="register-player",
        description="Register yourself in the system with your IGN and Game ID",
    )
    @app_commands.describe(
        ign="Your in-game name (optional — opens modal if empty)",
        game_id="Your game ID (optional — opens modal if empty)",
    )
    async def register_player(
        self,
        ctx: commands.Context,
        ign: str = "",
        game_id: str = "",
    ) -> None:
        if ign and game_id:
            user = ctx.author
            discord_id = str(user.id)
            if is_player_banned(discord_id):
                await ctx.send(embed=error("You are banned from registering."))
                return
            username = user.display_name

            existing = query_one("SELECT * FROM vtx_players WHERE discord_id = %s", (discord_id,))
            if existing:
                execute(
                    "UPDATE vtx_players SET username = %s, game_username = %s, game_id = %s WHERE discord_id = %s",
                    (username, ign.strip(), game_id.strip(), discord_id),
                )
                await ctx.send(
                    embed=success(f"Profile updated!\nIGN: **{ign.strip()}**\nGame ID: **{game_id.strip()}**"),
                )
            else:
                upsert_player(discord_id, username)
                execute(
                    "UPDATE vtx_players SET game_username = %s, game_id = %s WHERE discord_id = %s",
                    (ign.strip(), game_id.strip(), discord_id),
                )
                await ctx.send(
                    embed=success(f"Registered!\nIGN: **{ign.strip()}**\nGame ID: **{game_id.strip()}**"),
                )
        else:
            modal = RegisterModal(user=ctx.author)
            if ctx.interaction:
                await ctx.interaction.response.send_modal(modal)
            else:
                await ctx.send(
                    embed=base("📝 Register", 0x3498DB),
                    view=RegisterButton(ctx.author),
                )

    @commands.hybrid_command(name="commands", description="Show all available commands")
    async def help_command(self, ctx: commands.Context) -> None:
        embed = base("📚 Commands", 0x3498DB)
        embed.description = "All commands work as `/command` (slash) or `:command` (prefix)"

        embed.add_field(
            name="🎯 Event Commands",
            value=(
                "`create-event` — Create a new scrim/cup event with settings\n"
                "`create-scrim` — Create a scrim with auto-generated SCRIM-XXXX ID\n"
                "`start-event` — Start an event, create temp channel & team roles\n"
                "`start-scrim` — Start a scrim, dispatch room code to channel & DMs\n"
                "`start-game` — Start a game, DM players with room code\n"
                "`end-game` — End a game, show results & leaderboard\n"
                "`end-event` — End event, post final results, cleanup channel & roles\n"
                "`end-scrim` — End scrim, show final leaderboard\n"
                "`dm-players` — DM all registered players with event info\n"
                "`schedule` — Schedule an event (post embed with 🔔 interested button)\n"
                "`unschedule` — Cancel a scheduled event\n"
            ),
            inline=False,
        )

        embed.add_field(
            name="📝 Registration",
            value=(
                "`open-registration` — Open signups, post register button\n"
                "`close-registration` — Close signups, disable register button\n"
                "`register-player` — Register your IGN & Game ID\n"
                "`change-ign` — Change your in-game name\n"
                "`change-data` — Change your name, game id, country or region\n"
                "`stats` — View your or another player's stats\n"
            ),
            inline=False,
        )

        embed.add_field(
            name="🏟️ Lobbies",
            value=(
                "`create-lobby` — Create a lobby for an event\n"
                "`join-lobby` — Add a player to a lobby\n"
                "`remove-from-lobby` — Remove a player from a lobby\n"
                "`lobby-info` — Show lobby details and players\n"
                "`lobbies` — List all lobbies for an event\n"
                "`set-lobby-code` — Set room code for a lobby\n"
                "`close-lobby` — Close a lobby\n"
            ),
            inline=False,
        )

        embed.add_field(
            name="📊 Info & Fun",
            value=(
                "`ping` — Check bot latency & uptime\n"
                "`commands` — Show this message\n"
                "`leaderboard` — Show event leaderboard\n"
                "`events` — List active events\n"
                "`rank` — Check your rank & position\n"
                "`season` — Show current season\n"
                "`season-stats` — Stats for a season (yours or another player's)\n"
                "`best-players` — Best players by PR/kills/wins/placement\n"
                "`worst-players` — Worst players by PR/kills/wins/placement\n"
                "`game-style` — Analyze a player's game style\n"
                "`compare` — Settle who's the goat\n"
                "`event-placement` — Your placement in an event\n"
                "`event-stats` — Your stats in an event\n"
                "`how-to-play` — How to play in scrims\n"
                "`8ball` — Ask the magic 8-ball\n"
                "`flip` — Flip a coin\n"
                "`roll` — Roll dice (default 6 sides)\n"
                "`say-hi` — Greet the boss\n"
            ),
            inline=False,
        )

        embed.add_field(
            name="🔧 Admin Commands",
            value=(
                "`assign-points` — Assign points to a player in a game\n"
                "`add-kills` — Manually set kills for a player\n"
                "`dq-player` — Disqualify a player from a game\n"
                "`game-stats` — Show stats for a specific game\n"
                "`admin events` — List all events\n"
                "`admin players` — List players of an event\n"
                "`admin matches` — List matches of an event\n"
                "`admin leaderboard` — Show leaderboard of an event\n"
                "`admin info` — Show detailed event info\n"
                "`admin qualify` — Add a player to an event's qualified list\n"
                "`admin qualified` — List an event's qualified players\n"
                "`admin remove-qualified` — Remove a player from an event's qualified list\n"
                "`admin move-qualified` — Move qualified players to another event (no re-registration)\n"
            ),
            inline=False,
        )

        await ctx.send(embed=embed)

    @commands.hybrid_command(name="how-to-play", description="Learn how to register, join scrims, and earn points")
    async def how_to_play(self, ctx: commands.Context) -> None:
        embed = base("🎮 How to Play", 0xF39C12)

        embed.add_field(
            name="1️⃣ Register",
            value=(
                "Use `/register-player` to set your IGN and Game ID.\n"
                "Or click the Register button when signups open."
            ),
            inline=False,
        )

        embed.add_field(
            name="2️⃣ Join the Scrim",
            value=(
                "When the scrim starts, you'll get a DM with the room code.\n"
                "Join the game with the code provided."
            ),
            inline=False,
        )

        embed.add_field(
            name="3️⃣ Play & Earn Points",
            value=(
                "**Kill** = +1 point (configurable)\n"
                "**Win** = +5 points (configurable)\n"
                "Play consistently to climb the leaderboard!"
            ),
            inline=False,
        )

        embed.add_field(
            name="4️⃣ PR System",
            value=(
                "Your **PR (Power Rating)** increases based on performance.\n"
                "Base: 100 | +50 per win | +5 per kill\n"
                "Higher PR = better ranking!"
            ),
            inline=False,
        )

        embed.add_field(
            name="📋 Rules",
            value=(
                "• No teaming with other teams\n"
                "• No stream sniping\n"
                "• Be respectful in chat\n"
                "• Have fun!"
            ),
            inline=False,
        )

        await ctx.send(embed=embed)

    @commands.hybrid_command(name="8ball", description="Ask the magic 8-ball")
    @app_commands.describe(question="Your question")
    async def eight_ball(self, ctx: commands.Context, *, question: str) -> None:
        responses = [
            "It is certain.", "It is decidedly so.", "Without a doubt.",
            "Yes definitely.", "You may rely on it.", "As I see it, yes.",
            "Most likely.", "Outlook good.", "Yes.", "Signs point to yes.",
            "Reply hazy, try again.", "Ask again later.",
            "Better not tell you now.", "Cannot predict now.",
            "Concentrate and ask again.", "Don't count on it.",
            "My reply is no.", "My sources say no.",
            "Outlook not so good.", "Very doubtful.",
        ]
        answer = random.choice(responses)
        embed = base("🎱 Magic 8-Bolt", 0x9B59B6)
        embed.add_field(name="Question", value=question, inline=False)
        embed.add_field(name="Answer", value=answer, inline=False)
        await ctx.send(embed=embed)

    @commands.hybrid_command(name="flip", description="Flip a coin")
    async def flip(self, ctx: commands.Context) -> None:
        result = random.choice(["Heads", "Tails"])
        emoji = "🪙" if result == "Heads" else "🔴"
        embed = base(f"{emoji} Coin Flip", 0xF1C40F)
        embed.description = f"**{result}!**"
        await ctx.send(embed=embed)

    @commands.hybrid_command(name="roll", description="Roll dice")
    @app_commands.describe(sides="Number of sides (default: 6)")
    async def roll(self, ctx: commands.Context, sides: int = 6) -> None:
        if sides < 2:
            await ctx.send(
                embed=error("Sides must be at least 2."),
            )
            return
        result = random.randint(1, sides)
        embed = base("🎲 Dice Roll", 0xE74C3C)
        embed.description = f" Rolled **{result}** (1-{sides})"
        await ctx.send(embed=embed)

    @commands.hybrid_command(name="rank", description="Check your competitive rank based on your PR")
    @app_commands.describe(user="User to check rank for (optional)")
    async def rank(self, ctx: commands.Context, user: discord.Member | None = None) -> None:
        target = user or ctx.author
        rank_info = get_player_rank(str(target.id))
        if not rank_info:
            await ctx.send(embed=error("Player not registered in the system."))
            return

        position = get_player_position(str(target.id))
        next_tiers = [t for t in get_rank_tiers() if t["pr_min"] > rank_info["pr"]]

        embed = base("🏅 Competitive Rank", 0x2ECC71)
        embed.set_author(name=target.display_name, icon_url=target.display_avatar.url)
        embed.add_field(name="PR", value=str(rank_info["pr"]), inline=True)
        embed.add_field(name="Rank", value=rank_info["rank"], inline=True)
        embed.add_field(name="Position", value=f"#{position}", inline=True)
        if next_tiers:
            next_tier = next_tiers[-1]
            embed.add_field(
                name="Next Rank",
                value=f"{next_tier['name']} ({next_tier['pr_min']} PR)",
                inline=True,
            )
        embed.set_footer(text="PR updates after every game and event")
        await ctx.send(embed=embed)

    @commands.hybrid_command(name="leaderboard", description="Show the leaderboard of an event")
    @app_commands.describe(event_id="Event ID (optional — uses the latest active event)")
    async def leaderboard(self, ctx: commands.Context, event_id: int | None = None) -> None:
        if event_id is None:
            evs = query(
                "SELECT * FROM vtx_events WHERE status = 'in_progress' ORDER BY created_at DESC LIMIT 1"
            )
            if not evs:
                evs = query(
                    "SELECT * FROM vtx_events WHERE status IN ('setup', 'registration') "
                    "ORDER BY created_at DESC LIMIT 1"
                )
            if not evs:
                await ctx.send(
                    embed=error("No active events. Provide an event ID with `/leaderboard <id>`.")
                )
                return
            ev = evs[0]
        else:
            ev = get_event(event_id)
            if not ev:
                await ctx.send(embed=error("Event not found."))
                return

        if ev.get("team_size", 1) >= 2:
            board = get_team_leaderboard(ev["id"])
        else:
            board = get_leaderboard(ev["id"])

        if not board:
            await ctx.send(embed=base(f"🏆 {ev['name']} — No scores yet."))
            return

        medals = ["🥇", "🥈", "🥉"]
        lines = []
        for i, row in enumerate(board[:15]):
            medal = medals[i] if i < 3 else f"{i+1}."
            name = row.get("username") or row.get("team_name", "Unknown")
            if row.get("is_dq"):
                lines.append(f"{medal} ~~{name}~~ — **DQ**")
            else:
                lines.append(
                    f"{medal} **{name}** — {row['total_points']} pts ({row['total_kills']} kills) "
                    f"| {row.get('wins', 0)}W | {row.get('placement_points', 0)} pp"
                )

        embed = base(f"🏆 {ev['name']} — Leaderboard", 0xF1C40F)
        embed.description = "\n".join(lines)
        embed.set_footer(text=f"Event ID: {ev['id']} | Status: {ev['status']}")
        await ctx.send(embed=embed)

    @commands.hybrid_command(name="events", description="List all active events on the server")
    async def events_list(self, ctx: commands.Context) -> None:
        evs = query(
            "SELECT * FROM vtx_events WHERE status IN ('setup', 'registration', 'in_progress') "
            "ORDER BY created_at DESC LIMIT 10"
        )
        if not evs:
            await ctx.send(embed=base("📋 No active events right now."))
            return

        status_icons = {"setup": "🟡", "registration": "🟢", "in_progress": "🔵"}
        lines = []
        for ev in evs:
            team_label = {1: "Solo", 2: "Duo", 3: "Trio"}.get(ev["team_size"], "Solo")
            regs = query_one(
                "SELECT COUNT(*) AS cnt FROM vtx_registrations WHERE event_id = %s AND status = 'confirmed'",
                (ev["id"],),
            )
            count = regs["cnt"] if regs else 0
            icon = status_icons.get(ev["status"], "❓")
            games_label = f"{ev['current_game']}/{ev['total_games']} games"
            if not (ev.get("total_games") or 0):
                games_label = f"{ev['current_game']}/∞ games"
            lines.append(
                f"{icon} **{ev['name']}** (ID: {ev['id']})\n"
                f"  {team_label} | {ev.get('event_format', 'ZoneWars')} | {ev.get('region', 'EU')} "
                f"| {count} registered | {games_label}"
            )

        embed = base("📋 Active Events", 0x3498DB)
        embed.description = "\n\n".join(lines)
        embed.set_footer(text="Use /leaderboard <id> to view scores")
        await ctx.send(embed=embed)

    async def _event_autocomplete(
        self,
        interaction: discord.Interaction,
        current: str,
    ) -> list[app_commands.Choice]:
        events = query(
            "SELECT id, name, created_at FROM vtx_events ORDER BY created_at DESC LIMIT 25"
        )
        choices = []
        for ev in events:
            date = (ev["created_at"] or "")[:10]
            label = f"{ev['name']} - {date} - {ev['id']}"
            if current.lower() in label.lower():
                choices.append(app_commands.Choice(name=label, value=ev["id"]))
        return choices[:25]

    @commands.hybrid_command(
        name="compare",
        description="Compare two players — who's the real goat?",
    )
    @app_commands.describe(player1="First player", player2="Second player")
    async def compare(
        self,
        ctx: commands.Context,
        player1: discord.Member,
        player2: discord.Member,
    ) -> None:
        if player1.id == player2.id:
            embed = base("🤡 Compare", 0xE74C3C)
            embed.description = (
                f"{player1.mention} compared themselves to themselves. "
                "That's the most undefeated record in history — or the saddest. Probably both."
            )
            await ctx.send(embed=embed)
            return

        p1 = get_player_profile(str(player1.id))
        p2 = get_player_profile(str(player2.id))
        if not p1 or not p2:
            missing = player1.display_name if not p1 else player2.display_name
            await ctx.send(
                embed=error(f"**{missing}** isn't registered in the system yet. Tell them to `/register-player`!")
            )
            return

        pr1, pr2 = p1["player"]["pr"] or 0, p2["player"]["pr"] or 0
        w1, w2 = p1["total_wins"], p2["total_wins"]
        k1, k2 = p1["total_kills"], p2["total_kills"]
        g1, g2 = p1["total_games"], p2["total_games"]
        a1, a2 = p1["avg_placement"], p2["avg_placement"]

        score1 = pr1 * 1000 + w1 * 10 + k1
        score2 = pr2 * 1000 + w2 * 10 + k2

        lines = []
        lines.append(f"💎 **PR:** {player1.mention} {pr1} vs {pr2} {player2.mention} — {'ahead' if pr1 > pr2 else 'behind' if pr1 < pr2 else 'even'}")
        lines.append(f"🏆 **Wins:** {w1} vs {w2} — {'leading' if w1 > w2 else 'trailing' if w1 < w2 else 'tied'}")
        lines.append(f"☠️ **Kills:** {k1} vs {k2} — {'farming' if k1 > k2 else 'farmed' if k1 < k2 else 'even'}")
        lines.append(f"🎮 **Games:** {g1} vs {g2} — {'grinder' if g1 > g2 else 'casual' if g1 < g2 else 'equal effort'}")
        lines.append(
            f"🎯 **Avg Placement:** {f'#{a1}' if a1 is not None else '—'} vs {f'#{a2}' if a2 is not None else '—'}"
        )
        tally = "\n".join(lines)

        if score1 > score2:
            winner, loser = player1, player2
            diff = score1 - score2
        elif score2 > score1:
            winner, loser = player2, player1
            diff = score2 - score1
        else:
            winner = loser = None
            diff = 0

        if winner:
            verdict = [
                f"**{winner.mention}** is the goat of this lobby. 🐐",
                "No debate. No rematch clause.",
            ]
            if diff < 100:
                verdict.append(
                    f"**{loser.mention}** was so close they could smell victory... "
                    "then it evaporated. Diff: {diff}."
                )
                verdict.append("Next game, bro. Next game.")
            elif diff < 1000:
                verdict.append(
                    f"**{loser.mention}**'s stats are looking like a creative mode lobby."
                )
                verdict.append(f"Diff: **{diff}**. Comfortable. Embarrassing for the other side.")
            else:
                verdict.append(
                    f"**{loser.mention}** got absolutely obliterated. Not even close."
                )
                verdict.append(f"Diff: **{diff}**. Consider maining the tutorial.")
                verdict.append(
                    f"{loser.display_name}'s comeback arc is officially scheduled... never."
                )
            footer = f"{loser.display_name}'s revenge arc starts... eventually."
        else:
            verdict = [
                f"**{player1.mention}** and **{player2.mention}** are perfectly balanced.",
                "As all things should be.",
                "Two bots. One lobby. Infinite ties.",
            ]
            footer = "Couldn't decide who's worse. The tie says it all."

        embed = base(f"⚔️ The Great {player1.display_name} vs {player2.display_name} Debate", 0xF39C12)
        embed.description = "\n".join(verdict)
        embed.add_field(name="Scorecard", value=tally, inline=False)
        embed.add_field(
            name=player1.display_name,
            value=f"PR: **{pr1}**\nWins: **{w1}**\nKills: **{k1}**\nRank: **{p1['rank']}** (#{p1['position']})",
            inline=True,
        )
        embed.add_field(
            name=player2.display_name,
            value=f"PR: **{pr2}**\nWins: **{w2}**\nKills: **{k2}**\nRank: **{p2['rank']}** (#{p2['position']})",
            inline=True,
        )
        embed.set_footer(text=footer)
        await ctx.send(embed=embed)

    @commands.hybrid_command(
        name="event-placement",
        description="Check a player's final placement in an event",
    )
    @app_commands.describe(
        event="Event (search by name - date - id)",
        user="Player to check (default: you)",
    )
    @app_commands.autocomplete(event=_event_autocomplete)
    async def event_placement(
        self,
        ctx: commands.Context,
        event: int,
        user: discord.Member | None = None,
    ) -> None:
        target = user or ctx.author
        ev = get_event(event)
        if not ev:
            await ctx.send(embed=error("Event not found."))
            return

        result = get_event_player_placement(event, str(target.id))
        if not result or result["position"] is None:
            embed = base(f"📋 {ev['name']} — Placement", 0x3498DB)
            embed.description = f"**{target.display_name}** isn't on the {ev['name']} leaderboard (yet)."
            await ctx.send(embed=embed)
            return

        row = result["row"]
        team_label = "Team" if ev.get("team_size", 1) >= 2 else "Player"
        embed = base(f"🏅 {target.display_name} in {ev['name']}", 0x2ECC71)
        embed.add_field(name="Placement", value=f"**#{result['position']}** of {result['total']}", inline=True)
        embed.add_field(name="Points", value=str(row["total_points"]), inline=True)
        embed.add_field(name="Kills", value=str(row["total_kills"]), inline=True)
        if row.get("wins") is not None:
            embed.add_field(name="Wins", value=str(row["wins"]), inline=True)
        if row.get("avg_points") is not None:
            embed.add_field(name="Avg Points", value=str(row["avg_points"]), inline=True)
        embed.set_footer(text=f"{team_label} · Event ID: {event}")
        await ctx.send(embed=embed)

    @commands.hybrid_command(
        name="event-stats",
        description="Check a player's stats inside an event",
    )
    @app_commands.describe(
        event="Event (search by name - date - id)",
        user="Player to check (default: you)",
    )
    @app_commands.autocomplete(event=_event_autocomplete)
    async def event_stats(
        self,
        ctx: commands.Context,
        event: int,
        user: discord.Member | None = None,
    ) -> None:
        target = user or ctx.author
        ev = get_event(event)
        if not ev:
            await ctx.send(embed=error("Event not found."))
            return

        stats = get_event_player_stats(event, str(target.id))
        if not stats or stats["games"] == 0:
            embed = base(f"📊 {ev['name']} — Stats", 0x3498DB)
            embed.description = f"**{target.display_name}** hasn't played any games in **{ev['name']}** yet."
            await ctx.send(embed=embed)
            return

        placements = stats["placements"]
        pl_str = ", ".join(f"#{p}" for p in placements) if placements else "—"
        embed = base(f"📊 {target.display_name} in {ev['name']}", 0x3498DB)
        embed.add_field(name="Games", value=str(stats["games"]), inline=True)
        embed.add_field(name="Wins", value=str(stats["wins"]), inline=True)
        embed.add_field(name="Kills", value=str(stats["kills"]), inline=True)
        embed.add_field(name="Total Points", value=str(stats["points"]), inline=True)
        embed.add_field(name="Avg Points", value=str(stats["avg_points"]), inline=True)
        embed.add_field(name="Avg Placement", value=str(stats["avg_placement"]) if stats["avg_placement"] is not None else "—", inline=True)
        embed.add_field(name="Placements", value=pl_str, inline=False)
        embed.set_footer(text=f"Event ID: {event}")
        await ctx.send(embed=embed)

    @commands.hybrid_command(
        name="season",
        description="Show the current season",
    )
    async def season(self, ctx: commands.Context) -> None:
        await ctx.send(f"**Season {get_season()}**")

    @staticmethod
    def _stat_sort_key(stat: str, row: dict):
        if stat == "placement":
            return row.get("avg_placement") if row.get("avg_placement") is not None else 999
        if stat == "pr":
            return row.get("pr") or 0
        if stat == "wins":
            return row.get("total_wins") or 0
        return row.get("total_kills") or 0

    @commands.hybrid_command(
        name="best-players",
        description="Show the best players on the server by a stat",
    )
    @app_commands.describe(stat="Stat to rank by", limit="How many to show (max 25)")
    @app_commands.choices(stat=[
        app_commands.Choice(name="PR", value="pr"),
        app_commands.Choice(name="Kills", value="kills"),
        app_commands.Choice(name="Wins", value="wins"),
        app_commands.Choice(name="Placement", value="placement"),
    ])
    async def best_players(
        self,
        ctx: commands.Context,
        stat: str = "pr",
        limit: int = 10,
    ) -> None:
        players = [p for p in get_players_leaderboard() if (p.get("total_games") or 0) > 0]
        if not players:
            await ctx.send(embed=base("🏆 No players with stats yet."))
            return

        limit = max(1, min(limit, 25))
        best = sorted(
            players,
            key=lambda r: self._stat_sort_key(stat, r),
            reverse=(stat != "placement"),
        )[:limit]

        stat_icons = {"pr": "💎", "kills": "☠️", "wins": "🏅", "placement": "🎯"}
        medals = ["🥇", "🥈", "🥉"]
        lines = []
        for i, p in enumerate(best):
            medal = medals[i] if i < 3 else f"{i+1}."
            value = self._stat_sort_key(stat, p)
            if stat == "placement":
                value = f"#{value}"
            lines.append(f"{medal} **{p['username']}** — {value}")

        embed = base(f"{stat_icons[stat]} Best Players — {stat.title()}", 0xF1C40F)
        embed.description = "\n".join(lines)
        embed.set_footer(text=f"Season {get_season()} · {len(best)} shown")
        await ctx.send(embed=embed)

    @commands.hybrid_command(
        name="worst-players",
        description="Show the worst players on the server by a stat",
    )
    @app_commands.describe(stat="Stat to rank by", limit="How many to show (max 25)")
    @app_commands.choices(stat=[
        app_commands.Choice(name="PR", value="pr"),
        app_commands.Choice(name="Kills", value="kills"),
        app_commands.Choice(name="Wins", value="wins"),
        app_commands.Choice(name="Placement", value="placement"),
    ])
    async def worst_players(
        self,
        ctx: commands.Context,
        stat: str = "pr",
        limit: int = 10,
    ) -> None:
        players = [p for p in get_players_leaderboard() if (p.get("total_games") or 0) > 0]
        if not players:
            await ctx.send(embed=base("🏆 No players with stats yet."))
            return

        limit = max(1, min(limit, 25))
        worst = sorted(
            players,
            key=lambda r: self._stat_sort_key(stat, r),
            reverse=(stat == "placement"),
        )[:limit]

        stat_icons = {"pr": "💎", "kills": "☠️", "wins": "🏅", "placement": "🎯"}
        lines = []
        for i, p in enumerate(worst):
            value = self._stat_sort_key(stat, p)
            if stat == "placement":
                value = f"#{value}"
            lines.append(f"{i+1}. **{p['username']}** — {value}")

        embed = base(f"🧻 Worst Players — {stat.title()}", 0x95A5A6)
        embed.description = "\n".join(lines)
        embed.set_footer(text="No hard feelings. Maybe. | Season " + str(get_season()))
        await ctx.send(embed=embed)

    @commands.hybrid_command(
        name="game-style",
        description="Analyze a player's game style from their stats",
    )
    @app_commands.describe(user="Player to analyze (default: you)")
    async def game_style(
        self,
        ctx: commands.Context,
        user: discord.Member | None = None,
    ) -> None:
        target = user or ctx.author
        profile = get_player_profile(str(target.id))
        if not profile:
            await ctx.send(embed=error("Player not registered in the system."))
            return

        games = profile["total_games"]
        wins = profile["total_wins"]
        kills = profile["total_kills"]
        pr = profile["player"]["pr"] or 0
        avg_pl = profile["avg_placement"]

        if games == 0:
            style = "🪑 Benchwarmer"
            desc = (
                f"{target.display_name} hasn't touched a single game.\n"
                "Not one. Not even once.\n"
                "Style: spectating from the lobby, judging everyone, contributing nothing.\n"
                "The only thing warming up is the bench."
            )
        else:
            kpg = kills / games
            win_rate = wins / games
            if avg_pl is not None and avg_pl <= 3 and kpg >= 2:
                style = "👑 Win Machine"
                desc = (
                    "Almost always top 3 with a pile of bodies behind them.\n"
                    f"{kpg:.1f} kills per game and a win rate of {win_rate * 100:.0f}%.\n"
                    f"{target.display_name} doesn't just win — they hand out losses.\n"
                    "Check the leaderboard. Their name is at the top. Again."
                )
            elif kpg >= 3:
                style = "☠️ The Reaper"
                desc = (
                    f"{kpg:.1f} kills per game. Every fight, every storm circle.\n"
                    f"{target.display_name} drops on everything that moves.\n"
                    "Placement? Irrelevant. Body count? Everything.\n"
                    "The lobby prays the zone closes before they do."
                )
            elif kpg >= 1.5 and (avg_pl is None or avg_pl <= 10):
                style = "💥 Aggressive Slayer"
                desc = (
                    "Finds action in seconds and outplays it in the storm.\n"
                    "Third parties for breakfast, full lobbies for lunch.\n"
                    f"{kpg:.1f} kills per fight-heavy game.\n"
                    "Sometimes throws — but the highlight reel forgives everything."
                )
            elif avg_pl is not None and avg_pl <= 5 and kpg < 1:
                style = "❄️ Final Zone Phantom"
                desc = (
                    "Invisible until the last two circles.\n"
                    f"Only {kpg:.1f} kills per game — the kills are bait.\n"
                    f"Avg placement #{avg_pl} tells the real story.\n"
                    "By the time you notice them, it's already top 3."
                )
            elif avg_pl is not None and avg_pl >= 12 and kpg < 1:
                style = "🐀 Placement Merchant"
                desc = (
                    "Plays the storm like a pro, hides in bushes, camps a roof.\n"
                    f"Ends top 5 with {kpg:.1f} kills. The kills are fake, the placement is real.\n"
                    f"Avg placement #{avg_pl} — the zone is their best friend.\n"
                    "Cowardice? No. It's a *strategy*."
                )
            elif wins == 0 and games >= 10:
                style = "🕯️ The Almost"
                desc = (
                    f"{games} games. Zero wins.\n"
                    "Second place? Probably. Heartbreak? Definitely.\n"
                    "They chase victory like a cat chases a laser pointer.\n"
                    "One day. Maybe."
                )
            elif kills == 0:
                style = "🙏 The Pacifist"
                desc = (
                    "Zero eliminations. Not one. Ever.\n"
                    f"{target.display_name} plays Fortnite like a sightseeing tour.\n"
                    "Clear the lobby? They'd rather admire the scenery.\n"
                    "Wholesome. Useless. Blessed."
                )
            elif win_rate >= 0.1:
                style = "🧠 Clutch Artist"
                desc = (
                    f"Win rate of {win_rate * 100:.0f}% — wins against the odds.\n"
                    "Fights only when it counts, disappears otherwise.\n"
                    f"{wins} victories so far, each one colder than the last.\n"
                    "The zone, the mats, the timing: all theirs."
                )
            elif kpg >= 1:
                style = "⚖️ All-Rounder"
                desc = (
                    "Balanced kills, balanced placements, balanced everything.\n"
                    f"{kpg:.1f} kills per game, nobody's nightmare, nobody's free win.\n"
                    "Jack of all trades, master of none.\n"
                    "Still better than the 86% of the lobby that queues next game."
                )
            else:
                style = "🛡️ Storm Lover"
                desc = (
                    f"Low kills ({kpg:.1f}/game), lives to see the final circle.\n"
                    "Heals in the storm like it's a spa day.\n"
                    "Mostly harmless, occasionally top 10.\n"
                    f"PR: {pr} — the zone is their only boyfriend."
                )

        embed = base(f"🎮 {target.display_name}'s Game Style", 0x9B59B6)
        embed.description = f"**{style}**\n\n{desc}"
        embed.add_field(name="Kills/Game", value=f"{kills / games:.1f}" if games else "—", inline=True)
        embed.add_field(name="Win Rate", value=f"{wins / games * 100:.0f}%" if games else "—", inline=True)
        embed.add_field(name="Avg Placement", value=f"#{avg_pl}" if avg_pl is not None else "—", inline=True)
        embed.add_field(name="PR", value=str(pr), inline=True)
        await ctx.send(embed=embed)

    @commands.hybrid_command(
        name="season-stats",
        description="View a player's stats for a season (default: current season, you)",
    )
    @app_commands.describe(
        season="Season number (default: current)",
        user="Player to check (default: you)",
    )
    async def season_stats(
        self,
        ctx: commands.Context,
        season: int | None = None,
        user: discord.Member | None = None,
    ) -> None:
        target = user or ctx.author
        season_num = season if season is not None else get_season()

        row = get_season_stats(season_num, str(target.id))
        if not row:
            no_stats = (
                "No stats recorded for this season." if season is not None
                else "No stats recorded yet — play some games first!"
            )
            embed = base(f"📅 Season {season_num} — {target.display_name}", 0x3498DB)
            embed.description = no_stats
            embed.set_footer(text="Season stats reset every season.")
            await ctx.send(embed=embed)
            return

        label = "Live season record" if season_num == get_season() else "Season record"
        pr = row.get("pr") or 0
        rank_data = get_player_rank(str(target.id))

        embed = base(f"📅 Season {season_num} — {target.display_name}", 0x3498DB)
        embed.description = f"{label} · Season {season_num}"
        embed.add_field(name="💎 PR", value=str(pr), inline=True)
        embed.add_field(
            name="🏅 Rank",
            value=rank_data["rank"] if rank_data else "Unranked",
            inline=True,
        )
        position = row.get("position")
        embed.add_field(name="📍 Position", value=f"#{position}" if position else "—", inline=True)
        embed.add_field(name="🏆 Wins", value=str(row.get("wins") or 0), inline=True)
        embed.add_field(name="☠️ Kills", value=str(row.get("kills") or 0), inline=True)
        embed.add_field(name="🎮 Games", value=str(row.get("games") or 0), inline=True)
        avg_pl = row.get("avg_placement")
        embed.add_field(name="🎯 Avg Placement", value=f"#{avg_pl}" if avg_pl is not None else "—", inline=True)
        await ctx.send(embed=embed)

    @commands.hybrid_command(
        name="status",
        description="Bot status: ping, uptime, and currently active events",
    )
    async def status(self, ctx: commands.Context) -> None:
        embed = base("🟢 Bot Status", 0x2ECC71)
        embed.add_field(name="Ping", value=f"{round(self.bot.latency * 1000)} ms", inline=True)
        embed.add_field(name="Uptime", value=self._get_uptime(), inline=True)
        if ctx.guild:
            embed.add_field(
                name="Server",
                value=f"{ctx.guild.name} ({len(ctx.guild.members)} members)",
                inline=True,
            )

        active = query(
            "SELECT * FROM vtx_events WHERE status IN ('registration', 'in_progress') "
            "ORDER BY id DESC"
        )
        if active:
            from views.registration import team_label

            lines = []
            for ev in active:
                players = count_event_players(ev["id"])
                max_players = ev.get("max_players") or 0
                cap = f"/{max_players}" if max_players > 0 else ""
                lines.append(
                    f"**{ev['name']}** ({team_label(ev['team_size'])}) — {ev['status']} · "
                    f"{players}{cap} players"
                )
            embed.add_field(name="📋 Active Events", value="\n".join(lines), inline=False)
        else:
            embed.add_field(name="📋 Active Events", value="None right now", inline=False)
        await ctx.send(embed=embed)

    @commands.hybrid_command(
        name="say-hi",
        description="Personal greeting command (restricted)",
    )
    async def say_hi(self, ctx: commands.Context) -> None:
        from config import settings

        allowed_id = settings.discord_say_hi_user_id
        if not allowed_id or str(ctx.author.id) != allowed_id:
            await ctx.send(embed=error("This command is not for you. 😌"))
            return
        await ctx.send(
            f"Hai {ctx.author.mention} papichulo, how is your day going?"
        )

    def _get_uptime(self) -> str:
        uptime = int(time.time() - self.start_time)
        hours = uptime // 3600
        minutes = (uptime % 3600) // 60
        seconds = uptime % 60
        if hours > 0:
            return f"{hours}h {minutes}m"
        return f"{minutes}m {seconds}s"


async def setup(bot: commands.Bot) -> None:
    await bot.add_cog(GeneralCog(bot))
