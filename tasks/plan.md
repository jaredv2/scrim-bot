# Implementation Plan: Standalone Scrim/Cup Bot + Dashboard

## Overview
A self-contained bot + dashboard package for a separate BuildNow org running scrims and cups. Uses SQLite (no Supabase), Discord bot for registration and in-game management, minimal HTML dashboard for staff.

## Architecture Decisions
- **SQLite** — single file database, zero setup, perfect for small orgs
- **Separate project folder** — completely independent from the main bot
- **Minimal dependencies** — discord.py + FastAPI + sqlite3
- **Dashboard auth** — shared password, same pattern as main bot
- **Registration** — mention-based in a channel, bot validates and reacts with ✓

## Task List

### Phase 1: Foundation
- [x] Task 1: Create project folder structure + requirements.txt + .env.example
- [x] Task 2: SQLite database schema (players, events, registrations, games, game_players, lobbies, lobby_players)
- [x] Task 3: Bot config + database helper module
- [x] Task 4: Bot main.py + basic cog loading

### Checkpoint: Foundation
- [ ] Bot starts, connects to Discord, loads cogs

### Phase 2: Registration
- [ ] Task 5: Channel open/close command (lock/unlock a channel for registration)
- [ ] Task 6: Mention-based registration (parse @mentions, validate, react ✓)
- [ ] Task 7: Registration list command (show registered players/teams)

### Checkpoint: Registration
- [ ] Players can register by messaging @mentions, bot reacts, staff can open/close

### Phase 3: Events + Lobbies
- [ ] Task 8: Event creation command + database record
- [ ] Task 9: Event status management (open/close registration, start, end)
- [ ] Task 10: Lobby creation (on-demand, assign players to lobbies)
- [ ] Task 11: Room code posting (bot posts room code to channel)

### Checkpoint: Events + Lobbies
- [ ] Staff can create events, manage registration, create lobbies, post room codes

### Phase 4: Live Management
- [ ] Task 12: Kill feed tracking (bot reads messages, parses kills)
- [ ] Task 13: Point assignment (from bot or dashboard)
- [ ] Task 14: DQ command

### Checkpoint: Live Management
- [ ] Kill feed works, points can be assigned, DQ works

### Phase 5: Dashboard
- [ ] Task 15: FastAPI app + SQLite connection + auth
- [ ] Task 16: Dashboard HTML/CSS (minimal, blue accents, white bg, centered)
- [ ] Task 17: Event list + create event page
- [ ] Task 18: Event detail page (player list, lobbies, controls)
- [ ] Task 19: Kill feed log page
- [ ] Task 20: Point assignment + DQ from dashboard

### Checkpoint: Dashboard
- [ ] Staff can manage everything from the dashboard

### Phase 6: Polish
- [ ] Task 21: Docker compose + Dockerfile
- [ ] Task 22: End-to-end testing

## Risks and Mitigations
| Risk | Impact | Mitigation |
|------|--------|------------|
| Kill feed parsing is fragile | Medium | Simple regex, easy to extend |
| SQLite concurrency | Low | WAL mode, single-writer pattern |
| Bot + dashboard same SQLite | Low | WAL mode handles this fine |

## Database Schema

```sql
-- Players
CREATE TABLE players (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    discord_id TEXT UNIQUE NOT NULL,
    username TEXT NOT NULL,
    game_id TEXT,
    game_username TEXT,
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);

-- Events
CREATE TABLE events (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    name TEXT NOT NULL,
    status TEXT DEFAULT 'setup',  -- setup, registration, in_progress, completed
    channel_id TEXT,              -- Discord channel for registration
    room_code TEXT,
    max_players INTEGER DEFAULT 100,
    team_size INTEGER DEFAULT 1, -- 1=solo, 2=duo, 3=trio
    total_games INTEGER DEFAULT 1,
    current_game INTEGER DEFAULT 0,
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);

-- Registrations (per event)
CREATE TABLE registrations (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    event_id INTEGER REFERENCES events(id),
    discord_id TEXT NOT NULL,
    username TEXT NOT NULL,
    team_members TEXT,  -- JSON list of mentioned users for teams
    message_id TEXT,    -- Discord message ID for reference
    status TEXT DEFAULT 'pending',  -- pending, confirmed, removed
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    UNIQUE(event_id, discord_id)
);

-- Games (per event)
CREATE TABLE games (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    event_id INTEGER REFERENCES events(id),
    game_number INTEGER NOT NULL,
    room_code TEXT,
    status TEXT DEFAULT 'waiting',  -- waiting, in_progress, completed
    started_at TIMESTAMP,
    ended_at TIMESTAMP,
    UNIQUE(event_id, game_number)
);

-- Game players
CREATE TABLE game_players (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    game_id INTEGER REFERENCES games(id),
    player_id INTEGER REFERENCES players(id),
    kills INTEGER DEFAULT 0,
    placement INTEGER,
    points INTEGER DEFAULT 0,
    is_disqualified INTEGER DEFAULT 0,
    UNIQUE(game_id, player_id)
);

-- Lobbies (on-demand)
CREATE TABLE lobbies (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    event_id INTEGER REFERENCES events(id),
    name TEXT NOT NULL,
    room_code TEXT,
    status TEXT DEFAULT 'open',  -- open, full, closed
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);

-- Lobby players
CREATE TABLE lobby_players (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    lobby_id INTEGER REFERENCES lobbies(id),
    player_id INTEGER REFERENCES players(id),
    joined_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    UNIQUE(lobby_id, player_id)
);

-- Kill feed
CREATE TABLE kills (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    game_id INTEGER REFERENCES games(id),
    killer_id INTEGER REFERENCES players(id),
    victim_id INTEGER REFERENCES players(id),
    weapon TEXT,
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);
```
