# -*- coding: utf-8 -*-
"""
Nike 凭证自动续签（纯 API · 零微信/零模拟器/零 adb）
================================================
两级续期:
  L1  纯 HTTP: POST accounts.nike.com.cn/token/v1 (grant_type=refresh_token)
      → 用小程序客户端 (含 offline_access), 拿到后就完全不需要浏览器
      refresh_token 有效期通常数周~数月
  L2  浏览器静默授权 (refresh_token 失效后):
      浏览器的 accounts.nike.com.cn 登录会话 cookie →
      authorize/v1 (prompt=none + PKCE) → 授权码 → token/v1 交换
      → 新 access_token + 新 refresh_token (回到 L1)

小程序客户端 (与微信端同款, o2o 平台认它):
  client_id : 790b76033778d3e6f18743582900a4ae
  redirect  : https://mp-static-assets.gc.nike.com/auth/wechat/index.html
  scope     : openid offline_access nike.digital profile phone flow

同时采集: api.nike.com.cn 的 WAF 票据 (acw_sc__v2) + 浏览器 UA 指纹

用法: python nike_auth.py            # mp 客户端 (主用)
      python nike_auth.py --web      # web 客户端 (备用)
"""
import subprocess, os, sys, json, time, base64, hashlib, re, datetime
import urllib.request, urllib.parse, urllib.error

BASE = os.path.dirname(os.path.abspath(__file__))
CLI = os.path.expandvars(r"%LOCALAPPDATA%\Tabbit\LocalAgent\bin\tabbit-cli.exe")
TASK = "nike-auth"

CLIENTS = {
    "mp": {
        "client_id": "790b76033778d3e6f18743582900a4ae",
        "redirect": "https://mp-static-assets.gc.nike.com/auth/wechat/index.html",
        "scope": "openid offline_access nike.digital profile phone flow",
        "jwt_file": "jwt_latest.txt",
        "rt_file": "mp_refresh.txt",
    },
    "web": {
        "client_id": "e5517bcc26532d959b2532654ac037dd",
        "redirect": "https://www.nike.com.cn/auth/login",
        "scope": "openid nike.digital profile email phone flow country",
        "jwt_file": "web_access_token.txt",
        "rt_file": "web_refresh.txt",
    },
}

def p(*a): print(*a, flush=True)

def token_post(form):
    data = urllib.parse.urlencode(form).encode()
    h = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 Chrome/144.0.0.0 Safari/537.36",
         "Content-Type": "application/x-www-form-urlencoded",
         "Referer": "https://www.nike.com.cn/", "HOST": "accounts.nike.com.cn"}
    r = urllib.request.urlopen(urllib.request.Request(
        "https://accounts.nike.com.cn/token/v1", data=data, headers=h, method="POST"), timeout=25)
    return json.loads(r.read())

def save_tokens(cfg, tok):
    at = tok.get("access_token")
    if not at:
        return None
    open(os.path.join(BASE, cfg["jwt_file"]), "w").write(at)
    rt = tok.get("refresh_token")
    if rt:
        open(os.path.join(BASE, cfg["rt_file"]), "w").write(rt)
    pl = json.loads(base64.urlsafe_b64decode(at.split(".")[1] + "=" * (-len(at.split(".")[1]) % 4)))
    exp = datetime.datetime.fromtimestamp(pl["exp"])
    p(f"[auth] ✓ JWT 已刷新: exp={exp:%m-%d %H:%M:%S} prn={pl.get('prn')} rt={'有' if rt else '无'}")
    return at

def try_refresh_token(cfg):
    f = os.path.join(BASE, cfg["rt_file"])
    if not os.path.exists(f):
        return None
    rt = open(f).read().strip()
    if not rt:
        return None
    try:
        tok = token_post({"grant_type": "refresh_token", "refresh_token": rt,
                          "client_id": cfg["client_id"]})
    except urllib.error.HTTPError as ex:
        body = ex.read()[:200].decode("utf-8", "replace")
        p(f"[auth] refresh_token 续期失败({ex.code}): {body}")
        return None
    except Exception as ex:
        p("[auth] refresh_token 续期异常:", ex)
        return None
    return save_tokens(cfg, tok)

def browser_authorize(cfg, harvest_waf=True):
    """浏览器静默授权; 返回 (code, verifier) 或 (None, 原因)"""
    verifier = base64.urlsafe_b64encode(os.urandom(32)).rstrip(b"=").decode()
    challenge = base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).rstrip(b"=").decode()
    q = {
        "client_id": cfg["client_id"], "redirect_uri": cfg["redirect"], "response_type": "code",
        "scope": cfg["scope"], "ui_locales": "zh-CN",
        "code_challenge": challenge, "code_challenge_method": "S256",
    }
    authz_quiet = "https://accounts.nike.com.cn/authorize/v1?" + urllib.parse.urlencode({**q, "prompt": "none"})
    authz_plain = "https://accounts.nike.com.cn/authorize/v1?" + urllib.parse.urlencode(q)
    js = f'''
const quiet = {json.dumps(authz_quiet)};
const plain = {json.dumps(authz_plain)};
const sleep = ms => new Promise(r => setTimeout(r, ms));
const out = {{steps: []}};
async function visit(u) {{
  try {{
    await page.goto(u, {{waitUntil: "domcontentloaded", timeout: 35000}});
  }} catch (e) {{ out.steps.push("goto-err:" + String(e).slice(0, 120)); }}
  await sleep(4500);
  out.steps.push(page.url());
  return page.url();
}}
let u = await visit(quiet);
if (!/[?&]code=/.test(u)) u = await visit(plain);
// 账号确认/同意页: 勾选隐私条款后点"继续"
for (let i = 0; i < 4; i++) {{
  if (/[?&]code=/.test(page.url())) break;
  if (!/accounts\\.nike\\.com\\.cn/.test(page.url())) break;
  try {{
    const cb = await page.$("input#privacyTerms, input[type=checkbox]");
    if (cb) {{ try {{ await cb.check({{ timeout: 3000 }}); }} catch (e) {{ try {{ await cb.click(); }} catch (e2) {{}} }} }}
    out.steps.push("cb=" + (cb ? "checked" : "none"));
    const clicked = await page.evaluate(() => {{
      const els = Array.from(document.querySelectorAll("button,a[role=button],input[type=submit]"));
      const t = els.find(e => /继续|同意|授权|确认|Agree|Continue|Allow|Accept/i.test(((e.innerText||e.value||"")).trim()));
      if (t) {{ t.click(); return ((t.innerText||t.value||"")).trim(); }}
      return null;
    }});
    out.steps.push("clicked=" + clicked);
    await sleep(4500);
    out.steps.push("after=" + page.url().slice(0, 120));
  }} catch (e) {{ out.steps.push("consent-err:" + String(e).slice(0, 100)); }}
}}
u = page.url();
out.finalUrl = u;
if ({str(harvest_waf).lower()}) {{
  await visit("https://api.nike.com.cn/o2o/buy/regions/v1?anchor=0&count=1");
  await visit("https://www.nike.com.cn/");
  try {{
    const ck = await context.cookies();
    out.acw = ck.filter(c => /nike\\.com\\.cn$/i.test(c.domain) && c.name === "acw_sc__v2").map(c => ({{d: c.domain, v: c.value}}));
    out.ua = await page.evaluate(() => navigator.userAgent);
  }} catch (e) {{}}
}}
return out;
'''
    p("[auth] 浏览器静默授权中...")
    try:
        r = subprocess.run([CLI, "nodejs", "--task", TASK, "--request-id", f"a{int(time.time())}",
                            "--timeout-ms", "150000"], input=js, capture_output=True, text=True, timeout=180)
    except subprocess.TimeoutExpired:
        return None, "浏览器任务超时"
    out = r.stdout + r.stderr
    m = re.search(r'"finalUrl":"([^"]+)"', out)
    if not m:
        return None, f"浏览器任务无输出: {out[-200:]}"
    final = m.group(1).replace("\\u0026", "&").replace("\\/", "/")
    qq = urllib.parse.parse_qs(urllib.parse.urlparse(final).query)
    # 采集 WAF/UA
    for mm in re.finditer(r'\{"d":"([^"]+)","v":"([0-9a-f]{20,})"\}', out):
        if mm.group(1) == ".nike.com.cn":
            open(os.path.join(BASE, "waf_token.txt"), "w").write(mm.group(2))
            p(f"[waf] ✓ acw_sc__v2={mm.group(2)[:16]}... (域名 {mm.group(1)})")
    mu = re.search(r'"ua":"([^"]+)"', out)
    if mu:
        open(os.path.join(BASE, "ua.txt"), "w").write(mu.group(1))
        p(f"[waf] ✓ 浏览器UA已记录: {mu.group(1)[:60]}...")
    if "code" in qq:
        return (qq["code"][0], verifier), None
    if "error" in qq:
        return None, f"静默授权失败: {qq.get('error')} {qq.get('error_description', '')}"
    # 被 WAF 挑战页拦住 (URL 停在 accounts 上)
    if "accounts.nike.com.cn" in final:
        ms = re.search(r'"steps":\[(.*?)\]', out)
        return None, f"授权页被 WAF 挑战拦截, 未到 redirect。步骤: {ms.group(1)[:400] if ms else '无'}"
    return None, f"未拿到授权码: {final[:150]}"

def full_refresh(client="mp", force_browser=False):
    cfg = CLIENTS[client]
    if not force_browser:
        p(f"[auth] 尝试 refresh_token 纯HTTP续期 ({client})...")
        at = try_refresh_token(cfg)
        if at:
            return True
    # 浏览器静默授权
    res, err = browser_authorize(cfg)
    if err:
        p("[auth]", err)
        p("[auth] 提示: 若会话过期, 在浏览器里登录一次 https://www.nike.com.cn")
        return False
    code, verifier = res
    p("[auth] 授权码到手, 交换 token...")
    try:
        tok = token_post({"grant_type": "authorization_code", "code": code,
                          "redirect_uri": cfg["redirect"], "client_id": cfg["client_id"],
                          "code_verifier": verifier})
    except urllib.error.HTTPError as ex:
        p("[auth] token 交换失败:", ex.code, ex.read()[:300].decode("utf-8", "replace"))
        return False
    return bool(save_tokens(cfg, tok))

if __name__ == "__main__":
    client = "web" if "--web" in sys.argv else "mp"
    force = "--force-browser" in sys.argv
    sys.exit(0 if full_refresh(client, force) else 1)
