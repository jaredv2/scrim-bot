from __future__ import annotations

import asyncio
import json
import logging
import time

import discord
from config import digits_only
from discord import app_commands
from discord.ext import commands, tasks
from embeds import base, error, success

from database import (
    create_duel_ask,
    get_duel_ask,
    get_pending_duel_asks,
    get_stale_duel_asks,
    set_duel_ask_channels,
    set_duel_ask_status,
)

logger = logging.getLogger("scrim-bot")

DUEL_CATEGORY_NAME = "Duels"


def _duel_participants(ask: dict) -> list[str]:
    """All people involved in a duel ask (asker + partner + targets)."""
    ids = [ask["asker_id"]]
    if ask.get("partner_id"):
        ids.append(ask["partner_id"])
    try:
        targets = json.loads(ask["target_ids"] or "[]")
    except (json.JSONDecodeError, TypeError):
        targets = []
    ids.extend(str(t) for t in targets)
    return ids


def _team_labels(ask: dict) -> str:
    return "2v2" if ask.get("partner_id") else "1v1"


def _display_name(guild: discord.Guild, discord_id: str) -> str:
    member = guild.get_member(int(discord_id))
    return member.display_name if member else f"<@{discord_id}>"


def _deactivate(view: discord.ui.View) -> None:
    for item in view.children:
        item.disabled = True


def build_duel_embed(
    guild: discord.Guild,
    ask: dict,
    accepted: set[str] | None = None,
    status: str | None = None,
) -> discord.Embed:
    accepted = accepted or set()
    status = status or ask["status"]
    ids = _duel_participants(ask)
    names = " vs ".join(_display_name(guild, did) for did in ids)
    embed = base(f"⚔️ Duel Ask — {_team_labels(ask)}", 0xE74C3C)
    embed.description = names
    embed.add_field(name="Status", value=status.title())

    if status == "pending":
        waiting = [did for did in ids if did not in accepted]
        embed.add_field(
            name="Waiting on",
            value="\n".join(_display_name(guild, did) for did in waiting)
            or "Everyone accepted!",
            inline=False,
        )
        embed.add_field(
            name="Accepted",
            value="\n".join(_display_name(guild, did) for did in ids if did in accepted)
            or "—",
            inline=False,
        )
    return embed


class DuelAskView(discord.ui.View):
    """Accept/Decline/Cancel buttons for a pending duel ask (persistent custom ids).

    For accepted asks an End button is attached (custom_id `duel_end_<id>`).
    """

    def __init__(self, ask_id: int, accepted: set[str] | None = None) -> None:
        super().__init__(timeout=None)
        self.ask_id = ask_id
        self.accepted: set[str] = set(accepted or [])

        ask = get_duel_ask(ask_id)
        self.status = ask["status"] if ask else "missing"

        accept_btn = discord.ui.Button(
            label="Accept",
            emoji="✅",
            style=discord.ButtonStyle.success,
            custom_id=f"duel_accept_{ask_id}",
        )
        accept_btn.callback = self.accept

        decline_btn = discord.ui.Button(
            label="Decline",
            emoji="❌",
            style=discord.ButtonStyle.secondary,
            custom_id=f"duel_decline_{ask_id}",
        )
        decline_btn.callback = self.decline

        cancel_btn = discord.ui.Button(
            label="Cancel",
            emoji="🚫",
            style=discord.ButtonStyle.danger,
            custom_id=f"duel_cancel_{ask_id}",
        )
        cancel_btn.callback = self.cancel

        self.add_item(accept_btn)
        self.add_item(decline_btn)
        self.add_item(cancel_btn)

        if self.status == "accepted":
            self._add_end_button()

    def _add_end_button(self) -> None:
        end_btn = discord.ui.Button(
            label="End Duel",
            emoji="🏁",
            style=discord.ButtonStyle.danger,
            custom_id=f"duel_end_{self.ask_id}",
        )
        end_btn.callback = self.end_duel
        self.add_item(end_btn)

    @staticmethod
    def _is_participant(ask: dict, user_id: str) -> bool:
        return user_id in _duel_participants(ask)

    async def _refresh(self, interaction: discord.Interaction) -> None:
        ask = get_duel_ask(self.ask_id)
        if not ask:
            return
        embed = build_duel_embed(interaction.guild, ask, self.accepted, status=self.status)
        try:
            await interaction.message.edit(embed=embed, view=self)
        except discord.HTTPException:
            pass

    async def accept(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        ask = get_duel_ask(self.ask_id)
        if not ask or ask["status"] != "pending":
            await interaction.response.send_message(
                embed=error("This duel ask is no longer pending."), ephemeral=True
            )
            return
        if not self._is_participant(ask, str(interaction.user.id)):
            await interaction.response.send_message(
                embed=error("You're not part of this duel ask."), ephemeral=True
            )
            return

        self.accepted.add(str(interaction.user.id))
        await interaction.response.defer(ephemeral=True)

        required = set(_duel_participants(ask))
        if self.accepted >= required:
            await self._finalize(interaction)
            return

        await self._refresh(interaction)
        await interaction.followup.send(
            embed=success(f"Accepted! ({len(self.accepted)}/{len(required)} accepted)"),
            ephemeral=True,
        )

    async def _finalize(self, interaction: discord.Interaction) -> None:
        """All participants accepted — create the Duels channels."""
        ask = get_duel_ask(self.ask_id)
        if not ask:
            return
        guild = interaction.guild
        if not guild:
            return

        set_duel_ask_status(self.ask_id, "accepted")
        self.status = "accepted"

        category = discord.utils.get(guild.categories, name=DUEL_CATEGORY_NAME)
        if not category:
            try:
                category = await guild.create_category(
                    DUEL_CATEGORY_NAME,
                    overwrites={
                        guild.default_role: discord.PermissionOverwrite(
                            connect=False, read_messages=False
                        ),
                        guild.me: discord.PermissionOverwrite(
                            connect=True, read_messages=True
                        ),
                    },
                    reason="Auto-created Duels category",
                )
            except discord.HTTPException:
                category = None

        overwrites = {}
        for did in _duel_participants(ask):
            member = guild.get_member(int(did))
            if member:
                overwrites[member] = discord.PermissionOverwrite(
                    read_messages=True, connect=True
                )

        text_channel = None
        voice_channel = None
        try:
            text_channel = await guild.create_text_channel(
                name=f"duel-{self.ask_id}",
                category=category,
                overwrites=overwrites,
                reason=f"Duel ask {self.ask_id}",
            )
            voice_channel = await guild.create_voice_channel(
                name=f"duel-{self.ask_id}",
                category=category,
                overwrites=overwrites,
                reason=f"Duel ask {self.ask_id}",
            )
        except discord.HTTPException as exc:
            logger.warning("duel channel creation failed: %s", exc)

        if text_channel and voice_channel:
            set_duel_ask_channels(
                self.ask_id,
                str(text_channel.id),
                str(voice_channel.id),
                str(category.id) if category else "",
            )

            end_view = DuelEndView(self.ask_id)
            try:
                await text_channel.send(
                    embed=build_duel_embed(guild, ask, self.accepted, status="accepted"),
                    view=end_view,
                )
            except discord.HTTPException:
                pass

            _deactivate(self)
            await self._refresh(interaction)
            await interaction.followup.send(
                embed=success(
                    f"Duel created — channels ready in {DUEL_CATEGORY_NAME}!"
                ),
                ephemeral=True,
            )
        else:
            set_duel_ask_status(self.ask_id, "failed")
            self.status = "failed"
            await interaction.followup.send(
                embed=error("Couldn't create the duel channels."), ephemeral=True
            )

    async def decline(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        ask = get_duel_ask(self.ask_id)
        if not ask or ask["status"] != "pending":
            await interaction.response.send_message(
                embed=error("This duel ask is no longer pending."), ephemeral=True
            )
            return
        if not self._is_participant(ask, str(interaction.user.id)):
            await interaction.response.send_message(
                embed=error("You're not part of this duel ask."), ephemeral=True
            )
            return
        set_duel_ask_status(self.ask_id, "declined")
        self.status = "declined"
        _deactivate(self)
        await self._refresh(interaction)
        await interaction.response.send_message(
            embed=success("Duel declined."), ephemeral=True
        )

    async def cancel(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        ask = get_duel_ask(self.ask_id)
        if not ask:
            return
        if str(interaction.user.id) != ask["asker_id"]:
            await interaction.response.send_message(
                embed=error("Only the person who asked can cancel."), ephemeral=True
            )
            return
        set_duel_ask_status(self.ask_id, "cancelled")
        self.status = "cancelled"
        _deactivate(self)
        await self._refresh(interaction)
        await interaction.response.send_message(
            embed=success("Duel ask cancelled."), ephemeral=True
        )

    async def end_duel(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        await _end_duel(self, interaction)


class DuelEndView(discord.ui.View):
    """Lives on the duel text channel message; deletes the channels when pressed."""

    def __init__(self, ask_id: int) -> None:
        super().__init__(timeout=None)
        self.ask_id = ask_id
        end_btn = discord.ui.Button(
            label="End Duel",
            emoji="🏁",
            style=discord.ButtonStyle.danger,
            custom_id=f"duel_end_{ask_id}",
        )
        end_btn.callback = self.end_duel
        self.add_item(end_btn)

    async def end_duel(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        await _end_duel(self, interaction)


async def _end_duel(view: discord.ui.View, interaction: discord.Interaction) -> None:
    ask = get_duel_ask(view.ask_id)
    if not ask:
        return
    await interaction.response.defer(ephemeral=True)
    set_duel_ask_status(view.ask_id, "ended")
    for key in ("text_channel_id", "voice_channel_id"):
        cid = digits_only(ask.get(key) or "")
        if not cid:
            continue
        channel = interaction.guild.get_channel(int(cid)) if interaction.guild else None
        if channel:
            try:
                await channel.delete(reason=f"Duel {view.ask_id} ended")
            except discord.HTTPException:
                pass
    _deactivate(view)
    try:
        await interaction.message.edit(view=view)
    except discord.HTTPException:
        pass
    await interaction.followup.send(
        embed=success("Duel ended, channels cleaned up."), ephemeral=True
    )


class DuelsCog(commands.Cog):
    """1v1 / 2v2 duel asks with accept flow and auto channels."""

    def __init__(self, bot: commands.Bot) -> None:
        self.bot = bot
        self.ttl_loop.start()

    def cog_unload(self) -> None:
        self.ttl_loop.cancel()

    @tasks.loop(seconds=60)
    async def ttl_loop(self) -> None:
        try:
            stale = await asyncio.to_thread(get_stale_duel_asks)
        except Exception:
            logger.exception("duel ttl cleanup failed")
            return
        if not stale or not self.bot.guilds:
            return
        guild = self.bot.guilds[0]
        for ask in stale:
            for key in ("text_channel_id", "voice_channel_id"):
                cid = digits_only(ask.get(key) or "")
                if not cid:
                    continue
                channel = guild.get_channel(int(cid))
                if channel:
                    try:
                        await channel.delete(reason=f"Duel {ask['id']} expired")
                    except discord.HTTPException:
                        pass
            logger.info("duel ask %s expired and cleaned up", ask["id"])

    async def _ask(
        self,
        ctx: commands.Context,
        partner: discord.Member | None,
        opponents: list[discord.Member],
    ) -> None:
        if not ctx.guild:
            await ctx.send(embed=error("Server only."))
            return
        all_members = [ctx.author] + ([partner] if partner else []) + opponents
        ids = {str(m.id) for m in all_members}
        if len(ids) != len(all_members):
            await ctx.send(embed=error("Duplicate players in the duel ask."))
            return

        ask_id = create_duel_ask(
            str(ctx.author.id),
            str(partner.id) if partner else None,
            [str(o.id) for o in opponents],
        )
        ask = get_duel_ask(ask_id)
        view = DuelAskView(ask_id)
        embed = build_duel_embed(ctx.guild, ask, set(), status="pending")
        await ctx.send(embed=embed, view=view)

    @commands.hybrid_command(
        name="ask-1v1",
        description="Challenge someone to a 1v1 duel",
    )
    @app_commands.describe(opponent="Your opponent")
    async def ask_1v1(self, ctx: commands.Context, opponent: discord.Member) -> None:
        if opponent.bot:
            await ctx.send(embed=error("Can't duel a bot."))
            return
        await self._ask(ctx, None, [opponent])

    @commands.hybrid_command(
        name="ask-2v2",
        description="Challenge a duo: pick your partner and two opponents",
    )
    @app_commands.describe(
        partner="Your partner",
        opponent1="Opponent 1",
        opponent2="Opponent 2",
    )
    async def ask_2v2(
        self,
        ctx: commands.Context,
        partner: discord.Member,
        opponent1: discord.Member,
        opponent2: discord.Member,
    ) -> None:
        if partner.bot or opponent1.bot or opponent2.bot:
            await ctx.send(embed=error("Can't duel a bot."))
            return
        await self._ask(ctx, partner, [opponent1, opponent2])


async def setup(bot: commands.Bot) -> None:
    await bot.add_cog(DuelsCog(bot))
    for ask in get_pending_duel_asks():
        try:
            bot.add_view(DuelAskView(ask["id"]))
        except Exception:
            logger.exception("failed to register duel view %s", ask["id"])
