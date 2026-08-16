from __future__ import annotations

import re

import discord
from discord import ui
from discord.ext import commands

from database import (
    count_event_players,
    execute,
    get_event,
    get_event_registrations,
    is_player_banned,
    query_one,
    upsert_player,
)

SIGNUP_SKIN_MAX = 64

TEAM_LABELS = {1: "Solo", 2: "Duo", 3: "Trio", 4: "Squad"}


def team_label(team_size: int) -> str:
    return TEAM_LABELS.get(team_size, f"{team_size}-player")


def team_signup_format(team_size: int) -> str:
    """Example signup message: `@you @teammate1 ... SkinName` (you first)."""
    need = team_size - 1
    if need <= 0:
        return ""
    if need == 1:
        others = "@teammate"
    else:
        others = " ".join(f"@teammate{i}" for i in range(1, need + 1))
    return f"@you {others} SkinName"


_SIGNUP_SEP = r"(?:\s*[xX|,;&/+.\-]\s*|\s+)"


def parse_signup(content: str, count: int) -> tuple[list[str], str] | None:
    """Parse a strict signup message.

    Contract: the whole message must be exactly ``count`` mentions — you
    first, then your teammates — separated by whitespace or a separator
    (x, X, |, ,, &, /, +, -, .), followed by the skin, e.g.
    `@you x @teammate FBI Skin`. Returns (discord ids, skin) or None when the
    message shape doesn't match (casual chat is ignored).
    """
    pattern = _SIGNUP_SEP.join([r"<@!?(\d+)>"] * count) + r"\s+(\w.*)"
    m = re.fullmatch(pattern, content)
    if not m:
        return None
    ids = [m.group(i) for i in range(1, count + 1)]
    skin = m.group(count + 1).strip()
    return ids, skin


def register_team(ev: dict, members: list[tuple[str, str]], skin: str) -> dict:
    """Validate and persist a team signup.

    members is the full team in order — the first entry is the leader — as
    (discord_id, display_name) tuples. Mentioning yourself repeatedly (e.g.
    `@you x @you SkinName`) collapses into a solo registration. Returns a
    result dict with ``ok`` plus either ``code`` (error) or ``text``
    (confirmation).
    """
    team_size = ev.get("team_size", 1)
    team_ids = [tid for tid, _ in members]
    names_by_id = dict(members)

    if len(team_ids) != team_size:
        return {"ok": False, "code": "WRONG_MEMBER_COUNT", "need": team_size, "got": len(team_ids)}

    if len(set(team_ids)) != len(team_ids):
        if len(set(team_ids)) == 1:
            members = [(team_ids[0], names_by_id[team_ids[0]])]
            team_ids = [team_ids[0]]
        else:
            return {"ok": False, "code": "DUPLICATE"}

    for did in team_ids:
        if is_player_banned(did):
            return {"ok": False, "code": "BANNED"}

    max_players = ev.get("max_players") or 0
    if max_players > 0 and count_event_players(ev["id"]) + len(team_ids) > max_players:
        return {"ok": False, "code": "FULL"}

    team_ids_check = set(team_ids)
    for reg in get_event_registrations(ev["id"]):
        reg_ids = {reg["discord_id"]}
        if reg.get("team_members"):
            reg_ids.update(reg["team_members"].split(","))
        overlap = team_ids_check & reg_ids
        if overlap:
            overlap_names = [names_by_id[did] for did in team_ids if did in overlap]
            return {"ok": False, "code": "ALREADY_REGISTERED", "overlap": sorted(overlap_names)}

    skin = (skin or "").strip()
    if len(team_ids) > 1 and not skin:
        return {"ok": False, "code": "SKIN_REQUIRED"}
    skin = skin[:SIGNUP_SKIN_MAX]

    for did, uname in members:
        upsert_player(did, uname)

    lead_id, lead_name = members[0]
    team_json = ",".join(team_ids[1:]) if len(team_ids) > 1 else None
    execute(
        "INSERT INTO registrations "
        "(event_id, discord_id, username, team_members, skin, status) "
        "VALUES (?, ?, ?, ?, ?, 'confirmed')",
        (ev["id"], lead_id, lead_name, team_json, skin),
    )

    igns = []
    for did in team_ids:
        p = query_one("SELECT game_username FROM players WHERE discord_id = ?", (did,))
        igns.append(p["game_username"] if p and p["game_username"] else None)

    team_parts = []
    for did, ign in zip(team_ids, igns):
        team_parts.append(f"<@{did}> ({ign})" if ign else f"<@{did}>")

    regs = get_event_registrations(ev["id"])
    total_players = 0
    for r in regs:
        total_players += 1
        if r.get("team_members"):
            total_players += len(r["team_members"].split(","))

    skin_part = f" | Skin: {skin}" if skin else ""
    return {
        "ok": True,
        "code": "OK",
        "text": (
            f"✅ {' + '.join(team_parts)} registered for **{ev['name']}** "
            f"({total_players} player{'s' if total_players != 1 else ''}){skin_part}"
        ),
    }


class IGNModal(ui.Modal, title="In-Game Name"):
    ign = ui.TextInput(
        label="Enter your in-game name",
        placeholder="e.g. PlayerOne",
        required=True,
        max_length=32,
    )

    def __init__(self, event_id: int, team_size: int = 1) -> None:
        super().__init__()
        self.event_id = event_id
        self.team_size = team_size
        self.result: str | None = None
        self.skin_result: str | None = None
        self._skin_input: ui.TextInput | None = None
        if team_size >= 2:
            self._skin_input = ui.TextInput(
                label="Team skin/outfit",
                placeholder="e.g. FBI Skin",
                required=True,
                max_length=64,
            )
            self.add_item(self._skin_input)

    async def on_submit(self, interaction: discord.Interaction) -> None:
        self.result = self.ign.value.strip()
        if self.team_size >= 2 and self._skin_input:
            self.skin_result = self._skin_input.value.strip()
        discord_id = str(interaction.user.id)
        execute(
            "UPDATE players SET game_username = ? WHERE discord_id = ?",
            (self.result, discord_id),
        )
        await interaction.response.defer()


class RegisterView(ui.View):
    def __init__(self, event_id: int, team_size: int, qualification_enabled: bool = False) -> None:
        super().__init__(timeout=None)
        self.event_id = event_id
        self.team_size = team_size
        self.qualification_enabled = qualification_enabled

    def _can_register(self, ev: dict) -> bool:
        max_players = ev.get("max_players") or 0
        if max_players <= 0:
            return True
        return count_event_players(ev["id"]) < max_players

    @ui.button(label="Register", style=discord.ButtonStyle.green, custom_id="register_button")
    async def register(self, interaction: discord.Interaction, button: ui.Button) -> None:
        if is_player_banned(str(interaction.user.id)):
            await interaction.response.send_message(
                "🚫 You are banned from registering.", ephemeral=True
            )
            return
        ev = get_event(self.event_id)
        if not ev:
            await interaction.response.send_message("Event not found.", ephemeral=True)
            return
        if ev["status"] != "registration":
            await interaction.response.send_message("Registration is not open.", ephemeral=True)
            return
        if not self._can_register(ev):
            await interaction.response.send_message(
                "🚫 **The event is full!** No more spots available.",
                ephemeral=True,
            )
            return

        if self.team_size == 1:
            await self._register_solo(interaction, ev)
        else:
            await self._hint_team_signup(interaction, ev)

    async def _hint_team_signup(self, interaction: discord.Interaction, ev: dict) -> None:
        """Team events use message-based signups — point the player at the format."""
        signup_channel_id = ev["signup_channel_id"] or ev["channel_id"]
        channel = interaction.guild.get_channel(int(signup_channel_id)) if interaction.guild else None
        channel_hint = channel.mention if channel else "the signup channel"
        label = team_label(self.team_size)
        await interaction.response.send_message(
            f"📝 **{ev['name']}** is a **{label}** event — no buttons needed!\n"
            f"Go to {channel_hint} and type:\n"
            f"`{team_signup_format(self.team_size)}`\n"
            f"Start with your own mention, then your teammate(s), then the skin. "
            f"The bot registers all of you.",
            ephemeral=True,
        )

    async def _register_solo(self, interaction: discord.Interaction, ev: dict) -> None:
        discord_id = str(interaction.user.id)
        username = interaction.user.display_name

        existing = query_one(
            "SELECT * FROM registrations WHERE event_id = ? AND discord_id = ?",
            (ev["id"], discord_id),
        )
        if existing and existing["status"] == "confirmed":
            await interaction.response.send_message(
                "You are already registered.", ephemeral=True
            )
            return

        upsert_player(discord_id, username)
        player = query_one("SELECT * FROM players WHERE discord_id = ?", (discord_id,))

        if not player or not player["game_username"]:
            modal = IGNModal(event_id=ev["id"])
            await interaction.response.send_modal(modal)
            await modal.wait()
            ign = modal.result or username
            await interaction.followup.send(
                "⚠️ Changing your in-game name later will require running the `/change-ign` command.",
                ephemeral=True,
            )
        else:
            ign = player["game_username"]
            await interaction.response.defer()

        if existing:
            execute(
                "UPDATE registrations SET username = ?, team_members = NULL, "
                "status = 'confirmed' WHERE id = ?",
                (username, existing["id"]),
            )
        else:
            execute(
                "INSERT INTO registrations "
                "(event_id, discord_id, username, status) "
                "VALUES (?, ?, ?, 'confirmed')",
                (ev["id"], discord_id, username),
            )

        regs = get_event_registrations(ev["id"])
        total_players = self._count_players(regs)
        await interaction.followup.send(
            f"✅ {interaction.user.mention} ({ign}) registered for **{ev['name']}** ({total_players} player{'s' if total_players != 1 else ''})",
            ephemeral=False,
        )

    async def _ask_teammates(self, interaction: discord.Interaction, ev: dict) -> None:
        max_players = ev.get("max_players") or 0
        if max_players > 0 and count_event_players(ev["id"]) + (self.team_size - 1) > max_players:
            await interaction.response.send_message(
                "🚫 **The event is full!** No more spots available.",
                ephemeral=True,
            )
            return

        signup_channel_id = ev["signup_channel_id"] or ev["channel_id"]
        channel = interaction.guild.get_channel(int(signup_channel_id))
        if not channel:
            await interaction.response.send_message(
                "Signup channel not found.", ephemeral=True
            )
            return

        pending = query_one(
            "SELECT * FROM pending_registrations WHERE event_id = ? AND discord_id = ?",
            (ev["id"], str(interaction.user.id)),
        )
        if pending:
            await interaction.response.send_message(
                "You already have a pending registration. Reply to the bot's message above.",
                ephemeral=True,
            )
            return

        discord_id = str(interaction.user.id)
        username = interaction.user.display_name
        upsert_player(discord_id, username)
        player = query_one("SELECT * FROM players WHERE discord_id = ?", (discord_id,))

        if not player or not player["game_username"]:
            modal = IGNModal(event_id=ev["id"], team_size=self.team_size)
            await interaction.response.send_modal(modal)
            await modal.wait()
            ign = modal.result or username
            skin = modal.skin_result or "Default"
            await interaction.followup.send(
                "⚠️ Changing your in-game name later will require running the `/change-ign` command.",
                ephemeral=True,
            )
        else:
            ign = player["game_username"]
            skin_modal = IGNModal(event_id=ev["id"], team_size=self.team_size)
            skin_modal.ign.default = ign
            await interaction.response.send_modal(skin_modal)
            await skin_modal.wait()
            skin = skin_modal.skin_result or "Default"

        team_label = {1: "Solo", 2: "Duo", 3: "Trio"}.get(self.team_size, "Squad")
        need = self.team_size - 1
        prompt = await channel.send(
            f"📝 {interaction.user.mention} ({ign}) — **{ev['name']}** ({team_label})\n"
            f"Skin: {skin}\n"
            f"Reply with {need} mention{'s' if need > 1 else ''}: "
            f"{'@teammate1 @teammate2' if need > 1 else '@teammate'}"
        )

        execute(
            "INSERT INTO pending_registrations (event_id, discord_id, prompt_message_id, skin) "
            "VALUES (?, ?, ?, ?)",
            (ev["id"], discord_id, str(prompt.id), skin),
        )

    def _count_players(self, regs: list) -> int:
        count = 0
        for r in regs:
            count += 1
            if r.get("team_members"):
                count += len(r["team_members"].split(","))
        return count


class RegistrationHandler(commands.Cog):
    def __init__(self, bot: commands.Bot) -> None:
        self.bot = bot

    @commands.Cog.listener()
    async def on_message(self, message: discord.Message) -> None:
        if message.author.bot:
            return
        if not message.guild:
            return

        if await self._try_message_signup(message):
            return

        me = message.guild.get_member(self.bot.user.id)
        if me and me in message.mentions and not message.reference:
            open_event = query_one(
                "SELECT * FROM events WHERE status = 'registration' "
                "AND (signup_channel_id = ? OR channel_id = ?) ORDER BY id DESC LIMIT 1",
                (str(message.channel.id), str(message.channel.id)),
            )
            if open_event:
                user_ev = get_event(open_event["id"])
                if user_ev:
                    ev = user_ev
                    view = RegisterView(
                        event_id=ev["id"],
                        team_size=ev["team_size"],
                        qualification_enabled=bool(ev.get("qualification_enabled")),
                    )
                    await message.reply(
                        f"🎟️ {message.author.mention} — register for **{ev['name']}** below!",
                        view=view,
                    )
                    return

        if not message.reference:
            return
        if not message.reference.message_id:
            return

        try:
            replied = await message.channel.fetch_message(message.reference.message_id)
        except Exception:
            return
        if not replied or replied.author != self.bot.user:
            return

        pending = query_one(
            "SELECT * FROM pending_registrations WHERE prompt_message_id = ?",
            (str(replied.id),),
        )
        if not pending:
            return

        ev = get_event(pending["event_id"])
        if not ev:
            return

        team_size = ev["team_size"]
        user_ids = re.findall(r'<@!?(\d+)>', message.content)
        members = []
        for uid in user_ids:
            m = message.guild.get_member(int(uid))
            if m and not m.bot:
                members.append(m)

        need = team_size - 1
        if len(members) != need:
            await message.add_reaction("❌")
            return

        banned = [
            m for m in ([message.author] + members) if is_player_banned(str(m.id))
        ]
        if banned:
            await message.add_reaction("❌")
            await message.channel.send(
                f"🚫 {'One or more players' if len(banned) > 1 else banned[0].mention + ' is'} banned from registering.",
                delete_after=15,
            )
            execute("DELETE FROM pending_registrations WHERE id = ?", (pending["id"],))
            return

        max_players = ev.get("max_players") or 0
        if max_players > 0 and count_event_players(ev["id"]) + team_size > max_players:
            await message.add_reaction("❌")
            await message.channel.send(
                "🚫 **The event is full!** This team can't be added.",
                delete_after=15,
            )
            execute("DELETE FROM pending_registrations WHERE id = ?", (pending["id"],))
            return

        missing_igns = []
        for m in members:
            did = str(m.id)
            p = query_one("SELECT game_username FROM players WHERE discord_id = ?", (did,))
            if not p or not p["game_username"]:
                missing_igns.append(m)

        if missing_igns:
            mentions = " ".join([m.mention for m in missing_igns])
            await message.add_reaction("❌")
            await message.channel.send(
                f"❌ {mentions} {'doesn' + chr(39) + 't have an IGN set.' if len(missing_igns) == 1 else 'don' + chr(39) + 't have IGNs set.'} "
                f"{'They needs' if len(missing_igns) == 1 else 'They need'} to use `/change-ign` first.",
                delete_after=15,
            )
            execute("DELETE FROM pending_registrations WHERE id = ?", (pending["id"],))
            return

        lead = message.guild.get_member(int(pending["discord_id"]))
        if not lead:
            return

        all_members = [lead] + members
        discord_ids = [str(m.id) for m in all_members]
        usernames = [m.display_name for m in all_members]

        for did, uname in zip(discord_ids, usernames):
            upsert_player(did, uname)

        team_ids_check = set(discord_ids)
        all_regs = get_event_registrations(ev["id"])
        for reg in all_regs:
            reg_ids = {reg["discord_id"]}
            if reg.get("team_members"):
                reg_ids.update(reg["team_members"].split(","))
            if team_ids_check & reg_ids:
                overlap = team_ids_check & reg_ids
                overlap_names = [
                    m.display_name for m in all_members if str(m.id) in overlap
                ]
                await message.add_reaction("❌")
                await message.channel.send(
                    f"❌ {', '.join(overlap_names)} already registered.",
                    delete_after=10,
                )
                execute("DELETE FROM pending_registrations WHERE id = ?", (pending["id"],))
                return

        team_json = ",".join(discord_ids[1:]) if len(discord_ids) > 1 else None
        skin = pending.get("skin")
        execute(
            "INSERT INTO registrations "
            "(event_id, discord_id, username, team_members, skin, status) "
            "VALUES (?, ?, ?, ?, ?, 'confirmed')",
            (ev["id"], discord_ids[0], usernames[0], team_json, skin),
        )
        execute("DELETE FROM pending_registrations WHERE id = ?", (pending["id"],))

        igns = []
        for did in discord_ids:
            p = query_one("SELECT game_username FROM players WHERE discord_id = ?", (did,))
            ign = p["game_username"] if p and p["game_username"] else None
            igns.append(ign)

        team_parts = []
        for m, ign in zip(all_members, igns):
            if ign:
                team_parts.append(f"{m.mention} ({ign})")
            else:
                team_parts.append(m.mention)

        team_display = " + ".join(team_parts)
        regs = get_event_registrations(ev["id"])
        total_players = self._count_players(regs)
        await message.add_reaction("✅")
        skin_display = f" | Skin: {skin}" if skin else ""
        await message.channel.send(
            f"✅ {team_display} registered for **{ev['name']}** ({total_players} player{'s' if total_players != 1 else ''}){skin_display}"
        )

    async def _try_message_signup(self, message: discord.Message) -> bool:
        """Message-based team signups: `@you @t1 @t2 SkinName` while registration is open.

        Strictly anchored: the whole message must be exactly ``team_size``
        mentions (you first, then your teammates) plus the skin. Restricted to
        channels that have an open team event. Teammates are resolved from the
        guild cache, falling back to ``fetch_user`` for users not in the cache.

        Returns True when the message belonged to this flow (parsed, validated,
        and answered) — the caller should not fall through to the legacy flows.
        """
        if message.guild is None:
            return False
        ev = query_one(
            "SELECT * FROM events WHERE status = 'registration' AND team_size >= 2 "
            "AND (signup_channel_id = ? OR (signup_channel_id IS NULL AND channel_id = ?)) "
            "ORDER BY id DESC LIMIT 1",
            (str(message.channel.id), str(message.channel.id)),
        )
        if not ev:
            return False

        parsed = parse_signup(message.content, ev["team_size"])
        if parsed is None:
            return False  # wrong shape → casual chat, ignore silently

        ids, skin = parsed
        members = []
        for uid in ids:
            member = message.guild.get_member(int(uid))
            if member is not None and not member.bot:
                members.append((str(member.id), member.display_name))
                continue
            try:
                user = await self.bot.fetch_user(int(uid))
            except discord.NotFound:
                continue
            if not user.bot:
                members.append((str(user.id), user.name))

        if not members or members[0][0] != str(message.author.id):
            await message.add_reaction("❌")
            await message.channel.send(
                "❌ Start with **your own** mention first, e.g. "
                f"`{team_signup_format(ev['team_size'])}`."
            )
            return True

        result = register_team(
            ev,
            members,
            skin,
        )

        if result["ok"]:
            await message.add_reaction("✅")
            await message.channel.send(result["text"])
            return True

        await message.add_reaction("❌")
        reason = {
            "BANNED": "🚫 One or more players in this team are banned from registering.",
            "WRONG_MEMBER_COUNT": (
                f"Wrong mention count — {team_label(ev['team_size'])} needs exactly "
                f"{result['need']} mentions, got {result['got']}. "
                f"Example: `{team_signup_format(ev['team_size'])}`. "
                f"Solo? Mention yourself {result['need']} times instead."
            ),
            "DUPLICATE": (
                "❌ You mentioned the same player more than once. "
                "Playing solo? Mention only yourself."
            ),
            "FULL": "🚫 **The event is full!** This team can't be added.",
            "ALREADY_REGISTERED": (
                "❌ Already registered: " + ", ".join(result.get("overlap", [])) + "."
            ),
            "SKIN_REQUIRED": (
                "❌ Add your team skin after the mentions, e.g. "
                f"`{team_signup_format(ev['team_size'])}`."
            ),
        }.get(result["code"], "❌ Couldn't register that team.")
        await message.channel.send(reason)
        return True

    def _count_players(self, regs: list) -> int:
        count = 0
        for r in regs:
            count += 1
            if r.get("team_members"):
                count += len(r["team_members"].split(","))
        return count


async def setup(bot: commands.Bot) -> None:
    await bot.add_cog(RegistrationHandler(bot))
