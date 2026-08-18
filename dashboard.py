from __future__ import annotations

import os
import secrets
import sqlite3
import sys
import time
from pathlib import Path

from fastapi import Depends, FastAPI, HTTPException, Request, UploadFile, File
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse, FileResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

sys.path.insert(0, str(Path(__file__).parent.parent))
from config import settings

from database import (
    add_event_qualifier,
    apply_placement_points,
    ban_player,
    create_event_record,
    create_session,
    execute,
    get_bans,
    get_ban_by_id,
    get_divisions,
    get_event,
    get_event_active_session,
    get_event_games,
    get_event_lobbies,
    get_event_qualifiers,
    get_event_registrations,
    get_event_sessions,
    get_game_kills,
    get_game_players,
    get_game_team_leaderboard,
    get_leaderboard,
    get_player_profile,
    get_players_leaderboard,
    get_session_leaderboard,
    get_session_matches,
    get_solo_leaderboard,
    get_season,
    get_team_leaderboard,
    get_bot_logs,
    init_db,
    create_game_record,
    log_bot_action,
    query,
    query_one,
    queue_command,
    remove_event_qualifier,
    unban_player,
    update_player_fields,
    upsert_player,
)

app = FastAPI(title="Scrim Dashboard")

STATIC_DIR = Path(__file__).parent / "static"
TEMPLATE_DIR = Path(__file__).parent / "templates"
STATIC_DIR.mkdir(exist_ok=True)
TEMPLATE_DIR.mkdir(exist_ok=True)

app.mount("/static", StaticFiles(directory=str(STATIC_DIR)), name="static")
templates = Jinja2Templates(directory=str(TEMPLATE_DIR))

_dashboard_sessions: dict[str, dict] = {}
MAX_SESSIONS = 100
SESSION_TTL = 86400


def _cleanup_expired_sessions() -> None:
    now = time.time()
    expired = [k for k, v in _dashboard_sessions.items() if now - v["created_at"] > SESSION_TTL]
    for k in expired:
        del _dashboard_sessions[k]


def _prune_if_needed() -> None:
    if len(_dashboard_sessions) > MAX_SESSIONS:
        oldest = sorted(_dashboard_sessions, key=lambda k: _dashboard_sessions[k]["created_at"])
        for k in oldest[: len(oldest) - MAX_SESSIONS]:
            del _dashboard_sessions[k]


def get_current_user(request: Request) -> dict:
    token = request.cookies.get("dashboard_token")
    if not token or token not in _dashboard_sessions:
        raise HTTPException(status_code=302, headers={"Location": "/login"})
    return _dashboard_sessions[token]


def _is_ajax(request: Request) -> bool:
    return request.headers.get("X-Requested-With") == "XMLHttpRequest"


def _parse_duration(duration: str):
    """Parse a duration like '2h', '3d', '1w', '90m' into a timedelta (or None)."""
    import re
    from datetime import timedelta

    match = re.match(r"^(\d+)\s*([smhdw])$", duration.strip().lower())
    if not match:
        return None
    amount = int(match.group(1))
    unit = match.group(2)
    if unit == "s":
        return timedelta(seconds=amount)
    if unit == "m":
        return timedelta(minutes=amount)
    if unit == "h":
        return timedelta(hours=amount)
    if unit == "d":
        return timedelta(days=amount)
    if unit == "w":
        return timedelta(weeks=amount)
    return None


@app.on_event("startup")
async def startup() -> None:
    init_db()


@app.api_route("/health", methods=["GET", "POST", "HEAD", "OPTIONS"])
async def health():
    return {"status": "ok"}


@app.get("/backup/download")
async def backup_download(user: dict = Depends(get_current_user)):
    """Backups are managed by Supabase when running on Postgres."""
    raise HTTPException(
        status_code=400,
        detail="Backups are managed in Supabase (Database > Backups) when running on Postgres.",
    )


@app.post("/backup/upload")
@app.post("/restore")
async def restore_db(
    request: Request,
    user: dict = Depends(get_current_user),
    file: UploadFile = File(...),
):
    """Restoring a SQLite file is not supported on Postgres."""
    raise HTTPException(
        status_code=400,
        detail="Restoring a SQLite backup is not supported when running on Postgres. Use Supabase (Database > Restore) instead.",
    )


@app.get("/", response_class=HTMLResponse)
async def index(request: Request, user: dict = Depends(get_current_user)):
    events = query(
        "SELECT * FROM vtx_events ORDER BY created_at DESC LIMIT 50"
    )
    return templates.TemplateResponse(
        request, "index.html", {"events": events, "user": user}
    )


@app.get("/home", response_class=HTMLResponse)
async def home(request: Request):
    """Public landing page — no login required."""
    events = query(
        "SELECT id, name, status, team_size, event_type, region, "
        "(SELECT COUNT(*) FROM vtx_registrations r WHERE r.event_id = e.id AND r.status = 'confirmed') AS players "
        "FROM vtx_events e ORDER BY created_at DESC LIMIT 25"
    )
    from database import get_players_leaderboard

    top = get_players_leaderboard()[:10]
    return templates.TemplateResponse(
        request,
        "home.html",
        {"events": events, "top": top, "season": get_season()},
    )


@app.get("/players", response_class=HTMLResponse)
async def players_page(request: Request, user: dict = Depends(get_current_user)):
    players = get_players_leaderboard()
    return templates.TemplateResponse(
        request, "players.html", {"players": players, "detail": None, "user": user}
    )


@app.get("/player/{discord_id}", response_class=HTMLResponse)
async def player_detail(request: Request, discord_id: str, user: dict = Depends(get_current_user)):
    players = get_players_leaderboard()
    detail = get_player_profile(discord_id)
    if not detail:
        raise HTTPException(status_code=404)
    return templates.TemplateResponse(
        request, "players.html", {"players": players, "detail": detail, "user": user}
    )


@app.post("/player/{discord_id}/update")
async def player_update(
    request: Request, discord_id: str, user: dict = Depends(get_current_user)
):
    """Edit a player's data fields from the dashboard."""
    form = await request.form()
    changes = {}
    for field in ("username", "game_username", "game_id", "country", "region"):
        value = (form.get(field) or "").strip()
        changes[field] = value or None
    update_player_fields(discord_id, changes)
    log_bot_action(None, "player_updated", f"fields={list(changes)}", user_id=discord_id)
    return RedirectResponse(url=f"/player/{discord_id}", status_code=302)


@app.get("/players/export")
async def players_export(request: Request, user: dict = Depends(get_current_user)):
    """Download all players as CSV (Excel-friendly, UTF-8 BOM)."""
    import csv
    import io

    from fastapi.responses import Response

    players = get_players_leaderboard()
    buf = io.StringIO()
    writer = csv.writer(buf)
    writer.writerow(
        [
            "discord_id",
            "username",
            "ign",
            "game_id",
            "country",
            "region",
            "pr",
            "total_pr",
            "wins",
            "kills",
            "games",
            "avg_placement",
        ]
    )
    for p in players:
        writer.writerow(
            [
                p.get("discord_id"),
                p.get("username"),
                p.get("game_username"),
                p.get("game_id"),
                p.get("country"),
                p.get("region"),
                p.get("pr"),
                p.get("total_pr"),
                p.get("total_wins"),
                p.get("total_kills"),
                p.get("total_games"),
                p.get("avg_placement"),
            ]
        )
    data = "\ufeff" + buf.getvalue()
    return Response(
        content=data,
        media_type="text/csv; charset=utf-8",
        headers={
            "Content-Disposition": f"attachment; filename=players_{time.strftime('%Y%m%d')}.csv"
        },
    )


@app.get("/bans", response_class=HTMLResponse)
async def bans_page(request: Request, user: dict = Depends(get_current_user)):
    from datetime import datetime

    bans = get_bans()
    now = datetime.utcnow()
    return templates.TemplateResponse(
        request,
        "bans.html",
        {"bans": bans, "now_iso": now.isoformat(), "user": user},
    )


@app.post("/bans/add")
async def ban_add(request: Request, user: dict = Depends(get_current_user)):
    from datetime import datetime

    form = await request.form()
    discord_id = form.get("discord_id", "").strip()
    reason = form.get("reason", "").strip()
    duration = form.get("duration", "0").strip()

    if not discord_id:
        return RedirectResponse(url="/bans", status_code=302)

    banned_until = None
    if duration != "0":
        delta = _parse_duration(duration)
        if not delta:
            return RedirectResponse(url="/bans", status_code=302)
        banned_until = (datetime.utcnow() + delta).isoformat()

    ban_player(discord_id, banned_until or "", reason=reason, created_by="dashboard")
    return RedirectResponse(url="/bans", status_code=302)


@app.post("/bans/{ban_id}/unban")
async def ban_unban(ban_id: int, request: Request, user: dict = Depends(get_current_user)):
    ban = get_ban_by_id(ban_id)
    if ban:
        unban_player(ban["discord_id"])
    return RedirectResponse(url="/bans", status_code=302)


@app.get("/login", response_class=HTMLResponse)
async def login_page(request: Request):
    return templates.TemplateResponse(request, "login.html")


@app.post("/login")
async def login(request: Request):
    form = await request.form()
    password = form.get("password", "")
    if password != settings.dashboard_admin_password:
        raise HTTPException(status_code=401, detail="Invalid password")
    _cleanup_expired_sessions()
    _prune_if_needed()
    token = secrets.token_urlsafe(32)
    _dashboard_sessions[token] = {
        "token": token,
        "created_at": time.time(),
    }
    response = RedirectResponse(url="/", status_code=302)
    response.set_cookie("dashboard_token", token, httponly=True, max_age=86400)
    return response


@app.get("/event/{event_id}", response_class=HTMLResponse)
async def event_detail(request: Request, event_id: int, user: dict = Depends(get_current_user)):
    ev = get_event(event_id)
    if not ev:
        raise HTTPException(status_code=404)
    registrations = get_event_registrations(event_id)
    games = get_event_games(event_id)
    lobbies = get_event_lobbies(event_id)
    board = get_leaderboard(event_id)
    solo_board = get_solo_leaderboard(event_id)
    team_board = get_team_leaderboard(event_id)

    player_map = {}
    ign_map = {}
    for r in registrations:
        player_map[r["discord_id"]] = r["username"]
    for r in registrations:
        if r.get("team_members"):
            for mid in r["team_members"].split(","):
                if mid not in player_map:
                    p = query_one("SELECT username FROM vtx_players WHERE discord_id = %s", (mid,))
                    if p:
                        player_map[mid] = p["username"]

    all_discord_ids = set(player_map.keys())
    if all_discord_ids:
        placeholders = ",".join(["%s"] * len(all_discord_ids))
        players = query(f"SELECT discord_id, game_username FROM vtx_players WHERE discord_id IN ({placeholders})", tuple(all_discord_ids))
        for p in players:
            if p["game_username"]:
                ign_map[p["discord_id"]] = p["game_username"]

    if ev and ev.get("team_size", 1) >= 2:
        registrations = [r for r in registrations if r.get("team_members")]

    logs = get_bot_logs(event_id)

    qualifiers = {q["discord_id"] for q in get_event_qualifiers(event_id)}

    sessions = get_event_sessions(event_id)
    active_session = get_event_active_session(event_id)
    session_boards = {}
    session_match_lists = {}
    for s in sessions:
        session_boards[s["id"]] = get_session_leaderboard(s["id"])
        session_match_lists[s["id"]] = get_session_matches(s["id"])

    return templates.TemplateResponse(
        request,
        "event.html",
        {
            "event": ev,
            "registrations": registrations,
            "games": games,
            "lobbies": lobbies,
            "leaderboard": board,
            "solo_board": solo_board,
            "team_board": team_board,
            "user": user,
            "player_map": player_map,
            "ign_map": ign_map,
            "logs": logs,
            "qualifiers": qualifiers,
            "sessions": sessions,
            "active_session": active_session,
            "session_boards": session_boards,
            "session_match_lists": session_match_lists,
        },
    )


@app.get("/event/{event_id}/game/{game_number}", response_class=HTMLResponse)
async def game_detail(
    request: Request, event_id: int, game_number: int,
    user: dict = Depends(get_current_user),
):
    ev = get_event(event_id)
    game = query_one(
        "SELECT * FROM vtx_games WHERE event_id = %s AND game_number = %s",
        (event_id, game_number),
    )
    if not game:
        raise HTTPException(status_code=404)
    players = get_game_players(game["id"])
    kills = get_game_kills(game["id"])

    team_board = []
    player_map = {}
    ign_map = {}
    if ev and ev.get("team_size", 1) >= 2:
        team_board = get_game_team_leaderboard(game["id"], event_id)
        regs = get_event_registrations(event_id)
        for r in regs:
            player_map[r["discord_id"]] = r["username"]
            if r.get("team_members"):
                for mid in r["team_members"].split(","):
                    if mid not in player_map:
                        p = query_one("SELECT username FROM vtx_players WHERE discord_id = %s", (mid,))
                        if p:
                            player_map[mid] = p["username"]
        all_ids = set(player_map.keys())
        if all_ids:
            ph = ",".join(["%s"] * len(all_ids))
            pls = query(f"SELECT discord_id, game_username FROM vtx_players WHERE discord_id IN ({ph})", tuple(all_ids))
            for pl in pls:
                if pl["game_username"]:
                    ign_map[pl["discord_id"]] = pl["game_username"]

    winner = None
    winner_ign = None
    winner_team = None
    if ev and ev.get("team_size", 1) >= 2 and team_board:
        for t in team_board:
            if not t.get("is_dq"):
                winner_team = t
                wp = query_one("SELECT game_username FROM vtx_players WHERE discord_id = %s", (t["lead_id"],))
                if wp and wp["game_username"]:
                    winner_ign = wp["game_username"]
                break
    elif players:
        for p in players:
            if p.get("placement") == 1 and not p.get("is_disqualified"):
                winner = p
                wp = query_one("SELECT game_username FROM vtx_players WHERE discord_id = %s", (p["discord_id"],))
                if wp and wp["game_username"]:
                    winner_ign = wp["game_username"]
                break

    return templates.TemplateResponse(
        request,
        "game.html",
        {
            "event": ev,
            "game": game,
            "players": players,
            "kills": kills,
            "team_board": team_board,
            "player_map": player_map,
            "ign_map": ign_map,
            "winner": winner,
            "winner_ign": winner_ign,
            "winner_team": winner_team,
            "user": user,
        },
    )


@app.post("/event/create")
async def create_event(request: Request, user: dict = Depends(get_current_user)):
    form = await request.form()

    def _int_field(name: str, default: int = 0) -> int:
        try:
            return int(form.get(name) or default)
        except (TypeError, ValueError):
            return default

    def _str_field(name: str, default: str = "") -> str:
        return str(form.get(name) or default).strip()

    name = _str_field("name", "Untitled Event")
    event_type = _str_field("event_type", "cup").lower()
    entry_mode = _str_field("entry_mode", "open").lower()
    scoring_mode = _str_field("scoring_mode", "normal").lower()
    if event_type not in ("cup", "scrim", "bracket", "qualifier"):
        event_type = "cup"
    if entry_mode not in ("open", "pr_limited", "division"):
        entry_mode = "open"
    if scoring_mode not in ("normal", "placement_only", "coins"):
        scoring_mode = "normal"
    coins_cup = scoring_mode == "coins"

    required_division_id = None
    division_name = _str_field("division")
    if entry_mode == "division" and division_name:
        match = next(
            (
                d
                for d in get_divisions()
                if d["name"].lower() == division_name.lower()
            ),
            None,
        )
        required_division_id = match["id"] if match else None

    event_id = create_event_record(
        name=name,
        status="setup",
        channel_id=_str_field("channel_id"),
        signup_channel_id=_str_field("signup_channel_id"),
        updates_channel_id=_str_field("updates_channel_id"),
        dispatch_channel_id=_str_field("dispatch_channel_id"),
        team_size=_int_field("team_size", 1),
        total_games=_int_field("total_games"),
        max_players=_int_field("max_players", 100),
        region=_str_field("region", "EU"),
        event_format=_str_field("event_format", "ZoneWars"),
        point_kill=_int_field("point_kill", 1),
        point_win=_int_field("point_win", 5),
        placement_scale=_str_field("placement_scale", "[10,8,6,4,2,1]"),
        event_type=event_type,
        entry_mode=entry_mode,
        pr_cap=_int_field("pr_cap") or None,
        required_division_id=required_division_id,
        scoring_mode=scoring_mode,
        awards_pr=0 if coins_cup else (1 if form.get("awards_pr") == "1" else 0),
        coins_enabled=1 if coins_cup else 0,
    )
    if _is_ajax(request):
        return JSONResponse({"ok": True, "event_id": event_id})
    return RedirectResponse(url=f"/event/{event_id}", status_code=302)


@app.post("/event/{event_id}/room-code")
async def set_room_code(event_id: int, request: Request, user: dict = Depends(get_current_user)):
    form = await request.form()
    room_code = form.get("room_code", "")
    execute("UPDATE vtx_events SET room_code = %s WHERE id = %s", (room_code, event_id))
    return RedirectResponse(url=f"/event/{event_id}", status_code=302)


@app.post("/event/{event_id}/assign-points")
async def assign_points(event_id: int, request: Request, user: dict = Depends(get_current_user)):
    form = await request.form()
    game_number = int(form.get("game_number", 1))
    player_id = int(form.get("player_id", 0))
    points = int(form.get("points", 0))

    game = query_one(
        "SELECT * FROM vtx_games WHERE event_id = %s AND game_number = %s",
        (event_id, game_number),
    )
    if game:
        execute(
            "UPDATE vtx_game_players SET points = points + %s "
            "WHERE game_id = %s AND player_id = %s",
            (points, game["id"], player_id),
        )
    return RedirectResponse(url=f"/event/{event_id}", status_code=302)


@app.post("/event/{event_id}/dq")
async def dq_player(event_id: int, request: Request, user: dict = Depends(get_current_user)):
    form = await request.form()
    game_number = int(form.get("game_number", 1))
    player_id = int(form.get("player_id", 0))

    game = query_one(
        "SELECT * FROM vtx_games WHERE event_id = %s AND game_number = %s",
        (event_id, game_number),
    )
    if game:
        execute(
            "UPDATE vtx_game_players SET is_disqualified = 1, points = 0 "
            "WHERE game_id = %s AND player_id = %s",
            (game["id"], player_id),
        )
    return RedirectResponse(url=f"/event/{event_id}", status_code=302)


@app.post("/event/{event_id}/create-lobby")
async def create_lobby(event_id: int, request: Request, user: dict = Depends(get_current_user)):
    form = await request.form()
    name = form.get("name", "Lobby")
    execute("INSERT INTO vtx_lobbies (event_id, name) VALUES (%s, %s)", (event_id, name))
    return RedirectResponse(url=f"/event/{event_id}", status_code=302)


@app.post("/lobby/{lobby_id}/set-code")
async def set_lobby_code(lobby_id: int, request: Request, user: dict = Depends(get_current_user)):
    form = await request.form()
    room_code = form.get("room_code", "")
    execute("UPDATE vtx_lobbies SET room_code = %s WHERE id = %s", (room_code, lobby_id))
    lobby = query_one("SELECT event_id FROM vtx_lobbies WHERE id = %s", (lobby_id,))
    eid = lobby["event_id"] if lobby else 0
    return RedirectResponse(url=f"/event/{eid}", status_code=302)


@app.post("/event/{event_id}/status")
async def update_status(event_id: int, request: Request, user: dict = Depends(get_current_user)):
    form = await request.form()
    status = form.get("status", "setup")
    execute("UPDATE vtx_events SET status = %s WHERE id = %s", (status, event_id))
    return RedirectResponse(url=f"/event/{event_id}", status_code=302)


@app.post("/event/{event_id}/ajax/quick-kill")
async def ajax_quick_kill(event_id: int, request: Request, user: dict = Depends(get_current_user)):
    from fastapi.responses import JSONResponse
    form = await request.form()
    discord_id = form.get("discord_id", "").strip()
    game_number = int(form.get("game_number", 1))

    ev = get_event(event_id)
    if not ev:
        return JSONResponse({"error": "Event not found"}, status_code=404)

    if not discord_id:
        return JSONResponse({"error": "No player specified"}, status_code=400)

    player = query_one("SELECT id FROM vtx_players WHERE discord_id = %s", (discord_id,))
    if not player:
        return JSONResponse({"error": "Player not found in DB"}, status_code=404)

    player_id = player["id"]
    pts = ev.get("point_kill", 1)
    game = query_one(
        "SELECT * FROM vtx_games WHERE event_id = %s AND game_number = %s",
        (event_id, game_number),
    )
    if not game:
        game_id = create_game_record(event_id, game_number)
    else:
        game_id = game["id"]

    existing = query_one(
        "SELECT * FROM vtx_game_players WHERE game_id = %s AND player_id = %s",
        (game_id, player_id),
    )
    if existing:
        execute(
            "UPDATE vtx_game_players SET kills = kills + 1, points = points + %s "
            "WHERE game_id = %s AND player_id = %s",
            (pts, game_id, player_id),
        )
    else:
        execute(
            "INSERT INTO vtx_game_players (game_id, player_id, kills, points) VALUES (%s, %s, 1, %s)",
            (game_id, player_id, pts),
        )

    p = query_one("SELECT username FROM vtx_players WHERE id = %s", (player_id,))
    return JSONResponse({
        "ok": True,
        "player": p["username"] if p else "?",
        "points_added": pts,
    })


@app.post("/event/{event_id}/ajax/quick-win")
async def ajax_quick_win(event_id: int, request: Request, user: dict = Depends(get_current_user)):
    from fastapi.responses import JSONResponse
    form = await request.form()
    discord_id = form.get("discord_id", "").strip()
    game_number = int(form.get("game_number", 1))

    ev = get_event(event_id)
    if not ev:
        return JSONResponse({"error": "Event not found"}, status_code=404)

    if not discord_id:
        return JSONResponse({"error": "No player specified"}, status_code=400)

    player = query_one("SELECT id FROM vtx_players WHERE discord_id = %s", (discord_id,))
    if not player:
        return JSONResponse({"error": "Player not found in DB"}, status_code=404)

    player_id = player["id"]
    pts = ev.get("point_win", 5)
    game = query_one(
        "SELECT * FROM vtx_games WHERE event_id = %s AND game_number = %s",
        (event_id, game_number),
    )
    if not game:
        game_id = create_game_record(event_id, game_number)
    else:
        game_id = game["id"]

    existing = query_one(
        "SELECT * FROM vtx_game_players WHERE game_id = %s AND player_id = %s",
        (game_id, player_id),
    )
    if existing:
        execute(
            "UPDATE vtx_game_players SET points = points + %s, placement = 1 "
            "WHERE game_id = %s AND player_id = %s",
            (pts, game_id, player_id),
        )
    else:
        execute(
            "INSERT INTO vtx_game_players (game_id, player_id, points, placement) "
            "VALUES (%s, %s, %s, 1)",
            (game_id, player_id, pts),
        )

    p = query_one("SELECT username FROM vtx_players WHERE id = %s", (player_id,))
    return JSONResponse({
        "ok": True,
        "player": p["username"] if p else "?",
        "points_added": pts,
    })


@app.get("/event/{event_id}/ajax/placement-status")
async def ajax_placement_status(event_id: int, game_number: int = 1, user: dict = Depends(get_current_user)):
    from fastapi.responses import JSONResponse
    ev = get_event(event_id)
    if not ev:
        return JSONResponse({"error": "Event not found"}, status_code=404)
    game = query_one(
        "SELECT * FROM vtx_games WHERE event_id = %s AND game_number = %s",
        (event_id, game_number),
    )
    if not game:
        return JSONResponse({"ok": True, "projected": []})

    team_size = ev.get("team_size", 1) or 1
    registrations = query(
        "SELECT * FROM vtx_registrations WHERE event_id = %s AND status = 'confirmed'",
        (event_id,),
    )
    if team_size >= 2:
        participants = [r["discord_id"] for r in registrations if r.get("team_members")]
    else:
        participants = [r["discord_id"] for r in registrations]

    placed_map = {
        str(row["discord_id"]): row["placement"]
        for row in query(
            "SELECT pl.discord_id, gp.placement FROM vtx_game_players gp "
            "JOIN vtx_players pl ON pl.id = gp.player_id "
            "WHERE gp.game_id = %s AND gp.placement IS NOT NULL",
            (game["id"],),
        )
    }
    projected = []
    live_rank = 1
    for pid in participants:
        if pid in placed_map:
            projected.append({"discord_id": pid, "placement": placed_map[pid], "projected": None})
        else:
            projected.append({"discord_id": pid, "placement": None, "projected": live_rank})
            live_rank += 1
    return JSONResponse({"ok": True, "projected": projected})


@app.post("/event/{event_id}/ajax/qualify")
async def ajax_qualify(event_id: int, request: Request, user: dict = Depends(get_current_user)):
    from fastapi.responses import JSONResponse
    form = await request.form()
    discord_id = form.get("discord_id", "").strip()
    if not discord_id:
        return JSONResponse({"error": "No player specified"}, status_code=400)
    ev = get_event(event_id)
    if not ev:
        return JSONResponse({"error": "Event not found"}, status_code=404)
    player = query_one("SELECT username FROM vtx_players WHERE discord_id = %s", (discord_id,))
    if not player:
        return JSONResponse({"error": "Player not found"}, status_code=404)

    existing = query_one(
        "SELECT * FROM vtx_event_qualifiers WHERE event_id = %s AND discord_id = %s",
        (event_id, discord_id),
    )
    if existing:
        remove_event_qualifier(event_id, discord_id)
        qualified = False
    else:
        reg = query_one(
            "SELECT * FROM vtx_registrations WHERE event_id = %s AND discord_id = %s",
            (event_id, discord_id),
        )
        team_members = reg.get("team_members") if reg else None
        add_event_qualifier(event_id, discord_id, player["username"], team_members)
        qualified = True
    log_bot_action(event_id, "qualify", f"{'Qualified' if qualified else 'Unqualified'} {player['username']}", user.get("discord_id", "dashboard"))
    return JSONResponse({"ok": True, "qualified": qualified})


@app.post("/event/{event_id}/ajax/eliminate")
async def ajax_eliminate(event_id: int, request: Request, user: dict = Depends(get_current_user)):
    from fastapi.responses import JSONResponse
    form = await request.form()
    discord_id = form.get("discord_id", "").strip()
    game_number = int(form.get("game_number", 1))

    if not discord_id:
        return JSONResponse({"error": "No player specified"}, status_code=400)

    ev = get_event(event_id)
    if not ev:
        return JSONResponse({"error": "Event not found"}, status_code=404)

    player = query_one("SELECT id FROM vtx_players WHERE discord_id = %s", (discord_id,))
    if not player:
        return JSONResponse({"error": "Player not found in DB"}, status_code=404)

    game = query_one(
        "SELECT * FROM vtx_games WHERE event_id = %s AND game_number = %s",
        (event_id, game_number),
    )
    if game and game["status"] == "completed":
        return JSONResponse({"error": "Match is already ended — placements are locked."}, status_code=409)

    if not game:
        game_id = create_game_record(event_id, game_number)
    else:
        game_id = game["id"]

    team_size = ev.get("team_size", 1) or 1
    registrations = query(
        "SELECT * FROM vtx_registrations WHERE event_id = %s AND status = 'confirmed'",
        (event_id,),
    )
    if team_size >= 2:
        participants = [r["discord_id"] for r in registrations if r.get("team_members")]
    else:
        participants = [r["discord_id"] for r in registrations]
    total_participants = len(participants)

    existing = query_one(
        "SELECT * FROM vtx_game_players WHERE game_id = %s AND player_id = %s",
        (game_id, player["id"]),
    )

    undo = existing and existing.get("placement") is not None
    if undo:
        execute(
            "UPDATE vtx_game_players SET placement = NULL WHERE game_id = %s AND player_id = %s",
            (game_id, player["id"]),
        )
        placed_after = query(
            "SELECT id FROM vtx_game_players WHERE game_id = %s AND placement IS NOT NULL "
            "ORDER BY placement DESC",
            (game_id,),
        )
        for i, row in enumerate(placed_after):
            execute(
                "UPDATE vtx_game_players SET placement = %s WHERE id = %s",
                (total_participants - i, row["id"]),
            )
        placement = None
    else:
        already_eliminated = query(
            "SELECT * FROM vtx_game_players WHERE game_id = %s AND placement IS NOT NULL",
            (game_id,),
        )
        placement = total_participants - len(already_eliminated)
        if existing:
            execute(
                "UPDATE vtx_game_players SET placement = %s WHERE game_id = %s AND player_id = %s",
                (placement, game_id, player["id"]),
            )
        else:
            execute(
                "INSERT INTO vtx_game_players (game_id, player_id, placement, points) "
                "VALUES (%s, %s, %s, 0)",
                (game_id, player["id"], placement),
            )

    if placement is not None:
        apply_placement_points(game_id, event_id)

    placed_map = {
        str(row["discord_id"]): row["placement"]
        for row in query(
            "SELECT pl.discord_id, gp.placement FROM vtx_game_players gp "
            "JOIN vtx_players pl ON pl.id = gp.player_id "
            "WHERE gp.game_id = %s AND gp.placement IS NOT NULL",
            (game_id,),
        )
    }
    projected = []
    live_rank = 1
    for pid in participants:
        if pid in placed_map:
            projected.append({"discord_id": pid, "placement": placed_map[pid], "projected": None})
        else:
            projected.append({"discord_id": pid, "placement": None, "projected": live_rank})
            live_rank += 1

    p = query_one("SELECT username FROM vtx_players WHERE id = %s", (player["id"],))
    return JSONResponse({
        "ok": True,
        "player": p["username"] if p else "?",
        "placement": placement,
        "placed": not undo,
        "projected": projected,
    })


@app.post("/event/{event_id}/ajax/mark-teammate-eliminated")
async def ajax_mark_teammate_eliminated(event_id: int, request: Request, user: dict = Depends(get_current_user)):
    from fastapi.responses import JSONResponse
    from database import mark_teammate_eliminated as db_mark_eliminated, init_team_members

    form = await request.form()
    discord_id = form.get("discord_id", "").strip()
    game_number = int(form.get("game_number", 1))

    if not discord_id:
        return JSONResponse({"error": "No player specified"}, status_code=400)

    ev = get_event(event_id)
    if not ev:
        return JSONResponse({"error": "Event not found"}, status_code=404)

    game = query_one(
        "SELECT * FROM vtx_games WHERE event_id = %s AND game_number = %s",
        (event_id, game_number),
    )
    if not game:
        game_id = create_game_record(event_id, game_number)
    else:
        game_id = game["id"]

    init_team_members(game_id, event_id)

    result = db_mark_eliminated(game_id, discord_id)
    if not result["ok"]:
        return JSONResponse({"error": result["error"]}, status_code=400)

    return JSONResponse({
        "ok": True,
        "all_eliminated": result["all_eliminated"],
        "team_lead_id": result["team_lead_id"],
    })


@app.post("/event/{event_id}/ajax/update-points")
async def ajax_update_points(
    event_id: int, request: Request, user: dict = Depends(get_current_user)
):
    from fastapi.responses import JSONResponse
    form = await request.form()
    point_kill = form.get("point_kill")
    point_win = form.get("point_win")
    placement_scale = form.get("placement_scale")

    updates = []
    params = []
    if point_kill is not None:
        updates.append("point_kill = %s")
        params.append(int(point_kill))
    if point_win is not None:
        updates.append("point_win = %s")
        params.append(int(point_win))
    if placement_scale is not None:
        updates.append("placement_scale = %s")
        params.append(placement_scale)

    if updates:
        params.append(event_id)
        execute(f"UPDATE vtx_events SET {', '.join(updates)} WHERE id = %s", tuple(params))
    return JSONResponse({"ok": True})


@app.post("/event/{event_id}/ajax/dq-player")
async def ajax_dq_player(event_id: int, request: Request, user: dict = Depends(get_current_user)):
    from fastapi.responses import JSONResponse
    form = await request.form()
    discord_id = form.get("discord_id", "").strip()
    reason = form.get("reason", "No reason given").strip()

    if not discord_id:
        return JSONResponse({"error": "No player specified"}, status_code=400)

    player = query_one("SELECT id FROM vtx_players WHERE discord_id = %s", (discord_id,))
    if not player:
        return JSONResponse({"error": "Player not found in DB"}, status_code=404)

    player_id = player["id"]
    games = query(
        "SELECT id FROM vtx_games WHERE event_id = %s AND status IN ('in_progress', 'waiting')",
        (event_id,),
    )
    for g in games:
        existing = query_one(
            "SELECT id FROM vtx_game_players WHERE game_id = %s AND player_id = %s",
            (g["id"], player_id),
        )
        if existing:
            execute(
                "UPDATE vtx_game_players SET is_disqualified = 1, points = 0 "
                "WHERE game_id = %s AND player_id = %s",
                (g["id"], player_id),
            )
        else:
            execute(
                "INSERT INTO vtx_game_players (game_id, player_id, is_disqualified, points) "
                "VALUES (%s, %s, 1, 0)",
                (g["id"], player_id),
            )

    queue_command("dq_notify", {
        "event_id": event_id,
        "discord_id": discord_id,
        "reason": reason,
    })

    p = query_one("SELECT username FROM vtx_players WHERE id = %s", (player_id,))
    return JSONResponse({"ok": True, "player": p["username"] if p else "?"})


@app.post("/event/{event_id}/ajax/remove-points")
async def ajax_remove_points(event_id: int, request: Request, user: dict = Depends(get_current_user)):
    from fastapi.responses import JSONResponse
    form = await request.form()
    discord_id = form.get("discord_id", "").strip()
    points = int(form.get("points", 1))

    if not discord_id:
        return JSONResponse({"error": "No player specified"}, status_code=400)

    player = query_one("SELECT id FROM vtx_players WHERE discord_id = %s", (discord_id,))
    if not player:
        return JSONResponse({"error": "Player not found in DB"}, status_code=404)

    player_id = player["id"]
    games = query(
        "SELECT id FROM vtx_games WHERE event_id = %s AND status IN ('in_progress', 'waiting')",
        (event_id,),
    )
    for g in games:
        existing = query_one(
            "SELECT id, points FROM vtx_game_players WHERE game_id = %s AND player_id = %s",
            (g["id"], player_id),
        )
        if existing:
            new_pts = max(0, existing["points"] - points)
            execute(
                "UPDATE vtx_game_players SET points = %s WHERE game_id = %s AND player_id = %s",
                (new_pts, g["id"], player_id),
            )

    p = query_one("SELECT username FROM vtx_players WHERE id = %s", (player_id,))
    return JSONResponse({"ok": True, "player": p["username"] if p else "?"})


@app.post("/event/{event_id}/ajax/dispatch-kill")
async def ajax_dispatch_kill(
    event_id: int, request: Request, user: dict = Depends(get_current_user)
):
    form = await request.form()
    killer_id = form.get("killer_id", "").strip()
    victim_id = form.get("victim_id", "").strip()
    weapon = form.get("weapon", "")

    if not killer_id or not victim_id:
        return JSONResponse({"error": "Select both killer and victim"}, status_code=400)
    if killer_id == victim_id:
        return JSONResponse({"error": "Killer and victim cannot be the same player"}, status_code=400)

    queue_command("log_kill", {
        "event_id": event_id,
        "killer_id": killer_id,
        "victim_id": victim_id,
        "weapon": weapon,
    })

    ev = get_event(event_id)
    k_name = "?"
    v_name = "?"
    p = query_one("SELECT username FROM vtx_players WHERE discord_id = %s", (killer_id,))
    if p:
        k_name = p["username"]
    p = query_one("SELECT username FROM vtx_players WHERE discord_id = %s", (victim_id,))
    if p:
        v_name = p["username"]

    return JSONResponse({"ok": True, "killer": k_name, "victim": v_name})


@app.post("/event/{event_id}/add-player")
async def add_player(event_id: int, request: Request, user: dict = Depends(get_current_user)):
    form = await request.form()
    ev = get_event(event_id)
    team_size = ev["team_size"] if ev else 1

    players_to_add = []
    did = form.get("discord_id", "").strip()
    uname = form.get("username", "").strip()
    if did and uname:
        players_to_add.append((did, uname))

    if team_size >= 2:
        did2 = form.get("discord_id_2", "").strip()
        uname2 = form.get("username_2", "").strip()
        if did2 and uname2:
            players_to_add.append((did2, uname2))

    if team_size >= 3:
        did3 = form.get("discord_id_3", "").strip()
        uname3 = form.get("username_3", "").strip()
        if did3 and uname3:
            players_to_add.append((did3, uname3))

    if players_to_add:
        for did, uname in players_to_add:
            upsert_player(did, uname)

        if team_size >= 2 and len(players_to_add) >= 2:
            lead_id, lead_name = players_to_add[0]
            team_ids = ",".join(p[0] for p in players_to_add[1:])
            existing = query_one(
                "SELECT id FROM vtx_registrations WHERE event_id = %s AND discord_id = %s",
                (event_id, lead_id),
            )
            if not existing:
                execute(
                    "INSERT INTO vtx_registrations "
                    "(event_id, discord_id, username, team_members, status) "
                    "VALUES (%s, %s, %s, %s, 'confirmed')",
                    (event_id, lead_id, lead_name, team_ids),
                )
            else:
                execute(
                    "UPDATE vtx_registrations SET team_members = %s WHERE id = %s",
                    (team_ids, existing["id"]),
                )
            for did, uname in players_to_add[1:]:
                existing2 = query_one(
                    "SELECT id FROM vtx_registrations WHERE event_id = %s AND discord_id = %s",
                    (event_id, did),
                )
                if not existing2:
                    execute(
                        "INSERT INTO vtx_registrations "
                        "(event_id, discord_id, username, status) "
                        "VALUES (%s, %s, %s, 'confirmed')",
                        (event_id, did, uname),
                    )
        else:
            lead_id, lead_name = players_to_add[0]
            existing = query_one(
                "SELECT id FROM vtx_registrations WHERE event_id = %s AND discord_id = %s",
                (event_id, lead_id),
            )
            if not existing:
                execute(
                    "INSERT INTO vtx_registrations "
                    "(event_id, discord_id, username, status) "
                    "VALUES (%s, %s, %s, 'confirmed')",
                    (event_id, lead_id, lead_name),
                )

    return RedirectResponse(url=f"/event/{event_id}", status_code=302)


@app.post("/event/{event_id}/open-registration")
async def open_registration(event_id: int, request: Request, user: dict = Depends(get_current_user)):
    queue_command("open_registration", {"event_id": event_id})
    if _is_ajax(request):
        return JSONResponse({"ok": True})
    return RedirectResponse(url=f"/event/{event_id}", status_code=302)


@app.post("/event/{event_id}/close-registration")
async def close_registration(event_id: int, request: Request, user: dict = Depends(get_current_user)):
    queue_command("close_registration", {"event_id": event_id})
    if _is_ajax(request):
        return JSONResponse({"ok": True})
    return RedirectResponse(url=f"/event/{event_id}", status_code=302)


@app.post("/event/{event_id}/dispatch")
async def dispatch(event_id: int, request: Request, user: dict = Depends(get_current_user)):
    form = await request.form()
    queue_command("dispatch", {
        "event_id": event_id,
        "code": form.get("code", ""),
        "game_number": int(form["game_number"]) if form.get("game_number") else None,
        "dm_players": form.get("dm_players") == "on",
        "start_time": form.get("start_time", "TBD"),
    })
    if _is_ajax(request):
        return JSONResponse({"ok": True})
    return RedirectResponse(url=f"/event/{event_id}", status_code=302)


@app.post("/event/{event_id}/announce")
async def announce(event_id: int, request: Request, user: dict = Depends(get_current_user)):
    form = await request.form()
    queue_command("announce", {
        "event_id": event_id,
        "start_time": form.get("start_time", "TBD"),
        "point_kill": int(form.get("point_kill", 1)),
        "point_win": int(form.get("point_win", 5)),
        "stage": form.get("stage", "Main"),
    })
    return RedirectResponse(url=f"/event/{event_id}", status_code=302)


@app.post("/event/{event_id}/announce-signups")
async def announce_signups(event_id: int, request: Request, user: dict = Depends(get_current_user)):
    form = await request.form()
    queue_command("announce_signups", {
        "event_id": event_id,
        "start_time": form.get("start_time", "TBD"),
        "signup_channel": form.get("signup_channel", "sign-up"),
    })
    return RedirectResponse(url=f"/event/{event_id}", status_code=302)


@app.post("/event/{event_id}/announce-end")
async def announce_end(event_id: int, request: Request, user: dict = Depends(get_current_user)):
    form = await request.form()
    queue_command("announce_end", {
        "event_id": event_id,
        "winner": form.get("winner", ""),
        "runner_up": form.get("runner_up", ""),
        "next_event": form.get("next_event", ""),
    })
    return RedirectResponse(url=f"/event/{event_id}", status_code=302)


@app.post("/event/{event_id}/dm-players")
async def dm_players(event_id: int, request: Request, user: dict = Depends(get_current_user)):
    form = await request.form()
    queue_command("dm_players", {
        "event_id": event_id,
        "code": form.get("code", ""),
        "game_number": int(form.get("game_number", 1)),
        "start_time": form.get("start_time", "TBD"),
    })
    return RedirectResponse(url=f"/event/{event_id}", status_code=302)


@app.post("/event/{event_id}/start-game")
async def start_game(event_id: int, request: Request, user: dict = Depends(get_current_user)):
    form = await request.form()
    queue_command("start_game", {
        "event_id": event_id,
        "game_number": int(form.get("game_number", 1)),
        "room_code": form.get("room_code", ""),
    })
    if _is_ajax(request):
        return JSONResponse({"ok": True})
    return RedirectResponse(url=f"/event/{event_id}", status_code=302)


@app.post("/event/{event_id}/end-game")
async def end_game(event_id: int, request: Request, user: dict = Depends(get_current_user)):
    form = await request.form()
    queue_command("end_game", {
        "event_id": event_id,
        "game_number": int(form.get("game_number", 1)),
    })
    return RedirectResponse(url=f"/event/{event_id}", status_code=302)


@app.post("/event/{event_id}/start-live-feed")
async def start_live_feed(event_id: int, request: Request, user: dict = Depends(get_current_user)):
    form = await request.form()
    queue_command("start_live_feed", {
        "event_id": event_id,
        "game_number": int(form.get("game_number", 1)),
    })
    return RedirectResponse(url=f"/event/{event_id}", status_code=302)


@app.post("/event/{event_id}/log-kill")
async def log_kill(event_id: int, request: Request, user: dict = Depends(get_current_user)):
    form = await request.form()
    queue_command("log_kill", {
        "event_id": event_id,
        "killer_id": form.get("killer_id", ""),
        "victim_id": form.get("victim_id", ""),
        "weapon": form.get("weapon", ""),
    })
    return RedirectResponse(url=f"/event/{event_id}", status_code=302)


@app.post("/event/{event_id}/end-match")
async def end_match(event_id: int, request: Request, user: dict = Depends(get_current_user)):
    form = await request.form()
    queue_command("end_match", {
        "event_id": event_id,
        "game_number": int(form.get("game_number", 1)),
    })
    if _is_ajax(request):
        return JSONResponse({"ok": True})
    return RedirectResponse(url=f"/event/{event_id}", status_code=302)


@app.post("/event/{event_id}/create-session")
async def create_session_route(event_id: int, request: Request, user: dict = Depends(get_current_user)):
    sess = create_session(event_id)
    log_bot_action(event_id, "create_session", f"Session {sess['session_number']} created")
    if _is_ajax(request):
        return JSONResponse({"ok": True, "session_number": sess["session_number"]})
    return RedirectResponse(url=f"/event/{event_id}", status_code=302)


@app.post("/event/{event_id}/session/start")
async def start_session(event_id: int, request: Request, user: dict = Depends(get_current_user)):
    form = await request.form()
    queue_command("start_session", {
        "event_id": event_id,
        "room_code": form.get("room_code", ""),
    })
    if _is_ajax(request):
        return JSONResponse({"ok": True})
    return RedirectResponse(url=f"/event/{event_id}", status_code=302)


@app.post("/event/{event_id}/session/end-match")
async def end_session_match(event_id: int, request: Request, user: dict = Depends(get_current_user)):
    form = await request.form()
    queue_command("end_match", {
        "event_id": event_id,
        "game_number": int(form.get("game_number", 1)),
    })
    if _is_ajax(request):
        return JSONResponse({"ok": True})
    return RedirectResponse(url=f"/event/{event_id}", status_code=302)


@app.post("/event/{event_id}/session/end")
async def end_session(event_id: int, request: Request, user: dict = Depends(get_current_user)):
    queue_command("end_session", {"event_id": event_id})
    if _is_ajax(request):
        return JSONResponse({"ok": True})
    return RedirectResponse(url=f"/event/{event_id}", status_code=302)


@app.get("/api/players/search")
async def players_search(q: str = ""):
    q = (q or "").strip()
    if not q:
        return JSONResponse({"results": []})
    like = f"%{q}%"
    rows = query(
        "SELECT discord_id, username, game_username, pr FROM vtx_players "
        "WHERE username LIKE %s OR game_username LIKE %s "
        "ORDER BY pr DESC LIMIT 8",
        (like, like),
    )
    return JSONResponse({"results": list(rows)})


@app.get("/search", response_class=HTMLResponse)
async def search_page(request: Request):
    results = []
    q = request.query_params.get("q", "").strip()
    if q:
        like = f"%{q}%"
        results = query(
            "SELECT discord_id, username, game_username, pr FROM vtx_players "
            "WHERE username LIKE %s OR game_username LIKE %s "
            "ORDER BY pr DESC LIMIT 50",
            (like, like),
        )
    return templates.TemplateResponse(
        request, "search.html", {"q": q, "results": results}
    )


@app.get("/leaderboard", response_class=HTMLResponse)
async def leaderboard_page(request: Request):
    from database import get_rank_for_pr, get_players_leaderboard

    board = get_players_leaderboard()
    tiers = {}
    for row in board:
        t = get_rank_for_pr(row.get("pr") or 0)
        if t:
            tiers.setdefault(t["name"], []).append(row)
    return templates.TemplateResponse(
        request,
        "leaderboard.html",
        {"tiers": tiers, "season": get_season()},
    )


@app.get("/account/{discord_id}", response_class=HTMLResponse)
async def account_page(request: Request, discord_id: str):
    from database import get_player_profile

    profile = get_player_profile(discord_id)
    if not profile:
        raise HTTPException(status_code=404, detail="Player not found")
    return templates.TemplateResponse(
        request, "account.html", {"profile": profile, "player": profile["player"]}
    )


@app.get("/compare", response_class=HTMLResponse)
async def compare_page(request: Request):
    from database import get_player_profile

    p1 = request.query_params.get("p1", "").strip()
    p2 = request.query_params.get("p2", "").strip()
    left = get_player_profile(p1) if p1 else None
    right = get_player_profile(p2) if p2 else None
    players = query(
        "SELECT discord_id, username, game_username FROM vtx_players "
        "ORDER BY username ASC"
    )
    return templates.TemplateResponse(
        request,
        "compare.html",
        {"players": players, "left": left, "right": right, "p1": p1, "p2": p2},
    )


@app.get("/daddy", response_class=HTMLResponse)
async def daddy_page(request: Request):
    return templates.TemplateResponse(request, "daddy.html", {})


@app.post("/event/{event_id}/end-event")
async def end_event(event_id: int, request: Request, user: dict = Depends(get_current_user)):
    queue_command("end_event", {"event_id": event_id})
    if _is_ajax(request):
        return JSONResponse({"ok": True})
    return RedirectResponse(url=f"/event/{event_id}", status_code=302)


def start_dashboard() -> None:
    import uvicorn
    uvicorn.run(
        "dashboard:app",
        host="0.0.0.0",
        port=settings.dashboard_port,
        reload=False,
    )


if __name__ == "__main__":
    start_dashboard()