# -*- coding: utf-8 -*-
"""
模拟器商品墙采集器（adb 驱动，零侵入）
================================================
原理: 模拟器里的小程序商品墙 → adb 滑动 + 截图 → 读取商品名/门店价/原价/分类。
      比加购探针更全（拿得到全量清单），且完全不碰购物车。
前提:
  1. MuMu 模拟器运行中, 微信登录着 Nike 直营门店小程序
  2. 小程序停在「商品墙」页面 (满减专区 → 全部商品)
  3. Reqable Android 的悬浮窗关闭 (避免遮挡商品卡价格)
用法:
  python nike_emulator_harvest.py --pages 30 --out shots/
  然后对 shots/*.png 做 OCR 或人工/模型读取。
  已内置可选 OCR: 若安装 paddleocr 会自动识别并输出 JSON。
"""
import subprocess, os, sys, time, argparse, glob, json

ADB = r"C:\Program Files\Netease\MuMu\nx_main\adb.exe"
DEFAULT_OUT = os.path.join(os.path.dirname(os.path.abspath(__file__)), "shots")

def adb(*args, timeout=60):
    return subprocess.run([ADB, *args], capture_output=True, timeout=timeout).stdout

def screencap(path):
    with open(path, "wb") as f:
        f.write(adb("exec-out", "screencap", "-p", timeout=30))

def swipe_up(px=1000, ms=300):
    # 从下往上滑 (x固定, y从1300到1300-px)
    adb("shell", f"input swipe 450 1400 450 {1400-px} {ms}")

def harvest(pages, out_dir, interval=1.2):
    os.makedirs(out_dir, exist_ok=True)
    print(f"[harvest] 开始采集 {pages} 屏 -> {out_dir}")
    for i in range(pages):
        p = os.path.join(out_dir, f"wall_{i:03d}.png")
        screencap(p)
        print(f"  [{i+1}/{pages}] {os.path.basename(p)} ({os.path.getsize(p)//1024}KB)")
        swipe_up()
        time.sleep(interval)
    print("[harvest] 采集完成")

# ---------- 可选 OCR ----------
def ocr_all(shots_dir):
    """若装了 paddleocr 则自动识别; 输出 products.json"""
    try:
        from paddleocr import PaddleOCR
    except ImportError:
        print("[ocr] 未安装 paddleocr (pip install paddleocr paddlepaddle), 跳过; 图片已保存可供人工/模型读取")
        return
    ocr = PaddleOCR(use_angle_cls=False, lang="ch")
    products = {}
    for f in sorted(glob.glob(os.path.join(shots_dir, "*.png"))):
        res = ocr.ocr(f, cls=False)
        lines = []
        for block in (res or []):
            for item in (block or []):
                txt = item[1][0]
                lines.append(txt)
        # 解析: 商品名 / 分类 / ¥价格 ¥原价
        import re
        for idx, t in enumerate(lines):
            m = re.match(r"^¥([\d,]+)$", t.strip())
            if m and idx >= 1:
                price = int(m.group(1).replace(",", ""))
                name = lines[idx-2] if idx >= 2 else ""
                if name and not name.startswith("¥"):
                    products.setdefault(name, {"storePrice": price, "raw": lines[max(0,idx-3):idx+2]})
    out = os.path.join(shots_dir, "products.json")
    json.dump(products, open(out, "w", encoding="utf-8"), ensure_ascii=False, indent=1)
    print(f"[ocr] 识别 {len(products)} 件 -> {out}")

if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--pages", type=int, default=30)
    ap.add_argument("--out", default=DEFAULT_OUT)
    ap.add_argument("--ocr", action="store_true")
    args = ap.parse_args()
    harvest(args.pages, args.out)
    if args.ocr:
        ocr_all(args.out)
