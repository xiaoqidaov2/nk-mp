"""Transaction accounting and hard caps for upstream requests.

Every billable upstream transaction (review PUT, poll GET, retry attempt)
consumes exactly one unit. The tenant sees a single daily bucket shared by
availability and price traffic; the platform enforces a global daily cap and
an hourly steady-state cap. Enforcement is optimistic-locked in PostgreSQL:
the counters are read ``FOR UPDATE`` so concurrent workers cannot overshoot.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import TypeVar

from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.orm import Session

from app.models import PlatformUsageDaily, PlatformUsageHourly, UsageDaily

logger = logging.getLogger("nike_mp.quota")

AVAILABILITY_KIND = "availability"
PRICE_KIND = "price"

#: 额度窗口重开后的调度余量（秒）：不在窗口边界上抢跑，也给"自动继续"留出落库时间。
RESUME_MARGIN_SECONDS = 30

_M = TypeVar("_M")


def _today() -> datetime:
    now = datetime.now(UTC)
    return datetime(now.year, now.month, now.day, tzinfo=UTC)


def _hour() -> datetime:
    now = datetime.now(UTC)
    return datetime(now.year, now.month, now.day, now.hour, tzinfo=UTC)


def resume_at_for(rejection: str, now: datetime | None = None) -> datetime | None:
    """When the capacity behind a rejection reopens (next hour/day + margin).

    Returns ``None`` for reasons that have no wall-clock window (nothing to
    wait for); budget rejections always map to the next window boundary.
    """
    moment = now or datetime.now(UTC)
    if rejection == "platform_hourly_budget":
        hour = datetime(moment.year, moment.month, moment.day, moment.hour, tzinfo=UTC)
        return hour + timedelta(hours=1, seconds=RESUME_MARGIN_SECONDS)
    if rejection in ("platform_daily_budget", "tenant_budget"):
        day = datetime(moment.year, moment.month, moment.day, tzinfo=UTC)
        return day + timedelta(days=1, seconds=RESUME_MARGIN_SECONDS)
    return None


@dataclass(frozen=True)
class UsageSnapshot:
    """The three independent caps checked before each upstream request."""

    tenant_total: int
    tenant_budget: int
    platform_day_total: int
    platform_day_budget: int
    platform_hour_total: int
    platform_hour_budget: int

    def rejection(self) -> str:
        if self.tenant_total >= self.tenant_budget:
            return "tenant_budget"
        if self.platform_day_total >= self.platform_day_budget:
            return "platform_daily_budget"
        if self.platform_hour_total >= self.platform_hour_budget:
            return "platform_hourly_budget"
        return ""


class QuotaGuard:
    def __init__(
        self,
        tenant_id,
        global_budget: int,
        tenant_budget: int,
        hourly_cap: int = 900,
        hourly_slow_line: int = 600,
    ) -> None:
        self.tenant_id = tenant_id
        self.global_budget = global_budget
        self.tenant_budget = tenant_budget
        self.hourly_cap = hourly_cap
        self.hourly_slow_line = hourly_slow_line
        #: 最近一次被哪个闸拒绝（tenant_budget / platform_daily_budget /
        #: platform_hourly_budget）；job 用它决定冷却原因与恢复时刻。
        self.last_rejection: str | None = None

    def resume_at(self) -> datetime | None:
        """When the rejected cap reopens; ``None`` without a rejected cap."""
        if not self.last_rejection:
            return None
        return resume_at_for(self.last_rejection)

    # ------------------------------------------------------------- internals

    @staticmethod
    def _locked_or_create(session: Session, model: type[_M], index_elements: list[str], **keys) -> _M:
        """Fetch the counter row with a write lock, creating it if needed.

        Callers must lock in a consistent order (hour -> day -> tenant) so that
        concurrent workers cannot deadlock against each other.
        """
        stmt = select(model).filter_by(**keys).with_for_update()
        row = session.execute(stmt).scalar_one_or_none()
        if row is not None:
            return row
        session.execute(
            pg_insert(model).values(**keys).on_conflict_do_nothing(index_elements=index_elements)
        )
        row = session.execute(stmt).scalar_one_or_none()
        if row is None:  # pragma: no cover - only reachable if the row vanished mid-lock
            raise RuntimeError(f"failed to lock or create {model.__tablename__} row")
        return row

    def _snapshot(
        self, session: Session
    ) -> tuple[UsageSnapshot, PlatformUsageHourly, PlatformUsageDaily, UsageDaily]:
        hour_row = self._locked_or_create(
            session, PlatformUsageHourly, ["hour"], hour=_hour()
        )
        day_row = self._locked_or_create(
            session, PlatformUsageDaily, ["day"], day=_today()
        )
        tenant_row = self._locked_or_create(
            session,
            UsageDaily,
            ["tenant_id", "day"],
            tenant_id=self.tenant_id,
            day=_today(),
        )
        tenant_total = int(tenant_row.review_requests) + int(tenant_row.price_requests)
        snapshot = UsageSnapshot(
            tenant_total=tenant_total,
            tenant_budget=self.tenant_budget,
            platform_day_total=int(day_row.transactions),
            platform_day_budget=self.global_budget,
            platform_hour_total=int(hour_row.transactions),
            platform_hour_budget=self.hourly_cap,
        )
        return snapshot, hour_row, day_row, tenant_row

    # ---------------------------------------------------------------- public

    def consume_transaction(
        self, session: Session, kind: str = AVAILABILITY_KIND, charge: bool = True
    ) -> bool:
        """Check the caps (and, unless ``charge`` is false, book one transaction).

        Returns False when any cap is hit — the caller must not issue the
        request. ``charge=False`` is the pre-flight check: it never counts, so
        a request that cannot afford the budget is not even sent; the caller
        books it later with ``charge=True`` once upstream answered.

        The counters are committed immediately: the row locks are released
        right away (so concurrent workers are not serialised for the duration
        of a slow store probe) and the accounting survives a job rollback.
        """
        snapshot, hour_row, day_row, tenant_row = self._snapshot(session)
        rejection = snapshot.rejection()
        if rejection:
            self.last_rejection = rejection
            # Release the row locks promptly; no accounting change is kept.
            session.commit()
            logger.warning("quota rejected upstream request (%s)", rejection)
            return False
        if not charge:
            session.commit()
            return True
        if kind == PRICE_KIND:
            tenant_row.price_requests = int(tenant_row.price_requests) + 1
        else:
            tenant_row.review_requests = int(tenant_row.review_requests) + 1
        hour_row.transactions = int(hour_row.transactions) + 1
        day_row.transactions = int(day_row.transactions) + 1
        session.commit()
        if self.hourly_slow_line and hour_row.transactions == self.hourly_slow_line:
            logger.warning(
                "hourly transaction volume reached the slow-down observation line (%s)",
                self.hourly_slow_line,
            )
        return True
