-- BuildNow Scrim Bot -- Supabase (Postgres) schema
-- Single source of truth for all tables. Idempotent: safe to run repeatedly.
-- Every table carries the vtx_ prefix. Primary keys are BIGSERIAL (autoincrement).

CREATE TABLE IF NOT EXISTS vtx_players (
    id BIGSERIAL PRIMARY KEY,
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

CREATE TABLE IF NOT EXISTS vtx_events (
    id BIGSERIAL PRIMARY KEY,
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
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    event_type TEXT DEFAULT 'cup',
    entry_mode TEXT DEFAULT 'open',
    pr_cap INTEGER,
    required_division_id INTEGER,
    scoring_mode TEXT DEFAULT 'normal',
    awards_pr INTEGER DEFAULT 1,
    coins_enabled INTEGER DEFAULT 0,
    qualifier_requirements TEXT,
    pr_multiplier REAL,
    shoot_timer TEXT DEFAULT '0',
    scheduled_at INTEGER,
    schedule_message_id TEXT,
    schedule_channel_id TEXT,
    reminder_sent INTEGER DEFAULT 0,
    team_roles TEXT
);

CREATE TABLE IF NOT EXISTS vtx_registrations (
    id BIGSERIAL PRIMARY KEY,
    event_id INTEGER REFERENCES vtx_events(id),
    discord_id TEXT NOT NULL,
    username TEXT NOT NULL,
    team_members TEXT,
    skin TEXT,
    message_id TEXT,
    status TEXT DEFAULT 'pending',
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    UNIQUE(event_id, discord_id)
);

CREATE TABLE IF NOT EXISTS vtx_pending_registrations (
    id BIGSERIAL PRIMARY KEY,
    event_id INTEGER REFERENCES vtx_events(id),
    discord_id TEXT NOT NULL,
    prompt_message_id TEXT NOT NULL,
    skin TEXT,
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE IF NOT EXISTS vtx_lobbies (
    id BIGSERIAL PRIMARY KEY,
    event_id INTEGER REFERENCES vtx_events(id),
    name TEXT NOT NULL,
    room_code TEXT,
    status TEXT DEFAULT 'open',
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    max_players INTEGER DEFAULT 100,
    lobby_number INTEGER DEFAULT 1,
    settings TEXT,
    started_at TIMESTAMP,
    ended_at TIMESTAMP
);

CREATE TABLE IF NOT EXISTS vtx_sessions (
    id BIGSERIAL PRIMARY KEY,
    event_id INTEGER REFERENCES vtx_events(id),
    session_number INTEGER NOT NULL,
    status TEXT DEFAULT 'pending',
    room_code TEXT,
    current_match INTEGER DEFAULT 0,
    started_at TIMESTAMP,
    ended_at TIMESTAMP,
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    lobby_id INTEGER REFERENCES vtx_lobbies(id)
);

CREATE TABLE IF NOT EXISTS vtx_games (
    id BIGSERIAL PRIMARY KEY,
    event_id INTEGER REFERENCES vtx_events(id),
    game_number INTEGER NOT NULL,
    room_code TEXT,
    status TEXT DEFAULT 'pending',
    season INTEGER DEFAULT 1,
    started_at TIMESTAMP,
    ended_at TIMESTAMP,
    session_id INTEGER REFERENCES vtx_sessions(id)
);

CREATE TABLE IF NOT EXISTS vtx_game_players (
    id BIGSERIAL PRIMARY KEY,
    game_id INTEGER REFERENCES vtx_games(id),
    player_id INTEGER REFERENCES vtx_players(id),
    kills INTEGER DEFAULT 0,
    placement INTEGER,
    points INTEGER DEFAULT 0,
    is_disqualified INTEGER DEFAULT 0,
    eliminated INTEGER DEFAULT 0,
    eliminated_at TIMESTAMP,
    UNIQUE(game_id, player_id)
);

CREATE TABLE IF NOT EXISTS vtx_lobby_players (
    id BIGSERIAL PRIMARY KEY,
    lobby_id INTEGER REFERENCES vtx_lobbies(id),
    player_id INTEGER REFERENCES vtx_players(id),
    joined_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    UNIQUE(lobby_id, player_id)
);

CREATE TABLE IF NOT EXISTS vtx_kills (
    id BIGSERIAL PRIMARY KEY,
    game_id INTEGER REFERENCES vtx_games(id),
    killer_id INTEGER REFERENCES vtx_players(id),
    victim_id INTEGER REFERENCES vtx_players(id),
    weapon TEXT,
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE IF NOT EXISTS vtx_game_team_members (
    id BIGSERIAL PRIMARY KEY,
    game_id INTEGER REFERENCES vtx_games(id),
    discord_id TEXT NOT NULL,
    team_lead_id TEXT NOT NULL,
    eliminated INTEGER DEFAULT 0,
    eliminated_at TIMESTAMP,
    UNIQUE(game_id, discord_id)
);

CREATE TABLE IF NOT EXISTS vtx_command_queue (
    id BIGSERIAL PRIMARY KEY,
    command TEXT NOT NULL,
    params TEXT NOT NULL DEFAULT '{}',
    status TEXT DEFAULT 'pending',
    result TEXT,
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    executed_at TIMESTAMP
);

CREATE TABLE IF NOT EXISTS vtx_bot_logs (
    id BIGSERIAL PRIMARY KEY,
    event_id INTEGER,
    action TEXT NOT NULL,
    details TEXT,
    user_id TEXT,
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE IF NOT EXISTS vtx_bans (
    id BIGSERIAL PRIMARY KEY,
    discord_id TEXT UNIQUE NOT NULL,
    reason TEXT,
    banned_until TEXT,
    created_by TEXT,
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE IF NOT EXISTS vtx_rank_tiers (
    id BIGSERIAL PRIMARY KEY,
    name TEXT UNIQUE NOT NULL,
    pr_min INTEGER NOT NULL
);

CREATE TABLE IF NOT EXISTS vtx_kv_store (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS vtx_season_stats (
    id BIGSERIAL PRIMARY KEY,
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

CREATE TABLE IF NOT EXISTS vtx_event_qualifiers (
    id BIGSERIAL PRIMARY KEY,
    event_id INTEGER REFERENCES vtx_events(id) NOT NULL,
    discord_id TEXT NOT NULL,
    username TEXT NOT NULL,
    team_members TEXT,
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    UNIQUE(event_id, discord_id)
);

CREATE TABLE IF NOT EXISTS vtx_event_interests (
    id BIGSERIAL PRIMARY KEY,
    event_id INTEGER REFERENCES vtx_events(id) NOT NULL,
    discord_id TEXT NOT NULL,
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    UNIQUE(event_id, discord_id)
);

CREATE TABLE IF NOT EXISTS vtx_event_wins (
    id BIGSERIAL PRIMARY KEY,
    event_id INTEGER REFERENCES vtx_events(id) NOT NULL,
    player_id INTEGER REFERENCES vtx_players(id) NOT NULL,
    season INTEGER NOT NULL DEFAULT 1,
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    UNIQUE(event_id, player_id)
);

CREATE TABLE IF NOT EXISTS vtx_invite_coins (
    discord_id TEXT PRIMARY KEY,
    coins INTEGER NOT NULL DEFAULT 0,
    total_invites INTEGER NOT NULL DEFAULT 0,
    updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE IF NOT EXISTS vtx_coin_purchases (
    id BIGSERIAL PRIMARY KEY,
    discord_id TEXT NOT NULL,
    product TEXT NOT NULL,
    role_id TEXT NOT NULL,
    guild_id TEXT NOT NULL DEFAULT '',
    issued_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    expires_at INTEGER
);

CREATE TABLE IF NOT EXISTS vtx_invite_rewards (
    id BIGSERIAL PRIMARY KEY,
    guild_id TEXT NOT NULL,
    inviter_id TEXT NOT NULL,
    invited_user_id TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'pending',
    created_at INTEGER,
    left_at INTEGER,
    approved_at INTEGER,
    quality_score INTEGER,
    coins_granted INTEGER NOT NULL DEFAULT 0,
    flagged INTEGER NOT NULL DEFAULT 0,
    loyalty_granted INTEGER NOT NULL DEFAULT 0,
    participation_granted INTEGER NOT NULL DEFAULT 0,
    reason TEXT
);

CREATE TABLE IF NOT EXISTS vtx_user_messages (
    discord_id TEXT PRIMARY KEY,
    count INTEGER NOT NULL DEFAULT 0
);

CREATE TABLE IF NOT EXISTS vtx_divisions (
    id BIGSERIAL PRIMARY KEY,
    name TEXT UNIQUE NOT NULL,
    role_id TEXT,
    guild_id TEXT,
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE IF NOT EXISTS vtx_division_members (
    id BIGSERIAL PRIMARY KEY,
    division_id INTEGER REFERENCES vtx_divisions(id) NOT NULL,
    discord_id TEXT NOT NULL,
    qualified_from_event_id INTEGER,
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    UNIQUE(division_id, discord_id)
);

CREATE TABLE IF NOT EXISTS vtx_bracket_matches (
    id BIGSERIAL PRIMARY KEY,
    event_id INTEGER REFERENCES vtx_events(id) NOT NULL,
    round INTEGER NOT NULL DEFAULT 1,
    position INTEGER NOT NULL DEFAULT 1,
    player1_id INTEGER REFERENCES vtx_players(id),
    player2_id INTEGER REFERENCES vtx_players(id),
    winner_id INTEGER REFERENCES vtx_players(id),
    status TEXT DEFAULT 'ready',
    UNIQUE(event_id, round, position)
);

CREATE TABLE IF NOT EXISTS vtx_duel_asks (
    id BIGSERIAL PRIMARY KEY,
    asker_id TEXT NOT NULL,
    partner_id TEXT,
    target_ids TEXT NOT NULL DEFAULT '[]',
    status TEXT DEFAULT 'pending',
    category_id TEXT,
    text_channel_id TEXT,
    voice_channel_id TEXT,
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    expires_at INTEGER
);

-- Seed rank tiers (idempotent).
INSERT INTO vtx_rank_tiers (name, pr_min) VALUES
    ('Unranked', 0),
    ('Bronze I', 20),
    ('Bronze II', 40),
    ('Bronze III', 60),
    ('Silver I', 80),
    ('Silver II', 110),
    ('Silver III', 140),
    ('Gold I', 170),
    ('Gold II', 210),
    ('Gold III', 250),
    ('Platinum I', 300),
    ('Platinum II', 360),
    ('Platinum III', 420),
    ('Diamond I', 500),
    ('Diamond II', 600),
    ('Diamond III', 700),
    ('Elite I', 850),
    ('Elite II', 1000),
    ('Elite III', 1200),
    ('Champion I', 1400),
    ('Champion II', 1650),
    ('Champion III', 1900),
    ('Unreal I', 2200),
    ('Unreal II', 2600),
    ('Unreal III', 3200)
ON CONFLICT (name) DO NOTHING;

-- Keep sequences in sync when rows are inserted with explicit ids (migration).
-- Not run here; migrate_from_sqlite.py resets sequences explicitly.