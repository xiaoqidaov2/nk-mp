# -*- coding: utf-8 -*-
"""
Nike 门店库存价格探针 v2  ——  只读 · 纯 HTTP/2 · 零购物车 · 零微信
=================================================================
接口: PUT /o2o/buy/cart_reviews/v2/{uuid}   (只读审核, 不动购物车)
      GET /o2o/buy/cart_reviews/v2/{uuid}   (轮询取结果)
门店: 由请求头 x-nike-region = {门店regionId} 决定
机制: - 一次提交多件 (所有尺码的所有货号) → 返回不可买清单 (INVENTORY_INSUFFICIENT)
        → 未被列出的即"有货" (排除法)
      - 对有货子集再审核一次 → 拿 priceInfo (标价/折扣/实付) + 促销名
鉴权: jwt_latest.txt (nike_auth.py 维护; 剩余<5分钟自动续)
依赖: httpx (http2), 无需 Cookie / 无需浏览器 / 无需 WAF 票据

用法:
  python nike_store_probe.py --store 3752                # watch.txt 里的货号
  python nike_store_probe.py --store 3752 --code IO5031-600
  python nike_store_probe.py --region xian --code IO5031-600
  python nike_store_probe.py --list-stores               # 刷新门店清单
"""
import httpx, json, os, sys, time, uuid as U, base64, argparse, importlib.util

BASE = os.path.dirname(os.path.abspath(__file__))
JWT_FILE = os.path.join(BASE, "jwt_latest.txt")
GTIN_CACHE = os.path.join(BASE, "gtin_cache.json")
STORES_FILE = os.path.join(BASE, "all_stores.json")
WATCH_FILE = os.path.join(BASE, "watch.txt")
UA_DEFAULT = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
              "(KHTML, like Gecko) Chrome/144.0.0.0 Safari/537.36")
CHUNK = 40            # 单次审核最多提交商品数
POLL_TRIES = 12
POLL_WAIT = 2.5

def load_ua():
    f = os.path.join(BASE, "ua.txt")
    return open(f).read().strip() if os.path.exists(f) else UA_DEFAULT

def load_jwt(auto_refresh=True, force=False):
    at = open(JWT_FILE).read().strip() if os.path.exists(JWT_FILE) else ""
    if at and not force:
        try:
            p = at.split(".")[1]; p += "=" * (-len(p) % 4)
            exp = json.loads(base64.urlsafe_b64decode(p)).get("exp", 0)
            if exp - time.time() > 300:
                return at
            if not auto_refresh:
                return at
        except Exception:
            return at
    print("[auth] 调 nike_auth 续期...", flush=True)
    spec = importlib.util.spec_from_file_location("nikeauth", os.path.join(BASE, "nike_auth.py"))
    auth = importlib.util.module_from_spec(spec); spec.loader.exec_module(auth)
    auth.full_refresh("mp")
    return open(JWT_FILE).read().strip()

def H(region, jwt):
    return {"Authorization": f"Bearer {jwt}", "appid": "com.nike.nvs.comerce.wo.wechat.prod",
            "nike-api-caller-id": "nike:wechat:web:1.0", "x-nike-partner": "NDS",
            "x-nike-region": region, "accept": "application/json",
            "accept-language": "zh-CN,zh;q=0.9",
            "referer": "https://servicewechat.com/wx74b301953dcdd7db/18/page-frame.html",
            "user-agent": load_ua(), "content-type": "application/json; charset=UTF-8"}

class Prober:
    def __init__(self, region):
        self.region = region
        self.jwt = load_jwt()
        self.c = httpx.Client(http2=True, verify=False, timeout=30)

    def req(self, method, url, body=None, tries=7, region=None):
        """WAF 间歇挑战(405)自动重试; JWT 过期(401)自动续期; 网络抖动自动重试"""
        auth_retries, i = 0, 0
        r = None
        while i < tries:
            try:
                r = self.c.request(method, url, headers=H(region or self.region, self.jwt),
                                   content=json.dumps(body) if body is not None else None)
            except httpx.TransportError as e:
                print(f"  [net] {type(e).__name__}, 重试", flush=True)
                time.sleep(1.5 + i * 1.0)
                i += 1
                continue
            if r.status_code == 401 and auth_retries < 2:
                auth_retries += 1
                print("  [auth] 401 → 自动续期重试", flush=True)
                self.jwt = load_jwt(force=True)
                continue
            if not (r.status_code == 405 and b"data-spm" in r.content):
                return r
            time.sleep(2 + i * 1.5)
            i += 1
        return r

    def review(self, gtins, region=None, wait=None):
        """gtins: [gtin, ...] → 返回 (avail_set, info_dict)
           info 仅在全部可买时有 (含价格)"""
        items = [{"id": str(U.uuid4()), "skuId": g, "quantity": 1, "valueAddedServices": [],
                  "fulfillmentDetails": {"type": "SHIP", "location": {"type": "address/shipping",
                                                                      "postalAddress": {"country": "CN"}}}}
                 for g in gtins]
        body = {"request": {"country": "CN", "currency": "CNY", "locale": "zh_CN",
                            "items": items, "promotionCodes": []}}
        rid = str(U.uuid4())
        base = f"https://api.nike.com.cn/o2o/buy/cart_reviews/v2/{rid}"
        r = self.req("PUT", base, body, region=region)
        if r.status_code not in (200, 201, 202):
            print(f"  [review] PUT {r.status_code} {r.text[:120]}")
            return None, None
        for _ in range(POLL_TRIES):
            time.sleep(wait or POLL_WAIT)
            r = self.req("GET", base, region=region)
            try: d = r.json()
            except Exception: continue
            if "response" in d or "error" in d:
                err = d.get("error")
                groups = (d.get("response") or {}).get("fulfillmentGroups", [])
                if not err and not groups:
                    # 空完成态(无错误也无明细) → 不可信, 继续轮询; 轮满按失败处理
                    continue
                bad = set()
                if err:
                    for e in err.get("errors", []):
                        f = e.get("field", "")
                        c = e.get("code", "")
                        # 件数超限(上限3件)不算缺货; 仅 INVENTORY 类错误才是缺货
                        if "/items/" in f and c != "ITEM_QUANTITY_LIMIT":
                            try: bad.add(int(f.split("/items/")[1].split("/")[0]))
                            except Exception: pass
                    avail = {g for i, g in enumerate(gtins) if i not in bad}
                else:
                    # 无错误 → 可用项 = 实际返回明细的项
                    avail = {it["skuId"] for g in groups for it in g.get("items", [])}
                info = {}
                for g in groups:
                    for it in g.get("items", []):
                        info[it["skuId"]] = it
                return avail, info
        return None, None

def load_json(f, default):
    return json.load(open(f, encoding="utf-8")) if os.path.exists(f) else default

def ensure_gtins(store_code, style_code, cache):
    key = f"{store_code}:{style_code}"
    if key in cache and cache[key].get("sizes"):
        return cache[key]
    url = (f"https://api-test.nike.com.cn/store/product_details/v1/marketplace/CN/language/zh-Hans/"
           f"store_number/{store_code}/style_color/{style_code}")
    try:
        r = httpx.get(url, headers={"user-agent": load_ua(), "accept": "application/json"},
                      verify=False, timeout=20)
        d = r.json()
        prods = (d.get("productGroupings") or [{}])[0].get("products", [])
        if not prods:
            return None
        prod = prods[0]
        sizes = []
        for s in prod.get("sizes", []):
            g = (s.get("gtins") or [{}])[0].get("gtin")
            if g:
                sizes.append({"size": s.get("localizedLabel"), "gtin": g})
        title = ((prod.get("copy") or {}).get("fullTitle") or (prod.get("itemData") or {}).get("title")
                 or style_code)
        rec = {"title": title, "sizes": sizes, "fetchedAt": int(time.time())}
        cache[key] = rec
        return rec
    except Exception as e:
        print(f"  [{style_code}] GTIN 获取失败: {e}")
        return None

def resolve_store(args, stores):
    if args.region:
        return args.region, ""
    code = str(args.store)
    for s in stores:
        if str(s.get("storeCode")) == code or s.get("id") == code:
            return s["id"], s.get("displayName", "")
    return None, None

def probe_store(region, region_name, codes, store_code):
    pr = Prober(region)
    cache = load_json(GTIN_CACHE, {})
    # 收集 GTIN → (code, size)
    plan, missing = [], []
    for sc in codes:
        rec = ensure_gtins(store_code, sc, cache)
        if rec and rec["sizes"]:
            for s in rec["sizes"]:
                plan.append((s["gtin"], sc, s["size"]))
        else:
            missing.append(sc)
    if not plan:
        print("没有可探测的货号(GTIN 结构缺失)"); return None
    json.dump(cache, open(GTIN_CACHE, "w", encoding="utf-8"), ensure_ascii=False, indent=1)
    print(f"门店: {region_name or store_code} region={region} | 待测 {len(plan)} 个尺码"
          + (f" | 缺结构: {missing}" if missing else ""))

    # Phase A: 全量可用性 (排除法)
    avail_all, infos = set(), {}
    for i in range(0, len(plan), CHUNK):
        chunk = plan[i:i+CHUNK]
        gtins = [x[0] for x in chunk]
        print(f"  审核 {i+1}~{i+len(chunk)} ...")
        av, info = pr.review(gtins)
        if av is None:
            print("   失败"); continue
        avail_all |= av
        infos.update(info)
        time.sleep(1.5)

    # Phase B: 价格 —— 必须在"全可买"的 ≤3 件小批次里才返回价格
    # 审核是"全可买才返回价格"语义, 混入已失效项/超3件则整体报错 → 迭代收缩
    for attempt in range(4):
        missing = sorted(g for g in avail_all if g not in infos)
        if not missing:
            break
        print(f"  Phase B{attempt+1}: 取 {len(missing)} 个有货尺码的价格 (每组≤3件)...")
        got_any = False
        for j in range(0, len(missing), 3):
            group = missing[j:j+3]
            av2, info2 = pr.review(group)
            if info2:
                infos.update(info2); got_any = True
            elif av2 is not None:
                # 剔除批次中已失效的项
                lost = [g for g in group if g not in av2]
                if lost:
                    print(f"    剔除已失效: {lost}")
                    avail_all -= set(lost)
            time.sleep(1.2)
        if not got_any:
            break

    # 汇总
    by_code = {}
    for g, sc, size in plan:
        rec = by_code.setdefault(sc, {"sizesInStock": [], "sizesTotal": 0})
        rec["sizesTotal"] += 1
        if g in avail_all:
            rec["sizesInStock"].append(size)
    for sc, rec in by_code.items():
        title = (cache.get(f"{store_code}:{sc}") or {}).get("title", sc)
        rec["title"] = title
        p = None
        for g, s2, size in plan:
            if s2 == sc and g in infos:
                pc = infos[g].get("itemCosts", {}).get("priceInfo")
                if pc:
                    p = {"price": pc.get("price"), "total": pc.get("total"), "discount": pc.get("discount"),
                         "promos": [x.get("displayName") for x in
                                    infos[g].get("itemCosts", {}).get("promotionDiscounts", [])]}
                    break
        rec["price"] = p
        rec["checkedAt"] = time.strftime("%Y-%m-%d %H:%M:%S")
    return by_code

def show(by_code, store_code):
    print()
    for sc, r in by_code.items():
        line = f"《{r['title']}》 {sc}: 有货 {len(r['sizesInStock'])}/{r['sizesTotal']} 码"
        if r["sizesInStock"]:
            line += f" → {r['sizesInStock']}"
        p = r.get("price")
        if p:
            line += f" | ¥{p['price']:g}"
            if p.get("discount"):
                line += f"(促销-{p['discount']:g} → 实付¥{p['total']:g} {','.join(x for x in p['promos'] if x)})"
        print(line)

def diff_and_save(by_code, store_code):
    f = os.path.join(BASE, f"probe_state_{store_code}.json")
    old = load_json(f, {})
    changes = []
    for sc, r in by_code.items():
        o = old.get(sc)
        if o:
            new_in = set(r["sizesInStock"]) - set(o.get("sizesInStock", []))
            gone = set(o.get("sizesInStock", [])) - set(r["sizesInStock"])
            op, np_ = o.get("price") or {}, r.get("price") or {}
            if new_in: changes.append(f"  ★ {sc} 补货: {sorted(new_in)}")
            if gone: changes.append(f"  ✗ {sc} 售罄: {sorted(gone)}")
            if op.get("total") != np_.get("total"):
                changes.append(f"  ¥ {sc} 变价: {op.get('total')} → {np_.get('total')}")
        else:
            changes.append(f"  ＋ {sc} 新监控 {len(r['sizesInStock'])}/{r['sizesTotal']} 码有货")
    old.update(by_code)
    json.dump(old, open(f, "w", encoding="utf-8"), ensure_ascii=False, indent=1)
    if changes:
        print("\n变化:")
        print("\n".join(changes))
    return changes

def scan_stores(pr, codes, cache, pace=0.8, skip=0):
    """全国门店扫描: 对每个门店跑一次审核, 找出哪些店有这些货号
    注意: api-test 的 GTIN 结构可能按门店不同(实测 DN3707-202 有两套), 
          必须逐店取结构, 否则会漏报/误报"""
    raw = load_json(os.path.join(BASE, "regions_h2.json"), None)
    stores = (raw.get("objects") if isinstance(raw, dict) else raw) or load_json(STORES_FILE, [])
    if not stores:
        print("没有门店清单, 先跑 --list-stores"); return
    print(f"全国扫描 {len(stores)} 家门店 | 货号: {codes}")
    hits, failed, no_struct = {}, 0, []
    for i, s in enumerate(stores):
        if i < skip: continue
        region, name, sn = s.get("id"), s.get("displayName", ""), s.get("storeCode")
        # 每店用自己的结构
        plan = []
        for sc in codes:
            rec = ensure_gtins(sn or "3752", sc, cache)
            if rec and rec["sizes"]:
                for x in rec["sizes"]:
                    plan.append((x["gtin"], sc, x["size"]))
            elif sc not in no_struct:
                no_struct.append(sc)
        if not plan:
            print(f"  [{i+1}/{len(stores)}] {name}: 无结构, 跳过")
            continue
        gtins = [g for g, _, _ in plan]
        av, _ = pr.review(gtins, region=region, wait=1.2)
        if av is None:
            failed += 1
            print(f"  [{i+1}/{len(stores)}] {name}: 审核失败")
        else:
            by = {}
            for g, sc, size in plan:
                if g in av:
                    by.setdefault(sc, []).append(size)
            if by:
                hits[f"{region}"] = {"name": name, "storeCode": sn, "by": by}
                txt = " | ".join(f"{sc}: {','.join(x for x in sizes if x)}" for sc, sizes in by.items())
                print(f"  [{i+1}/{len(stores)}] ★ {name}: {txt}")
            elif (i + 1) % 10 == 0:
                print(f"  [{i+1}/{len(stores)}] ...已扫, 暂无新增")
        time.sleep(pace)
    json.dump(cache, open(GTIN_CACHE, "w", encoding="utf-8"), ensure_ascii=False, indent=1)
    print(f"\n===== 汇总: {len(hits)}/{len(stores)} 家门店有货"
          + (f" ({failed} 家审核失败)" if failed else "")
          + (f" | 无结构货号: {no_struct}" if no_struct else "") + " =====")
    for r_, h in hits.items():
        for sc, sizes in h["by"].items():
            print(f"  {h['name']} (店{h['storeCode']}): {sc} → {sizes}")
    out = os.path.join(BASE, f"scan_{time.strftime('%m%d_%H%M')}.json")
    json.dump(hits, open(out, "w", encoding="utf-8"), ensure_ascii=False, indent=1)
    print(f"结果已存: {out}")
    return hits

def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--store", help="门店号(如 3752)或 regionId")
    ap.add_argument("--region", help="直接指定 regionId (如 NIKE0S088 / xian)")
    ap.add_argument("--code", action="append", help="货号(可多次)")
    ap.add_argument("--codes-file", default=WATCH_FILE)
    ap.add_argument("--scan", action="store_true", help="全国门店扫描(找哪些店有货)")
    ap.add_argument("--pace", type=float, default=0.8, help="扫描时每店间隔秒")
    ap.add_argument("--skip", type=int, default=0, help="扫描时跳过前N家(断点续扫)")
    ap.add_argument("--list-stores", action="store_true")
    args = ap.parse_args()

    if args.list_stores:
        pr = Prober("NIKE0S088")
        r = pr.req("GET", "https://api.nike.com.cn/o2o/buy/regions/v1?anchor=0&count=100")
        if r.status_code == 200:
            json.dump(r.json(), open(STORES_FILE, "w", encoding="utf-8"), ensure_ascii=False, indent=1)
            objs = r.json().get("objects", [])
            print(f"✓ 门店清单已更新: {len(objs)} 家")
            for s in objs[:5]:
                print(f"  {s.get('storeCode')} {s.get('displayName')} region={s.get('id')}")
        else:
            print("获取失败:", r.status_code)
        return

    codes = args.code or [l.strip() for l in open(args.codes_file, encoding="utf-8")
                          if l.strip() and not l.startswith("#")] if os.path.exists(args.codes_file) else args.code
    if not codes:
        print("没有货号"); return
    if args.scan:
        pr = Prober("NIKE0S088")
        scan_stores(pr, codes, load_json(GTIN_CACHE, {}), pace=args.pace, skip=args.skip)
        return

    stores = load_json(STORES_FILE, [])
    region, name = resolve_store(args, stores)
    if not region:
        print(f"未找到门店 {args.store}; 先跑 --list-stores"); return
    by_code = probe_store(region, name, codes, args.store or region)
    if by_code:
        show(by_code, args.store)
        diff_and_save(by_code, args.store)

if __name__ == "__main__":
    main()
