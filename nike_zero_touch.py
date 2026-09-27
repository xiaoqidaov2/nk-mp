# -*- coding: utf-8 -*-
"""
Nike 门店监控 · 零人工定时引擎 (纯 API 版)
================================================
每小时自动完成（无需任何人工操作、无微信、无模拟器、无 adb、无浏览器*）:
  1. 凭证续签: refresh_token 纯 HTTP 换新 token (浏览器仅在 refresh_token 失效后兜底)
  2. 上新监控: discover/product_wall (匿名)
  3. 门店活动/预览监控
  4. 关注货号只读探针: o2o cart_reviews —— 门店×尺码 库存 + 门店价/折扣/促销 (不动购物车)

用法:
  python nike_zero_touch.py               # 单次
  python nike_zero_touch.py --loop 3600   # 每小时
挂 Windows 计划任务即可长期无人值守。
"""
import subprocess, time, os, sys, argparse

BASE = os.path.dirname(os.path.abspath(__file__))

def auth_refresh():
    """纯 API 凭证续签 (优先 refresh_token 纯HTTP, 失败才拉起浏览器静默授权)"""
    import importlib.util
    spec = importlib.util.spec_from_file_location("nikeauth", os.path.join(BASE, "nike_auth.py"))
    auth = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(auth)
    return auth.full_refresh()

def run_script(name, *extra):
    p = os.path.join(BASE, name)
    if not os.path.exists(p):
        return
    print(f"\n---- 运行 {name} ----")
    try:
        r = subprocess.run([sys.executable, p, *extra], capture_output=True, text=True, timeout=1800)
    except subprocess.TimeoutExpired:
        print(f"[{name}] 超时"); return
    out = ((r.stdout or "") + (r.stderr or ""))[-1500:]
    print(out)
    return out

def run_once(store, codes_file):
    print(f"\n======== {time.strftime('%Y-%m-%d %H:%M:%S')} 零人工轮次 ========")
    # 1. 凭证续签 (纯 API)
    ok = auth_refresh()
    if not ok:
        print("[warn] 凭证刷新未成功, 沿用现有(可能仍有效)")
    # 2. 监控任务
    run_script("nike_new_arrivals.py")
    run_script("nike_store_monitor.py")
    # 3. 门店×尺码 只读探针
    if store and codes_file and os.path.exists(codes_file):
        run_script("nike_store_probe.py", "--store", store, "--codes-file", codes_file)

def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--loop", type=int, default=0, help="轮询秒数 (建议 3600)")
    ap.add_argument("--store", default="3752")
    ap.add_argument("--codes-file", default=os.path.join(BASE, "watch.txt"))
    args = ap.parse_args()
    while True:
        try:
            run_once(args.store, args.codes_file)
        except Exception as e:
            print(f"[engine] 本轮异常: {e}")
        if not args.loop:
            break
        print(f"[engine] 休眠 {args.loop}s ...")
        time.sleep(args.loop)

if __name__ == "__main__":
    main()
