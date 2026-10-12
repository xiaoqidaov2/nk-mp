from __future__ import annotations

import uuid
from datetime import datetime
from enum import StrEnum

from sqlalchemy import (
    BigInteger,
    Boolean,
    DateTime,
    Float,
    ForeignKey,
    Index,
    Integer,
    Numeric,
    String,
    Text,
    UniqueConstraint,
    func,
    text,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.dialects.postgresql import UUID as PGUUID
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.db import Base


class Role(StrEnum):
    OPS_ADMIN = "ops_admin"
    OPS_OPERATOR = "ops_operator"
    TENANT_ADMIN = "tenant_admin"
    TENANT_VIEWER = "tenant_viewer"


class Availability(StrEnum):
    IN_STOCK = "IN_STOCK"
    OUT_OF_STOCK = "OUT_OF_STOCK"
    NOT_CARRIED = "NOT_CARRIED"
    UNKNOWN = "UNKNOWN"
    STRUCTURE_MISSING = "STRUCTURE_MISSING"


class PriceStatus(StrEnum):
    OK = "OK"
    MISSING = "MISSING"


class Trust(StrEnum):
    TRUSTED = "TRUSTED"
    UNTRUSTED = "UNTRUSTED"


class JobStatus(StrEnum):
    QUEUED = "QUEUED"
    RUNNING = "RUNNING"
    SUCCEEDED = "SUCCEEDED"
    PARTIAL = "PARTIAL"
    FAILED = "FAILED"
    CANCELLED = "CANCELLED"


class StoreJobStatus(StrEnum):
    PENDING = "PENDING"
    RUNNING = "RUNNING"
    SUCCEEDED = "SUCCEEDED"
    FAILED = "FAILED"
    UNKNOWN = "UNKNOWN"


class EventType(StrEnum):
    RESTOCK = "RESTOCK"
    SOLD_OUT = "SOLD_OUT"
    PRICE_CHANGE = "PRICE_CHANGE"
    NEW_ARRIVAL_AT_STORE = "NEW_ARRIVAL_AT_STORE"
    SCAN_FAILED = "SCAN_FAILED"
    STRUCTURE_CHANGED = "STRUCTURE_CHANGED"


class CredentialStatus(StrEnum):
    UNASSIGNED = "UNASSIGNED"
    ACTIVE = "ACTIVE"
    EXPIRING = "EXPIRING"
    NEEDS_AUTHORIZATION = "NEEDS_AUTHORIZATION"
    REVOKED = "REVOKED"


class BootstrapState(StrEnum):
    PENDING = "PENDING"
    RUNNING = "RUNNING"
    SUCCEEDED = "SUCCEEDED"
    FAILED = "FAILED"


class NikeAuthState(StrEnum):
    SENDING = "SENDING"
    CODE_SENT = "CODE_SENT"
    VERIFYING = "VERIFYING"
    CONNECTED = "CONNECTED"
    FAILED = "FAILED"
    EXPIRED = "EXPIRED"
    CANCELLED = "CANCELLED"
    CHALLENGE_REQUIRED = "CHALLENGE_REQUIRED"


ACTIVE_AUTH_STATES = (
    NikeAuthState.SENDING.value,
    NikeAuthState.CODE_SENT.value,
    NikeAuthState.VERIFYING.value,
)


class StructureStatus(StrEnum):
    OK = "OK"
    MISSING = "MISSING"


class NotificationPlatform(StrEnum):
    DINGTALK = "dingtalk"
    #: 钉钉企业内部应用机器人：AppKey/AppSecret 换 accessToken 后调新版群消息接口。
    DINGTALK_APP = "dingtalk_app"
    FEISHU = "feishu"
    WECOMBOT = "wecombot"
    SCHAN = "schan"
    BARK = "bark"
    MAILTO = "mailto"
    CUSTOM = "custom"


class NotificationDeliveryState(StrEnum):
    PENDING = "PENDING"
    SENT = "SENT"
    FAILED = "FAILED"


def _uuid() -> uuid.UUID:
    return uuid.uuid4()


class Tenant(Base):
    __tablename__ = "tenant"

    id: Mapped[uuid.UUID] = mapped_column(PGUUID(as_uuid=True), primary_key=True, default=_uuid)
    slug: Mapped[str] = mapped_column(String(64), unique=True)
    name: Mapped[str] = mapped_column(String(160))
    is_platform: Mapped[bool] = mapped_column(Boolean, default=False)
    is_active: Mapped[bool] = mapped_column(Boolean, default=True)
    daily_review_budget: Mapped[int] = mapped_column(Integer, default=5000)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())

    users: Mapped[list[User]] = relationship(back_populates="tenant")


class User(Base):
    __tablename__ = "app_user"
    __table_args__ = (UniqueConstraint("email", name="uq_app_user_email"),)

    id: Mapped[uuid.UUID] = mapped_column(PGUUID(as_uuid=True), primary_key=True, default=_uuid)
    tenant_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("tenant.id", ondelete="CASCADE"))
    email: Mapped[str] = mapped_column(String(200))
    display_name: Mapped[str] = mapped_column(String(120), default="")
    password_hash: Mapped[str] = mapped_column(String(255))
    role: Mapped[str] = mapped_column(String(32))
    is_active: Mapped[bool] = mapped_column(Boolean, default=True)
    last_login_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    failed_login_attempts: Mapped[int] = mapped_column(Integer, default=0, server_default="0")
    login_locked_until: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())

    tenant: Mapped[Tenant] = relationship(back_populates="users")


class AuthSession(Base):
    __tablename__ = "auth_session"

    id: Mapped[uuid.UUID] = mapped_column(PGUUID(as_uuid=True), primary_key=True, default=_uuid)
    user_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("app_user.id", ondelete="CASCADE"))
    token_hash: Mapped[str] = mapped_column(String(64), unique=True)
    csrf_secret: Mapped[str] = mapped_column(String(64))
    user_agent: Mapped[str] = mapped_column(String(300), default="")
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    revoked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())

    user: Mapped[User] = relationship()


class Store(Base):
    __tablename__ = "store"
    __table_args__ = (UniqueConstraint("owner_user_id", "store_code", name="uq_store_owner_code"),)

    id: Mapped[uuid.UUID] = mapped_column(PGUUID(as_uuid=True), primary_key=True, default=_uuid)
    #: 目录通过谁的 Nike 连接取得，就归谁；不共享给其他用户。
    owner_user_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("app_user.id", ondelete="CASCADE")
    )
    store_code: Mapped[str] = mapped_column(String(32))
    display_name: Mapped[str] = mapped_column(String(200), default="")
    address: Mapped[str] = mapped_column(String(300), default="")
    concept: Mapped[str] = mapped_column(String(120), default="")
    region_id: Mapped[str] = mapped_column(String(64), default="")
    is_active: Mapped[bool] = mapped_column(Boolean, default=True)
    last_seen_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )


class StoreRegionAlias(Base):
    __tablename__ = "store_region_alias"
    __table_args__ = (
        UniqueConstraint("owner_user_id", "region_id", name="uq_store_region_alias_owner_region"),
    )

    id: Mapped[uuid.UUID] = mapped_column(PGUUID(as_uuid=True), primary_key=True, default=_uuid)
    owner_user_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("app_user.id", ondelete="CASCADE")
    )
    store_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("store.id", ondelete="CASCADE"))
    region_id: Mapped[str] = mapped_column(String(64))
    source: Mapped[str] = mapped_column(String(32), default="regions_api")
    first_seen_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    last_seen_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())

    store: Mapped[Store] = relationship()


class Style(Base):
    __tablename__ = "style"
    __table_args__ = (UniqueConstraint("owner_user_id", "style_code", name="uq_style_owner_code"),)

    id: Mapped[uuid.UUID] = mapped_column(PGUUID(as_uuid=True), primary_key=True, default=_uuid)
    owner_user_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("app_user.id", ondelete="CASCADE")
    )
    style_code: Mapped[str] = mapped_column(String(64))
    title: Mapped[str] = mapped_column(String(300), default="")
    image_url: Mapped[str] = mapped_column(String(600), default="")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )


class GtinStructure(Base):
    __tablename__ = "gtin_structure"
    __table_args__ = (
        UniqueConstraint(
            "owner_user_id", "store_id", "style_id", name="uq_gtin_structure_owner_store_style"
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(PGUUID(as_uuid=True), primary_key=True, default=_uuid)
    owner_user_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("app_user.id", ondelete="CASCADE")
    )
    store_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("store.id", ondelete="CASCADE"))
    style_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("style.id", ondelete="CASCADE"))
    status: Mapped[str] = mapped_column(String(16), default=StructureStatus.OK.value)
    structure_hash: Mapped[str] = mapped_column(String(64), default="")
    sizes: Mapped[list[dict]] = mapped_column(JSONB, default=list)
    fetched_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    store: Mapped[Store] = relationship()
    style: Mapped[Style] = relationship()


class OwnerBootstrap(Base):
    """绑定成功后的预加载计划：用该用户自己的连接拉取并缓存其门店目录。

    数据只属于这个用户；进度对用户可见（环形进度条）。
    """

    __tablename__ = "owner_bootstrap"

    owner_user_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("app_user.id", ondelete="CASCADE"), primary_key=True
    )
    state: Mapped[str] = mapped_column(String(16), default=BootstrapState.PENDING.value)
    progress: Mapped[float] = mapped_column(Float, default=0.0)
    stage: Mapped[str] = mapped_column(String(80), default="")
    store_count: Mapped[int] = mapped_column(Integer, default=0)
    error_code: Mapped[str | None] = mapped_column(String(48))
    attempts: Mapped[int] = mapped_column(Integer, default=0)
    lease_until: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )


class Observation(Base):
    __tablename__ = "observation"
    __table_args__ = (
        Index("ix_observation_store_style_checked", "store_id", "style_id", "checked_at"),
    )

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    owner_user_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("app_user.id", ondelete="SET NULL")
    )
    credential_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("credential.id", ondelete="SET NULL")
    )
    credential_generation: Mapped[int | None] = mapped_column(Integer)
    store_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("store.id", ondelete="CASCADE"))
    style_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("style.id", ondelete="CASCADE"))
    size_label: Mapped[str] = mapped_column(String(32))
    gtin: Mapped[str] = mapped_column(String(32), default="")
    availability: Mapped[str] = mapped_column(String(32))
    trust: Mapped[str] = mapped_column(String(16), default=Trust.TRUSTED.value)
    price_status: Mapped[str] = mapped_column(String(16), default=PriceStatus.MISSING.value)
    price: Mapped[float | None] = mapped_column(Numeric(10, 2))
    full_price: Mapped[float | None] = mapped_column(Numeric(10, 2))
    discount_amount: Mapped[float | None] = mapped_column(Numeric(10, 2))
    total_price: Mapped[float | None] = mapped_column(Numeric(10, 2))
    promotions: Mapped[list] = mapped_column(JSONB, default=list)
    error_code: Mapped[str | None] = mapped_column(String(48))
    job_id: Mapped[uuid.UUID | None] = mapped_column(
        PGUUID(as_uuid=True), ForeignKey("scan_job.id", ondelete="SET NULL")
    )
    checked_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))


class ObservationLatest(Base):
    __tablename__ = "observation_latest"
    __table_args__ = (
        UniqueConstraint(
            "owner_user_id", "store_id", "style_id", "size_label",
            name="uq_observation_latest_owner_store_style_size",
        ),
    )

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    owner_user_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("app_user.id", ondelete="SET NULL")
    )
    credential_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("credential.id", ondelete="SET NULL")
    )
    credential_generation: Mapped[int | None] = mapped_column(Integer)
    store_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("store.id", ondelete="CASCADE")
    )
    style_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("style.id", ondelete="CASCADE")
    )
    size_label: Mapped[str] = mapped_column(String(32))
    gtin: Mapped[str] = mapped_column(String(32), default="")
    availability: Mapped[str] = mapped_column(String(32))
    trust: Mapped[str] = mapped_column(String(16), default=Trust.TRUSTED.value)
    price_status: Mapped[str] = mapped_column(String(16), default=PriceStatus.MISSING.value)
    price: Mapped[float | None] = mapped_column(Numeric(10, 2))
    full_price: Mapped[float | None] = mapped_column(Numeric(10, 2))
    discount_amount: Mapped[float | None] = mapped_column(Numeric(10, 2))
    total_price: Mapped[float | None] = mapped_column(Numeric(10, 2))
    promotions: Mapped[list] = mapped_column(JSONB, default=list)
    error_code: Mapped[str | None] = mapped_column(String(48))
    baseline_ready: Mapped[bool] = mapped_column(Boolean, default=False)
    last_observation_id: Mapped[int | None] = mapped_column(
        BigInteger, ForeignKey("observation.id", ondelete="SET NULL")
    )
    checked_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))


class MonitorTask(Base):
    __tablename__ = "monitor_task"
    __table_args__ = (UniqueConstraint("created_by", "name", name="uq_monitor_task_owner_name"),)

    id: Mapped[uuid.UUID] = mapped_column(PGUUID(as_uuid=True), primary_key=True, default=_uuid)
    tenant_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("tenant.id", ondelete="CASCADE"))
    name: Mapped[str] = mapped_column(String(160))
    store_codes: Mapped[list] = mapped_column(JSONB, default=list)
    style_codes: Mapped[list] = mapped_column(JSONB, default=list)
    #: 任务级尺码选择；空列表 = 全部尺码（与 store_codes 空=全部门店同构）。
    #: 保留大小写（尺码形如 "42.5"/"XL"），不能复用 store/style 的 uppercase 规范化。
    size_labels: Mapped[list] = mapped_column(JSONB, default=list)
    all_stores: Mapped[bool] = mapped_column(Boolean, default=False)
    cadence_seconds: Mapped[int] = mapped_column(Integer, default=1800)
    is_active: Mapped[bool] = mapped_column(Boolean, default=True)
    notify_on_restock: Mapped[bool] = mapped_column(Boolean, default=True)
    notify_on_sold_out: Mapped[bool] = mapped_column(Boolean, default=True)
    notify_on_price_change: Mapped[bool] = mapped_column(Boolean, default=True)
    next_run_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    last_run_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    created_by: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("app_user.id", ondelete="SET NULL")
    )
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )


class ScanJob(Base):
    __tablename__ = "scan_job"
    __table_args__ = (Index("ix_scan_job_status_created", "status", "created_at"),)

    id: Mapped[uuid.UUID] = mapped_column(PGUUID(as_uuid=True), primary_key=True, default=_uuid)
    tenant_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("tenant.id", ondelete="CASCADE"))
    task_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("monitor_task.id", ondelete="SET NULL"))
    kind: Mapped[str] = mapped_column(String(16), default="SCAN")
    status: Mapped[str] = mapped_column(String(16), default=JobStatus.QUEUED.value)
    store_codes: Mapped[list] = mapped_column(JSONB, default=list)
    style_codes: Mapped[list] = mapped_column(JSONB, default=list)
    #: 合并采集：一个作业覆盖多个任务（字符串 UUID 列表）；单任务作业为空、用 ``task_id``。
    task_ids: Mapped[list] = mapped_column(JSONB, default=list)
    #: 入队时从任务冻结的尺码选择；空列表 = 全部尺码。
    size_labels: Mapped[list] = mapped_column(JSONB, default=list)
    total_stores: Mapped[int] = mapped_column(Integer, default=0)
    done_stores: Mapped[int] = mapped_column(Integer, default=0)
    failed_stores: Mapped[int] = mapped_column(Integer, default=0)
    unknown_stores: Mapped[int] = mapped_column(Integer, default=0)
    attempt: Mapped[int] = mapped_column(Integer, default=0)
    #: 失败门店自动补跑的轮次：原作业为 0，每次补跑 +1（上限 job_max_attempts）。
    retry_round: Mapped[int] = mapped_column(Integer, default=0, server_default="0")
    #: 最早可被租约的时间（冷却窗口内的补跑作业不会被提前领走）。
    not_before: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    lease_expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    heartbeat_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    leased_by: Mapped[str | None] = mapped_column(String(96))
    requested_by: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("app_user.id", ondelete="SET NULL")
    )
    owner_user_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("app_user.id", ondelete="SET NULL")
    )
    credential_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("credential.id", ondelete="SET NULL")
    )
    credential_generation: Mapped[int | None] = mapped_column(Integer)
    error_summary: Mapped[str] = mapped_column(Text, default="")
    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class ScanJobStore(Base):
    __tablename__ = "scan_job_store"
    __table_args__ = (UniqueConstraint("job_id", "store_id", name="uq_scan_job_store"),)

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    job_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("scan_job.id", ondelete="CASCADE"))
    store_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("store.id", ondelete="CASCADE"))
    status: Mapped[str] = mapped_column(String(16), default=StoreJobStatus.PENDING.value)
    attempt: Mapped[int] = mapped_column(Integer, default=0)
    error_code: Mapped[str | None] = mapped_column(String(48))
    message: Mapped[str] = mapped_column(String(300), default="")
    observations_written: Mapped[int] = mapped_column(Integer, default=0)
    events_written: Mapped[int] = mapped_column(Integer, default=0)
    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class ChangeEvent(Base):
    __tablename__ = "change_event"
    __table_args__ = (
        UniqueConstraint("dedupe_key", name="uq_change_event_dedupe"),
        Index("ix_change_event_detected", "detected_at"),
        Index("ix_change_event_tenant_detected", "tenant_id", "detected_at"),
        Index("ix_change_event_job_event", "job_id", "event_type"),
    )

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    tenant_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("tenant.id", ondelete="CASCADE"))
    owner_user_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("app_user.id", ondelete="SET NULL")
    )
    credential_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("credential.id", ondelete="SET NULL")
    )
    credential_generation: Mapped[int | None] = mapped_column(Integer)
    job_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("scan_job.id", ondelete="SET NULL"))
    store_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("store.id", ondelete="CASCADE"))
    style_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("style.id", ondelete="CASCADE")
    )
    size_label: Mapped[str] = mapped_column(String(32), default="")
    event_type: Mapped[str] = mapped_column(String(32))
    before_status: Mapped[str | None] = mapped_column(String(32))
    after_status: Mapped[str | None] = mapped_column(String(32))
    before_total_price: Mapped[float | None] = mapped_column(Numeric(10, 2))
    after_total_price: Mapped[float | None] = mapped_column(Numeric(10, 2))
    promotions: Mapped[list] = mapped_column(JSONB, default=list)
    payload_hash: Mapped[str] = mapped_column(String(64), default="")
    dedupe_key: Mapped[str] = mapped_column(String(200))
    before_observation_id: Mapped[int | None] = mapped_column(
        BigInteger, ForeignKey("observation.id", ondelete="SET NULL")
    )
    after_observation_id: Mapped[int | None] = mapped_column(
        BigInteger, ForeignKey("observation.id", ondelete="SET NULL")
    )
    checked_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    detected_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())

    store: Mapped[Store] = relationship()
    style: Mapped[Style] = relationship()


class Credential(Base):
    __tablename__ = "credential"
    __table_args__ = (
        UniqueConstraint("owner_user_id", name="uq_credential_owner_user"),
        Index(
            "uq_credential_phone_active",
            "phone_hash",
            unique=True,
            postgresql_where=text("phone_hash IS NOT NULL AND status <> 'REVOKED'"),
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(PGUUID(as_uuid=True), primary_key=True, default=_uuid)
    owner_user_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("app_user.id", ondelete="CASCADE")
    )
    binding_generation: Mapped[int] = mapped_column(Integer, default=1, server_default="1")
    client_id: Mapped[str] = mapped_column(String(120), default="")
    access_token_enc: Mapped[str] = mapped_column(Text, default="")
    refresh_token_enc: Mapped[str] = mapped_column(Text, default="")
    status: Mapped[str] = mapped_column(String(32), default=CredentialStatus.UNASSIGNED.value)
    access_expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    last_refreshed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    last_attempt_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    last_error: Mapped[str] = mapped_column(String(300), default="")
    last_error_code: Mapped[str | None] = mapped_column(String(48))
    #: 账号级冷却：额度/风控受限时暂停派发，到点自动恢复（见 services/throttle.py）。
    cooldown_until: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    cooldown_level: Mapped[int] = mapped_column(Integer, default=0, server_default="0")
    cooldown_reason: Mapped[str | None] = mapped_column(String(32))
    cooldown_notified_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    phone_masked: Mapped[str | None] = mapped_column(String(32))
    phone_hash: Mapped[str | None] = mapped_column(String(64))
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )


class NikeAuthAttempt(Base):
    __tablename__ = "nike_auth_attempt"
    __table_args__ = (
        UniqueConstraint(
            "owner_user_id", "client_request_id", name="uq_nike_auth_attempt_request"
        ),
        Index(
            "uq_nike_auth_attempt_owner_active",
            "owner_user_id",
            unique=True,
            postgresql_where=text("state IN ('SENDING','CODE_SENT','VERIFYING')"),
        ),
        Index(
            "uq_nike_auth_attempt_phone_active",
            "phone_hash",
            unique=True,
            postgresql_where=text("state IN ('SENDING','CODE_SENT','VERIFYING')"),
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(PGUUID(as_uuid=True), primary_key=True, default=_uuid)
    owner_user_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("app_user.id", ondelete="SET NULL")
    )
    auth_session_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("auth_session.id", ondelete="SET NULL")
    )
    client_request_id: Mapped[uuid.UUID] = mapped_column(PGUUID(as_uuid=True))
    state: Mapped[str] = mapped_column(String(24), default=NikeAuthState.SENDING.value)
    phone_enc: Mapped[str] = mapped_column(Text, default="")
    phone_masked: Mapped[str] = mapped_column(String(32), default="")
    phone_hash: Mapped[str] = mapped_column(String(64))
    ip_hash: Mapped[str] = mapped_column(String(64), default="")
    provider_context_enc: Mapped[str] = mapped_column(Text, default="")
    base_credential_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("credential.id", ondelete="SET NULL")
    )
    base_generation: Mapped[int] = mapped_column(Integer, default=0, server_default="0")
    result_credential_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("credential.id", ondelete="SET NULL")
    )
    result_generation: Mapped[int | None] = mapped_column(Integer)
    verify_attempts: Mapped[int] = mapped_column(Integer, default=0, server_default="0")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )
    expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    resend_after: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    operation_started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    operation_deadline_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    last_error_code: Mapped[str | None] = mapped_column(String(48))


class NikeAuthLimit(Base):
    __tablename__ = "nike_auth_limit"

    owner_user_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("app_user.id", ondelete="CASCADE"), primary_key=True
    )
    verify_window_started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    verify_attempts_in_window: Mapped[int] = mapped_column(Integer, default=0, server_default="0")
    verify_locked_until: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class UsageDaily(Base):
    __tablename__ = "usage_daily"
    __table_args__ = (UniqueConstraint("tenant_id", "day", name="uq_usage_daily_tenant_day"),)

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    tenant_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("tenant.id", ondelete="CASCADE"))
    day: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    review_requests: Mapped[int] = mapped_column(Integer, default=0, server_default="0")
    price_requests: Mapped[int] = mapped_column(Integer, default=0, server_default="0")
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )


class PlatformUsageDaily(Base):
    """Platform-wide transaction counter for the daily hard cap."""

    __tablename__ = "platform_usage_daily"
    __table_args__ = (UniqueConstraint("day", name="uq_platform_usage_daily_day"),)

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    day: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    transactions: Mapped[int] = mapped_column(Integer, default=0, server_default="0")


class PlatformUsageHourly(Base):
    """Platform-wide transaction counter for the steady-state rate cap."""

    __tablename__ = "platform_usage_hourly"
    __table_args__ = (UniqueConstraint("hour", name="uq_platform_usage_hourly_hour"),)

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    hour: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    transactions: Mapped[int] = mapped_column(Integer, default=0, server_default="0")


class NotificationChannel(Base):
    """A user's own push target; the Apprise URL is encrypted at rest."""

    __tablename__ = "notification_channel"
    __table_args__ = (
        UniqueConstraint("owner_user_id", "target_fingerprint", name="uq_notification_channel_target"),
    )

    id: Mapped[uuid.UUID] = mapped_column(PGUUID(as_uuid=True), primary_key=True, default=_uuid)
    owner_user_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("app_user.id", ondelete="CASCADE"))
    platform: Mapped[str] = mapped_column(String(32))
    name: Mapped[str] = mapped_column(String(64), default="")
    target_enc: Mapped[str] = mapped_column(Text)
    target_fingerprint: Mapped[str] = mapped_column(String(64))
    target_hint: Mapped[str] = mapped_column(String(120), default="")
    enabled: Mapped[bool] = mapped_column(Boolean, default=True)
    last_ok_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    last_error_code: Mapped[str | None] = mapped_column(String(48))
    last_error: Mapped[str | None] = mapped_column(String(300))
    last_error_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )


class NotificationDelivery(Base):
    """Outbox row: one aggregated message per (job, channel).

    System notices (e.g. a credential that truly needs re-verification) carry
    no scan job; ``job_id`` is NULL for them.
    """

    __tablename__ = "notification_delivery"
    __table_args__ = (
        # 去重按 (job_id, channel_id, task_id) 在代码里判（合并采集后一个作业可能推多条：
        # 每个任务一条，各自的开关与标题），所以这里只留普通索引供查询。
        Index("ix_notification_delivery_job_channel", "job_id", "channel_id"),
        Index("ix_notification_delivery_state_ready", "state", "next_attempt_at"),
    )

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    owner_user_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("app_user.id", ondelete="CASCADE"))
    channel_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("notification_channel.id", ondelete="SET NULL")
    )
    job_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("scan_job.id", ondelete="CASCADE"))
    task_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("monitor_task.id", ondelete="SET NULL"))
    title: Mapped[str] = mapped_column(String(200), default="")
    body: Mapped[str] = mapped_column(Text, default="")
    state: Mapped[str] = mapped_column(String(16), default=NotificationDeliveryState.PENDING.value)
    attempts: Mapped[int] = mapped_column(Integer, default=0)
    next_attempt_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    lease_until: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    last_error_code: Mapped[str | None] = mapped_column(String(48))
    last_error: Mapped[str | None] = mapped_column(String(300))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    sent_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class DingTalkRobot(Base):
    """客户自己填的钉钉企业机器人：AppKey/AppSecret（密钥加密保存）。"""

    __tablename__ = "dingtalk_robot"

    owner_user_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("app_user.id", ondelete="CASCADE"), primary_key=True
    )
    robot_code: Mapped[str] = mapped_column(String(64))
    robot_secret_enc: Mapped[str] = mapped_column(Text, default="")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )


class DingTalkBotLink(Base):
    """平台机器人绑定的会话：该会话里可以用本账号的数据，提醒也推到这里。"""

    __tablename__ = "dingtalk_bot_link"
    __table_args__ = (UniqueConstraint("conversation_id", name="uq_dingtalk_bot_link_conversation"),)

    id: Mapped[uuid.UUID] = mapped_column(PGUUID(as_uuid=True), primary_key=True, default=_uuid)
    owner_user_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("app_user.id", ondelete="CASCADE"))
    dingtalk_user_id: Mapped[str] = mapped_column(String(64), default="")
    dingtalk_corp_id: Mapped[str] = mapped_column(String(64), default="")
    conversation_id: Mapped[str] = mapped_column(String(128))
    #: 1 单聊，2 群聊（与钉钉回调一致）。
    conversation_type: Mapped[int] = mapped_column(Integer, default=2)
    conversation_title: Mapped[str] = mapped_column(String(160), default="")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )
