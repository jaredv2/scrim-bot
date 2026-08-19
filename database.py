from __future__ import annotations

import json
import re
import threading
import time
from contextlib import contextmanager
from pathlib import Path

import psycopg2
from psycopg2 import pool
from psycopg2.extras import RealDictCursor

from config import settings

_db_epoch = 0
_db_epoch_lock = threading.Lock()

INIT_LOCK = threading.Lock()

RESTART_FLAG_PREFIX = "."

_conn_pool: pool.ThreadedConnectionPool | None = None


def _get_pool() -> pool.ThreadedConnectionPool:
    global _conn_pool
    if not settings.supabase_db_url:
        raise RuntimeError("SUPABASE_DB_URL is not set in .env")
    if _conn_pool is None:
        last_exc: Exception | None = None
        for attempt in range(5):
            try:
                _conn_pool = pool.ThreadedConnectionPool(
                    1,
                    10,
                    settings.supabase_db_url,
                    connect_timeout=15,
                    keepalives=1,
                )
                break
            except Exception as exc:  # transient pooler/network blips
                last_exc = exc
                time.sleep(2 * (attempt + 1))
        if _conn_pool is None:
            raise RuntimeError(f"Could not connect to Supabase: {last_exc}") from last_exc
    return _conn_pool


def _close_all_connections() -> None:
    """Close every pooled Postgres connection (thread-safe)."""
    global _conn_pool
    if _conn_pool is not None:
        try:
            _conn_pool.closeall()
        except Exception:
            pass
        _conn_pool = None


def _restart_flag_path() -> Path:
    """Legacy helper kept for API compatibility. Postgres has no local file to
    swap, so dashboard restore/backup are no-ops."""
    return Path(settings.database_path).with_name(
        RESTART_FLAG_PREFIX + Path(settings.database_path).name + ".restart"
    )


def _bump_epoch() -> None:
    """Recycle every pooled connection (there is no local file to reload)."""
    global _db_epoch
    with _db_epoch_lock:
        _db_epoch += 1
    _close_all_connections()


def reload_db_if_needed() -> bool:
    """No-op under Postgres: there is no local database file to swap."""
    return False


def reload_db_now() -> None:
    """No-op under Postgres."""


def mark_db_restored() -> None:
    """No-op under Postgres."""


_SCHEMA_FILE = Path(__file__).parent / "migrations" / "supabase_schema.sql"


def _get_conn():
    """Deprecated helper kept for compatibility; prefer get_db()."""
    return _get_pool().getconn()


def init_db() -> None:
    """Create all vtx_* tables and seed rank tiers (idempotent)."""
    with INIT_LOCK:
        with get_db() as conn:
            _exec(conn,_SCHEMA_FILE.read_text(encoding="utf-8"))


def get_rank_tiers() -> list[dict]:
    return query(
        "SELECT * FROM vtx_rank_tiers ORDER BY pr_min DESC"
    )


def get_rank_for_pr(pr: int) -> dict | None:
    tiers = query("SELECT * FROM vtx_rank_tiers ORDER BY pr_min DESC")
    for tier in tiers:
        if pr >= tier["pr_min"]:
            return tier
    return tiers[-1] if tiers else None


@contextmanager
def get_db():
    pool_ = _get_pool()
    conn = pool_.getconn()
    conn.autocommit = False
    conn.cursor_factory = RealDictCursor
    try:
        yield conn
        conn.commit()
    except (psycopg2.OperationalError, psycopg2.InterfaceError) as exc:
        # The pooler may have dropped this connection; discard it so the pool
        # creates a fresh one on the next request, then re-raise.
        try:
            pool_.putconn(conn, close=True)
        except Exception:
            pass
        raise exc
    except Exception:
        conn.rollback()
        raise
    finally:
        if not conn.closed:
            try:
                pool_.putconn(conn)
            except Exception:
                pass


def _exec(conn, sql: str, params: tuple = ()):
    """Execute on a fresh RealDict cursor; returns the cursor (RealDictCursor.execute
    returns None, unlike the base psycopg2 cursor)."""
    cur = conn.cursor()
    cur.execute(sql, params)
    return cur


_NO_ID_TABLES = {"vtx_kv_store", "vtx_user_messages", "vtx_invite_coins"}


def query(sql: str, params: tuple = ()) -> list[dict]:
    with get_db() as conn:
        with conn.cursor(cursor_factory=RealDictCursor) as cur:
            cur.execute(sql, params)
            return [dict(r) for r in cur.fetchall()]


def query_one(sql: str, params: tuple = ()) -> dict | None:
    with get_db() as conn:
        with conn.cursor(cursor_factory=RealDictCursor) as cur:
            cur.execute(sql, params)
            row = cur.fetchone()
            return dict(row) if row else None


def execute(sql: str, params: tuple = ()) -> int:
    """Run a statement. For plain INSERTs returns the new row id (mirrors the
    old sqlite lastrowid); otherwise returns the affected row count."""
    with get_db() as conn:
        with conn.cursor() as cur:
            if sql.lstrip()[:6].upper() == "INSERT" and "RETURNING" not in sql.upper():
                m = re.search(r"\bINSERT\s+INTO\s+(\w+)", sql, re.I)
                table = m.group(1).lower() if m else ""
                if table not in _NO_ID_TABLES:
                    cur.execute(sql + " RETURNING id", params)
                    row = cur.fetchone()
                    return row["id"] if row else 0
            cur.execute(sql, params)
            return cur.rowcount


def execute_many(sql: str, params_list: list[tuple]) -> None:
    with get_db() as conn:
        with conn.cursor() as cur:
            cur.executemany(sql, params_list)



def upsert_player(discord_id: str, username: str) -> dict:
    existing = query_one(
        "SELECT * FROM vtx_players WHERE discord_id = %s", (discord_id,)
    )
    if existing:
        execute(
            "UPDATE vtx_players SET username = %s WHERE discord_id = %s",
            (username, discord_id),
        )
        return query_one("SELECT * FROM vtx_players WHERE discord_id = %s", (discord_id,))
    player_id = execute(
        "INSERT INTO vtx_players (discord_id, username) VALUES (%s, %s)",
        (discord_id, username),
    )
    return query_one("SELECT * FROM vtx_players WHERE id = %s", (player_id,))


PLAYER_EDITABLE_FIELDS = {
    "username": "discord name",
    "game_username": "in-game name",
    "game_id": "Fortnite game id",
    "country": "country",
    "region": "region",
}


def update_player_fields(discord_id: str, changes: dict) -> dict | None:
    """Update one or more player fields. Returns the updated player row (or None if not found).

    Only keys in PLAYER_EDITABLE_FIELDS are allowed.
    """
    allowed = {k: v for k, v in changes.items() if k in PLAYER_EDITABLE_FIELDS}
    if not allowed:
        return query_one("SELECT * FROM vtx_players WHERE discord_id = %s", (discord_id,))
    sets = ", ".join(f"{k} = %s" for k in allowed)
    execute(f"UPDATE vtx_players SET {sets} WHERE discord_id = %s", (*allowed.values(), discord_id))
    return query_one("SELECT * FROM vtx_players WHERE discord_id = %s", (discord_id,))


def get_event(event_id: int) -> dict | None:
    return query_one("SELECT * FROM vtx_events WHERE id = %s", (event_id,))


def get_active_events() -> list[dict]:
    return query(
        "SELECT * FROM vtx_events WHERE status IN ('setup', 'registration', 'in_progress') "
        "ORDER BY created_at DESC"
    )


def get_event_registrations(event_id: int) -> list[dict]:
    return query(
        "SELECT * FROM vtx_registrations WHERE event_id = %s AND status = 'confirmed' "
        "ORDER BY created_at",
        (event_id,),
    )


def get_event_players(event_id: int) -> list[dict]:
    return query(
        "SELECT p.*, r.team_members FROM vtx_registrations r "
        "JOIN vtx_players p ON r.discord_id = p.discord_id "
        "WHERE r.event_id = %s AND r.status = 'confirmed' "
        "ORDER BY r.created_at",
        (event_id,),
    )


def get_event_games(event_id: int) -> list[dict]:
    return query(
        "SELECT * FROM vtx_games WHERE event_id = %s ORDER BY game_number",
        (event_id,),
    )


def get_game_players(game_id: int) -> list[dict]:
    return query(
        "SELECT gp.*, COALESCE(p.game_username, p.username) AS username, p.discord_id "
        "FROM vtx_game_players gp "
        "JOIN vtx_players p ON gp.player_id = p.id "
        "WHERE gp.game_id = %s ORDER BY gp.points DESC",
        (game_id,),
    )


def get_game_team_leaderboard(game_id: int, event_id: int) -> list[dict]:
    registrations = query(
        "SELECT * FROM vtx_registrations WHERE event_id = %s AND status = 'confirmed' "
        "ORDER BY created_at",
        (event_id,),
    )
    if not registrations:
        return []

    game = query_one("SELECT id FROM vtx_games WHERE id = %s", (game_id,))
    if not game:
        return []

    result = []
    for reg in registrations:
        all_ids = [reg["discord_id"]]
        if reg["team_members"]:
            all_ids.extend(mid.strip() for mid in reg["team_members"].split(",") if mid.strip())

        player_ids = []
        for did in all_ids:
            p = query_one("SELECT id FROM vtx_players WHERE discord_id = %s", (did,))
            if p:
                player_ids.append(p["id"])

        total_points = 0
        total_kills = 0
        is_dq = 0
        best_placement = None
        for pid in player_ids:
            gp = query_one(
                "SELECT points, kills, is_disqualified, placement "
                "FROM vtx_game_players WHERE game_id = %s AND player_id = %s",
                (game_id, pid),
            )
            if gp:
                total_points += gp["points"] or 0
                total_kills += gp["kills"] or 0
                if gp["is_disqualified"]:
                    is_dq = 1
                if gp["placement"] and gp["placement"] > 0:
                    if best_placement is None or gp["placement"] < best_placement:
                        best_placement = gp["placement"]

        result.append({
            "team_name": reg["username"],
            "lead_id": reg["discord_id"],
            "team_members": reg["team_members"],
            "total_points": total_points,
            "total_kills": total_kills,
            "is_dq": is_dq,
            "best_placement": best_placement,
        })

    result.sort(key=lambda x: (x["is_dq"], -x["total_points"]))
    return result


def get_game_kills(game_id: int) -> list[dict]:
    return query(
        "SELECT k.*, pk.username AS killer_name, pv.username AS victim_name "
        "FROM vtx_kills k "
        "JOIN vtx_players pk ON k.killer_id = pk.id "
        "JOIN vtx_players pv ON k.victim_id = pv.id "
        "WHERE k.game_id = %s ORDER BY k.created_at",
        (game_id,),
    )


def get_lobby(lobby_id: int) -> dict | None:
    return query_one("SELECT * FROM vtx_lobbies WHERE id = %s", (lobby_id,))


def get_lobby_players(lobby_id: int) -> list[dict]:
    return query(
        "SELECT p.* FROM vtx_lobby_players lp "
        "JOIN vtx_players p ON lp.player_id = p.id "
        "WHERE lp.lobby_id = %s ORDER BY lp.joined_at",
        (lobby_id,),
    )


def get_event_lobbies(event_id: int) -> list[dict]:
    return query(
        "SELECT * FROM vtx_lobbies WHERE event_id = %s ORDER BY created_at",
        (event_id,),
    )


def get_leaderboard(event_id: int) -> list[dict]:
    rows = query(
        "SELECT p.id, COALESCE(p.game_username, p.username) AS username, p.discord_id, "
        "SUM(gp.points) AS total_points, "
        "SUM(gp.kills) AS total_kills, "
        "MAX(gp.is_disqualified) AS is_dq "
        "FROM vtx_game_players gp "
        "JOIN vtx_players p ON gp.player_id = p.id "
        "JOIN vtx_games g ON gp.game_id = g.id "
        "WHERE g.event_id = %s "
        "GROUP BY p.id "
        "ORDER BY is_dq ASC, total_points DESC",
        (event_id,),
    )
    return _enrich_solo_leaderboard(event_id, rows)


def _parse_placement_scale(ev: dict) -> list[int]:
    try:
        return json.loads(ev.get("placement_scale") or "[]")
    except (json.JSONDecodeError, TypeError):
        return []


def _enrich_rows(ev: dict | None, rows: list[dict], counts: list[dict], placements: list[dict]) -> list[dict]:
    """Attach derived per-player stats (wins, placement points, averages) to leaderboard rows."""
    scale = _parse_placement_scale(ev) if ev else []
    season = 1
    if ev:
        g = query_one(
            "SELECT season FROM vtx_games WHERE event_id = %s LIMIT 1", (ev["id"],)
        )
        season = g["season"] if g else 1
    cnt_map = {c["player_id"]: c["cnt"] for c in counts}
    plc_map = {}
    for p in placements:
        plc_map.setdefault(p["player_id"], []).append(p["placement"])

    for row in rows:
        games_played = cnt_map.get(row["id"], 0)
        pls = plc_map.get(row["id"], [])
        row["games_played"] = games_played
        row["wins"] = sum(1 for p in pls if p == 1) + get_event_wins_count(row["id"], season)
        row["placements"] = pls
        row["placement_points"] = sum(
            scale[p - 1] for p in pls if 1 <= p <= len(scale)
        )
        row["avg_placement"] = round(sum(pls) / len(pls), 1) if pls else None
        row["avg_points"] = (
            round((row["total_points"] or 0) / games_played, 1)
            if games_played
            else 0
        )
    return rows


def _enrich_solo_leaderboard(event_id: int, rows: list[dict]) -> list[dict]:
    ev = get_event(event_id)
    counts = query(
        "SELECT gp.player_id, COUNT(gp.id) AS cnt "
        "FROM vtx_game_players gp JOIN vtx_games g ON gp.game_id = g.id "
        "WHERE g.event_id = %s GROUP BY gp.player_id",
        (event_id,),
    )
    placements = query(
        "SELECT gp.player_id, gp.placement "
        "FROM vtx_game_players gp JOIN vtx_games g ON gp.game_id = g.id "
        "WHERE g.event_id = %s AND gp.placement IS NOT NULL "
        "ORDER BY g.game_number",
        (event_id,),
    )
    return _enrich_rows(ev, rows, counts, placements)


def get_solo_leaderboard(event_id: int) -> list[dict]:
    registrations = query(
        "SELECT discord_id, team_members FROM vtx_registrations "
        "WHERE event_id = %s AND status = 'confirmed'",
        (event_id,),
    )
    team_player_ids = set()
    for reg in registrations:
        if reg["team_members"]:
            team_player_ids.add(reg["discord_id"])
            for mid in reg["team_members"].split(","):
                mid = mid.strip()
                if mid:
                    team_player_ids.add(mid)

    if not team_player_ids:
        return query(
            "SELECT COALESCE(p.game_username, p.username) AS username, p.discord_id, "
            "SUM(gp.points) AS total_points, "
            "SUM(gp.kills) AS total_kills, "
            "MAX(gp.is_disqualified) AS is_dq "
            "FROM vtx_game_players gp "
            "JOIN vtx_players p ON gp.player_id = p.id "
            "JOIN vtx_games g ON gp.game_id = g.id "
            "WHERE g.event_id = %s "
            "GROUP BY p.id "
            "ORDER BY is_dq ASC, total_points DESC",
            (event_id,),
        )

    ph = ",".join(["%s"] * len(team_player_ids))
    return query(
        f"SELECT p.username, p.discord_id, SUM(gp.points) AS total_points, "
        f"SUM(gp.kills) AS total_kills, "
        f"MAX(gp.is_disqualified) AS is_dq "
        f"FROM vtx_game_players gp "
        f"JOIN vtx_players p ON gp.player_id = p.id "
        f"JOIN vtx_games g ON gp.game_id = g.id "
        f"WHERE g.event_id = %s "
        f"AND p.discord_id NOT IN ({ph}) "
        f"GROUP BY p.id "
        f"ORDER BY is_dq ASC, total_points DESC",
        (event_id, *team_player_ids),
    )


def get_team_leaderboard(event_id: int) -> list[dict]:
    registrations = query(
        "SELECT * FROM vtx_registrations WHERE event_id = %s AND status = 'confirmed' "
        "ORDER BY created_at",
        (event_id,),
    )
    if not registrations:
        return []

    ev = get_event(event_id)
    scale = _parse_placement_scale(ev) if ev else []

    games = query("SELECT id FROM vtx_games WHERE event_id = %s", (event_id,))
    game_ids = [g["id"] for g in games]

    result = []
    for reg in registrations:
        all_ids = [reg["discord_id"]]
        if reg["team_members"]:
            all_ids.extend(mid.strip() for mid in reg["team_members"].split(",") if mid.strip())

        player_ids = []
        for did in all_ids:
            p = query_one("SELECT id FROM vtx_players WHERE discord_id = %s", (did,))
            if p:
                player_ids.append(p["id"])

        total_points = 0
        total_kills = 0
        is_dq = 0
        played_game_ids = set()
        best_by_game = {}
        for pid in player_ids:
            if game_ids:
                ph = ",".join(["%s"] * len(game_ids))
                rows = query(
                    f"SELECT COALESCE(SUM(points),0) AS pts, COALESCE(SUM(kills),0) AS k, "
                    f"MAX(is_disqualified) AS dq FROM vtx_game_players "
                    f"WHERE game_id IN ({ph}) AND player_id = %s",
                    (*game_ids, pid),
                )
                if rows:
                    total_points += rows[0]["pts"]
                    total_kills += rows[0]["k"]
                    if rows[0]["dq"]:
                        is_dq = 1
                member_games = query(
                    f"SELECT DISTINCT game_id FROM vtx_game_players WHERE game_id IN ({ph}) AND player_id = %s",
                    (*game_ids, pid),
                )
                for mg in member_games:
                    played_game_ids.add(mg["game_id"])
                placements = query(
                    f"SELECT game_id, placement FROM vtx_game_players "
                    f"WHERE game_id IN ({ph}) AND player_id = %s AND placement IS NOT NULL",
                    (*game_ids, pid),
                )
                for pl in placements:
                    best_by_game.setdefault(pl["game_id"], []).append(pl["placement"])

        best_placements = [min(v) for v in best_by_game.values()]
        games_played = len(played_game_ids)

        result.append({
            "team_name": reg["username"],
            "lead_id": reg["discord_id"],
            "team_members": reg["team_members"],
            "total_points": total_points,
            "total_kills": total_kills,
            "is_dq": is_dq,
            "games_played": games_played,
            "wins": sum(1 for p in best_placements if p == 1),
            "placements": best_placements,
            "placement_points": sum(
                scale[p - 1] for p in best_placements if 1 <= p <= len(scale)
            ),
            "avg_placement": round(sum(best_placements) / len(best_placements), 1) if best_placements else None,
            "avg_points": round(total_points / games_played, 1) if games_played else 0,
        })

    result.sort(key=lambda x: (x["is_dq"], -x["total_points"]))

    g = query_one("SELECT season FROM vtx_games WHERE event_id = %s LIMIT 1", (event_id,))
    season = g["season"] if g else 1
    for reg_row in result:
        ids = [reg_row["lead_id"]]
        ids += [
            m.strip()
            for m in (reg_row.get("team_members") or "").split(",")
            if m.strip()
        ]
        ph = ",".join(["%s"] * len(ids))
        ew = query_one(
            f"SELECT COUNT(DISTINCT ew.event_id) AS c FROM vtx_event_wins ew "
            f"JOIN vtx_players p ON ew.player_id = p.id "
            f"WHERE p.discord_id IN ({ph}) AND ew.season = %s",
            (*ids, season),
        )
        reg_row["wins"] = (reg_row.get("wins") or 0) + (ew["c"] if ew else 0)
    return result


def get_leaderboard_full(event_id: int) -> list[dict]:
    ev = get_event(event_id)
    if not ev:
        return []
    if ev.get("team_size", 1) >= 2:
        return get_team_leaderboard(event_id)
    return get_leaderboard(event_id)


def get_event_player_placement(event_id: int, discord_id: str) -> dict | None:
    ev = get_event(event_id)
    if not ev:
        return None

    if ev.get("team_size", 1) >= 2:
        board = get_team_leaderboard(event_id)
        idx = None
        for i, r in enumerate(board):
            members = [m.strip() for m in (r.get("team_members") or "").split(",") if m.strip()]
            if r["lead_id"] == discord_id or discord_id in members:
                idx = i
                break
    else:
        board = get_leaderboard(event_id)
        idx = next(
            (i for i, r in enumerate(board) if r["discord_id"] == discord_id),
            None,
        )

    if idx is None:
        return {"position": None, "total": len(board), "row": None}
    return {"position": idx + 1, "total": len(board), "row": board[idx]}


def get_event_player_stats(event_id: int, discord_id: str) -> dict | None:
    player = query_one("SELECT * FROM vtx_players WHERE discord_id = %s", (discord_id,))
    if not player:
        return None

    rows = query(
        "SELECT gp.placement, gp.points, gp.kills, g.game_number "
        "FROM vtx_game_players gp JOIN vtx_games g ON gp.game_id = g.id "
        "WHERE g.event_id = %s AND gp.player_id = %s "
        "ORDER BY g.game_number",
        (event_id, player["id"]),
    )
    games = len(rows)
    wins = sum(1 for r in rows if r["placement"] == 1)
    kills = sum(r["kills"] or 0 for r in rows)
    points = sum(r["points"] or 0 for r in rows)
    placements = [r["placement"] for r in rows if r["placement"] is not None]

    return {
        "player": player,
        "games": games,
        "wins": wins,
        "kills": kills,
        "points": points,
        "placements": placements,
        "avg_points": round(points / games, 1) if games else 0,
        "avg_placement": round(sum(placements) / len(placements), 1) if placements else None,
    }


def queue_command(command: str, params: dict | None = None) -> int:
    return execute(
        "INSERT INTO vtx_command_queue (command, params) VALUES (%s, %s)",
        (command, json.dumps(params or {})),
    )


def pop_command() -> dict | None:
    with get_db() as conn:
        row = _exec(conn,
            "SELECT * FROM vtx_command_queue WHERE status = 'pending' "
            "ORDER BY created_at ASC LIMIT 1"
        ).fetchone()
        if not row:
            return None
        cmd = dict(row)
        _exec(conn,
            "UPDATE vtx_command_queue SET status = 'running' WHERE id = %s",
            (cmd["id"],),
        )
        conn.commit()
        return cmd


def complete_command(cmd_id: int, result: str = "ok") -> None:
    execute(
        "UPDATE vtx_command_queue SET status = 'done', result = %s, "
        "executed_at = CURRENT_TIMESTAMP WHERE id = %s",
        (result, cmd_id),
    )


def fail_command(cmd_id: int, error: str) -> None:
    execute(
        "UPDATE vtx_command_queue SET status = 'failed', result = %s WHERE id = %s",
        (error, cmd_id),
    )


def get_pending_commands() -> list[dict]:
    return query(
        "SELECT * FROM vtx_command_queue WHERE status = 'pending' ORDER BY created_at"
    )


def log_bot_action(event_id: int | None, action: str, details: str = "", user_id: str = "") -> None:
    execute(
        "INSERT INTO vtx_bot_logs (event_id, action, details, user_id) VALUES (%s, %s, %s, %s)",
        (event_id, action, details, user_id),
    )


def get_bot_logs(event_id: int | None = None, limit: int = 50) -> list[dict]:
    if event_id:
        return query(
            "SELECT * FROM vtx_bot_logs WHERE event_id = %s ORDER BY created_at DESC LIMIT %s",
            (event_id, limit),
        )
    return query(
        "SELECT * FROM vtx_bot_logs ORDER BY created_at DESC LIMIT %s",
        (limit,),
    )


def get_event_wins_count(player_id: int, season: int) -> int:
    row = query_one(
        "SELECT COUNT(*) AS c FROM vtx_event_wins WHERE player_id = %s AND season = %s",
        (player_id, season),
    )
    return row["c"] if row else 0


def record_event_wins(event_id: int) -> None:
    """Credit +1 win to every player on the winning side of a completed event."""
    ev = get_event(event_id)
    if not ev or ev.get("status") != "completed":
        return
    if (ev.get("event_type") or "cup") == "bracket":
        return
    if ev.get("team_size", 1) >= 2:
        board = get_team_leaderboard(event_id)
    else:
        board = get_leaderboard(event_id)
    if not board or board[0].get("is_dq"):
        return

    top = board[0]
    dids = [top["discord_id"]] if top.get("discord_id") else []
    if ev.get("team_size", 1) >= 2:
        dids = [top["lead_id"]] if top.get("lead_id") else []
        dids += [
            m.strip()
            for m in (top.get("team_members") or "").split(",")
            if m.strip()
        ]

    g = query_one("SELECT season FROM vtx_games WHERE event_id = %s LIMIT 1", (event_id,))
    season = g["season"] if g else get_season()
    for did in dids:
        p = query_one("SELECT id FROM vtx_players WHERE discord_id = %s", (did,))
        if not p:
            continue
        execute(
            "INSERT INTO vtx_event_wins (event_id, player_id, season) VALUES (%s, %s, %s) ON CONFLICT DO NOTHING",
            (event_id, p["id"], season),
        )


def _row_member_discord_ids(row: dict, team_size: int) -> list[str]:
    """All discord ids represented by a leaderboard row (solo leader or whole team)."""
    if team_size >= 2:
        dids = [row["lead_id"]] if row.get("lead_id") else []
        dids += [
            m.strip()
            for m in (row.get("team_members") or "").split(",")
            if m.strip()
        ]
        return dids
    return [row["discord_id"]] if row.get("discord_id") else []


def grant_event_coin_rewards(event_id: int) -> None:
    """Credit coins for a completed event: win, podium, and participation payouts.

    Idempotent per event (guarded by a kv_store marker) so both finalize paths
    can call it safely.
    """
    guard = f"event_coins_granted:{event_id}"
    if get_kv(guard):
        return
    ev = get_event(event_id)
    if not ev or ev.get("status") != "completed":
        return

    team_size = ev.get("team_size", 1)
    if team_size >= 2:
        board = get_team_leaderboard(event_id)
    else:
        board = get_leaderboard(event_id)

    if not board:
        return

    podium = [
        row for row in board[:3] if not row.get("is_dq")
    ]
    payouts = (
        (0, settings.coin_event_win),
        (1, settings.coin_placement_2),
        (2, settings.coin_placement_3),
    )
    for idx, amount in payouts:
        if idx >= len(podium):
            break
        for did in _row_member_discord_ids(podium[idx], team_size):
            add_coins(did, amount)

    part_rows = query(
        "SELECT DISTINCT p.discord_id FROM vtx_game_players gp "
        "JOIN vtx_players p ON p.id = gp.player_id "
        "JOIN vtx_games g ON g.id = gp.game_id WHERE g.event_id = %s",
        (event_id,),
    )
    for row in part_rows:
        if row.get("discord_id"):
            add_coins(row["discord_id"], settings.coin_participation)

    set_kv(guard, "1")


def get_player_stats(
    discord_id: str,
    event_id: int | None = None,
    season: int | None = None,
) -> dict | None:
    player = query_one("SELECT * FROM vtx_players WHERE discord_id = %s", (discord_id,))
    if not player:
        return None

    if event_id:
        stats = query_one(
            "SELECT "
            "COUNT(CASE WHEN gp.placement = 1 THEN 1 END) AS total_wins, "
            "COALESCE(SUM(gp.kills), 0) AS total_kills, "
            "COUNT(gp.id) AS total_games, "
            "ROUND(AVG(gp.placement), 1) AS avg_placement "
            "FROM vtx_game_players gp "
            "JOIN vtx_games g ON gp.game_id = g.id "
            "WHERE gp.player_id = %s AND g.event_id = %s",
            (player["id"], event_id),
        )
    else:
        if season is None:
            season = get_season()
        stats = query_one(
            "SELECT "
            "COUNT(CASE WHEN gp.placement = 1 THEN 1 END) AS total_wins, "
            "COALESCE(SUM(gp.kills), 0) AS total_kills, "
            "COUNT(gp.id) AS total_games, "
            "ROUND(AVG(gp.placement), 1) AS avg_placement "
            "FROM vtx_game_players gp "
            "JOIN vtx_games g ON gp.game_id = g.id "
            "JOIN vtx_events e ON g.event_id = e.id "
            "WHERE gp.player_id = %s AND e.status = 'completed' AND g.season = %s",
            (player["id"], season),
        )

    event_wins = get_event_wins_count(player["id"], season) if not event_id else 0
    return {
        "player": player,
        "total_wins": (stats["total_wins"] if stats else 0) + event_wins,
        "total_kills": stats["total_kills"] if stats else 0,
        "total_games": stats["total_games"] if stats else 0,
        "avg_placement": stats["avg_placement"] if stats and stats["avg_placement"] is not None else None,
    }


def calc_event_pr(event_id: int) -> dict[str, float]:
    ev = get_event(event_id)
    if not ev:
        return {}

    try:
        placement_scale = json.loads(ev.get("placement_scale") or "[10,8,6,4,2,1]")
    except (json.JSONDecodeError, TypeError):
        placement_scale = [10, 8, 6, 4, 2, 1]

    point_kill = ev.get("point_kill", 1) or 1
    point_win = ev.get("point_win", 5) or 5
    team_size = ev.get("team_size", 1) or 1
    scoring_mode = ev.get("scoring_mode") or "normal"
    if scoring_mode == "coins":
        return {}

    is_team = team_size >= 2
    registrations = query(
        "SELECT * FROM vtx_registrations WHERE event_id = %s AND status = 'confirmed'",
        (event_id,),
    )

    if is_team:
        participant_count = len([r for r in registrations if r.get("team_members")])
    else:
        participant_count = len(registrations)

    if participant_count < 8:
        multiplier = 1.2
    elif participant_count < 20:
        multiplier = 1.5
    else:
        multiplier = 1.9

    custom_multiplier = ev.get("pr_multiplier") or 0
    if custom_multiplier > 0:
        multiplier = float(custom_multiplier)

    games = query("SELECT id FROM vtx_games WHERE event_id = %s", (event_id,))
    game_ids = [g["id"] for g in games]
    if not game_ids:
        return {}

    ph = ",".join(["%s"] * len(game_ids))

    if is_team:
        result = {}
        for reg in registrations:
            if not reg.get("team_members"):
                continue
            all_ids = [reg["discord_id"]]
            all_ids.extend(mid.strip() for mid in reg["team_members"].split(",") if mid.strip())

            total_points = 0
            total_kills = 0
            wins = 0
            for did in all_ids:
                p = query_one("SELECT id FROM vtx_players WHERE discord_id = %s", (did,))
                if not p:
                    continue
                rows = query(
                    f"SELECT COALESCE(SUM(points),0) AS pts, COALESCE(SUM(kills),0) AS k, "
                    f"COUNT(CASE WHEN placement = 1 THEN 1 END) AS w "
                    f"FROM vtx_game_players WHERE game_id IN ({ph}) AND player_id = %s",
                    (*game_ids, p["id"]),
                )
                if rows:
                    total_points += rows[0]["pts"]
                    total_kills += rows[0]["k"]
                    wins += rows[0]["w"]

            if scoring_mode == "placement_only":
                raw_pr = total_points
            else:
                raw_pr = total_points + (total_kills * point_kill) + (wins * point_win)
            final_pr = raw_pr * multiplier / team_size
            result[reg["discord_id"]] = round(final_pr, 1)
        return result
    else:
        result = {}
        for reg in registrations:
            did = reg["discord_id"]
            p = query_one("SELECT id FROM vtx_players WHERE discord_id = %s", (did,))
            if not p:
                continue
            rows = query(
                f"SELECT COALESCE(SUM(points),0) AS pts, COALESCE(SUM(kills),0) AS k, "
                f"COUNT(CASE WHEN placement = 1 THEN 1 END) AS w "
                f"FROM vtx_game_players WHERE game_id IN ({ph}) AND player_id = %s",
                (*game_ids, p["id"]),
            )
            if rows:
                total_points = rows[0]["pts"]
                total_kills = rows[0]["k"]
                wins = rows[0]["w"]
            else:
                total_points = total_kills = wins = 0

            if scoring_mode == "placement_only":
                raw_pr = total_points
            else:
                raw_pr = total_points + (total_kills * point_kill) + (wins * point_win)
            final_pr = raw_pr * multiplier
            result[did] = round(final_pr, 1)
        return result


def update_player_pr(discord_id: str, base_pr: int = 100, event_id: int | None = None) -> int:
    if event_id:
        pr_map = calc_event_pr(event_id)
        new_pr = pr_map.get(discord_id, 0)
    else:
        stats = get_player_stats(discord_id)
        if not stats:
            return 0
        total_wins = stats["total_wins"]
        total_kills = stats["total_kills"]
        new_pr = base_pr + (total_wins * 50) + (total_kills * 5)

    execute(
        "UPDATE vtx_players SET pr = %s WHERE discord_id = %s",
        (new_pr, discord_id),
    )
    return new_pr


def apply_placement_points(game_id: int, event_id: int) -> None:
    ev = get_event(event_id)
    if not ev:
        return
    try:
        scale = json.loads(ev.get("placement_scale") or "[]")
    except (json.JSONDecodeError, TypeError):
        scale = []
    if not scale:
        return

    rows = query(
        "SELECT id, player_id, placement FROM vtx_game_players WHERE game_id = %s AND placement IS NOT NULL",
        (game_id,),
    )
    for row in rows:
        p = row["placement"]
        if p and 1 <= p <= len(scale):
            pts = scale[p - 1]
            execute(
                "UPDATE vtx_game_players SET points = points + %s WHERE id = %s",
                (pts, row["id"]),
            )


def init_team_members(game_id: int, event_id: int) -> None:
    ev = get_event(event_id)
    if not ev or ev.get("team_size", 1) < 2:
        return
    registrations = query(
        "SELECT * FROM vtx_registrations WHERE event_id = %s AND status = 'confirmed' "
        "AND team_members IS NOT NULL AND team_members != ''",
        (event_id,),
    )
    for reg in registrations:
        all_ids = [reg["discord_id"]]
        if reg["team_members"]:
            all_ids.extend(mid.strip() for mid in reg["team_members"].split(",") if mid.strip())
        for did in all_ids:
            existing = query_one(
                "SELECT id FROM vtx_game_team_members WHERE game_id = %s AND discord_id = %s",
                (game_id, did),
            )
            if not existing:
                execute(
                    "INSERT INTO vtx_game_team_members (game_id, discord_id, team_lead_id) VALUES (%s, %s, %s)",
                    (game_id, did, reg["discord_id"]),
                )


def mark_teammate_eliminated(game_id: int, discord_id: str) -> dict:
    existing = query_one(
        "SELECT * FROM vtx_game_team_members WHERE game_id = %s AND discord_id = %s",
        (game_id, discord_id),
    )
    if not existing:
        return {"ok": False, "error": "Player not found in game"}
    if existing["eliminated"]:
        return {"ok": False, "error": "Already eliminated"}

    execute(
        "UPDATE vtx_game_team_members SET eliminated = 1, eliminated_at = CURRENT_TIMESTAMP "
        "WHERE game_id = %s AND discord_id = %s",
        (game_id, discord_id),
    )

    team_lead_id = existing["team_lead_id"]
    total = query_one(
        "SELECT COUNT(*) AS cnt FROM vtx_game_team_members WHERE game_id = %s AND team_lead_id = %s",
        (game_id, team_lead_id),
    )
    eliminated = query_one(
        "SELECT COUNT(*) AS cnt FROM vtx_game_team_members WHERE game_id = %s AND team_lead_id = %s AND eliminated = 1",
        (game_id, team_lead_id),
    )
    all_eliminated = total["cnt"] > 0 and eliminated["cnt"] >= total["cnt"]

    return {"ok": True, "all_eliminated": all_eliminated, "team_lead_id": team_lead_id}


def get_team_elimination_status(game_id: int, team_lead_id: str) -> dict:
    total = query_one(
        "SELECT COUNT(*) AS cnt FROM vtx_game_team_members WHERE game_id = %s AND team_lead_id = %s",
        (game_id, team_lead_id),
    )
    eliminated = query_one(
        "SELECT COUNT(*) AS cnt FROM vtx_game_team_members WHERE game_id = %s AND team_lead_id = %s AND eliminated = 1",
        (game_id, team_lead_id),
    )
    return {
        "total": total["cnt"] if total else 0,
        "eliminated": eliminated["cnt"] if eliminated else 0,
        "all_eliminated": (total["cnt"] > 0 and eliminated["cnt"] >= total["cnt"]) if total and eliminated else False,
    }


def set_player_pr(discord_id: str, pr: int) -> int:
    pr = max(0, int(pr))
    execute(
        "UPDATE vtx_players SET pr = %s WHERE discord_id = %s",
        (pr, discord_id),
    )
    return pr


def add_player_pr(discord_id: str, amount: int) -> int:
    current = query_one(
        "SELECT pr FROM vtx_players WHERE discord_id = %s", (discord_id,)
    )
    new_pr = max(0, (current["pr"] if current else 0) + int(amount))
    execute(
        "UPDATE vtx_players SET pr = %s WHERE discord_id = %s",
        (new_pr, discord_id),
    )
    return new_pr


def ban_player(
    discord_id: str,
    banned_until: str,
    reason: str = "",
    created_by: str = "",
) -> None:
    execute(
        "INSERT INTO vtx_bans (discord_id, reason, banned_until, created_by) "
        "VALUES (%s, %s, %s, %s) "
        "ON CONFLICT(discord_id) DO UPDATE SET "
        "reason = excluded.reason, "
        "banned_until = excluded.banned_until, "
        "created_by = excluded.created_by",
        (discord_id, reason, banned_until, created_by),
    )


def unban_player(discord_id: str) -> None:
    execute("DELETE FROM vtx_bans WHERE discord_id = %s", (discord_id,))


def get_player_ban(discord_id: str) -> dict | None:
    return query_one("SELECT * FROM vtx_bans WHERE discord_id = %s", (discord_id,))


def is_player_banned(discord_id: str, now: str | None = None) -> bool:
    ban = get_player_ban(discord_id)
    if not ban:
        return False
    if ban["banned_until"]:
        from datetime import datetime

        try:
            until = datetime.fromisoformat(ban["banned_until"])
            current = (
                datetime.fromisoformat(now)
                if now
                else datetime.utcnow()
            )
            if current >= until:
                return False
        except ValueError:
            pass
    return True


def get_player_position(discord_id: str) -> int:
    players = query(
        "SELECT discord_id, pr FROM vtx_players ORDER BY pr DESC, username ASC"
    )
    for i, p in enumerate(players, 1):
        if p["discord_id"] == discord_id:
            return i
    return len(players) + 1


def create_game_record(
    event_id: int,
    game_number: int,
    room_code: str = "",
    status: str = "in_progress",
    session_id: int | None = None,
) -> int:
    return execute(
        "INSERT INTO vtx_games (event_id, game_number, room_code, status, season, session_id) "
        "VALUES (%s, %s, %s, %s, %s, %s)",
        (event_id, game_number, room_code, status, get_season(), session_id),
    )


def create_session(event_id: int) -> dict:
    """Create the next pending session for an event (returns the session row)."""
    last = query_one(
        "SELECT COALESCE(MAX(session_number), 0) AS n FROM vtx_sessions WHERE event_id = %s",
        (event_id,),
    )
    session_number = (last["n"] if last else 0) + 1
    sid = execute(
        "INSERT INTO vtx_sessions (event_id, session_number, status) VALUES (%s, %s, 'pending')",
        (event_id, session_number),
    )
    return get_session(sid)


def get_session(session_id: int) -> dict | None:
    return query_one("SELECT * FROM vtx_sessions WHERE id = %s", (session_id,))


def get_event_sessions(event_id: int) -> list[dict]:
    return query(
        "SELECT * FROM vtx_sessions WHERE event_id = %s ORDER BY session_number",
        (event_id,),
    )


def get_session_matches(session_id: int) -> list[dict]:
    return query(
        "SELECT * FROM vtx_games WHERE session_id = %s ORDER BY game_number",
        (session_id,),
    )


def get_latest_session(event_id: int) -> dict | None:
    return query_one(
        "SELECT * FROM vtx_sessions WHERE event_id = %s ORDER BY session_number DESC LIMIT 1",
        (event_id,),
    )


def get_event_active_session(event_id: int) -> dict | None:
    return query_one(
        "SELECT * FROM vtx_sessions WHERE event_id = %s AND status = 'in_progress' "
        "ORDER BY session_number DESC LIMIT 1",
        (event_id,),
    )


def get_session_leaderboard(session_id: int) -> list[dict]:
    """Player leaderboard accumulated across all matches of a session."""
    sess = get_session(session_id)
    if not sess:
        return []
    ev = get_event(sess["event_id"])
    scale = _parse_placement_scale(ev) if ev else []

    rows = query(
        "SELECT p.id, COALESCE(p.game_username, p.username) AS username, p.discord_id, "
        "SUM(gp.points) AS total_points, "
        "SUM(gp.kills) AS total_kills, "
        "MAX(gp.is_disqualified) AS is_dq "
        "FROM vtx_game_players gp "
        "JOIN vtx_players p ON gp.player_id = p.id "
        "JOIN vtx_games g ON gp.game_id = g.id "
        "WHERE g.session_id = %s "
        "GROUP BY p.id "
        "ORDER BY is_dq ASC, total_points DESC",
        (session_id,),
    )

    placements = query(
        "SELECT gp.player_id, gp.placement "
        "FROM vtx_game_players gp JOIN vtx_games g ON gp.game_id = g.id "
        "WHERE g.session_id = %s AND gp.placement IS NOT NULL "
        "ORDER BY g.game_number",
        (session_id,),
    )
    plc_map = {}
    for p in placements:
        plc_map.setdefault(p["player_id"], []).append(p["placement"])

    for row in rows:
        pls = plc_map.get(row["id"], [])
        row["games_played"] = len(pls)
        g = query_one(
            "SELECT season FROM vtx_games WHERE session_id = %s LIMIT 1", (session_id,)
        )
        row["wins"] = sum(1 for p in pls if p == 1) + get_event_wins_count(
            row["id"], g["season"] if g else 1
        )
        row["placements"] = pls
        row["placement_points"] = sum(
            scale[p - 1] for p in pls if 1 <= p <= len(scale)
        )
        row["avg_placement"] = round(sum(pls) / len(pls), 1) if pls else None
        row["avg_points"] = (
            round((row["total_points"] or 0) / len(pls), 1) if pls else 0
        )
    return rows


def get_players_leaderboard(season: int | None = None) -> list[dict]:
    if season is None:
        season = get_season()
    return query(
        "SELECT p.discord_id, p.username, p.game_username, p.game_id, p.country, p.region, "
        "p.pr, p.total_pr, "
        "(COUNT(CASE WHEN gp.placement = 1 AND e.status = 'completed' THEN 1 END) "
        "+ (SELECT COUNT(*) FROM vtx_event_wins ew WHERE ew.player_id = p.id AND ew.season = %s)) AS total_wins, "
        "COALESCE(SUM(CASE WHEN e.status = 'completed' THEN gp.kills END), 0) AS total_kills, "
        "COUNT(CASE WHEN e.status = 'completed' THEN gp.id END) AS total_games, "
        "ROUND(AVG(CASE WHEN e.status = 'completed' THEN gp.placement END), 1) AS avg_placement "
        "FROM vtx_players p "
        "LEFT JOIN vtx_game_players gp ON gp.player_id = p.id "
        "LEFT JOIN vtx_games g ON gp.game_id = g.id AND g.season = %s "
        "LEFT JOIN vtx_events e ON g.event_id = e.id "
        "GROUP BY p.id "
        "ORDER BY p.pr DESC, total_wins DESC, total_kills DESC, p.username ASC",
        (season, season),
    )


def get_server_legend() -> dict | None:
    """Server legend must hold 5000+ PR, 20+ wins and 200+ kills this season."""
    players = get_players_leaderboard()
    for p in players:
        if (p.get("pr") or 0) < 5000:
            continue
        if (p.get("total_wins") or 0) < 20:
            continue
        if (p.get("total_kills") or 0) < 200:
            continue
        return p
    return None


def get_player_event_history(discord_id: str) -> list[dict]:
    p = query_one("SELECT id FROM vtx_players WHERE discord_id = %s", (discord_id,))
    if not p:
        return []
    return query(
        "SELECT e.id, e.name, e.team_size, e.region, e.event_format, "
        "e.created_at, e.status, "
        "COUNT(CASE WHEN gp.placement = 1 THEN 1 END) AS wins, "
        "COALESCE(SUM(gp.kills), 0) AS kills, "
        "COALESCE(SUM(gp.points), 0) AS points, "
        "ROUND(AVG(gp.placement), 1) AS avg_placement, "
        "COUNT(gp.id) AS games "
        "FROM vtx_registrations r "
        "JOIN vtx_events e ON r.event_id = e.id "
        "LEFT JOIN vtx_games g ON g.event_id = e.id "
        "LEFT JOIN vtx_game_players gp ON gp.game_id = g.id AND gp.player_id = %s "
        "WHERE r.discord_id = %s AND e.status = 'completed' "
        "GROUP BY e.id "
        "ORDER BY e.created_at DESC",
        (p["id"], discord_id),
    )


def get_player_profile(discord_id: str) -> dict | None:
    stats = get_player_stats(discord_id)
    if not stats:
        return None
    player = stats["player"]
    tier = get_rank_for_pr(player.get("pr") or 0)
    return {
        "player": player,
        "rank": tier["name"] if tier else "Unranked",
        "position": get_player_position(discord_id),
        "total_wins": stats["total_wins"],
        "total_kills": stats["total_kills"],
        "total_games": stats["total_games"],
        "avg_placement": stats["avg_placement"],
        "history": get_player_event_history(discord_id),
    }


def add_player_to_event(event_id: int, discord_id: str, username: str) -> dict:
    upsert_player(discord_id, username)
    execute(
        "INSERT INTO vtx_registrations "
        "(event_id, discord_id, username, status) VALUES (%s, %s, %s, 'confirmed') "
        "ON CONFLICT DO NOTHING",
        (event_id, discord_id, username),
    )
    return query_one("SELECT * FROM vtx_registrations WHERE event_id = %s AND discord_id = %s", (event_id, discord_id))


def remove_player_from_event(event_id: int, discord_id: str) -> bool:
    p = query_one("SELECT id FROM vtx_players WHERE discord_id = %s", (discord_id,))
    execute(
        "DELETE FROM vtx_registrations WHERE event_id = %s AND discord_id = %s",
        (event_id, discord_id),
    )

    regs = query(
        "SELECT * FROM vtx_registrations WHERE event_id = %s AND team_members LIKE %s",
        (event_id, f"%{discord_id}%"),
    )
    for reg in regs:
        members = [m.strip() for m in (reg["team_members"] or "").split(",") if m.strip()]
        remaining = [m for m in members if m != discord_id]
        if not remaining:
            execute("DELETE FROM vtx_registrations WHERE id = %s", (reg["id"],))
        else:
            execute(
                "UPDATE vtx_registrations SET team_members = %s WHERE id = %s",
                (",".join(remaining), reg["id"]),
            )

    for g in get_event_games(event_id):
        if p:
            execute(
                "DELETE FROM vtx_game_players WHERE game_id = %s AND player_id = %s",
                (g["id"], p["id"]),
            )
        execute(
            "DELETE FROM vtx_game_team_members WHERE game_id = %s AND discord_id = %s",
            (g["id"], discord_id),
        )

    for l in get_event_lobbies(event_id):
        if p:
            execute(
                "DELETE FROM vtx_lobby_players WHERE lobby_id = %s AND player_id = %s",
                (l["id"], p["id"]),
            )

    return True


def remove_team_from_event(event_id: int, leader_discord_id: str) -> dict:
    """Remove a whole team registration (leader + members) from an event,
    including their lobby and game entries. Returns {removed_members, ok}."""
    reg = query_one(
        "SELECT * FROM vtx_registrations WHERE event_id = %s AND discord_id = %s",
        (event_id, leader_discord_id),
    )
    if not reg:
        return {"ok": False, "removed_members": 0}

    members = [m.strip() for m in (reg["team_members"] or "").split(",") if m.strip()]
    all_ids = [leader_discord_id] + members

    for did in all_ids:
        remove_player_from_event(event_id, did)

    return {"ok": True, "removed_members": len(all_ids)}


def reset_event_scores(event_id: int) -> int:
    """Delete every game + game_players row for an event and reset its progress.
    Returns how many games were deleted."""
    games = get_event_games(event_id)
    for g in games:
        execute("DELETE FROM vtx_game_players WHERE game_id = %s", (g["id"],))
        execute("DELETE FROM vtx_kills WHERE game_id = %s", (g["id"],))
        execute("DELETE FROM vtx_games WHERE id = %s", (g["id"],))
    execute(
        "UPDATE vtx_events SET current_game = 0, status = 'registration' WHERE id = %s",
        (event_id,),
    )
    return len(games)


def get_bans() -> list[dict]:
    return query(
        "SELECT b.*, p.username FROM vtx_bans b "
        "LEFT JOIN vtx_players p ON b.discord_id = p.discord_id "
        "ORDER BY b.created_at DESC"
    )


def get_ban_by_id(ban_id: int) -> dict | None:
    return query_one("SELECT * FROM vtx_bans WHERE id = %s", (ban_id,))


def get_event_qualifiers(event_id: int) -> list[dict]:
    return query(
        "SELECT * FROM vtx_event_qualifiers WHERE event_id = %s ORDER BY id ASC",
        (event_id,),
    )


def toggle_event_interest(event_id: int, discord_id: str) -> bool:
    """Toggle a player's interest in an event. Returns True if now interested."""
    with get_db() as conn:
        cur = _exec(conn,
            "DELETE FROM vtx_event_interests WHERE event_id = %s AND discord_id = %s",
            (event_id, discord_id),
        )
        if cur.rowcount > 0:
            return False
        _exec(conn,
            "INSERT INTO vtx_event_interests (event_id, discord_id) VALUES (%s, %s)",
            (event_id, discord_id),
        )
        return True


def get_event_interested(event_id: int) -> list[dict]:
    return query(
        "SELECT discord_id FROM vtx_event_interests WHERE event_id = %s ORDER BY id ASC",
        (event_id,),
    )


def count_event_interests(event_id: int) -> int:
    row = query_one(
        "SELECT COUNT(*) AS c FROM vtx_event_interests WHERE event_id = %s",
        (event_id,),
    )
    return row["c"] if row else 0


def get_scheduled_events() -> list[dict]:
    return query(
        "SELECT * FROM vtx_events WHERE scheduled_at IS NOT NULL AND scheduled_at > 0 "
        "ORDER BY scheduled_at ASC",
    )


def set_event_schedule(
    event_id: int,
    scheduled_at: int,
    channel_id: str | None = None,
    message_id: str | None = None,
) -> None:
    execute(
        "UPDATE vtx_events SET scheduled_at = %s, schedule_channel_id = %s, "
        "schedule_message_id = %s, reminder_sent = 0 WHERE id = %s",
        (scheduled_at, channel_id, message_id, event_id),
    )


def clear_event_schedule(event_id: int) -> None:
    execute(
        "UPDATE vtx_events SET scheduled_at = NULL, schedule_channel_id = NULL, "
        "schedule_message_id = NULL, reminder_sent = 0 WHERE id = %s",
        (event_id,),
    )


def mark_event_reminded(event_id: int) -> None:
    execute("UPDATE vtx_events SET reminder_sent = 1 WHERE id = %s", (event_id,))


def add_event_qualifier(event_id: int, discord_id: str, username: str, team_members: str | None = None) -> dict:
    """Add a player to the event's qualified list (idempotent)."""
    existing = query_one(
        "SELECT * FROM vtx_event_qualifiers WHERE event_id = %s AND discord_id = %s",
        (event_id, discord_id),
    )
    if existing:
        return existing
    execute(
        "INSERT INTO vtx_event_qualifiers (event_id, discord_id, username, team_members) "
        "VALUES (%s, %s, %s, %s)",
        (event_id, discord_id, username, team_members),
    )
    return query_one(
        "SELECT * FROM vtx_event_qualifiers WHERE event_id = %s AND discord_id = %s",
        (event_id, discord_id),
    )


def remove_event_qualifier(event_id: int, discord_id: str) -> bool:
    with get_db() as conn:
        cur = _exec(conn,
            "DELETE FROM vtx_event_qualifiers WHERE event_id = %s AND discord_id = %s",
            (event_id, discord_id),
        )
        return cur.rowcount > 0


def move_qualifiers(source_event: int, target_event: int) -> dict:
    """Move qualified players from one event to another, registering them directly.
    Returns {"moved": int, "skipped": int, "names": [...]}."""
    qualifiers = get_event_qualifiers(source_event)
    if not qualifiers:
        return {"moved": 0, "skipped": 0, "names": []}
    moved = 0
    skipped = 0
    names = []
    for q in qualifiers:
        existing = query_one(
            "SELECT * FROM vtx_registrations WHERE event_id = %s AND discord_id = %s",
            (target_event, q["discord_id"]),
        )
        if existing:
            skipped += 1
            continue
        usernames = q["username"]
        execute(
            "INSERT INTO vtx_registrations "
            "(event_id, discord_id, username, team_members, status) "
            "VALUES (%s, %s, %s, %s, 'confirmed')",
            (target_event, q["discord_id"], usernames, q.get("team_members")),
        )
        execute(
            "INSERT INTO vtx_event_qualifiers (event_id, discord_id, username, team_members) "
            "VALUES (%s, %s, %s, %s)",
            (target_event, q["discord_id"], usernames, q.get("team_members")),
        )
        moved += 1
        names.append(q["username"])
        execute(
            "DELETE FROM vtx_event_qualifiers WHERE event_id = %s AND discord_id = %s",
            (source_event, q["discord_id"]),
        )
    return {"moved": moved, "skipped": skipped, "names": names}


def count_event_players(event_id: int) -> int:
    """Total seats taken: each registration counts its lead plus team members."""
    regs = get_event_registrations(event_id)
    count = 0
    for r in regs:
        count += 1
        if r.get("team_members"):
            count += len([m for m in r["team_members"].split(",") if m])
    return count


def get_placement_fields(ev: dict) -> list[tuple[str, str]]:
    """Return non-empty placement labels from an event row: [(label, value), ...]."""
    fields = [
        ("1st Place", ev.get("place_1")),
        ("2nd Place", ev.get("place_2")),
        ("3rd Place", ev.get("place_3")),
        ("4th Place+", ev.get("place_4plus")),
    ]
    return [(label, str(value)) for label, value in fields if value]


def get_kv(key: str, default: str = "") -> str:
    row = query_one("SELECT value FROM vtx_kv_store WHERE key = %s", (key,))
    return row["value"] if row else default


def get_coins(discord_id: str) -> int:
    row = query_one("SELECT coins FROM vtx_invite_coins WHERE discord_id = %s", (discord_id,))
    return row["coins"] if row else 0


def add_coins(discord_id: str, amount: int) -> int:
    """Add coins to a balance (creating the row if needed). Returns new balance."""
    with get_db() as conn:
        row = _exec(conn,
            "SELECT coins FROM vtx_invite_coins WHERE discord_id = %s", (discord_id,)
        ).fetchone()
        if row is None:
            _exec(conn,
                "INSERT INTO vtx_invite_coins (discord_id, coins) VALUES (%s, %s)",
                (discord_id, amount),
            )
            return amount
        _exec(conn,
            "UPDATE vtx_invite_coins SET coins = coins + %s, updated_at = CURRENT_TIMESTAMP "
            "WHERE discord_id = %s",
            (amount, discord_id),
        )
        return row["coins"] + amount


def grant_invite_coin(discord_id: str) -> int:
    """Legacy immediate-grant path (kept for the approval flow via add_coins)."""
    return add_coins(discord_id, 1)


def grant_invite_coin(discord_id: str) -> int:
    """Grant 1 coin for a successful invite. Returns the new balance."""
    with get_db() as conn:
        row = _exec(conn,
            "SELECT coins FROM vtx_invite_coins WHERE discord_id = %s", (discord_id,)
        ).fetchone()
        if row is None:
            _exec(conn,
                "INSERT INTO vtx_invite_coins (discord_id, coins, total_invites) VALUES (%s, 1, 1)",
                (discord_id,),
            )
            return 1
        _exec(conn,
            "UPDATE vtx_invite_coins SET coins = coins + 1, total_invites = total_invites + 1, "
            "updated_at = CURRENT_TIMESTAMP WHERE discord_id = %s",
            (discord_id,),
        )
        return row["coins"] + 1


def spend_coins(discord_id: str, amount: int) -> bool:
    """Atomically deduct coins. Returns False when balance is too low."""
    with get_db() as conn:
        row = _exec(conn,
            "SELECT coins FROM vtx_invite_coins WHERE discord_id = %s", (discord_id,)
        ).fetchone()
        if row is None or row["coins"] < amount:
            return False
        _exec(conn,
            "UPDATE vtx_invite_coins SET coins = coins - %s WHERE discord_id = %s",
            (amount, discord_id),
        )
        return True


def add_coin_purchase(
    discord_id: str, product: str, role_id: str, expires_at: int, guild_id: str
) -> None:
    execute(
        "INSERT INTO vtx_coin_purchases (discord_id, product, role_id, expires_at, guild_id) "
        "VALUES (%s, %s, %s, %s, %s)",
        (discord_id, product, role_id, expires_at, guild_id),
    )


def get_expired_purchases(now_ts: int) -> list[dict]:
    return query(
        "SELECT * FROM vtx_coin_purchases WHERE expires_at IS NOT NULL AND expires_at <= %s",
        (now_ts,),
    )


def delete_purchase(purchase_id: int) -> None:
    execute("DELETE FROM vtx_coin_purchases WHERE id = %s", (purchase_id,))


def get_coin_leaderboard(limit: int = 10) -> list[dict]:
    return query(
        "SELECT * FROM vtx_invite_coins ORDER BY coins DESC, total_invites DESC LIMIT %s",
        (limit,),
    )


# ------------------------------------------------------------------ invite rewards


def create_invite_reward(
    guild_id: str, inviter_id: str, invited_user_id: str, joined_at: int
) -> int:
    return execute(
        "INSERT INTO vtx_invite_rewards (guild_id, inviter_id, invited_user_id, created_at) "
        "VALUES (%s, %s, %s, %s)",
        (guild_id, inviter_id, invited_user_id, joined_at),
    )


def get_reward_by_invited(invited_user_id: str) -> dict | None:
    return query_one(
        "SELECT * FROM vtx_invite_rewards WHERE invited_user_id = %s ORDER BY id DESC LIMIT 1",
        (invited_user_id,),
    )


def get_reward(reward_id: int) -> dict | None:
    return query_one("SELECT * FROM vtx_invite_rewards WHERE id = %s", (reward_id,))


def get_pending_rewards() -> list[dict]:
    return query("SELECT * FROM vtx_invite_rewards WHERE status = 'pending'")


def mark_reward_left(reward_id: int, left_at: int) -> None:
    execute("UPDATE vtx_invite_rewards SET left_at = %s WHERE id = %s", (left_at, reward_id))


def set_reward_status(reward_id: int, status: str, reason: str = "", approved_at: int = 0) -> None:
    execute(
        "UPDATE vtx_invite_rewards SET status = %s, reason = %s, approved_at = "
        "(CASE WHEN %s = 'approved' THEN %s ELSE approved_at END) "
        "WHERE id = %s",
        (status, reason, status, approved_at, reward_id),
    )


def approve_invite_reward(reward_id: int, coins: int, quality: int, approved_at: int) -> None:
    execute(
        "UPDATE vtx_invite_rewards SET status = 'approved', coins_granted = %s, "
        "quality_score = %s, approved_at = %s, reason = '' WHERE id = %s",
        (coins, quality, approved_at, reward_id),
    )


def flag_invite_reward(reward_id: int, reason: str) -> None:
    execute(
        "UPDATE vtx_invite_rewards SET flagged = 1, reason = %s WHERE id = %s",
        (reason, reward_id),
    )


def update_reward_quality(reward_id: int, quality: int, reason: str = "") -> None:
    execute(
        "UPDATE vtx_invite_rewards SET quality_score = %s, reason = %s WHERE id = %s",
        (quality, reason, reward_id),
    )


def count_approved_since(inviter_id: str, since_ts: int) -> int:
    row = query_one(
        "SELECT COUNT(*) AS c FROM vtx_invite_rewards "
        "WHERE inviter_id = %s AND status = 'approved' AND approved_at >= %s",
        (inviter_id, since_ts),
    )
    return row["c"] if row else 0


def count_invites_created_since(inviter_id: str, since_ts: int) -> int:
    row = query_one(
        "SELECT COUNT(*) AS c FROM vtx_invite_rewards WHERE inviter_id = %s AND created_at >= %s",
        (inviter_id, since_ts),
    )
    return row["c"] if row else 0


def get_approved_rewards_without_loyalty() -> list[dict]:
    return query(
        "SELECT * FROM vtx_invite_rewards WHERE status = 'approved' AND loyalty_granted = 0"
    )


def mark_loyalty_granted(reward_id: int) -> None:
    execute("UPDATE vtx_invite_rewards SET loyalty_granted = 1 WHERE id = %s", (reward_id,))


def mark_participation_granted(reward_id: int) -> None:
    execute("UPDATE vtx_invite_rewards SET participation_granted = 1 WHERE id = %s", (reward_id,))


def get_rewards_for_review() -> list[dict]:
    return query(
        "SELECT * FROM vtx_invite_rewards "
        "WHERE status = 'pending' AND (flagged = 1 OR reason IN ('review', 'rate-limit')) "
        "ORDER BY created_at ASC"
    )


def has_event_participation(discord_id: str) -> bool:
    row = query_one(
        "SELECT 1 FROM vtx_registrations WHERE discord_id = %s AND status = 'confirmed' "
        "UNION ALL "
        "SELECT 1 FROM vtx_game_players gp "
        "JOIN vtx_players p ON p.id = gp.player_id WHERE p.discord_id = %s LIMIT 1",
        (discord_id, discord_id),
    )
    return row is not None


def increment_user_message(discord_id: str) -> None:
    with get_db() as conn:
        _exec(conn,
            "INSERT INTO vtx_user_messages (discord_id, count) VALUES (%s, 1) "
            "ON CONFLICT(discord_id) DO UPDATE SET count = vtx_user_messages.count + 1",
            (discord_id,),
        )


def get_user_message_count(discord_id: str) -> int:
    row = query_one("SELECT count FROM vtx_user_messages WHERE discord_id = %s", (discord_id,))
    return row["count"] if row else 0


def set_kv(key: str, value: str) -> None:
    execute(
        "INSERT INTO vtx_kv_store (key, value) VALUES (%s, %s) "
        "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
        (key, value),
    )


def get_season() -> int:
    try:
        return max(1, int(get_kv("season", "1") or "1"))
    except ValueError:
        return 1


def bump_season() -> int:
    new_season = get_season() + 1
    set_kv("season", str(new_season))
    return new_season


def snapshot_season(season: int) -> int:
    """Store each player's season-end stats under the given season number.
    Returns the number of players snapshotted."""
    board = get_players_leaderboard(season)
    snapshot_time = __import__("datetime").datetime.utcnow().isoformat()
    saved = 0
    with get_db() as conn:
        for i, row in enumerate(board, 1):
            _exec(conn,
                "INSERT INTO vtx_season_stats "
                "(season, discord_id, pr, kills, wins, games, avg_placement, position) "
                "VALUES (%s, %s, %s, %s, %s, %s, %s, %s) "
                "ON CONFLICT (season, discord_id) DO UPDATE SET "
                "pr = EXCLUDED.pr, kills = EXCLUDED.kills, wins = EXCLUDED.wins, "
                "games = EXCLUDED.games, avg_placement = EXCLUDED.avg_placement, "
                "position = EXCLUDED.position",
                (
                    season,
                    row["discord_id"],
                    row.get("pr") or 0,
                    row.get("total_kills") or 0,
                    row.get("total_wins") or 0,
                    row.get("total_games") or 0,
                    row.get("avg_placement"),
                    i,
                ),
            )
            saved += 1
    set_kv(f"season_{season}_snapshot_at", snapshot_time)
    return saved


def get_season_stats(season: int, discord_id: str) -> dict | None:
    """Season stats for a player. Snapshot for past seasons, live for the current one."""
    if season == get_season():
        player = get_player_stats(discord_id, season=season)
        if not player:
            return None
        return {
            "season": season,
            "discord_id": discord_id,
            "pr": player["player"].get("pr") or 0,
            "kills": player["total_kills"],
            "wins": player["total_wins"],
            "games": player["total_games"],
            "avg_placement": player["avg_placement"],
            "position": get_player_position(discord_id),
            "is_snapshot": False,
        }
    return query_one(
        "SELECT * FROM vtx_season_stats WHERE season = %s AND discord_id = %s",
        (season, discord_id),
    ) or None


def season_reset() -> int:
    """Snap the closing season's stats, transfer PR to the lifetime total,
    zero season PR, then move to the next season."""
    from datetime import datetime

    closing_season = get_season()
    snapshot_season(closing_season)

    players = query("SELECT discord_id FROM vtx_players")
    for p in players:
        execute(
            "UPDATE vtx_players SET total_pr = total_pr + pr, pr = 0 "
            "WHERE discord_id = %s",
            (p["discord_id"],),
        )
    season = bump_season()
    set_kv("last_reset_at", datetime.utcnow().isoformat())
    return len(players)


def start_season() -> int:
    """Begin a new season without touching stats (announcement not dispatched yet)."""
    return bump_season()


# ============================ Phase 1: new architecture core model ============================

EVENT_TYPES = ("cup", "scrim", "bracket", "qualifier")
ENTRY_MODES = ("open", "pr_limited", "division")
SCORING_MODES = ("normal", "placement_only", "coins")

EVENT_COLUMNS = {
    "name", "status", "channel_id", "signup_channel_id", "updates_channel_id",
    "dispatch_channel_id", "room_code", "region", "event_format", "max_players",
    "team_size", "total_games", "current_game", "point_kill", "point_win",
    "placement_scale", "qualification_enabled", "place_1", "place_2", "place_3",
    "place_4plus", "pr_multiplier", "shoot_timer", "scheduled_at",
    "event_type", "entry_mode", "pr_cap", "required_division_id",
    "scoring_mode", "awards_pr", "coins_enabled", "qualifier_requirements",
}


def create_event_record(name: str, **fields) -> int:
    """Insert an event using any whitelisted column values. Returns the new event id."""
    allowed = {k: v for k, v in fields.items() if k in EVENT_COLUMNS}
    allowed["name"] = name
    cols = ", ".join(allowed)
    placeholders = ", ".join(["%s"] * len(allowed))
    return execute(
        f"INSERT INTO vtx_events ({cols}) VALUES ({placeholders})",
        tuple(allowed.values()),
    )


# ------------------------------------------------------------------ entry checks


def get_player_divisions(discord_id: str) -> list[dict]:
    return query(
        "SELECT d.* FROM vtx_division_members dm JOIN vtx_divisions d ON dm.division_id = d.id "
        "WHERE dm.discord_id = %s ORDER BY d.name",
        (discord_id,),
    )


def get_player_division_ids(discord_id: str) -> list[int]:
    return [d["id"] for d in get_player_divisions(discord_id)]


def get_event_qualifier_sources(event_id: int) -> list[int]:
    """Qualifier events that gate entry into `event_id` (via qualifier_requirements.target_event_id)."""
    sources = []
    for ev in query(
        "SELECT id, qualifier_requirements FROM vtx_events "
        "WHERE event_type = 'qualifier' AND qualifier_requirements IS NOT NULL"
    ):
        try:
            req = json.loads(ev["qualifier_requirements"]) or {}
        except (json.JSONDecodeError, TypeError):
            continue
        if int(req.get("target_event_id") or 0) == event_id:
            sources.append(ev["id"])
    return sources


def check_event_entry(event_id: int, discord_id: str) -> dict:
    """Entry validation for a cup/bracket: PR cap, required division, qualifier gating.
    Returns {"ok": True} or {"ok": False, "reason": "..."}."""
    ev = get_event(event_id)
    if not ev:
        return {"ok": False, "reason": "Event not found."}
    if (ev.get("event_type") or "cup") == "scrim":
        return {"ok": True}

    mode = ev.get("entry_mode") or "open"
    if mode == "pr_limited":
        cap = ev.get("pr_cap")
        if cap:
            p = query_one("SELECT pr FROM vtx_players WHERE discord_id = %s", (discord_id,))
            pr = (p["pr"] if p else 0) or 0
            if pr > cap:
                return {
                    "ok": False,
                    "reason": f"Your PR (**{pr}**) exceeds the **{cap} PR** cap for this cup.",
                }
    if mode == "division":
        div_id = ev.get("required_division_id")
        if div_id and int(div_id) not in get_player_division_ids(discord_id):
            div = get_division(int(div_id))
            label = f"the **{div['name']}** division" if div else "a specific division"
            return {"ok": False, "reason": f"This cup requires {label} to enter."}

    for src in get_event_qualifier_sources(event_id):
        q = query_one(
            "SELECT 1 FROM vtx_event_qualifiers WHERE event_id = %s AND discord_id = %s",
            (src, discord_id),
        )
        if not q:
            src_ev = get_event(src)
            name = src_ev["name"] if src_ev else "the qualifier"
            return {
                "ok": False,
                "reason": f"You must qualify via **{name}** to join this cup.",
            }
    return {"ok": True}


# ------------------------------------------------------------------ lobbies


def _create_lobby_with_players(
    event_id: int, lobby_number: int, max_players: int, discord_ids: list[str]
) -> dict:
    name = f"Lobby {lobby_number}"
    lid = execute(
        "INSERT INTO vtx_lobbies (event_id, name, lobby_number, max_players, status) "
        "VALUES (%s, %s, %s, %s, 'open')",
        (event_id, name, lobby_number, max_players),
    )
    for did in discord_ids:
        p = query_one("SELECT id FROM vtx_players WHERE discord_id = %s", (did,))
        if p:
            execute(
                "INSERT INTO vtx_lobby_players (lobby_id, player_id) VALUES (%s, %s) ON CONFLICT DO NOTHING",
                (lid, p["id"]),
            )
    return get_lobby(lid)


def auto_split_lobbies(event_id: int, force: bool = False) -> list[dict]:
    """Split confirmed registrations into vtx_lobbies when seats exceed the event cap.
    Teams stay together (a team too big for the cap gets its own lobby).
    Returns the lobby rows ([] when no split is needed)."""
    ev = get_event(event_id)
    if not ev:
        return []
    max_players = int(ev.get("max_players") or 100)
    if max_players <= 0:
        return []
    regs = get_event_registrations(event_id)
    if not regs:
        return []

    seats = []
    for reg in regs:
        members = [reg["discord_id"]]
        members += [m.strip() for m in (reg.get("team_members") or "").split(",") if m.strip()]
        seats.append((reg["discord_id"], members))
    if sum(len(members) for _, members in seats) <= max_players:
        return []

    existing = get_event_lobbies(event_id)
    if existing and not force:
        return existing
    if force:
        for l in existing:
            execute("DELETE FROM vtx_lobby_players WHERE lobby_id = %s", (l["id"],))
            execute("DELETE FROM vtx_lobbies WHERE id = %s", (l["id"],))

    created = []
    lobby_number = 0
    cur: list[str] = []
    cur_seats = 0
    for _, members in seats:
        if cur and cur_seats + len(members) > max_players:
            lobby_number += 1
            created.append(_create_lobby_with_players(event_id, lobby_number, max_players, cur))
            cur, cur_seats = [], 0
        if len(members) > max_players:
            if cur:
                lobby_number += 1
                created.append(_create_lobby_with_players(event_id, lobby_number, max_players, cur))
                cur, cur_seats = [], 0
            lobby_number += 1
            created.append(_create_lobby_with_players(event_id, lobby_number, max_players, members))
            continue
        cur.extend(members)
        cur_seats += len(members)
    if cur:
        lobby_number += 1
        created.append(_create_lobby_with_players(event_id, lobby_number, max_players, cur))
    return created


def _lobby_number_map(event_id: int) -> dict:
    return {
        lp["player_id"]: lp["lobby_number"]
        for lp in query(
            "SELECT lp.player_id, l.lobby_number FROM vtx_lobby_players lp "
            "JOIN vtx_lobbies l ON lp.lobby_id = l.id WHERE l.event_id = %s",
            (event_id,),
        )
    }


def get_event_leaderboard_with_lobbies(event_id: int) -> list[dict]:
    """Event-wide leaderboard; every player/team row is tagged with its lobby number
    (None when the event has no lobbies)."""
    ev = get_event(event_id)
    if not ev:
        return []
    if (ev.get("team_size") or 1) >= 2:
        rows = get_team_leaderboard(event_id)
    else:
        rows = get_leaderboard(event_id)
    lobby_map = _lobby_number_map(event_id)
    for row in rows:
        lead_id = row.get("lead_id") or row.get("discord_id")
        if lead_id:
            p = query_one("SELECT id FROM vtx_players WHERE discord_id = %s", (lead_id,))
            row["lobby_number"] = lobby_map.get(p["id"]) if p else None
        else:
            row["lobby_number"] = None
    return rows


def get_lobby_sessions(lobby_id: int) -> list[dict]:
    return query(
        "SELECT * FROM vtx_sessions WHERE lobby_id = %s ORDER BY session_number",
        (lobby_id,),
    )


def get_lobby_active_session(lobby_id: int) -> dict | None:
    return query_one(
        "SELECT * FROM vtx_sessions WHERE lobby_id = %s AND status = 'in_progress' "
        "ORDER BY session_number DESC LIMIT 1",
        (lobby_id,),
    )


def get_lobby_matches(lobby_id: int) -> list[dict]:
    return query(
        "SELECT g.* FROM vtx_games g JOIN vtx_sessions s ON g.session_id = s.id "
        "WHERE s.lobby_id = %s ORDER BY g.game_number",
        (lobby_id,),
    )


def get_lobby_leaderboard(lobby_id: int) -> list[dict]:
    """Player leaderboard accumulated across all matches of a lobby's sessions."""
    lobby = get_lobby(lobby_id)
    if not lobby:
        return []
    ev = get_event(lobby["event_id"])
    rows = query(
        "SELECT p.id, COALESCE(p.game_username, p.username) AS username, p.discord_id, "
        "SUM(gp.points) AS total_points, "
        "SUM(gp.kills) AS total_kills, "
        "MAX(gp.is_disqualified) AS is_dq "
        "FROM vtx_game_players gp "
        "JOIN vtx_players p ON gp.player_id = p.id "
        "JOIN vtx_games g ON gp.game_id = g.id "
        "JOIN vtx_sessions s ON g.session_id = s.id "
        "WHERE s.lobby_id = %s "
        "GROUP BY p.id "
        "ORDER BY is_dq ASC, total_points DESC",
        (lobby_id,),
    )
    counts = query(
        "SELECT gp.player_id, COUNT(gp.id) AS cnt "
        "FROM vtx_game_players gp JOIN vtx_games g ON gp.game_id = g.id "
        "JOIN vtx_sessions s ON g.session_id = s.id "
        "WHERE s.lobby_id = %s GROUP BY gp.player_id",
        (lobby_id,),
    )
    placements = query(
        "SELECT gp.player_id, gp.placement "
        "FROM vtx_game_players gp JOIN vtx_games g ON gp.game_id = g.id "
        "JOIN vtx_sessions s ON g.session_id = s.id "
        "WHERE s.lobby_id = %s AND gp.placement IS NOT NULL "
        "ORDER BY g.game_number",
        (lobby_id,),
    )
    return _enrich_rows(ev, rows, counts, placements)


# ------------------------------------------------------------------ sessions & matches


def create_session(event_id: int, lobby_id: int | None = None) -> dict:
    """Create the next pending session for an event (or for one of its lobbies)."""
    last = query_one(
        "SELECT COALESCE(MAX(session_number), 0) AS n FROM vtx_sessions "
        "WHERE event_id = %s AND lobby_id IS %s",
        (event_id, lobby_id),
    )
    session_number = (last["n"] if last else 0) + 1
    sid = execute(
        "INSERT INTO vtx_sessions (event_id, session_number, status, lobby_id) "
        "VALUES (%s, %s, 'pending', %s)",
        (event_id, session_number, lobby_id),
    )
    return get_session(sid)


def create_match(
    event_id: int,
    session_id: int | None = None,
    room_code: str = "",
    status: str = "in_progress",
) -> int:
    """Create the next match (game) for an event, optionally inside a session.
    Returns the new match id."""
    row = query_one(
        "SELECT COALESCE(MAX(game_number), 0) AS n FROM vtx_games WHERE event_id = %s",
        (event_id,),
    )
    return create_game_record(
        event_id, (row["n"] if row else 0) + 1, room_code, status, session_id
    )


def register_match_players(
    match_id: int, event_id: int, lobby_id: int | None = None
) -> int:
    """Copy the source roster (lobby players when a lobby is given, else event
    players) into a match. Returns the roster size after insertion."""
    if lobby_id:
        source = query(
            "SELECT p.* FROM vtx_lobby_players lp JOIN vtx_players p ON lp.player_id = p.id "
            "WHERE lp.lobby_id = %s ORDER BY lp.joined_at",
            (lobby_id,),
        )
    else:
        source = get_event_players(event_id)
    for p in source:
        execute(
            "INSERT INTO vtx_game_players (game_id, player_id) VALUES (%s, %s) ON CONFLICT DO NOTHING",
            (match_id, p["id"]),
        )
    count = query_one(
        "SELECT COUNT(*) AS c FROM vtx_game_players WHERE game_id = %s", (match_id,)
    )
    return count["c"] if count else 0


def get_active_match(
    session_id: int | None = None,
    lobby_id: int | None = None,
    event_id: int | None = None,
) -> dict | None:
    """The in-progress match for a session, lobby, or event (checked in that order)."""
    if session_id:
        return query_one(
            "SELECT * FROM vtx_games WHERE session_id = %s AND status = 'in_progress' "
            "ORDER BY game_number DESC LIMIT 1",
            (session_id,),
        )
    if lobby_id:
        return query_one(
            "SELECT g.* FROM vtx_games g JOIN vtx_sessions s ON g.session_id = s.id "
            "WHERE s.lobby_id = %s AND g.status = 'in_progress' "
            "ORDER BY g.game_number DESC LIMIT 1",
            (lobby_id,),
        )
    if event_id:
        return query_one(
            "SELECT * FROM vtx_games WHERE event_id = %s AND status = 'in_progress' "
            "ORDER BY game_number DESC LIMIT 1",
            (event_id,),
        )
    return None


def get_match_state(match_id: int) -> dict:
    """Live state of a match: roster with elimination flags and alive/total counts."""
    match = query_one("SELECT * FROM vtx_games WHERE id = %s", (match_id,))
    players = get_game_players(match_id) if match else []
    alive = sum(1 for p in players if not p["eliminated"])
    return {
        "match": match,
        "players": players,
        "alive": alive,
        "total": len(players),
        "winner": next((p for p in players if p.get("placement") == 1), None),
    }


def get_match_team_state(match_id: int) -> dict:
    """Team-level elimination summary for a match (teams alive / total)."""
    teams = query(
        "SELECT team_lead_id, COUNT(*) AS total, "
        "COALESCE(SUM(eliminated), 0) AS eliminated_count "
        "FROM vtx_game_team_members WHERE game_id = %s GROUP BY team_lead_id",
        (match_id,),
    )
    alive_teams = sum(1 for t in teams if (t["eliminated_count"] or 0) < t["total"])
    return {
        "teams": teams,
        "alive_teams": alive_teams,
        "total_teams": len(teams),
    }


def eliminate_match_player(match_id: int, player_id: int) -> dict:
    """Mark a player eliminated. Their match placement is set to the number of
    alive players at the moment of elimination (the last player standing gets 1st).
    Returns the remaining alive count and the winner when only one is left."""
    gp = query_one(
        "SELECT * FROM vtx_game_players WHERE game_id = %s AND player_id = %s",
        (match_id, player_id),
    )
    if not gp:
        return {"ok": False, "error": "Player is not in this match."}
    if gp["eliminated"]:
        return {"ok": False, "error": "Player is already eliminated."}
    alive = query_one(
        "SELECT COUNT(*) AS c FROM vtx_game_players WHERE game_id = %s AND eliminated = 0",
        (match_id,),
    )["c"]
    execute(
        "UPDATE vtx_game_players SET eliminated = 1, eliminated_at = CURRENT_TIMESTAMP, "
        "placement = COALESCE(placement, %s) WHERE id = %s",
        (alive, gp["id"]),
    )
    alive_left = alive - 1
    result = {"ok": True, "placement": alive, "alive_left": alive_left}
    if alive_left == 1:
        winner = query_one(
            "SELECT gp.*, COALESCE(p.game_username, p.username) AS username, p.discord_id "
            "FROM vtx_game_players gp JOIN vtx_players p ON gp.player_id = p.id "
            "WHERE gp.game_id = %s AND gp.eliminated = 0",
            (match_id,),
        )
        if winner:
            execute("UPDATE vtx_game_players SET placement = 1 WHERE id = %s", (winner["id"],))
            result["winner"] = winner
    return result


def resolve_match_placements(match_id: int) -> dict | None:
    """Finalize placements: the last player standing gets 1st. Returns the winner row."""
    alive = query(
        "SELECT gp.*, COALESCE(p.game_username, p.username) AS username, p.discord_id "
        "FROM vtx_game_players gp JOIN vtx_players p ON gp.player_id = p.id "
        "WHERE gp.game_id = %s AND gp.eliminated = 0",
        (match_id,),
    )
    if len(alive) == 1:
        execute("UPDATE vtx_game_players SET placement = 1 WHERE id = %s", (alive[0]["id"],))
        return alive[0]
    return None


def end_match(match_id: int) -> dict:
    """Mark a match completed; when exactly one player remains they are the winner.
    Returns {"ok": True, "winner": row | None}."""
    execute(
        "UPDATE vtx_games SET status = 'completed', ended_at = CURRENT_TIMESTAMP "
        "WHERE id = %s AND status != 'completed'",
        (match_id,),
    )
    return {"ok": True, "winner": resolve_match_placements(match_id)}


# ------------------------------------------------------------------ divisions


def create_division(name: str, role_id: str, guild_id: str) -> dict:
    """Create a division (idempotent by name). Returns the division row."""
    existing = query_one("SELECT * FROM vtx_divisions WHERE name = %s", (name,))
    if existing:
        return existing
    did = execute(
        "INSERT INTO vtx_divisions (name, role_id, guild_id) VALUES (%s, %s, %s)",
        (name, role_id, guild_id),
    )
    return get_division(did)


def get_division(division_id: int) -> dict | None:
    return query_one("SELECT * FROM vtx_divisions WHERE id = %s", (division_id,))


def get_divisions() -> list[dict]:
    return query("SELECT * FROM vtx_divisions ORDER BY name")


def delete_division(division_id: int) -> None:
    execute("DELETE FROM vtx_division_members WHERE division_id = %s", (division_id,))
    execute("DELETE FROM vtx_divisions WHERE id = %s", (division_id,))


def add_division_member(
    division_id: int, discord_id: str, qualified_from_event_id: int | None = None
) -> dict:
    execute(
        "INSERT INTO vtx_division_members (division_id, discord_id, qualified_from_event_id) "
        "VALUES (%s, %s, %s) ON CONFLICT DO NOTHING",
        (division_id, discord_id, qualified_from_event_id),
    )
    return query_one(
        "SELECT * FROM vtx_division_members WHERE division_id = %s AND discord_id = %s",
        (division_id, discord_id),
    )


def remove_division_member(division_id: int, discord_id: str) -> bool:
    with get_db() as conn:
        cur = _exec(conn,
            "DELETE FROM vtx_division_members WHERE division_id = %s AND discord_id = %s",
            (division_id, discord_id),
        )
        return cur.rowcount > 0


def get_division_members(division_id: int) -> list[dict]:
    return query(
        "SELECT dm.*, p.username FROM vtx_division_members dm "
        "LEFT JOIN vtx_players p ON dm.discord_id = p.discord_id "
        "WHERE dm.division_id = %s ORDER BY dm.created_at",
        (division_id,),
    )


# ------------------------------------------------------------------ qualifiers


def set_event_qualifier_requirements(event_id: int, requirements: dict) -> None:
    execute(
        "UPDATE vtx_events SET qualifier_requirements = %s WHERE id = %s",
        (json.dumps(requirements or {}), event_id),
    )


def get_event_qualifier_requirements(event_id: int) -> dict:
    ev = get_event(event_id)
    if not ev:
        return {}
    try:
        return json.loads(ev.get("qualifier_requirements") or "{}") or {}
    except (json.JSONDecodeError, TypeError):
        return {}


def evaluate_qualifier(event_id: int) -> dict:
    """Evaluate a completed qualifier event against its requirements.
    Returns {"qualified": [...], "requirements": {...}}."""
    ev = get_event(event_id)
    if not ev:
        return {"qualified": [], "requirements": {}}
    req = get_event_qualifier_requirements(event_id)
    if not req:
        return {"qualified": [], "requirements": {}}
    top = int(req.get("top") or 0)
    min_kills = int(req.get("min_kills") or 0)
    min_wins = int(req.get("min_wins") or 0)

    if (ev.get("team_size") or 1) >= 2:
        board = get_team_leaderboard(event_id)
    else:
        board = get_leaderboard(event_id)

    qualified = []
    for i, row in enumerate(board, 1):
        if top and i > top:
            break
        did = row.get("lead_id") or row.get("discord_id")
        if not did:
            continue
        if min_kills and (row.get("total_kills") or 0) < min_kills:
            continue
        if min_wins and (row.get("wins") or 0) < min_wins:
            continue
        qualified.append({
            "discord_id": did,
            "username": row.get("username") or row.get("team_name", ""),
            "placement": i,
            "wins": row.get("wins") or 0,
            "kills": row.get("total_kills") or 0,
            "team_members": row.get("team_members"),
        })
    return {"qualified": qualified, "requirements": req}


def grant_qualification(
    event_id: int, discord_id: str, username: str, team_members: str | None = None
) -> dict:
    """Record a player as qualified; if the qualifier targets a division, also add
    them to it. Returns {"ok": True, "division_id": int | None}."""
    add_event_qualifier(event_id, discord_id, username, team_members)
    req = get_event_qualifier_requirements(event_id)
    division_id = req.get("target_division_id")
    if division_id:
        add_division_member(int(division_id), discord_id, qualified_from_event_id=event_id)
    return {"ok": True, "division_id": division_id}


# ------------------------------------------------------------------ brackets


def _get_or_create_bracket_match(event_id: int, round_num: int, position: int) -> dict:
    match = query_one(
        "SELECT * FROM vtx_bracket_matches WHERE event_id = %s AND round = %s AND position = %s",
        (event_id, round_num, position),
    )
    if match:
        return match
    mid = execute(
        "INSERT INTO vtx_bracket_matches (event_id, round, position, status) "
        "VALUES (%s, %s, %s, 'ready')",
        (event_id, round_num, position),
    )
    return query_one("SELECT * FROM vtx_bracket_matches WHERE id = %s", (mid,))


def _bracket_round_count(event_id: int) -> int:
    row = query_one(
        "SELECT COALESCE(MAX(round), 1) AS r FROM vtx_bracket_matches WHERE event_id = %s",
        (event_id,),
    )
    return row["r"] if row else 1


def _resolve_bracket_byes(event_id: int) -> None:
    """Auto-advance lone players through empty bracket matches (cascading byes)."""
    changed = True
    while changed:
        changed = False
        matches = query(
            "SELECT * FROM vtx_bracket_matches WHERE event_id = %s ORDER BY round, position",
            (event_id,),
        )
        by_round: dict[int, list[dict]] = {}
        for m in matches:
            by_round.setdefault(m["round"], []).append(m)
        max_round = max(by_round) if by_round else 1
        for r in range(1, max_round):
            for m in by_round.get(r, []):
                if m["status"] == "done" or m["winner_id"]:
                    continue
                p1, p2 = m["player1_id"], m["player2_id"]
                if p1 and not p2:
                    winner = p1
                elif p2 and not p1:
                    winner = p2
                else:
                    continue
                execute(
                    "UPDATE vtx_bracket_matches SET winner_id = %s, status = 'done' WHERE id = %s",
                    (winner, m["id"]),
                )
                nxt = _get_or_create_bracket_match(
                    event_id, r + 1, (m["position"] - 1) // 2 + 1
                )
                if nxt["player1_id"] is None:
                    execute(
                        "UPDATE vtx_bracket_matches SET player1_id = %s WHERE id = %s",
                        (winner, nxt["id"]),
                    )
                elif nxt["player2_id"] is None:
                    execute(
                        "UPDATE vtx_bracket_matches SET player2_id = %s WHERE id = %s",
                        (winner, nxt["id"]),
                    )
                changed = True


def seed_bracket(event_id: int) -> dict:
    """Seed a 1v1 single-elimination bracket for a bracket event, ordered by PR
    (highest first). Returns {"ok": True, "rounds": int, "champion": id|None}."""
    ev = get_event(event_id)
    if not ev:
        return {"ok": False, "error": "Event not found."}
    if (ev.get("event_type") or "cup") != "bracket":
        return {"ok": False, "error": "Event is not a bracket."}
    if query_one("SELECT 1 FROM vtx_bracket_matches WHERE event_id = %s LIMIT 1", (event_id,)):
        return {"ok": False, "error": "Bracket is already seeded."}

    players = get_event_players(event_id)
    players.sort(key=lambda p: (p.get("pr") or 0), reverse=True)
    if not players:
        return {"ok": False, "error": "No players to seed."}
    if len(players) == 1:
        return {"ok": True, "rounds": 1, "champion": players[0]["id"]}

    matches = []
    for i in range(0, len(players), 2):
        p1 = players[i]
        p2 = players[i + 1] if i + 1 < len(players) else None
        mid = execute(
            "INSERT INTO vtx_bracket_matches (event_id, round, position, player1_id, player2_id, status) "
            "VALUES (%s, 1, %s, %s, %s, 'ready')",
            (event_id, i // 2 + 1, p1["id"], p2["id"] if p2 else None),
        )
        matches.append(mid)

    round_num = 1
    while len(matches) > 1:
        round_num += 1
        nxt = []
        for pos in range(1, (len(matches) + 1) // 2 + 1):
            mid = _get_or_create_bracket_match(event_id, round_num, pos)
            nxt.append(mid)
        matches = nxt

    _resolve_bracket_byes(event_id)
    return {"ok": True, "rounds": round_num, "champion": None}


def get_bracket_matches(event_id: int) -> list[dict]:
    return query(
        "SELECT m.*, "
        "COALESCE(p1.game_username, p1.username) AS player1_name, p1.discord_id AS player1_discord_id, "
        "COALESCE(p2.game_username, p2.username) AS player2_name, p2.discord_id AS player2_discord_id, "
        "COALESCE(pw.game_username, pw.username) AS winner_name, pw.discord_id AS winner_discord_id "
        "FROM vtx_bracket_matches m "
        "LEFT JOIN vtx_players p1 ON m.player1_id = p1.id "
        "LEFT JOIN vtx_players p2 ON m.player2_id = p2.id "
        "LEFT JOIN vtx_players pw ON m.winner_id = pw.id "
        "WHERE m.event_id = %s ORDER BY m.round, m.position",
        (event_id,),
    )


def advance_bracket_winner(match_id: int, winner_id: int) -> dict:
    """Record a bracket match winner and advance them to the next round.
    Returns {"ok": True, "finished": bool, "next_match": int|None} — finished when
    the final match was won."""
    match = query_one("SELECT * FROM vtx_bracket_matches WHERE id = %s", (match_id,))
    if not match:
        return {"ok": False, "error": "Match not found."}
    if match["winner_id"]:
        return {"ok": False, "error": "Match is already decided."}
    if winner_id not in (match["player1_id"], match["player2_id"]):
        return {"ok": False, "error": "Winner is not part of this match."}
    execute(
        "UPDATE vtx_bracket_matches SET winner_id = %s, status = 'done' WHERE id = %s",
        (winner_id, match_id),
    )
    rounds_total = _bracket_round_count(match["event_id"])
    if match["round"] >= rounds_total:
        return {"ok": True, "finished": True, "next_match": None}
    nxt = _get_or_create_bracket_match(
        match["event_id"], match["round"] + 1, (match["position"] - 1) // 2 + 1
    )
    if nxt["player1_id"] is None:
        execute("UPDATE vtx_bracket_matches SET player1_id = %s WHERE id = %s", (winner_id, nxt["id"]))
    elif nxt["player2_id"] is None:
        execute("UPDATE vtx_bracket_matches SET player2_id = %s WHERE id = %s", (winner_id, nxt["id"]))
    return {"ok": True, "finished": False, "next_match": nxt["id"]}


def get_bracket_placements(event_id: int) -> list[dict]:
    """Final standings from the bracket tree: the final winner is 1st; losers of
    round r (1-based, final = rounds_total) place within
    (2**(rounds_total - r + 1) - 1, 2**(rounds_total - r + 1)]."""
    rounds_total = _bracket_round_count(event_id)
    matches = query(
        "SELECT * FROM vtx_bracket_matches WHERE event_id = %s AND winner_id IS NOT NULL "
        "ORDER BY round DESC, position",
        (event_id,),
    )
    losers_by_round: dict[int, list[int]] = {}
    for m in matches:
        if m["winner_id"] == m["player1_id"]:
            loser = m["player2_id"]
        else:
            loser = m["player1_id"]
        if loser:
            losers_by_round.setdefault(m["round"], []).append(loser)

    standings = []
    final = next((m for m in matches if m["round"] == rounds_total), None)
    if final and final["winner_id"]:
        standings.append({"player_id": final["winner_id"], "placement": 1})
    for r in sorted(losers_by_round, reverse=True):
        band_lo = 2 ** (rounds_total - r + 1) - 1
        band_hi = 2 ** (rounds_total - r + 1)
        for i, pid in enumerate(losers_by_round[r]):
            placement = min(band_lo + 1 + i, band_hi)
            standings.append({"player_id": pid, "placement": placement})
    standings.sort(key=lambda s: s["placement"])
    return standings


# ------------------------------------------------------------------ duel asks


DUEL_ASK_TTL_SECONDS = 900


def create_duel_ask(asker_id: str, partner_id: str | None, target_ids: list[str]) -> int:
    """Record a pending 1v1/2v2 ask. Returns the ask id."""
    return execute(
        "INSERT INTO vtx_duel_asks (asker_id, partner_id, target_ids, status, expires_at) "
        "VALUES (%s, %s, %s, 'pending', %s)",
        (asker_id, partner_id, json.dumps(list(target_ids)),
         int(time.time()) + DUEL_ASK_TTL_SECONDS),
    )


def get_duel_ask(ask_id: int) -> dict | None:
    return query_one("SELECT * FROM vtx_duel_asks WHERE id = %s", (ask_id,))


def get_pending_duel_asks() -> list[dict]:
    """All non-expired asks (pending or accepted) — used to re-attach buttons."""
    return query(
        "SELECT * FROM vtx_duel_asks WHERE status IN ('pending', 'accepted') "
        "ORDER BY id ASC"
    )


def set_duel_ask_status(ask_id: int, status: str) -> None:
    execute("UPDATE vtx_duel_asks SET status = %s WHERE id = %s", (status, ask_id))


def set_duel_ask_channels(
    ask_id: int, text_channel_id: str, voice_channel_id: str, category_id: str
) -> None:
    execute(
        "UPDATE vtx_duel_asks SET text_channel_id = %s, voice_channel_id = %s, category_id = %s "
        "WHERE id = %s",
        (text_channel_id, voice_channel_id, category_id, ask_id),
    )


def expire_stale_duel_asks() -> int:
    """Mark pending asks past their TTL as expired. Returns how many were expired."""
    with get_db() as conn:
        cur = _exec(conn,
            "UPDATE vtx_duel_asks SET status = 'expired' "
            "WHERE status = 'pending' AND expires_at <= %s",
            (int(time.time()),),
        )
        return cur.rowcount


def get_stale_duel_asks() -> list[dict]:
    """Return pending asks past their TTL (for channel cleanup), expiring them."""
    with get_db() as conn:
        rows = _exec(conn,
            "SELECT * FROM vtx_duel_asks WHERE status = 'pending' AND expires_at <= %s",
            (int(time.time()),),
        ).fetchall()
        if rows:
            _exec(conn,
                "UPDATE vtx_duel_asks SET status = 'expired' "
                "WHERE status = 'pending' AND expires_at <= %s",
                (int(time.time()),),
            )
        return list(rows)


# ------------------------------------------------------------------ coins


def remove_coins(discord_id: str, amount: int) -> int:
    """Subtract coins (floor at 0). Returns the new balance."""
    with get_db() as conn:
        row = _exec(conn,
            "SELECT coins FROM vtx_invite_coins WHERE discord_id = %s", (discord_id,)
        ).fetchone()
        new_balance = max(0, (row["coins"] if row else 0) - int(amount))
        _exec(conn,
            "INSERT INTO vtx_invite_coins (discord_id, coins) VALUES (%s, %s) "
            "ON CONFLICT(discord_id) DO UPDATE SET coins = %s, updated_at = CURRENT_TIMESTAMP",
            (discord_id, new_balance, new_balance),
        )
        return new_balance


def reset_coins(discord_id: str) -> int:
    """Zero a player's coin balance. Returns the previous balance."""
    with get_db() as conn:
        row = _exec(conn,
            "SELECT coins FROM vtx_invite_coins WHERE discord_id = %s", (discord_id,)
        ).fetchone()
        old = row["coins"] if row else 0
        _exec(conn,
            "INSERT INTO vtx_invite_coins (discord_id, coins) VALUES (%s, 0) "
            "ON CONFLICT(discord_id) DO UPDATE SET coins = 0, updated_at = CURRENT_TIMESTAMP",
            (discord_id,),
        )
        return old


def award_coins(discord_id: str, amount: int) -> int:
    """Credit coins to a player (used by coins cups). Returns the new balance."""
    return add_coins(discord_id, amount)


# ============================ Phase 2: scoring & entry helpers ============================


def event_awards_pr(ev: dict | None) -> bool:
    """Whether an event awards PR (coins cups and explicitly PR-less events don't)."""
    if not ev:
        return False
    if (ev.get("scoring_mode") or "normal") == "coins":
        return False
    return bool(ev.get("awards_pr", 1))


def award_coins_for_placements(game_id: int, event_id: int) -> int:
    """Pay out placement-scale values as invite coins (coins cups only).
    Returns how many players were paid."""
    ev = get_event(event_id)
    if not ev or not ev.get("coins_enabled"):
        return 0
    try:
        scale = json.loads(ev.get("placement_scale") or "[]")
    except (json.JSONDecodeError, TypeError):
        scale = []
    if not scale:
        return 0
    rows = query(
        "SELECT player_id, placement FROM vtx_game_players "
        "WHERE game_id = %s AND placement IS NOT NULL",
        (game_id,),
    )
    paid = 0
    for row in rows:
        p = row["placement"]
        if p and 1 <= p <= len(scale):
            did_row = query_one(
                "SELECT discord_id FROM vtx_players WHERE id = %s", (row["player_id"],)
            )
            if did_row:
                add_coins(did_row["discord_id"], int(scale[p - 1]))
                paid += 1
    return paid


def get_lobby_latest_session(lobby_id: int) -> dict | None:
    return query_one(
        "SELECT * FROM vtx_sessions WHERE lobby_id = %s ORDER BY session_number DESC LIMIT 1",
        (lobby_id,),
    )


def get_lobby_leaderboard_full(lobby_id: int) -> list[dict]:
    """Full lobby leaderboard with derived stats (alias used by dashboard)."""
    return get_lobby_leaderboard(lobby_id)


def finalize_bracket(event_id: int) -> dict:
    """Turn bracket standings into a completed game record with placements,
    apply placement points, pay out coins (coins cups), and mark the event
    completed. Returns {"ok": True, "game_id", "standings"} or an error."""
    ev = get_event(event_id)
    if not ev:
        return {"ok": False, "error": "Event not found."}
    if (ev.get("event_type") or "cup") != "bracket":
        return {"ok": False, "error": "Not a bracket event."}
    standings = get_bracket_placements(event_id)
    if not standings:
        return {"ok": False, "error": "The bracket has no finished matches yet."}
    if not any(s["placement"] == 1 for s in standings):
        return {"ok": False, "error": "The bracket final is not decided yet."}

    row = query_one(
        "SELECT COALESCE(MAX(game_number), 0) AS n FROM vtx_games WHERE event_id = %s",
        (event_id,),
    )
    gid = create_game_record(
        event_id, (row["n"] if row else 0) + 1, ev.get("room_code") or "", "completed"
    )
    placed_ids = set()
    for s in standings:
        execute(
            "INSERT INTO vtx_game_players (game_id, player_id, placement) "
            "VALUES (%s, %s, %s) ON CONFLICT DO NOTHING",
            (gid, s["player_id"], s["placement"]),
        )
        placed_ids.add(s["player_id"])
    for p in get_event_players(event_id):
        if p["id"] not in placed_ids:
            execute(
                "INSERT INTO vtx_game_players (game_id, player_id) VALUES (%s, %s) ON CONFLICT DO NOTHING",
                (gid, p["id"]),
            )
    apply_placement_points(gid, event_id)
    if ev.get("coins_enabled"):
        award_coins_for_placements(gid, event_id)
    execute("UPDATE vtx_events SET status = 'completed' WHERE id = %s", (event_id,))
    return {"ok": True, "game_id": gid, "standings": standings}
