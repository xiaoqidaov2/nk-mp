# -*- coding: utf-8 -*-
"""
JWT / WAF 票据 自动提取器
================================================
数据源: Reqable（PC 端）实时抓包。模拟器里的小程序每次冷启动都会刷新 API JWT,
        流量经 Reqable Android → 同步到 PC Reqable → 本脚本自动提取写入
        jwt_latest.txt / waf_token.txt, 供 oracle / monitor 使用。
用法: python nike_jwt_autofetch.py
建议: 用 Windows 计划任务每小时跑一次 (小程序自然刷新后即可取到新的)
"""
import sys, os, json, base64, datetime, re

BASE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, BASE)
from reqable_mcp import Reqable

def main():
    r = Reqable()
    ids = json.loads(r.call("capture_live_filter", {"filters": []}, timeout=15)["text"])
    best_jwt = None
    waf = None
    for rid in ids[-120:]:
        try:
            rec = json.loads(r.call("capture_live_get_by_id", {"id": rid}, timeout=10)["text"])
        except Exception:
            continue
        u = rec.get("url", "")
        m = re.search(r"acw_sc__v2=([0-9a-f]{20,})", u)
        if m:
            waf = m.group(1)
        for h in rec["request"]["headers"]:
            if h["name"].lower() == "authorization" and h["value"].startswith("Bearer ey"):
                try:
                    pl = json.loads(base64.urlsafe_b64decode(
                        h["value"][7:].split(".")[1] + "=" * (-len(h["value"][7:].split(".")[1]) % 4)))
                    if pl.get("iss") == "oauth2acc-cn":   # 只认 api 域 JWT
                        ts = rec["connection"]["timestamp"]
                        if best_jwt is None or ts > best_jwt[0]:
                            best_jwt = (ts, h["value"], pl)
                except Exception:
                    pass
    r.close()
    if best_jwt:
        ts, tok, pl = best_jwt
        exp = datetime.datetime.fromtimestamp(pl["exp"])
        open(os.path.join(BASE, "jwt_latest.txt"), "w").write(tok)
        remain = exp - datetime.datetime.now()
        print(f"[jwt] 已更新: 签发 {ts[11:19]}  过期 {exp:%H:%M:%S}  剩余 {remain}")
        if remain.total_seconds() < 0:
            print("[jwt] 警告: 该 JWT 已过期, 请在模拟器里重新打开一次小程序")
    else:
        print("[jwt] 抓包里没找到 oauth2acc-cn JWT (打开一次模拟器里的小程序即可)")
    if waf:
        open(os.path.join(BASE, "waf_token.txt"), "w").write(waf)
        print(f"[waf] 已更新: {waf[:16]}...")
    else:
        print("[waf] 未在 URL 中发现 acw_sc__v2 (沿用旧值)")

if __name__ == "__main__":
    main()
