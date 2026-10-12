"""T03: job checkpoints, lease ownership and restart recovery.

Uses a real PostgreSQL session and a scripted upstream client; no HTTP. Jobs
are personal now: the owner and credential binding are frozen at enqueue time
and every persisted artifact carries that owner.
"""

from __future__ import annotations

import time
from datetime import UTC, datetime, timedelta
from uuid import uuid4

import pytest
from sqlalchemy import select, update

from app.config import get_settings
from app.db import SessionLocal
from app.models import (
    Credential,
    JobStatus,
    MonitorTask,
    Observation,
    ObservationLatest,
    ScanJob,
    ScanJobStore,
    Store,
    StoreJobStatus,
)
from app.nike.errors import ErrorCode, ScanError
from app.nike.types import PriceInfo, ReviewBatch, ReviewItem, SizeEntry, SizeStructure
from app.services import jobs as jobs_module
from app.services.credentials import CredentialService
from app.services.jobs import enqueue_job, lease_next_job, requeue_failed_stores, run_job
from app.services.quota import resume_at_for

from .helpers import make_store, make_style, make_tenant, make_user, store_credential


class FakeNikeClient:
    """Scripted upstream client; behaviour keyed by region id."""

    behaviours: dict[str, str] = {}
    calls: list[tuple[str, bool]] = []
    #: 结构获取阶段直接抛错（模拟 403 出现在任何 review 请求之前）。
    structure_error: bool = False
    #: 并发测试：每次 review 的默认模拟耗时、按 region 的覆盖值，以及调用窗口。
    review_delay: float = 0.0
    delays: dict[str, float] = {}
    windows: list[tuple[str, float, float]] = []

    def __init__(
        self,
        settings,
        access_token,
        token_provider=None,
        credential_guard=None,
    ):  # noqa: ANN001
        self.settings = settings
        self.access_token = access_token
        self.token_provider = token_provider
        self.credential_guard = credential_guard
        self.transaction_listener = None

    def fetch_structure(self, store_code: str, style_code: str) -> SizeStructure:
        if type(self).structure_error:
            raise ScanError(ErrorCode.THROTTLED, "simulated structure rate limit")
        return SizeStructure(
            store_code, style_code, "OK", "测试标题", (SizeEntry("42", f"G-{store_code}"),)
        )

    def fetch_structures(  # noqa: ANN001 - 生产走批量端点，假客户端同样按"逐款"忠实返回
        self, store_code: str, style_codes: list[str]
    ) -> dict[str, SizeStructure]:
        return {code: self.fetch_structure(store_code, code) for code in style_codes}

    def review(self, gtins, region_id, with_price=False):  # noqa: ANN001
        start = time.monotonic()
        try:
            return self._review(gtins, region_id, with_price)
        finally:
            type(self).windows.append((region_id, start, time.monotonic()))

    def _review(self, gtins, region_id, with_price=False):  # noqa: ANN001
        type(self).calls.append((region_id, with_price))
        delay = type(self).delays.get(region_id, type(self).review_delay)
        if delay:
            time.sleep(delay)
        behaviour = self.behaviours.get(region_id, "ok")
        if behaviour == "crash":
            raise KeyboardInterrupt("simulated process abort")
        if behaviour == "takeover":
            # Another worker steals the lease while this one is probing.
            with SessionLocal() as db:
                job = db.execute(select(ScanJob)).scalars().first()
                job.leased_by = "workerB"
                job.lease_expires_at = datetime.now(UTC) + timedelta(minutes=5)
                db.commit()
            behaviour = "ok"
        if behaviour == "untrusted":
            return ReviewBatch(
                items={gtin: ReviewItem(gtin=gtin, availability="UNKNOWN") for gtin in gtins},
                trusted=False,
                error_code="EMPTY_COMPLETED",
                message="simulated empty completion",
            )
        if behaviour == "throttled":
            raise ScanError(ErrorCode.THROTTLED, "simulated upstream rate limit")
        if behaviour == "budget":
            raise ScanError(ErrorCode.BUDGET_EXHAUSTED, "simulated quota rejection")
        if behaviour == "auth":
            raise ScanError(ErrorCode.AUTH_REQUIRED, "simulated auth expiry")
        if behaviour == "waf":
            raise ScanError(ErrorCode.WAF_CHALLENGE, "simulated 405 storm")
        if behaviour == "priced_availability":
            # 小批量可用性响应本身就带价格（真实上游在条目少时也会这样）。
            price = PriceInfo(total_price=99.0, price=99.0)
            return ReviewBatch(
                items={
                    gtin: ReviewItem(gtin=gtin, availability="IN_STOCK", price=price)
                    for gtin in gtins
                },
                trusted=True,
            )
        price = PriceInfo(total_price=99.0, price=99.0) if with_price else None
        return ReviewBatch(
            items={
                gtin: ReviewItem(gtin=gtin, availability="IN_STOCK", price=price)
                for gtin in gtins
            },
            trusted=True,
        )

    def close(self) -> None:
        return None


@pytest.fixture
def seeded():
    settings = get_settings()
    with SessionLocal() as db:
        tenant = make_tenant(db, budget=100000)
        user = make_user(db, tenant, email="job-owner@example.com")
        make_store(db, owner=user, code="S1", name="一店", region_id="R1")
        make_store(db, owner=user, code="S2", name="二店", region_id="R2")
        make_style(db, owner=user, code="DN3707-202")
        db.commit()
        seed = {"tenant_id": tenant.id, "user_id": user.id}
    # Fake credential so the job's frozen binding is usable without HTTP.
    seed["binding"] = store_credential(settings, seed["user_id"])
    return seed


def _enqueue(seeded) -> str:  # noqa: ANN001
    settings = get_settings()
    with SessionLocal() as db:
        binding = CredentialService(settings, seeded["user_id"]).binding()
        job = enqueue_job(
            db,
            tenant_id=seeded["tenant_id"],
            store_codes=["S1", "S2"],
            style_codes=["DN3707-202"],
            owner_user_id=seeded["user_id"],
            credential_binding=binding,
        )
        db.commit()
        return job.id


def _lease(job_id, worker_id: str = "workerA") -> str:
    settings = get_settings()
    with SessionLocal() as db:
        leased = lease_next_job(db, settings, worker_id)
        assert leased is not None and leased.id == job_id
        db.commit()
    return job_id


def _run(job_id, worker_id: str = "workerA") -> None:
    settings = get_settings()
    with SessionLocal() as db:
        job = db.get(ScanJob, job_id)
        assert job is not None
        run_job(db, settings, job, worker_id)
        # The worker main loop commits after a successful run.
        db.commit()


def _store_id(code: str):
    with SessionLocal() as db:
        return db.execute(select(Store.id).where(Store.store_code == code)).scalar_one()


def _rows(job_id) -> dict:
    with SessionLocal() as db:
        return {
            row.store_id: row
            for row in db.execute(
                select(ScanJobStore).where(ScanJobStore.job_id == job_id)
            ).scalars()
        }


def test_completed_store_is_committed_before_a_crash(seeded, monkeypatch) -> None:
    """Regression: per-unit checkpoints must survive a process abort."""
    monkeypatch.setattr(jobs_module, "NikeClient", FakeNikeClient)
    FakeNikeClient.calls = []
    FakeNikeClient.behaviours = {"R1": "ok", "R2": "crash"}

    job_id = _lease(_enqueue(seeded))
    with pytest.raises(KeyboardInterrupt):
        _run(job_id)

    store1, store2 = _store_id("S1"), _store_id("S2")
    rows = _rows(job_id)
    assert rows[store1].status == StoreJobStatus.SUCCEEDED.value
    assert rows[store2].status == StoreJobStatus.PENDING.value

    with SessionLocal() as db:
        observations = db.execute(select(Observation)).scalars().all()
        assert len(observations) == 1
        assert observations[0].availability == "IN_STOCK"
        assert observations[0].job_id is not None
        assert observations[0].owner_user_id == seeded["user_id"]
        assert observations[0].credential_id == seeded["binding"].credential_id
        latest = db.execute(select(ObservationLatest)).scalars().all()
        assert len(latest) == 1
        assert latest[0].owner_user_id == seeded["user_id"]
    with SessionLocal() as db:
        job = db.get(ScanJob, job_id)
        assert job.status == JobStatus.RUNNING.value
        assert job.finished_at is None


def test_stolen_lease_stops_the_old_worker(seeded, monkeypatch) -> None:
    """Regression: a worker that lost its lease must not write results."""
    monkeypatch.setattr(jobs_module, "NikeClient", FakeNikeClient)
    FakeNikeClient.calls = []
    FakeNikeClient.behaviours = {"R1": "takeover", "R2": "ok"}

    job_id = _lease(_enqueue(seeded))
    _run(job_id)

    store1, store2 = _store_id("S1"), _store_id("S2")
    rows = _rows(job_id)
    assert rows[store1].status == StoreJobStatus.PENDING.value
    assert rows[store2].status == StoreJobStatus.PENDING.value
    with SessionLocal() as db:
        job = db.get(ScanJob, job_id)
        assert job.leased_by == "workerB"
        assert job.status == JobStatus.RUNNING.value
        assert job.finished_at is None
        assert db.execute(select(Observation)).scalars().all() == []


def test_only_one_worker_can_hold_the_lease(seeded) -> None:
    settings = get_settings()
    job_id = _enqueue(seeded)

    db1, db2 = SessionLocal(), SessionLocal()
    try:
        first = lease_next_job(db1, settings, "workerA")
        assert first is not None and first.id == job_id
        # Not yet committed: the row lock is held, and a committed fresh lease
        # must not be stealable either.
        assert lease_next_job(db2, settings, "workerB") is None
        db1.commit()
        assert lease_next_job(db2, settings, "workerB") is None

        with SessionLocal() as db:
            stale = db.get(ScanJob, job_id)
            stale.lease_expires_at = datetime.now(UTC) - timedelta(seconds=1)
            db.commit()

        second = lease_next_job(db2, settings, "workerB")
        assert second is not None and second.id == job_id
        assert second.leased_by == "workerB"
        assert second.attempt == 2
        db2.commit()
    finally:
        db1.close()
        db2.close()


def test_retry_requeues_only_incomplete_stores(seeded, monkeypatch) -> None:
    monkeypatch.setattr(jobs_module, "NikeClient", FakeNikeClient)
    FakeNikeClient.calls = []
    FakeNikeClient.behaviours = {"R2": "untrusted"}

    job_id = _lease(_enqueue(seeded))
    _run(job_id)

    store1, store2 = _store_id("S1"), _store_id("S2")
    rows = _rows(job_id)
    assert rows[store1].status == StoreJobStatus.SUCCEEDED.value
    assert rows[store2].status == StoreJobStatus.UNKNOWN.value

    with SessionLocal() as db:
        job = db.get(ScanJob, job_id)
        assert job.status == JobStatus.PARTIAL.value
        assert requeue_failed_stores(db, job) == 1
        db.commit()

    FakeNikeClient.behaviours = {"R2": "ok"}
    r1_calls = sum(1 for region, _ in FakeNikeClient.calls if region == "R1")
    _lease(job_id)
    _run(job_id)

    rows = _rows(job_id)
    assert rows[store2].status == StoreJobStatus.SUCCEEDED.value
    assert (
        sum(1 for region, _ in FakeNikeClient.calls if region == "R1") == r1_calls
    ), "the already-succeeded store must not be probed again"
    with SessionLocal() as db:
        job = db.get(ScanJob, job_id)
        assert job.status == JobStatus.SUCCEEDED.value
        assert job.finished_at is not None
        # The frozen owner never changes across retries.
        assert job.owner_user_id == seeded["user_id"]
        assert job.credential_id == seeded["binding"].credential_id


def _enqueue_with_task(
    seeded,
    *,
    retry_round: int = 0,
    cadence: int = 1800,
    store_codes: list[str] | None = None,
    style_codes: list[str] | None = None,
) -> tuple:
    """Queue a job that belongs to an active task (auto-retry needs one).

    Returns the raw UUIDs, matching the other helpers in this module.
    """
    settings = get_settings()
    codes = list(store_codes or ["S1", "S2"])
    styles = list(style_codes or ["DN3707-202"])
    with SessionLocal() as db:
        task = MonitorTask(
            tenant_id=seeded["tenant_id"],
            name=f"补跑任务-{uuid4().hex[:8]}",
            store_codes=codes,
            style_codes=styles,
            cadence_seconds=cadence,
            next_run_at=datetime.now(UTC) + timedelta(hours=1),
            created_by=seeded["user_id"],
        )
        db.add(task)
        db.flush()
        binding = CredentialService(settings, seeded["user_id"]).binding()
        job = enqueue_job(
            db,
            tenant_id=seeded["tenant_id"],
            store_codes=codes,
            style_codes=styles,
            task_id=task.id,
            kind="MONITOR",
            owner_user_id=seeded["user_id"],
            credential_binding=binding,
        )
        job.retry_round = retry_round
        job.not_before = datetime.now(UTC) - timedelta(seconds=1)
        db.commit()
        return job.id, task.id


def _cooldown(owner_id) -> dict:
    with SessionLocal() as db:
        record = db.execute(
            select(Credential).where(Credential.owner_user_id == owner_id)
        ).scalar_one()
        return {
            "until": record.cooldown_until,
            "level": record.cooldown_level,
            "reason": record.cooldown_reason,
        }


def _expire_cooldown(owner_id) -> None:
    with SessionLocal() as db:
        db.execute(
            update(Credential)
            .where(Credential.owner_user_id == owner_id)
            .values(cooldown_until=datetime.now(UTC) - timedelta(seconds=1))
        )
        db.commit()


def test_throttled_round_aborts_early_and_schedules_the_retry(seeded, monkeypatch) -> None:
    """THROTTLED：不再逐店空转，未完成门店标 FAILED，账号冷却并排队补跑。"""
    settings = get_settings()
    monkeypatch.setattr(jobs_module, "NikeClient", FakeNikeClient)
    FakeNikeClient.calls = []
    FakeNikeClient.behaviours = {"R1": "throttled", "R2": "ok"}

    job_id, task_id = _enqueue_with_task(seeded)
    _lease(job_id)
    _run(job_id)

    rows = _rows(job_id)
    assert rows[_store_id("S1")].status == StoreJobStatus.FAILED.value
    assert rows[_store_id("S1")].error_code == ErrorCode.THROTTLED.value
    assert rows[_store_id("S2")].status == StoreJobStatus.FAILED.value
    assert rows[_store_id("S2")].error_code == ErrorCode.THROTTLED.value
    # 第二个门店根本没发请求（否则就是被限流后继续逐店空转）。
    assert FakeNikeClient.calls == [("R1", False)]

    with SessionLocal() as db:
        job = db.get(ScanJob, job_id)
        assert job.status == JobStatus.FAILED.value
        assert job.error_summary == ErrorCode.THROTTLED.value
        retry = db.execute(select(ScanJob).where(ScanJob.kind == "RETRY")).scalar_one()
        assert retry.task_id == task_id
        assert retry.retry_round == 1
        assert retry.status == JobStatus.QUEUED.value
        assert sorted(retry.store_codes) == ["S1", "S2"]
        assert retry.owner_user_id == seeded["user_id"]

    cooldown = _cooldown(seeded["user_id"])
    assert cooldown["reason"] == "throttled"
    assert cooldown["level"] == 1
    assert cooldown["until"] is not None
    assert 290 <= (cooldown["until"] - datetime.now(UTC)).total_seconds() <= 305
    with SessionLocal() as db:
        retry = db.execute(select(ScanJob).where(ScanJob.kind == "RETRY")).scalar_one()
        # 补跑不早于冷却窗口；not_before 未到时连租约都拿不到。
        assert retry.not_before is not None
        assert retry.not_before >= cooldown["until"] - timedelta(seconds=1)
        assert lease_next_job(db, settings, "workerA") is None


def test_budget_exhaustion_waits_for_the_next_hour(seeded, monkeypatch) -> None:
    """BUDGET_EXHAUSTED：按小时窗口冷却，不升级风控阶梯。"""
    monkeypatch.setattr(jobs_module, "NikeClient", FakeNikeClient)
    FakeNikeClient.calls = []
    FakeNikeClient.behaviours = {"R1": "budget", "R2": "ok"}

    job_id, _task_id = _enqueue_with_task(seeded)
    _lease(job_id)
    _run(job_id)

    rows = _rows(job_id)
    assert {row.error_code for row in rows.values()} == {ErrorCode.BUDGET_EXHAUSTED.value}
    cooldown = _cooldown(seeded["user_id"])
    assert cooldown["reason"] == "platform_hourly_budget"
    assert cooldown["level"] == 0
    expected = resume_at_for("platform_hourly_budget", datetime.now(UTC))
    assert cooldown["until"] is not None and expected is not None
    assert abs((cooldown["until"] - expected).total_seconds()) <= 2


def test_retry_job_runs_after_the_window_and_converges(seeded, monkeypatch) -> None:
    """窗口过后补跑先跑并成功；整轮成功重置阶梯、清掉已过期窗口。"""
    monkeypatch.setattr(jobs_module, "NikeClient", FakeNikeClient)
    FakeNikeClient.calls = []
    FakeNikeClient.behaviours = {"R1": "throttled", "R2": "ok"}

    job_id, task_id = _enqueue_with_task(seeded)
    _lease(job_id)
    _run(job_id)
    with SessionLocal() as db:
        retry_id = db.execute(select(ScanJob).where(ScanJob.kind == "RETRY")).scalar_one().id

    # 模拟窗口到点：not_before 与 cooldown_until 都已成为过去。
    with SessionLocal() as db:
        retry = db.get(ScanJob, retry_id)
        retry.not_before = datetime.now(UTC) - timedelta(seconds=1)
        db.commit()
    _expire_cooldown(seeded["user_id"])

    FakeNikeClient.behaviours = {}
    _lease(retry_id)
    _run(retry_id)

    with SessionLocal() as db:
        retry = db.get(ScanJob, retry_id)
        assert retry.status == JobStatus.SUCCEEDED.value
        assert retry.done_stores == 2
        task = db.get(MonitorTask, task_id)
        assert task.last_run_at is not None
        assert task.next_run_at is not None and task.next_run_at > datetime.now(UTC)
        # 不再产生第三轮补跑。
        assert db.execute(select(ScanJob).where(ScanJob.kind == "RETRY")).scalars().all() == [
            retry
        ]
    cooldown = _cooldown(seeded["user_id"])
    assert cooldown["level"] == 0
    assert cooldown["until"] is None
    assert cooldown["reason"] is None


def test_credential_failures_never_schedule_a_retry(seeded, monkeypatch) -> None:
    """凭证类失败（NIKE_*）不自动补跑：先让用户重新验证，也不进冷却。"""
    monkeypatch.setattr(jobs_module, "NikeClient", FakeNikeClient)
    FakeNikeClient.calls = []
    FakeNikeClient.behaviours = {"R1": "auth", "R2": "ok"}

    job_id, _task_id = _enqueue_with_task(seeded)
    _lease(job_id)
    _run(job_id)

    rows = _rows(job_id)
    assert rows[_store_id("S1")].error_code == "NIKE_CREDENTIAL_REQUIRED"
    with SessionLocal() as db:
        job = db.get(ScanJob, job_id)
        assert job.status == JobStatus.FAILED.value
        assert db.execute(select(ScanJob).where(ScanJob.kind == "RETRY")).scalars().all() == []
    assert _cooldown(seeded["user_id"])["until"] is None


def test_retry_round_cap_stops_scheduling(seeded, monkeypatch) -> None:
    """补跑轮数到达 job_max_attempts 后不再排新作业。"""
    settings = get_settings()
    monkeypatch.setattr(jobs_module, "NikeClient", FakeNikeClient)
    FakeNikeClient.calls = []
    FakeNikeClient.behaviours = {"R1": "throttled", "R2": "ok"}

    job_id, _task_id = _enqueue_with_task(seeded, retry_round=settings.job_max_attempts)
    _lease(job_id)
    _run(job_id)

    with SessionLocal() as db:
        assert db.execute(select(ScanJob).where(ScanJob.kind == "RETRY")).scalars().all() == []


def test_no_retry_while_another_round_is_still_queued(seeded, monkeypatch) -> None:
    """该任务还有排队作业时不补跑（防重叠），由下一轮自然覆盖。"""
    settings = get_settings()
    monkeypatch.setattr(jobs_module, "NikeClient", FakeNikeClient)
    FakeNikeClient.calls = []
    FakeNikeClient.behaviours = {"R1": "throttled", "R2": "ok"}

    job_id, task_id = _enqueue_with_task(seeded)
    with SessionLocal() as db:
        binding = CredentialService(settings, seeded["user_id"]).binding()
        sibling = enqueue_job(
            db,
            tenant_id=seeded["tenant_id"],
            store_codes=["S1"],
            style_codes=["DN3707-202"],
            task_id=task_id,
            kind="MONITOR",
            owner_user_id=seeded["user_id"],
            credential_binding=binding,
        )
        # 让兄弟作业排队但不可被租约，保证测试里先跑的是已知的那个。
        sibling.not_before = datetime.now(UTC) + timedelta(hours=2)
        db.commit()

    _lease(job_id)
    _run(job_id)

    with SessionLocal() as db:
        assert db.execute(select(ScanJob).where(ScanJob.kind == "RETRY")).scalars().all() == []


def test_structure_stage_throttling_also_aborts_the_round(seeded, monkeypatch) -> None:
    """结构获取阶段被限速（403）：整轮结束并进入冷却，不落到门店级 UNKNOWN。"""
    monkeypatch.setattr(jobs_module, "NikeClient", FakeNikeClient)
    monkeypatch.setattr(FakeNikeClient, "structure_error", True)
    FakeNikeClient.calls = []
    FakeNikeClient.behaviours = {}

    job_id, _task_id = _enqueue_with_task(seeded)
    _lease(job_id)
    _run(job_id)

    rows = _rows(job_id)
    assert {row.error_code for row in rows.values()} == {ErrorCode.THROTTLED.value}
    assert FakeNikeClient.calls == []  # 一次 review 都没发出去
    cooldown = _cooldown(seeded["user_id"])
    assert cooldown["reason"] == "throttled"
    assert cooldown["level"] == 1
    with SessionLocal() as db:
        job = db.get(ScanJob, job_id)
        assert job.status == JobStatus.FAILED.value


def test_prices_from_the_availability_batch_skip_the_price_phase(seeded, monkeypatch) -> None:
    """可用性批次已带回价格时不再重复发价格请求（每次都要 PUT + 轮询）。"""
    monkeypatch.setattr(jobs_module, "NikeClient", FakeNikeClient)
    FakeNikeClient.calls = []
    FakeNikeClient.behaviours = {"R1": "priced_availability", "R2": "priced_availability"}

    job_id, _task_id = _enqueue_with_task(seeded)
    _lease(job_id)
    _run(job_id)

    assert [call for call in FakeNikeClient.calls if call[1]] == []
    with SessionLocal() as db:
        rows = db.execute(select(ScanJobStore).where(ScanJobStore.job_id == job_id)).scalars().all()
        assert {row.status for row in rows} == {StoreJobStatus.SUCCEEDED.value}
        observations = db.execute(
            select(Observation).where(Observation.job_id == job_id)
        ).scalars().all()
        assert observations
        assert {row.price_status for row in observations} == {"OK"}
        assert {float(row.total_price) for row in observations} == {99.0}


def _seed_many_stores(count: int) -> dict:
    """一次检查覆盖 count 家门店的种子数据（并发测试用）。"""
    settings = get_settings()
    with SessionLocal() as db:
        tenant = make_tenant(db, budget=100000)
        user = make_user(db, tenant)
        for index in range(count):
            make_store(
                db,
                owner=user,
                code=f"S{index + 1}",
                name=f"{index + 1}店",
                region_id=f"R{index + 1}",
            )
        make_style(db, owner=user, code="DN3707-202")
        db.commit()
        seed = {"tenant_id": tenant.id, "user_id": user.id}
    seed["binding"] = store_credential(settings, seed["user_id"])
    return seed


def _max_overlap(windows: list[tuple[str, float, float]]) -> int:
    events: list[tuple[float, int]] = []
    for _region, start, end in windows:
        events.append((start, 1))
        events.append((end, -1))
    events.sort()
    current = best = 0
    for _moment, delta in events:
        current += delta
        best = max(best, current)
    return best


def test_parallel_stores_overlap_and_every_checkpoint_commits(monkeypatch) -> None:
    """门店级并发：多店同时在跑，且每店的检查点独立落库。"""
    settings = get_settings()
    monkeypatch.setattr(settings, "store_concurrency", 4)
    seeded = _seed_many_stores(4)
    monkeypatch.setattr(jobs_module, "NikeClient", FakeNikeClient)
    FakeNikeClient.calls = []
    FakeNikeClient.windows = []
    FakeNikeClient.delays = {}
    FakeNikeClient.review_delay = 0.3
    FakeNikeClient.behaviours = {}

    job_id, _task_id = _enqueue_with_task(
        seeded, store_codes=["S1", "S2", "S3", "S4"]
    )
    _lease(job_id)
    _run(job_id)

    with SessionLocal() as db:
        job = db.get(ScanJob, job_id)
        assert job.status == JobStatus.SUCCEEDED.value
        rows = db.execute(select(ScanJobStore).where(ScanJobStore.job_id == job_id)).scalars().all()
        assert {row.status for row in rows} == {StoreJobStatus.SUCCEEDED.value}
        observations = db.execute(
            select(Observation).where(Observation.job_id == job_id)
        ).scalars().all()
        assert len(observations) == 4
    # 4 家门店的 review 窗口确实重叠（顺序执行时不可能出现）。
    assert _max_overlap(FakeNikeClient.windows) >= 2


def test_parallel_abort_keeps_succeeded_stores_and_retries_only_the_failed_one(
    monkeypatch,
) -> None:
    """并发下命中限速：已成功的门店保持 SUCCEEDED，其余 FAILED 并只补跑失败门店。"""
    settings = get_settings()
    monkeypatch.setattr(settings, "store_concurrency", 4)
    seeded = _seed_many_stores(4)
    monkeypatch.setattr(jobs_module, "NikeClient", FakeNikeClient)
    FakeNikeClient.calls = []
    FakeNikeClient.windows = []
    # S4 先慢后撞限速：其余三家早已成功提交，收尾时不能被误标 FAILED。
    FakeNikeClient.review_delay = 0.0
    FakeNikeClient.delays = {"R4": 0.3}
    FakeNikeClient.behaviours = {"R4": "throttled"}

    job_id, _task_id = _enqueue_with_task(seeded, store_codes=["S1", "S2", "S3", "S4"])
    _lease(job_id)
    _run(job_id)

    with SessionLocal() as db:
        job = db.get(ScanJob, job_id)
        assert job.status == JobStatus.FAILED.value
        assert job.error_summary == ErrorCode.THROTTLED.value
        rows = {
            row.store_id: row
            for row in db.execute(
                select(ScanJobStore).where(ScanJobStore.job_id == job_id)
            ).scalars()
        }
        for code in ("S1", "S2", "S3"):
            assert rows[_store_id(code)].status == StoreJobStatus.SUCCEEDED.value, code
        assert rows[_store_id("S4")].status == StoreJobStatus.FAILED.value
        assert rows[_store_id("S4")].error_code == ErrorCode.THROTTLED.value
        retry = db.execute(select(ScanJob).where(ScanJob.kind == "RETRY")).scalar_one()
        assert retry.store_codes == ["S4"]
        assert retry.retry_round == 1
    cooldown = _cooldown(seeded["user_id"])
    assert cooldown["reason"] == "throttled"
    assert cooldown["level"] == 1


def test_waf_challenge_stays_a_per_store_failure_when_the_breaker_is_off(
    seeded, monkeypatch
) -> None:
    """默认（熔断关闭）：一店被 405 打满只影响该店，不触发冷却。"""
    monkeypatch.setattr(jobs_module, "NikeClient", FakeNikeClient)
    FakeNikeClient.calls = []
    FakeNikeClient.delays = {}
    FakeNikeClient.review_delay = 0.0
    FakeNikeClient.behaviours = {"R1": "waf"}

    job_id, _task_id = _enqueue_with_task(seeded)
    _lease(job_id)
    _run(job_id)

    rows = _rows(job_id)
    assert rows[_store_id("S1")].status == StoreJobStatus.UNKNOWN.value
    assert rows[_store_id("S1")].error_code == ErrorCode.WAF_CHALLENGE.value
    assert rows[_store_id("S2")].status == StoreJobStatus.SUCCEEDED.value
    with SessionLocal() as db:
        job = db.get(ScanJob, job_id)
        assert job.status == JobStatus.PARTIAL.value
    assert _cooldown(seeded["user_id"])["until"] is None


def test_waf_storm_breaker_aborts_the_round_and_cools_down(seeded, monkeypatch) -> None:
    """405 挑战风暴熔断：达到阈值即整轮结束、账号冷却，剩余门店交给补跑。"""
    settings = get_settings()
    monkeypatch.setattr(settings, "waf_storm_store_limit", 1)
    monkeypatch.setattr(jobs_module, "NikeClient", FakeNikeClient)
    FakeNikeClient.calls = []
    FakeNikeClient.delays = {}
    FakeNikeClient.review_delay = 0.0
    FakeNikeClient.behaviours = {"R1": "waf"}

    job_id, _task_id = _enqueue_with_task(seeded)
    _lease(job_id)
    _run(job_id)

    rows = _rows(job_id)
    assert rows[_store_id("S1")].error_code == ErrorCode.WAF_CHALLENGE.value
    # 没轮到的那家店也被统一标失败，并交给补跑。
    assert rows[_store_id("S2")].status == StoreJobStatus.FAILED.value
    with SessionLocal() as db:
        job = db.get(ScanJob, job_id)
        assert job.status == JobStatus.FAILED.value
        assert job.error_summary == ErrorCode.WAF_CHALLENGE.value
        retry = db.execute(select(ScanJob).where(ScanJob.kind == "RETRY")).scalar_one()
        assert sorted(retry.store_codes) == ["S1", "S2"]
        assert retry.retry_round == 1
        assert retry.not_before is not None
    cooldown = _cooldown(seeded["user_id"])
    assert cooldown["reason"] == "throttled"
    assert cooldown["level"] == 1
    assert cooldown["until"] is not None
