# -*- coding: utf-8 -*-
"""One-time migration: SQLite data/scrim.db -> Supabase Postgres.

Usage:
  python migrate_from_sqlite.py

Requirements:
  - SUPABASE_DB_URL (or supabase_db_url in .env) set to the Postgres URL.
  - data/scrim.db is the REAL database (copy it out of the container first:
    docker cp <container>:/app/data/scrim.db data/scrim.db).

Behaviour:
  - Creates the vtx_* schema (migrations/supabase_schema.sql).
  - Renumbers every table's ids to 1..N so the bot's absolute-id assumptions
    hold and all foreign keys line up.
  - Drops junk events (names matching JUNK_EVENT_NAMES, default any name
    containing "smoke") plus every row that references a dropped event, so the
    remaining events renumber 1..N and the most recent event becomes the last
    id (e.g. 9 instead of 13).
  - Resets Postgres sequences so new rows keep incrementing correctly.
"""
import os
import sqlite3
import sys
from pathlib import Path

import psycopg2

sys.path.insert(0, str(Path(__file__).parent))

from config import settings  # noqa: E402

ROOT = Path(__file__).parent
DB_PATH = Path(settings.database_path)
SCHEMA_FILE = ROOT / "migrations" / "supabase_schema.sql"

# Every table, in FK-safe dependency order (parents before children).
TABLES: list[str] = [
    "players",
    "divisions",
    "events",
    "registrations",
    "pending_registrations",
    "lobbies",
    "sessions",
    "games",
    "game_players",
    "lobby_players",
    "kills",
    "game_team_members",
    "command_queue",
    "bot_logs",
    "bans",
    "kv_store",
    "season_stats",
    "event_qualifiers",
    "event_interests",
    "event_wins",
    "invite_coins",
    "coin_purchases",
    "invite_rewards",
    "user_messages",
    "division_members",
    "bracket_matches",
    "duel_asks",
]

# column -> table the column references (for id remapping)
FK_MAPS: dict[str, dict[str, str]] = {
    "events": {"required_division_id": "divisions"},
    "registrations": {"event_id": "events"},
    "pending_registrations": {"event_id": "events"},
    "sessions": {"event_id": "events", "lobby_id": "lobbies"},
    "lobbies": {"event_id": "events"},
    "games": {"event_id": "events", "session_id": "sessions"},
    "game_players": {"game_id": "games", "player_id": "players"},
    "lobby_players": {"lobby_id": "lobbies", "player_id": "players"},
    "kills": {"game_id": "games", "killer_id": "players", "victim_id": "players"},
    "game_team_members": {"game_id": "games"},
    "bot_logs": {"event_id": "events"},
    "event_qualifiers": {"event_id": "events"},
    "event_interests": {"event_id": "events"},
    "event_wins": {"event_id": "events", "player_id": "players"},
    "division_members": {"division_id": "divisions", "qualified_from_event_id": "events"},
    "bracket_matches": {"event_id": "events", "player1_id": "players", "player2_id": "players", "winner_id": "players"},
}

EVENT_FK_TABLES = [t for t, fks in FK_MAPS.items() if "event_id" in fks]

# Table whose PK is TEXT (no BIGSERIAL sequence).
IDLESS_TABLES = {"kv_store", "invite_coins", "user_messages"}

# Soft FKs: null them out instead of dropping the row when the target is gone.
SOFT_FK_COLUMNS = {"required_division_id", "qualified_from_event_id", "lobby_id", "session_id"}


def junk_events(rows: list[dict]) -> set[int]:
    """Ids of events that should be dropped before renumbering."""
    names = [n.strip().lower() for n in os.environ.get("JUNK_EVENT_NAMES", "smoke").split(",") if n.strip()]
    ids = {int(x) for x in os.environ.get("JUNK_EVENT_IDS", "").split(",") if x.strip()}
    dropped = set()
    for r in rows:
        name = (r.get("name") or "").lower()
        if r["id"] in ids or any(n in name for n in names):
            dropped.add(r["id"])
    return dropped


def read_table(conn: sqlite3.Connection, name: str) -> list[dict]:
    try:
        cur = conn.execute(f'SELECT * FROM "{name}"')
    except sqlite3.OperationalError:
        return []
    cols = [d[0] for d in cur.description]
    return [dict(zip(cols, row)) for row in cur.fetchall()]


def make_map(rows: list[dict]) -> dict[int, int]:
    return {r["id"]: i + 1 for i, r in enumerate(rows)}


def remap(value, id_map: dict[int, int]):
    if value is None:
        return None
    v = id_map.get(int(value))
    return v


def main() -> None:
    if not DB_PATH.exists() or DB_PATH.stat().st_size == 0:
        sys.exit(f"FATAL: {DB_PATH} is missing or empty. Copy the real DB out of "
                 "the container first:\n  docker cp <container>:/app/data/scrim.db data/scrim.db")
    if not settings.supabase_db_url:
        sys.exit("FATAL: SUPABASE_DB_URL is not set in .env")

    src = sqlite3.connect(str(DB_PATH))
    print(f"Reading SQLite database: {DB_PATH}")
    data = {t: read_table(src, t) for t in TABLES}
    src.close()

    # --- 1. Drop junk events + every row that references them ---
    dropped_events = junk_events(data["events"])
    if dropped_events:
        print(f"Dropping junk events (ids {sorted(dropped_events)}):")
        for r in data["events"]:
            if r["id"] in dropped_events:
                print(f"  - #{r['id']} {r.get('name')!r} ({r.get('status')})")
    events_rows = [r for r in data["events"] if r["id"] not in dropped_events]
    events_rows.sort(key=lambda r: r.get("created_at") or "")
    events_map = make_map(events_rows)
    print(f"Events after cleanup: {len(events_rows)} (most recent becomes #{len(events_rows)})")
    if os.environ.get("EXPECT_EVENT_COUNT"):
        expected = int(os.environ["EXPECT_EVENT_COUNT"])
        if len(events_rows) != expected:
            sys.exit(f"FATAL: expected {expected} events after cleanup, got {len(events_rows)}")

    for t in EVENT_FK_TABLES:
        data[t] = [
            r for r in data[t]
            if r.get("event_id") is None or int(r["event_id"]) in events_map
        ]

    # --- 2. Renumber every table ---
    maps: dict[str, dict[int, int]] = {"events": events_map}
    for t in TABLES:
        if t in IDLESS_TABLES:
            maps[t] = {}
            continue
        if t == "events":
            continue
        rows = data[t]
        rows.sort(key=lambda r: (r.get("created_at") or r.get("id") or 0, r.get("id") or 0))
        maps[t] = make_map(rows)

    # --- 3. Create schema + insert everything into Postgres ---
    dst = psycopg2.connect(settings.supabase_db_url)
    dst.autocommit = False
    try:
        cur = dst.cursor()
        cur.execute(SCHEMA_FILE.read_text(encoding="utf-8"))
        dst.commit()
        print("Schema created.")

        # Wipe existing vtx_ tables so reruns are safe (idempotent migration).
        wipe = ", ".join(f'"vtx_{t}"' for t in TABLES)
        cur.execute(f"TRUNCATE {wipe} CASCADE")
        dst.commit()
        print("Existing vtx_ tables cleared.")

        for t in TABLES:
            if t == "rank_tiers":
                continue  # seeded by the schema
            rows = data[t]
            if not rows:
                print(f"{t}: 0 rows")
                continue
            cols = [c for c in rows[0].keys()]
            inserts = []
            for r in rows:
                new_row = {}
                skip = False
                for c in cols:
                    if c == "id":
                        new_row[c] = maps[t].get(int(r["id"]))
                        continue
                    ref = FK_MAPS.get(t, {}).get(c)
                    if ref is not None:
                        v = remap(r[c], maps[ref])
                        if r[c] is not None and v is None:
                            if c in SOFT_FK_COLUMNS:
                                new_row[c] = None
                                continue
                            skip = True
                            break
                        new_row[c] = v
                    else:
                        new_row[c] = r[c]
                if not skip:
                    inserts.append(new_row)
            if not inserts:
                print(f"{t}: 0 rows")
                continue
            columns = list(inserts[0].keys())
            ph = ",".join(["%s"] * len(columns))
            sql = f'INSERT INTO "vtx_{t}" ({",".join(columns)}) VALUES ({ph})'
            cur.executemany(sql, [tuple(rw[c] for c in columns) for rw in inserts])
            dst.commit()
            print(f"{t}: {len(inserts)} rows")

        # --- 4. Reset sequences ---
        for t in TABLES:
            if t in ("rank_tiers",) or t in IDLESS_TABLES:
                continue
            cur.execute(f"SELECT setval('vtx_{t}_id_seq', COALESCE((SELECT MAX(id) FROM vtx_{t}), 1))")
        dst.commit()
        print("Sequences reset.")
    except Exception:
        dst.rollback()
        raise
    finally:
        dst.close()

    print("\nMigration complete.")


if __name__ == "__main__":
    main()