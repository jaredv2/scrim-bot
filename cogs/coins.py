from __future__ import annotations

import datetime as dt
import logging
from typing import Literal

import discord
from config import digits_only, settings
from discord import app_commands
from discord.ext import commands, tasks

from database import (
    add_coin_purchase,
    add_coins,
    approve_invite_reward,
    count_approved_since,
    count_invites_created_since,
    create_invite_reward,
    delete_purchase,
    flag_invite_reward,
    get_coins,
    get_coin_leaderboard,
    get_expired_purchases,
    get_approved_rewards_without_loyalty,
    get_pending_rewards,
    get_reward_by_invited,
    get_rewards_for_review,
    get_reward,
    get_user_message_count,
    has_event_participation,
    increment_user_message,
    mark_loyalty_granted,
    mark_participation_granted,
    mark_reward_left,
    set_reward_status,
    spend_coins,
    update_reward_quality,
)
from embeds import base, error, success, warning

log = logging.getLogger("scrim-bot")

# duration_key -> (price_in_coins, seconds)
PIC_PERM_DURATIONS: dict[str, tuple[int, int]] = {
    "30s": (1, 30),
    "1m": (2, 60),
    "3m": (5, 180),
    "5m": (10, 300),
    "10m": (20, 600),
    "1d": (5000, 86400),
}

PIC_PERMS_ROLE_NAME = "Pic Perms"
MEDIA_LOUNGE_NAME = "📷｜showcase"
MEDIA_LOUNGE_LEGACY_NAMES = ["media-lounge", "media", "showcase"]

# ---------------------------------------------------------------- quality score


def _now_ts() -> int:
    return int(dt.datetime.now(dt.timezone.utc).timestamp())


def compute_quality_score(account_age_days: float, stay_days: float, msg_count: int, participated: bool) -> int:
    """0-100 invite quality score. Thresholds in settings."""
    age_component = min(1.0, account_age_days / max(1, settings.invite_min_account_days))
    stay_component = min(1.0, stay_days / max(1, settings.invite_min_stay_hours / 24))
    msg_component = min(1.0, msg_count / 50)
    part_component = 1.0 if participated else 0.0
    score = (
        0.35 * age_component
        + 0.25 * stay_component
        + 0.25 * msg_component
        + 0.15 * part_component
    ) * 100
    return round(score, 1)


class InviteCoinsCog(commands.Cog):
    """Invite-coin tracking, anti-abuse pipeline, shop, and timed roles."""

    def __init__(self, bot: commands.Bot) -> None:
        self.bot = bot
        self._invites: dict[int, dict[str, int]] = {}
        self.expiry_loop.start()
        self.rewards_loop.start()
        self.loyalty_loop.start()

    def cog_unload(self) -> None:
        self.expiry_loop.cancel()
        self.rewards_loop.cancel()
        self.loyalty_loop.cancel()

    # ------------------------------------------------------------------ roles

    async def _get_or_create_role(self, guild: discord.Guild, name: str, colour: discord.Colour) -> discord.Role | None:
        role_id_cfg = {
            PIC_PERMS_ROLE_NAME: settings.discord_shop_pic_role_id,
        }.get(name)
        if role_id_cfg:
            role = guild.get_role(digits_only_to_int(role_id_cfg))
            if role:
                return role
        existing = discord.utils.get(guild.roles, name=name)
        if existing:
            return existing
        try:
            return await guild.create_role(name=name, colour=colour)
        except discord.Forbidden:
            log.warning("Missing Manage Roles — cannot create %s", name)
            return None

    async def _ensure_media_lounge(self, guild: discord.Guild) -> discord.TextChannel | None:
        """Resolve (or create) the channel where images are Pic-Perms only."""
        cfg = settings.discord_media_lounge_channel_id
        if cfg:
            channel = guild.get_channel(digits_only_to_int(cfg))
            return channel if isinstance(channel, discord.TextChannel) else None
        existing = next(
            (ch for ch in guild.channels if isinstance(ch, discord.TextChannel) and ch.name in MEDIA_LOUNGE_LEGACY_NAMES),
            None,
        )
        if existing:
            return existing
        try:
            return await guild.create_text_channel(MEDIA_LOUNGE_NAME)
        except discord.Forbidden:
            log.warning("Missing Manage Channels — cannot create %s", MEDIA_LOUNGE_NAME)
            return None

    async def _sync_lounge_permissions(self, guild: discord.Guild) -> None:
        """text-only @everyone, images allowed only for Pic Perms holders."""
        lounge = await self._ensure_media_lounge(guild)
        role = await self._pic_perms_role(guild)
        if not lounge or not role:
            return
        try:
            await lounge.set_permissions(
                guild.default_role,
                send_messages=True,
                attach_files=False,
                embed_links=False,
            )
            await lounge.set_permissions(role, attach_files=True, embed_links=True)
        except discord.Forbidden:
            log.warning("Missing Manage Channels — cannot lock %s", MEDIA_LOUNGE_NAME)

    async def _pic_perms_role(self, guild: discord.Guild) -> discord.Role | None:
        return await self._get_or_create_role(guild, PIC_PERMS_ROLE_NAME, discord.Colour.teal())

    # ------------------------------------------------------------- invite math

    async def _cache_invites(self, guild: discord.Guild) -> None:
        try:
            invites = await guild.invites()
        except (discord.Forbidden, discord.HTTPException) as exc:
            log.warning("Can't read invites for guild %s (%s) — need Manage Server permission", guild.id, exc)
            self._invites.pop(guild.id, None)
            return
        self._invites[guild.id] = {inv.code: inv.uses or 0 for inv in invites}

    @commands.Cog.listener()
    async def on_ready(self) -> None:
        for guild in self.bot.guilds:
            await self._cache_invites(guild)
            await self._sync_lounge_permissions(guild)

    @commands.Cog.listener()
    async def on_guild_join(self, guild: discord.Guild) -> None:
        await self._cache_invites(guild)
        await self._sync_lounge_permissions(guild)

    @commands.Cog.listener()
    async def on_message(self, message: discord.Message) -> None:
        if message.guild is None or message.author.bot:
            return
        increment_user_message(str(message.author.id))

    @commands.Cog.listener()
    async def on_member_join(self, member: discord.Member) -> None:
        if member.bot or member.guild is None:
            return
        try:
            invites = await member.guild.invites()
        except (discord.Forbidden, discord.HTTPException):
            self._invites.pop(member.guild.id, None)
            return
        before = self._invites.get(member.guild.id, {})
        inviter_id: int | None = None
        for inv in invites:
            if inv.code in before and (inv.uses or 0) > before[inv.code]:
                inviter_id = inv.inviter.id if inv.inviter else None
                break
        self._invites[member.guild.id] = {i.code: i.uses or 0 for i in invites}
        if inviter_id is None or inviter_id == member.id:
            return  # self-invites count for nothing
        create_invite_reward(
            str(member.guild.id),
            str(inviter_id),
            str(member.id),
            _now_ts(),
        )
        inviter = member.guild.get_member(inviter_id)
        if inviter:
            try:
                stay_text = (
                    f"Your coin is **pending** — it's paid out once they've been here "
                    f"{settings.invite_min_stay_hours}h (account "
                    f"{settings.invite_min_account_days}+ days old). "
                    f"If they leave early the reward is cancelled."
                    if settings.invite_min_stay_hours > 0
                    else (
                        f"Your coin is **on its way** — it pays out once the invite is "
                        f"approved (account {settings.invite_min_account_days}+ days old)."
                    )
                )
                await inviter.send(
                    f"🪙 **{member.display_name}** joined through your invite!\n{stay_text}"
                )
            except (discord.Forbidden, discord.HTTPException):
                pass
        log.info("invite pending: %s invited %s", inviter_id, member.display_name)

    @commands.Cog.listener()
    async def on_member_remove(self, member: discord.Member) -> None:
        reward = get_reward_by_invited(str(member.id))
        if not reward or reward["status"] != "pending" or reward["left_at"] is not None:
            return
        left_at = _now_ts()
        mark_reward_left(reward["id"], left_at)
        if left_at - (reward["created_at"] or left_at) < settings.invite_min_stay_hours * 3600:
            set_reward_status(reward["id"], "rejected", "left_early")
            inviter = member.guild.get_member(int(reward["inviter_id"]))
            if inviter:
                try:
                    await inviter.send(
                        f"🚫 {member.display_name} left the server — invite reward cancelled."
                    )
                except (discord.Forbidden, discord.HTTPException):
                    pass
            log.info("invite reward %s rejected (left early)", reward["id"])

    # ------------------------------------------------------------ expiry cleanup

    @tasks.loop(seconds=60)
    async def expiry_loop(self) -> None:
        now = _now_ts()
        for p in get_expired_purchases(now):
            guild = self.bot.get_guild(int(p["guild_id"])) if p["guild_id"] else None
            if guild:
                member = guild.get_member(int(p["discord_id"]))
                role = guild.get_role(int(p["role_id"]))
                if member and role and role in member.roles:
                    try:
                        await member.remove_roles(role, reason="Coin shop purchase expired")
                    except discord.Forbidden:
                        pass
            delete_purchase(p["id"])

    # ------------------------------------------------------- reward pipeline

    @tasks.loop(seconds=60)
    async def rewards_loop(self) -> None:
        now = _now_ts()
        for reward in get_pending_rewards():
            try:
                await self._process_reward(reward, now)
            except Exception:
                log.exception("reward_processing_failed: %s", reward["id"])

    async def _process_reward(self, reward: dict, now: int) -> None:
        guild = self.bot.get_guild(int(reward["guild_id"])) if reward["guild_id"] else None
        if guild is None:
            return
        member = guild.get_member(int(reward["invited_user_id"]))
        joined_at = reward["created_at"] or now
        stay_seconds = (reward["left_at"] or now) - joined_at

        # Max-age guard — never let a pending row linger forever.
        if now - joined_at > settings.invite_max_pending_days * 86400:
            await self._reject_reward(reward, "pending_too_long")
            return

        # 2) leave too early
        if reward["left_at"] is not None and stay_seconds < settings.invite_min_stay_hours * 3600:
            await self._reject_reward(reward, "left_early")
            return

        # 3) still waiting for the 24h stay.
        if stay_seconds < settings.invite_min_stay_hours * 3600:
            return

        account_age_days = -1.0
        if member is not None:
            account_age_days = max(0.0, (now - int(member.created_at.timestamp())) / 86400)
        elif reward["left_at"] is not None:
            # We can't read the account age of a member who already left;
            # fall back to a conservative 0 (will be rejected by the age gate).
            account_age_days = 0.0

        # Account must be 7+ days old; while still in the server we can wait.
        if member is not None and account_age_days < settings.invite_min_account_days:
            return
        if member is None and account_age_days < settings.invite_min_account_days:
            await self._reject_reward(reward, "account_too_new")
            return

        # 4) rate limits — hold when the inviter's caps are spent.
        day_start = now - (now % 86400)
        week_start = now - 7 * 86400
        if count_approved_since(reward["inviter_id"], day_start) >= settings.invite_daily_limit:
            update_reward_quality(reward["id"], -1, "daily_rate_limit")
            return
        if count_approved_since(reward["inviter_id"], week_start) >= settings.invite_weekly_limit:
            update_reward_quality(reward["id"], -1, "weekly_rate_limit")
            return

        # 5) suspicion — many same-window invites or many fresh accounts.
        if not reward["flagged"]:
            window_joins = count_invites_created_since(
                reward["inviter_id"], now - settings.invite_suspicious_window_hours * 3600
            )
            if window_joins >= settings.invite_suspicious_joins:
                for r in get_pending_rewards():
                    if r["inviter_id"] == reward["inviter_id"] and r["flagged"] == 0:
                        flag_invite_reward(r["id"], "suspicious_volume")
                log.warning("invite flag: inviter %s hit %d joins/%dh", reward["inviter_id"], window_joins, settings.invite_suspicious_window_hours)
                return
        if reward["flagged"]:
            return  # paused until an admin reviews

        # 6) quality gate
        msg_count = get_user_message_count(reward["invited_user_id"])
        part = has_event_participation(reward["invited_user_id"])
        score = compute_quality_score(
            account_age_days,
            stay_seconds / 86400,
            msg_count,
            part,
        )
        update_reward_quality(reward["id"], score)
        approve_bar = settings.invite_score_approve
        if score >= approve_bar:
            await self._approve_reward(reward, participated=part, approved_at=now)
        elif score >= settings.invite_score_review:
            # mid-quality: keep pending; auto-approve after the review window.
            auto_at = joined_at + max(settings.invite_loyalty_days, settings.invite_review_auto_days) * 86400
            if now >= auto_at:
                await self._approve_reward(reward, participated=part, approved_at=now)
            else:
                update_reward_quality(reward["id"], score, "midscore_review")
        else:
            await self._reject_reward(reward, "quality_too_low")

    async def _approve_reward(self, reward: dict, participated: bool, approved_at: int) -> None:
        coins = settings.invite_reward_coins
        part_bonus = settings.invite_participation_bonus if participated else 0
        approve_invite_reward(reward["id"], coins + part_bonus, reward["quality_score"] or 50, approved_at)
        add_coins(reward["inviter_id"], coins + part_bonus)
        inviter = self._inviter(int(reward["guild_id"]), int(reward["inviter_id"]))
        if inviter:
            extra = " (incl. participation bonus)" if part_bonus else ""
            try:
                await inviter.send(
                    f"✅ Invite reward **approved**: +{coins + part_bonus} 🪙 to your balance{extra}. "
                    f"They stay 7 days → +{settings.invite_loyalty_bonus} loyalty bonus."
                )
            except (discord.Forbidden, discord.HTTPException):
                pass
        log.info("invite reward %s approved (+%d coins)", reward["id"], coins + part_bonus)

    async def _reject_reward(self, reward: dict, reason: str) -> None:
        set_reward_status(reward["id"], "rejected", reason)
        if reason != "left_early":  # leave-early already DM'd
            inviter = self._inviter(int(reward["guild_id"]), int(reward["inviter_id"]))
            if inviter:
                try:
                    await inviter.send(f"❌ Invite reward was **cancelled** (reason: {reason}).")
                except (discord.Forbidden, discord.HTTPException):
                    pass
        log.info("invite reward %s rejected (%s)", reward["id"], reason)

    def _inviter(self, guild_id: int, user_id: int) -> discord.Member | None:
        guild = self.bot.get_guild(guild_id)
        return guild.get_member(user_id) if guild else None

    # ------------------------------------------------------- loyalty sweep

    @tasks.loop(minutes=5)
    async def loyalty_loop(self) -> None:
        now = _now_ts()
        for reward in get_approved_rewards_without_loyalty():
            guild = self.bot.get_guild(int(reward["guild_id"])) if reward["guild_id"] else None
            member = guild.get_member(int(reward["invited_user_id"])) if guild else None
            if member is None:
                continue
            # participation bonus if not yet granted and they played an event
            if reward["participation_granted"] == 0 and has_event_participation(reward["invited_user_id"]):
                add_coins(reward["inviter_id"], settings.invite_participation_bonus)
                mark_participation_granted(reward["id"])
                inviter = guild.get_member(int(reward["inviter_id"]))
                if inviter:
                    try:
                        await inviter.send(
                            f"🎮 Participation bonus **+{settings.invite_participation_bonus} 🪙** — your invitee joined an event!"
                        )
                    except (discord.Forbidden, discord.HTTPException):
                        pass
            # loyalty bonus after 7 days in the server
            approved_at = reward["approved_at"] or now
            if now - approved_at >= settings.invite_loyalty_days * 86400:
                if member.joined_at and _now_ts() - int(member.joined_at.timestamp()) >= settings.invite_loyalty_days * 86400:
                    add_coins(reward["inviter_id"], settings.invite_loyalty_bonus)
                    mark_loyalty_granted(reward["id"])
                    inviter = guild.get_member(int(reward["inviter_id"]))
                    if inviter:
                        try:
                            await inviter.send(
                                f"🏡 Loyalty bonus **+{settings.invite_loyalty_bonus} 🪙** — "
                                f"your invitee stayed {settings.invite_loyalty_days} days!"
                            )
                        except (discord.Forbidden, discord.HTTPException):
                            pass

    @loyalty_loop.before_loop
    async def _wait_boot(self) -> None:
        await self.bot.wait_until_ready()

    # ------------------------------------------------------------------ commands

    @commands.hybrid_command(name="invite-coins", description="Check your invite-coin balance.")
    @app_commands.describe(user="Member to check (defaults to you)")
    async def invite_coins(self, ctx: commands.Context, user: discord.Member | None = None) -> None:
        target = user or ctx.author
        balance = get_coins(str(target.id))
        embed = base("🪙 Invite Coins", 0xF1C40F)
        embed.description = (
            f"{target.display_name} has **{balance} coins**.\n"
            "Your invite pays out **1 coin per person** once they've stayed "
            f"**{settings.invite_min_stay_hours}h** on an account that's "
            f"**{settings.invite_min_account_days}+ days old** — invitees that leave early grant nothing.\n"
            "Spend them with `/shop`."
        )
        await ctx.send(embed=embed)

    @commands.hybrid_command(name="shop", description="Browse what you can buy with invite coins.")
    async def shop(self, ctx: commands.Context) -> None:
        embed = base("🛒 Vortex Coin Shop", 0xF1C40F)
        embed.add_field(
            name=f"📸 Pic Perms (#{MEDIA_LOUNGE_NAME})",
            value=(
                "Posting pictures in the showcase channel for a limited time:\n"
                "30s → **1 coin**\n"
                "1m → **2 coins**\n"
                "3m → **5 coins**\n"
                "5m → **10 coins**\n"
                "10m → **20 coins**\n"
                "1d → **5000 coins**\n"
            ),
            inline=False,
        )
        embed.set_footer(text="Earn coins by inviting friends. Buy with /pic-perms.")
        await ctx.send(embed=embed)

    @commands.hybrid_command(name="pic-perms", description="Buy the Pic Perms role for a duration.")
    @app_commands.describe(duration="30s, 1m, 3m, 5m, 10m or 1d")
    async def pic_perms(
        self,
        ctx: commands.Context,
        duration: Literal["30s", "1m", "3m", "5m", "10m", "1d"],
    ) -> None:
        if ctx.guild is None:
            await ctx.send(embed=error("This command only works in a server."))
            return
        price, seconds = PIC_PERM_DURATIONS[duration]
        role = await self._pic_perms_role(ctx.guild)
        if role is None:
            await ctx.send(embed=error("The Pic Perms role isn't set up — check bot permissions."))
            return

        lounge = await self._ensure_media_lounge(ctx.guild)
        if lounge:
            await self._sync_lounge_permissions(ctx.guild)

        if not spend_coins(str(ctx.author.id), price):
            await ctx.send(
                embed=error(f"Not enough coins! **{duration} Pic Perms** costs **{price}** 🪙. Invite friends!")
            )
            return
        await ctx.author.add_roles(role, reason=f"Bought pic perms for {duration}")
        expires = _now_ts() + seconds
        add_coin_purchase(str(ctx.author.id), f"pic-{duration}", str(role.id), expires, str(ctx.guild.id))
        where = f" in #{lounge.name}" if lounge else (" (no #media-lounge channel exists yet — "
                                                      "create one or set DISCORD_MEDIA_LOUNGE_CHANNEL_ID)")
        await ctx.send(
            embed=success(
                f"📸 **Pic Perms** granted for **{duration}** (-{price} 🪙). "
                f"Images allowed {where}. It expires automatically."
            )
        )

    @commands.hybrid_command(name="coin-top", description="See the biggest inviters.")
    async def coin_top(self, ctx: commands.Context) -> None:
        rows = get_coin_leaderboard(10)
        if not rows:
            await ctx.send(embed=warning("Nobody has coins yet — get inviting!"))
            return
        lines = []
        for place, row in enumerate(rows, 1):
            member = ctx.guild.get_member(int(row["discord_id"])) if ctx.guild else None
            name = member.display_name if member else f"<@{row['discord_id']}>"
            lines.append(f"**{place}.** {name} — {row['coins']} 🪙 ({row['total_invites']} invites)")
        embed = base("🏆 Coin Leaderboard", 0xF1C40F)
        embed.description = "\n".join(lines)
        await ctx.send(embed=embed)

    @commands.hybrid_command(name="invite-info", description="Find your invite link so joins credit you.")
    async def invite_info(self, ctx: commands.Context) -> None:
        if ctx.guild is None:
            await ctx.send(embed=error("Only works inside the server."))
            return
        try:
            invites = await ctx.guild.invites()
        except discord.Forbidden:
            await ctx.send(embed=error("Bot needs the **Manage Server** permission to see invites."))
            return
        mine = [i for i in invites if i.inviter and i.inviter.id == ctx.author.id]
        if not mine:
            await ctx.send(
                embed=warning(
                    "You don't have an invite link yet. Create one in the server's "
                    "member menu (Invite People) and future joins will credit you."
                )
            )
            return
        lines = [f"`{i.url}` — {i.uses or 0} use(s)" for i in mine]
        embed = base("🔗 Your invites", 0xF1C40F)
        embed.description = "\n".join(lines) + (
            "\n\nEach new person who joins through these starts a **pending reward** "
            f"(pays out after they stay {settings.invite_min_stay_hours}h on a "
            f"{settings.invite_min_account_days}+ day old account)."
        )
        await ctx.send(embed=embed)

    # ------------------------------------------------------------ admin review

    async def _check_admin(self, ctx: commands.Context) -> bool:
        if not ctx.author.guild_permissions.administrator:
            await ctx.send(embed=error("You need administrator permissions."))
            return False
        return True

    @commands.hybrid_command(name="invite-review", description="ADMIN: list pending/suspicious invite rewards.")
    async def invite_review(self, ctx: commands.Context) -> None:
        if not await self._check_admin(ctx):
            return
        rows = get_rewards_for_review()
        if not rows:
            await ctx.send(embed=warning("No pending or flagged rewards."))
            return
        lines = []
        for r in rows:
            inviter = ctx.guild.get_member(int(r["inviter_id"]))
            invited = ctx.guild.get_member(int(r["invited_user_id"]))
            name_i = inviter.display_name if inviter else f"<@{r['inviter_id']}>"
            name_u = invited.display_name if invited else f"<@{r['invited_user_id']}>"
            flag = " ⚠️" if r["flagged"] else ""
            score = r["quality_score"] if r["quality_score"] is not None else "?"
            reason = f" ({r['reason']})" if r["reason"] else ""
            lines.append(
                f"`#{r['id']}` {name_i} → {name_u} — score {score}{flag}{reason}"
            )
        embed = base("🔎 Invite Reward Queue", 0xF1C40F)
        embed.description = "\n".join(lines)
        embed.set_footer(text="Approve: ;invite-approve <id> · Reject: ;invite-reject <id>")
        await ctx.send(embed=embed)

    @commands.hybrid_command(name="invite-approve", description="ADMIN: force-approve a pending reward.")
    @app_commands.describe(reward_id="Reward ID from ;invite-review")
    async def invite_approve(self, ctx: commands.Context, reward_id: int) -> None:
        if not await self._check_admin(ctx):
            return
        reward = get_reward(reward_id)
        if not reward:
            await ctx.send(embed=error(f"Reward #{reward_id} not found."))
        elif reward["status"] != "pending":
            await ctx.send(embed=error(f"Reward #{reward_id} is already **{reward['status']}**."))
        else:
            await self._approve_manual(reward)
            await ctx.send(embed=success(f"Reward #{reward_id} approved."))

    async def _approve_manual(self, reward: dict) -> None:
        participated = has_event_participation(reward["invited_user_id"])
        await self._approve_reward(reward, participated, _now_ts())

    @commands.hybrid_command(name="invite-reject", description="ADMIN: reject a pending reward.")
    @app_commands.describe(reward_id="Reward ID from ;invite-review", reason="Why it was rejected")
    async def invite_reject(self, ctx: commands.Context, reward_id: int, reason: str = "admin") -> None:
        if not await self._check_admin(ctx):
            return
        reward = get_reward(reward_id)
        if not reward:
            await ctx.send(embed=error(f"Reward #{reward_id} not found."))
        elif reward["status"] != "pending":
            await ctx.send(embed=error(f"Reward #{reward_id} is already **{reward['status']}**."))
        else:
            await self._reject_reward(reward, reason)
            await ctx.send(embed=success(f"Reward #{reward_id} rejected ({reason})."))


def digits_only_to_int(value: str | int | None) -> int | None:
    if not value:
        return None
    cleaned = "".join(ch for ch in str(value) if ch.isdigit())
    return int(cleaned) if cleaned else None


async def setup(bot: commands.Bot) -> None:
    await bot.add_cog(InviteCoinsCog(bot))