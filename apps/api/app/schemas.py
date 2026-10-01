from __future__ import annotations

import uuid
from datetime import datetime

from pydantic import BaseModel, ConfigDict, Field, field_validator


class LoginRequest(BaseModel):
    email: str
    password: str


class CurrentUser(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    email: str
    display_name: str
    role: str
    tenant_id: uuid.UUID
    tenant_name: str
    tenant_slug: str
    is_platform: bool


class LoginResponse(BaseModel):
    user: CurrentUser
    csrf_token: str


class StoreOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    store_code: str
    display_name: str
    address: str
    concept: str
    is_active: bool


class StyleOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    style_code: str
    title: str
    image_url: str


class TaskIn(BaseModel):
    """Create a monitor task.

    An empty ``store_codes`` means "every active store" (全国扫描), matching the
    scan API semantics; it is stored as ``all_stores`` on the task.
    """

    name: str = Field(min_length=1, max_length=120)
    store_codes: list[str] = Field(default_factory=list)
    style_codes: list[str] = Field(default_factory=list)
    cadence_seconds: int = Field(default=1800, ge=300, le=86400)
    is_active: bool = True
    notify_on_restock: bool = True
    notify_on_sold_out: bool = True
    notify_on_price_change: bool = True

    @field_validator("store_codes", "style_codes")
    @classmethod
    def _normalize(cls, value: list[str]) -> list[str]:
        seen: list[str] = []
        for item in value:
            cleaned = str(item).strip().upper()
            if cleaned and cleaned not in seen:
                seen.append(cleaned)
        return seen


class TaskUpdate(BaseModel):
    name: str | None = Field(default=None, min_length=1, max_length=120)
    cadence_seconds: int | None = Field(default=None, ge=300, le=86400)
    is_active: bool | None = None
    notify_on_restock: bool | None = None
    notify_on_sold_out: bool | None = None
    notify_on_price_change: bool | None = None


class TaskOut(BaseModel):
    id: uuid.UUID
    name: str
    store_codes: list[str]
    style_codes: list[str]
    all_stores: bool
    cadence_seconds: int
    is_active: bool
    notify_on_restock: bool
    notify_on_sold_out: bool
    notify_on_price_change: bool
    next_run_at: datetime | None
    last_run_at: datetime | None
    created_at: datetime
    store_count: int
    style_count: int


class ScanCreateIn(BaseModel):
    store_codes: list[str] = Field(default_factory=list)
    style_codes: list[str] = Field(default_factory=list)
    all_stores: bool = False
    task_id: uuid.UUID | None = None


class ScanStoreOut(BaseModel):
    store_code: str
    store_name: str
    status: str
    attempt: int
    error_code: str | None
    message: str
    observations_written: int
    events_written: int
    started_at: datetime | None
    finished_at: datetime | None


class ScanJobOut(BaseModel):
    id: uuid.UUID
    task_id: uuid.UUID | None
    kind: str
    status: str
    store_codes: list[str]
    style_codes: list[str]
    total_stores: int
    done_stores: int
    failed_stores: int
    unknown_stores: int
    attempt: int
    error_summary: str
    started_at: datetime | None
    finished_at: datetime | None
    created_at: datetime
    progress: float


class MatrixCell(BaseModel):
    store_code: str
    store_name: str
    size_label: str
    availability: str
    trust: str
    price_status: str
    price: float | None
    total_price: float | None
    promotions: list[str]
    error_code: str | None
    checked_at: datetime | None


class MatrixOut(BaseModel):
    style_code: str
    title: str
    image_url: str
    sizes: list[str]
    stores: list[dict]
    cells: list[MatrixCell]
    structure_status: str
    last_checked_at: datetime | None


class ChangeEventOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    event_type: str
    store_code: str
    store_name: str
    style_code: str
    style_title: str
    size_label: str
    before_status: str | None
    after_status: str | None
    before_total_price: float | None
    after_total_price: float | None
    promotions: list[str]
    checked_at: datetime
    detected_at: datetime


class DashboardOut(BaseModel):
    active_tasks: int
    monitored_styles: int
    monitored_stores: int
    store_coverage: int
    restock_today: int
    sold_out_today: int
    price_change_today: int
    new_arrival_today: int
    failed_jobs: int
    running_jobs: int
    last_checked_at: datetime | None
    credential_status: str
    credential_expires_at: datetime | None
    review_requests_today: int
    daily_review_budget: int


class HealthOut(BaseModel):
    status: str
    database: str
    queue_depth: int


class NikeCredentialOut(BaseModel):
    status: str
    phone_masked: str | None
    access_expires_at: datetime | None
    last_refreshed_at: datetime | None
    last_error_code: str | None
    binding_generation: int | None
    login_available: bool


class NikeBootstrapOut(BaseModel):
    """绑定后的预加载进度（门店目录只属于该用户）。"""

    state: str
    progress: float
    stage: str
    store_count: int
    error_code: str | None
    updated_at: datetime | None


class NikeAuthSessionIn(BaseModel):
    phone: str = Field(min_length=5, max_length=32)
    consent: bool
    request_id: uuid.UUID

    @field_validator("phone")
    @classmethod
    def _strip_phone(cls, value: str) -> str:
        return value.strip()

    @field_validator("consent")
    @classmethod
    def _require_consent(cls, value: bool) -> bool:
        if not value:
            raise ValueError("需要同意使用条款")
        return value


class NikeAuthVerifyIn(BaseModel):
    code: str = Field(min_length=1, max_length=32)

    @field_validator("code")
    @classmethod
    def _strip_code(cls, value: str) -> str:
        cleaned = value.strip()
        if not cleaned:
            raise ValueError("验证码不能为空")
        return cleaned


class NikeAuthAttemptOut(BaseModel):
    id: uuid.UUID
    state: str
    phone_masked: str
    expires_at: datetime | None
    resend_after: datetime | None
    attempts_remaining: int
    error_code: str | None


class NotificationChannelIn(BaseModel):
    """渠道表单：平台 + 该平台的字段，Apprise URL 由服务端拼装。"""

    platform: str = Field(min_length=1, max_length=32)
    name: str = Field(default="", max_length=64)
    fields: dict[str, str] = Field(default_factory=dict)


class NotificationChannelPatch(BaseModel):
    name: str | None = Field(default=None, max_length=64)
    enabled: bool | None = None


class NotificationChannelOut(BaseModel):
    id: uuid.UUID
    platform: str
    name: str
    target_hint: str
    enabled: bool
    last_ok_at: datetime | None
    last_error_code: str | None
    last_error: str | None
    last_error_at: datetime | None
    created_at: datetime


class NotificationDeliveryOut(BaseModel):
    id: int
    job_id: uuid.UUID
    task_name: str | None
    channel_id: uuid.UUID
    channel_name: str
    platform: str
    state: str
    attempts: int
    last_error: str | None
    created_at: datetime
    sent_at: datetime | None
    title: str
    summary: str


class DingTalkLinkCodeOut(BaseModel):
    """一次性绑定码：在钉钉里发给平台机器人即完成账号↔会话绑定。"""

    code: str
    expires_at: datetime


class DingTalkLinkOut(BaseModel):
    id: uuid.UUID
    conversation_title: str
    #: 1 单聊，2 群聊
    conversation_type: int
    created_at: datetime
