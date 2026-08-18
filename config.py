from __future__ import annotations

import re
from pathlib import Path

from pydantic_settings import BaseSettings, SettingsConfigDict


def digits_only(value: str) -> str:
    """Strip everything but digits from an ID setting.

    Tolerates paste artifacts like '=<id>' (Excel) or '<#id>' (channel mention).
    """
    if not value:
        return ""
    return re.sub(r"\D", "", value)


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )

    discord_bot_token: str = ""
    discord_guild_id: str = ""
    discord_admin_role_id: str = ""
    discord_registration_channel_id: str = ""
    discord_leaderboard_log_channel_id: str = ""
    discord_hall_of_fame_channel_id: str = ""
    discord_schedule_channel_id: str = ""
    discord_shop_pic_role_id: str = ""
    discord_media_lounge_channel_id: str = ""
    discord_lfg_channel_id: str = ""
    discord_crown_role_id: str = ""

    # Invite-coin anti-abuse thresholds (all overridable via .env)
    # invite_min_stay_hours=0 pays invite rewards out instantly (no stay wait)
    invite_min_stay_hours: int = 0
    invite_min_account_days: int = 7
    invite_reward_coins: int = 1
    invite_loyalty_bonus: int = 2
    invite_loyalty_days: int = 7
    invite_participation_bonus: int = 1
    invite_daily_limit: int = 10
    invite_weekly_limit: int = 50
    invite_score_approve: int = 70
    invite_score_review: int = 30
    invite_review_auto_days: int = 7
    invite_max_pending_days: int = 14
    invite_suspicious_joins: int = 5
    invite_suspicious_window_hours: int = 24
    invite_suspicious_created_days: int = 7
    discord_tournament_role_id: str = ""
    discord_scrim_role_id: str = ""
    discord_say_hi_user_id: str = ""
    dashboard_admin_password: str = ""
    dashboard_port: int = 8080

    database_path: str = str(Path(__file__).parent / "data" / "scrim.db")

    # Supabase Postgres connection string (postgresql://user:pass@host:5432/db).
    # When set, the bot uses Postgres instead of the local SQLite file.
    supabase_db_url: str = ""


settings = Settings()


def ensure_data_dir() -> None:
    db_dir = Path(settings.database_path).parent
    db_dir.mkdir(parents=True, exist_ok=True)
