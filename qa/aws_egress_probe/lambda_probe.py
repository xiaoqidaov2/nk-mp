"""一次性探针：从 AWS Lambda 测出口 IP、到 Nike 的延迟与拦截率。

对照基线（我们生产机 = 香港机房、中国优化线路）：
  DNS/TCP 建连 47–490ms、TTFB 175–634ms、405 挑战率 57–60%。

只用标准库 → 控制台直接粘贴即可，或 zip 部署：
  aws lambda create-function --runtime python3.12 --handler lambda_probe.handler \
      --zip-file fileb://probe.zip --role <role-arn> --timeout 60 --memory-size 256

测试事件（可选字段）：
  {"requests": 5, "token": "<Nike access token>", "store": "3402", "style": "IW1160-700"}
  · requests：每个"臂"发几次请求（默认 5；这是探针，故意很小）
  · token：给了才额外测"带 token 的门店列表"这一臂

安全：默认只打免鉴权端点；只读，不做任何写入/下单。
"""

from __future__ import annotations

import socket
import ssl
import time
import urllib.error
import urllib.request

API = "https://api.nike.com.cn"
TEST = "https://api-test.nike.com.cn"

#: 与平台客户端一致的请求头（否则 WAF 判定不具代表性）。
HEADERS = {
    "appid": "com.nike.nvs.comerce.wo.wechat.prod",
    "nike-api-caller-id": "nike:wechat:web:1.0",
    "x-nike-partner": "NDS",
    "referer": "https://servicewechat.com/wx74b301953dcdd7db/18/page-frame.html",
    "accept": "application/json",
    "accept-language": "zh-CN,zh;q=0.9",
    "user-agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/144.0.0.0 Safari/537.36"
    ),
}


def _get(url: str, *, token: str | None = None) -> dict:
    """一次请求 → 状态/耗时/是否 405 挑战；异常也当样本，绝不抛。"""
    headers = dict(HEADERS)
    if token:
        headers["Authorization"] = f"Bearer {token}"
    started = time.monotonic()
    try:
        with urllib.request.urlopen(urllib.request.Request(url, headers=headers), timeout=20) as res:
            body = res.read(2048)
            return {
                "status": res.status,
                "ms": round((time.monotonic() - started) * 1000, 1),
                "challenge": b"data-spm" in body,
            }
    except urllib.error.HTTPError as exc:
        try:
            body = exc.read(2048)
        except Exception:  # noqa: BLE001
            body = b""
        return {
            "status": exc.code,
            "ms": round((time.monotonic() - started) * 1000, 1),
            "challenge": exc.code == 405 or b"data-spm" in body,
        }
    except Exception as exc:  # noqa: BLE001 - 传输失败也算样本
        return {
            "status": 0,
            "ms": round((time.monotonic() - started) * 1000, 1),
            "challenge": False,
            "error": type(exc).__name__,
        }


def _latency(host: str) -> dict:
    """DNS / TCP / TLS 建连耗时（毫秒）。"""
    out: dict[str, float | str] = {}
    try:
        t0 = time.monotonic()
        socket.getaddrinfo(host, 443)
        out["dns_ms"] = round((time.monotonic() - t0) * 1000, 1)

        t0 = time.monotonic()
        raw = socket.create_connection((host, 443), timeout=10)
        out["tcp_ms"] = round((time.monotonic() - t0) * 1000, 1)

        t0 = time.monotonic()
        with ssl.create_default_context().wrap_socket(raw, server_hostname=host):
            out["tls_ms"] = round((time.monotonic() - t0) * 1000, 1)
    except Exception as exc:  # noqa: BLE001
        out["error"] = type(exc).__name__
    return out


def _egress_ips() -> list[str]:
    ips = []
    for url in ("https://api.ipify.org", "https://checkip.amazonaws.com"):
        try:
            with urllib.request.urlopen(url, timeout=8) as res:
                ips.append(res.read(64).decode().strip())
        except Exception as exc:  # noqa: BLE001
            ips.append(f"ERR:{type(exc).__name__}")
    return ips


def _arm(label: str, url: str, count: int, *, token: str | None = None) -> dict:
    samples = [_get(url, token=token) for _ in range(max(1, count))]
    statuses: dict[str, int] = {}
    for item in samples:
        key = str(item["status"]) + ("(challenge)" if item["challenge"] else "")
        statuses[key] = statuses.get(key, 0) + 1
    return {
        "label": label,
        "statuses": statuses,
        "challenged_pct": round(
            100.0 * sum(1 for item in samples if item["challenge"]) / len(samples), 1
        ),
        "ok": sum(1 for item in samples if item["status"] == 200),
        "avg_ms": round(sum(item["ms"] for item in samples) / len(samples), 1),
    }


def handler(event, context):  # noqa: ANN001, ANN201 - Lambda 入口
    event = event or {}
    count = int(event.get("requests") or 5)
    token = event.get("token") or None
    store = str(event.get("store") or "3402")
    style = str(event.get("style") or "IW1160-700")

    arms = [
        _arm("免鉴权·门店列表(api.nike.com.cn)", f"{API}/o2o/buy/regions/v1?anchor=0&count=5", count),
        _arm(
            "对照臂·测试域尺码表(应为 0% 挑战)",
            f"{TEST}/store/product_details/v1/marketplace/CN/language/zh-Hans"
            f"/store_number/{store}/style_color/{style}",
            count,
        ),
    ]
    if token:
        arms.append(
            _arm(
                "带 token·门店列表",
                f"{API}/o2o/buy/regions/v1?anchor=0&count=5",
                count,
                token=token,
            )
        )

    return {
        "egress_ips": _egress_ips(),
        "latency_api_nike": _latency("api.nike.com.cn"),
        "latency_api_test_nike": _latency("api-test.nike.com.cn"),
        "arms": arms,
        "baseline": "生产机(香港)：405 挑战率 57–60%、TCP 47–490ms、TTFB 175–634ms",
    }
