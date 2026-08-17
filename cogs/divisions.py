from __future__ import annotations

import discord
from config import settings
from discord import app_commands
from discord.ext import commands
from embeds import base, error, success

from database import (
    add_division_member,
    create_division,
    delete_division,
    get_division_members,
    get_divisions,
    remove_division_member,
)


def _find_division(name: str) -> dict | None:
    return next(
        (d for d in get_divisions() if d["name"].lower() == name.strip().lower()),
        None,
    )


def _division_role(guild: discord.Guild, division: dict) -> discord.Role | None:
    if not division.get("role_id"):
        return None
    return guild.get_role(int(division["role_id"]))


class DivisionsCog(commands.Cog):
    def __init__(self, bot: commands.Bot) -> None:
        self.bot = bot

    @commands.hybrid_command(
        name="create-division",
        description="Create a division (optionally linked to a Discord role) for gated cups",
    )
    @app_commands.describe(name="Division name", role="Optional Discord role to link")
    async def create_division_cmd(
        self,
        ctx: commands.Context,
        name: str,
        role: discord.Role | None = None,
    ) -> None:
        if not await self._check_admin(ctx):
            return
        if ctx.interaction:
            await ctx.interaction.response.defer(ephemeral=True)

        division = create_division(
            name.strip(), str(role.id) if role else "", str(ctx.guild.id)
        )
        role_note = f" ({role.mention})" if role else " (no role linked)"
        await ctx.send(
            embed=success(
                f"Division **{division['name']}** created (ID: {division['id']}){role_note}."
            )
        )

    @commands.hybrid_command(
        name="delete-division",
        description="Delete a division (the linked Discord role is kept)",
    )
    @app_commands.describe(name="Division name")
    async def delete_division_cmd(
        self, ctx: commands.Context, name: str
    ) -> None:
        if not await self._check_admin(ctx):
            return
        division = _find_division(name)
        if not division:
            await ctx.send(embed=error(f"Division **{name}** not found."))
            return

        delete_division(division["id"])
        await ctx.send(
            embed=success(f"Division **{division['name']}** deleted.")
        )

    @commands.hybrid_command(
        name="divisions",
        description="List all divisions with their member counts",
    )
    async def list_divisions(self, ctx: commands.Context) -> None:
        divisions = get_divisions()
        if not divisions:
            await ctx.send(embed=base("🏷️ No divisions yet. Use `/create-division`."))
            return

        lines = []
        for d in divisions:
            members = get_division_members(d["id"])
            lines.append(f"**{d['name']}** (ID: {d['id']}) — {len(members)} member(s)")

        embed = base("🏷️ Divisions", 0x3498DB)
        embed.description = "\n".join(lines)
        await ctx.send(embed=embed)

    @commands.hybrid_command(
        name="add-division-member",
        description="Add a player to a division (grants the linked role too)",
    )
    @app_commands.describe(name="Division name", member="Player to add")
    async def add_division_member_cmd(
        self,
        ctx: commands.Context,
        name: str,
        member: discord.Member,
    ) -> None:
        if not await self._check_admin(ctx):
            return
        division = _find_division(name)
        if not division:
            await ctx.send(embed=error(f"Division **{name}** not found."))
            return

        add_division_member(division["id"], str(member.id))
        role = _division_role(ctx.guild, division)
        role_note = ""
        if role:
            try:
                await member.add_roles(role, reason=f"Division {division['name']}")
                role_note = f" + {role.mention}"
            except Exception:
                role_note = " (couldn't grant the linked role)"
        await ctx.send(
            embed=success(
                f"{member.mention} added to division **{division['name']}**{role_note}."
            )
        )

    @commands.hybrid_command(
        name="remove-division-member",
        description="Remove a player from a division (removes the linked role too)",
    )
    @app_commands.describe(name="Division name", member="Player to remove")
    async def remove_division_member_cmd(
        self,
        ctx: commands.Context,
        name: str,
        member: discord.Member,
    ) -> None:
        if not await self._check_admin(ctx):
            return
        division = _find_division(name)
        if not division:
            await ctx.send(embed=error(f"Division **{name}** not found."))
            return

        removed = remove_division_member(division["id"], str(member.id))
        if not removed:
            await ctx.send(
                embed=error(f"{member.mention} is not in division **{division['name']}**.")
            )
            return
        role = _division_role(ctx.guild, division)
        role_note = ""
        if role:
            try:
                await member.remove_roles(role, reason=f"Division {division['name']}")
                role_note = f" − {role.mention}"
            except Exception:
                role_note = " (couldn't remove the linked role)"
        await ctx.send(
            embed=success(
                f"{member.mention} removed from division **{division['name']}**{role_note}."
            )
        )

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
    await bot.add_cog(DivisionsCog(bot))