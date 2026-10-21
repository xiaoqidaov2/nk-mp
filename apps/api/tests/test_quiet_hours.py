"""夜间降速窗口（北京时间）：0–4 点最慢、4–6 点线性恢复。

窗口由同一个比例驱动两个旋钮：任务间隔 ×1/factor、小时额度 ×factor。
"""

from __future__ import annotations

from datetime import datetime

from app.config import Settings
from app.services import quiet_hours


def _cst(hour: int, minute: int = 0) -> datetime:
    return datetime(2026, 10, 21, hour, minute, tzinfo=quiet_hours.CST)


def _settings(**overrides) -> Settings:
    return Settings(**overrides)


def test_outside_the_window_runs_at_full_speed() -> None:
    settings = _settings()
    for hour, minute in ((6, 0), (12, 0), (23, 59)):
        assert quiet_hours.factor(settings, _cst(hour, minute)) == 1.0
        assert quiet_hours.active(settings, _cst(hour, minute)) is False


def test_slow_window_holds_the_floor() -> None:
    settings = _settings()
    for hour, minute in ((0, 0), (2, 30), (3, 59)):
        assert quiet_hours.factor(settings, _cst(hour, minute)) == 0.25
        assert quiet_hours.active(settings, _cst(hour, minute)) is True


def test_recovery_window_ramps_linearly() -> None:
    settings = _settings()
    assert quiet_hours.factor(settings, _cst(4, 0)) == 0.25
    assert quiet_hours.factor(settings, _cst(5, 0)) == 0.625
    assert quiet_hours.factor(settings, _cst(5, 59)) < 1.0
    assert quiet_hours.factor(settings, _cst(6, 0)) == 1.0


def test_interval_is_stretched_never_shortened() -> None:
    settings = _settings()
    assert quiet_hours.interval(settings, 30, _cst(2, 0)) == 120
    assert quiet_hours.interval(settings, 600, _cst(2, 0)) == 2400
    assert quiet_hours.interval(settings, 600, _cst(5, 0)) == 960
    assert quiet_hours.interval(settings, 600, _cst(12, 0)) == 600


def test_switches_and_bounds_are_honoured() -> None:
    assert quiet_hours.factor(_settings(quiet_hours_enabled=False), _cst(2, 0)) == 1.0
    assert quiet_hours.factor(_settings(quiet_hours_slow_factor=1.0), _cst(2, 0)) == 1.0
    custom = _settings(
        quiet_hours_slow_from_hour=1,
        quiet_hours_slow_until_hour=3,
        quiet_hours_recover_until_hour=5,
        quiet_hours_slow_factor=0.5,
    )
    assert quiet_hours.factor(custom, _cst(0, 30)) == 1.0
    assert quiet_hours.factor(custom, _cst(2, 0)) == 0.5
    assert quiet_hours.factor(custom, _cst(4, 0)) == 0.75
    assert quiet_hours.factor(custom, _cst(5, 0)) == 1.0
