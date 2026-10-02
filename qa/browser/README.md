# 浏览器测试电池

把对抗性浏览器检查沉淀成可复用的脚本：参数化目标与账号，对着任意部署（本地 / QA / 生产）
都能一键跑完，失败以退出码 1 结束。

只用于开发与验收，不在运行时路径上。

## 安装与运行

```bash
cd qa/browser
npm install
NKMP_BROWSER_URL=https://<目标> \
NKMP_BROWSER_EMAIL=<账号> \
NKMP_BROWSER_PASSWORD=<密码> \
npm run battery
```

只跑部分用例：`NKMP_BROWSER_ONLY=csrf-selfheal,two-tab-logout npm run battery`。

## 环境变量

| 变量 | 默认 | 说明 |
| --- | --- | --- |
| `NKMP_BROWSER_URL` | `http://127.0.0.1:5173` | 站点根地址，不带尾斜杠 |
| `NKMP_BROWSER_EMAIL` / `NKMP_BROWSER_PASSWORD` | 空 | 登录账号（生产用 ops 账号） |
| `NKMP_BROWSER_ONLY` | 空 | 逗号分隔的用例 id；不设则全跑 |
| `NKMP_BROWSER_INSECURE` | `0` | `1` 接受自签证书（本地 QA 常用） |
| `NKMP_BROWSER_HEADED` | `0` | `1` 显示浏览器窗口 |
| `NKMP_BROWSER_DEBUG` | `0` | `1` 打印用例内的分步进度 |
| `CHROME_PATH` | 自动探测 | Chromium/Chrome 可执行文件 |

## 用例

| id | 检查 |
| --- | --- |
| `login-session` | 登录可用；顺手清掉上次失败遗留的 `bt-` 渠道 |
| `double-submit` | 连点两次「保存渠道」只落 1 条 |
| `csrf-selfheal` | 删除 CSRF cookie 后重载自动补发，写入恢复 204 |
| `hostile-name` | 渠道名按文本转义：不执行脚本、不注入节点 |
| `overlong-name` | 超长名称被拒绝且有提示，渠道数不变 |
| `deep-links` | 不存在的任务/检查 id 给中文空态，不白屏 |
| `console-scan` | 巡览 5 个页面：0 控制台错误、0 页面异常、0 混合内容 |
| `two-tab-logout` | A 标签页退出后，B 标签页下一次请求回到登录页 |

## 约定

- 测试自建的渠道一律 `bt-` 前缀，用例结束即删；`login-session` 会兜底清理。
- `two-tab-logout` 会真退出登录，排在最后；跑完需要重新登录才能再跑前半段。
- 断言用稳定文案（如「不存在」），不绑定具体排版；改动对用户可见文案时同步更新。
- 需要 ops 账号才能让 `console-scan` 巡览「管理后台」；普通账号该页 403，不算失败但会少测一页。
