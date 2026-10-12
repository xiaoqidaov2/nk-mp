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

    #: 一次可用性请求的 GTIN 上限。上游实测 **50 是硬上限**（51 → 400 FIELD_INVALID），
    #: 原来是 40；同一门店的多个货号会一起进这个批次，所以提高直接减少请求数。
    availability_chunk_size: int = 50
    #: 缺价时的补价批量。价格本来就会随可用性响应回来，这里只补"还缺的"在货尺码；
    #: 上游对**单个请求的 items 总数**限制才是 50，价格并不需要拆 3 个/批（旧值是我方策略）。
    price_chunk_size: int = 10
    review_poll_tries: int = 12
    #: 首次轮询等待（秒）：实测 PUT 后**立即** GET 就已是 COMPLETED（0ms），默认不再等待；
    #: 未就绪时会按 review_poll_interval_seconds 退避重试。
    review_poll_first_seconds: float = 0.0
    #: 后续轮询的间隔上限（秒）：从 first 起每次翻倍，封顶到这个值。
    review_poll_interval_seconds: float = 2.5
    request_retry_limit: int = 7
    #: 405 挑战（阿里 WAF 阻断页）单独的重试上限。线上实测每个请求独立抽签、约一半
    #: 被拦；上限 7 时"连续被拦"概率 ≈0.8%（每轮约 5 家门店 UNKNOWN），提到 12 降到
    #: ≈0.02%。与 request_retry_limit 分开，避免 403/5xx 也一起放大。
    waf_retry_limit: int = 12
    #: 405 挑战的重试间隔（秒）：实测"立即重试"与退避成功率相同（7/8 vs 7/8），
    #: 但每次成功墙钟 0.43s vs 1.00s —— 退避只加时延，所以挑战重试几乎不等待。
    waf_retry_delay_seconds: float = 0.2
    #: 上游重试（405 挑战 / 5xx / 403 / 429 / 传输错误）的退避基数：base * 2^(n-1)，封顶 max。
    request_retry_base_seconds: float = 1.5
    request_retry_max_seconds: float = 8.0
    structure_ttl_days: int = 7

    worker_lease_seconds: int = 120
    #: 绑定后预加载（门店目录）任务的租约时长（秒）。
    bootstrap_lease_seconds: int = 120
    #: 单个作业内的门店并发数：1 = 顺序执行（默认）；>1 时单轮耗时近似成比例下降，
    #: 代价是瞬时上游请求数与配额消耗速度上升（风控由账号级冷却兜底）。
    store_concurrency: int = 1

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
    #: 同一任务失败门店自动补跑的总轮数上限（原作业算第 0 轮）。
    job_max_attempts: int = 3
    #: 补跑作业与冷却窗口的取较晚者：正常情况下的最小等待秒数。
    job_retry_delay_seconds: int = 60
    #: 派发粒度：越小越及时，代价是每分钟多一次轻查询（默认 10s）。
    scheduler_interval_seconds: int = 10
    default_cadence_seconds: int = 1800
    #: 派发成功后给下一轮时间加的随机右移上限（秒）；实际抖动 ≤ 间隔的 10%。
    #: 多个任务同一间隔时避免同秒齐发、把小时额度瞬间打满。
    dispatch_jitter_seconds: float = 10.0
    #: 派发收拢窗口（秒）：把"马上到期"的同类任务提前收进同一轮派发，否则抖动会让
    #: 同一批任务被摊到多轮里、每个任务各成一个作业（合并采集就失效了）。
    dispatch_merge_window_seconds: float = 30.0
    #: 每个 (门店, 货号, 尺码) 保留的历史观测条数：间隔小、量大，历史只用于回溯。
    observation_history_limit: int = 30
    daily_review_budget: int = 60000
    quota_hourly_cap: int = 900
    quota_hourly_slow_line: int = 600
    #: 被上游风控（403/429）命中后的冷却阶梯（秒，逗号分隔），连续命中逐级延长。
    throttle_backoff_seconds: str = "300,900,1800,3600"
    #: 同类冷却通知的最小间隔（秒）；默认 8 小时 => 每 24 小时最多 3 条。
    throttle_notice_interval_seconds: int = 28800
    #: 405 挑战风暴熔断：一轮里这么多家门店被 WAF 挑战打满重试就整轮结束并进入冷却
    #: （0 = 关闭；需求 D-NFR-RATE-06 的 15% 阈值在 160 店规模上约合 3 家门店）。
    waf_storm_store_limit: int = 0
    # ---- 出口池与指纹轮换（见 docs/cadence-throttle.md「容量」）----
    #: 上游出口代理列表（JSON 数组，如 ["http://1.2.3.4:8080"]）；空=直连。
    proxy_urls: list[str] = []
    #: 代理列表文件（每行一个 URL，# 注释）；约 60s 重载一次，换池不必重启。
    proxy_urls_file: str = ""
    #: True = 钉住同一个出口（稳定住宅出口：一账号一 IP，不轮换）；
    #: False = 老的"一请求一出口"轮转（多出口分散并发用）。
    proxy_sticky: bool = False
    #: 传输层失败后该出口冷却秒数（405 挑战是常态，不算失败、不冷却）。
    proxy_cooldown_seconds: float = 120.0
    #: 每请求轮换 UA/客户端提示；实测单请求通过率与特征无关，此项服务于并发规模。
    fingerprint_rotation: bool = True
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
