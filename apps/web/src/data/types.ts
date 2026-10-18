export type Availability =
  | "IN_STOCK"
  | "OUT_OF_STOCK"
  | "NOT_CARRIED"
  | "UNKNOWN"
  | "STRUCTURE_MISSING";

export type PriceStatus = "OK" | "MISSING";
export type Trust = "TRUSTED" | "UNTRUSTED";

export interface CurrentUser {
  id: string;
  email: string;
  display_name: string;
  role: "ops_admin" | "ops_operator" | "tenant_admin" | "tenant_viewer";
  tenant_id: string;
  tenant_name: string;
  tenant_slug: string;
  is_platform: boolean;
}

export interface StoreOut {
  store_code: string;
  display_name: string;
  address: string;
  concept: string;
  is_active: boolean;
}

export interface StyleOut {
  style_code: string;
  title: string;
  image_url: string;
}

export interface MonitorTask {
  id: string;
  name: string;
  store_codes: string[];
  style_codes: string[];
  /** 选码：空数组表示全部尺码（与 store_codes 空=全部门店同构）。 */
  size_labels: string[];
  all_stores: boolean;
  cadence_seconds: number;
  is_active: boolean;
  notify_on_restock: boolean;
  notify_on_sold_out: boolean;
  notify_on_price_change: boolean;
  next_run_at: string | null;
  last_run_at: string | null;
  created_at: string;
  store_count: number;
  style_count: number;
  /** 账号级冷却（额度/风控受限）：非空表示冷却中，将在该时刻自动继续。 */
  cooldown_until: string | null;
  cooldown_reason: string | null;
}

export interface ScanJob {
  id: string;
  task_id: string | null;
  /** 合并检查作业覆盖的任务；单个任务的作业为空表（task_id 为空时靠它归属）。 */
  task_ids: string[];
  kind: string;
  status: "QUEUED" | "RUNNING" | "SUCCEEDED" | "PARTIAL" | "FAILED" | "CANCELLED";
  store_codes: string[];
  style_codes: string[];
  total_stores: number;
  done_stores: number;
  failed_stores: number;
  unknown_stores: number;
  attempt: number;
  error_summary: string;
  started_at: string | null;
  finished_at: string | null;
  created_at: string;
  progress: number;
}

export interface ScanStore {
  store_code: string;
  store_name: string;
  status: "PENDING" | "RUNNING" | "SUCCEEDED" | "FAILED" | "UNKNOWN";
  attempt: number;
  error_code: string | null;
  message: string;
  observations_written: number;
  events_written: number;
  started_at: string | null;
  finished_at: string | null;
}

export interface MatrixCell {
  store_code: string;
  store_name: string;
  size_label: string;
  availability: Availability;
  trust: Trust;
  price_status: PriceStatus;
  price: number | null;
  total_price: number | null;
  promotions: string[];
  error_code: string | null;
  checked_at: string | null;
}

export interface Matrix {
  style_code: string;
  title: string;
  image_url: string;
  sizes: string[];
  stores: { store_code: string; store_name: string }[];
  cells: MatrixCell[];
  structure_status: string;
  last_checked_at: string | null;
}

export interface ChangeEvent {
  id: number;
  event_type:
    | "RESTOCK"
    | "SOLD_OUT"
    | "PRICE_CHANGE"
    | "NEW_ARRIVAL_AT_STORE"
    | "SCAN_FAILED"
    | "STRUCTURE_CHANGED";
  store_code: string;
  store_name: string;
  style_code: string;
  style_title: string;
  size_label: string;
  before_status: string | null;
  after_status: string | null;
  before_total_price: number | null;
  after_total_price: number | null;
  promotions: string[];
  checked_at: string;
  detected_at: string;
}

export interface Dashboard {
  active_tasks: number;
  monitored_styles: number;
  monitored_stores: number;
  store_coverage: number;
  restock_today: number;
  sold_out_today: number;
  price_change_today: number;
  new_arrival_today: number;
  failed_jobs: number;
  running_jobs: number;
  last_checked_at: string | null;
  credential_status: string;
  credential_expires_at: string | null;
  review_requests_today: number;
  daily_review_budget: number;
}

/**
 * Personal Nike connection state, mirroring the backend `me/nike-credentials`
 * DTO. `NOT_CONNECTED` replaces the old shared/`UNASSIGNED` credential: the
 * platform never exposes a default or another user's binding.
 */
export type NikeCredentialStatus =
  | "NOT_CONNECTED"
  | "UNASSIGNED"
  | "ACTIVE"
  | "EXPIRING"
  | "NEEDS_AUTHORIZATION"
  | "REVOKED";

export interface NikeCredentialOut {
  /** 一行 = 一个已绑定的 Nike 账号；断开服务端按此 id 定位。 */
  id: string;
  status: NikeCredentialStatus;
  phone_masked: string | null;
  access_expires_at: string | null;
  last_refreshed_at: string | null;
  last_error_code: string | null;
  binding_generation: number | null;
  /** 非空且晚于当前时刻＝该账号正在冷却，到点自动恢复。 */
  cooldown_until: string | null;
  /** 后端原因码（如 "throttled"）；界面只显示中文解释，不直接展示。 */
  cooldown_reason: string | null;
  login_available: boolean;
}

/** Lifecycle of one本站 phone/SMS bind session; the frozen upstream contract
 * decides the code format, so the UI only drives off these server fields. */
export type NikeAuthAttemptState =
  | "SENDING"
  | "CODE_SENT"
  | "VERIFYING"
  | "CONNECTED"
  | "FAILED"
  | "EXPIRED"
  | "CANCELLED"
  | "CHALLENGE_REQUIRED";

/** Stable `error_code` values returned by the self-service auth endpoints. */
export type NikeAuthErrorCode =
  | "NIKE_AUTH_NOT_AVAILABLE"
  | "NIKE_AUTH_IN_PROGRESS"
  | "NIKE_AUTH_SESSION_MISMATCH"
  | "NIKE_AUTH_REQUEST_CONFLICT"
  | "NIKE_SMS_SEND_UNCERTAIN"
  | "NIKE_VERIFY_UNCERTAIN"
  | "NIKE_CHALLENGE_REQUIRED"
  | "NIKE_AUTH_CONTRACT_CHANGED"
  | "NIKE_CODE_INVALID"
  | "NIKE_CODE_EXPIRED"
  | "NIKE_AUTH_RATE_LIMITED"
  | "NIKE_PHONE_CONNECT_UNAVAILABLE"
  | "NIKE_CREDENTIAL_REQUIRED"
  | "NIKE_CREDENTIAL_CHANGED"
  | "NIKE_REAUTH_REQUIRED"
  | "NIKE_UPSTREAM_UNAVAILABLE";

export type NikeBootstrapState = "NONE" | "PENDING" | "RUNNING" | "SUCCEEDED" | "FAILED";

/** 绑定后的预加载进度：门店目录用本人连接拉取，只属于本人（缓存）。 */
export interface NikeBootstrapOut {
  state: NikeBootstrapState;
  progress: number;
  stage: string;
  store_count: number;
  error_code: string | null;
  updated_at: string | null;
}

/** Attempt DTO returned by `POST/GET me/nike-auth/sessions`. */
export interface NikeAuthAttempt {
  id: string;
  state: NikeAuthAttemptState;
  phone_masked: string;
  expires_at: string | null;
  resend_after: string | null;
  attempts_remaining: number | null;
  error_code: NikeAuthErrorCode | null;
}

/** Output alias kept for symmetry with the backend `NikeAuthAttemptOut` schema. */
export type NikeAuthAttemptOut = NikeAuthAttempt;

export type NotificationPlatform =
  | "dingtalk"
  | "dingtalk_app"
  | "feishu"
  | "wecombot"
  | "schan"
  | "bark"
  | "mailto"
  | "custom";

/** 一条推送渠道；target_hint 只保留非敏感的提示，完整地址不下发。 */
export interface NotificationChannel {
  id: string;
  platform: NotificationPlatform;
  name: string;
  target_hint: string;
  enabled: boolean;
  last_ok_at: string | null;
  last_error_code: string | null;
  last_error: string | null;
  last_error_at: string | null;
  created_at: string;
}

export type NotificationDeliveryState = "PENDING" | "SENT" | "FAILED";

/** 一次检查聚合出来的单条推送及其投递状态。 */
/** 今日推送用量：达到上限后不再排新消息，页面据此提示。 */
export interface PushUsage {
  used: number;
  cap: number;
  limited: boolean;
}

export interface NotificationDelivery {
  id: number;
  job_id: string | null;
  task_name: string | null;
  channel_id: string | null;
  channel_name: string | null;
  platform: NotificationPlatform | null;
  state: NotificationDeliveryState;
  attempts: number;
  last_error: string | null;
  created_at: string;
  sent_at: string | null;
  title: string;
  summary: string;
}

/** 客户自己填的钉钉企业机器人（AppKey/AppSecret）配置状态。 */
export interface DingTalkRobot {
  configured: boolean;
  robot_code: string;
}

/** 钉钉机器人在钉钉里绑定的一个会话（群或单聊）。 */
export interface DingTalkLink {
  id: string;
  conversation_title: string;
  /** 1 单聊，2 群聊 */
  conversation_type: number;
  created_at: string;
}

/** 一个调度/执行进程最近一次心跳；`alive` 由服务端按新鲜度判定。 */
export interface SchedulerHealthInstance {
  instance_id: string;
  last_seen_at: string | null;
  age_seconds: number;
  alive: boolean;
}

/** 执行节点在此基础上附上正在跑的作业数。 */
export interface SchedulerHealthWorker extends SchedulerHealthInstance {
  running_jobs: number;
}

export interface SchedulerOverdueTask {
  task_id: string;
  name: string;
  next_run_at: string;
  overdue_seconds: number;
  last_finished_at: string | null;
}

export interface SchedulerCoolingAccount {
  owner_user_id: string;
  credential_id: string;
  /** 后端原因码；界面映射成中文，不直接展示。 */
  reason: string | null;
  until: string;
}

/** 运维接口 `GET /ops/scheduler-health` 的实时快照（不做落库）。 */
export interface SchedulerDisk {
  total_gb: number;
  used_gb: number;
  free_gb: number;
  used_percent: number;
  warning: boolean;
}

export interface SchedulerHealth {
  scheduler: SchedulerHealthInstance[];
  workers: SchedulerHealthWorker[];
  overdue_tasks: SchedulerOverdueTask[];
  cooling_accounts: SchedulerCoolingAccount[];
  queue: Record<"QUEUED" | "RUNNING" | "SUCCEEDED" | "PARTIAL" | "FAILED" | "CANCELLED", number>;
  disk: SchedulerDisk;
}
