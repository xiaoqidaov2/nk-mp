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
   B keeps scanning; ``GET /me/nike-credentials`` stays self-consistent.
6. 30 秒档：任务保存 30 秒频率，调度器连续推两轮（一轮接一轮，不叠加）。
7. 风控冷却：stub ``throttled`` 场景 -> 整轮提前结束（THROTTLED）、账号冷却、
   降速通知、补跑作业在窗口结束后先跑并成功。
8. 一用户多账号轮换：A 绑第二个 Nike 账号 -> ``GET /me/nike-credentials`` 两行；
   stub ``accounts2`` 只让第二个账号的 review 轮询 403 -> 该账号冷却，下一轮由
   第一个账号完成（``GET /__state`` 的 ``reviews_by_identity`` 证明它零调用），
   任务本身不显示冷却。

    apps/api/.venv/bin/python qa/smoke.py

    # 额度用尽（小时闸）专用闭环：先按压低闸值重启栈
    QA_QUOTA_HOURLY_CAP=2 docker compose ... up -d
    QA_SMOKE_MODE=cooldown apps/api/.venv/bin/python qa/smoke.py

Environment: ``QA_API`` (default http://127.0.0.1:18000) and ``QA_STUB``
(default http://127.0.0.1:19100); ``QA_PUSH_URL`` overrides the channel
address the *API* must reach (bridged stacks: ``json://stub:9100/__push``);
``QA_SMOKE_MODE`` = full | cooldown;
``QA_THROTTLE_BACKOFF_SECONDS`` / ``QA_JOB_RETRY_DELAY_SECONDS`` mirror the
stack's own throttle knobs so the script knows the expected windows.
"""

from __future__ import annotations

import os
import sys
import time
import uuid
from datetime import UTC, datetime, timedelta
from urllib.parse import urlparse

import httpx

API = os.environ.get("QA_API", "http://127.0.0.1:18000")
STUB = os.environ.get("QA_STUB", "http://127.0.0.1:19100")
#: 渠道地址是给 **API/worker** 用的，和宿主看到的 STUB 不一定是同一台主机：
#: 桥接网络下 API 在容器里，必须走服务名（如 json://stub:9100/__push）。
PUSH_URL = os.environ.get("QA_PUSH_URL", f"json://{urlparse(STUB).netloc}/__push")
PASSWORD = "qa-password"
CODE = "123456"
TERMINAL = {"SUCCEEDED", "PARTIAL", "FAILED", "CANCELLED"}
STOCK_EVENT_TYPES = {"RESTOCK", "SOLD_OUT", "PRICE_CHANGE", "NEW_ARRIVAL_AT_STORE"}

#: full = 常规闭环 + 30 秒档 + 风控冷却；cooldown = 压低小时闸的额度冷却闭环。
SMOKE_MODE = os.environ.get("QA_SMOKE_MODE", "full")
#: 与 QA 栈的 NIKE_MP_THROTTLE_BACKOFF_SECONDS / NIKE_MP_JOB_RETRY_DELAY_SECONDS 保持一致。
THROTTLE_WINDOW = float(os.environ.get("QA_THROTTLE_BACKOFF_SECONDS", "30"))
RETRY_DELAY = float(os.environ.get("QA_JOB_RETRY_DELAY_SECONDS", "5"))

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
#: A 的第二个 Nike 账号（一用户多账号）：只有它在 ``accounts2`` 场景下被限速。
A2_PHONE = "+8613800000003"
A2_IDENTITY = "A2"
A_PRICE = 699
B_PRICE = 799
A2_PRICE = 899


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

    def credentials(self) -> list[dict]:
        """All bound Nike accounts of this platform user (one user, N accounts)."""
        response = self.client.get("/api/v1/me/nike-credentials")
        if response.status_code != 200:
            fail(f"{self.label} credentials read: {response.status_code} {response.text}")
        return response.json()

    def credential(self) -> dict:
        """The first account row, or a NOT_CONNECTED placeholder when unbound."""
        rows = self.credentials()
        return rows[0] if rows else {"status": "NOT_CONNECTED", "access_expires_at": None}

    def unbind(self, index: int = 0) -> None:
        rows = self.credentials()
        if len(rows) <= index:
            fail(f"{self.label} has no account at index {index} to unbind: {rows}")
        target = rows[index]["id"]
        response = self.client.delete(
            f"/api/v1/me/nike-credentials/{target}", headers=self.headers()
        )
        if response.status_code != 204:
            fail(f"{self.label} unbind: {response.status_code} {response.text}")
        if not self._eventually(lambda: len(self.credentials()) <= index):
            fail(f"{self.label}: unbind did not become visible")

    # ----------------------------------------------------------- scanning

    def create_task(
        self,
        name: str,
        store_codes: list[str],
        cadence: int | None = None,
        active: bool | None = None,
    ) -> httpx.Response:
        payload: dict = {"name": name, "store_codes": store_codes, "style_codes": [STYLE]}
        if cadence is not None:
            payload["cadence_seconds"] = cadence
        if active is not None:
            payload["is_active"] = active
        response = self.client.post(
            "/api/v1/tasks",
            json=payload,
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


def _task_entry(ops: "User", task_id: str) -> dict:
    response = ops.client.get("/api/v1/tasks")
    if response.status_code != 200:
        fail(f"task list read failed: {response.status_code} {response.text}")
    for row in response.json():
        if row["id"] == task_id:
            return row
    fail(f"task {task_id} missing from the list")


def _task_jobs(ops: "User", task_id: str) -> list[dict]:
    response = ops.client.get("/api/v1/scans", params={"task_id": task_id})
    if response.status_code != 200:
        fail(f"scan list read failed: {response.status_code} {response.text}")
    return sorted(response.json(), key=lambda row: row["created_at"])


def _wait_jobs(ops: "User", task_id: str, *, minimum: int, timeout: float = 150.0) -> list[dict]:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        rows = _task_jobs(ops, task_id)
        if len(rows) >= minimum:
            return rows
        time.sleep(1.0)
    fail(f"task {task_id} did not reach {minimum} round(s) within {timeout}s")


def _wait_job_kind(ops: "User", task_id: str, kind: str, timeout: float = 30.0) -> dict:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        for row in _task_jobs(ops, task_id):
            if row["kind"] == kind:
                return row
        time.sleep(1.0)
    fail(f"task {task_id} produced no {kind} job within {timeout}s")


def _wait_push(stub: httpx.Client, predicate, timeout: float = 20.0) -> dict:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        response = stub.get("/__push")
        if response.status_code == 200:
            for row in response.json():
                if predicate(row):
                    return row
        time.sleep(0.5)
    fail("expected push notice did not reach the stub")


def _section_new_store(stub: httpx.Client, ops: "User") -> None:
    """新店自动纳入监控：同步后就该被「全部门店」任务覆盖，变化也要推送。"""
    stub.post("/__scenario", json={"scenario": "baseline"})
    added = stub.post("/__stores", json={"store_code": "QA900", "display_name": "离线换季优惠新店 900"})
    if added.status_code != 200 or not added.json().get("added"):
        fail(f"stub add store failed: {added.status_code} {added.text}")
    synced = ops.client.post("/api/v1/ops/stores/sync", headers=ops.headers())
    if synced.status_code != 200:
        fail(f"store sync failed: {synced.status_code} {synced.text}")
    if synced.json()["synced"] < 3:
        fail(f"store sync missed the new store: {synced.text}")
    stores = ops.client.get("/api/v1/stores").json()
    codes = sorted(item["store_code"] for item in stores)
    if "QA900" not in codes:
        fail(f"new store not in the catalogue: {codes}")

    task = ops.create_task(f"新店任务 {time.strftime('%Y%m%d-%H%M%S')}", [])
    if task.status_code != 201:
        fail(f"new-store task create failed: {task.status_code} {task.text}")
    task_id = task.json()["id"]
    first = ops.run_scan(task_id)
    if first["status"] != "SUCCEEDED" or first["total_stores"] != len(codes):
        fail(f"all-store task did not cover every store: {first} vs {codes}")

    pushes_before = len(stub.get("/__push").json())
    stub.post("/__scenario", json={"scenario": "restock"})
    second = ops.run_scan(task_id)
    if second["status"] != "SUCCEEDED":
        fail(f"second round unexpected: {second}")
    notice = _wait_push(
        stub, lambda row: "QA900" in row["body"] and "补货" in row["body"]
    )
    if len(stub.get("/__push").json()) <= pushes_before:
        fail("new store change produced no push")
    if "有货" not in notice["body"]:
        fail(f"new store push misses the current state: {notice['body']!r}")
    stub.post("/__scenario", json={"scenario": "baseline"})
    print(f"[新店] 同步后自动纳入监控：{codes}；新店变化已推送")

def _review_requests(ops: "User") -> int:
    return int(ops.client.get("/api/v1/dashboard").json()["review_requests_today"])


def _section_waf_billing(stub: httpx.Client, ops: "User") -> None:
    """405 挑战重试不计费：同一轮工作，挑战轮与普通轮计费相同、上游调用翻倍。"""
    stub.post("/__scenario", json={"scenario": "baseline"})
    created = ops.create_task(f"计费任务 {time.strftime('%Y%m%d-%H%M%S')}", [STORE])
    if created.status_code != 201:
        fail(f"billing task create failed: {created.status_code} {created.text}")
    task_id = created.json()["id"]

    before = _review_requests(ops)
    plain = ops.run_scan(task_id)
    if plain["status"] != "SUCCEEDED":
        fail(f"billing baseline round unexpected: {plain}")
    billed_plain = _review_requests(ops) - before

    calls_before = int(stub.get("/__stats").json()["review_calls"])
    stub.post("/__scenario", json={"scenario": "waf"})
    before = _review_requests(ops)
    waf_job = ops.run_scan(task_id)
    if waf_job["status"] != "SUCCEEDED":
        fail(f"challenge round unexpected: {waf_job}")
    billed_waf = _review_requests(ops) - before
    stats = stub.get("/__stats").json()
    calls_waf = int(stats["review_calls"]) - calls_before
    challenges = int(stats["waf_challenges"])

    if billed_plain <= 0:
        fail("baseline round metered nothing")
    if billed_waf != billed_plain:
        fail(f"challenge retries were billed: plain={billed_plain} waf={billed_waf}")
    if calls_waf < billed_waf * 1.5:
        fail(f"challenge scenario did not add upstream attempts: calls={calls_waf} billed={billed_waf}")
    stub.post("/__scenario", json={"scenario": "baseline"})
    print(
        f"[计费] 挑战不计费：普通轮 {billed_plain} 次 vs 挑战轮 {billed_waf} 次；"
        f"上游调用 {calls_waf} 次（挑战 {challenges} 次全部免费）"
    )

def _pause_task(ops: "User", task_id: str) -> None:
    response = ops.client.patch(
        f"/api/v1/tasks/{task_id}", json={"is_active": False}, headers=ops.headers()
    )
    if response.status_code != 200:
        fail(f"task pause failed: {response.status_code} {response.text}")


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


def _bootstrap_ops(stub: httpx.Client) -> tuple["User", int, dict]:
    """ops 登录 + 离线校验 + 推送渠道 + 绑定手机 + 同步门店清单。"""
    ops = User("ops", "ops@qa.local").login()
    _assert_offline_upstream(ops)
    # 推送渠道必须先于第一次采集闭环建好：检查结束时会往这里汇总投递。
    channel = ops.client.post(
        "/api/v1/me/notification-channels",
        json={
            "platform": "custom",
            "name": "冒烟推送",
            "fields": {"url": PUSH_URL},
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
    credential = ops.bind(OPS_PHONE)
    sync = ops.client.post("/api/v1/ops/stores/sync", headers=ops.headers())
    if sync.status_code != 200 or not sync.json().get("ok"):
        fail(f"store sync failed: {sync.status_code} {sync.text}")
    return ops, int(sync.json()["synced"]), credential


def _section_30s(ops: "User") -> None:
    """30 秒档：上一轮结束后立刻开始下一轮；上一轮未结束前不叠加。"""
    created = ops.create_task(f"QA 30秒任务 {time.strftime('%Y%m%d-%H%M%S')}", [STORE], cadence=30)
    if created.status_code != 201:
        fail(f"30s task create failed: {created.status_code} {created.text}")
    task = created.json()
    if task["cadence_seconds"] != 30 or not task["is_active"]:
        fail(f"30s task did not keep its cadence: {task}")
    task_id = task["id"]
    rounds = _wait_jobs(ops, task_id, minimum=2, timeout=150.0)
    first = ops.wait_job(rounds[0]["id"], timeout=120.0)
    if first["status"] != "SUCCEEDED" or first["done_stores"] != 1:
        fail(f"first 30s round unexpected: {first}")
    second = ops.wait_job(rounds[1]["id"], timeout=120.0)
    if second["status"] != "SUCCEEDED" or second["done_stores"] != 1:
        fail(f"second 30s round unexpected: {second}")
    if datetime.fromisoformat(second["created_at"]) <= datetime.fromisoformat(first["finished_at"]):
        fail(f"30s rounds must not stack: {first} -> {second}")
    _pause_task(ops, task_id)
    print(f"[10/12] 30 秒档闭环：连续两轮（{first['id'][:8]} → {second['id'][:8]}），已暂停")


def _section_throttle(stub: httpx.Client, ops: "User") -> None:
    """风控限速：整轮提前结束、账号冷却、到点先补跑失败门店。"""
    created = ops.create_task(
        f"QA 冷却任务 {time.strftime('%Y%m%d-%H%M%S')}", [STORE, "QA002"], cadence=3600
    )
    if created.status_code != 201:
        fail(f"cooldown task create failed: {created.status_code} {created.text}")
    task_id = created.json()["id"]
    baseline = _wait_jobs(ops, task_id, minimum=1, timeout=60.0)
    first = ops.wait_job(baseline[0]["id"], timeout=120.0)
    if first["status"] != "SUCCEEDED" or first["done_stores"] != 2:
        fail(f"cooldown task baseline round unexpected: {first}")

    stub.post("/__scenario", json={"scenario": "throttled"})
    manual = ops.scan(task_id)
    if manual.status_code != 201:
        fail(f"throttled scan create failed: {manual.status_code} {manual.text}")
    aborted = ops.wait_job(manual.json()["id"], timeout=180.0)
    if aborted["status"] != "FAILED" or aborted["failed_stores"] != 2:
        fail(f"throttled round should end early with both stores failed: {aborted}")
    store_rows = ops.client.get(f"/api/v1/scans/{aborted['id']}/stores").json()
    if {row["error_code"] for row in store_rows} != {"THROTTLED"}:
        fail(f"store rows should carry THROTTLED: {store_rows}")

    entry = _task_entry(ops, task_id)
    if entry["cooldown_reason"] != "throttled" or not entry["cooldown_until"]:
        fail(f"task should report a throttled cooldown: {entry}")
    until = datetime.fromisoformat(entry["cooldown_until"])
    window = (until - datetime.now(UTC)).total_seconds()
    if not (0 < window <= THROTTLE_WINDOW + 30):
        fail(f"cooldown window {window:.1f}s should follow the QA ladder ({THROTTLE_WINDOW}s)")
    # 上游先恢复：冷却结束后补跑必须能成功（通知已在冷却进入时入队，不受影响）。
    stub.post("/__scenario", json={"scenario": "baseline"})
    notice = _wait_push(
        stub, lambda row: "自动继续" in row["body"] and "被 Nike 限速" in row["body"]
    )
    if "检查已自动降速" not in notice["title"]:
        fail(f"cooldown notice title unexpected: {notice}")

    # 补跑作业已排队，但 not_before 在冷却窗口内：不允许提前执行。
    retry = _wait_job_kind(ops, task_id, "RETRY", timeout=30.0)
    while datetime.now(UTC) < until - timedelta(seconds=2):
        current = ops.client.get(f"/api/v1/scans/{retry['id']}").json()
        if current["status"] != "QUEUED":
            fail(f"retry job must wait for the cooldown window: {current}")
        time.sleep(1.0)

    done = ops.wait_job(retry["id"], timeout=THROTTLE_WINDOW + RETRY_DELAY + 90)
    if done["status"] != "SUCCEEDED" or done["done_stores"] != 2:
        fail(f"retry after the cooldown should recover both stores: {done}")
    jobs = _task_jobs(ops, task_id)
    if len(jobs) != 3:
        fail(f"the retry must run before another full round: {jobs}")
    settled = _task_entry(ops, task_id)
    if settled["cooldown_until"] is not None:
        fail(f"a fully successful round should clear the expired cooldown: {settled}")
    print("[11/12] 风控冷却闭环：THROTTLED → 冷却 → 补跑成功（无下一轮抢先）")


def _stub_reviews(stub: httpx.Client) -> dict[str, int]:
    """Per-identity review traffic (PUT + poll): proves which account served a round."""
    response = stub.get("/__state")
    if response.status_code != 200:
        fail(f"stub state unavailable: {response.status_code} {response.text}")
    return {
        str(key): int(value)
        for key, value in (response.json().get("reviews_by_identity") or {}).items()
    }


def _pause_active_tasks(user: "User") -> int:
    """Pause every active task of ``user`` (manual rounds must not race the scheduler)."""
    response = user.client.get("/api/v1/tasks")
    if response.status_code != 200:
        fail(f"{user.label} task list read failed: {response.status_code} {response.text}")
    paused = 0
    for row in response.json():
        if row["is_active"]:
            _pause_task(user, row["id"])
            paused += 1
    return paused


def _section_multi_account(stub: httpx.Client, a: "User") -> None:
    """一用户多账号：一个账号被限速冷却后，同一个任务自动换另一个账号继续。

    全程只走 HTTP。账号选择是「未冷却里最近最少使用优先」，所以先用调度器真正
    派发一轮把第一个账号记成"刚用过"（``mark_used``），第二个账号（从未使用）就
    会稳定地被优先选中——不靠运气，冒烟可重复。
    """
    stub.post("/__scenario", json={"scenario": "baseline"})
    registered = stub.post(
        "/__accounts",
        json={"phone": A2_PHONE, "identity": A2_IDENTITY, "price": A2_PRICE},
    )
    if registered.status_code != 200 or not registered.json().get("ok"):
        fail(f"stub A2 account registration failed: {registered.status_code} {registered.text}")

    # 账号一（identity A）：先绑上，并让调度器派发一次，写掉它的 last_attempt_at。
    a_first = a.bind(A_PHONE)
    priming = a.create_task(f"A 轮换预热 {time.strftime('%Y%m%d-%H%M%S')}", [STORE], cadence=30)
    if priming.status_code != 201:
        fail(f"priming task create failed: {priming.status_code} {priming.text}")
    priming_id = priming.json()["id"]
    primed = _wait_jobs(a, priming_id, minimum=1, timeout=120.0)
    a.wait_job(primed[0]["id"], timeout=120.0)
    # 之后 A 的任务全部停用：调度器不得抢派（它会用另一个账号去撞 403，污染计数）。
    _pause_active_tasks(a)

    # 账号二（identity A2）：作为第二个账号绑定，从未使用 → LRU 排在第一位。
    a_second = a.bind(A2_PHONE)
    rows = a.credentials()
    if len(rows) != 2:
        fail(f"a user with two Nike accounts must list exactly 2 rows: {rows}")
    if {row["id"] for row in rows} != {a_first["id"], a_second["id"]}:
        fail(f"credential rows must be the two bound accounts: {rows}")

    # 只做手动检查的容器任务（停用）：轮换发生在账号选择层，与调度器共用同一段逻辑。
    rotation = a.create_task(
        f"A 轮换任务 {time.strftime('%Y%m%d-%H%M%S')}", [STORE], cadence=3600, active=False
    )
    if rotation.status_code != 201:
        fail(f"rotation task create failed: {rotation.status_code} {rotation.text}")
    rotation_id = rotation.json()["id"]

    stub.post("/__scenario", json={"scenario": "accounts2"})
    before = _stub_reviews(stub)
    limited = a.run_scan(rotation_id)
    after_limited = _stub_reviews(stub)
    if limited["status"] != "FAILED" or limited["failed_stores"] != 1:
        fail(f"the limited account round must end early with the store failed: {limited}")
    store_rows = a.client.get(f"/api/v1/scans/{limited['id']}/stores").json()
    if {row["error_code"] for row in store_rows} != {"THROTTLED"}:
        fail(f"store rows should carry THROTTLED: {store_rows}")
    a2_calls = after_limited.get(A2_IDENTITY, 0) - before.get(A2_IDENTITY, 0)
    a_calls = after_limited.get("A", 0) - before.get("A", 0)
    if a2_calls <= 0 or a_calls != 0:
        fail(f"the limited round must be served by A2 only: A2={a2_calls} A={a_calls}")

    # 冷却只落在被限速的那个账号上；另一个账号照常可用。
    by_phone = {str(row["phone_masked"]): row for row in a.credentials()}
    limited_row = by_phone.get(str(a_second["phone_masked"]))
    healthy_row = by_phone.get(str(a_first["phone_masked"]))
    if limited_row is None or healthy_row is None:
        fail(f"credential rows must be addressable by masked phone: {by_phone}")
    if not limited_row["cooldown_until"] or limited_row["cooldown_reason"] != "throttled":
        fail(f"the limited account must carry a throttled cooldown: {limited_row}")
    if healthy_row["cooldown_until"] is not None:
        fail(f"the healthy account must not be cooling down: {healthy_row}")
    window_end = datetime.fromisoformat(limited_row["cooldown_until"])
    if datetime.now(UTC) >= window_end:
        fail(
            "the QA cooldown window elapsed before the recovery round (raise "
            f"QA_THROTTLE_BACKOFF_SECONDS, now {THROTTLE_WINDOW}s)"
        )

    # 冷却期内下一轮必须落到另一个账号，且被限速账号一次也不许被调用。
    before_success = _stub_reviews(stub)
    recovered = a.run_scan(rotation_id)
    after_success = _stub_reviews(stub)
    if recovered["status"] != "SUCCEEDED" or recovered["done_stores"] != 1:
        fail(f"the round after the cooldown must be served by the other account: {recovered}")
    if after_success.get(A2_IDENTITY, 0) != before_success.get(A2_IDENTITY, 0):
        fail(
            "the successful round must not touch the cooling account: "
            f"A2 {before_success.get(A2_IDENTITY, 0)} → {after_success.get(A2_IDENTITY, 0)}"
        )
    if after_success.get("A", 0) <= before_success.get("A", 0):
        fail(
            "the successful round must be served by account A: "
            f"{before_success.get('A', 0)} → {after_success.get('A', 0)}"
        )

    entry = _task_entry(a, rotation_id)
    if entry["cooldown_until"] is not None or entry["cooldown_reason"] is not None:
        fail(f"another account is still usable, so the task must not report a cooldown: {entry}")

    stub.post("/__scenario", json={"scenario": "baseline"})
    print(
        f"[12/12] 一用户多账号轮换：{a_second['phone_masked']} 被限速 → 冷却 "
        f"{limited_row['cooldown_until']}；下一轮由 {a_first['phone_masked']} 完成，"
        f"被限速账号零调用；任务无冷却"
    )


def _run_quota_cooldown(stub: httpx.Client) -> None:
    """``QA_QUOTA_HOURLY_CAP=2`` 专用：额度用尽 → 整轮提前结束 + 账号冷却。

    小时窗口只能到点重开，脚本不断言"等待恢复"，改为断言窗口、通知与冷却期间
    不再派发（自动恢复由风控闭环的 30 秒阶梯用例证明）。
    """
    stub.post("/__reset")
    ops, synced, _credential = _bootstrap_ops(stub)
    print(f"[1/5] ops bound + store sync ok: {synced} stores")
    created = ops.create_task(f"QA 额度冷却 {time.strftime('%Y%m%d-%H%M%S')}", [STORE, "QA002"])
    if created.status_code != 201:
        fail(f"quota cooldown task create failed: {created.status_code} {created.text}")
    task_id = created.json()["id"]
    rounds = _wait_jobs(ops, task_id, minimum=1, timeout=60.0)
    job = ops.wait_job(rounds[0]["id"], timeout=120.0)
    if job["status"] != "FAILED" or job["done_stores"] != 1 or job["failed_stores"] != 1:
        fail(
            "cap=2 round should abort early (1 done / 1 failed): "
            f"{job}（若刚跑过一次，小时闸已被上一轮用尽：请重建 QA 数据卷后重试）"
        )
    store_rows = ops.client.get(f"/api/v1/scans/{job['id']}/stores").json()
    failed = [row for row in store_rows if row["status"] == "FAILED"]
    if len(failed) != 1 or failed[0]["error_code"] != "BUDGET_EXHAUSTED":
        fail(f"the unpaid store should fail with BUDGET_EXHAUSTED: {store_rows}")
    print(f"[2/5] 额度用尽：作业提前结束（{job['done_stores']} 完成 / {job['failed_stores']} 失败）")

    entry = _task_entry(ops, task_id)
    if entry["cooldown_reason"] != "platform_hourly_budget" or not entry["cooldown_until"]:
        fail(f"task should report the hourly-budget cooldown: {entry}")
    until = datetime.fromisoformat(entry["cooldown_until"])
    expected = datetime.now(UTC).replace(minute=0, second=0, microsecond=0) + timedelta(
        hours=1, seconds=30
    )
    if abs((until - expected).total_seconds()) > 150:
        fail(f"hourly cooldown should reopen at the next hour + 30s: {until} vs {expected}")
    notice = _wait_push(
        stub, lambda row: "自动继续" in row["body"] and "本小时查询额度已用完" in row["body"]
    )
    if "检查已自动降速" not in notice["title"]:
        fail(f"cooldown notice title unexpected: {notice}")
    print(f"[3/5] 冷却与通知：至 {until.isoformat()}，通知已送达 stub")

    retry = _wait_job_kind(ops, task_id, "RETRY", timeout=30.0)
    if retry["status"] != "QUEUED":
        fail(f"retry must wait for the budget window: {retry}")
    next_run = datetime.fromisoformat(entry["next_run_at"])
    if next_run < until - timedelta(seconds=1):
        fail(f"next run must not precede the cooldown: {entry}")
    before = len(_task_jobs(ops, task_id))
    time.sleep(15.0)
    after = len(_task_jobs(ops, task_id))
    if after != before:
        fail("no new round may be dispatched while the account is cooling down")
    print("[4/5] 冷却期间不再派发；补跑作业排在窗口之后")
    print("[5/5] 额度冷却闭环通过（窗口到点由调度器自动继续）")
    print("SMOKE COOLDOWN PASS")


def main() -> None:
    with httpx.Client(base_url=STUB, timeout=10.0) as stub:
        if SMOKE_MODE == "cooldown":
            _run_quota_cooldown(stub)
            return
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
        ops, synced, ops_credential = _bootstrap_ops(stub)
        print(f"[1/12] ops bound + store sync ok: {synced} stores")

        # --- one phone, one platform account: a second binder is refused ---
        a = User("A", "qa-a@qa.local").login()
        clash = a.start_binding(OPS_PHONE)
        if clash.status_code != 409 or clash.json().get("error_code") != "NIKE_PHONE_CONNECT_UNAVAILABLE":
            fail(f"sharing the operator phone should be refused: {clash.status_code} {clash.text}")
        a_credential = a.bind(A_PHONE)
        b = User("B", "qa-b@qa.local").login()
        b_credential = b.bind(B_PHONE)
        print(
            f"[2/12] three identities bound (ops={ops_credential['phone_masked']}, "
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
        print("[3/12] baseline scan ok: 40=OUT_OF_STOCK, 42/44/46=IN_STOCK with price")

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
        print(f"[4/12] untrusted round honest: {untrusted['status']} / unknown={untrusted['unknown_stores']}")

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
        # 补货必须推送到渠道：只记事件不推送等于"补货了不通知"。
        restock_notice = _wait_push(stub, lambda row: "补货" in row["body"])
        if "库存提醒" not in restock_notice["title"]:
            fail(f"restock push title unexpected: {restock_notice}")
        # 文案必须能直接定位：货号 + 尺码 + 门店 + 当前状态（有货）。
        for token in (STYLE, "40", "QA001", "有货"):
            if token not in restock_notice["body"]:
                fail(f"restock push body misses {token!r}: {restock_notice['body']!r}")
        print(f"[5/12] retry + events + push ok: {sorted(restocked)}; quota={dashboard['review_requests_today']}")

        # --- price change must notify: same stock, new price ---
        stub.post("/__scenario", json={"scenario": "price_up"})
        prices_before = {
            row["id"]
            for row in ops.client.get("/api/v1/changes", params={"event_type": "PRICE_CHANGE"}).json()
        }
        price_job = ops.run_scan(task_id)
        if price_job["status"] != "SUCCEEDED":
            fail(f"price round unexpected: {price_job}")
        new_prices = [
            row
            for row in ops.client.get("/api/v1/changes", params={"event_type": "PRICE_CHANGE"}).json()
            if row["id"] not in prices_before
        ]
        if not new_prices:
            fail("a real price change produced no PRICE_CHANGE event")
        price_notice = _wait_push(stub, lambda row: "变价" in row["body"])
        if "库存提醒" not in price_notice["title"]:
            fail(f"price push title unexpected: {price_notice}")
        # 价格改回 baseline：这一轮用「临时检查」（无 task_id）跑，
        # 生产里手动检查/任务删除后的补跑都是这种形态，一样必须推送。
        pushes_before = len(stub.get("/__push").json())
        stub.post("/__scenario", json={"scenario": "baseline"})
        manual = ops.client.post(
            "/api/v1/scans",
            json={"store_codes": [STORE], "style_codes": [STYLE]},
            headers=ops.headers(),
        )
        if manual.status_code != 201:
            fail(f"manual scan create failed: {manual.status_code} {manual.text}")
        manual_job = ops.wait_job(manual.json()["id"])
        if manual_job["status"] != "SUCCEEDED" or manual_job.get("task_id") is not None:
            fail(f"manual scan unexpected: {manual_job}")
        pushes_after = len(stub.get("/__push").json())
        if pushes_after <= pushes_before:
            fail("a task-less manual check pushed nothing")
        print(
            f"[5b/12] price change notified: {len(new_prices)} 条事件已推送；"
            f"临时检查（无任务）也推送（{pushes_before} → {pushes_after}）"
        )

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
        print(f"[6/12] identity prices differ: A={a_cell['total_price']} B={b_cell['total_price']}, owner-scoped")

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
        print(f"[7/12] A unbound -> 409 {a_scan.json()['error_code']}; B still scanning at {B_PRICE}")

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
            f"[8/12] credential states ok: A={a_state['status']} "
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
        print("[9/12] 推送：测试消息与汇总消息均已送达")
        _section_30s(ops)
        _section_throttle(stub, ops)
        _section_new_store(stub, ops)
        _section_waf_billing(stub, ops)
        _section_multi_account(stub, a)
        print("SMOKE PASS")


if __name__ == "__main__":
    main()
