"""QA smoke test for the self-service Nike binding loop.

Covers, against a running QA stack (offline stub only):

1. ``POST /ops/stores/sync`` after the operator binds its own phone.
2. Phone binding for three accounts (ops / A / B): 201 + CODE_SENT, then
   ``verify`` with the fixed code 123456 -> 200 and status ACTIVE/EXPIRING.
3. The classic honest-collection loop on the operator (scenario-driven
   identity): task -> scan -> matrix -> untrusted round -> retry -> RESTOCK.
4. Identity-aware prices: A and B scan the same store/style through their own
   bindings and see *their own* price (699 vs 799), each visible only to its
   owner.
5. Unbinding A: a new scan/task fails with 409 NIKE_CREDENTIAL_REQUIRED while
   B keeps scanning; ``GET /me/nike-credential`` stays self-consistent.

    apps/api/.venv/bin/python qa/smoke.py

Environment: ``QA_API`` (default http://127.0.0.1:18000) and ``QA_STUB``
(default http://127.0.0.1:19100).
"""

from __future__ import annotations

import os
import sys
import time
import uuid
from urllib.parse import urlparse

import httpx

API = os.environ.get("QA_API", "http://127.0.0.1:18000")
STUB = os.environ.get("QA_STUB", "http://127.0.0.1:19100")
PASSWORD = "qa-password"
CODE = "123456"
TERMINAL = {"SUCCEEDED", "PARTIAL", "FAILED", "CANCELLED"}
STOCK_EVENT_TYPES = {"RESTOCK", "SOLD_OUT", "PRICE_CHANGE", "NEW_ARRIVAL_AT_STORE"}

STYLE = "DN3707-202"
STORE = "QA001"

#: The operator keeps the one phone the stub always knows; that identity is
#: scenario-driven, so the classic baseline/restock/untrusted loop still holds.
#: A and B need their own phones because the platform enforces exactly one
#: active platform account per phone (``uq_credential_phone_active``); the stub
#: registers those phones with identity-scoped prices.
OPS_PHONE = "+8613800000000"
A_PHONE = "+8613800000001"
B_PHONE = "+8613800000002"
A_PRICE = 699
B_PRICE = 799


def fail(message: str) -> None:
    print(f"FAIL: {message}", file=sys.stderr)
    raise SystemExit(1)


class User:
    def __init__(self, label: str, email: str) -> None:
        self.label = label
        self.email = email
        self.client = httpx.Client(base_url=API, timeout=30.0)
        self.csrf = ""

    def login(self) -> User:
        self.client.cookies.clear()
        response = self.client.post(
            "/api/v1/auth/login", json={"email": self.email, "password": PASSWORD}
        )
        if response.status_code != 200:
            fail(f"{self.label} login: {response.status_code} {response.text}")
        self.csrf = response.json()["csrf_token"]
        if not self._eventually(lambda: self.client.get("/api/v1/auth/me").status_code == 200):
            fail(f"{self.label}: login session never became visible")
        return self

    def headers(self) -> dict[str, str]:
        return {"X-CSRF-Token": self.csrf}

    def _eventually(self, predicate, timeout: float = 5.0) -> bool:
        """Poll until ``predicate`` holds.

        The API commits inside the ``get_db`` dependency teardown, which FastAPI
        runs *after* the response is sent, so a read issued immediately after a
        2xx write can still miss that write (see qa/README.md "已知问题").
        """
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if predicate():
                return True
            time.sleep(0.1)
        return False

    # ------------------------------------------------------------- binding

    def start_binding(self, phone: str) -> httpx.Response:
        return self.client.post(
            "/api/v1/me/nike-auth/sessions",
            json={"phone": phone, "consent": True, "request_id": str(uuid.uuid4())},
            headers=self.headers(),
        )

    def bind(self, phone: str) -> dict:
        started = self.start_binding(phone)
        if started.status_code != 201:
            fail(f"{self.label} bind {phone}: {started.status_code} {started.text}")
        attempt = started.json()
        if attempt["state"] != "CODE_SENT":
            fail(f"{self.label} attempt not CODE_SENT: {attempt}")
        verified = self.client.post(
            f"/api/v1/me/nike-auth/sessions/{attempt['id']}/verify",
            json={"code": CODE},
            headers=self.headers(),
        )
        if verified.status_code != 200:
            fail(f"{self.label} verify: {verified.status_code} {verified.text}")
        credential = verified.json()
        if credential["status"] not in ("ACTIVE", "EXPIRING"):
            fail(f"{self.label} credential not usable after verify: {credential}")
        return credential

    def credential(self) -> dict:
        response = self.client.get("/api/v1/me/nike-credential")
        if response.status_code != 200:
            fail(f"{self.label} credential read: {response.status_code} {response.text}")
        return response.json()

    def unbind(self) -> None:
        response = self.client.delete("/api/v1/me/nike-credential", headers=self.headers())
        if response.status_code != 204:
            fail(f"{self.label} unbind: {response.status_code} {response.text}")
        if not self._eventually(
            lambda: self.credential()["status"] in ("REVOKED", "NOT_CONNECTED")
        ):
            fail(f"{self.label}: unbind did not become visible")

    # ----------------------------------------------------------- scanning

    def create_task(self, name: str, store_codes: list[str]) -> httpx.Response:
        response = self.client.post(
            "/api/v1/tasks",
            json={"name": name, "store_codes": store_codes, "style_codes": [STYLE]},
            headers=self.headers(),
        )
        if response.status_code == 201:
            task_id = response.json()["id"]
            # A scan resolves the task in a fresh request; make sure the row is
            # committed first (same teardown-commit race as login).
            if not self._eventually(
                lambda: self.client.get(f"/api/v1/tasks/{task_id}").status_code == 200
            ):
                fail(f"{self.label}: task {task_id} never became visible")
        return response

    def scan(self, task_id: str) -> httpx.Response:
        return self.client.post(
            "/api/v1/scans", json={"task_id": task_id}, headers=self.headers()
        )

    def wait_job(self, job_id: str, timeout: float = 180.0, previous_finished_at=None) -> dict:
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            job = self.client.get(f"/api/v1/scans/{job_id}").json()
            # A requeue resets the job; ignore the terminal snapshot that was
            # already there before the retry (teardown-commit lag again).
            if job["status"] in TERMINAL and job["finished_at"] != previous_finished_at:
                return job
            time.sleep(1.0)
        fail(f"{self.label}: job {job_id} did not finish within {timeout}s")

    def run_scan(self, task_id: str) -> dict:
        created = self.scan(task_id)
        if created.status_code != 201:
            fail(f"{self.label} scan create: {created.status_code} {created.text}")
        return self.wait_job(created.json()["id"])

    def matrix(self, store_codes: str = "") -> dict:
        params = {"style_code": STYLE}
        if store_codes:
            params["store_codes"] = store_codes
        response = self.client.get("/api/v1/matrix", params=params)
        if response.status_code != 200:
            fail(f"{self.label} matrix: {response.status_code} {response.text}")
        return response.json()


def cell_map(matrix: dict) -> dict[tuple[str, str], dict]:
    return {(cell["store_code"], cell["size_label"]): cell for cell in matrix["cells"]}


def _assert_offline_upstream(ops: "Session") -> None:
    """离线冒烟只能对着 stub 跑：真实模式下会发真实短信、查真实 Nike。"""
    detail = ops.client.get("/api/v1/ops/health/detail")
    if detail.status_code != 200:
        fail(f"health detail unavailable: {detail.status_code} {detail.text}")
    base = str(detail.json().get("nike_api_base", ""))
    if not base.startswith(("http://127.0.0.1", "http://stub", "http://localhost")):
        fail(
            "QA 栈当前指向真实 Nike（NIKE_MP_NIKE_API_BASE="
            f"{base}）；smoke 只允许对着 stub 运行。请叠加 qa/compose.stub.*.yaml 后重试。"
        )


def main() -> None:
    with httpx.Client(base_url=STUB, timeout=10.0) as stub:
        # --- stub reset + identity registration (A/B get their own prices) ---
        stub.post("/__reset")
        for phone, identity, price in (
            (A_PHONE, "A", A_PRICE),
            (B_PHONE, "B", B_PRICE),
        ):
            registered = stub.post(
                "/__accounts",
                json={"phone": phone, "identity": identity, "price": price},
            )
            if registered.status_code != 200 or not registered.json().get("ok"):
                fail(f"stub account registration failed: {registered.text}")

        # Stub contract spot-checks: unknown phone -> 404, wrong code -> 400.
        unknown = stub.post("/credential_lookup/v1", json={"credential": "+8613999999999"})
        if unknown.status_code != 404:
            fail(f"unknown phone should be 404, got {unknown.status_code}")
        bad_code = stub.post(
            "/challenge/code/v1",
            json={
                "credential": OPS_PHONE,
                "verification_code": "000000",
                "client_id": "qa",
                "verification_code_type": "CHALLENGE",
            },
        )
        if bad_code.status_code != 400 or (
            (bad_code.json().get("errors") or [{}])[0].get("code") != "invalid_verification_code"
        ):
            fail(f"wrong code should be 400 invalid_verification_code: {bad_code.text}")

        # --- operator binds its phone and syncs the store catalogue ---
        ops = User("ops", "ops@qa.local").login()
        _assert_offline_upstream(ops)
        # 推送渠道必须先于第一次采集闭环建好：检查结束时会往这里汇总投递。
        channel = ops.client.post(
            "/api/v1/me/notification-channels",
            json={
                "platform": "custom",
                "name": "冒烟推送",
                "fields": {"url": f"json://{urlparse(STUB).netloc}/__push"},
            },
            headers=ops.headers(),
        )
        if channel.status_code != 201:
            fail(f"ops push channel create failed: {channel.status_code} {channel.text}")
        channel_id = channel.json()["id"]
        tested = ops.client.post(
            f"/api/v1/me/notification-channels/{channel_id}/test", headers=ops.headers()
        )
        if tested.status_code != 200 or not tested.json().get("ok"):
            fail(f"push test message failed: {tested.status_code} {tested.text}")
        ops_credential = ops.bind(OPS_PHONE)
        sync = ops.client.post("/api/v1/ops/stores/sync", headers=ops.headers())
        if sync.status_code != 200 or not sync.json().get("ok"):
            fail(f"store sync failed: {sync.status_code} {sync.text}")
        print(f"[1/9] ops bound + store sync ok: {sync.json()['synced']} stores")

        # --- one phone, one platform account: a second binder is refused ---
        a = User("A", "qa-a@qa.local").login()
        clash = a.start_binding(OPS_PHONE)
        if clash.status_code != 409 or clash.json().get("error_code") != "NIKE_PHONE_CONNECT_UNAVAILABLE":
            fail(f"sharing the operator phone should be refused: {clash.status_code} {clash.text}")
        a_credential = a.bind(A_PHONE)
        b = User("B", "qa-b@qa.local").login()
        b_credential = b.bind(B_PHONE)
        print(
            f"[2/9] three identities bound (ops={ops_credential['phone_masked']}, "
            f"A={a_credential['phone_masked']}, B={b_credential['phone_masked']})"
        )

        # --- classic honest-collection loop on the scenario identity (ops) ---
        task = ops.create_task(f"QA 冒烟任务 {time.strftime('%Y%m%d-%H%M%S')}", [])
        if task.status_code != 201:
            fail(f"ops task create failed: {task.status_code} {task.text}")
        task_id = task.json()["id"]
        if not task.json()["all_stores"]:
            fail("ops task should be marked all-stores")
        baseline = ops.run_scan(task_id)
        if baseline["status"] != "SUCCEEDED" or baseline["done_stores"] != 2:
            fail(f"baseline scan unexpected: {baseline}")

        cells = cell_map(ops.matrix())
        statuses = {key: cell["availability"] for key, cell in cells.items()}
        if statuses.get(("QA001", "40")) != "OUT_OF_STOCK":
            fail(f"size 40 should be out of stock in baseline: {statuses}")
        if statuses.get(("QA001", "42")) != "IN_STOCK" or statuses.get(("QA002", "46")) != "IN_STOCK":
            fail(f"in-stock sizes missing: {statuses}")
        priced = cells.get(("QA001", "42"))
        if priced is None or priced["price_status"] != "OK" or priced["total_price"] != 699:
            fail(f"price should be present for in-stock sizes: {priced}")
        before_events = ops.client.get(
            "/api/v1/changes", params={"event_type": "RESTOCK"}
        ).json()
        if before_events:
            fail("the first scan must only build a baseline, not a RESTOCK")
        print("[3/9] baseline scan ok: 40=OUT_OF_STOCK, 42/44/46=IN_STOCK with price")

        # --- untrusted round: no stock conclusion, no stock events ---
        def stock_event_ids() -> set[int]:
            rows = ops.client.get("/api/v1/changes").json()
            return {row["id"] for row in rows if row["event_type"] in STOCK_EVENT_TYPES}

        stock_before = stock_event_ids()
        failures_before = {
            row["id"]
            for row in ops.client.get("/api/v1/changes", params={"event_type": "SCAN_FAILED"}).json()
        }
        stub.post("/__scenario", json={"scenario": "untrusted"})
        untrusted = ops.run_scan(task_id)
        if untrusted["unknown_stores"] != 2:
            fail(f"untrusted round should mark both stores unknown: {untrusted}")
        new_stock_events = stock_event_ids() - stock_before
        if new_stock_events:
            fail(f"an untrusted round must not produce stock events: {new_stock_events}")
        failures_after = {
            row["id"]
            for row in ops.client.get("/api/v1/changes", params={"event_type": "SCAN_FAILED"}).json()
        }
        if len(failures_after - failures_before) != 2:
            fail("untrusted round should record one SCAN_FAILED diagnostic per store")
        stores = ops.client.get(f"/api/v1/scans/{untrusted['id']}/stores").json()
        if any(row["status"] != "UNKNOWN" for row in stores):
            fail(f"store rows should be UNKNOWN: {stores}")
        print(f"[4/9] untrusted round honest: {untrusted['status']} / unknown={untrusted['unknown_stores']}")

        # --- retry the incomplete stores after the restock flip ---
        stub.post("/__scenario", json={"scenario": "restock"})
        restocks_before = {
            row["id"]
            for row in ops.client.get("/api/v1/changes", params={"event_type": "RESTOCK"}).json()
        }
        retry = ops.client.post(
            f"/api/v1/scans/{untrusted['id']}/retry-failed", headers=ops.headers()
        )
        if retry.status_code != 200:
            fail(f"retry failed: {retry.status_code} {retry.text}")
        retried = ops.wait_job(
            untrusted["id"], previous_finished_at=untrusted.get("finished_at")
        )
        if retried["status"] != "SUCCEEDED" or retried["done_stores"] != 2:
            fail(f"retry should finish all stores: {retried}")
        new_restocks = [
            row
            for row in ops.client.get("/api/v1/changes", params={"event_type": "RESTOCK"}).json()
            if row["id"] not in restocks_before
        ]
        restocked = {(row["store_code"], row["size_label"]) for row in new_restocks}
        if restocked != {("QA001", "40"), ("QA002", "40")}:
            fail(f"expected two RESTOCK events for size 40: {restocked}")
        dashboard = ops.client.get("/api/v1/dashboard").json()
        if dashboard["review_requests_today"] <= 0:
            fail("quota usage was not metered")
        stub.post("/__scenario", json={"scenario": "baseline"})
        print(f"[5/9] retry + events ok: {sorted(restocked)}; quota usage={dashboard['review_requests_today']}")

        # --- identity-aware prices: same store/style, different accounts ---
        a_task = a.create_task(f"A 冒烟任务 {time.strftime('%Y%m%d-%H%M%S')}", [STORE])
        if a_task.status_code != 201:
            fail(f"A task create failed: {a_task.status_code} {a_task.text}")
        a_job = a.run_scan(a_task.json()["id"])
        if a_job["status"] != "SUCCEEDED" or a_job["done_stores"] != 1:
            fail(f"A scan unexpected: {a_job}")

        b_task = b.create_task(f"B 冒烟任务 {time.strftime('%Y%m%d-%H%M%S')}", [STORE])
        if b_task.status_code != 201:
            fail(f"B task create failed: {b_task.status_code} {b_task.text}")
        b_job = b.run_scan(b_task.json()["id"])
        if b_job["status"] != "SUCCEEDED" or b_job["done_stores"] != 1:
            fail(f"B scan unexpected: {b_job}")

        a_cell = cell_map(a.matrix(STORE)).get((STORE, "42"))
        b_cell = cell_map(b.matrix(STORE)).get((STORE, "42"))
        if not a_cell or a_cell["total_price"] != A_PRICE or a_cell["price_status"] != "OK":
            fail(f"A should see its own price {A_PRICE}: {a_cell}")
        if not b_cell or b_cell["total_price"] != B_PRICE or b_cell["price_status"] != "OK":
            fail(f"B should see its own price {B_PRICE}: {b_cell}")
        if a_cell["total_price"] == b_cell["total_price"]:
            fail("A and B must see different prices for the same sku")
        # Owner isolation inside one tenant: A must not read B's job.
        if a.client.get(f"/api/v1/scans/{b_job['id']}").status_code != 404:
            fail("A must not be able to read B's scan job")
        print(f"[6/9] identity prices differ: A={a_cell['total_price']} B={b_cell['total_price']}, owner-scoped")

        # --- unbind A: A is locked out, B keeps working ---
        a.unbind()
        a_scan = a.scan(a_task.json()["id"])
        if a_scan.status_code != 409 or a_scan.json().get("error_code") not in (
            "NIKE_CREDENTIAL_REQUIRED",
            "NIKE_CREDENTIAL_CHANGED",
        ):
            fail(f"A scan after unbind should be 409 credential error: {a_scan.status_code} {a_scan.text}")
        a_new_task = a.create_task(f"A 解绑后任务 {time.strftime('%Y%m%d-%H%M%S')}", [STORE])
        if a_new_task.status_code != 409 or a_new_task.json().get("error_code") not in (
            "NIKE_CREDENTIAL_REQUIRED",
            "NIKE_CREDENTIAL_CHANGED",
        ):
            fail(f"A task after unbind should be 409 credential error: {a_new_task.status_code} {a_new_task.text}")
        b_again = b.run_scan(b_task.json()["id"])
        if b_again["status"] != "SUCCEEDED" or b_again["done_stores"] != 1:
            fail(f"B must keep scanning after A unbound: {b_again}")
        b_cell_again = cell_map(b.matrix(STORE)).get((STORE, "42"))
        if not b_cell_again or b_cell_again["total_price"] != B_PRICE:
            fail(f"B price must stay {B_PRICE}: {b_cell_again}")
        print(f"[7/9] A unbound -> 409 {a_scan.json()['error_code']}; B still scanning at {B_PRICE}")

        # --- credential status is self-consistent for all three accounts ---
        a_state = a.credential()
        b_state = b.credential()
        ops_state = ops.credential()
        if a_state["status"] not in ("REVOKED", "NOT_CONNECTED"):
            fail(f"A should read back as unbound: {a_state}")
        if a_state["access_expires_at"] is not None:
            fail(f"A must not expose a live token after unbind: {a_state}")
        if b_state["status"] not in ("ACTIVE", "EXPIRING") or b_state["access_expires_at"] is None:
            fail(f"B should read back as connected: {b_state}")
        if ops_state["status"] not in ("ACTIVE", "EXPIRING"):
            fail(f"ops should read back as connected: {ops_state}")

        stats = stub.get("/__stats").json()
        if stats["send_count"] != 3:
            fail(f"the stub should have counted exactly 3 SMS sends: {stats}")
        print(
            f"[8/9] credential states ok: A={a_state['status']} "
            f"B={b_state['status']} ops={ops_state['status']}; stub sends={stats['send_count']}"
        )

        # --- push: test message and the aggregated scan message reach the stub ---
        def stub_pushes() -> list[dict]:
            response = stub.get("/__push")
            if response.status_code != 200:
                fail(f"stub push log unavailable: {response.status_code} {response.text}")
            return response.json()

        deadline = time.monotonic() + 10.0
        pushes: list[dict] = []
        while time.monotonic() < deadline:
            pushes = stub_pushes()
            if any(row["title"].startswith("【测试】") for row in pushes) and any(
                row["title"].startswith("【库存提醒】") for row in pushes
            ):
                break
            time.sleep(0.5)
        if not any(row["title"].startswith("【测试】") for row in pushes):
            fail(f"推送断言失败：测试消息没有送达 stub（{pushes}）")
        summaries = [row for row in pushes if row["title"].startswith("【库存提醒】")]
        if not any("补货" in row["body"] and "DN3707-202" in row["body"] for row in summaries):
            fail(f"推送断言失败：汇总消息缺少补货明细（{summaries}）")
        notifications = ops.client.get("/api/v1/me/notifications", params={"limit": 5})
        if notifications.status_code != 200:
            fail(f"推送记录读取失败：{notifications.status_code} {notifications.text}")
        rows = notifications.json()
        if not rows or rows[0]["state"] != "SENT":
            fail(f"推送断言失败：最近记录首条应为已发送（{rows}）")
        print("[9/9] 推送：测试消息与汇总消息均已送达")
        print("SMOKE PASS")


if __name__ == "__main__":
    main()
