"""对**线上部署**手动跑一次真实检查（会消耗真实额度，仅人工执行）。

    NKMP_LIVE_URL=https://<域名> \
    NKMP_LIVE_EMAIL=<操作员邮箱> NKMP_LIVE_PASSWORD=<密码> \
    NKMP_LIVE_TASK=<任务名或 id> \
    apps/api/.venv/bin/python qa/live_check.py

它只做三件事：读连接状态 → 对指定任务发起一次「立即检查」→ 轮询并打印
每个门店的状态/原因码/写入条数（以及配额消耗）。默认**不创建**任何任务；
没有合适的任务时用 `NKMP_LIVE_TASK` 指定，或先到页面上建一个 1 店任务。

这不是测试套件的一部分（绝不进 pytest），也绝不猜测或存储任何凭据。
"""

from __future__ import annotations

import os
import sys
import time
import uuid

import httpx

BASE = os.environ.get("NKMP_LIVE_URL", "").rstrip("/")
EMAIL = os.environ.get("NKMP_LIVE_EMAIL", "")
PASSWORD = os.environ.get("NKMP_LIVE_PASSWORD", "")
TASK = os.environ.get("NKMP_LIVE_TASK", "")
TERMINAL = {"SUCCEEDED", "PARTIAL", "FAILED", "CANCELLED"}


def fail(message: str) -> None:
    print(f"FAIL: {message}", file=sys.stderr)
    raise SystemExit(1)


def main() -> None:
    if not (BASE and EMAIL and PASSWORD):
        fail("需要 NKMP_LIVE_URL / NKMP_LIVE_EMAIL / NKMP_LIVE_PASSWORD")

    with httpx.Client(base_url=BASE, timeout=60.0) as client:
        login = client.post("/api/v1/auth/login", json={"email": EMAIL, "password": PASSWORD})
        if login.status_code != 200:
            fail(f"登录失败：{login.status_code} {login.text}")
        headers = {"X-CSRF-Token": login.json()["csrf_token"]}
        print(f"登录成功：{login.json()['user']['email']}（{login.json()['user']['tenant_name']}）")

        credential = client.get("/api/v1/me/nike-credential")
        if credential.status_code != 200:
            fail(f"读取连接状态失败：{credential.status_code} {credential.text}")
        state = credential.json()
        print(
            f"Nike 连接：status={state['status']} phone={state['phone_masked']} "
            f"expires={state['access_expires_at']} last_error={state['last_error_code']}"
        )
        if state["status"] not in ("ACTIVE", "EXPIRING"):
            fail("连接当前不是可用状态，先重新验证账号再做真实检查")

        tasks = client.get("/api/v1/tasks").json()
        if not tasks:
            fail("线上没有监控任务；先建一个 1 店 1 货号的小任务再重跑本脚本")
        target = None
        if TASK:
            for row in tasks:
                if row["id"] == TASK or row["name"] == TASK:
                    target = row
                    break
            if target is None:
                fail(f"没找到任务 {TASK!r}；现有：{[row['name'] for row in tasks]}")
        else:
            target = min(tasks, key=lambda row: (row["style_count"], row["store_count"]))
        print(
            f"目标任务：{target['name']}（门店 {target['store_count']}，货号 {target['style_count']}，"
            f"频率 {target['cadence_seconds']}s，冷却 {target['cooldown_reason'] or '无'}）"
        )

        before = client.get("/api/v1/dashboard").json()
        created = client.post("/api/v1/scans", json={"task_id": target["id"]}, headers=headers)
        if created.status_code != 201:
            fail(f"发起检查失败：{created.status_code} {created.text}")
        job_id = created.json()["id"]
        print(f"已发起检查：{job_id}（门店 {created.json()['total_stores']}）")

        deadline = time.monotonic() + 600
        job = None
        while time.monotonic() < deadline:
            job = client.get(f"/api/v1/scans/{job_id}").json()
            if job["finished_at"]:
                break
            time.sleep(2.0)
        if job is None or not job["finished_at"]:
            fail("检查在 10 分钟内没有结束")
        print(
            f"结果：{job['status']} 完成 {job['done_stores']} / 失败 {job['failed_stores']} / "
            f"不完整 {job['unknown_stores']}，错误摘要 {job['error_summary'] or '—'}"
        )
        for row in client.get(f"/api/v1/scans/{job_id}/stores").json():
            print(
                f"  {row['store_code']:>8s} {row['status']:<9s} "
                f"{row['error_code'] or '—':<18s} obs={row['observations_written']} "
                f"events={row['events_written']} {row['message'][:60]}"
            )
        after = client.get("/api/v1/dashboard").json()
        print(
            f"额度：今日 {before['review_requests_today']} → {after['review_requests_today']} "
            f"（本轮 +{after['review_requests_today'] - before['review_requests_today']}）"
        )
        refreshed = client.get(f"/api/v1/tasks/{target['id']}").json()
        print(
            f"任务：下次检查 {refreshed['next_run_at']}，冷却 "
            f"{refreshed['cooldown_reason'] or '无'} / {refreshed['cooldown_until'] or '—'}"
        )
        print("LIVE CHECK DONE")


if __name__ == "__main__":
    main()