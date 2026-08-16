# BuildNow Scrim Bot — Full Spec & Guide

---

## Table of Contents
1. [Overview](#overview)
2. [Architecture](#architecture)
3. [Command Reference](#command-reference)
4. [Staff Guide](#staff-guide)
5. [Player Guide](#player-guide)
6. [Database Schema](#database-schema)
7. [Configuration](#configuration)

---

## Overview

BuildNow Scrim Bot is a Discord bot for managing BuildNow Studio (Fortnite) scrim sessions. It handles event creation, player registration, game lifecycle, scoring, lobby management, leaderboards, and a PR (Power Rating) system.

**Key Capabilities:**
- Solo, Duo, Trio event formats
- Multi-match events (configurable match count)
- Automated registration with button UI
- Team registration with skin/outfit collection
- Temp private channels and team roles for events
- Room code DMs to players
- Kill feed parsing from text messages
- Per-game and cumulative leaderboards
- PR ranking system
- Web dashboard (separate Flask app)

---

## Architecture

```
scrim-bot/
├── bot.py              # Entry point, cog loading, slash command sync
├── config.py           # Environment settings (pydantic-settings)
├── database.py         # SQLite schema, queries, migrations
├── embeds.py           # Discord embed helpers
├── templates_fmt.py    # Message templates
├── cogs/
│   ├── events.py       # Event & scrim lifecycle commands
│   ├── general.py      # Utility & fun commands
│   ├── lobbies.py      # Lobby management commands
│   ├── admin.py        # Admin scoring/DQ commands
│   ├── registration.py # Registration open/close, change-ign, stats
│   └── queue_processor.py  # Command queue processing
└── views/
    └── registration.py # Button UI, modals, team reply handler
```

**Tech Stack:**
- Python 3.11+
- discord.py 2.x (slash commands, views, modals)
- SQLite (WAL mode, thread-local connections)
- pydantic-settings (config from .env)

---

## Command Reference

### Event Commands (Staff Only)

| Command | Description | Ephemeral |
|---------|-------------|-----------|
| `/create-event` | Create a new event/cup | Yes |
| `/start-event` | Start event: creates temp channel + team roles | Yes |
| `/start-game` | Dispatch room code via DM + channel message | Yes |
| `/end-game` | End a game, show game leaderboard | Yes |
| `/end-event` | End event: final leaderboard, cleanup channel/roles | Yes |
| `/dm-players` | DM all registered players with room code | Yes |

**`/create-event`**
- `name` — Event name
- `channel` — Announcement channel
- `signup_channel` — Registration channel
- `team_size` — Solo (1) / Duo (2) / Trio (3)
- `total_games` — Number of matches
- `max_players` — Max players (default: 100)
- `region` — EU / NA / ASIA / etc.
- `event_format` — ZoneWars / BoxFights / etc.
- `start_time` — Displayed start time
- `point_kill` — Points per elimination (default: 1)
- `point_win` — Points for victory (default: 5)

Posts an announcement in the specified channel. Locks signup channel (no one can type).

**`/start-event`**
- `event_id` — Event ID
- `room_code` — Fortnite room code

Creates:
- A temporary private text channel (visible only to registered players)
- Colored team roles (for duo/trio events)
- Assigns team roles to players
- Stores channel and role IDs for cleanup

**`/start-game`**
- `event_id` — Event ID
- `game_number` — Game number (1, 2, 3...)

Actions:
- Creates/updates game record
- Registers all event players in game_players
- DMs every registered player with room code
- Posts game start message in event channel

**`/end-game`**
- `event_id` — Event ID
- `game_number` — Game number
- `first` — 1st place (optional)
- `second` — 2nd place (optional)
- `third` — 3rd place (optional)

Actions:
- Updates placements and win points
- Shows game leaderboard
- Updates player PR
- Posts result in event channel

**`/end-event`**
- `event_id` — Event ID

Actions:
- Shows final leaderboard (solo or team)
- Mentions winner and runner-up
- Deletes temp channel
- Deletes team roles
- Updates all player PRs

**`/dm-players`**
- `event_id` — Event ID
- `room_code` — Room code to send
- `game_number` — Game number
- `start_time` — Start time text

---

### Scrim Commands (Staff Only)

| Command | Description | Ephemeral |
|---------|-------------|-----------|
| `/create-scrim` | Create scrim with auto-generated ID | Yes |
| `/start-scrim` | Start scrim, dispatch message + DMs | Yes |
| `/end-scrim` | End scrim, show final leaderboard | Yes |

**`/create-scrim`**
- `channel` — Announcement channel
- `signup_channel` — Registration channel
- `team_size` — Solo / Duo / Trio
- `match_count` — Number of matches (default: 3)
- `base_pr_kill` — PR gained per kill (default: 5)
- `base_pr_win` — PR gained for win (default: 25)
- `region` — Region
- `event_format` — Format

Auto-generates a `SCRIM-XXXX` ID. Posts scrim details embed.

**`/start-scrim`**
- `event_id` — Event ID
- `room_code` — Room code

Posts plain text message in channel with format, region, and code.
DMs all registered players with room code.

**`/end-scrim`**
- `event_id` — Event ID

Shows leaderboard and GG message in channel.

---

### Lobby Commands (Staff Only)

| Command | Description |
|---------|-------------|
| `/create-lobby` | Create a lobby for an event |
| `/join-lobby` | Add a player to a lobby |
| `/remove-from-lobby` | Remove a player from a lobby |
| `/lobby-info` | Show lobby details and players |
| `/lobbies` | List all lobbies for an event |
| `/set-lobby-code` | Set room code for a lobby |
| `/close-lobby` | Close a lobby |

---

### Admin Commands (Staff Only)

| Command | Description |
|---------|-------------|
| `/assign-points` | Assign points to a player in a game |
| `/dq-player` | Disqualify a player from a game |
| `/add-kills` | Manually set kills for a player |
| `/game-stats` | Show stats for a specific game |

**Kill Feed Auto-Tracking:**
The bot listens for messages matching `X killed Y with Z` or `X eliminated Y with Z` in event channels and automatically records kills.

---

### Registration Commands

| Command | Description | Visibility |
|---------|-------------|------------|
| `/open-registration` | Open signups for an event | Staff only |
| `/close-registration` | Close signups for an event | Staff only |
| `/register-player` | Register your IGN and Game ID | Everyone |
| `/change-ign` | Change your in-game name | Everyone |
| `/stats` | View your stats (IGN, Game ID, PR, wins, kills) | Everyone |

---

### Info & Fun Commands

| Command | Description | Visibility |
|---------|-------------|------------|
| `/ping` | Check bot latency | Everyone |
| `/help` | Show all available commands | Everyone |
| `/how-to-play` | How to play guide | Everyone |
| `/8ball` | Ask the magic 8-ball | Everyone |
| `/flip` | Flip a coin | Everyone |
| `/roll` | Roll dice (configurable sides) | Everyone |
| `/rank` | Get a random competitive rank | Everyone |

---

## Staff Guide

### Event Lifecycle

```
1. /create-event
   → Event created in "setup" status
   → Announcement posted
   → Signup channel locked

2. /open-registration
   → Unlocks signup channel
   → Posts register button
   → Players click Register → fill modal → team replies

3. /close-registration
   → Disables register button
   → Locks signup channel
   → Shows total registered

4. /start-event (room_code)
   → Creates temp private channel
   → Creates team roles (duo/trio)
   → Assigns roles to players

5. /start-game (game_number)
   → Creates game record
   → DMs all players with room code
   → Posts start message in temp channel

6. [Game happens in Fortnite]

7. /end-game (game_number, first?, second?, third?)
   → Records placements and win points
   → Shows game leaderboard
   → Updates player PR

8. Repeat steps 5-7 for each game

9. /end-event
   → Final leaderboard
   → Winner/runner-up mentions
   → Deletes temp channel
   → Deletes team roles
```

### Scrim Lifecycle (Shorter Flow)

```
1. /create-scrim
   → Auto-ID generated
   → Embed posted

2. /open-registration

3. /close-registration

4. /start-scrim (room_code)
   → Plain text message in channel
   → DMs players

5. [Game happens]

6. /end-scrim
   → Leaderboard posted
   → GG message
```

### Registration Flow (Team Events)

1. Player clicks **Register** button
2. Modal appears asking for:
   - **IGN** (in-game name)
   - **Game ID**
   - **Team Skin** (for duo/trio only)
3. Bot posts message in signup channel:
   ```
   📝 @Player (IGN) — Event Name (Duo)
   Skin: Skull Trooper
   Reply with 2 mentions: @teammate1 @teammate2
   ```
4. Teammates reply with their mentions
5. Bot validates:
   - Correct number of mentions
   - All players have IGNs set
   - No duplicate registrations
6. Registration confirmed ✅

### Team Event Cleanup

When `/end-event` runs:
- All colored team roles are deleted
- Temp event channel is deleted
- Player PRs are updated

### PR (Power Rating) Formula

```
PR = 100 + (total_wins × 50) + (total_kills × 5)
```

PR is recalculated after every game end and event end.

---

## Player Guide

### Getting Started

1. **Register your profile:**
   ```
   /register-player ign:YourName game_id:12345678
   ```

2. **Join an event:**
   - Wait for staff to open registration
   - Click the **Register** button
   - Fill in your IGN, Game ID, and team skin (if team event)
   - For team events, reply to the bot's message with teammate mentions

3. **Wait for the scrim to start:**
   - Staff will start the event
   - You'll receive a DM with the room code

4. **Play:**
   - Join Fortnite with the room code
   - Play the matches

5. **Check your stats:**
   ```
   /stats
   ```

### Player Commands

| Command | What it does |
|---------|-------------|
| `/register-player` | Set your IGN and Game ID |
| `/change-ign` | Update your in-game name |
| `/stats` | See your wins, kills, PR, and games played |
| `/ping` | Check if the bot is online |
| `/help` | List all commands |
| `/how-to-play` | Learn the basics |

### Team Registration

For Duo/Trio events:
1. Click **Register** button
2. Fill in your IGN, Game ID, and team skin
3. Bot posts a message asking you to mention teammates
4. Reply to that message with `@teammate1 @teammate2` (exact count needed)
5. All teammates must have `/register-player` done first
6. Once confirmed, you're registered

### Understanding Your PR

- Base PR: 100
- Each win: +50 PR
- Each kill: +5 PR
- PR updates after each game and event

---

## Database Schema

### players
| Column | Type | Description |
|--------|------|-------------|
| id | INTEGER | Primary key |
| discord_id | TEXT | Discord user ID (unique) |
| username | TEXT | Discord display name |
| game_id | TEXT | Fortnite game ID |
| game_username | TEXT | In-game name (IGN) |
| pr | INTEGER | Power Rating (default: 0) |
| created_at | TIMESTAMP | Account creation time |

### events
| Column | Type | Description |
|--------|------|-------------|
| id | INTEGER | Primary key |
| name | TEXT | Event name |
| status | TEXT | setup / registration / in_progress / completed |
| channel_id | TEXT | Announcement channel |
| signup_channel_id | TEXT | Registration channel |
| dispatch_channel_id | TEXT | Temp event channel |
| room_code | TEXT | Fortnite room code |
| region | TEXT | EU / NA / ASIA |
| event_format | TEXT | ZoneWars / BoxFights |
| team_size | INTEGER | 1=solo, 2=duo, 3=trio |
| total_games | INTEGER | Number of matches |
| current_game | INTEGER | Current game number |
| point_kill | INTEGER | Points per kill |
| point_win | INTEGER | Points for win |
| team_roles | TEXT | Comma-separated role IDs |

### registrations
| Column | Type | Description |
|--------|------|-------------|
| id | INTEGER | Primary key |
| event_id | INTEGER | Event reference |
| discord_id | TEXT | Player Discord ID |
| username | TEXT | Player username |
| team_members | TEXT | Comma-separated teammate Discord IDs |
| skin | TEXT | Team skin/outfit |
| status | TEXT | pending / confirmed |

### games
| Column | Type | Description |
|--------|------|-------------|
| id | INTEGER | Primary key |
| event_id | INTEGER | Event reference |
| game_number | INTEGER | Game number |
| room_code | TEXT | Room code |
| status | TEXT | waiting / in_progress / completed |

### game_players
| Column | Type | Description |
|--------|------|-------------|
| id | INTEGER | Primary key |
| game_id | INTEGER | Game reference |
| player_id | INTEGER | Player reference |
| kills | INTEGER | Kill count |
| placement | INTEGER | Final placement |
| points | INTEGER | Points earned |
| is_disqualified | INTEGER | DQ flag |

### lobbies
| Column | Type | Description |
|--------|------|-------------|
| id | INTEGER | Primary key |
| event_id | INTEGER | Event reference |
| name | TEXT | Lobby name |
| room_code | TEXT | Room code |
| status | TEXT | open / closed |

### lobby_players
| Column | Type | Description |
|--------|------|-------------|
| id | INTEGER | Primary key |
| lobby_id | INTEGER | Lobby reference |
| player_id | INTEGER | Player reference |

### kills
| Column | Type | Description |
|--------|------|-------------|
| id | INTEGER | Primary key |
| game_id | INTEGER | Game reference |
| killer_id | INTEGER | Killer player ID |
| victim_id | INTEGER | Victim player ID |
| weapon | TEXT | Weapon used |

---

## Configuration

Environment variables (`.env` file):

| Variable | Description | Default |
|----------|-------------|---------|
| `DISCORD_BOT_TOKEN` | Bot token | — |
| `DISCORD_GUILD_ID` | Server ID for command sync | — |
| `DISCORD_ADMIN_ROLE_ID` | Admin role ID | — |
| `DATABASE_PATH` | SQLite database path | `data/scrim.db` |

### Admin Permissions

Staff can use event/scrim/admin commands if they have:
- Discord `administrator` permission, OR
- The configured admin role

### Visibility Rules

| Response Type | Visibility |
|---------------|------------|
| Create/start/end event/scrim | Ephemeral (staff only) |
| Admin scoring/DQ | Ephemeral (staff only) |
| Registration success | Public |
| Player commands (stats, change-ign) | Public |
| Fun commands (8ball, flip, roll) | Public |
| Info commands (help, how-to-play) | Public |
| Error messages | Ephemeral |
