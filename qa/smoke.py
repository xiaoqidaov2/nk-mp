"""Phase B smoke test: 登录 → 创建任务 → 调度 → 采集 → 检查点 → 矩阵 → 变化流水 → 重试.

Drives the running QA stack over HTTP. The stack must be started with the
offline stub overlay; this script never calls the real Nike service.

    apps/api/.venv/bin/python qa/smoke.py
"""

from __future__ import annotations

import os
import sys
import time

import httpx

API = os.environ.get("QA_API", "http://localhost:8000")
STUB = os.environ.get("QA_STUB", "http://localhost:9100")
PASSWORD = "qa-password"
TERMINAL = {"SUCCEEDED", "PARTIAL", "FAILED", "CANCELLED"}


def fail(message: str) -> None:
    print(f"FAIL: {message}", file=sys.stderr)
    raise SystemExit(1)


def login(client: httpx.Client, email: str) -> str:
    response = client.post("/api/v1/auth/login", json={"email": email, "password": PASSWORD})
    if response.status_code != 200:
        fail(f"login {email}: {response.status_code} {response.text}")
    return response.json()["csrf_token"]


def wait_job(client: httpx.Client, job_id: str, timeout: float = 180.0) -> dict:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        job = client.get(f"/api/v1/scans/{job_id}").json()
        if job["status"] in TERMINAL:
            return job
        time.sleep(1.0)
    fail(f"job {job_id} did not finish within {timeout}s")


def main() -> None:
    with httpx.Client(base_url=API, timeout=30.0) as client, httpx.Client(base_url=STUB, timeout=10.0) as stub:
        if stub.get("/healthz").json().get("scenario") != "baseline":
            stub.post("/__scenario", json={"scenario": "baseline"})

        # --- platform operator syncs the store catalogue through the stub ---
        client.cookies.clear()
        ops_csrf = login(client, "ops@qa.local")
        sync = client.post("/api/v1/ops/stores/sync", headers={"X-CSRF-Token": ops_csrf})
        if sync.status_code != 200 or not sync.json().get("ok"):
            fail(f"store sync failed: {sync.status_code} {sync.text}")
        print(f"[1/6] store sync ok: {sync.json()['synced']} stores")

        # --- customer tenant creates a nationwide task and scans once ---
        client.cookies.clear()
        csrf = login(client, "qa@qa.local")
        task = client.post(
            "/api/v1/tasks",
            json={
                "name": f"QA 冒烟任务 {time.strftime('%Y%m%d-%H%M%S')}",
                "store_codes": [],
                "style_codes": ["DN3707-202"],
            },
            headers={"X-CSRF-Token": csrf},
        )
        if task.status_code != 201:
            fail(f"task create failed: {task.status_code} {task.text}")
        task_id = task.json()["id"]
        if not task.json()["all_stores"]:
            fail("task should be marked all-stores")
        print(f"[2/6] task created: {task_id} (all stores)")

        job = client.post("/api/v1/scans", json={"task_id": task_id}, headers={"X-CSRF-Token": csrf})
        if job.status_code != 201:
            fail(f"scan create failed: {job.status_code} {job.text}")
        first = wait_job(client, job.json()["id"])
        if first["status"] != "SUCCEEDED" or first["done_stores"] != 2:
            fail(f"first scan unexpected: {first}")
        print(f"[3/6] baseline scan succeeded: {first['done_stores']} stores")

        matrix = client.get("/api/v1/matrix", params={"style_code": "DN3707-202"}).json()
        cells = {(cell["store_code"], cell["size_label"]): cell for cell in matrix["cells"]}
        statuses = {key: cell["availability"] for key, cell in cells.items()}
        if statuses.get(("QA001", "40")) != "OUT_OF_STOCK":
            fail(f"size 40 should be out of stock in baseline: {statuses}")
        if statuses.get(("QA001", "42")) != "IN_STOCK" or statuses.get(("QA002", "46")) != "IN_STOCK":
            fail(f"in-stock sizes missing: {statuses}")
        priced = cells.get(("QA001", "42"))
        if priced is None or priced["price_status"] != "OK" or priced["total_price"] != 699:
            fail(f"price should be present for in-stock sizes: {priced}")
        if not matrix["last_checked_at"]:
            fail("matrix has no last_checked_at")
        print("[4/6] matrix ok: 40=OUT_OF_STOCK, 42/44/46=IN_STOCK with price")

        # --- untrusted round: no stock conclusion, no stock events ---
        STOCK_EVENT_TYPES = {"RESTOCK", "SOLD_OUT", "PRICE_CHANGE", "NEW_ARRIVAL_AT_STORE"}

        def stock_event_ids() -> set[int]:
            rows = client.get("/api/v1/changes").json()
            return {row["id"] for row in rows if row["event_type"] in STOCK_EVENT_TYPES}

        stock_before = stock_event_ids()
        failures_before = {
            row["id"]
            for row in client.get("/api/v1/changes", params={"event_type": "SCAN_FAILED"}).json()
        }
        stub.post("/__scenario", json={"scenario": "untrusted"})
        job = client.post("/api/v1/scans", json={"task_id": task_id}, headers={"X-CSRF-Token": csrf})
        untrusted = wait_job(client, job.json()["id"])
        if untrusted["unknown_stores"] != 2:
            fail(f"untrusted round should mark both stores unknown: {untrusted}")
        new_stock_events = stock_event_ids() - stock_before
        if new_stock_events:
            fail(f"an untrusted round must not produce stock events: {new_stock_events}")
        # Scan failures are tenant-private diagnostics and must be visible.
        failures_after = {
            row["id"]
            for row in client.get("/api/v1/changes", params={"event_type": "SCAN_FAILED"}).json()
        }
        if len(failures_after - failures_before) != 2:
            fail("untrusted round should record one SCAN_FAILED diagnostic per store")
        detail = client.get(f"/api/v1/scans/{untrusted['id']}").json()
        stores = client.get(f"/api/v1/scans/{untrusted['id']}/stores").json()
        if any(row["status"] != "UNKNOWN" for row in stores):
            fail(f"store rows should be UNKNOWN: {stores}")
        print(f"[5/6] untrusted round honest: {detail['status']} / unknown={detail['unknown_stores']}")

        # --- retry the incomplete stores after the restock flip ---
        stub.post("/__scenario", json={"scenario": "restock"})
        restocks_before = {
            row["id"]
            for row in client.get("/api/v1/changes", params={"event_type": "RESTOCK"}).json()
        }
        retry = client.post(f"/api/v1/scans/{untrusted['id']}/retry-failed", headers={"X-CSRF-Token": csrf})
        if retry.status_code != 200:
            fail(f"retry failed: {retry.status_code} {retry.text}")
        retried = wait_job(client, untrusted["id"])
        if retried["status"] != "SUCCEEDED" or retried["done_stores"] != 2:
            fail(f"retry should finish all stores: {retried}")

        new_restocks = [
            row
            for row in client.get("/api/v1/changes", params={"event_type": "RESTOCK"}).json()
            if row["id"] not in restocks_before
        ]
        restocked = {(row["store_code"], row["size_label"]) for row in new_restocks}
        if restocked != {("QA001", "40"), ("QA002", "40")}:
            fail(f"expected two RESTOCK events for size 40: {restocked}")

        dashboard = client.get("/api/v1/dashboard").json()
        if dashboard["review_requests_today"] <= 0:
            fail("quota usage was not metered")
        print(f"[6/6] retry + events ok: {sorted(restocked)}; quota usage={dashboard['review_requests_today']}")
        print("SMOKE PASS")


if __name__ == "__main__":
    main()
