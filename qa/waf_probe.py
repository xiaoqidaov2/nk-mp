#!/usr/bin/env python3
"""WAF 拦截率探针（纯标准库，任何机器都能跑）。

用法::

    python3 waf_probe.py --n 20                 # 每臂 20 发，约 3 分钟
    python3 waf_probe.py --n 5 --url https://...  # 换路径/快速试

它做四件事：
1. 打印本机出口的 IP / 国家 / ASN（ipinfo.io）与响应里阿里云边缘节点（`via: ens-cache*.xxNN`）；
2. 逐臂交错发请求，统计 **405（拦截页）** 占比：
   A 基线（平台现有头组合）      B 小程序 UA
   C caller-id=miniapp           D appid=微信 AppID
   E X-Forwarded-For 伪造        F geoloc=cc=CN
3. 单独测【连发（无间隔）】的 405 占比，与 1/s 的 A 臂对比（速率维度）；
4. 打印每臂见过的状态码分布，便于识别"不是 405 的其它拒绝"（403/429/200/401）。

给运维/逆向用：把一个环境的结果贴出来就能横比（本次记录：HK VPS vs 台北 GCP）。
"""

from __future__ import annotations

import argparse
import json
import random
import re
import ssl
import statistics
import sys
import time
import urllib.request

DEFAULT_URL = "https://api.nike.com.cn/o2o/buy/cart_reviews/v2/11111111-2222-3333-4444-555555555555"

#: 由 --proxy 注入（如 http://user:pass@host:port）；None = 直连。
PROXY: str | None = None
UA_CHROME = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/144.0.0.0 Safari/537.36"
)
UA_WECHAT = UA_CHROME + (
    " MicroMessenger/8.0.60.2560(0x28003C3D) WeChat/arm64 Weixin NetType/WIFI "
    "Language/zh_CN ABI/arm64 MiniProgramEnv/android"
)
BASE_HEADERS = {
    "appid": "com.nike.nvs.comerce.wo.wechat.prod",
    "nike-api-caller-id": "nike:wechat:web:1.0",
    "x-nike-partner": "NDS",
    "x-nike-region": "NIKE0S001",
    "accept": "application/json",
    "referer": "https://servicewechat.com/wx74b301953dcdd7db/18/page-frame.html",
    "user-agent": UA_CHROME,
}

ARMS = {
    "A 基线": {},
    "B 小程序UA": {"user-agent": UA_WECHAT},
    "C caller=miniapp": {"nike-api-caller-id": "nike:wechat:miniapp:1.0"},
    "D appid=wx…": {"appid": "wx74b301953dcdd7db"},
    "E XFF 伪造": {"X-Forwarded-For": "114.114.114.114"},
    "F geoloc=CN": {"Cookie": "geoloc=cc=CN"},
}


def _slurp(url: str, timeout: float = 12.0) -> tuple[int, dict[str, str], bytes]:
    ctx = ssl.create_default_context()
    opener = urllib.request.build_opener(*_handlers())
    request = urllib.request.Request(url, headers={"user-agent": UA_CHROME})
    try:
        with opener.open(request, timeout=timeout) as response:  # noqa: S310 - 探针工具
            return response.status, dict(response.headers), response.read()[:4096]
    except urllib.error.HTTPError as exc:  # 4xx/5xx 都会走到这里
        return exc.code, dict(exc.headers), exc.read()[:4096]


def _handlers() -> tuple:
    """代理（可选）：``--proxy`` 对 HTTPS 走 CONNECT，标准库即可。"""
    if not PROXY:
        return ()
    return (urllib.request.ProxyHandler({"http": PROXY, "https": PROXY}),)


def egress() -> str:
    try:
        status, _headers, body = _slurp("https://ipinfo.io/json")
        if status == 200:
            data = json.loads(body)
            return f"{data.get('ip')} {data.get('country')} {data.get('org', '')[:40]} @{data.get('city')}"
    except Exception as exc:  # noqa: BLE001
        return f"<unknown: {exc}>"
    return "<unknown>"


def request_once(url: str, headers: dict[str, str]) -> tuple[int, str]:
    merged = dict(BASE_HEADERS)
    merged.update(headers)
    opener = urllib.request.build_opener(*_handlers())
    request = urllib.request.Request(url, headers=merged)
    try:
        with opener.open(request, timeout=15.0) as response:  # noqa: S310 - 探针工具
            return response.status, response.headers.get("via", "")
    except urllib.error.HTTPError as exc:
        return exc.code, exc.headers.get("via", "")
    except Exception:  # noqa: BLE001 - 网络层失败也计入（打印在网络错误里）
        return 0, ""


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--n", type=int, default=20, help="每臂请求数")
    parser.add_argument("--url", default=DEFAULT_URL)
    parser.add_argument("--pause", type=float, default=1.0, help="臂内请求间隔（秒）")
    parser.add_argument("--burst", type=int, default=10, help="连发臂的请求数")
    parser.add_argument("--proxy", default=None, help="出口代理，如 http://user:pass@host:port")
    parser.add_argument(
        "--arms", default="", help="只跑这些臂（逗号分隔的键首字符，如 A,C,F）；空=全部"
    )
    parser.add_argument("--seed", type=int, default=20261009, help="臂顺序随机种子")
    args = parser.parse_args()

    global PROXY
    PROXY = args.proxy

    print(f"出口: {egress()}" + (f"  （经代理 {PROXY.split('@')[-1]}）" if PROXY else ""))
    print(f"目标: {args.url}")
    print(f"每臂 n={args.n}，间隔 {args.pause}s；连发臂 {args.burst} 发；臂顺序已随机化\n")

    selected = dict(ARMS)
    if args.arms:
        wanted = {item.strip().upper() for item in args.arms.split(",") if item.strip()}
        selected = {name: extra for name, extra in ARMS.items() if name[:1].upper() in wanted}
        if not selected:
            print("!! --arms 没匹配到任何臂")
            return 2
    rng = random.Random(args.seed)
    results: dict[str, list[int]] = {name: [] for name in selected}
    edges: set[str] = set()
    for index in range(args.n):
        names = list(selected)
        rng.shuffle(names)  # 每轮随机臂顺序：消除"时间窗"偏差
        for name in names:
            extra = selected[name]
            status, via = request_once(args.url, extra)
            results[name].append(status)
            if via:
                match = re.search(r"ens-cache\d+\.([a-z]{2}\d+)", via)
                edges.add(match.group(1) if match else via[:40])
            time.sleep(args.pause)

    print(f"{'臂':<16}{'405率':>10}{'中位数耗时':>12}   状态码分布")
    for name, codes in results.items():
        blocked = sum(1 for code in codes if code == 405)
        pct = 100.0 * blocked / max(1, len(codes))
        dist = {code: codes.count(code) for code in sorted(set(codes))}
        print(f"{name:<16}{pct:>9.0f}%{'':>10}   {dist}")

    burst_codes = []
    started = time.monotonic()
    for _ in range(args.burst):
        status, _via = request_once(args.url, {})
        burst_codes.append(status)
    elapsed = time.monotonic() - started
    burst_blocked = sum(1 for code in burst_codes if code == 405)
    rate = args.burst / max(0.001, elapsed)
    print(
        f"\n连发臂（{args.burst} 发 / {elapsed:.1f}s ≈ {rate:.1f} req/s）: "
        f"405 = {burst_blocked}/{args.burst} = {100.0 * burst_blocked / max(1, len(burst_codes)):.0f}%"
    )
    if edges:
        print(f"边缘节点（via 尾段）: {', '.join(sorted(edges))}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
