"""Daily review-budget accounting."""

from __future__ import annotations

from datetime import UTC, datetime

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models import Store, Tenant, UsageDaily


def _today() -> datetime:
    now = datetime.now(UTC)
    return datetime(now.year, now.month, now.day, tzinfo=UTC)


class QuotaGuard:
    def __init__(self, tenant_id, global_budget: int, tenant_budget: int) -> None:
        self.tenant_id = tenant_id
        self.global_budget = global_budget
        self.tenant_budget = tenant_budget

    def _row(self, session: Session) -> UsageDaily:
        row = session.execute(
            select(UsageDaily).where(
                UsageDaily.tenant_id == self.tenant_id, UsageDaily.day == _today()
            )
        ).scalar_one_or_none()
        if row is None:
            row = UsageDaily(tenant_id=self.tenant_id, day=_today())
            session.add(row)
            session.flush()
        return row

    def try_consume(self, session: Session, store: Store, units: int, kind: str = "availability") -> bool:
        row = self._row(session)
        if kind == "availability":
            if row.review_requests + units > self.tenant_budget:
                return False
            row.review_requests += units
        else:
            if row.price_requests + units > self.tenant_budget:
                return False
            row.price_requests += units
        row.updated_at = datetime.now(UTC)
        session.flush()
        return True

    @staticmethod
    def global_usage(session: Session) -> int:
        rows = session.execute(select(UsageDaily).where(UsageDaily.day == _today())).scalars().all()
        return sum(row.review_requests + row.price_requests for row in rows)

    @staticmethod
    def tenant_for(session: Session, tenant_id) -> Tenant | None:
        return session.execute(select(Tenant).where(Tenant.id == tenant_id)).scalar_one_or_none()
