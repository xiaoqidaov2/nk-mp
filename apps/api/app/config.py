from functools import lru_cache

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", env_prefix="NIKE_MP_", extra="ignore")

    database_url: str = "postgresql+psycopg://nike_mp:nike_mp@db:5432/nike_mp"
    credential_key: str = ""
    session_ttl_hours: int = 12
    cookie_secure: bool = False
    cookie_domain: str | None = None

    nike_api_base: str = "https://api.nike.com.cn"
    nike_test_api_base: str = "https://api-test.nike.com.cn"
    nike_oauth_base: str = "https://accounts.nike.com.cn"
    nike_app_id: str = "com.nike.nvs.comerce.wo.wechat.prod"
    nike_partner: str = "NDS"
    nike_referer: str = "https://servicewechat.com/wx74b301953dcdd7db/18/page-frame.html"
    nike_user_agent: str = (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/144.0.0.0 Safari/537.36"
    )
    oauth_client_id: str = "790b76033778d3e6f18743582900a4ae"

    availability_chunk_size: int = 40
    price_chunk_size: int = 3
    review_poll_tries: int = 12
    review_poll_interval_seconds: float = 2.5
    request_retry_limit: int = 7
    request_pace_seconds: float = 1.2
    structure_ttl_days: int = 7

    worker_lease_seconds: int = 120
    worker_heartbeat_seconds: int = 20
    worker_poll_seconds: float = 3.0
    job_max_attempts: int = 3
    scheduler_interval_seconds: int = 30
    default_cadence_seconds: int = 1800
    daily_review_budget: int = 60000
    quota_hourly_cap: int = 900
    quota_hourly_slow_line: int = 600
    baseline_max_gap_seconds: int = 7 * 24 * 3600
    credential_refresh_cooldown_seconds: int = 60


@lru_cache
def get_settings() -> Settings:
    return Settings()
