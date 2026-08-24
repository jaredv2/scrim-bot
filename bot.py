from __future__ import annotations

import asyncio
import logging
import traceback

import discord
from discord import app_commands
from config import settings
from discord.ext import commands

from database import init_db

logger = logging.getLogger("scrim-bot")

ACTIVITY_ROTATION = [
    ("watching", "Vortex BuildNow Competitive Server"),
    ("playing", "Vortex scrims with daddy #Chris"),
    ("watching", "Vortex BuildNow tournaments"),
    ("competing", "Cups for the Best PR of the server"),
    ("watching", "Vortex qualifiers with #Chris"),
    ("playing", "Vortex Scrims"),
    ("listening", "to #Chris's instructions"),
    ("watching", "the Vortex leaderboard"),
    ("playing", "Vortex Competitive Cups"),
]

ACTIVITY_TYPES = {
    "playing": discord.ActivityType.playing,
    "watching": discord.ActivityType.watching,
    "listening": discord.ActivityType.listening,
    "competing": discord.ActivityType.competing,
}

COMMAND_SYNTAX = {
    "create-event": "`;create-event <name> <#channel> <#signup_channel> [type] [entry_mode] [pr_cap] [division] [scoring_mode] [region] [format] [start_time] [team_size] [total_games] [max_players] [point_kill] [point_win] [dispatch] [room_code]`",
    "start-event": "`;start-event <event_id> <room_code>`",
    "start-scrim": "`;start-scrim <event_id> <room_code>`",
    "start-game": "`;start-game <event_id> <game_number> [room_code]`",
    "end-game": "`;end-game <event_id> <game_number> [first] [second] [third]`",
    "create-session": "`;create-session <event_id> [lobby_id]`",
    "start-session": "`;start-session <event_id> [lobby_id] [room_code]`",
    "end-session": "`;end-session <event_id> [lobby_id]`",
    "end-event": "`;end-event <event_id>`",
    "end-scrim": "`;end-scrim <event_id>`",
    "dm-players": "`;dm-players <event_id> <room_code> [game_number] [start_time]`",
    "split-lobbies": "`;split-lobbies <event_id> [force]` — auto-split registrations into lobbies",
    "lobby-leaderboard": "`;lobby-leaderboard <lobby_id>`",
    "seed-bracket": "`;seed-bracket <event_id>` — seed a 1v1 bracket ordered by PR",
    "bracket": "`;bracket <event_id>` — view the bracket",
    "advance-bracket": "`;advance-bracket <match_id> <@winner>`",
    "end-bracket": "`;end-bracket <event_id>` — finalize standings + PR",
    "create-division": "`;create-division <name> [role]`",
    "delete-division": "`;delete-division <name>`",
    "divisions": "`;divisions` — list divisions",
    "add-division-member": "`;add-division-member <name> <@player>`",
    "remove-division-member": "`;remove-division-member <name> <@player>`",
    "evaluate-qualifier": "`;evaluate-qualifier <event_id> [apply]` — preview or grant qualifier results",
    "qualifier-status": "`;qualifier-status <event_id>`",
    "open-registration": "`;open-registration <event_id>`",
    "close-registration": "`;close-registration <event_id>`",
    "reopen-registration": "`;reopen-registration <event_id>`",
    "event-status": "`;event-status <event_id> <setup|registration|in_progress|completed>`",
    "event-settings": "`;event-settings <event_id> [team_size] [max_players] [total_games] [point_kill] [point_win] [region] [event_format]`",
    "add-team": "`;add-team <event_id> <@leader> <@player2> [<@player3> <@player4>] [skin]`",
    "assign-player": "`;assign-player <event_id> <@player> <@team_leader>`",
    "remove-from-team": "`;remove-from-team <event_id> <@player>`",
    "register-player": "`;register-player [ign] [game_id]`",
    "change-ign": "`;change-ign <new_ign>`",
    "change-data": "`;change-data [username] [game_name] [game_id] [country] [region]`",
    "stats": "`;stats [@user]`",
    "rank": "`;rank [@user]`",
    "leaderboard": "`;leaderboard [event_id]`",
    "events": "`;events`",
    "compare": "`;compare <@player1> <@player2>`",
    "event-placement": "`;event-placement <event_id> [@user]`",
    "event-stats": "`;event-stats <event_id> [@user]`",
    "assign-points": "`;assign-points <event_id> <game_number> <@player> <points>`",
    "add-kills": "`;add-kills <event_id> <game_number> <@player> <kills>`",
    "dq-player": "`;dq-player <event_id> <game_number> <@player> [reason]`",
    "game-stats": "`;game-stats <event_id> <game_number>`",
    "create-lobby": "`;create-lobby <event_id> <name>`",
    "join-lobby": "`;join-lobby <lobby_id> <@player>`",
    "remove-from-lobby": "`;remove-from-lobby <lobby_id> <@player>`",
    "lobby-info": "`;lobby-info <lobby_id>`",
    "lobbies": "`;lobbies <event_id>`",
    "set-lobby-code": "`;set-lobby-code <lobby_id> <room_code>`",
    "close-lobby": "`;close-lobby <lobby_id>`",
    "8ball": "`;8ball <question>`",
    "roll": "`;roll [sides]`",
    "status": "`;status`",
    "season": "`;season`",
    "season-stats": "`;season-stats [season] [@user]`",
    "best-players": "`;best-players [stat] [limit]`",
    "worst-players": "`;worst-players [stat] [limit]`",
    "game-style": "`;game-style [@user]`",
    "say-hi": "`;say-hi`",
    "qualify": "`;qualify` (via ⭐ Qualify button)",
    "admin-qualify": "`;admin qualify <event_id> <@player>`",
    "admin-qualified": "`;admin qualified <event_id>`",
    "admin-remove-qualified": "`;admin remove-qualified <event_id> <@player>`",
    "admin-move-qualified": "`;admin move-qualified <source_event> <target_event> confirm:yes`",
    "schedule": "`;schedule [event_id] [time]` — schedules an event (e.g. `3:00 PM EST`) or lists scheduled events",
    "unschedule": "`;unschedule <event_id>`",
    "invite-coins": "`;invite-coins [@user]` — check your invite-coin balance",
    "shop": "`;shop` — browse the coin shop",
    "pic-perms": "`;pic-perms <30s|1m|3m|5m>` — buy Pic Perms (30s=2, 1m=5, 3m=7, 5m=15 coins)",
    "coin-top": "`;coin-top` — top inviters by coins",
    "invite-info": "`;invite-info` — see your invite links",
    "invite-review": "`;invite-review` (ADMIN) — list pending/suspicious invite rewards",
    "invite-approve": "`;invite-approve <reward_id>` (ADMIN) — force-approve a reward",
    "invite-reject": "`;invite-reject <reward_id> [reason]` (ADMIN) — reject a reward",
    "removeteam": "`;removeteam <event_id> <@leader>` (ADMIN) — remove a whole team",
    "undq": "`;undq <event_id> <game_number> <@player>` (ADMIN) — undo a DQ",
    "reset-score": "`;reset-score <event_id> confirm:yes` (ADMIN) — delete all scores for an event",
    "add-coins": "`;add-coins <@player> <amount>` (ADMIN)",
    "remove-coins": "`;remove-coins <@player> <amount>` (ADMIN)",
    "reset-coins": "`;reset-coins <@player>` (ADMIN)",
    "lfd": "`;lfd [detail]` — Looking For Duo (1/h cooldown)",
    "lft": "`;lft [detail]` — Looking For Trio (1/h cooldown)",
    "fls": "`;fls [detail]` — Looking For Squad (1/h cooldown)",
    "ask-1v1": "`;ask-1v1 <@opponent>` — challenge to a 1v1 duel",
    "ask-2v2": "`;ask-2v2 <@partner> <@opp1> <@opp2>` — challenge a duo (all must accept)",
}


class ScrimBot(commands.Bot):
    def __init__(self) -> None:
        intents = discord.Intents.default()
        intents.members = True
        intents.guilds = True
        intents.message_content = True
        super().__init__(
            command_prefix=";",
            intents=intents,
            activity=discord.Activity(
                type=ACTIVITY_TYPES[ACTIVITY_ROTATION[0][0]],
                name=ACTIVITY_ROTATION[0][1],
            ),
        )

    async def setup_hook(self) -> None:
        try:
            await asyncio.to_thread(init_db)
        except Exception as exc:
            logger.warning("bot init_db failed (bot will start, DB queries will retry later): %s", exc)
        await self.load_cogs()
        self.tree.error(self._on_app_command_error)
        self.activity_loop = self.loop.create_task(self._rotate_activity())
        logger.info("cogs_loaded")

    async def _rotate_activity(self) -> None:
        index = 1
        while True:
            await asyncio.sleep(180)
            try:
                activity_type, name = ACTIVITY_ROTATION[index % len(ACTIVITY_ROTATION)]
                await self.change_presence(
                    activity=discord.Activity(type=ACTIVITY_TYPES[activity_type], name=name)
                )
                index += 1
            except Exception:
                logger.warning("activity_rotation_failed", exc_info=True)

    async def load_cogs(self) -> None:
        cogs = [
            "cogs.registration",
            "cogs.events",
            "cogs.general",
            "cogs.admin",
            "cogs.lobbies",
            "cogs.brackets",
            "cogs.divisions",
            "cogs.qualifiers",
            "cogs.queue_processor",
            "cogs.hall_of_fame",
            "cogs.schedule",
            "cogs.coins",
            "cogs.lfg",
            "cogs.duels",
            "cogs.ai",
            "cogs.health_ping",
            "views.registration",
        ]
        for cog_path in cogs:
            try:
                await self.load_extension(cog_path)
                logger.info("cog_loaded: %s", cog_path)
            except Exception as e:
                logger.error("cog_load_failed: %s — %s", cog_path, e, exc_info=True)

    async def on_command_error(self, ctx: commands.Context, error: commands.CommandError) -> None:
        if isinstance(error, commands.MissingRequiredArgument):
            cmd_name = ctx.command.name if ctx.command else "command"
            syntax = COMMAND_SYNTAX.get(cmd_name, "")
            msg = f"**Missing required parameter:** `{error.param.name}`\n"
            if syntax:
                msg += f"Correct syntax: {syntax}"
            await ctx.send(embed=discord.Embed(description=msg, color=0xE74C3C))
        elif isinstance(error, commands.TooManyArguments):
            cmd_name = ctx.command.name if ctx.command else "command"
            syntax = COMMAND_SYNTAX.get(cmd_name, "")
            msg = "**Too many arguments provided.**\n"
            if syntax:
                msg += f"Correct syntax: {syntax}"
            await ctx.send(embed=discord.Embed(description=msg, color=0xE74C3C))
        elif isinstance(error, commands.BadArgument):
            cmd_name = ctx.command.name if ctx.command else "command"
            syntax = COMMAND_SYNTAX.get(cmd_name, "")
            msg = f"**Invalid argument:** {error}\n"
            if syntax:
                msg += f"Correct syntax: {syntax}"
            await ctx.send(embed=discord.Embed(description=msg, color=0xE74C3C))
        elif isinstance(error, commands.CommandNotFound):
            pass
        else:
            logger.error("command_error: %s — %s", ctx.command, error, exc_info=True)
            await ctx.send(
                embed=discord.Embed(
                    description=f"An error occurred: {error}",
                    color=0xE74C3C,
                )
            )

    async def _on_app_command_error(
        self, interaction: discord.Interaction, error: app_commands.AppCommandError
    ) -> None:
        if isinstance(error, app_commands.CommandNotFound):
            logger.warning(
                "unknown slash command attempted: %s", interaction.data.get("name")
            )
            return
        if isinstance(error, app_commands.TransformerError):
            msg = f"**Invalid argument type:** {error}"
        elif isinstance(error, app_commands.CommandInvokeError):
            msg = f"**Command error:** {error.original}"
        else:
            msg = f"**Error:** {error}"

        embed = discord.Embed(description=msg, color=0xE74C3C)
        try:
            if interaction.response.is_done():
                await interaction.followup.send(embed=embed, ephemeral=True)
            else:
                await interaction.response.send_message(embed=embed, ephemeral=True)
        except (discord.NotFound, discord.HTTPException):
            # Interaction already expired (e.g. bot restarted mid-use) - nothing to do.
            pass

    async def on_ready(self) -> None:
        logger.info("bot_ready", extra={"user": str(self.user)})
        guild_id = settings.discord_guild_id
        try:
            if guild_id:
                guild = discord.Object(id=int(guild_id))
                self.tree.copy_global_to(guild=guild)
                cmds = await self.tree.sync(guild=guild)
            else:
                cmds = await self.tree.sync()
            logger.info("commands_synced", extra={"count": len(cmds)})
        except Exception:
            logger.exception("command sync failed")


async def main() -> None:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    bot = ScrimBot()
    async with bot:
        await bot.start(settings.discord_bot_token)


if __name__ == "__main__":
    asyncio.run(main())
