#!/usr/bin/env node
/**
 * NK-MP 浏览器测试电池（开发/QA 用，不参与运行时）。
 *
 * 抽象目标：把一次性的对抗性浏览器检查沉淀为可复用、可参数化、可单选的用例集合：
 *   - 目标与账号全部来自环境变量，不对部署环境写死；
 *   - 每个用例自带准备与清理（自建渠道用 `bt-` 前缀，跑完即删）；
 *   - 失败以退出码 1 结束，可直接进 CI。
 *
 * 用法与变量见同目录 README.md。
 */
import { existsSync, readdirSync } from "node:fs";
import process from "node:process";
import puppeteer from "puppeteer-core";

const SCRATCH = "bt-";
const MODAL = ".ant-modal";

function defaultChrome() {
  if (process.env.CHROME_PATH) return process.env.CHROME_PATH;
  const root = "/home/codespace/.omp/puppeteer/chrome";
  if (existsSync(root)) {
    for (const dir of readdirSync(root).sort().reverse()) {
      const candidate = `${root}/${dir}/chrome-linux64/chrome`;
      if (existsSync(candidate)) return candidate;
    }
  }
  for (const candidate of [
    "/usr/bin/chromium",
    "/usr/bin/chromium-browser",
    "/usr/bin/google-chrome",
    "/usr/bin/google-chrome-stable",
    "/opt/google/chrome/chrome",
  ]) {
    if (existsSync(candidate)) return candidate;
  }
  throw new Error("找不到 Chromium 可执行文件，请用 CHROME_PATH 指定");
}

const CFG = {
  baseUrl: (process.env.NKMP_BROWSER_URL ?? "http://127.0.0.1:5173").replace(/\/+$/, ""),
  email: process.env.NKMP_BROWSER_EMAIL ?? "",
  password: process.env.NKMP_BROWSER_PASSWORD ?? "",
  insecure: process.env.NKMP_BROWSER_INSECURE === "1",
  headed: process.env.NKMP_BROWSER_HEADED === "1",
  debug: process.env.NKMP_BROWSER_DEBUG === "1",
  chrome: defaultChrome(),
  only: (process.env.NKMP_BROWSER_ONLY ?? "")
    .split(",")
    .map((s) => s.trim())
    .filter(Boolean),
};

const sleep = (ms) => new Promise((resolve) => setTimeout(resolve, ms));

/** waitForFunction 默认用 rAF 轮询；后台标签页 rAF 会暂停，这里统一定时轮询。 */
function waitFor(page, fn, { timeout = 15000, args = [] } = {}) {
  return page.waitForFunction(fn, { timeout, polling: 100 }, ...args);
}

function assert(condition, message) {
  if (!condition) throw new Error(message);
}

async function gotoApp(page, path, settle = 600) {
  await page.goto(CFG.baseUrl + path, { waitUntil: "domcontentloaded", timeout: 30000 });
  await waitFor(
    page,
    () =>
      document.querySelector('input[autocomplete="current-password"]') ||
      document.querySelector(".ant-layout-sider, .ant-layout-header"),
    { timeout: 20000 },
  );
  await sleep(settle);
}

async function fill(page, selector, value) {
  await page.waitForSelector(selector, { timeout: 10000 });
  await page.evaluate(
    (sel, val) => {
      const el = document.querySelector(sel);
      const setter = Object.getOwnPropertyDescriptor(HTMLInputElement.prototype, "value").set;
      setter.call(el, val);
      el.dispatchEvent(new Event("input", { bubbles: true }));
      el.dispatchEvent(new Event("change", { bubbles: true }));
    },
    selector,
    value,
  );
}

async function clickText(page, selector, text, root = "body") {
  const found = await page.evaluate(
    (sel, txt, rootSel) => {
      const scope = document.querySelector(rootSel) ?? document;
      const el = [...scope.querySelectorAll(sel)].find((e) => e.innerText?.includes(txt));
      if (!el) return false;
      el.click();
      return true;
    },
    selector,
    text,
    root,
  );
  if (!found) throw new Error(`找不到「${text}」（${selector}）`);
}

async function apiGet(page, path) {
  return page.evaluate(async (p) => {
    const r = await fetch(p, { credentials: "same-origin" });
    return { status: r.status, body: await r.json().catch(() => null) };
  }, `/api/v1${path}`);
}

async function apiMutate(page, path, method, body) {
  return page.evaluate(
    async (p, m, b) => {
      const csrf = document.cookie
        .split(";")
        .map((s) => s.trim())
        .find((s) => s.startsWith("nike_mp_csrf="));
      const token = csrf ? decodeURIComponent(csrf.split("=").slice(1).join("=")) : "";
      const r = await fetch(p, {
        method: m,
        credentials: "same-origin",
        headers: { "X-CSRF-Token": token, ...(b ? { "Content-Type": "application/json" } : {}) },
        body: b ? JSON.stringify(b) : undefined,
      });
      return { status: r.status, body: await r.json().catch(() => null) };
    },
    `/api/v1${path}`,
    method,
    body ?? null,
  );
}

async function ensureLoggedIn(page) {
  await gotoApp(page, "/login");
  if (await page.$('input[autocomplete="current-password"]')) {
    assert(CFG.email && CFG.password, "需要 NKMP_BROWSER_EMAIL / NKMP_BROWSER_PASSWORD");
    await fill(page, 'input[autocomplete="username"]', CFG.email);
    await fill(page, 'input[autocomplete="current-password"]', CFG.password);
    await clickText(page, "button", "登");
    await waitFor(page, () => !location.pathname.startsWith("/login"), { timeout: 20000 });
  }
}

/** 清掉历史失败运行遗留的测试渠道（`bt-` 前缀）。 */
async function cleanupScratch(page) {
  const list = await apiGet(page, "/me/notification-channels");
  if (list.status !== 200 || !Array.isArray(list.body)) return 0;
  let removed = 0;
  for (const channel of list.body) {
    if (typeof channel.name === "string" && channel.name.startsWith(SCRATCH)) {
      await apiMutate(page, `/me/notification-channels/${channel.id}`, "DELETE");
      removed += 1;
    }
  }
  return removed;
}

async function createScratchChannel(page, name, url) {
  const created = await apiMutate(page, "/me/notification-channels", "POST", {
    platform: "custom",
    name,
    fields: { url },
  });
  assert(created.status === 201, `建渠道应返回 201，实际 ${created.status}`);
  return created.body?.id;
}

/** 打开「添加渠道」并切到自定义（Apprise URL）平台，返回时表单就绪。 */
async function openAddChannel(page) {
  await gotoApp(page, "/account");
  await clickText(page, "button", "添加渠道");
  await page.waitForSelector(`${MODAL} input#name`, { timeout: 10000 });
  await page.evaluate(() => {
    document
      .querySelector(".ant-modal .ant-select-selector")
      .dispatchEvent(new MouseEvent("mousedown", { bubbles: true }));
  });
  await waitFor(
    page,
    () => [...document.querySelectorAll(".ant-select-item-option")].some((o) => o.innerText.includes("自定义")),
    { timeout: 10000 },
  );
  await page.evaluate(() => {
    [...document.querySelectorAll(".ant-select-item-option")]
      .find((o) => o.innerText.includes("自定义"))
      .dispatchEvent(new MouseEvent("click", { bubbles: true }));
  });
  await page.waitForSelector(`${MODAL} input#fields_url`, { timeout: 10000 });
}

async function clickSave(page) {
  const ok = await page.evaluate(() => {
    const button = [...document.querySelectorAll(".ant-modal button")].find((b) =>
      b.innerText.includes("保存渠道"),
    );
    if (!button) return false;
    button.click();
    return true;
  });
  if (!ok) throw new Error("找不到「保存渠道」按钮");
}

async function waitForChannel(page, name, timeoutMs = 6000) {
  const deadline = Date.now() + timeoutMs;
  while (Date.now() < deadline) {
    const list = await apiGet(page, "/me/notification-channels");
    const mine = (list.body ?? []).filter((c) => c.name === name);
    if (mine.length > 0) return mine;
    await sleep(300);
  }
  return [];
}

// ---------------------------------------------------------------- 用例

/** 登录后会话就绪，并清掉历史遗留的测试渠道。 */
async function caseLoginSession({ page }) {
  await ensureLoggedIn(page);
  const me = await apiGet(page, "/auth/me");
  assert(me.status === 200, `会话应有效，/auth/me 返回 ${me.status}`);
  const removed = await cleanupScratch(page);
  return `会话有效${removed ? `，清理遗留渠道 ${removed} 条` : ""}`;
}

/** 连点两次保存只应落一条渠道。 */
async function caseDoubleSubmit({ page }) {
  const name = `${SCRATCH}dbl-${Date.now()}`;
  await openAddChannel(page);
  await fill(page, `${MODAL} input#name`, name);
  await fill(page, `${MODAL} input#fields_url`, "json://httpbin.org/post?battery=double");
  await clickSave(page);
  await sleep(80);
  await clickSave(page).catch(() => {});
  await sleep(2500);
  const mine = await waitForChannel(page, name);
  assert(mine.length === 1, `应只有 1 条渠道，实际 ${mine.length} 条`);
  await apiMutate(page, `/me/notification-channels/${mine[0].id}`, "DELETE");
  return "连点两次只建 1 条";
}

/** CSRF cookie 丢失后，重载页面应自动补发，写入随之恢复。 */
async function caseCsrfSelfHeal({ page }) {
  const name = `${SCRATCH}csrf-${Date.now()}`;
  const id = await createScratchChannel(page, name, "json://httpbin.org/post?battery=csrf");
  await page.deleteCookie({ name: "nike_mp_csrf", url: CFG.baseUrl });
  const gone = await page.evaluate(() => document.cookie.includes("nike_mp_csrf="));
  assert(!gone, "测试前置失败：CSRF cookie 未被删除");

  await gotoApp(page, "/account", 1200);
  const cookie = await page.evaluate(() => document.cookie);
  assert(cookie.includes("nike_mp_csrf="), "重载后 CSRF cookie 未补发");

  const removed = await apiMutate(page, `/me/notification-channels/${id}`, "DELETE");
  assert(removed.status === 204, `补发后写入应 204，实际 ${removed.status}`);
  return "重载补发 cookie，写入恢复";
}

/** 渠道名按文本转义，不允许注入节点或执行脚本。 */
async function caseHostileName({ page }) {
  const name = `<img src=x onerror="window.__xss=1"><b>粗</b>`;
  await openAddChannel(page);
  await fill(page, `${MODAL} input#name`, name);
  await fill(page, `${MODAL} input#fields_url`, "json://httpbin.org/post?battery=xss");
  await clickSave(page);
  const mine = await waitForChannel(page, name);
  assert(mine.length === 1, "恶意名称渠道未创建成功");
  // 列表刷新是异步的，等这一行真正渲染出来再断言转义。
  await waitFor(page, () => document.body.innerText.includes("粗"), { timeout: 8000 }).catch(() => {});

  const probe = await page.evaluate(() => ({
    xss: window.__xss,
    injected: document.querySelectorAll('img[src="x"]').length,
    shown: document.body.innerText.includes("粗"),
  }));
  assert(probe.xss === undefined, "onerror 脚本被执行");
  assert(probe.injected === 0, "渠道名注入了 img 节点");
  assert(probe.shown, "渠道名没有按文本显示");

  await apiMutate(page, `/me/notification-channels/${mine[0].id}`, "DELETE");
  return "按文本转义，无脚本执行";
}

/** 超长名称应被拒绝，且界面给出提示。 */
async function caseOverlongName({ page }) {
  const before = await apiGet(page, "/me/notification-channels");
  await openAddChannel(page);
  await fill(page, `${MODAL} input#name`, "x".repeat(300));
  await fill(page, `${MODAL} input#fields_url`, "json://httpbin.org/post?battery=long");
  await clickSave(page);
  await sleep(2000);
  const after = await apiGet(page, "/me/notification-channels");
  assert(after.body.length === before.body.length, "超长名称不应创建渠道");
  const message = await page.evaluate(() =>
    [...document.querySelectorAll(".ant-form-item-explain-error, .ant-message-notice")]
      .map((e) => e.innerText.trim())
      .join(" | "),
  );
  assert(message.length > 0, "超长名称未显示校验提示");
  await page.evaluate(() => document.querySelector(".ant-modal-close")?.click());
  return `已拒绝：${message.slice(0, 40)}`;
}

/** 不存在的任务 / 检查 id 应给中文空态而不是白屏或崩溃。 */
async function caseDeepLinks({ page }) {
  const probes = [
    "/tasks/00000000-0000-0000-0000-000000000000",
    "/scans/00000000-0000-0000-0000-000000000000",
    "/tasks/not-a-uuid",
  ];
  for (const path of probes) {
    await gotoApp(page, path, 300);
    await waitFor(page, () => /不存在|没有权限/.test(document.body.innerText), { timeout: 10000 });
    const state = await page.evaluate(() => ({
      text: document.body.innerText,
      rendered: (document.querySelector("#root")?.childElementCount ?? 0) > 0,
    }));
    assert(state.rendered, `${path} 渲染为空`);
    assert(/不存在|没有权限/.test(state.text), `${path} 缺少空态文案`);
  }
  return "3 条深链均为中文空态";
}

/** 全站巡览：无控制台错误、无页面异常、无混合内容。 */
async function caseConsoleScan({ page, consoleErrors, pageErrors }) {
  consoleErrors.length = 0;
  pageErrors.length = 0;
  for (const path of ["/dashboard", "/tasks", "/changes", "/account", "/ops"]) {
    await gotoApp(page, path, 900);
  }
  assert(consoleErrors.length === 0, `控制台错误 ${consoleErrors.length} 条：${consoleErrors[0] ?? ""}`);
  assert(pageErrors.length === 0, `页面异常 ${pageErrors.length} 条：${pageErrors[0] ?? ""}`);
  const mixed = await page.evaluate(() =>
    performance
      .getEntriesByType("resource")
      .map((r) => r.name)
      .filter((n) => n.startsWith("http://")),
  );
  assert(mixed.length === 0, `混合内容 ${mixed.length} 条：${mixed[0] ?? ""}`);
  return "5 页巡览干净";
}

/** 一个标签页退出后，另一个标签页的下一次请求应回到登录页。 */
async function caseTwoTabLogout({ page, browser }) {
  const mark = (m) => {
    if (CFG.debug) console.log(`      · ${m}`);
  };
  const second = await browser.newPage();
  try {
    await second.setViewport({ width: 1280, height: 900 });
    mark("ensureLoggedIn(page)");
    await ensureLoggedIn(page);
    mark("gotoApp(second, /account)");
    await gotoApp(second, "/account");
    mark(`second path=${await second.evaluate(() => location.pathname)}`);
    mark("click 退出登录");
    await clickText(page, "button", "退出登录");
    await waitFor(page, () => location.pathname === "/login", { timeout: 10000 });
    mark("page 已到 /login");

    const clicked = await second.evaluate(() => {
      const item = [...document.querySelectorAll(".ant-menu-item")].find(
        (e) => e.innerText.trim() === "监控任务",
      );
      if (!item) return false;
      item.click();
      return true;
    });
    assert(clicked, "侧边导航未找到「监控任务」");
    await waitFor(second, () => location.pathname === "/login", { timeout: 10000 });
    return "第二个标签页随首次请求回到登录页";
  } finally {
    await second.close();
  }
}

const CASES = [
  ["login-session", "登录与会话", caseLoginSession],
  ["double-submit", "连点保存不重复", caseDoubleSubmit],
  ["csrf-selfheal", "CSRF 丢失自愈", caseCsrfSelfHeal],
  ["hostile-name", "恶意渠道名转义", caseHostileName],
  ["overlong-name", "超长名称拒绝", caseOverlongName],
  ["deep-links", "非法深链空态", caseDeepLinks],
  ["console-scan", "控制台与混合内容", caseConsoleScan],
  ["two-tab-logout", "双标签页登出传播", caseTwoTabLogout],
];

// ---------------------------------------------------------------- 运行器

async function main() {
  const selected = CASES.filter(([id]) => CFG.only.length === 0 || CFG.only.includes(id));
  const unknown = CFG.only.filter((id) => !CASES.some(([caseId]) => caseId === id));
  assert(unknown.length === 0, `未知用例：${unknown.join(", ")}`);

  console.log(`NK-MP 浏览器电池 → ${CFG.baseUrl}（${selected.length} 个用例）`);
  const browser = await puppeteer.launch({
    executablePath: CFG.chrome,
    headless: !CFG.headed,
    acceptInsecureCerts: CFG.insecure,
    args: ["--no-sandbox", "--disable-dev-shm-usage", "--disable-gpu"],
  });

  const consoleErrors = [];
  const pageErrors = [];
  const page = await browser.newPage();
  await page.setViewport({ width: 1280, height: 900 });
  page.on("console", (msg) => {
    if (msg.type() === "error") consoleErrors.push(msg.text().slice(0, 200));
  });
  page.on("pageerror", (err) => pageErrors.push(String(err).slice(0, 200)));

  let failed = 0;
  const results = [];
  try {
    for (const [id, title, run] of selected) {
      const started = Date.now();
      try {
        const detail = await run({ page, browser, consoleErrors, pageErrors });
        results.push([id, "PASS", detail, Date.now() - started]);
      } catch (error) {
        failed += 1;
        results.push([id, "FAIL", error.message, Date.now() - started]);
      }
    }
  } finally {
    await browser.close();
  }

  for (const [id, status, detail, ms] of results) {
    console.log(`${status}  ${id.padEnd(15)} ${(ms / 1000).toFixed(1)}s  ${detail}`);
  }
  console.log(`${results.length - failed}/${results.length} 通过${failed ? "（有失败）" : ""}`);
  process.exitCode = failed ? 1 : 0;
}

main().catch((error) => {
  console.error(`电池运行失败：${error.message}`);
  process.exitCode = 1;
});
