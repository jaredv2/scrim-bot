# 🪙 Invite Coin System

Invite friends to the server and earn **coins per successful invite** — once
the invitee proves they're a real, staying member. Spend coins in the
`/shop` on timed picture permissions or cosmetic roles.

## How to use it (players)

| Command             | What it does                                                          |
| ------------------- | --------------------------------------------------------------------- |
| `/shop`             | Browse everything you can buy and its price                           |
| `/invite-coins`     | Check your coin balance (add `@user` to check someone else's)         |
| `/invite-info`      | See your invite links and how many people joined through each        |
| `/coin-top`         | Leaderboard of the biggest inviters                                   |
| `/pic-perms <dur>`  | Buy Pic Perms for `30s` (2 🪙), `1m` (5 🪙), `3m` (7 🪙), `5m` (15 🪙)  |
| `/buy-cosmetic`     | Buy a 24h cosmetic role — see rarity tiers below                      |

### Earning coins — step by step

1. Create a server invite (Server Settings → Member menu → **Invite People**).
2. A friend joins through your link → your reward moves to **pending**:
   - the invitee must stay in the server for **5 hours**, and
   - their Discord account must be at least **7 days old**.
3. When the reward is approved you are DM'd and get **+1 coin**.
4. The invitee player gets an **invite quality score** — members who chat and
   join events pay out faster; senders with very low scores may be rejected
   (see How it works below).
5. **Bonuses** — after the invited member stays **7 days**, you get
   **+2 loyalty coins**; if they also join an event (register/play), you get
   **+1 participation coin**.
6. If the invited member **leaves the server early**, the reward is
   **cancelled** — no coins.

### Pic Perms (showcase channel)

`Pic Perms` is a role that allows **posting images**, but only in the
`#📷｜showcase` channel — the bot locks that channel so everyone else is
text-only there. Purchasing a duration (30s / 1m / 3m / 5m) pins the role to
you for that long; when it expires the bot **removes the role automatically**
(and the permissions revert instantly).

## How it works (maintainers)

### Invite detection

Inviters are found by **invite-uses diffing** in `cogs/coins.py`:

1. On `on_ready` / `on_guild_join` the bot snapshots every invite per guild:
   `{invite_code: uses}`.
2. On `on_member_join` it re-fetches the invites; if a code's usage counter
   went up, that code's inviter is credited.
3. Self-invites (invited user == inviter) are ignored; bots don't count.

### Reward pipeline (anti-abuse)

Every join is recorded as a **pending** row in `invite_rewards`. A background
task (`rewards_loop`, every 60s) walks pending rows and applies these gates,
**in order**:

1. **Max pending age** — a pending row older than
   `INVITE_MAX_PENDING_DAYS` (default 14) is rejected.
2. **Leave detection** — if the invited user left before
   `INVITE_MIN_STAY_HOURS` (default 24h), the reward is rejected (also
   handled immediately on `on_member_remove`).
3. **Minimum stay** — payment only happens once the invitee has been in the
   server ≥ 24h.
4. **Minimum account age** — the invitee's Discord account must be
   ≥ `INVITE_MIN_ACCOUNT_DAYS` (default 7) days old; while the member is
   still present the row simply waits.
5. **Rate limits** — an inviter may earn at most `INVITE_DAILY_LIMIT` (10)
   approved coins per day and `INVITE_WEEKLY_LIMIT` (50) per week; beyond
   that rows stay pending until the window resets.
6. **Suspicion flag** — if an inviter lands ≥ `INVITE_SUSPICIOUS_JOINS` (5)
   joins within `INVITE_SUSPICIOUS_WINDOW_HOURS` (24h), **all** their pending
   rewards are flagged and paused until an admin reviews them
   (via `;invite-review` / `;invite-approve` / `;invite-reject`).
7. **Quality score** (0-100, stored in `quality_score`):
   - `≥ INVITE_SCORE_APPROVE` (70) → approved **+1 coin** (immediately).
   - `≥ INVITE_SCORE_REVIEW` (30) → held for manual review; **auto-approves**
     after `INVITE_REVIEW_AUTO_DAYS` (7) days.
   - `< 30` → rejected (`quality_too_low`).

The score blends: account age (35%), stay time (25%), message count (25%,
counted via `on_message` into `user_messages`), and event participation
(15%, checked in `registrations` / `game_players`).

> **Admin channels:** `;invite-review` (queue), `;invite-approve <id>`,
> `;invite-reject <id> [reason]` — approver action perform the same DM payouts
> a normal approval would.

### Bonuses

- **Loyalty:** approved rewards are re-checked by the loyalty loop (5 min
  interval); once the invitee has stayed ≥ `INVITE_LOYALTY_DAYS` (7) days
  past approval, the inviter is granted `INVITE_LOYALTY_BONUS` (2) coins.
- **Participation:** if the invitee has joined an event (register confirmed
  or played a game), the inviter gets `INVITE_PARTICIPATION_BONUS` (1) coin —
  granted either at approval time or at the loyalty sweep.

### Storage

- `invite_coins`: `discord_id` PK, `coins` balance, `total_invites`
  (lifetime payout count, used by `/coin-top`).
- `invite_rewards`: `guild_id`, `inviter_id`, `invited_user_id`, `status`
  (`pending` / `approved` / `rejected`), `created_at` (join ts), `left_at`,
  `approved_at`, `quality_score`, `coins_granted`, `flagged`, plus the
  loyalty/participation marks, `reason` (a reject/hold reason code).
- `user_messages`: per-user tooth on_message counts for the quality score.
- All tables auto-create via `init_db()` — no manual migrations.

### Spending & expiry

- `spend_coins()` deducts atomically (single transaction); a purchase can
  never overdraw.
- Purchases write a `coin_purchases` row (`role_id`, `guild_id`, `expires_at`
  unix seconds). A 60s loop removes the role once `expires_at` passes and
  deletes the row — restarts never stick expired roles.

### Roles & media lounge

- **Pic Perms** (teal) — auto-created unless `DISCORD_SHOP_PIC_ROLE_ID` is
  set (then that role is used instead).
- **`#📷｜showcase`**: auto-created unless `DISCORD_MEDIA_LOUNGE_CHANNEL_ID`
  is set. On startup and on every purchase, the bot syncs overwrites
  there: `@everyone` deny `attach_files` + `embed_links`, Pic Perms role
  allowed. Roles must sit **below the bot's highest role** or grant/removal
  will silently fail.

### Bot permissions required

- **Manage Server** — reading invites; without it nothing is tracked
  (warning logged).
- **Manage Roles** — role creation and grant/revoke.
- **Manage Channels** — to create/lock `#📷｜showcase`.
- **Send Messages / Embed Links** — shop and confirmation embeds.
- **Message Content intent** — for `on_message` chat counting (already
  enabled in `bot.py`).

## Troubleshooting

| Symptom                                  | Cause / fix                                         |
| ---------------------------------------- | --------------------------------------------------- |
| No pending DM after a join               | Bot lacks **Manage Server**; no invites readable (see logs `Cannot read invites for guild ...`) |
| Reward stuck "pending"                   | Normal — min stay 5h / account 7d not met yet; or flagged for review, or daily cap reached. Check with `;invite-review`. |
| Reward rejected right after a join      | Invited user left early / account too new / quality score < 30. |
| Coins earned but no DM                    | Member DMs closed; balance still credited.           |
| Role doesn't get removed at expiry        | Bot lost Manage Roles, or role is above the bot's own (Server Settings → Roles). |
| Pic Perms shows "showcase ..." but channel missing | Bot lacks Manage Channels — configure `DISCORD_MEDIA_LOUNGE_CHANNEL_ID`. |
| Score questions                            | `INVITE_SCORE_APPROVE` / `INVITE_SCORE_REVIEW` (config.py); score mixes age/stay/messages/participation. |

## Tuning knobs (`.env`)

```
INVITE_MIN_STAY_HOURS=5
INVITE_MIN_ACCOUNT_DAYS=7
INVITE_REWARD_COINS=1
INVITE_LOYALTY_DAYS=7
INVITE_LOYALTY_BONUS=2
INVITE_PARTICIPATION_BONUS=1
INVITE_DAILY_LIMIT=10
INVITE_WEEKLY_LIMIT=50
INVITE_SCORE_APPROVE=70
INVITE_SCORE_REVIEW=30
INVITE_REVIEW_AUTO_DAYS=7
INVITE_MAX_PENDING_DAYS=14
INVITE_SUSPICIOUS_JOINS=5
INVITE_SUSPICIOUS_WINDOW_HOURS=24
DISCORD_SHOP_PIC_ROLE_ID=
DISCORD_MEDIA_LOUNGE_CHANNEL_ID=
```

## Extending

- Pic durations: edit `PIC_PERM_DURATIONS` in `cogs/coins.py`; the shop
  embed reads it automatically.
- New reward gates: add steps inside `_process_reward` (order matters).