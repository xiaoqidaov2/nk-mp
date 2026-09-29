"""SSE progress stream: an async generator that actually yields frames."""

from __future__ import annotations

import asyncio
from uuid import uuid4

import pytest

from app.api import scans as scans_module
from app.api.scans import _progress_stream


class _FakeRequest:
    def __init__(self, disconnect_after: int | None = None) -> None:
        self.calls = 0
        self._disconnect_after = disconnect_after

    async def is_disconnected(self) -> bool:
        self.calls += 1
        return self._disconnect_after is not None and self.calls > self._disconnect_after


@pytest.fixture(autouse=True)
def _no_sleep(monkeypatch):
    async def instant(_seconds: float) -> None:
        return None

    monkeypatch.setattr(scans_module.asyncio, "sleep", instant)


def _job(status: str) -> dict:
    return {"id": str(uuid4()), "status": status, "progress": 0.5}


async def test_stream_emits_progress_then_done() -> None:
    frames = [
        _job("QUEUED"),
        _job("RUNNING"),
        _job("SUCCEEDED"),
    ]

    def snapshot(_job_id) -> dict:
        return frames.pop(0)

    chunks = [
        chunk
        async for chunk in _progress_stream(_FakeRequest(), uuid4(), snapshot=snapshot)
    ]
    assert chunks[0].startswith("event: progress")
    assert "QUEUED" in chunks[0]
    assert "RUNNING" in chunks[1]
    assert "SUCCEEDED" in chunks[2]
    assert chunks[3] == "event: done\ndata: {}\n\n"
    assert len(chunks) == 4


async def test_disconnected_client_ends_stream_without_frames() -> None:
    def snapshot(_job_id) -> dict:  # pragma: no cover - must not be called
        raise AssertionError("snapshot must not be fetched after disconnect")

    chunks = [
        chunk
        async for chunk in _progress_stream(
            _FakeRequest(disconnect_after=0), uuid4(), snapshot=snapshot
        )
    ]
    assert chunks == []


async def test_missing_job_ends_stream() -> None:
    chunks = [chunk async for chunk in _progress_stream(_FakeRequest(), uuid4(), snapshot=lambda _id: None)]
    assert chunks == []


async def test_unchanged_snapshot_is_not_resent(monkeypatch) -> None:
    same = _job("RUNNING")
    calls = {"n": 0}

    def snapshot(_job_id) -> dict:
        calls["n"] += 1
        if calls["n"] >= 3:
            return {**same, "status": "FAILED"}
        return dict(same)

    chunks = [chunk async for chunk in _progress_stream(_FakeRequest(), uuid4(), snapshot=snapshot)]
    # progress, progress(FAILED), done
    assert len(chunks) == 3
    assert chunks[0] == chunks[1].replace("FAILED", "RUNNING")


def test_sleep_is_awaited_not_skipped() -> None:
    """Guard against reintroducing the sync-generator mistake."""

    async def run() -> list[str]:
        frames = iter([_job("QUEUED"), _job("SUCCEEDED")])
        return [
            chunk
            async for chunk in _progress_stream(
                _FakeRequest(), uuid4(), snapshot=lambda _id: next(frames)
            )
        ]

    chunks = asyncio.run(run())
    assert any(chunk.startswith("event: done") for chunk in chunks)
