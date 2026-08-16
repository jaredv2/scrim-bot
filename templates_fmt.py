from __future__ import annotations

import re
from datetime import datetime, timedelta, timezone

# Timezone offsets (hours from UTC) for common abbreviations used in announcement
# strings like "3:00 PM EST". Defaults to UTC when the abbreviation is unknown.
TIME_TZ_OFFSETS = {
    "utc": 0, "gmt": 0, "est": -5, "edt": -4, "cst": -6, "cdt": -5,
    "mst": -7, "mdt": -6, "pst": -8, "pdt": -7, "bst": 1, "cet": 1,
    "cest": 2, "eet": 2, "ist": 5.5, "jst": 9, "kst": 9, "aest": 10,
    "aedt": 11, "sgt": 8, "hkt": 8, "nzst": 12, "nzdt": 13,
}

_TIME_PATTERN = re.compile(
    r"^(?P<h>\d{1,2})(?::(?P<m>\d{2}))?\s*(?P<ampm>am|pm|AM|PM)?\s*(?P<tz>[a-zA-Z]+)?\s*$"
)


def to_unix_ts(time_str: str | None) -> int | None:
    """Parse '3:00 PM EST' / '13:00' / '3pm' into a UTC unix timestamp.

    Falls back to the next occurrence of that time of day. Returns None when
    the string can't be parsed as a clock time.
    """
    if not time_str:
        return None
    m = _TIME_PATTERN.match(time_str.strip())
    if not m:
        return None
    hour = int(m.group("h"))
    minute = int(m.group("m") or 0)
    if hour > 23 or minute > 59:
        return None
    ampm = (m.group("ampm") or "").lower()
    tz = (m.group("tz") or "utc").lower()
    if ampm == "pm" and hour < 12:
        hour += 12
    if ampm == "am" and hour == 12:
        hour = 0
    offset = TIME_TZ_OFFSETS.get(tz, 0)
    now = datetime.now(timezone.utc)
    target = now.replace(hour=hour, minute=minute, second=0, microsecond=0)
    target -= timedelta(hours=offset)
    if target < now:
        target += timedelta(days=1)
    return int(target.timestamp())


def dynamic_time(time_str: str | None, style: str = "F") -> str:
    """Render a time string as a Discord dynamic timestamp, e.g. `<t:1723075200:F>`.

    Falls back to the raw string (times Discord can't render).
    """
    unix = to_unix_ts(time_str)
    if unix is None:
        return time_str.strip() if time_str else ""
    return f"<t:{unix}:{style}>"


def role_ping(role_id: str) -> str:
    """Role mention for announcements; falls back to @everyone when not configured."""
    return f"<@&{role_id}>" if role_id else "@everyone"


def cup_announcement(
    name: str,
    format_label: str,
    region: str,
    start_time: str,
    point_kill: int = 1,
    point_win: int = 5,
    ping_role: str = "@everyone",
    place_1: str | None = None,
    place_2: str | None = None,
    place_3: str | None = None,
    place_4plus: str | None = None,
) -> str:
    placement_lines = [
        ("🥇 1st", place_1),
        ("🥈 2nd", place_2),
        ("🥉 3rd", place_3),
        ("🎖️ 4th+", place_4plus),
    ]
    placement_block = "\n".join(
        f"{label} — **{value}**" for label, value in placement_lines if value
    )
    placement_section = f"\n\n## 🏆 Placements\n\n{placement_block}\n" if placement_block else ""
    start_stamp = dynamic_time(start_time, "F")
    start_rel = dynamic_time(start_time, "R")
    return (
        f"# 🏆 {name} Announcement\n\n\n"
        f"\n"
        f"🗓️ **Today** — {start_stamp}\n\n"
        f"⏰ **{start_rel}**\n"
        f"\n"
        f"🌍 **Region:** {region}\n\n"
        f"👥 **Mode:** {format_label}\n"
        f"\n"
        f"## 📊 Scoring\n\n"
        f"\n"
        f"🎯 **Elimination** — **+{point_kill} Point{'s' if point_kill != 1 else ''}**\n\n"
        f"👑 **Victory** — **+{point_win} Points**\n"
        f"{placement_section}"
        f"\n"
        f"🍀 Good luck to all competitors!\n"
        f"\n"
        f"{ping_role}\n"
    )


def signup_announcement(
    name: str,
    format_label: str,
    region: str,
    start_time: str,
    signup_channel: str,
    ping_role: str = "@everyone",
) -> str:
    start_stamp = dynamic_time(start_time, "F")
    start_rel = dynamic_time(start_time, "R")
    return (
        f"🚨 {name} – SIGNUPS OPEN 🚨\n"
        f"{ping_role}\n"
        f"\n"
        f"Sign-ups are NOW OPEN! 🔥\n"
        f"\n"
        f"🎮 Format: {format_label}\n\n"
        f"📍 Region: {region}\n\n"
        f"🕑 Start Time: {start_stamp} ({start_rel})\n\n"
        f"🎟️ Sign-Ups: OPEN NOW\n"
        f"\n"
        f"⚡ How to Join:\n\n"
        f"• Head to the {signup_channel} channel.\n"
        f"• Register your IGN.\n"
        f"• Be ready before the tournament starts.\n"
        f"\n\n"
        f"Don't miss out — secure your spot now!"
    )


def end_tournament(
    name: str,
    winner_mention: str,
    runner_up_mention: str = "",
    ping_role: str = "@everyone",
    winner_stats: str = "",
    runner_up_stats: str = "",
    next_event: str = "",
) -> str:
    lines = [
        f"{ping_role} 🏆 {name} HAS OFFICIALLY ENDED! 🏆",
        "",
        "What a series it has been! 🔥 From the qualifiers all the way to the finals, "
        "we saw some insane competition, crazy performances, and a ton of unforgettable moments. "
        f"GGs to {winner_mention} for putting on an incredible performance and taking the top spots! 🏆⚡ "
        "You showed amazing consistency and proved yourself when it mattered most.",
    ]
    if winner_stats:
        lines.append(f"{winner_mention} {winner_stats}")
    if runner_up_mention:
        lines.append(
            f"GGs to {runner_up_mention} as well for an amazing run! 🥈"
        )
        if runner_up_stats:
            lines.append(f"{runner_up_mention} {runner_up_stats}")
    lines.extend([
        "",
        "🔥 Huge GGs to EVERYONE who competed!",
        "",
        "This is just the start of something much bigger for the community. "
        "Keep grinding, keep improving, and most importantly — DON'T GIVE UP.",
    ])
    if next_event:
        lines.extend([
            "",
            f"🎟️ Up next: **{next_event}** — stay tuned!",
        ])
    return "\n".join(lines)


def dm_message(
    event_name: str,
    format_label: str,
    region: str,
    start_time: str,
    room_code: str,
    game_number: int = 1,
    shoot_timer: int = 0,
    placement_scale: str = "",
) -> str:
    timer_line = f"\n⏱️ **Shoot Timer:** {shoot_timer}s\n" if (shoot_timer or 0) > 0 else ""
    start_stamp = dynamic_time(start_time, "F")
    start_rel = dynamic_time(start_time, "R")
    scale_line = f"\nPlacement points: {placement_scale}\n" if placement_scale else ""
    return (
        f"🎮 **{event_name} — Game {game_number}**\n"
        f"\n"
        f"Format: {format_label}\n\n"
        f"Region: {region}\n\n"
        f"{scale_line}"
        f"Start Time: {start_stamp} ({start_rel})\n"
        f"{timer_line}"
        f"\n"
        f"**Room Code:** `{room_code}`\n"
        f"\n"
        f"Copy the code above and join the game. Good luck!"
    )


def parse_time(time_str: str) -> str:
    """Parse time string like '3:00 PM EST' or '13:00'."""
    return time_str.strip()
