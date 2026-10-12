"""Realistic client fingerprints for upstream requests.

The upstream API is the WeChat mini-program's own backend, so the most
plausible profiles are WeChat WebViews (iOS/Android) plus a couple of desktop
browsers. Rotation exists for *scale*: many concurrent stores can present
different, self-consistent profiles instead of one static header set.

Measured caveat (2026-10-06): the 405 challenge rate on the o2o endpoints is
~40% regardless of UA/header set, so rotation does not raise the per-request
pass rate — it only avoids fingerprint-shaped throttling and keeps a fleet of
concurrent requests from looking like one bot.
"""

from __future__ import annotations

#: Self-consistent header profiles. Each entry is merged over the caller's
#: base headers, so the API-identifying headers (appid, caller-id, partner,
#: region, referer) always stay in place.
PROFILES: tuple[dict[str, str], ...] = (
    {
        "user-agent": (
            "Mozilla/5.0 (iPhone; CPU iPhone OS 16_6 like Mac OS X) AppleWebKit/605.1.15 "
            "(KHTML, like Gecko) Mobile/15E148 MicroMessenger/8.0.49(0x1800312b) "
            "NetType/WIFI Language/zh_CN"
        ),
        "accept-language": "zh-CN,zh;q=0.9",
    },
    {
        "user-agent": (
            "Mozilla/5.0 (Linux; Android 13; SM-S9080 Build/TP1A.220624.014; wv) AppleWebKit/537.36 "
            "(KHTML, like Gecko) Version/4.0 Chrome/116.0.0.0 Mobile Safari/537.36 XWEB/1160065 "
            "MMWEBSDK/20231202 MicroMessenger/8.0.47.2560(0x28002F35) Weixin NetType/WIFI "
            "Language/zh_CN ABI/arm64 MiniProgramEnv/android"
        ),
        "accept-language": "zh-CN,zh;q=0.9",
    },
    {
        "user-agent": (
            "Mozilla/5.0 (Linux; Android 14; PJD110 Build/UKQ1.230924.001; wv) AppleWebKit/537.36 "
            "(KHTML, like Gecko) Version/4.0 Chrome/120.0.6099.230 Mobile Safari/537.36 XWEB/1200100 "
            "MMWEBSDK/20240301 MicroMessenger/8.0.50.2701(0x2800323D) Weixin NetType/5G "
            "Language/zh_CN ABI/arm64 MiniProgramEnv/android"
        ),
        "accept-language": "zh-CN,zh;q=0.9",
    },
    {
        "user-agent": (
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) "
            "Chrome/144.0.0.0 Safari/537.36"
        ),
        "accept-language": "zh-CN,zh;q=0.9,en;q=0.8",
        "sec-ch-ua": '"Chromium";v="144", "Not(A:Brand";v="24", "Google Chrome";v="144"',
        "sec-ch-ua-mobile": "?0",
        "sec-ch-ua-platform": '"Windows"',
    },
    {
        "user-agent": (
            "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/605.1.15 (KHTML, like Gecko) "
            "Version/17.4 Safari/605.1.15"
        ),
        "accept-language": "zh-CN,zh;q=0.9",
    },
)


def pick_profile(index: int) -> dict[str, str]:
    """Return a copy of profile ``index % len(PROFILES)``."""
    return dict(PROFILES[index % len(PROFILES)])
