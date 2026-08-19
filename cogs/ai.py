from __future__ import annotations

import logging
import re
import time
from typing import Literal

import discord
from discord import app_commands
from discord.ext import commands

from config import settings
from database import get_kv, query_one, set_kv
from embeds import base, error, success

log = logging.getLogger("scrim-bot")

AI_TOGGLE_KEY = "ai_enabled"

SYSTEM_PROMPT = (
    "You are Vortex, the mascot bot of a Builnow.gg scrim and competitive (PR) community Discord server. "
    "You talk like a hype, chill Discord teammate — short, playful, a little cocky, PG-13, and full of "
    "gamer energy. You know the server's systems: PR (Power Rating) and the rank ladder, scrim events and "
    "cups, divisions, qualifiers, duels, brackets, and the coin shop (earn coins by inviting friends, winning "
    "events, or participating; spend them on Pic Perms, custom color roles, and VIP). chris is daddy and the vortex team runs"
    "everything — everyone knows it, you respect him and them, and you play along with the banter around it. "
    "Never pretend you ran a command: if someone needs something done, tell them the real command "
    "(like ;create-event, ;start-event, /stats, /rank, /leaderboard, /shop, /coin-top) and keep it to one or "
    "two sentences. Don't explain yourself at length. Use emotes sparingly (🏆🪙🔫). Never give advice on "
    "cheating, exploits, or harassment, and keep it clean."
)

_MENTION_RE = re.compile(r"<@!?&?\d+>")

_SHORT_WORDS = {"ok", "okay", "lol", "lmao", "nice", "yes", "no", "hi", "hey", "yo", "hmm", "xd", "ffs", "gg", "yikes"}


def _key_looks_valid() -> bool:
    key = (settings.groq_api_key or "").strip()
    return key.startswith("gsk_") and len(key) >= 20


class AICog(commands.Cog):
    """Mention-or-reply AI chat powered by Groq."""

    def __init__(self, bot: commands.Bot) -> None:
        self.bot = bot
        self._groq_client = None
        self._cooldowns: dict[int, float] = {}
        self._blocked_words: set[str] | None = None

    # ------------------------------------------------------------- helpers

    def _client(self):
        if self._groq_client is None and _key_looks_valid():
            from groq import AsyncGroq

            self._groq_client = AsyncGroq(api_key=settings.groq_api_key)
        return self._groq_client

    def _is_enabled(self) -> bool:
        if not _key_looks_valid():
            return False
        return get_kv(AI_TOGGLE_KEY, "1") != "0"

    def _blocked(self, content: str) -> bool:
        if self._blocked_words is None:
            self._blocked_words = {
                w.strip().lower()
                for w in settings.ai_blocked_words.split(",")
                if w.strip()
            }
        if not self._blocked_words:
            return False
        lower = content.lower()
        return any(w in lower for w in self._blocked_words)

    def _strip_mention(self, content: str) -> str:
        return _MENTION_RE.sub("", content).strip()

    def _is_short_filler(self, content: str) -> bool:
        tokens = content.split()
        if not tokens:
            return True
        if len(tokens) == 1 and len(content) <= 8:
            return True
        if len(tokens) <= 2 and content.lower() in _SHORT_WORDS:
            return True
        return False

    def _is_registration_prompt(self, message: discord.Message) -> bool:
        ref_id = message.reference.message_id if message.reference else None
        if not ref_id:
            return False
        row = query_one(
            "SELECT 1 FROM vtx_pending_registrations WHERE prompt_message_id = %s",
            (str(ref_id),),
        )
        return row is not None

    async def _resolve_reply(self, message: discord.Message) -> discord.Message | None:
        if not message.reference or not message.reference.message_id:
            return None
        ref = message.reference.resolved
        if isinstance(ref, discord.Message):
            return ref
        try:
            return await message.channel.fetch_message(message.reference.message_id)
        except Exception:
            return None

    # ------------------------------------------------------------- listener

    @commands.Cog.listener()
    async def on_message(self, message: discord.Message) -> None:
        if message.author.bot or not message.guild:
            return
        if not self._is_enabled():
            return
        if self._client() is None:
            return

        content = message.content.strip()
        if not content or content.startswith(";"):
            return

        mentioned = self.bot.user in message.mentions
        replying_to_bot = False
        ref_msg = await self._resolve_reply(message)
        if ref_msg is not None and ref_msg.author.id == self.bot.user.id:
            replying_to_bot = True

        if not (mentioned or replying_to_bot):
            return

        clean = self._strip_mention(content) if mentioned else content
        if self._is_short_filler(clean):
            return

        if getattr(message.channel, "is_nsfw", lambda: False)():
            return
        if self._blocked(clean):
            return
        if replying_to_bot and self._is_registration_prompt(message):
            return

        now = time.monotonic()
        if now - self._cooldowns.get(message.author.id, 0) < settings.ai_cooldown_seconds:
            return
        self._cooldowns[message.author.id] = now

        await self._reply(message, clean)

    async def _reply(self, message: discord.Message, content: str) -> None:
        client = self._client()
        messages = await self._build_messages(message, content)
        try:
            async with message.channel.typing():
                resp = await client.chat.completions.create(
                    model=settings.ai_model,
                    messages=messages,
                    max_tokens=settings.ai_max_tokens,
                )
        except Exception as exc:
            log.warning("ai_request_failed: %s", exc)
            await message.reply("🤖 My brain glitched — try that again.")
            return
        text = (resp.choices[0].message.content or "").strip()[:2000]
        if not text:
            await message.reply("🤖 ...")
            return
        await message.reply(text)

    async def _build_messages(self, message: discord.Message, content: str) -> list[dict]:
        messages = [{"role": "system", "content": SYSTEM_PROMPT}]
        try:
            history = [
                msg
                async for msg in message.channel.history(
                    limit=settings.ai_context_messages + 1, before=message
                )
            ]
        except Exception:
            history = []
        budget = settings.ai_context_chars
        used = 0
        for m in reversed(history):
            if m.author.bot:
                role, body = "assistant", (m.content or "")
            else:
                role, body = "user", f"{m.author.display_name}: {m.content}"
            body = body.strip()
            if not body:
                continue
            if len(body) > 300:
                body = body[:300] + "…"
            if used + len(body) > budget:
                break
            messages.append({"role": role, "content": body})
            used += len(body)
        messages.append({"role": "user", "content": content})
        return messages

    # ------------------------------------------------------------- admin toggle

    async def _check_admin(self, ctx: commands.Context) -> bool:
        if not ctx.author.guild_permissions.administrator:
            await ctx.send(embed=error("You need administrator permissions."))
            return False
        return True

    @commands.hybrid_command(name="ai", description="ADMIN: toggle the AI chat (on/off/status).")
    @app_commands.describe(action="on, off or status")
    async def ai(self, ctx: commands.Context, action: Literal["on", "off", "status"]) -> None:
        if not await self._check_admin(ctx):
            return
        if action == "on":
            set_kv(AI_TOGGLE_KEY, "1")
            await ctx.send(embed=success("🤖 AI chat enabled."))
        elif action == "off":
            set_kv(AI_TOGGLE_KEY, "0")
            await ctx.send(embed=success("🤖 AI chat disabled."))
        else:
            key_set = bool(settings.groq_api_key)
            toggle = get_kv(AI_TOGGLE_KEY, "1") != "0"
            state = "enabled" if (key_set and toggle) else "disabled"
            reason = ""
            if not key_set:
                reason = " — set GROQ_API_KEY in .env"
            embed = base("🤖 AI status", 0xF1C40F)
            embed.description = f"AI chat is **{state}**{reason}. Model: `{settings.ai_model}`."
            await ctx.send(embed=embed)


async def setup(bot: commands.Bot) -> None:
    await bot.add_cog(AICog(bot))