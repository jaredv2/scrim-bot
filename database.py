from __future__ import annotations

import json
import os
import sqlite3
import threading
import time
from contextlib import contextmanager
from pathlib import Path

from config import ensure_data_dir, settings

_local = threading.local()
_db_epoch = 0
_db_epoch_lock = threading.Lock()
_last_restart_stamp = 0

_connections: list[sqlite3.Connection] = []
_connections_lock = threading.Lock()

INIT_LOCK = threading.Lock()

RESTART_FLAG_PREFIX = "."


def _register_connection(conn: sqlite3.Connection) -> None:
    with _connections_lock:
        _connections.append(conn)


def _unregister_connection(conn: sqlite3.Connection) -> None:
    with _connections_lock:
        if conn in _connections:
            _connections.remove(conn)


def _close_all_connections() -> None:
    """Close every tracked connection in this process (thread-safe; connections
    are created with check_same_thread=False so cross-thread close is legal)."""
    with _connections_lock:
        conns, _connections[:] = _connections[:], []
    for conn in conns:
        try:
            conn.close()
        except Exception:
            pass


def _restart_flag_path() -> Path:
    """Cross-process marker: the dashboard writes a timestamp here after a
    restore so the bot process knows the database file was swapped."""
    return Path(settings.database_path).with_name(
        RESTART_FLAG_PREFIX + Path(settings.database_path).name + ".restart"
    )


def _bump_epoch() -> None:
    """Close every cached connection in the process and force a reopen on the
    next use (threads reopen lazily because their cached epoch goes stale)."""
    global _db_epoch
    with _db_epoch_lock:
        _db_epoch += 1
    _close_all_connections()
    if getattr(_local, "conn", None) is not None:
        _local.conn = None


def reload_db_if_needed() -> bool:
    """Polled by the bot's worker loop. If the dashboard restored a database
    file in the other process, close cached connections now so the next access
    reopens the new file. Returns True when a reload was triggered."""
    global _last_restart_stamp
    flag = _restart_flag_path()
    try:
        stamp = int(flag.read_text().strip()) if flag.exists() else -1
    except Exception:
        return False
    if stamp <= _last_restart_stamp:
        return False
    _last_restart_stamp = stamp
    _bump_epoch()
    return True


def reload_db_now() -> None:
    """Called by the restoring process itself (dashboard): drop the cached
    connection immediately so subsequent requests hit the new file."""
    _bump_epoch()


def mark_db_restored() -> None:
    """Write the cross-process marker so the bot process reloads on its next poll."""
    flag = _restart_flag_path()
    flag.parent.mkdir(parents=True, exist_ok=True)
    flag.write_text(str(int(time.time() * 1000)))

SCHEMA = """
CREATE TABLE IF NOT EXISTS players (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    discord_id TEXT UNIQUE NOT NULL,
    username TEXT NOT NULL,
    game_id TEXT,
    game_username TEXT,
    country TEXT,
    region TEXT,
    pr INTEGER DEFAULT 0,
    total_pr INTEGER DEFAULT 0,
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE IF NOT EXISTS events (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    name TEXT NOT NULL,
    status TEXT DEFAULT 'setup',
    channel_id TEXT,
    signup_channel_id TEXT,
    updates_channel_id TEXT,
    dispatch_channel_id TEXT,
    room_code TEXT,
    region TEXT DEFAULT 'EU',
    event_format TEXT DEFAULT 'ZoneWars',
    max_players INTEGER DEFAULT 100,
    team_size INTEGER DEFAULT 1,
    total_games INTEGER DEFAULT 1,
    current_game INTEGER DEFAULT 0,
    live_feed_message_id TEXT,
    register_button_message_id TEXT,
    point_kill INTEGER DEFAULT 1,
    point_win INTEGER DEFAULT 5,
    placement_scale TEXT DEFAULT '[10,8,6,4,2,1]',
    qualification_enabled INTEGER DEFAULT 0,
    place_1 TEXT,
    place_2 TEXT,
    place_3 TEXT,
    place_4plus TEXT,
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE IF NOT EXISTS registrations (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    event_id INTEGER REFERENCES events(id),
    discord_id TEXT NOT NULL,
    username TEXT NOT NULL,
    team_members TEXT,
    skin TEXT,
    message_id TEXT,
    status TEXT DEFAULT 'pending',
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    UNIQUE(event_id, discord_id)
);

CREATE TABLE IF NOT EXISTS pending_registrations (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    event_id INTEGER REFERENCES events(id),
    discord_id TEXT NOT NULL,
    prompt_message_id TEXT NOT NULL,
    skin TEXT,
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE IF NOT EXISTS sessions (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    event_id INTEGER REFERENCES events(id),
    session_number INTEGER NOT NULL,
    status TEXT DEFAULT 'pending',
    room_code TEXT,
    current_match INTEGER DEFAULT 0,
    started_at TIMESTAMP,
    ended_at TIMESTAMP,
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE IF NOT EXISTS games (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    event_id INTEGER REFERENCES events(id),
    game_number INTEGER NOT NULL,
    room_code TEXT,
    status TEXT DEFAULT 'pending',
    season INTEGER DEFAULT 1,
    started_at TIMESTAMP,
    ended_at TIMESTAMP
);

CREATE TABLE IF NOT EXISTS game_players (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    game_id INTEGER REFERENCES games(id),
    player_id INTEGER REFERENCES players(id),
    kills INTEGER DEFAULT 0,
    placement INTEGER,
    points INTEGER DEFAULT 0,
    is_disqualified INTEGER DEFAULT 0,
    UNIQUE(game_id, player_id)
);

CREATE TABLE IF NOT EXISTS lobbies (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    event_id INTEGER REFERENCES events(id),
    name TEXT NOT NULL,
    room_code TEXT,
    status TEXT DEFAULT 'open',
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE IF NOT EXISTS lobby_players (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    lobby_id INTEGER REFERENCES lobbies(id),
    player_id INTEGER REFERENCES players(id),
    joined_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    UNIQUE(lobby_id, player_id)
);

CREATE TABLE IF NOT EXISTS kills (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    game_id INTEGER REFERENCES games(id),
    killer_id INTEGER REFERENCES players(id),
    victim_id INTEGER REFERENCES players(id),
    weapon TEXT,
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE IF NOT EXISTS game_team_members (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    game_id INTEGER REFERENCES games(id),
    discord_id TEXT NOT NULL,
    team_lead_id TEXT NOT NULL,
    eliminated INTEGER DEFAULT 0,
    eliminated_at TIMESTAMP,
    UNIQUE(game_id, discord_id)
);

CREATE TABLE IF NOT EXISTS command_queue (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    command TEXT NOT NULL,
    params TEXT NOT NULL DEFAULT '{}',
    status TEXT DEFAULT 'pending',
    result TEXT,
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    executed_at TIMESTAMP
);

CREATE TABLE IF NOT EXISTS bot_logs (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    event_id INTEGER,
    action TEXT NOT NULL,
    details TEXT,
    user_id TEXT,
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE IF NOT EXISTS bans (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    discord_id TEXT UNIQUE NOT NULL,
    reason TEXT,
    banned_until TEXT,
    created_by TEXT,
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE IF NOT EXISTS rank_tiers (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    name TEXT UNIQUE NOT NULL,
    pr_min INTEGER NOT NULL
);

CREATE TABLE IF NOT EXISTS kv_store (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS season_stats (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    season INTEGER NOT NULL,
    discord_id TEXT NOT NULL,
    pr REAL NOT NULL DEFAULT 0,
    kills INTEGER NOT NULL DEFAULT 0,
    wins INTEGER NOT NULL DEFAULT 0,
    games INTEGER NOT NULL DEFAULT 0,
    avg_placement REAL,
    position INTEGER,
    UNIQUE(season, discord_id)
);

CREATE TABLE IF NOT EXISTS event_qualifiers (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    event_id INTEGER REFERENCES events(id) NOT NULL,
    discord_id TEXT NOT NULL,
    username TEXT NOT NULL,
    team_members TEXT,
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    UNIQUE(event_id, discord_id)
);

CREATE TABLE IF NOT EXISTS event_interests (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    event_id INTEGER REFERENCES events(id) NOT NULL,
    discord_id TEXT NOT NULL,
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    UNIQUE(event_id, discord_id)
);
"""

DEFAULT_RANK_TIERS = [
    ("Unranked", 0),
    ("Bronze I", 20),
    ("Bronze II", 40),
    ("Bronze III", 60),
    ("Silver I", 80),
    ("Silver II", 110),
    ("Silver III", 140),
    ("Gold I", 170),
    ("Gold II", 210),
    ("Gold III", 250),
    ("Platinum I", 300),
    ("Platinum II", 360),
    ("Platinum III", 420),
    ("Diamond I", 500),
    ("Diamond II", 600),
    ("Diamond III", 700),
    ("Elite I", 850),
    ("Elite II", 1000),
    ("Elite III", 1200),
    ("Champion I", 1400),
    ("Champion II", 1650),
    ("Champion III", 1900),
    ("Unreal I", 2200),
    ("Unreal II", 2600),
    ("Unreal III", 3200),
]


def _get_conn() -> sqlite3.Connection:
    if getattr(_local, "conn", None) is not None:
        if getattr(_local, "conn_epoch", -1) != _db_epoch:
            try:
                _local.conn.close()
            except Exception:
                pass
            _local.conn = None
    if getattr(_local, "conn", None) is None:
        ensure_data_dir()
        conn = sqlite3.connect(
            settings.database_path,
            timeout=5.0,
            check_same_thread=False,
        )
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA foreign_keys=ON")
        conn.execute("PRAGMA busy_timeout=5000")
        _local.conn = conn
        _local.conn_epoch = _db_epoch
        _register_connection(conn)
    return _local.conn


def init_db() -> None:
    with INIT_LOCK:
        conn = _get_conn()
        conn.executescript(SCHEMA)
        _migrate(conn)
        _seed_rank_tiers(conn)
        conn.commit()


def _add_col(conn: sqlite3.Connection, ddl: str) -> None:
    """Add a column, ignoring 'duplicate column name' from concurrent startup races."""
    try:
        conn.execute(ddl)
    except sqlite3.OperationalError as e:
        if "duplicate column name" in str(e).lower():
            return
        raise


def _migrate(conn: sqlite3.Connection) -> None:
    cols = {row[1] for row in conn.execute("PRAGMA table_info(events)")}
    if "point_kill" not in cols:
        _add_col(conn, "ALTER TABLE events ADD COLUMN point_kill INTEGER DEFAULT 1")
    if "point_win" not in cols:
        _add_col(conn, "ALTER TABLE events ADD COLUMN point_win INTEGER DEFAULT 5")
    if "live_feed_message_id" not in cols:
        _add_col(conn, "ALTER TABLE events ADD COLUMN live_feed_message_id TEXT")
    if "signup_channel_id" not in cols:
        _add_col(conn, "ALTER TABLE events ADD COLUMN signup_channel_id TEXT")
    if "updates_channel_id" not in cols:
        _add_col(conn, "ALTER TABLE events ADD COLUMN updates_channel_id TEXT")
    if "dispatch_channel_id" not in cols:
        _add_col(conn, "ALTER TABLE events ADD COLUMN dispatch_channel_id TEXT")
    if "register_button_message_id" not in cols:
        _add_col(conn, "ALTER TABLE events ADD COLUMN register_button_message_id TEXT")
    if "team_roles" not in cols:
        _add_col(conn, "ALTER TABLE events ADD COLUMN team_roles TEXT")
    if "placement_scale" not in cols:
        _add_col(conn, "ALTER TABLE events ADD COLUMN placement_scale TEXT DEFAULT '[10,8,6,4,2,1]'")
    if "qualification_enabled" not in cols:
        _add_col(conn, "ALTER TABLE events ADD COLUMN qualification_enabled INTEGER DEFAULT 0")
    if "place_1" not in cols:
        _add_col(conn, "ALTER TABLE events ADD COLUMN place_1 TEXT")
    if "place_2" not in cols:
        _add_col(conn, "ALTER TABLE events ADD COLUMN place_2 TEXT")
    if "place_3" not in cols:
        _add_col(conn, "ALTER TABLE events ADD COLUMN place_3 TEXT")
    if "place_4plus" not in cols:
        _add_col(conn, "ALTER TABLE events ADD COLUMN place_4plus TEXT")
    if "pr_multiplier" not in cols:
        _add_col(conn, "ALTER TABLE events ADD COLUMN pr_multiplier REAL")
    if "shoot_timer" not in cols:
        _add_col(conn, "ALTER TABLE events ADD COLUMN shoot_timer INTEGER DEFAULT 0")
    if "scheduled_at" not in cols:
        _add_col(conn, "ALTER TABLE events ADD COLUMN scheduled_at INTEGER")
    if "schedule_message_id" not in cols:
        _add_col(conn, "ALTER TABLE events ADD COLUMN schedule_message_id TEXT")
    if "schedule_channel_id" not in cols:
        _add_col(conn, "ALTER TABLE events ADD COLUMN schedule_channel_id TEXT")
    if "reminder_sent" not in cols:
        _add_col(conn, "ALTER TABLE events ADD COLUMN reminder_sent INTEGER DEFAULT 0")

    player_cols = {row[1] for row in conn.execute("PRAGMA table_info(players)")}
    if "pr" not in player_cols:
        _add_col(conn, "ALTER TABLE players ADD COLUMN pr INTEGER DEFAULT 0")
    if "game_id" not in player_cols:
        _add_col(conn, "ALTER TABLE players ADD COLUMN game_id TEXT")
    if "country" not in player_cols:
        _add_col(conn, "ALTER TABLE players ADD COLUMN country TEXT")
    if "region" not in player_cols:
        _add_col(conn, "ALTER TABLE players ADD COLUMN region TEXT")

    game_cols = {row[1] for row in conn.execute("PRAGMA table_info(games)")}
    if "session_id" not in game_cols:
        _add_col(conn, "ALTER TABLE games ADD COLUMN session_id INTEGER")
    if "total_pr" not in player_cols:
        _add_col(conn, "ALTER TABLE players ADD COLUMN total_pr INTEGER DEFAULT 0")

    game_cols = {row[1] for row in conn.execute("PRAGMA table_info(games)")}
    if "season" not in game_cols:
        _add_col(conn, "ALTER TABLE games ADD COLUMN season INTEGER DEFAULT 1")

    reg_cols = {row[1] for row in conn.execute("PRAGMA table_info(registrations)")}
    if "skin" not in reg_cols:
        _add_col(conn, "ALTER TABLE registrations ADD COLUMN skin TEXT")

    pending_cols = {row[1] for row in conn.execute("PRAGMA table_info(pending_registrations)")}
    if "skin" not in pending_cols:
        _add_col(conn, "ALTER TABLE pending_registrations ADD COLUMN skin TEXT")

    tables = {row[0] for row in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    if "invite_coins" not in tables:
        conn.execute(
            "CREATE TABLE IF NOT EXISTS invite_coins ("
            "discord_id TEXT PRIMARY KEY,"
            "coins INTEGER NOT NULL DEFAULT 0,"
            "total_invites INTEGER NOT NULL DEFAULT 0,"
            "updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP"
            ")"
        )
    if "coin_purchases" not in tables:
        conn.execute(
            "CREATE TABLE IF NOT EXISTS coin_purchases ("
            "id INTEGER PRIMARY KEY AUTOINCREMENT,"
            "discord_id TEXT NOT NULL,"
            "product TEXT NOT NULL,"
            "role_id TEXT NOT NULL,"
            "guild_id TEXT NOT NULL DEFAULT '',"
            "issued_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,"
            "expires_at INTEGER"
            ")"
        )
    if "invite_rewards" not in tables:
        conn.execute(
            "CREATE TABLE IF NOT EXISTS invite_rewards ("
            "id INTEGER PRIMARY KEY AUTOINCREMENT,"
            "guild_id TEXT NOT NULL,"
            "inviter_id TEXT NOT NULL,"
            "invited_user_id TEXT NOT NULL,"
            "status TEXT NOT NULL DEFAULT 'pending',"
            "created_at INTEGER,"
            "left_at INTEGER,"
            "approved_at INTEGER,"
            "quality_score INTEGER,"
            "coins_granted INTEGER NOT NULL DEFAULT 0,"
            "flagged INTEGER NOT NULL DEFAULT 0,"
            "loyalty_granted INTEGER NOT NULL DEFAULT 0,"
            "participation_granted INTEGER NOT NULL DEFAULT 0,"
            "reason TEXT"
            ")"
        )
    if "user_messages" not in tables:
        conn.execute(
            "CREATE TABLE IF NOT EXISTS user_messages ("
            "discord_id TEXT PRIMARY KEY,"
            "count INTEGER NOT NULL DEFAULT 0"
            ")"
        )
    if "kv_store" not in tables:
        conn.execute(
            "CREATE TABLE IF NOT EXISTS kv_store ("
            "key TEXT PRIMARY KEY,"
            "value TEXT NOT NULL"
            ")"
        )
    if "season_stats" not in tables:
        conn.execute(
            "CREATE TABLE IF NOT EXISTS season_stats ("
            "id INTEGER PRIMARY KEY AUTOINCREMENT,"
            "season INTEGER NOT NULL,"
            "discord_id TEXT NOT NULL,"
            "pr REAL NOT NULL DEFAULT 0,"
            "kills INTEGER NOT NULL DEFAULT 0,"
            "wins INTEGER NOT NULL DEFAULT 0,"
            "games INTEGER NOT NULL DEFAULT 0,"
            "avg_placement REAL,"
            "position INTEGER,"
            "UNIQUE(season, discord_id)"
            ")"
        )
    if "event_qualifiers" not in tables:
        conn.execute(
            "CREATE TABLE IF NOT EXISTS event_qualifiers ("
            "id INTEGER PRIMARY KEY AUTOINCREMENT,"
            "event_id INTEGER REFERENCES events(id) NOT NULL,"
            "discord_id TEXT NOT NULL,"
            "username TEXT NOT NULL,"
            "team_members TEXT,"
            "created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,"
            "UNIQUE(event_id, discord_id)"
            ")"
        )
    if "bans" not in tables:
        conn.execute(
            "CREATE TABLE IF NOT EXISTS bans ("
            "id INTEGER PRIMARY KEY AUTOINCREMENT,"
            "discord_id TEXT UNIQUE NOT NULL,"
            "reason TEXT,"
            "banned_until TEXT,"
            "created_by TEXT,"
            "created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP"
            ")"
        )
    if "rank_tiers" not in tables:
        conn.execute(
            "CREATE TABLE IF NOT EXISTS rank_tiers ("
            "id INTEGER PRIMARY KEY AUTOINCREMENT,"
            "name TEXT UNIQUE NOT NULL,"
            "pr_min INTEGER NOT NULL"
            ")"
        )
    if "pending_registrations" not in tables:
        conn.execute(
            "CREATE TABLE IF NOT EXISTS pending_registrations ("
            "id INTEGER PRIMARY KEY AUTOINCREMENT,"
            "event_id INTEGER REFERENCES events(id),"
            "discord_id TEXT NOT NULL,"
            "prompt_message_id TEXT NOT NULL,"
            "created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP"
            ")"
        )
    if "game_team_members" not in tables:
        conn.execute(
            "CREATE TABLE IF NOT EXISTS game_team_members ("
            "id INTEGER PRIMARY KEY AUTOINCREMENT,"
            "game_id INTEGER REFERENCES games(id),"
            "discord_id TEXT NOT NULL,"
            "team_lead_id TEXT NOT NULL,"
            "eliminated INTEGER DEFAULT 0,"
            "eliminated_at TIMESTAMP,"
            "UNIQUE(game_id, discord_id)"
            ")"
        )


def _seed_rank_tiers(conn: sqlite3.Connection) -> None:
    conn.execute("DELETE FROM rank_tiers")
    for name, pr_min in DEFAULT_RANK_TIERS:
        conn.execute(
            "INSERT INTO rank_tiers (name, pr_min) VALUES (?, ?)",
            (name, pr_min),
        )


def get_rank_tiers() -> list[dict]:
    return query(
        "SELECT * FROM rank_tiers ORDER BY pr_min DESC"
    )


def get_rank_for_pr(pr: int) -> dict | None:
    tiers = query("SELECT * FROM rank_tiers ORDER BY pr_min DESC")
    for tier in tiers:
        if pr >= tier["pr_min"]:
            return tier
    return tiers[-1] if tiers else None


@contextmanager
def get_db():
    conn = _get_conn()
    try:
        yield conn
        conn.commit()
    except Exception:
        conn.rollback()
        raise


def query(sql: str, params: tuple = ()) -> list[dict]:
    with get_db() as conn:
        rows = conn.execute(sql, params).fetchall()
        return [dict(r) for r in rows]


def query_one(sql: str, params: tuple = ()) -> dict | None:
    with get_db() as conn:
        row = conn.execute(sql, params).fetchone()
        return dict(row) if row else None


def execute(sql: str, params: tuple = ()) -> int:
    with get_db() as conn:
        cursor = conn.execute(sql, params)
        return cursor.lastrowid


def execute_many(sql: str, params_list: list[tuple]) -> None:
    with get_db() as conn:
        conn.executemany(sql, params_list)


def upsert_player(discord_id: str, username: str) -> dict:
    existing = query_one(
        "SELECT * FROM players WHERE discord_id = ?", (discord_id,)
    )
    if existing:
        execute(
            "UPDATE players SET username = ? WHERE discord_id = ?",
            (username, discord_id),
        )
        return query_one("SELECT * FROM players WHERE discord_id = ?", (discord_id,))
    player_id = execute(
        "INSERT INTO players (discord_id, username) VALUES (?, ?)",
        (discord_id, username),
    )
    return query_one("SELECT * FROM players WHERE id = ?", (player_id,))


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
        return query_one("SELECT * FROM players WHERE discord_id = ?", (discord_id,))
    sets = ", ".join(f"{k} = ?" for k in allowed)
    execute(f"UPDATE players SET {sets} WHERE discord_id = ?", (*allowed.values(), discord_id))
    return query_one("SELECT * FROM players WHERE discord_id = ?", (discord_id,))


def get_event(event_id: int) -> dict | None:
    return query_one("SELECT * FROM events WHERE id = ?", (event_id,))


def get_active_events() -> list[dict]:
    return query(
        "SELECT * FROM events WHERE status IN ('setup', 'registration', 'in_progress') "
        "ORDER BY created_at DESC"
    )


def get_event_registrations(event_id: int) -> list[dict]:
    return query(
        "SELECT * FROM registrations WHERE event_id = ? AND status = 'confirmed' "
        "ORDER BY created_at",
        (event_id,),
    )


def get_event_players(event_id: int) -> list[dict]:
    return query(
        "SELECT p.*, r.team_members FROM registrations r "
        "JOIN players p ON r.discord_id = p.discord_id "
        "WHERE r.event_id = ? AND r.status = 'confirmed' "
        "ORDER BY r.created_at",
        (event_id,),
    )


def get_event_games(event_id: int) -> list[dict]:
    return query(
        "SELECT * FROM games WHERE event_id = ? ORDER BY game_number",
        (event_id,),
    )


def get_game_players(game_id: int) -> list[dict]:
    return query(
        "SELECT gp.*, COALESCE(p.game_username, p.username) AS username, p.discord_id "
        "FROM game_players gp "
        "JOIN players p ON gp.player_id = p.id "
        "WHERE gp.game_id = ? ORDER BY gp.points DESC",
        (game_id,),
    )


def get_game_team_leaderboard(game_id: int, event_id: int) -> list[dict]:
    registrations = query(
        "SELECT * FROM registrations WHERE event_id = ? AND status = 'confirmed' "
        "AND team_members IS NOT NULL AND team_members != '' "
        "ORDER BY created_at",
        (event_id,),
    )
    if not registrations:
        return []

    game = query_one("SELECT id FROM games WHERE id = ?", (game_id,))
    if not game:
        return []

    result = []
    for reg in registrations:
        all_ids = [reg["discord_id"]]
        if reg["team_members"]:
            all_ids.extend(mid.strip() for mid in reg["team_members"].split(",") if mid.strip())

        player_ids = []
        for did in all_ids:
            p = query_one("SELECT id FROM players WHERE discord_id = ?", (did,))
            if p:
                player_ids.append(p["id"])

        total_points = 0
        total_kills = 0
        is_dq = 0
        best_placement = None
        for pid in player_ids:
            gp = query_one(
                "SELECT points, kills, is_disqualified, placement "
                "FROM game_players WHERE game_id = ? AND player_id = ?",
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
        "FROM kills k "
        "JOIN players pk ON k.killer_id = pk.id "
        "JOIN players pv ON k.victim_id = pv.id "
        "WHERE k.game_id = ? ORDER BY k.created_at",
        (game_id,),
    )


def get_lobby(lobby_id: int) -> dict | None:
    return query_one("SELECT * FROM lobbies WHERE id = ?", (lobby_id,))


def get_lobby_players(lobby_id: int) -> list[dict]:
    return query(
        "SELECT p.* FROM lobby_players lp "
        "JOIN players p ON lp.player_id = p.id "
        "WHERE lp.lobby_id = ? ORDER BY lp.joined_at",
        (lobby_id,),
    )


def get_event_lobbies(event_id: int) -> list[dict]:
    return query(
        "SELECT * FROM lobbies WHERE event_id = ? ORDER BY created_at",
        (event_id,),
    )


def get_leaderboard(event_id: int) -> list[dict]:
    rows = query(
        "SELECT p.id, COALESCE(p.game_username, p.username) AS username, p.discord_id, "
        "SUM(gp.points) AS total_points, "
        "SUM(gp.kills) AS total_kills, "
        "MAX(gp.is_disqualified) AS is_dq "
        "FROM game_players gp "
        "JOIN players p ON gp.player_id = p.id "
        "JOIN games g ON gp.game_id = g.id "
        "WHERE g.event_id = ? "
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


def _enrich_solo_leaderboard(event_id: int, rows: list[dict]) -> list[dict]:
    ev = get_event(event_id)
    scale = _parse_placement_scale(ev) if ev else []

    counts = query(
        "SELECT gp.player_id, COUNT(gp.id) AS cnt "
        "FROM game_players gp JOIN games g ON gp.game_id = g.id "
        "WHERE g.event_id = ? GROUP BY gp.player_id",
        (event_id,),
    )
    cnt_map = {c["player_id"]: c["cnt"] for c in counts}

    placements = query(
        "SELECT gp.player_id, gp.placement "
        "FROM game_players gp JOIN games g ON gp.game_id = g.id "
        "WHERE g.event_id = ? AND gp.placement IS NOT NULL "
        "ORDER BY g.game_number",
        (event_id,),
    )
    plc_map = {}
    for p in placements:
        plc_map.setdefault(p["player_id"], []).append(p["placement"])

    for row in rows:
        games_played = cnt_map.get(row["id"], 0)
        pls = plc_map.get(row["id"], [])
        row["games_played"] = games_played
        row["wins"] = sum(1 for p in pls if p == 1)
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


def get_solo_leaderboard(event_id: int) -> list[dict]:
    registrations = query(
        "SELECT discord_id, team_members FROM registrations "
        "WHERE event_id = ? AND status = 'confirmed'",
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
            "FROM game_players gp "
            "JOIN players p ON gp.player_id = p.id "
            "JOIN games g ON gp.game_id = g.id "
            "WHERE g.event_id = ? "
            "GROUP BY p.id "
            "ORDER BY is_dq ASC, total_points DESC",
            (event_id,),
        )

    ph = ",".join(["?"] * len(team_player_ids))
    return query(
        f"SELECT p.username, p.discord_id, SUM(gp.points) AS total_points, "
        f"SUM(gp.kills) AS total_kills, "
        f"MAX(gp.is_disqualified) AS is_dq "
        f"FROM game_players gp "
        f"JOIN players p ON gp.player_id = p.id "
        f"JOIN games g ON gp.game_id = g.id "
        f"WHERE g.event_id = ? "
        f"AND p.discord_id NOT IN ({ph}) "
        f"GROUP BY p.id "
        f"ORDER BY is_dq ASC, total_points DESC",
        (event_id, *team_player_ids),
    )


def get_team_leaderboard(event_id: int) -> list[dict]:
    registrations = query(
        "SELECT * FROM registrations WHERE event_id = ? AND status = 'confirmed' "
        "AND team_members IS NOT NULL AND team_members != '' "
        "ORDER BY created_at",
        (event_id,),
    )
    if not registrations:
        return []

    ev = get_event(event_id)
    scale = _parse_placement_scale(ev) if ev else []

    games = query("SELECT id FROM games WHERE event_id = ?", (event_id,))
    game_ids = [g["id"] for g in games]

    result = []
    for reg in registrations:
        all_ids = [reg["discord_id"]]
        if reg["team_members"]:
            all_ids.extend(mid.strip() for mid in reg["team_members"].split(",") if mid.strip())

        player_ids = []
        for did in all_ids:
            p = query_one("SELECT id FROM players WHERE discord_id = ?", (did,))
            if p:
                player_ids.append(p["id"])

        total_points = 0
        total_kills = 0
        is_dq = 0
        played_game_ids = set()
        best_by_game = {}
        for pid in player_ids:
            if game_ids:
                ph = ",".join(["?"] * len(game_ids))
                rows = query(
                    f"SELECT COALESCE(SUM(points),0) AS pts, COALESCE(SUM(kills),0) AS k, "
                    f"MAX(is_disqualified) AS dq FROM game_players "
                    f"WHERE game_id IN ({ph}) AND player_id = ?",
                    (*game_ids, pid),
                )
                if rows:
                    total_points += rows[0]["pts"]
                    total_kills += rows[0]["k"]
                    if rows[0]["dq"]:
                        is_dq = 1
                member_games = query(
                    f"SELECT DISTINCT game_id FROM game_players WHERE game_id IN ({ph}) AND player_id = ?",
                    (*game_ids, pid),
                )
                for mg in member_games:
                    played_game_ids.add(mg["game_id"])
                placements = query(
                    f"SELECT game_id, placement FROM game_players "
                    f"WHERE game_id IN ({ph}) AND player_id = ? AND placement IS NOT NULL",
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
    player = query_one("SELECT * FROM players WHERE discord_id = ?", (discord_id,))
    if not player:
        return None

    rows = query(
        "SELECT gp.placement, gp.points, gp.kills, g.game_number "
        "FROM game_players gp JOIN games g ON gp.game_id = g.id "
        "WHERE g.event_id = ? AND gp.player_id = ? "
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
        "INSERT INTO command_queue (command, params) VALUES (?, ?)",
        (command, json.dumps(params or {})),
    )


def pop_command() -> dict | None:
    with get_db() as conn:
        row = conn.execute(
            "SELECT * FROM command_queue WHERE status = 'pending' "
            "ORDER BY created_at ASC LIMIT 1"
        ).fetchone()
        if not row:
            return None
        cmd = dict(row)
        conn.execute(
            "UPDATE command_queue SET status = 'running' WHERE id = ?",
            (cmd["id"],),
        )
        conn.commit()
        return cmd


def complete_command(cmd_id: int, result: str = "ok") -> None:
    execute(
        "UPDATE command_queue SET status = 'done', result = ?, "
        "executed_at = CURRENT_TIMESTAMP WHERE id = ?",
        (result, cmd_id),
    )


def fail_command(cmd_id: int, error: str) -> None:
    execute(
        "UPDATE command_queue SET status = 'failed', result = ? WHERE id = ?",
        (error, cmd_id),
    )


def get_pending_commands() -> list[dict]:
    return query(
        "SELECT * FROM command_queue WHERE status = 'pending' ORDER BY created_at"
    )


def log_bot_action(event_id: int | None, action: str, details: str = "", user_id: str = "") -> None:
    execute(
        "INSERT INTO bot_logs (event_id, action, details, user_id) VALUES (?, ?, ?, ?)",
        (event_id, action, details, user_id),
    )


def get_bot_logs(event_id: int | None = None, limit: int = 50) -> list[dict]:
    if event_id:
        return query(
            "SELECT * FROM bot_logs WHERE event_id = ? ORDER BY created_at DESC LIMIT ?",
            (event_id, limit),
        )
    return query(
        "SELECT * FROM bot_logs ORDER BY created_at DESC LIMIT ?",
        (limit,),
    )


def get_player_stats(
    discord_id: str,
    event_id: int | None = None,
    season: int | None = None,
) -> dict | None:
    player = query_one("SELECT * FROM players WHERE discord_id = ?", (discord_id,))
    if not player:
        return None

    if event_id:
        stats = query_one(
            "SELECT "
            "COUNT(CASE WHEN gp.placement = 1 THEN 1 END) AS total_wins, "
            "COALESCE(SUM(gp.kills), 0) AS total_kills, "
            "COUNT(gp.id) AS total_games, "
            "ROUND(AVG(gp.placement), 1) AS avg_placement "
            "FROM game_players gp "
            "JOIN games g ON gp.game_id = g.id "
            "WHERE gp.player_id = ? AND g.event_id = ?",
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
            "FROM game_players gp "
            "JOIN games g ON gp.game_id = g.id "
            "JOIN events e ON g.event_id = e.id "
            "WHERE gp.player_id = ? AND e.status = 'completed' AND g.season = ?",
            (player["id"], season),
        )

    return {
        "player": player,
        "total_wins": stats["total_wins"] if stats else 0,
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

    is_team = team_size >= 2
    registrations = query(
        "SELECT * FROM registrations WHERE event_id = ? AND status = 'confirmed'",
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

    games = query("SELECT id FROM games WHERE event_id = ?", (event_id,))
    game_ids = [g["id"] for g in games]
    if not game_ids:
        return {}

    ph = ",".join(["?"] * len(game_ids))

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
                p = query_one("SELECT id FROM players WHERE discord_id = ?", (did,))
                if not p:
                    continue
                rows = query(
                    f"SELECT COALESCE(SUM(points),0) AS pts, COALESCE(SUM(kills),0) AS k, "
                    f"COUNT(CASE WHEN placement = 1 THEN 1 END) AS w "
                    f"FROM game_players WHERE game_id IN ({ph}) AND player_id = ?",
                    (*game_ids, p["id"]),
                )
                if rows:
                    total_points += rows[0]["pts"]
                    total_kills += rows[0]["k"]
                    wins += rows[0]["w"]

            raw_pr = total_points + (total_kills * point_kill) + (wins * point_win)
            final_pr = raw_pr * multiplier / team_size
            result[reg["discord_id"]] = round(final_pr, 1)
        return result
    else:
        result = {}
        for reg in registrations:
            did = reg["discord_id"]
            p = query_one("SELECT id FROM players WHERE discord_id = ?", (did,))
            if not p:
                continue
            rows = query(
                f"SELECT COALESCE(SUM(points),0) AS pts, COALESCE(SUM(kills),0) AS k, "
                f"COUNT(CASE WHEN placement = 1 THEN 1 END) AS w "
                f"FROM game_players WHERE game_id IN ({ph}) AND player_id = ?",
                (*game_ids, p["id"]),
            )
            if rows:
                total_points = rows[0]["pts"]
                total_kills = rows[0]["k"]
                wins = rows[0]["w"]
            else:
                total_points = total_kills = wins = 0

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
        "UPDATE players SET pr = ? WHERE discord_id = ?",
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
        "SELECT id, player_id, placement FROM game_players WHERE game_id = ? AND placement IS NOT NULL",
        (game_id,),
    )
    for row in rows:
        p = row["placement"]
        if p and 1 <= p <= len(scale):
            pts = scale[p - 1]
            execute(
                "UPDATE game_players SET points = points + ? WHERE id = ?",
                (pts, row["id"]),
            )


def init_team_members(game_id: int, event_id: int) -> None:
    ev = get_event(event_id)
    if not ev or ev.get("team_size", 1) < 2:
        return
    registrations = query(
        "SELECT * FROM registrations WHERE event_id = ? AND status = 'confirmed' "
        "AND team_members IS NOT NULL AND team_members != ''",
        (event_id,),
    )
    for reg in registrations:
        all_ids = [reg["discord_id"]]
        if reg["team_members"]:
            all_ids.extend(mid.strip() for mid in reg["team_members"].split(",") if mid.strip())
        for did in all_ids:
            existing = query_one(
                "SELECT id FROM game_team_members WHERE game_id = ? AND discord_id = ?",
                (game_id, did),
            )
            if not existing:
                execute(
                    "INSERT INTO game_team_members (game_id, discord_id, team_lead_id) VALUES (?, ?, ?)",
                    (game_id, did, reg["discord_id"]),
                )


def mark_teammate_eliminated(game_id: int, discord_id: str) -> dict:
    existing = query_one(
        "SELECT * FROM game_team_members WHERE game_id = ? AND discord_id = ?",
        (game_id, discord_id),
    )
    if not existing:
        return {"ok": False, "error": "Player not found in game"}
    if existing["eliminated"]:
        return {"ok": False, "error": "Already eliminated"}

    execute(
        "UPDATE game_team_members SET eliminated = 1, eliminated_at = CURRENT_TIMESTAMP "
        "WHERE game_id = ? AND discord_id = ?",
        (game_id, discord_id),
    )

    team_lead_id = existing["team_lead_id"]
    total = query_one(
        "SELECT COUNT(*) AS cnt FROM game_team_members WHERE game_id = ? AND team_lead_id = ?",
        (game_id, team_lead_id),
    )
    eliminated = query_one(
        "SELECT COUNT(*) AS cnt FROM game_team_members WHERE game_id = ? AND team_lead_id = ? AND eliminated = 1",
        (game_id, team_lead_id),
    )
    all_eliminated = total["cnt"] > 0 and eliminated["cnt"] >= total["cnt"]

    return {"ok": True, "all_eliminated": all_eliminated, "team_lead_id": team_lead_id}


def get_team_elimination_status(game_id: int, team_lead_id: str) -> dict:
    total = query_one(
        "SELECT COUNT(*) AS cnt FROM game_team_members WHERE game_id = ? AND team_lead_id = ?",
        (game_id, team_lead_id),
    )
    eliminated = query_one(
        "SELECT COUNT(*) AS cnt FROM game_team_members WHERE game_id = ? AND team_lead_id = ? AND eliminated = 1",
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
        "UPDATE players SET pr = ? WHERE discord_id = ?",
        (pr, discord_id),
    )
    return pr


def add_player_pr(discord_id: str, amount: int) -> int:
    current = query_one(
        "SELECT pr FROM players WHERE discord_id = ?", (discord_id,)
    )
    new_pr = max(0, (current["pr"] if current else 0) + int(amount))
    execute(
        "UPDATE players SET pr = ? WHERE discord_id = ?",
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
        "INSERT INTO bans (discord_id, reason, banned_until, created_by) "
        "VALUES (?, ?, ?, ?) "
        "ON CONFLICT(discord_id) DO UPDATE SET "
        "reason = excluded.reason, "
        "banned_until = excluded.banned_until, "
        "created_by = excluded.created_by",
        (discord_id, reason, banned_until, created_by),
    )


def unban_player(discord_id: str) -> None:
    execute("DELETE FROM bans WHERE discord_id = ?", (discord_id,))


def get_player_ban(discord_id: str) -> dict | None:
    return query_one("SELECT * FROM bans WHERE discord_id = ?", (discord_id,))


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
        "SELECT discord_id, pr FROM players ORDER BY pr DESC, username ASC"
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
        "INSERT INTO games (event_id, game_number, room_code, status, season, session_id) "
        "VALUES (?, ?, ?, ?, ?, ?)",
        (event_id, game_number, room_code, status, get_season(), session_id),
    )


def create_session(event_id: int) -> dict:
    """Create the next pending session for an event (returns the session row)."""
    last = query_one(
        "SELECT COALESCE(MAX(session_number), 0) AS n FROM sessions WHERE event_id = ?",
        (event_id,),
    )
    session_number = (last["n"] if last else 0) + 1
    sid = execute(
        "INSERT INTO sessions (event_id, session_number, status) VALUES (?, ?, 'pending')",
        (event_id, session_number),
    )
    return get_session(sid)


def get_session(session_id: int) -> dict | None:
    return query_one("SELECT * FROM sessions WHERE id = ?", (session_id,))


def get_event_sessions(event_id: int) -> list[dict]:
    return query(
        "SELECT * FROM sessions WHERE event_id = ? ORDER BY session_number",
        (event_id,),
    )


def get_session_matches(session_id: int) -> list[dict]:
    return query(
        "SELECT * FROM games WHERE session_id = ? ORDER BY game_number",
        (session_id,),
    )


def get_latest_session(event_id: int) -> dict | None:
    return query_one(
        "SELECT * FROM sessions WHERE event_id = ? ORDER BY session_number DESC LIMIT 1",
        (event_id,),
    )


def get_event_active_session(event_id: int) -> dict | None:
    return query_one(
        "SELECT * FROM sessions WHERE event_id = ? AND status = 'in_progress' "
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
        "FROM game_players gp "
        "JOIN players p ON gp.player_id = p.id "
        "JOIN games g ON gp.game_id = g.id "
        "WHERE g.session_id = ? "
        "GROUP BY p.id "
        "ORDER BY is_dq ASC, total_points DESC",
        (session_id,),
    )

    placements = query(
        "SELECT gp.player_id, gp.placement "
        "FROM game_players gp JOIN games g ON gp.game_id = g.id "
        "WHERE g.session_id = ? AND gp.placement IS NOT NULL "
        "ORDER BY g.game_number",
        (session_id,),
    )
    plc_map = {}
    for p in placements:
        plc_map.setdefault(p["player_id"], []).append(p["placement"])

    for row in rows:
        pls = plc_map.get(row["id"], [])
        row["games_played"] = len(pls)
        row["wins"] = sum(1 for p in pls if p == 1)
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
        "COUNT(CASE WHEN gp.placement = 1 AND e.status = 'completed' THEN 1 END) AS total_wins, "
        "COALESCE(SUM(CASE WHEN e.status = 'completed' THEN gp.kills END), 0) AS total_kills, "
        "COUNT(CASE WHEN e.status = 'completed' THEN gp.id END) AS total_games, "
        "ROUND(AVG(CASE WHEN e.status = 'completed' THEN gp.placement END), 1) AS avg_placement "
        "FROM players p "
        "LEFT JOIN game_players gp ON gp.player_id = p.id "
        "LEFT JOIN games g ON gp.game_id = g.id AND g.season = ? "
        "LEFT JOIN events e ON g.event_id = e.id "
        "GROUP BY p.id "
        "ORDER BY p.pr DESC, total_wins DESC, total_kills DESC, p.username ASC",
        (season,),
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
    p = query_one("SELECT id FROM players WHERE discord_id = ?", (discord_id,))
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
        "FROM registrations r "
        "JOIN events e ON r.event_id = e.id "
        "LEFT JOIN games g ON g.event_id = e.id "
        "LEFT JOIN game_players gp ON gp.game_id = g.id AND gp.player_id = ? "
        "WHERE r.discord_id = ? AND e.status = 'completed' "
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
        "INSERT OR IGNORE INTO registrations "
        "(event_id, discord_id, username, status) VALUES (?, ?, ?, 'confirmed')",
        (event_id, discord_id, username),
    )
    return query_one("SELECT * FROM registrations WHERE event_id = ? AND discord_id = ?", (event_id, discord_id))


def remove_player_from_event(event_id: int, discord_id: str) -> bool:
    p = query_one("SELECT id FROM players WHERE discord_id = ?", (discord_id,))
    execute(
        "DELETE FROM registrations WHERE event_id = ? AND discord_id = ?",
        (event_id, discord_id),
    )

    regs = query(
        "SELECT * FROM registrations WHERE event_id = ? AND team_members LIKE ?",
        (event_id, f"%{discord_id}%"),
    )
    for reg in regs:
        members = [m.strip() for m in (reg["team_members"] or "").split(",") if m.strip()]
        remaining = [m for m in members if m != discord_id]
        if not remaining:
            execute("DELETE FROM registrations WHERE id = ?", (reg["id"],))
        else:
            execute(
                "UPDATE registrations SET team_members = ? WHERE id = ?",
                (",".join(remaining), reg["id"]),
            )

    for g in get_event_games(event_id):
        if p:
            execute(
                "DELETE FROM game_players WHERE game_id = ? AND player_id = ?",
                (g["id"], p["id"]),
            )
        execute(
            "DELETE FROM game_team_members WHERE game_id = ? AND discord_id = ?",
            (g["id"], discord_id),
        )

    for l in get_event_lobbies(event_id):
        if p:
            execute(
                "DELETE FROM lobby_players WHERE lobby_id = ? AND player_id = ?",
                (l["id"], p["id"]),
            )

    return True


def get_bans() -> list[dict]:
    return query(
        "SELECT b.*, p.username FROM bans b "
        "LEFT JOIN players p ON b.discord_id = p.discord_id "
        "ORDER BY b.created_at DESC"
    )


def get_ban_by_id(ban_id: int) -> dict | None:
    return query_one("SELECT * FROM bans WHERE id = ?", (ban_id,))


def get_event_qualifiers(event_id: int) -> list[dict]:
    return query(
        "SELECT * FROM event_qualifiers WHERE event_id = ? ORDER BY id ASC",
        (event_id,),
    )


def toggle_event_interest(event_id: int, discord_id: str) -> bool:
    """Toggle a player's interest in an event. Returns True if now interested."""
    with get_db() as conn:
        cur = conn.execute(
            "DELETE FROM event_interests WHERE event_id = ? AND discord_id = ?",
            (event_id, discord_id),
        )
        if cur.rowcount > 0:
            return False
        conn.execute(
            "INSERT INTO event_interests (event_id, discord_id) VALUES (?, ?)",
            (event_id, discord_id),
        )
        return True


def get_event_interested(event_id: int) -> list[dict]:
    return query(
        "SELECT discord_id FROM event_interests WHERE event_id = ? ORDER BY id ASC",
        (event_id,),
    )


def count_event_interests(event_id: int) -> int:
    row = query_one(
        "SELECT COUNT(*) AS c FROM event_interests WHERE event_id = ?",
        (event_id,),
    )
    return row["c"] if row else 0


def get_scheduled_events() -> list[dict]:
    return query(
        "SELECT * FROM events WHERE scheduled_at IS NOT NULL AND scheduled_at > 0 "
        "ORDER BY scheduled_at ASC",
    )


def set_event_schedule(
    event_id: int,
    scheduled_at: int,
    channel_id: str | None = None,
    message_id: str | None = None,
) -> None:
    execute(
        "UPDATE events SET scheduled_at = ?, schedule_channel_id = ?, "
        "schedule_message_id = ?, reminder_sent = 0 WHERE id = ?",
        (scheduled_at, channel_id, message_id, event_id),
    )


def clear_event_schedule(event_id: int) -> None:
    execute(
        "UPDATE events SET scheduled_at = NULL, schedule_channel_id = NULL, "
        "schedule_message_id = NULL, reminder_sent = 0 WHERE id = ?",
        (event_id,),
    )


def mark_event_reminded(event_id: int) -> None:
    execute("UPDATE events SET reminder_sent = 1 WHERE id = ?", (event_id,))


def add_event_qualifier(event_id: int, discord_id: str, username: str, team_members: str | None = None) -> dict:
    """Add a player to the event's qualified list (idempotent)."""
    existing = query_one(
        "SELECT * FROM event_qualifiers WHERE event_id = ? AND discord_id = ?",
        (event_id, discord_id),
    )
    if existing:
        return existing
    execute(
        "INSERT INTO event_qualifiers (event_id, discord_id, username, team_members) "
        "VALUES (?, ?, ?, ?)",
        (event_id, discord_id, username, team_members),
    )
    return query_one(
        "SELECT * FROM event_qualifiers WHERE event_id = ? AND discord_id = ?",
        (event_id, discord_id),
    )


def remove_event_qualifier(event_id: int, discord_id: str) -> bool:
    with get_db() as conn:
        cur = conn.execute(
            "DELETE FROM event_qualifiers WHERE event_id = ? AND discord_id = ?",
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
            "SELECT * FROM registrations WHERE event_id = ? AND discord_id = ?",
            (target_event, q["discord_id"]),
        )
        if existing:
            skipped += 1
            continue
        usernames = q["username"]
        execute(
            "INSERT INTO registrations "
            "(event_id, discord_id, username, team_members, status) "
            "VALUES (?, ?, ?, ?, 'confirmed')",
            (target_event, q["discord_id"], usernames, q.get("team_members")),
        )
        execute(
            "INSERT INTO event_qualifiers (event_id, discord_id, username, team_members) "
            "VALUES (?, ?, ?, ?)",
            (target_event, q["discord_id"], usernames, q.get("team_members")),
        )
        moved += 1
        names.append(q["username"])
        execute(
            "DELETE FROM event_qualifiers WHERE event_id = ? AND discord_id = ?",
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
    row = query_one("SELECT value FROM kv_store WHERE key = ?", (key,))
    return row["value"] if row else default


def get_coins(discord_id: str) -> int:
    row = query_one("SELECT coins FROM invite_coins WHERE discord_id = ?", (discord_id,))
    return row["coins"] if row else 0


def add_coins(discord_id: str, amount: int) -> int:
    """Add coins to a balance (creating the row if needed). Returns new balance."""
    with get_db() as conn:
        row = conn.execute(
            "SELECT coins FROM invite_coins WHERE discord_id = ?", (discord_id,)
        ).fetchone()
        if row is None:
            conn.execute(
                "INSERT INTO invite_coins (discord_id, coins) VALUES (?, ?)",
                (discord_id, amount),
            )
            return amount
        conn.execute(
            "UPDATE invite_coins SET coins = coins + ?, updated_at = CURRENT_TIMESTAMP "
            "WHERE discord_id = ?",
            (amount, discord_id),
        )
        return row["coins"] + amount


def grant_invite_coin(discord_id: str) -> int:
    """Legacy immediate-grant path (kept for the approval flow via add_coins)."""
    return add_coins(discord_id, 1)


def grant_invite_coin(discord_id: str) -> int:
    """Grant 1 coin for a successful invite. Returns the new balance."""
    with get_db() as conn:
        row = conn.execute(
            "SELECT coins FROM invite_coins WHERE discord_id = ?", (discord_id,)
        ).fetchone()
        if row is None:
            conn.execute(
                "INSERT INTO invite_coins (discord_id, coins, total_invites) VALUES (?, 1, 1)",
                (discord_id,),
            )
            return 1
        conn.execute(
            "UPDATE invite_coins SET coins = coins + 1, total_invites = total_invites + 1, "
            "updated_at = CURRENT_TIMESTAMP WHERE discord_id = ?",
            (discord_id,),
        )
        return row["coins"] + 1


def spend_coins(discord_id: str, amount: int) -> bool:
    """Atomically deduct coins. Returns False when balance is too low."""
    with get_db() as conn:
        row = conn.execute(
            "SELECT coins FROM invite_coins WHERE discord_id = ?", (discord_id,)
        ).fetchone()
        if row is None or row["coins"] < amount:
            return False
        conn.execute(
            "UPDATE invite_coins SET coins = coins - ? WHERE discord_id = ?",
            (amount, discord_id),
        )
        return True


def add_coin_purchase(
    discord_id: str, product: str, role_id: str, expires_at: int, guild_id: str
) -> None:
    execute(
        "INSERT INTO coin_purchases (discord_id, product, role_id, expires_at, guild_id) "
        "VALUES (?, ?, ?, ?, ?)",
        (discord_id, product, role_id, expires_at, guild_id),
    )


def get_expired_purchases(now_ts: int) -> list[dict]:
    return query(
        "SELECT * FROM coin_purchases WHERE expires_at IS NOT NULL AND expires_at <= ?",
        (now_ts,),
    )


def delete_purchase(purchase_id: int) -> None:
    execute("DELETE FROM coin_purchases WHERE id = ?", (purchase_id,))


def get_coin_leaderboard(limit: int = 10) -> list[dict]:
    return query(
        "SELECT * FROM invite_coins ORDER BY coins DESC, total_invites DESC LIMIT ?",
        (limit,),
    )


# ------------------------------------------------------------------ invite rewards


def create_invite_reward(
    guild_id: str, inviter_id: str, invited_user_id: str, joined_at: int
) -> int:
    return execute(
        "INSERT INTO invite_rewards (guild_id, inviter_id, invited_user_id, created_at) "
        "VALUES (?, ?, ?, ?)",
        (guild_id, inviter_id, invited_user_id, joined_at),
    )


def get_reward_by_invited(invited_user_id: str) -> dict | None:
    return query_one(
        "SELECT * FROM invite_rewards WHERE invited_user_id = ? ORDER BY id DESC LIMIT 1",
        (invited_user_id,),
    )


def get_reward(reward_id: int) -> dict | None:
    return query_one("SELECT * FROM invite_rewards WHERE id = ?", (reward_id,))


def get_pending_rewards() -> list[dict]:
    return query("SELECT * FROM invite_rewards WHERE status = 'pending'")


def mark_reward_left(reward_id: int, left_at: int) -> None:
    execute("UPDATE invite_rewards SET left_at = ? WHERE id = ?", (left_at, reward_id))


def set_reward_status(reward_id: int, status: str, reason: str = "", approved_at: int = 0) -> None:
    execute(
        "UPDATE invite_rewards SET status = ?, reason = ?, approved_at = "
        "(CASE WHEN ? = 'approved' THEN ? ELSE approved_at END) "
        "WHERE id = ?",
        (status, reason, status, approved_at, reward_id),
    )


def approve_invite_reward(reward_id: int, coins: int, quality: int, approved_at: int) -> None:
    execute(
        "UPDATE invite_rewards SET status = 'approved', coins_granted = ?, "
        "quality_score = ?, approved_at = ?, reason = '' WHERE id = ?",
        (coins, quality, approved_at, reward_id),
    )


def flag_invite_reward(reward_id: int, reason: str) -> None:
    execute(
        "UPDATE invite_rewards SET flagged = 1, reason = ? WHERE id = ?",
        (reason, reward_id),
    )


def update_reward_quality(reward_id: int, quality: int, reason: str = "") -> None:
    execute(
        "UPDATE invite_rewards SET quality_score = ?, reason = ? WHERE id = ?",
        (quality, reason, reward_id),
    )


def count_approved_since(inviter_id: str, since_ts: int) -> int:
    row = query_one(
        "SELECT COUNT(*) AS c FROM invite_rewards "
        "WHERE inviter_id = ? AND status = 'approved' AND approved_at >= ?",
        (inviter_id, since_ts),
    )
    return row["c"] if row else 0


def count_invites_created_since(inviter_id: str, since_ts: int) -> int:
    row = query_one(
        "SELECT COUNT(*) AS c FROM invite_rewards WHERE inviter_id = ? AND created_at >= ?",
        (inviter_id, since_ts),
    )
    return row["c"] if row else 0


def get_approved_rewards_without_loyalty() -> list[dict]:
    return query(
        "SELECT * FROM invite_rewards WHERE status = 'approved' AND loyalty_granted = 0"
    )


def mark_loyalty_granted(reward_id: int) -> None:
    execute("UPDATE invite_rewards SET loyalty_granted = 1 WHERE id = ?", (reward_id,))


def mark_participation_granted(reward_id: int) -> None:
    execute("UPDATE invite_rewards SET participation_granted = 1 WHERE id = ?", (reward_id,))


def get_rewards_for_review() -> list[dict]:
    return query(
        "SELECT * FROM invite_rewards "
        "WHERE status = 'pending' AND (flagged = 1 OR reason IN ('review', 'rate-limit')) "
        "ORDER BY created_at ASC"
    )


def has_event_participation(discord_id: str) -> bool:
    row = query_one(
        "SELECT 1 FROM registrations WHERE discord_id = ? AND status = 'confirmed' "
        "UNION ALL "
        "SELECT 1 FROM game_players WHERE player_id = ? LIMIT 1",
        (discord_id, discord_id),
    )
    return row is not None


def increment_user_message(discord_id: str) -> None:
    with get_db() as conn:
        conn.execute(
            "INSERT INTO user_messages (discord_id, count) VALUES (?, 1) "
            "ON CONFLICT(discord_id) DO UPDATE SET count = count + 1",
            (discord_id,),
        )


def get_user_message_count(discord_id: str) -> int:
    row = query_one("SELECT count FROM user_messages WHERE discord_id = ?", (discord_id,))
    return row["count"] if row else 0


def set_kv(key: str, value: str) -> None:
    execute(
        "INSERT INTO kv_store (key, value) VALUES (?, ?) "
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
            conn.execute(
                "INSERT OR REPLACE INTO season_stats "
                "(season, discord_id, pr, kills, wins, games, avg_placement, position) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
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
        "SELECT * FROM season_stats WHERE season = ? AND discord_id = ?",
        (season, discord_id),
    ) or None


def season_reset() -> int:
    """Snap the closing season's stats, transfer PR to the lifetime total,
    zero season PR, then move to the next season."""
    from datetime import datetime

    closing_season = get_season()
    snapshot_season(closing_season)

    players = query("SELECT discord_id FROM players")
    for p in players:
        execute(
            "UPDATE players SET total_pr = total_pr + pr, pr = 0 "
            "WHERE discord_id = ?",
            (p["discord_id"],),
        )
    season = bump_season()
    set_kv("last_reset_at", datetime.utcnow().isoformat())
    return len(players)


def start_season() -> int:
    """Begin a new season without touching stats (announcement not dispatched yet)."""
    return bump_season()
