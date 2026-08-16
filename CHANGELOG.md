# Changelog

Changes are grouped by release, from the perspective of the people using the bot
and dashboard (event admins and players).

## Unreleased

### For players
- **Invite coin system hardened against abuse** — coins from invites are now
  **pending** until the invited member has stayed **5h** on an account
  **7+ days old**; leaving early cancels the reward. Inviters also earn
  loyalty (+2 after 7 days) and participation (+1 if the invitee joins an
  event) bonuses. Extremely low-score invitees pay nothing.
- **Pic Perms moved to the showcase channel** — images are only allowed in
  `#📷｜showcase` while you have the role (30s 2 🪙 / 1m 5 / 3m 7 / 5m 15).
- Admins can review the invite queue: `;invite-review`, `;invite-approve`,
  `;invite-reject`.
- **Dashboard**: player profiles now have an **Edit player data** form
  (Discord name, IGN, Game ID, country, region) and an **Export CSV** button
  on the Players page.
- Rank ladder compressed — every rank (Bronze → Unreal) now has I/II/III tiers
  and thresholds are much easier to reach; no ranks are skipped. Unreal Legend
  still requires 5000+ PR, 20+ wins and 200+ kills.
- `/game-style` personalities are richer (4 lines each, with new
  "Final Zone Phantom", "The Almost", "The Pacifist" flavors).
- `/compare` now posts a multi-line scorecard (PR, wins, kills, games,
  avg placement) plus a multi-line verdict.
- `/say-hi` is now a multi-line greeting.
- The bot's status cycles through 10 Vortex BuildNow-themed activities
  (watching/playing/listening/competing) every 3 minutes.
- `/say-hi` — new fun command to greet the boss.
- The ⭐ Qualify button no longer appears on signup messages in Discord.
  Qualification is now managed by admins in the dashboard or via `/admin qualify`.
- Scheduled events now show a 🔔 **Interested** button; pressing it adds you to
  the list and you get a **DM 1 hour before the event** starts. Press it again to
  remove yourself.
- Times in announcements, signup messages and game DMs are now **live Discord
  timestamps** (e.g. `<t:...:F>` / `<t:...:R>`) that show in your own timezone,
  whenever a start time like `3:00 PM EST` is given.

### For admins
- `create-event` and `create-scrim` gained a **PR multiplier** override
  (0 = automatic based on player count, as before) and a **shoot timer**
  (in seconds), which is announced in game DMs and on the dispatch embed.
- New `/schedule` command:
  - `/schedule <event_id> <time>` — schedule the event (e.g. `3:00 PM EST`),
    posts a schedule embed with the 🔔 Interested button in the current channel.
  - `/schedule` — list all scheduled events with relative times.
  - `/unschedule <event_id>` — removes the schedule, the posted embed and all
    pending reminders.
- New `DISCORD_SCHEDULE_CHANNEL_ID` setting (`.env`): when set, schedule embeds
  and the scheduled-events list are always posted there instead of the channel
  where the command ran.
- Interested players are DM'd automatically 1 hour before the scheduled start.
- `/end-game` gained optional `leaderboard_channel` and `tournament_channel`
  fields: results are posted to the chosen leaderboard channel (falls back to
  the event dispatch channel) and also to the tournament channel when given.
- `/log-leaderboard` gained an optional `channel` field (defaults to the event
  dispatch channel) and now pings **only the tournament role** when posting.
- Dashboard **Players tab** now shows a ⭐ Qualify / ⭐ Qualified button for every
  player and team, and toggling it marks them as qualified for the event
  (and its `move-qualified` flow) without touching Discord.
- New admin commands:
  - `/admin qualify <event_id> <@player>` — add a player to the qualified list.
  - `/admin qualified <event_id>` — list who is qualified.
  - `/admin remove-qualified <event_id> <@player>` — remove a player.
  - `/admin move-qualified <source> <target> confirm:yes` — move all qualified
    players to another event without re-registration.
- Game screen (dashboard):
  - A **Placement** column now always shows live projected placements while a
    game is running and updates as players get eliminated.
  - Re-clicking **Eliminate** (now labelled Undo) will un-eliminate a player and
    automatically re-dispatch the placements of everyone else.
  - **End Match** button locks placements; once a match is ended no eliminations
    or placement changes are possible until it is reset.
  - Final result announcements now include placement list, wins, average points
    and placement points.
- `/health` no longer refuses non-GET probes (fixes 405 from uptime monitors).
- **Upload & Restore (Backup tab)** is now safe and coordinated:
  the uploaded file is validated (`PRAGMA integrity_check`) before anything is
  touched, the current database is kept as `scrim.db.bak`, stale WAL/SHM files
  are removed, the swap is atomic, and the bot process automatically reloads
  its connection within ~3 seconds — no restart required. The old
  `/backup/upload` that overwrote the file raw is replaced by `/restore`.

## Notes

- Backups taken from the dashboard Backup tab (Download) are complete, consistent
  snapshots: the WAL is checkpointed into the file before the download, so the
  downloaded `.db` contains all of your data.
- Restoring a backup can be done right from the dashboard (Backup tab →
  Upload & Restore). It works on any host — Docker, Render, bare metal —
  because the swap is done at the file level and both processes (bot and
  dashboard) are told to reopen the database through a restart marker; they
  coordinate without timing guesses.
- Manual, container-level restore (e.g. before a fresh push): stop the service,
  delete stale `data/scrim.db-wal` / `-shm` files, replace `data/scrim.db` with
  the downloaded file, start again.