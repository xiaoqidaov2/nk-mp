# -*- coding: utf-8 -*-
"""
Nike 中国门店折扣监控（直连部分，已实测可用）
================================================
数据来源: GET https://api.nike.com.cn/store/home/v1/storeNumber/{门店号}
  - 实测无需登录、无需 Authorization，只要带对指纹头即可 200
  - 返回该店「满减专区」活动规则（55折/7折等）+ 每期 10 件预览商品码
轮询对比 state.json，输出活动与商品预览的变动。

用法:
  python nike_store_monitor.py            # 跑一轮并对比
  python nike_store_monitor.py --loop     # 持续轮询（默认 10 分钟一轮）
"""
import requests, json, os, sys, time, random, datetime

STORE_NUMBERS = ["3752"]          # 目标门店号（可加多个，如 ["3752","3875"]）
POLL_INTERVAL = 600               # 轮询间隔(秒)
STATE_FILE    = os.path.join(os.path.dirname(os.path.abspath(__file__)), "state.json")

# ---- 实测可通过 CDN ACL 的最小指纹头（2026-09-23 验证）----
HEADERS = {
    "appid":            "com.nike.nvs.comerce.wo.wechat.prod",
    "nike-api-caller-id": "nike:wechat:web:1.0",
    "x-nike-partner":   "NDS",
    "x-nike-ux-id":     "com.nike.nvs.comerce.wo.wechat.prod",
    "x-nike-region":    "NIKE0S088",
    "xweb_xhr":         "1",
    "referer":          "https://servicewechat.com/wx74b301953dcdd7db/18/page-frame.html",
    "user-agent":       ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                         "(KHTML, like Gecko) Chrome/144.0.0.0 Safari/537.36 "
                         "MicroMessenger/7.0.20.1781(0x6700143B) NetType/WIFI "
                         "MiniProgramEnv/Windows WindowsWechat/WMPF "
                         "WindowsWechat(0x63090a13) UnifiedPCWindowsWechat(0xf2541f0d) XWEB/25715"),
    "accept":           "application/json",
    "content-type":     "application/json; charset=UTF-8",
    "accept-language":  "zh-CN,zh;q=0.9",
}

def diagnose(resp):
    """三层结构自诊断: CDN ACL -> 鉴权 -> 路由"""
    t = resp.text[:150]
    if "custom_acl" in t or "Tengine" in t:
        return "被CDN边缘ACL拦截(路径不在放行名单——该路径只能走微信云通道)"
    if resp.status_code == 401:
        return "过ACL但被鉴权层拦截(需有效JWT)"
    if "Missing Authentication Token" in t:
        return "路由不存在或路径写错(AWS API Gateway特征)"
    return f"HTTP {resp.status_code}: {t[:100]}"

def _get(url):
    # 系统代理可能是 Reqable(自签CA) → 证书校验失败时回退
    try:
        return requests.get(url, headers=HEADERS, timeout=15)
    except requests.exceptions.SSLError:
        import urllib3; urllib3.disable_warnings()
        return requests.get(url, headers=HEADERS, timeout=15, verify=False)

def get_store_home(sn):
    url = f"https://api.nike.com.cn/store/home/v1/storeNumber/{sn}"
    r = _get(url)
    if r.status_code != 200:
        raise RuntimeError(f"店{sn}: {diagnose(r)}")
    return r.json()

def snapshot_store(sn):
    """提取门店快照: 门店名/地址 + 活动规则 + 预览商品码"""
    data = get_store_home(sn)
    out = {"fetchedAt": int(time.time()), "nodes": {}}
    for node in data.get("nodes", []):
        t = node.get("type")
        p = node.get("properties") or {}
        if t == "STORE_PROMOTION":
            out["nodes"]["promotion"] = {
                "title":   p.get("title", "").strip(),
                "subtitle": p.get("subtitle", "").strip(),
                "products": [x.get("productCode") for x in p.get("products", [])],
            }
        elif t == "MEMBER_LOGIN":
            out["nodes"]["memberLogin"] = {"title": p.get("title", "").strip()}
    return out

def diff_store(sn, old, new):
    """对比并打印变动"""
    lines = []
    o, n = (old or {}).get("nodes", {}), new.get("nodes", {})
    po, pn = o.get("promotion") or {}, n.get("promotion") or {}
    if po.get("title") != pn.get("title") or po.get("subtitle") != pn.get("subtitle"):
        lines.append(f"[活动变更] {po.get('title')}/{po.get('subtitle')} -> {pn.get('title')}/{pn.get('subtitle')}")
    co, cn = po.get("products") or [], pn.get("products") or []
    added   = [c for c in cn if c not in co]
    removed = [c for c in co if c not in cn]
    for c in added:   lines.append(f"[预览+入] {c}")
    for c in removed: lines.append(f"[预览-出] {c}")
    return lines

def run_once(state):
    all_lines = []
    for sn in STORE_NUMBERS:
        try:
            snap = snapshot_store(sn)
        except Exception as e:
            print(f"店{sn} 采集失败: {e}")
            continue
        key = f"store_{sn}"
        lines = diff_store(sn, state.get(key), snap)
        for l in lines: print(f"  店{sn} {l}")
        state[key] = snap
        print(f"  店{sn} 当前: {snap['nodes'].get('promotion',{}).get('title')} "
              f"预览{len(snap['nodes'].get('promotion',{}).get('products',[]))}件")
    return state

if __name__ == "__main__":
    sp = os.path.join(os.path.dirname(os.path.abspath(__file__)), "state.json")
    state = json.load(open(sp, encoding="utf-8")) if os.path.exists(sp) else {}
    if "--loop" in sys.argv:
        while True:
            print(f"\n== {time.strftime('%H:%M:%S')} ==")
            state = run_once(state)
            json.dump(state, open(sp, "w", encoding="utf-8"), ensure_ascii=False, indent=1)
            time.sleep(POLL_INTERVAL + random.uniform(-30, 30))
    else:
        state = run_once(state)
        json.dump(state, open(sp, "w", encoding="utf-8"), ensure_ascii=False, indent=1)
