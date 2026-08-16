from __future__ import annotations

import discord

from database import get_rank_for_pr, query_one

RANK_COLORS = {
    "Unranked": 0x6B7280,
    "Bronze I": 0xCD7F32, "Bronze II": 0xC07A2B, "Bronze III": 0xB3714B,
    "Silver I": 0xC0C0C0, "Silver II": 0xA9B4C2, "Silver III": 0x9AA7B8,
    "Gold I": 0xF1C40F, "Gold II": 0xE0B50C, "Gold III": 0xD4A017,
    "Platinum I": 0x85C1E9, "Platinum II": 0x74B4E0, "Platinum III": 0x63A7D6,
    "Diamond I": 0x5DADE2, "Diamond II": 0x4A9AD1, "Diamond III": 0x3E87BF,
    "Elite I": 0xAF7AC5, "Elite II": 0x9E6AB8, "Elite III": 0x8D5AAA,
    "Champion I": 0xE67E22, "Champion II": 0xD9771E, "Champion III": 0xCC6F1A,
    "Unreal I": 0xE74C3C, "Unreal II": 0xD14334, "Unreal III": 0xC2382B,
}

LEGEND_ROLE_NAME = "Unreal Legend"
LEGEND_COLOR = 0x9B59B6


def _rank_role_name(rank_name: str) -> str:
    return f"Rank {rank_name}"


def get_player_rank(discord_id: str) -> dict | None:
    player = query_one(
        "SELECT pr FROM players WHERE discord_id = ?", (discord_id,)
    )
    if not player:
        return None
    tier = get_rank_for_pr(player["pr"] or 0)
    return {
        "pr": player["pr"] or 0,
        "rank": tier["name"] if tier else "Unranked",
    }


async def _ensure_rank_role(guild: discord.Guild, rank_name: str) -> discord.Role:
    role_name = _rank_role_name(rank_name)
    role = discord.utils.get(guild.roles, name=role_name)
    if role:
        return role
    color = RANK_COLORS.get(rank_name, 0x99AAB5)
    return await guild.create_role(
        name=role_name,
        color=discord.Color(color),
        reason="Auto-created rank role",
    )


async def sync_rank_role(guild: discord.Guild, member: discord.Member, pr: int) -> str:
    tier = get_rank_for_pr(pr)
    rank_name = tier["name"] if tier else "Unranked"
    target_role = await _ensure_rank_role(guild, rank_name)

    rank_roles = [
        r for r in member.roles if r.name.startswith("Rank ")
    ]
    for role in rank_roles:
        if role.id != target_role.id and role.name != _rank_role_name("Unranked"):
            try:
                await member.remove_roles(role, reason="PR rank update")
            except discord.HTTPException:
                pass

    if target_role not in member.roles:
        try:
            await member.add_roles(target_role, reason="PR rank update")
        except discord.HTTPException:
            pass

    return rank_name


async def _ensure_legend_role(guild: discord.Guild) -> discord.Role:
    role = discord.utils.get(guild.roles, name=LEGEND_ROLE_NAME)
    if role:
        return role
    return await guild.create_role(
        name=LEGEND_ROLE_NAME,
        color=discord.Color(LEGEND_COLOR),
        reason="Auto-created legend role",
    )


async def sync_legend_role(guild: discord.Guild) -> discord.Member | None:
    """Give the 'Unreal Legend' role to the server's best player (top PR,
    tie-broken by wins and kills) and remove it from everyone else."""
    from database import get_server_legend

    legend = get_server_legend()
    legend_member = guild.get_member(int(legend["discord_id"])) if legend else None

    role = await _ensure_legend_role(guild)
    for member in guild.members:
        if member.bot:
            continue
        if any(r.name == LEGEND_ROLE_NAME for r in member.roles) and member != legend_member:
            try:
                await member.remove_roles(role, reason="Unreal Legend update")
            except discord.HTTPException:
                pass

    if legend_member:
        try:
            if not any(r.name == LEGEND_ROLE_NAME for r in legend_member.roles):
                await legend_member.add_roles(role, reason="Unreal Legend update")
        except discord.HTTPException:
            pass

    return legend_member
