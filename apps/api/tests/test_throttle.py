"""账号级冷却的纯计算：窗口、阶梯解析与通知文案（不连数据库）。"""

from __future__ import annotations

from datetime import UTC, datetime

from app.config import Settings
from app.services import quota, throttle


def test_budget_windows_reopen_at_the_next_boundary_plus_margin() -> None:
    now = datetime(2026, 10, 5, 9, 59, 15, tzinfo=UTC)
    assert quota.resume_at_for("platform_hourly_budget", now) == datetime(
        2026, 10, 5, 10, 0, 30, tzinfo=UTC
    )
    assert quota.resume_at_for("platform_daily_budget", now) == datetime(
        2026, 10, 6, 0, 0, 30, tzinfo=UTC
    )
    assert quota.resume_at_for("tenant_budget", now) == datetime(2026, 10, 6, 0, 0, 30, tzinfo=UTC)
    # 没有钟摆窗口的原因不给出恢复时刻（风控走阶梯，由 throttle 自己算）。
    assert quota.resume_at_for("throttled", now) is None
    assert quota.resume_at_for("", now) is None


def test_quota_guard_resume_follows_the_last_rejection() -> None:
    guard = quota.QuotaGuard("tenant", 60000, 5000)
    assert guard.resume_at() is None
    guard.last_rejection = "platform_hourly_budget"
    expected = quota.resume_at_for("platform_hourly_budget", datetime.now(UTC))
    resume = guard.resume_at()
    assert resume is not None and abs((resume - expected).total_seconds()) < 2


def test_ladder_parsing_falls_back_to_a_safe_default() -> None:
    def ladder(value: str) -> list[int]:
        return throttle._ladder(Settings(credential_key="k", throttle_backoff_seconds=value))

    assert ladder("30, 60") == [30, 60]
    assert ladder(" 120 , ,240 ") == [120, 240]
    assert ladder("abc") == [300]
    assert ladder("0,-5") == [300]
    assert ladder("") == [300]
    assert ladder("300,900,1800,3600") == [300, 900, 1800, 3600]


def test_notice_body_names_the_reason_and_the_resume_moment() -> None:
    until = datetime(2026, 10, 5, 9, 30, tzinfo=UTC)  # 北京时间 17:30
    assert (
        throttle._notice_body("platform_hourly_budget", until)
        == "原因：本小时查询额度已用完。系统将在 17:30 自动继续检查，无需操作。"
    )
    assert "被 Nike 限速" in throttle._notice_body(throttle.THROTTLED_REASON, until)
    assert "今日查询额度已用完" in throttle._notice_body("tenant_budget", until)
    assert "今日查询额度已用完" in throttle._notice_body("platform_daily_budget", until)
