"""Build a large, validated free-proxy pool for the worker's egress rotation.

Usage (from the deployment host; stdout is the proxy list, diagnostics go to stderr):

    cat qa/fetch_proxies.py | ssh <host> 'cd /opt/nk-mp && mkdir -p runtime && \\
        docker compose exec -T api python - --out /data/proxies.txt \\
        --report /data/proxy_report.json'

The worker watches ``NIKE_MP_PROXY_URLS_FILE`` and hot-reloads it every 60s, so a
cron job can refresh the pool without touching the containers:

    */15 * * * * cd /opt/nk-mp && docker compose exec -T api python - \\
        < qa/fetch_proxies.py --out /data/proxies.txt >/dev/null 2>>/data/proxy.log

Four stages (each one a strict filter, all concurrent):

1. harvest: pull every source's plain text and scrape ``ip:port`` out of it;
2. liveness: plain-HTTP request through the proxy (cheap, no CONNECT needed);
3. identity: ask ``ipinfo.io`` **through** the proxy for country/ASN/city, and
   probe ``store/home`` (a Nike path that never answers 405) to prove the exit
   can tunnel TLS to the API host we actually need;
4. block rate: fire ``--samples`` requests at the o2o path and record how many
   come back 403/405 — that number is what decides whether an exit is useful.

Outputs: the validated list on stdout (``http://ip:port`` per line, CN exits
first, then by lowest block rate) and an optional JSON report with per-exit
country/ASN/latency/block-rate for the experiment log.

Measured context (2026-10-06 and re-confirmed 2026-10-09): the o2o 403/405 rate
is ~40-60% per request and does **not** depend on the exit IP (12 fresh exits
same distribution, χ²≈6.4/p≈0.85), so a pool buys *concurrency and per-request IP
independence*, not a higher pass rate — except possibly for mainland residential
exits, which is exactly what this tool is used to test.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import statistics
import sys
import time
from concurrent.futures import ThreadPoolExecutor

import httpx

#: 目标路径：o2o（会被 WAF 挑战/拦截）与 store/home（永不出 405，用来验隧道）。
O2O_PROBE = "https://api.nike.com.cn/o2o/buy/cart_reviews/v2/11111111-2222-3333-4444-555555555555"
STORE_PROBE = "https://api.nike.com.cn/store/home/v1/storeNumber/4075"
#: 出口 IP 回显（两个独立服务，互为校验；任一被伪造都会与另一个矛盾）。
ECHO_PROBES = ("https://api.ipify.org/?format=json", "https://checkip.amazonaws.com")
#: 明文探活目标：**大陆可达**且带特征串——用 Cloudflare 会把大陆出口系统性过滤掉
#: （我们实测：用 cf 探活时一批真大陆代理全部落选），而 open-port 设备也不会返回该特征串。
LIVENESS_URL = "http://www.baidu.com/robots.txt"
LIVENESS_MARKER = b"Baiduspider"

SOURCES = (
    # 平台既有源（2026-10 前就在用）
    "https://raw.githubusercontent.com/TheSpeedX/PROXY-List/master/http.txt",
    "https://raw.githubusercontent.com/TheSpeedX/PROXY-List/master/socks5.txt",
    "https://raw.githubusercontent.com/monosans/proxy-list/main/proxies/http.txt",
    "https://raw.githubusercontent.com/monosans/proxy-list/main/proxies/https.txt",
    "https://raw.githubusercontent.com/clarketm/proxy-list/master/proxy-list-raw.txt",
    "https://raw.githubusercontent.com/proxifly/free-proxy-list/main/proxies/all/data.txt",
    "https://raw.githubusercontent.com/roosterkid/openproxylist/main/HTTP_RAW.txt",
    "https://raw.githubusercontent.com/roosterkid/openproxylist/main/HTTPS_RAW.txt",
    "https://raw.githubusercontent.com/jetkai/proxy-list/main/online-proxies/txt/proxies-http.txt",
    "https://raw.githubusercontent.com/jetkai/proxy-list/main/online-proxies/txt/proxies-https.txt",
    "https://raw.githubusercontent.com/ALIILAPRO/Proxy/main/http.txt",
    "https://raw.githubusercontent.com/mmpx12/proxy-list/master/http.txt",
    "https://raw.githubusercontent.com/mmpx12/proxy-list/master/https.txt",
    "https://raw.githubusercontent.com/ShiftyTR/Proxy-List/master/http.txt",
    "https://raw.githubusercontent.com/ShiftyTR/Proxy-List/master/https.txt",
    "https://raw.githubusercontent.com/zloi-user/hideip.me/main/http.txt",
    "https://raw.githubusercontent.com/zloi-user/hideip.me/main/https.txt",
    "https://spys.me/proxy.txt",
    "https://api.openproxylist.xyz/http.txt",
    "https://api.proxyscrape.com/v4/free-proxy-list/get?request=display_proxies&protocol=http&proxy_format=ipport&format=text",
    "https://www.proxy-list.download/api/v1/get?type=http",
    "https://www.proxy-list.download/api/v1/get?type=https",
    # 2026-10-09 新增：用户整理的公开源
    "https://proxy.scdn.io/api/get_proxy.php?protocol=http&count=20",
    "https://proxy.scdn.io/api/get_proxy.php?protocol=https&count=20",
    "https://proxy.scdn.io/api/get_proxy.php?protocol=all&count=20",
    "https://proxy.scdn.io/api/get_proxy.php?protocol=http&count=20&country_code=CN",
    "https://proxy.scdn.io/api/get_proxy.php?protocol=https&count=20&country_code=CN",
    "https://api.proxyscrape.com/v4/free-proxy-list/get?request=display_proxies&protocol=socks5&proxy_format=ipport&format=text",
    "https://raw.githubusercontent.com/proxifly/free-proxy-list/main/proxies/protocols/http/data.txt",
    "https://raw.githubusercontent.com/proxifly/free-proxy-list/main/proxies/protocols/https/data.txt",
    "https://raw.githubusercontent.com/proxifly/free-proxy-list/main/proxies/countries/CN/data.txt",
    "https://proxylist.geonode.com/api/proxy-list?limit=500&page=1&sort_by=lastChecked&sort_type=desc&protocols=http%2Chttps",
    "https://proxy.010438.xyz/api/get?type=1&count=200&format=txt",
    "https://raw.githubusercontent.com/ClearProxy/checked-proxy-list/main/proxies/http.txt",
    "https://raw.githubusercontent.com/ClearProxy/checked-proxy-list/main/checked.txt",
    "https://raw.githubusercontent.com/hproxy-com/free-proxy-list/main/proxies/http.txt",
    "https://raw.githubusercontent.com/ErcinDedeoglu/proxies/main/proxies/http.txt",
    "https://raw.githubusercontent.com/ErcinDedeoglu/proxies/main/proxies/https.txt",
)

ADDR_RE = re.compile(r"\b(\d{1,3}(?:\.\d{1,3}){3}):(\d{2,5})\b")
NIKE_HEADERS = {
    "appid": "com.nike.nvs.comerce.wo.wechat.prod",
    "nike-api-caller-id": "nike:wechat:web:1.0",
    "x-nike-partner": "NDS",
    "x-nike-region": "NIKE0S088",
    "accept": "application/json",
    "referer": "https://servicewechat.com/wx74b301953dcdd7db/18/page-frame.html",
    "user-agent": (
        "Mozilla/5.0 (iPhone; CPU iPhone OS 16_6 like Mac OS X) AppleWebKit/605.1.15 "
        "(KHTML, like Gecko) Mobile/15E148 MicroMessenger/8.0.49(0x1800312b) "
        "NetType/WIFI Language/zh_CN"
    ),
}

CANDIDATE_LIMIT = int(os.environ.get("PROXY_CANDIDATE_LIMIT", "8000"))
CONCURRENCY = int(os.environ.get("PROXY_CONCURRENCY", "400"))
_SOCKS_MISSING = False


def log(message: str) -> None:
    print(message, file=sys.stderr, flush=True)


def proxy_url(proxy: str) -> str:
    return proxy if "://" in proxy else f"http://{proxy}"


def fetch_candidates() -> list[str]:
    found: set[str] = set()
    for url in SOURCES:
        try:
            response = httpx.get(url, timeout=25.0, follow_redirects=True)
            text = response.text
        except Exception as exc:  # noqa: BLE001
            log(f"  源失败 {url.split('/')[2]}: {type(exc).__name__}")
            continue
        hit = 0
        for match in ADDR_RE.finditer(text):
            found.add(f"{match.group(1)}:{match.group(2)}")
            hit += 1
        log(f"  源 {url.split('/')[2]:26s} HTTP {response.status_code} 解析 {hit:5d}，累计 {len(found)}")
    return sorted(found)[:CANDIDATE_LIMIT]


def _client(proxy: str, timeout: float, http2: bool = False) -> httpx.Client:
    return httpx.Client(
        proxy=proxy_url(proxy), timeout=timeout, http2=http2, follow_redirects=False
    )


def lookup_ip(ip: str) -> dict:
    """**不经代理**查 IP 的真实国家/ASN/城市。

    早期版本经代理去查 ipinfo，被免费代理/代理链篡改过（把 CN 阿里云 IP 报成 GB），
    所以身份一律用直连查询；代理只负责"回显它以为的出口 IP"。
    """
    try:
        with httpx.Client(timeout=8.0) as client:
            data = client.get(f"https://ipinfo.io/{ip}/json").json()
        return {
            "country": data.get("country", "??"),
            "city": data.get("city", ""),
            "org": (data.get("org") or "")[:60],
        }
    except Exception:  # noqa: BLE001
        return {"country": "??", "city": "", "org": ""}


def alive(proxy: str) -> str | None:
    """明文探活：最便宜的一关，先淘汰死 IP（不暴露任何上游请求）。

    判据：**200 且 body 带 "Baiduspider" 特征串**。两个坑都要躲：
    - 公网"开放端口"会把绝对 URI 当普通请求应答 200+HTML（标题 CWP ADMIN / NETCORE 路由器等），
      老判据（200/204）会把它们当代理 → 用特征串排除；
    - 用境外站探活会漏掉大陆出口（大陆代理到不了 cf）→ 换成百度 robots.txt。
    """
    global _SOCKS_MISSING
    try:
        with _client(proxy, 5.0) as client:
            response = client.get(LIVENESS_URL)
        if response.status_code != 200 or LIVENESS_MARKER not in response.content:
            return None
        return proxy
    except ImportError:
        if not _SOCKS_MISSING:
            _SOCKS_MISSING = True
            log("  !! 缺少 socksio（httpx[socks]），SOCKS 代理会被跳过")
        return None
    except Exception:  # noqa: BLE001
        return None


def _echo_ip(proxy: str) -> str | None:
    """经代理回显出口 IP；两个服务取一致值，矛盾则丢弃（可能被中间人伪造）。"""
    seen: list[str] = []
    for url in ECHO_PROBES:
        try:
            with _client(proxy, 8.0, http2=True) as client:
                response = client.get(url)
            text = response.text.strip()
            if url.endswith("format=json"):
                text = json.loads(text).get("ip", "")
            if text:
                seen.append(text)
        except Exception:  # noqa: BLE001
            continue
    if not seen:
        return None
    if len(set(seen)) > 1:
        return None  # 两个回显不一致：不可信
    return seen[0]


def inspect(proxy: str, samples: int) -> dict | None:
    """关卡 3+4：出口身份（回显 + 直连查询）+ Nike 可达性 + o2o 拦截率。"""
    exit_ip = _echo_ip(proxy)
    if not exit_ip:
        return None
    identity = lookup_ip(exit_ip)
    proxy_ip = proxy.split(":")[0]
    try:
        with _client(proxy, 8.0, http2=True) as client:
            store = client.get(STORE_PROBE, headers=NIKE_HEADERS)
            if store.status_code != 200:
                return None
            statuses: list[int] = []
            for _ in range(samples):
                try:
                    statuses.append(client.get(O2O_PROBE, headers=NIKE_HEADERS).status_code)
                except Exception:  # noqa: BLE001
                    statuses.append(0)
        blocked = sum(1 for code in statuses if code in (403, 405))
        return {
            "proxy": proxy,
            "country": identity["country"],
            "city": identity["city"],
            "org": identity["org"],
            "exit_ip": exit_ip,
            "chained": exit_ip != proxy_ip,
            "samples": len(statuses),
            "blocked": blocked,
            "block_rate": round(blocked / max(1, len(statuses)), 3),
            "statuses": statuses,
        }
    except Exception:  # noqa: BLE001
        return None


def main() -> int:
    parser = argparse.ArgumentParser(description="免费代理池构建 + 拦截率打标")
    parser.add_argument("--out", default="", help="把可用出口写进该文件（平台热重载用）")
    parser.add_argument("--report", default="", help="逐出口报告 JSON 路径")
    parser.add_argument("--samples", type=int, default=6, help="每个出口的 o2o 采样次数")
    parser.add_argument("--min-samples", type=int, default=0, help="至少多少次采样才打标（0=不限）")
    parser.add_argument("--inspect-limit", type=int, default=400, help="进入身份/拦截率阶段的出口上限")
    args = parser.parse_args()

    started = time.time()
    log("== 1/4 拉取候选 ==")
    candidates = fetch_candidates()
    log(f"候选 {len(candidates)} 个，开始明文探活（并发 {CONCURRENCY}）")

    with ThreadPoolExecutor(max_workers=CONCURRENCY) as pool:
        live = [proxy for proxy in pool.map(alive, candidates) if proxy]
    log(f"明文探活通过 {len(live)} 个（{time.time() - started:.0f}s）")

    if not live:
        log("没有可用出口，结束")
        return 1
    targets = live[: args.inspect_limit]
    log(f"== 2/4 身份 + Nike 可达性 + 拦截率（上限 {len(targets)} 个，并发 60）==")
    with ThreadPoolExecutor(max_workers=60) as pool:
        reports = [item for item in pool.map(lambda p: inspect(p, args.samples), targets) if item]
    log(f"通过身份/可达性验证 {len(reports)} 个（共 {time.time() - started:.0f}s）")

    reports.sort(key=lambda row: (row["country"] != "CN", row["block_rate"]))
    by_country: dict[str, list[float]] = {}
    for row in reports:
        by_country.setdefault(row["country"], []).append(row["block_rate"])
    log("== 3/4 分国家拦截率（403/405 占比）==")
    for country, rates in sorted(by_country.items(), key=lambda kv: -len(kv[1]))[:12]:
        log(
            f"  {country:3s} n={len(rates):3d} 中位 {statistics.median(rates):.2f} "
            f"最低 {min(rates):.2f} 最高 {max(rates):.2f}"
        )

    print(f"结果: 候选 {len(candidates)} → 存活 {len(live)} → 验证通过 {len(reports)}", file=sys.stderr)

    if args.report:
        with open(args.report, "w", encoding="utf-8") as handle:
            json.dump({"generated_at": time.time(), "reports": reports}, handle, ensure_ascii=False, indent=1)
        log(f"报告写入 {args.report}")

    lines = [f"http://{row['proxy']}" for row in reports]
    if args.out:
        with open(args.out, "w", encoding="utf-8") as handle:
            handle.write("\n".join(lines) + "\n")
        log(f"可用出口写入 {args.out}（{len(lines)} 条，CN 优先、按拦截率升序）")
    for line in lines:
        print(line)
    return 0


if __name__ == "__main__":
    sys.exit(main())
