"""任务级尺码选择的 schema 规范化：保留大小写、去空、去重。

store/style 走 uppercase，尺码不能（"42.5"/"XL" 需原样命中上游结构 label）。
"""

from __future__ import annotations

from app.schemas import TaskIn, TaskUpdate


def test_task_in_size_labels_preserve_case_and_dedup() -> None:
    task = TaskIn(
        name="t",
        style_codes=["DN3707-202"],
        size_labels=[" 42.5 ", "XL", "42.5", "", "  "],
    )
    assert task.size_labels == ["42.5", "XL"]


def test_task_in_size_labels_default_is_all_sizes() -> None:
    assert TaskIn(name="t", style_codes=["DN3707-202"]).size_labels == []


def test_task_in_store_codes_still_uppercase_but_size_labels_not() -> None:
    task = TaskIn(
        name="t",
        store_codes=["s001", "s001"],
        style_codes=["dn3707-202"],
        size_labels=["xl"],
    )
    assert task.store_codes == ["S001"]
    assert task.style_codes == ["DN3707-202"]
    assert task.size_labels == ["xl"]


def test_task_update_size_labels_preserve_case_and_dedup() -> None:
    update = TaskUpdate(size_labels=[" XL ", "xl", "42.5"])
    assert update.size_labels == ["XL", "xl", "42.5"]


def test_task_update_none_means_leave_unchanged() -> None:
    update = TaskUpdate()
    assert update.store_codes is None
    assert update.size_labels is None


def test_task_update_empty_list_normalizes_to_empty() -> None:
    assert TaskUpdate(size_labels=[]).size_labels == []
    assert TaskUpdate(store_codes=["s001", "S001"]).store_codes == ["S001"]