from __future__ import annotations

import asyncio
import logging
import time

import os

import aiohttp
import discord
from discord.ext import commands, tasks

from config import settings

logger = logging.getLogger("scrim-bot.health")


def _effective_port() -> int:
    # Runtime check so $PORT injected by PaaS after import is respected
    for key in ("PORT", "DASHBOARD_PORT"):
        val = os.getenv(key)
        if val and val.strip().isdigit():
            return int(val.strip())
    return int(settings.dashboard_port)


def _resolve_health_url() -> str:
    raw = (settings.api_health_url or "").strip()
    port = _effective_port()
    if raw:
        if raw.startswith("/"):
            return f"http://127.0.0.1:{port}{raw}"
        if raw.startswith("http://") or raw.startswith("https://"):
            return raw
        # bare host:port/path without scheme
        if raw.startswith("localhost") or raw.startswith("127.0.0.1"):
            return f"http://{raw}"
        return raw
    return f"http://127.0.0.1:{port}/health"


class HealthPingCog(commands.Cog):
    """Silent health pinger.

    - Background cron hits ``/health`` every ``health_ping_interval_seconds``.
    - Every bot command (prefix + slash/hybrid) silently hits ``/health`` in
      a fire-and-forget task so user-visible behaviour is never affected.
    """

    def __init__(self, bot: commands.Bot) -> None:
        self.bot = bot
        self._session: aiohttp.ClientSession | None = None
        self._last_schedule_ts: float = 0.0
        # honor enabled flag — tasks loop will no-op if disabled
        interval = max(30, int(settings.health_ping_interval_seconds or 300))
        # set loop interval before starting
        try:
            self.health_cron.change_interval(seconds=interval)
        except Exception:
            pass
        self.health_cron.start()
        logger.info(
            "health_ping_cog_loaded enabled=%s interval=%ss url=%s",
            settings.health_ping_enabled,
            interval,
            _resolve_health_url(),
        )

    def cog_unload(self) -> None:
        self.health_cron.cancel()
        if self._session and not self._session.closed:
            # schedule close without blocking unload
            try:
                asyncio.create_task(self._session.close())
            except RuntimeError:
                pass

    async def _get_session(self) -> aiohttp.ClientSession:
        if self._session is None or self._session.closed:
            self._session = aiohttp.ClientSession()
        return self._session

    async def _ping(self) -> None:
        """Silently GET /health. Never raises, never logs at warning level."""
        if not settings.health_ping_enabled:
            return
        url = _resolve_health_url()
        timeout = aiohttp.ClientTimeout(total=max(2, int(settings.health_ping_timeout_seconds or 5)))
        try:
            session = await self._get_session()
            async with session.get(url, timeout=timeout) as resp:
                # consume body so connection can be reused; ignore content
                try:
                    await resp.read()
                except Exception:
                    pass
                logger.debug("health_ping ok status=%s url=%s", resp.status, url)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            # silent — debug only so we don't spam logs if API is sleeping
            logger.debug("health_ping failed url=%s err=%s", url, exc)

    def _schedule(self) -> None:
        """Fire-and-forget wrapper; never blocks the command.

        Deduplicates: a slash command fires both ``on_interaction`` and
        ``on_app_command_completion`` ~ms apart — skip the second.
        """
        if not settings.health_ping_enabled:
            return
        now = time.monotonic()
        if now - self._last_schedule_ts < 1.0:
            return
        self._last_schedule_ts = now
        try:
            # create_task is safe even inside a running command/hook
            asyncio.create_task(self._ping())
        except RuntimeError:
            # no running loop (e.g. during startup/cog load) — ignore
            pass

    # ---- Background cron ----

    @tasks.loop(seconds=300)
    async def health_cron(self) -> None:
        await self._ping()

    @health_cron.before_loop
    async def before_health_cron(self) -> None:
        await self.bot.wait_until_ready()

    # ---- Per-command silent ping ----

    # Prefix / hybrid commands via commands framework
    @commands.Cog.listener()
    async def on_command(self, ctx: commands.Context) -> None:
        self._schedule()

    # Slash / hybrid commands via app_commands (Interaction-based)
    @commands.Cog.listener()
    async def on_interaction(self, interaction: discord.Interaction) -> None:
        try:
            if interaction.type == discord.InteractionType.application_command:
                self._schedule()
        except Exception:
            pass

    # Some discord.py versions dispatch app_command_completion — cover it too
    @commands.Cog.listener()
    async def on_app_command_completion(
        self, interaction: discord.Interaction, command
    ) -> None:
        self._schedule()


async def setup(bot: commands.Bot) -> None:
    await bot.add_cog(HealthPingCog(bot))
