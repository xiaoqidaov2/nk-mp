"""Job finalisation must reflect partial and unknown outcomes honestly."""

from __future__ import annotations

from datetime import UTC, datetime

from app.models import JobStatus, ScanJob, StoreJobStatus
from app.services.jobs import _finalize


class _FakeRows:
    def __init__(self, rows: list[object]) -> None:
        self._rows = rows

    def scalars(self) -> _FakeRows:
        return self

    def all(self) -> list[object]:
        return self._rows


class _FakeSession:
    def __init__(self, rows: list[object]) -> None:
        self._rows = rows
        self.flushes = 0

    def execute(self, _stmt):  # noqa: ANN001 - duck-typed
        return _FakeRows(self._rows)

    def flush(self) -> None:
        self.flushes += 1

    def expire_all(self) -> None:
        """并发收尾前会丢弃 identity map 缓存；测试替身无需做任何事。"""

    def commit(self) -> None:
        """`_finalize` 本身不提交；调用方负责，替身记录即可。"""


class _Row:
    def __init__(self, status: str) -> None:
        self.status = status


def _job() -> ScanJob:
    return ScanJob(
        id=None,
        tenant_id=None,
        status=JobStatus.RUNNING.value,
        total_stores=0,
        done_stores=0,
        failed_stores=0,
        unknown_stores=0,
        attempt=1,
        error_summary="",
        created_at=datetime.now(UTC),
    )


def _finalize_with(rows: list[_Row]) -> ScanJob:
    job = _job()
    _finalize(_FakeSession(rows), None, job, None, "")  # type: ignore[arg-type]
    return job


def test_all_succeeded_is_success() -> None:
    job = _finalize_with([_Row(StoreJobStatus.SUCCEEDED.value)] * 3)
    assert job.status == JobStatus.SUCCEEDED.value
    assert job.done_stores == 3


def test_mixed_outcome_is_partial_not_success() -> None:
    job = _finalize_with(
        [
            _Row(StoreJobStatus.SUCCEEDED.value),
            _Row(StoreJobStatus.FAILED.value),
        ]
    )
    assert job.status == JobStatus.PARTIAL.value
    assert job.failed_stores == 1


def test_unknown_stores_are_counted_and_prevent_success() -> None:
    job = _finalize_with(
        [
            _Row(StoreJobStatus.SUCCEEDED.value),
            _Row(StoreJobStatus.UNKNOWN.value),
        ]
    )
    assert job.status == JobStatus.PARTIAL.value
    assert job.unknown_stores == 1


def test_all_failed_is_failure() -> None:
    job = _finalize_with([_Row(StoreJobStatus.FAILED.value)] * 2)
    assert job.status == JobStatus.FAILED.value
    assert job.done_stores == 0


def test_forced_status_wins() -> None:
    job = _job()
    _finalize(
        _FakeSession([_Row(StoreJobStatus.SUCCEEDED.value)]),  # type: ignore[arg-type]
        None,  # type: ignore[arg-type]
        job,
        JobStatus.FAILED.value,
        "credential missing",
    )
    assert job.status == JobStatus.FAILED.value
    assert job.error_summary == "credential missing"
