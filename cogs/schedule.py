from __future__ import annotations

import logging
import time

import discord
from config import digits_only, settings
from discord import app_commands
from discord.ext import commands, tasks

from embeds import base, error, success
from templates_fmt import to_unix_ts

from database import (
    clear_event_schedule,
    count_event_interests,
    execute,
    get_event,
    get_event_interested,
    get_scheduled_events,
    mark_event_reminded,
    query,
    set_event_schedule,
    toggle_event_interest,
)

logger = logging.getLogger("scrim-bot")


class InterestView(discord.ui.View):
    """Persistent 'Interested' button attached to a scheduled-event embed."""

    def __init__(self, event_id: int) -> None:
        super().__init__(timeout=None)
        self.event_id = event_id

    @discord.ui.button(
        label="Interested",
        emoji="🔔",
        style=discord.ButtonStyle.primary,
        custom_id="event_interest_button",
    )
    async def interested(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        interested = toggle_event_interest(self.event_id, str(interaction.user.id))
        if interested:
            text = (
                "You're on the list! 🎮\n"
                "You'll get a DM **1 hour** before the event starts."
            )
        else:
            text = "You removed your interest. See you next time!"
        try:
            if interaction.message and interaction.message.embeds:
                new_embed = interaction.message.embeds[0].copy()
                new_embed.set_footer(
                    text=f"🔔 {count_event_interests(self.event_id)} interested — press 🔔 to join the list"
                )
                await interaction.message.edit(embed=new_embed)
        except Exception:
            pass
        await interaction.response.send_message(embed=success(text), ephemeral=True)


def build_schedule_embed(ev: dict) -> discord.Embed:
    emb = base(f"🎮 {ev['name']} — Scheduled", 0x2ECC71)
    emb.add_field(name="Event ID", value=str(ev["id"]))
    ts = int(ev["scheduled_at"]) if ev.get("scheduled_at") else None
    if ts:
        emb.add_field(name="Start Time", value=f"<t:{ts}:F>")
        emb.add_field(name="Relative", value=f"<t:{ts}:R>")
    emb.add_field(name="Format", value={1: "Solo", 2: "Duo", 3: "Trio"}.get(ev.get("team_size", 1), "Solo"))
    emb.add_field(name="Region", value=ev.get("region", "EU"))
    emb.add_field(name="Status", value=ev.get("status", "setup"))
    emb.set_footer(
        text=f"🔔 {count_event_interests(ev['id'])} interested — press 🔔 to join the list"
    )
    return emb


class ScheduleCog(commands.Cog):
    def __init__(self, bot: commands.Bot) -> None:
        self.bot = bot
        self.reminder_loop.start()

    def cog_unload(self) -> None:
        self.reminder_loop.cancel()

    async def _check_admin(self, ctx: commands.Context) -> bool:
        if not ctx.author.guild_permissions.administrator:
            await ctx.send(embed=error("You need administrator permissions."))
            return False
        return True

    @staticmethod
    def _target_channel(ctx: commands.Context) -> tuple[discord.TextChannel | None, str]:
        """Resolve where schedule embeds go: configured channel, else the current channel."""
        channel_id = digits_only(settings.discord_schedule_channel_id)
        if channel_id:
            channel = ctx.guild.get_channel(int(channel_id)) if ctx.guild else None
            if channel:
                return channel, "here"
            return None, f"configured channel `{channel_id}` not found"
        return ctx.interaction.channel if ctx.interaction else ctx.channel, ""

    @commands.hybrid_command(
        name="schedule",
        description="Schedule an event (or list scheduled events)",
    )
    @app_commands.describe(
        event_id="Event ID to schedule (optional: list scheduled events instead)",
        time="Start time, e.g. '3:00 PM EST' (required when event_id is given)",
    )
    async def schedule(
        self,
        ctx: commands.Context,
        event_id: int | None = None,
        time: str | None = None,
    ) -> None:
        if not await self._check_admin(ctx):
            return
        if ctx.interaction:
            await ctx.interaction.response.defer(ephemeral=True)

        if event_id is None:
            channel, missing = _target_channel(ctx)
            if not channel:
                await ctx.send(embed=error(missing))
                return
            scheduled = get_scheduled_events()
            if not scheduled:
                await ctx.send(embed=error("No events are currently scheduled."))
                return
            lines = []
            for ev in scheduled:
                ts = int(ev["scheduled_at"])
                lines.append(
                    f"**ID {ev['id']}** — {ev['name']} — <t:{ts}:R> "
                    f"({count_event_interests(ev['id'])} interested)"
                )
            embed = base("📅 Scheduled Events", 0x2ECC71)
            embed.description = "\n".join(lines)
            await channel.send(embed=embed)
            return

        ev = get_event(event_id)
        if not ev:
            await ctx.send(embed=error("Event not found."))
            return
        if not time:
            await ctx.send(
                embed=error("Provide a start time, e.g. `;schedule <event_id> 3:00 PM EST`.")
            )
            return
        unix = to_unix_ts(time)
        if unix is None:
            await ctx.send(
                embed=error(
                    f"Couldn't parse time **{time}**. Use a format like `3:00 PM EST` "
                    "or `18:00 UTC`."
                )
            )
            return

        channel, missing = _target_channel(ctx)
        if not channel:
            await ctx.send(embed=error(missing))
            return
        set_event_schedule(event_id, unix, str(channel.id))
        scheduled_ev = get_event(event_id) or ev
        sent = await channel.send(embed=build_schedule_embed(scheduled_ev), view=InterestView(event_id))
        execute(
            "UPDATE events SET schedule_message_id = ? WHERE id = ?",
            (str(sent.id), event_id),
        )

        location = f" in {channel.mention}" if channel not in (ctx.interaction.channel if ctx.interaction else ctx.channel) else ""
        await ctx.send(
            embed=success(
                f"**{ev['name']}** (ID {event_id}) scheduled for "
                f"<t:{unix}:F> (<t:{unix}:R>).\n"
                f"Schedule posted {location or 'here'}.\n"
                f"Interested members are pinged 1 hour before."
            ),
        )

    @commands.hybrid_command(
        name="unschedule",
        description="Cancel a scheduled event and its pending reminders",
    )
    @app_commands.describe(event_id="Event ID to unschedule")
    async def unschedule(self, ctx: commands.Context, event_id: int) -> None:
        if not await self._check_admin(ctx):
            return
        if ctx.interaction:
            await ctx.interaction.response.defer(ephemeral=True)

        ev = get_event(event_id)
        if not ev:
            await ctx.send(embed=error("Event not found."))
            return
        if not ev.get("scheduled_at"):
            await ctx.send(embed=error("This event isn't scheduled."))
            return

        msg_id = ev.get("schedule_message_id")
        chan_id = ev.get("schedule_channel_id")
        if msg_id and chan_id:
            try:
                channel = ctx.guild.get_channel(int(chan_id))
                if channel:
                    message = channel.get_partial_message(int(msg_id))
                    if message:
                        await message.delete()
            except Exception:
                pass
        clear_event_schedule(event_id)
        await ctx.send(embed=success(f"**{ev['name']}** (ID {event_id}) unscheduled."))

    @tasks.loop(seconds=60)
    async def reminder_loop(self) -> None:
        try:
            await self.bot.wait_until_ready()
        except Exception:
            return
        now = time.time()
        upcoming = query(
            "SELECT * FROM events WHERE scheduled_at IS NOT NULL AND reminder_sent = 0",
        )
        for ev in upcoming:
            ts = int(ev["scheduled_at"])
            if now < ts - 3600 or now >= ts:
                continue
            for row in get_event_interested(ev["id"]):
                try:
                    member = self.bot.get_user(int(row["discord_id"]))
                    if member:
                        await member.send(
                            f"⏰ **{ev['name']}** starts in 1 hour (<t:{ts}:R>)!\n"
                            "Make sure to be on the server and ready to go. Good luck! 🍀"
                        )
                except Exception:
                    continue
            mark_event_reminded(ev["id"])


async def setup(bot: commands.Bot) -> None:
    for ev in get_scheduled_events():
        if ev.get("scheduled_at"):
            bot.add_view(InterestView(ev["id"]))
    await bot.add_cog(ScheduleCog(bot))