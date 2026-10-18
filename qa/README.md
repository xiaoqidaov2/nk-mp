# QA 隔离联调环境

独立 Compose 项目 + 独立数据库卷 + 离线上游 stub。**绝不访问真实 Nike 接口。**
stub 同时模拟门店只读接口与 `app/nike/phone_auth.py` 冻结的身份（短信登录）契约，
因此本目录可以完整验收「自助绑定 Nike 账号」闭环。

## 1. 自助绑定验收（`compose.auth.*`，推荐）

`qa/compose.auth.yaml` 自包含（不依赖根 `compose.yaml`），并打开短信登录开关
（`NIKE_MP_NIKE_PHONE_AUTH_ENABLED=true`、契约版本 `2026-09-29/mp-phone-otc-pkce`）。
端口与凭证密钥都可用环境变量注入：`QA_API_PORT`（默认 18000）、`QA_WEB_PORT`
（默认 15173）、`QA_STUB_PORT`（默认 19100）、`QA_CREDENTIAL_KEY`（默认真实加密用的
假密钥）、host 网络变体的 `QA_DB_PORT`（默认 15432）。

```bash
# 1. 启动（首次需要构建）
docker compose -p nike-mp-auth-qa -f qa/compose.auth.yaml up -d --build

# 2. 写入种子数据（ops + A + B 三个用户、平台/客户租户、货号 DN3707-202；不写任何凭证）
docker compose -p nike-mp-auth-qa -f qa/compose.auth.yaml \
  exec -T api python - < qa/seed_qa.py

# 3. 运行冒烟（默认就是 18000 / 19100，可显式覆盖）
QA_API=http://127.0.0.1:18000 QA_STUB=http://127.0.0.1:19100 \
  apps/api/.venv/bin/python qa/smoke.py

> 渠道地址由容器内的 API/worker 访问：桥接网络下要额外给 `QA_PUSH_URL`
> （如 `json://stub:19100/__push`），host 变体保持默认推导即可。

# 4. 查看日志 / 停止
docker compose -p nike-mp-auth-qa logs -f worker
docker compose -p nike-mp-auth-qa down            # 保留数据卷
docker compose -p nike-mp-auth-qa down -v         # 连数据卷一起删除
```

### 端口被占用

18000 / 15173 / 19100 若被占用，改用 28000 / 25173 / 29100（host 变体同理）：

```bash
QA_API_PORT=28000 QA_WEB_PORT=25173 QA_STUB_PORT=29100 \
  docker compose -p nike-mp-auth-qa -f qa/compose.auth.yaml up -d --build
docker compose -p nike-mp-auth-qa -f qa/compose.auth.yaml \
  exec -T api python - < qa/seed_qa.py
QA_API=http://127.0.0.1:28000 QA_STUB=http://127.0.0.1:29100 \
  apps/api/.venv/bin/python qa/smoke.py
```

三组端口仍被占用即视为环境阻断，先释放再执行。

### 环境受限时的 host 网络变体

部分沙箱/CI 无法在容器之间建立桥接网络（表现为 `db:5432` TCP 超时，`migrate`
以 `psycopg.errors.ConnectionTimeout` 退出）。此时用自包含的 host 网络变体，隔离性
（独立项目名、独立数据卷、独立端口）不变：

```bash
docker compose -p nike-mp-auth-qa -f qa/compose.auth.hostnet.yaml up -d --build
docker compose -p nike-mp-auth-qa -f qa/compose.auth.hostnet.yaml \
  exec -T api python - < qa/seed_qa.py
QA_API=http://127.0.0.1:18000 QA_STUB=http://127.0.0.1:19100 \
  apps/api/.venv/bin/python qa/smoke.py
```

## 2. 旧版只读采集联调（`compose.qa.*`，叠加根 compose）

仍可用，已同样打开短信登录开关（`enabled=true` + 契约版本 + 假 key）：

```bash
docker compose -p nike-mp-qa -f compose.yaml -f qa/compose.qa.yaml up -d --build
docker compose -p nike-mp-qa -f compose.yaml -f qa/compose.qa.yaml \
  exec -T api python - < qa/seed_qa.py
QA_API=http://127.0.0.1:8000 QA_STUB=http://127.0.0.1:9100 \
  apps/api/.venv/bin/python qa/smoke.py
```

> 桥接网络下 API 在容器内：推送渠道地址必须用服务名，再加
> `QA_PUSH_URL=json://stub:9100/__push`（host 网络变体无需，默认 127.0.0.1 即可）。
> 内网目标默认被 SSRF 保护拒绝，两个变体都已预置 `NIKE_MP_PUSH_ALLOW_PRIVATE_TARGETS=true`。

## stub 契约

`qa/stub_upstream.py` 内存实现，重启即清空：

- 身份端点（严格对齐 `app/nike/phone_auth.py` 的请求形状）
  - `GET  /authorize/v1` → 200 HTML + 会话 Cookie。
  - `POST /credential_lookup/v1` → 已知手机号 200，其它 404。
  - `POST /verification_code/send/v1` → 204，并计入 `/__stats.send_count`。
  - `POST /challenge/code/v1` → 固定验证码 `123456` 返回 200，否则 400
    `{"errors":[{"code":"invalid_verification_code"}]}`。
  - `POST /check/v1` → `{"requiredStates":null,"redirect_uri":"...?code=<code>&state=<state>"}`。
  - `POST /token/v1` → 同时支持 `authorization_code`（每个 code/身份签发**互相区分**的
    access/refresh token，并校验 PKCE S256）与 `refresh_token`（轮换后保持同一身份）。
- 身份与数据
  - 只读接口：`/o2o/buy/regions/v1`（门店清单）与 `/o2o/buy/cart_reviews/v2/{id}`
    （PUT 提交 + GET 轮询），以及两种尺码结构端点——
    `…/product_details/v1/…/store_number/{store}/style_color/{style}`
    （`NikeClient.fetch_structure`）与批量
    `…/product_details/internal/v1/…/store_number/{store}?productCodes=…`
    （`NikeClient.fetch_structures`，按 ≤30 货号分块）。未知门店返回空 `products`，
    响应里缺的货号由客户端按 `MISSING` 处理（未知绝不当作结论）。
  - 压测门店：`STUB_EXTRA_STORES=N` 让 stub 额外生成 `QA003..QA0NN`（各带 40/42/44/46 尺码表），
    用于并发/额度曲线压测；默认 0，冒烟仍是 QA001/QA002 两家、断言不变。
  - `+8613800000000` 为内置的 **default 身份**，走 `baseline`/`restock`/`untrusted`/`throttled`/
    `price_up` 场景开关（`POST /__scenario`）；未携带/未知 token 同样按 default 处理。
    - `throttled`：review 轮询返回 403（风控/限速），用于验收「整轮提前结束 + 账号冷却 +
      到点先补跑失败门店」；已登记的身份（A/B）不受场景开关影响。
    - `accounts2`：**只**让第二个 Nike 账号（scoped 身份 `A2`）的 review 轮询返回 403；
      其它身份（`A`/`B`/`default`）行为不变。用于验收「一用户多账号：一个账号被限速冷却，
      同一个任务下一轮自动换另一个账号继续」。
    - `price_up`：在货状态不变、价格 +100，用于验收「变价必须推送」（冒烟第 5b 步）。
  - `POST /__accounts {"phone","identity","price"}` 登记身份（smoke 用 A=699、B=799、
    多账号轮换用 A2=899）；已登记的 token 在 o2o 接口返回**自己的固定价格**，不受场景开关影响。
- 观测与复位
  - `GET /__stats`（`send_count`/`codes_issued`/`token_grants`/`refresh_grants`/`push_count` 等）。
  - `GET /__state`：身份级运行态。`reviews_by_identity` 是每个身份（`A`/`A2`/`B`/`default`）
    的 review 调用数（PUT + 每次轮询），冒烟用它证明**一轮究竟由哪个 Nike 账号在跑**
    （失败轮 `A2` 增长、随后成功轮 `A2` 不变）。
  - `POST /__reset` 清空全部内存状态（账号除外会重置为仅 default；`pushes` 也一并清空），便于反复冒烟。
- 推送落点（离线渠道）
  - `POST /__push`：Apprise `json://` 目标收到的载荷记为一条
    `{"title","body","received_at"}`，返回 `{"ok":true,"count":<条数>}`。
  - `GET /__push`：按到达顺序返回已记录的推送，smoke 用它断言测试消息与汇总消息都已送达。
  - `/__stats.push_count`：已记录的推送条数（与 `GET /__push` 长度一致）。
  - `/__reset` 会清空 `pushes`，因此每次 smoke 都从空列表开始。

## smoke 断言（`qa/smoke.py`）
1. ops/A/B 各自 `POST /me/nike-auth/sessions`（`consent=true`、uuid `request_id`）
   → 201/`CODE_SENT`，`verify {code:"123456"}` → 200 且 `ACTIVE`/`EXPIRING`。
2. ops 执行 `/ops/stores/sync`；随后保留完整只读采集闭环：建任务 → 扫描 → 矩阵
   （40 无货、42/44/46 有货带价）→ 不可信轮（门店 `UNKNOWN`、不产生库存事件）→
   `retry-failed` → 尺码 40 的 RESTOCK；配额逐笔增长。
3. A/B 各自建任务并扫描同一门店同一货号，矩阵中价格互不相同且各自可见
   （A=699、B=799），并验证 A 读不到 B 的扫描任务（同租户、跨 owner 隔离）。
4. `DELETE /me/nike-credentials/{id}` 解绑 A 的那个账号 → A 新建扫描/任务返回 409
   `NIKE_CREDENTIAL_REQUIRED`；B 继续扫描成功；`GET /me/nike-credentials`
   三人状态自洽（A 空列表 = 未连接、B/ops 仍 ACTIVE）。
5. stub 计数：`send_count == 3`；未知手机号 404、错误验证码 400 亦在断言内。
9. 消息推送：渠道创建 → 测试消息 → 检查结束后的汇总消息送达 stub（`/__push`）；
   最近一条投递记录为 `SENT`。
10. 30 秒档：新建 `cadence_seconds=30` 的任务 → 调度器连续推两轮（第二轮在第一轮
    `finished_at` 之后创建，即「一轮接一轮、不叠加」）→ 任务暂停。
11. 风控冷却：stub 切 `throttled` → 手动检查整轮提前结束（两店均 `FAILED`，原因
    `THROTTLED`）→ 账号冷却写入并在任务接口可见（`cooldown_reason=throttled`，窗口
    跟随 QA 阶梯）→ 降速通知送达 stub（含「自动继续」）→ 补跑作业排队且在窗口内保持
    `QUEUED`（`not_before` 未到不会被租约）→ 窗口过后按 `baseline` 先补跑并成功 →
    冷却被清空、任务下没有多跑的整轮。
12. 一用户多账号轮换（12/12，全 HTTP）：stub 登记 `+8613800000003` 为身份 `A2`
    （价格 899）→ A 先绑回第一个账号（`+8613800000001`，身份 `A`），让调度器真派发一轮
    把它的 `last_attempt_at` 写掉（「最近最少使用」因此有确定顺序）→ 再绑 `A2` 作为第二个
    账号 → `GET /me/nike-credentials` **两行** → stub 切 `accounts2` → 手动检查一轮命中
    `A2`：作业 `FAILED`、门店行 `error_code=THROTTLED`，且 `GET /__state` 的
    `reviews_by_identity` 显示只有 `A2` 增长 → `A2` 的账号行带 `cooldown_until`（原因
    `throttled`）、`A` 的行没有 → 紧接着（冷却窗口内）再手动检查一轮由 `A` 完成
    `SUCCEEDED`，且该轮 `A2` 计数**零增长** → 任务条目 `cooldown_until`/`cooldown_reason`
    为空（还有可用账号）→ 场景复位 `baseline`。窗口长度由 `QA_THROTTLE_BACKOFF_SECONDS`
    对齐（被限速账号在整个冷却窗口内不会被再次选中）。

### 冷却与补跑专项（`QA_SMOKE_MODE=cooldown`，压低小时闸）

小时额度窗口只能到点重开，脚本不断言「等一小时」，改为断言结构性事实（整轮提前结束、
窗口=下一个整点 +30 秒、通知、冷却期间不再派发、补跑作业排在窗口之后）；「到点自动恢复」
由上面的风控冷却用例（30 秒阶梯）证明。需要单独起一个压低闸值的栈并重建数据卷：

```bash
# 先停掉并清空默认栈（小时计数在库里，重复跑会立刻无额度）
docker compose -p nike-mp-auth-qa -f qa/compose.auth.hostnet.yaml \
  -f qa/compose.stub.hostnet.yaml down -v

QA_QUOTA_HOURLY_CAP=2 \
  docker compose -p nike-mp-auth-qa -f qa/compose.auth.hostnet.yaml \
  -f qa/compose.stub.hostnet.yaml up -d --build

docker compose -p nike-mp-auth-qa -f qa/compose.auth.hostnet.yaml \
  -f qa/compose.stub.hostnet.yaml exec -T api python - < qa/seed_qa.py

QA_SMOKE_MODE=cooldown QA_API=http://127.0.0.1:18000 QA_STUB=http://127.0.0.1:19100 \
  apps/api/.venv/bin/python qa/smoke.py     # 结束时打印 SMOKE COOLDOWN PASS
```

相关端口若被占用，沿用上面的 `QA_API_PORT` / `QA_WEB_PORT` / `QA_STUB_PORT` 覆盖方式
（hostnet 变体还有 `QA_DB_PORT`），并把 `QA_API` / `QA_STUB` 指到对应端口。

### 离线速度与冷却参数（QA 专用默认值）

stub 叠加文件与自包含 QA 栈默认把节奏压到秒级，便于在冒烟里走完「冷却 → 自动补跑」：

| 变量 | 默认 | 说明 |
|---|---|---|
| `NIKE_MP_REQUEST_RETRY_LIMIT` | `2` | 403/429 退避重试上限（离线不按真实节奏） |
| `NIKE_MP_THROTTLE_BACKOFF_SECONDS` | `30` | 风控冷却阶梯（真实默认 `300,900,1800,3600`） |
| `NIKE_MP_JOB_RETRY_DELAY_SECONDS` | `5` | 补跑作业的最小等待（真实默认 `60`） |
| `QA_QUOTA_HOURLY_CAP` | `900` | 平台小时闸，冷却专项显式设为 `2` |

`qa/smoke.py` 用 `QA_THROTTLE_BACKOFF_SECONDS` / `QA_JOB_RETRY_DELAY_SECONDS` 对齐期望窗口，
换值启动栈时把同样的值传给脚本即可。

### 调度主备与监控（双 scheduler / `monitor` 服务）

自包含 QA 栈（`qa/compose.auth*.yaml`）与根 `compose.yaml` 一样带 `monitor` 服务
（`python -m app.monitor_main`）：它只写心跳并实时核对每个活跃任务的「上次执行 / 下次
预期 / 是否迟到」，不落任何调度状态。`scheduler`/`worker`/`monitor` 每个实例每拍往
`service_heartbeat` upsert 一行。

```bash
# 双 scheduler：第二个进程抢不到派发事务锁，自动待命（同一任务只派一份作业）
docker compose -p nike-mp-auth-qa -f qa/compose.auth.yaml -f qa/compose.stub.yaml \
  up -d --scale scheduler=2
docker compose -p nike-mp-auth-qa -f qa/compose.auth.yaml -f qa/compose.stub.yaml ps
docker compose -p nike-mp-auth-qa -f qa/compose.auth.yaml -f qa/compose.stub.yaml \
  exec -T db psql -U nike_mp -d nike_mp \
  -c "select service, instance_id, last_seen_at from service_heartbeat order by service, instance_id" \
  -c "update monitor_task set next_run_at = now() - interval '1 minute' where id = '<task>'::uuid" \
  -c "select count(*) from scan_job where task_id = '<task>'::uuid and status = 'QUEUED'"

# 运维接口（ops 账号）：实例心跳 / worker 列表 / 迟到任务 / 冷却账号 / 队列深度
curl -b cookies.txt http://127.0.0.1:18000/api/v1/ops/scheduler-health
```

等两拍（该栈 `NIKE_MP_SCHEDULER_INTERVAL_SECONDS=5`，约 15 秒）后统计应为 **1**
（advisory lock 让每一拍只有一个 scheduler 真正派发）。

### 关于手机号

平台侧一个手机号只能绑定一个平台账号（`credential.uq_credential_phone_active`，
`create_attempt` 在发码前即拒绝重复绑定并返回 409 `NIKE_PHONE_CONNECT_UNAVAILABLE`）。
因此 smoke 让 ops 用内置的 `+8613800000000`（default 身份，保留场景语义），A/B 用
stub 登记的两个号码；smoke 中专门断言了「第二个账号绑定同一手机号被拒」，作为该
约束的证据。

## 浏览器测试电池（`qa/browser/`）

把对抗性浏览器检查做成可复用脚本：对着任意部署（本地 / QA / 生产）都能一键跑，失败退出码 1。

```bash
cd qa/browser && npm install
NKMP_BROWSER_URL=https://<目标> NKMP_BROWSER_EMAIL=<账号> NKMP_BROWSER_PASSWORD=<密码> npm run battery
```

覆盖：连点保存不重复、CSRF 丢失自愈、恶意渠道名转义、超长名称拒绝、非法深链空态、
控制台/混合内容巡检、双标签页登出传播。用例 id、变量与约定见 `qa/browser/README.md`。

## 上游模式：默认真实 Nike，离线 smoke 显式叠加 stub

**默认真实**：`qa/compose.auth.hostnet.yaml` / `qa/compose.auth.yaml` 的
api/worker/scheduler 直接使用 `api.nike.com.cn` / `api-test.nike.com.cn` /
`accounts.nike.com.cn`，并按生产节奏轮询与限速——与生产同构，不需要任何手工切换。

```bash
docker compose -p nike-mp-auth-qa -f qa/compose.auth.hostnet.yaml up -d
```

**离线 smoke**：叠加 stub 文件后上游才切回本地假服务（速度优先、不触碰真实 Nike）：

```bash
docker compose -p nike-mp-auth-qa -f qa/compose.auth.hostnet.yaml \
  -f qa/compose.stub.hostnet.yaml up -d
QA_API=http://127.0.0.1:18000 QA_STUB=http://127.0.0.1:19100 \
  apps/api/.venv/bin/python qa/smoke.py
```

- `qa/smoke.py` 启动时先查 `/ops/health/detail` 的 `nike_api_base`：**上游不是本地 stub 会直接拒绝运行**，
  避免在真实模式下发真短信、查真实 Nike。
- 真实模式下平台侧限额仍生效（每账号每小时 5 次发码、每号码每天 10 次）。
- 数据归属：门店/货号/尺码表目录都通过**用户自己的 Nike 连接**取得，只属于该用户（缓存）；
  绑定成功的瞬间会自动预加载，界面显示环形进度。

## 钉钉机器人（可选，默认关闭）

平台自己持有一个钉钉企业内部应用机器人：用户在网页「账户与额度 → 消息推送 → 钉钉机器人」
生成一次性验证码，在群里 @ 机器人或单聊发给它即完成「账号 ↔ 会话」绑定；之后关键字指令
都用**绑定账号**的鉴权与数据回答，提醒也推到这个会话（自动生成 `dingtalk_app` 渠道）。

配置（凭据只进部署环境，绝不入库；QA 用仓库外的 override 文件注入）：

```yaml
# /tmp/qa-bot-override.yaml 示例（不要提交）
services:
  api:
    environment:
      NIKE_MP_DINGTALK_BOT_ENABLED: "true"
      NIKE_MP_DINGTALK_BOT_CLIENT_ID: "ding…"     # 应用 AppKey / robotCode
      NIKE_MP_DINGTALK_BOT_CLIENT_SECRET: "…"     # 应用 AppSecret
      NIKE_MP_DINGTALK_LINK_CODE_TTL_SECONDS: "3600"
```

```bash
docker compose -p nike-mp-auth-qa -f qa/compose.auth.hostnet.yaml \
  -f /tmp/qa-bot-override.yaml up -d --build api
# 日志出现 wss://wss-open-connection-union.dingtalk.com 即已连上钉钉
```

已支持指令（在钉钉里发「帮助」查看）：`绑定/解绑/注销`、`任务/概览/额度/变化[补货|售罄|变价]/门店/货号/详情 <货号>/状态`、
`检查/暂停/启用/删除`、`新建`、`加货号/删货号`、`加门店/删门店`、`盯`、`频率`、`通知`、
`上次检查/失败门店/重试失败`。写操作全部作用于绑定账号自己的任务。

## 已知问题（`app/` 侧，本轮只记录未修改）

### 登录响应后会话短暂不可见（可复现竞态）

**现象**：`POST /api/v1/auth/login` 返回 200 后立刻发起下一个请求，偶发
`401 {"detail":"session not found"}`。

**最小复现**（对运行中的 QA api 反复执行）：

```python
for i in range(40):
    c = httpx.Client(base_url="http://127.0.0.1:18000")
    assert c.post("/api/v1/auth/login",
                  json={"email": "qa-a@qa.local", "password": "qa-password"}).status_code == 200
    assert c.get("/api/v1/auth/me").status_code == 200   # 观测到约 5/40 次为 401
```

本沙箱实测：40 次紧凑循环中 5 次 401（约 12%）。

**证据**：登录响应本身耗时 ~80–170ms，而新会话在 `/auth/me` 上可见还要再晚
~10–150ms；同一 cookie 稍后重试即成功。

**根因**：`app/api/deps.py` 的 `get_db` 在生成器 teardown 里 `commit()`，而 FastAPI
（0.106+，本环境 0.141.1）在**响应发出之后**才运行依赖的退出代码，因此新写入的行
在响应到达客户端时可能尚未提交。**所有“写请求返回 2xx 后立刻读同一行”的模式都受
影响**，不只是登录：

- 登录后立刻请求 → `401 session not found`（实测 ~12%，见上）；
- `POST /tasks` 201 后立刻 `POST /scans {task_id}` → 可能 `404 任务不存在`；
- `DELETE /me/nike-credentials/{id}` 204 后立刻发起扫描 → 可能仍读到旧绑定（不返回 409）；
- `POST /scans/{id}/retry-failed` 后立刻轮询 → 可能先读到重排前的终态快照。

**影响面**：任何“写后紧接读”的自动化/前端快速跳转都可能偶发失败；人工操作的浏览器
几乎不会命中。**未修改 `app/` 代码**；`qa/smoke.py` 通过 `User._eventually` 等待写入
可见、并用 `wait_job(..., previous_finished_at=...)` 忽略重排前的终态来规避上述四种。

## 说明

- 该环境用于阶段 B（离线联调）、阶段 C（故障注入）与自助绑定验收；
  真实接口冒烟（阶段 D）必须使用独立授权账号并单独评审。
- stub 只实现只读接口，任何写操作（购物车写入/下单/支付）都不会被模拟。
