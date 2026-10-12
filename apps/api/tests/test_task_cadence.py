"""频率字段边界：最快 30 秒（API 层由 pydantic 校验映射为 422）。"""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from app.schemas import TaskIn, TaskUpdate


def test_task_cadence_accepts_the_30_second_floor() -> None:
    assert TaskIn(name="t", style_codes=["DN3707-202"], cadence_seconds=30).cadence_seconds == 30
    assert TaskUpdate(cadence_seconds=30).cadence_seconds == 30


@pytest.mark.parametrize("value", [29, 0, -1, 86401])
def test_task_cadence_rejects_out_of_range_values(value: int) -> None:
    with pytest.raises(ValidationError):
        TaskIn(name="t", style_codes=["DN3707-202"], cadence_seconds=value)
    with pytest.raises(ValidationError):
        TaskUpdate(cadence_seconds=value)


def test_task_cadence_default_and_ceiling() -> None:
    assert TaskIn(name="t", style_codes=["DN3707-202"]).cadence_seconds == 1800
    assert TaskIn(name="t", style_codes=["DN3707-202"], cadence_seconds=86400).cadence_seconds == 86400
    assert TaskUpdate().cadence_seconds is None
