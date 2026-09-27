# -*- coding: utf-8 -*-
"""
Nike 上新监控（纯公开接口，零鉴权）
================================================
原理:
  1. 在线新品墙: GET api.nike.com.cn/discover/product_wall/v1/...?path=/w/new-3n82y&anchor=..&count=50
     (无需 JWT/WAF, 公开; 每件带 标题/现价/原价/折扣率/PDP链接)
  2. diff 出新货号 → 可选: 用门店 oracle 探测这些新货号在你门店是否到货(有货+门店价)
用法:
  python nike_new_arrivals.py                      # 只看在线上新
  python nike_new_arrivals.py --store 3752 --region NIKE0S088   # 上新+门店到货探测
  python nike_new_arrivals.py --loop 600           # 每10分钟
"""
import json, os, sys, time, argparse, urllib.request, urllib.parse, gzip

BASE = os.path.dirname(os.path.abspath(__file__))
STATE = os.path.join(BASE, "new_arrivals_state.json")
CHANNEL = "d9a5bc42-4b9c-4976-858a-f159cf99c647"     # 官方 web 渠道
PATHS = ["/w/new-3n82y"]                              # 新品总入口 (可加更多分类)
MAX_PAGES = 6

HDRS = {
    "accept": "application/json",
    "user-agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/144.0.0.0 Safari/537.36",
    "nike-api-caller-id": "nike:dotcom:web:1.0",
    "referer": "https://www.nike.com.cn/",
}

def http_get(url):
    req = urllib.request.Request(url, headers=HDRS, method="GET")
    r = urllib.request.urlopen(req, timeout=25)
    b = r.read()
    if r.headers.get("Content-Encoding") == "gzip":
        b = gzip.decompress(b)
    return json.loads(b)

def wall_url(path, anchor):
    return ("https://api.nike.com.cn/discover/product_wall/v1/marketplace/CN/language/zh-Hans/"
            f"consumerChannelId/{CHANNEL}?path={urllib.parse.quote(path)}"
            f"&queryType=PRODUCTS&anchor={anchor}&count=50")

def fetch_new_products():
    """拉新品墙所有页, 返回 {货号: {title, price, fullPrice, discount, url}}"""
    out = {}
    for path in PATHS:
        anchor = 0
        for _ in range(MAX_PAGES):
            try:
                d = http_get(wall_url(path, anchor))
            except Exception as e:
                print(f"  [{path} anchor={anchor}] 失败: {e}")
                break
            found = 0
            for g in (d.get("productGroupings") or []):
                for p in (g.get("products") or []):
                    code = p.get("productCode")
                    if not code:
                        continue
                    pr = p.get("prices") or {}
                    out[code] = {
                        "title": (p.get("copy") or {}).get("title", ""),
                        "subTitle": (p.get("copy") or {}).get("subTitle", ""),
                        "price": pr.get("currentPrice"),
                        "fullPrice": pr.get("initialPrice"),
                        "discount": pr.get("discountPercentage"),
                        "url": (p.get("pdpUrl") or {}).get("url", ""),
                    }
                    found += 1
            nxt = (d.get("pages") or {}).get("next")
            print(f"  [{path} anchor={anchor}] 本页 {found} 件, 累计 {len(out)}")
            if not nxt:
                break
            anchor += 50
            time.sleep(2)
    return out

def store_probe(codes, store, region, only_avail=True):
    """用门店 oracle 探测新货号是否到店 (复用 nike_store_oracle 的机制)"""
    sys.path.insert(0, BASE)
    import importlib.util
    spec = importlib.util.spec_from_file_location("oracle", os.path.join(BASE, "nike_store_oracle.py"))
    oracle = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(oracle)
    cache = oracle.load_cache()
    watch = []
    for c in codes[:20]:                    # 单次上限 20 个货号, 保护 WAF
        rec = oracle.get_size_gtins(store, c, cache)
        if rec and rec["sizes"]:
            watch.append((c, rec))
        time.sleep(1.5)
    json.dump(cache, open(oracle.GTIN_CACHE, "w", encoding="utf-8"), ensure_ascii=False, indent=1)
    if not watch:
        return {}
    res = oracle.probe(region, watch, f"store {store}", pace=180)
    return res or {}

def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--store", default="")
    ap.add_argument("--region", default="")
    ap.add_argument("--loop", type=int, default=0, help="轮询秒数, 0=单次")
    args = ap.parse_args()
    while True:
        print(f"\n== {time.strftime('%Y-%m-%d %H:%M:%S')} 拉取新品墙 ==")
        cur = fetch_new_products()
        old = json.load(open(STATE, encoding="utf-8")) if os.path.exists(STATE) else {}
        new_codes = [c for c in cur if c not in old]
        print(f"\n共 {len(cur)} 件新品; 本次新增 {len(new_codes)} 件")
        for c in new_codes:
            x = cur[c]
            print(f"  [上新] {c} {x['title']} ¥{x['price']} (原价¥{x['fullPrice']}) {x['url']}")
        # 门店到货探测(可选)
        if new_codes and args.store and args.region:
            print(f"\n== 探测门店 {args.store} 是否到货 ==")
            res = store_probe(new_codes, args.store, args.region)
            for c, r in (res or {}).items():
                if r.get("sizesInStock"):
                    print(f"  ★[到店] {c} {r['title']} 门店价¥{r['price']} 有货尺码{len(r['sizesInStock'])}个")
        json.dump(cur, open(STATE, "w", encoding="utf-8"), ensure_ascii=False, indent=1)
        if not args.loop:
            break
        time.sleep(args.loop)

if __name__ == "__main__":
    main()
