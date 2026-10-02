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
    #: 绑定后预加载（门店目录）任务的租约时长（秒）。
    bootstrap_lease_seconds: int = 120

    #: 推送消息里的"查看"链接前缀（前端地址）。
    public_base_url: str = "http://127.0.0.1:15173"
    #: 关闭后不再产生新的推送投递。
    push_enabled: bool = True
    push_max_channels_per_user: int = 10
    #: 每用户滚动 24 小时的投递上限（含各种状态）。
    push_max_per_day: int = 200
    push_timeout_seconds: int = 8
    #: 第 N 次失败后的等待秒数（逗号分隔）；用完后置为 FAILED。
    push_backoff_seconds: str = "60,300,900"
    #: 单个 worker 循环处理的投递条数。
    push_drain_batch: int = 10
    #: 允许推送到内网/回环地址（仅离线冒烟需要，生产保持 False）。
    push_allow_private_targets: bool = False

    #: 客户在“消息推送”里填过钉钉机器人凭据后，API 进程按账号保持 Stream 连接；测试里关掉。
    dingtalk_bot_enabled: bool = True

    #: 门店目录缓存的保鲜期（秒）：到期后调度器会用该用户自己的连接自动重取。
    catalog_ttl_seconds: int = 86400
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

    # Self-service phone binding (disabled by default; the contract version
    # must match SUPPORTED_CONTRACT_VERSION in app/nike/phone_auth.py).
    nike_phone_auth_enabled: bool = False
    nike_phone_auth_contract_version: str = ""
    nike_sms_cooldown_seconds: int = 60
    nike_sms_user_hour_limit: int = 5
    nike_sms_phone_hour_limit: int = 5
    nike_sms_phone_day_limit: int = 10
    nike_sms_ip_hour_limit: int = 20
    nike_sms_session_ttl_seconds: int = 300
    nike_sms_verify_limit: int = 5
    nike_sms_lock_seconds: int = 900

    # 密码登录失败限速：连续失败达到次数后锁定一段时间。
    login_max_attempts: int = 5
    login_lock_seconds: int = 300
    nike_sms_operation_timeout_seconds: int = 60
    nike_sms_trusted_proxy_cidrs: list[str] = []


@lru_cache
def get_settings() -> Settings:
    return Settings()
