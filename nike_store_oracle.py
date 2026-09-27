# -*- coding: utf-8 -*-
"""
Nike 门店价 + 尺码库存 查询器（直连 API 版）
=====================================================
原理（全部实测验证 2026-09-23）:
  1. 尺码条码(GTIN) 结构数据来自测试环境 product_details（无 ACL, 结构真实）
     GET https://api-test.nike.com.cn/store/product_details/v1/marketplace/CN/language/zh-Hans/
         store_number/{storeCode}/style_color/{styleCode}
  2. 生产购物车「加购带库存校验」= 库存探针（有货才加得进）:
     PATCH https://api.nike.com.cn/o2o/buy/carts/v2/CN/NIKE/WECHAT/{regionId}
           ?modifiers=VALIDATELIMITS,VALIDATEAVAILABILITY
     body: [{"op":"add","path":"/items","value":{"gtin":...,"quantity":1,"itemData":{...}}}]
  3. 购物车读取返回门店价:
     GET   https://api.nike.com.cn/o2o/buy/carts/v2/CN/NIKE/WECHAT/{regionId}
     item.priceInfo = {"price": 门店价, "fullPrice": 原价}
  4. 门店清单: GET https://api.nike.com.cn/o2o/buy/regions/v1?anchor=0&count=50

用法:
  python nike_store_oracle.py --store 3752 --codes IO5031-600,IR2281-100
  python nike_store_oracle.py --store 3752 --codes-file watch.txt   (每行一个货号)
凭证:
  jwt.txt  — 微信小程序 api 域 JWT（1小时有效, 小程序冷启动会刷新）
  waf.txt  — 阿里 WAF 票据 acw_sc__v2（浏览器访问 nike.com.cn 后从 cookie/请求URL取, 长期有效）
"""
import json, os, sys, time, argparse, base64, datetime
import urllib.request, urllib.error, gzip

BASE = os.path.dirname(os.path.abspath(__file__))
UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) "
      "Chrome/144.0.0.0 Safari/537.36 MicroMessenger/7.0.20.1781(0x6700143B) NetType/WIFI "
      "MiniProgramEnv/Windows WindowsWechat/WMPF WindowsWechat(0x63090a13) "
      "UnifiedPCWindowsWechat(0xf2541f0d) XWEB/25715")

def read_file(name, default=""):
    p = os.path.join(BASE, name)
    return open(p, encoding="utf-8").read().strip() if os.path.exists(p) else default

JWT = read_file("jwt_latest.txt")
WAF = read_file("waf_token.txt", "6ab33e628dc22fc2efcc1429cbdf18626b1d3104")

FP = {
    "appid": "com.nike.nvs.comerce.wo.wechat.prod",
    "nike-api-caller-id": "nike:wechat:web:1.0",
    "x-nike-partner": "NDS",
    "x-nike-region": "NIKE0S088",
    "x-nike-ux-id": "com.nike.nvs.comerce.wo.wechat.prod",
    "accept": "application/json",
    "content-type": "application/json; charset=UTF-8",
    "accept-language": "zh-CN,zh;q=0.9",
    "referer": "https://servicewechat.com/wx74b301953dcdd7db/18/page-frame.html",
    "user-agent": UA,
}

SSL_FALLBACK = __import__('ssl')._create_unverified_context()  # 系统代理为 Reqable 时回退用

def http(method, url, body=None, auth=True, waf=True, timeout=25):
    h = dict(FP)
    if auth and JWT: h["Authorization"] = JWT
    if waf and WAF: h["Cookie"] = f"acw_sc__v2={WAF}"
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(url, data=data, headers=h, method=method)
    try:
        r = urllib.request.urlopen(req, timeout=timeout)
        b = r.read()
        if r.headers.get("Content-Encoding") == "gzip": b = gzip.decompress(b)
        return r.status, b
    except urllib.error.HTTPError as ex:
        b = ex.read()
        try: b = gzip.decompress(b)
        except Exception: pass
        return ex.code, b
    except urllib.error.URLError as ue:
        if "CERTIFICATE_VERIFY_FAILED" in str(ue):
            try:
                r = urllib.request.urlopen(req, timeout=timeout, context=SSL_FALLBACK)
                b = r.read()
                if r.headers.get("Content-Encoding") == "gzip": b = gzip.decompress(b)
                return r.status, b
            except urllib.error.HTTPError as ex2:
                b = ex2.read()
                try: b = gzip.decompress(b)
                except Exception: pass
                return ex2.code, b
        raise
    except urllib.error.HTTPError as ex:
        b = ex.read()
        try: b = gzip.decompress(b)
        except Exception: pass
        return ex.code, b

def jwt_remaining():
    if not JWT: return None
    try:
        p = json.loads(base64.urlsafe_b64decode(JWT[7:].split(".")[1] + "=" * (-len(JWT[7:].split(".")[1]) % 4)))
        return datetime.datetime.fromtimestamp(p["exp"]) - datetime.datetime.now()
    except Exception:
        return None

# ---------- 1. GTIN 结构(测试环境, 带缓存) ----------
GTIN_CACHE = os.path.join(BASE, "gtin_cache.json")

def load_cache():
    return json.load(open(GTIN_CACHE, encoding="utf-8")) if os.path.exists(GTIN_CACHE) else {}

def get_size_gtins(store_code, style_code, cache):
    key = f"{store_code}:{style_code}"
    if key in cache and cache[key].get("sizes"):
        return cache[key]
    url = (f"https://api-test.nike.com.cn/store/product_details/v1/marketplace/CN/language/zh-Hans/"
           f"store_number/{store_code}/style_color/{style_code}")
    code, b = http("GET", url, auth=False, waf=False)
    if code != 200:
        return None
    d = json.loads(b)
    prods = d.get("productGroupings", [{}])[0].get("products", [])
    if not prods:
        return None
    prod = prods[0]
    sizes = []
    for s in prod.get("sizes", []):
        g = (s.get("gtins") or [{}])[0].get("gtin")
        if g:
            sizes.append({"size": s.get("localizedLabel"), "gtin": g})
    rec = {"title": (prod.get("itemData") or {}).get("title") or prod.get("labelName") or style_code,
           "sizes": sizes, "fetchedAt": int(time.time())}
    cache[key] = rec
    return rec

# ---------- 2/3. 购物车探针 ----------
def cart_url(region_id):
    return f"https://api.nike.com.cn/o2o/buy/carts/v2/CN/NIKE/WECHAT/{region_id}"

def probe(region_id, watch, region_name="", pace=180, retries=4):
    """watch: [(styleCode, sizeRec)] — 批量加购探测.
    机制: 加购带 VALIDATEAVAILABILITY 修饰符时, 无货尺码会以
    INVALID_QUANTITY/ITEM_QUANTITY_LIMIT 报错 (errors 里带 gtin=...),
    有货尺码成功入车并在购物车里带 priceInfo.
    返回 {styleCode: {...}}
    """
    import re as _re
    all_adds = []
    wanted_gtins = set()
    for sc, rec in watch:
        for s in rec["sizes"]:
            wanted_gtins.add(s["gtin"])
            all_adds.append({"op": "add", "path": "/items",
                             "value": {"gtin": s["gtin"], "quantity": 1,
                                       "itemData": {"title": rec["title"], "subTitle": ""}}})
    # 0) 先读购物车, 清掉本清单的残留(购物车有总容量上限)
    rd_code, rd_b = http("GET", cart_url(region_id) + "?acw_sc__v2=" + WAF)
    if rd_code == 200:
        cur = json.loads(rd_b)
        residue = [it["id"] for it in cur.get("items", []) if it.get("gtin") in wanted_gtins]
        if residue:
            print(f"  清理残留 {len(residue)} 件...")
            time.sleep(pace)
            http("PATCH", cart_url(region_id) + "?acw_sc__v2=" + WAF,
                 [{"op": "remove", "path": "/items", "value": {"id": i}} for i in residue])
            time.sleep(pace)
    cart = None
    oos_gtins = set()
    pending = all_adds
    for attempt in range(retries):
        if not pending:
            break
        batch = pending[:4]
        url = cart_url(region_id) + "?modifiers=VALIDATELIMITS,VALIDATEAVAILABILITY&acw_sc__v2=" + WAF
        code, b = http("PATCH", url, batch)
        if code == 200:
            cart = json.loads(b)
            merged = {it["gtin"]: it for it in (cart or {}).get("items", [])}
            pending = pending[4:]
            if not pending:
                break
            time.sleep(pace)
            continue
        if code == 405:
            wait = pace * (attempt + 1)
            print(f"  WAF 限速(405), 等待 {wait}s...")
            time.sleep(wait)
            continue
        if code == 401:
            print("  JWT 过期(401): 请打开一次微信小程序刷新, 更新 jwt_latest.txt")
            return None
        if code == 400:
            txt = b.decode("utf-8", "replace")
            bad = set(_re.findall(r"gtin=(\d+)", txt))
            if bad:
                oos_gtins |= bad
                pending = [a for a in pending if a["value"]["gtin"] not in oos_gtins]
                print(f"  无货尺码 {len(bad)} 个, 剔除后重试 {len(pending)} 个...")
                time.sleep(pace)
                continue
        print(f"  PATCH 失败 HTTP {code}: {b[:200].decode('utf-8','replace')}")
        return None

    by_gtin = {}
    if cart:
        by_gtin = {it["gtin"]: it for it in cart.get("items", [])}

    result = {}
    for sc, rec in watch:
        avail, prices = [], None
        for s in rec["sizes"]:
            it = by_gtin.get(s["gtin"])
            if it:
                avail.append(s["size"])
                if prices is None and it.get("priceInfo"):
                    prices = it["priceInfo"]
        result[sc] = {"title": rec["title"], "store": region_name,
                      "price": (prices or {}).get("price"),
                      "fullPrice": (prices or {}).get("fullPrice"),
                      "sizesInStock": avail, "sizesTotal": len(rec["sizes"]),
                      "checkedAt": datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")}
    # 清理: 移除本次加入的
    time.sleep(pace)
    rm = [{"op": "remove", "path": "/items", "value": {"id": it["id"]}}
          for it in by_gtin.values() if it["gtin"] in wanted_gtins]
    if rm:
        c2, _ = http("PATCH", cart_url(region_id) + "?acw_sc__v2=" + WAF, rm)
        print(f"  清理 {len(rm)} 件: HTTP {c2}")
    return result

def diff_and_report(state_file, new_state):
    old = json.load(open(state_file, encoding="utf-8")) if os.path.exists(state_file) else {}
    for sc, n in new_state.items():
        o = old.get(sc)
        if not o:
            print(f"[新增] {sc} {n['title']}: 门店价 ¥{n['price']} (原价 ¥{n['fullPrice']}) "
                  f"有货尺码 {len(n['sizesInStock'])}/{n['sizesTotal']}")
        else:
            if o.get("price") != n.get("price"):
                print(f"[价格变动] {sc}: ¥{o.get('price')} -> ¥{n.get('price')}")
            add = [s for s in n["sizesInStock"] if s not in (o.get("sizesInStock") or [])]
            rem = [s for s in (o.get("sizesInStock") or []) if s not in n["sizesInStock"]]
            for s in add: print(f"[补货] {sc} 尺码{s} 有货")
            for s in rem: print(f"[断码] {sc} 尺码{s} 无货")
    json.dump(new_state, open(state_file, "w", encoding="utf-8"), ensure_ascii=False, indent=1)

def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--store", required=True, help="门店号(storeCode), 如 3752")
    ap.add_argument("--region", default="", help="区域id(如 NIKE0S088), 默认按门店号查regions自动匹配")
    ap.add_argument("--codes", default="", help="货号, 逗号分隔")
    ap.add_argument("--codes-file", default="", help="关注清单文件, 每行一个货号")
    ap.add_argument("--pace", type=int, default=95, help="写操作间隔秒(默认95)")
    args = ap.parse_args()

    rem = jwt_remaining()
    print(f"JWT 剩余: {rem}")
    codes = [c.strip() for c in args.codes.split(",") if c.strip()]
    if args.codes_file:
        codes += [l.strip() for l in open(args.codes_file, encoding="utf-8") if l.strip()]
    if not codes:
        print("没有货号"); return

    # 找 region id
    region_id = args.region
    region_name = ""
    if not region_id:
        code, b = http("GET", "https://api.nike.com.cn/o2o/buy/regions/v1?anchor=0&count=100")
        if code == 200:
            for o in json.loads(b).get("objects", []):
                if str(o.get("storeCode")) == str(args.store):
                    region_id, region_name = o["id"], o.get("displayName", "")
                    break
    if not region_id:
        print(f"未找到门店 {args.store} 的 region id, 用 --region 指定"); return
    print(f"门店: {region_name or args.store} region={region_id}")

    cache = load_cache()
    watch = []
    for sc in codes:
        rec = get_size_gtins(args.store, sc, cache)
        if rec and rec["sizes"]:
            watch.append((sc, rec))
            print(f"  {sc}: {len(rec['sizes'])} 个尺码, GTIN 就绪")
        else:
            print(f"  {sc}: 测试环境无此货号结构数据")
        time.sleep(1.5)
    json.dump(cache, open(GTIN_CACHE, "w", encoding="utf-8"), ensure_ascii=False, indent=1)
    if not watch:
        print("没有可探测的货号"); return

    result = probe(region_id, watch, region_name, pace=args.pace)
    if result:
        diff_and_report(os.path.join(BASE, f"oracle_state_{args.store}.json"), result)

if __name__ == "__main__":
    main()
