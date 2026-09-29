# MVP 联调计划

> 原则：先离线、后真实；先小范围、后扩量。阶段 D 之前不得连接真实 Nike；任何阶段发现库存假阳性、跨租户泄露或凭证轮换丢失，立即停止并回到对应回归。

## 阶段 A：冻结最小契约（已完成）

| 契约点 | 冻结结论 |
|---|---|
| 门店“全部/显式选择” | 空门店列表 = 全部已同步门店；任务存 `all_stores=true`，调度与手动扫描时解析为当刻活跃门店 |
| 尺码范围 | 未实现选择器；当前按门店结构全量尺码（后续迭代项） |
| CSRF 恢复 | 登录下发可读 `nike_mp_csrf` Cookie；写请求头 `X-CSRF-Token` 优先取内存值，其次 Cookie |
| 任务↔扫描关联 | `ScanJob.task_id`；`GET /scans?task_id=` 过滤；任务详情页按此过滤 |
| 进度状态 | `QUEUED/RUNNING/SUCCEEDED/PARTIAL/FAILED/CANCELLED`；SSE `progress`/`done` + 轮询回退 |
| 最近成功/最近尝试 | 矩阵展示最近成功（`checked_at`）；每次尝试与失败原因见扫描详情页（矩阵级“最近尝试”未实现） |
| 共享观测 vs 租户事件 | 库存变化事件为共享事件（全体租户可见）；扫描失败事件归发起租户 |
| 请求估算 | 无可信估算时显示“暂无法可靠预估”，不展示硬编码公式 |

## 阶段 B：隔离环境离线联调（QA 环境已就绪）

```bash
docker compose -p nike-mp-qa -f compose.yaml -f qa/compose.qa.yaml up -d --build
docker compose -p nike-mp-qa -f compose.yaml -f qa/compose.qa.yaml \
  exec -T api python - < qa/seed_qa.py
apps/api/.venv/bin/python qa/smoke.py
```

覆盖路径：**登录 → 创建任务 → 调度 → 采集 → 检查点 → 矩阵 → 变化流水 → 重试**。
关键断言（详见 `qa/smoke.py`）：

1. 平台运维通过 stub 同步 2 家门店；迁移门禁先行，api 健康后才启动 web。
2. 客户租户创建“全部门店”任务并触发扫描，逐店完成并写入观测。
3. 矩阵：尺码 40 = 无货、42/44/46 = 有货且带价格；结构按店不同（QA002 多一个尺码）。
4. 不可信轮：门店标记 `UNKNOWN`、Job 不伪装成功、**不产生任何变化事件**。
5. “仅重试失败门店”：翻转 stub 场景后重扫，产生两条尺码 40 的 RESTOCK（共享事件，租户可见）。
6. 配额计数随真实请求增长。

**执行记录**：见文末“执行结果”。

## 阶段 C：故障与并发联调（自动化覆盖 + 待注入清单）

已在自动化中验证（真实 PostgreSQL）：

- 进程中止后已完成检查点保留（T03 `test_completed_store_is_committed_before_a_crash`）。
- 租约被抢占后旧执行者不写结果、不终结任务（T03 `test_stolen_lease_stops_the_old_worker`）。
- 两 worker 不能同时持有同一任务租约（T03 `test_only_one_worker_can_hold_the_lease`）。
- 并发扣量不超预算（T07 8 线程 × 5 次尝试 = 恰好 10 次成功）。
- 不可信上游轮次不覆盖可信基线（T06）。

QA 环境待注入（阶段 C 补跑，不在本轮结果内）：

| 注入 | 预期 | 检查 |
|---|---|---|
| `docker compose stop db` 后触发扫描 | 明确报错、任务失败但不丢检查点 | worker 日志 + `scan_job_store` 状态 |
| `docker compose kill worker` 中途 | 租约过期后由新 worker 续跑，已完成门店不重跑 | `attempt` 与观测行数 |
| stub 返回 401 后 `/token/v1` 失败 | 凭证置 `NEEDS_AUTHORIZATION`，不无限重试 | `credential` 表 + 日志 |
| 重复提交同一任务 | 幂等：同一 cadence 内不堆叠扫描 | `scan_job` 计数 |

## 阶段 D：受控真实接口冒烟（未执行）

前置条件：

1. P1 回归全部通过（已满足）；QA 环境冒烟通过（见执行结果）。
2. 确认旧脚本/计划任务已停止，避免双系统并发轮换同一 refresh token。
3. 单一授权账号、一个租户、一家门店、一个货号、一个周期；低预算 + 停止开关就绪。

步骤：导入真实凭证 → 单店单货号扫描一次 → 核对矩阵与原始响应 → 观察 1 小时后扩到 3 家门店。
禁止：购物车修改、下单、支付；只读接口以外的任何调用。

### 阶段 D 预检（2026-09-29，部分）

- 出网连通性：`www.nike.com.cn` 静态配置 HTTP 200（未携带站点 Cookie）。
- 真实尺码结构（免登录结构接口，平台同款调用）：门店 `3875` × 货号 `DN3707-202` → HTTP 200，Air Jordan 3 Retro 男子运动鞋，26 个尺码及真实 GTIN。
- 门店清单接口（需鉴权）：携带应用头但无 Authorization → HTTP 403。
- 库存/价格审核（需鉴权 + HTTP/2）：HTTP/1.1 直接探测命中 WAF 405（`data-spm` 页面），与 F-01 记载一致；平台客户端已启用 `http2=True`，完整验证仍需有效小程序 JWT。
- 边界：未做购物车写入、下单、支付等任何非只读调用；未使用浏览器站点 Cookie（`www.nike.com.cn` 域与小程序 API 域不是同一套凭证）。

### 阶段 D 真实只读探测（2026-09-29，进行中）

使用用户提供的 refresh token（仅 refresh token，平台自动换取 access token 并持久化轮换结果）执行受控只读探测：

| 项 | 结果 |
|---|---|
| 凭证刷新 | ✅ access token 正常；refresh token 已轮换并加密持久化 |
| 门店清单（鉴权） | ✅ 166 家门店可读 |
| 3875 × DN3707-202 | 26 个尺码，两个 regionId（`xian`/`NIKE0S08`）均为 `NOT_CARRIED`（该店不售，非参数问题） |
| **3752 × DN3707-202** | **18 个尺码：6 个在售、12 个缺货**；在售尺码 41 / 42 / 42.5 / 43 / 44 / 44.5；缺货 40 / 40.5 / 45–52.5 共 12 码 |
| 价格与促销 | 现价 1399、折扣 559.6、实付 839.4；促销「指定鞋/服/配件 单笔消费满988及以上享6折」。该响应未返回 `fullPrice`（原价），平台按 `total_price` 判定价格可用 |
| WAF 观察 | 连续请求后出现 405 间歇挑战（对应 F-02）；等待约 2 分钟后以生产默认重试（7 次退避）成功；明细抓取共消耗 26 笔 review 事务 |

边界：全部为只读请求（结构 + 可用性 + 价格），未做购物车写入/下单/支付。

## 阶段 E：小范围持续观察（未执行）

连续 ≥24h，记录：成功率、`UNKNOWN` 比例、401/405 次数、实际 TX 用量（对齐 900/h、60k/天）、检查点恢复次数、数据新鲜度。
停止条件（立即）：库存假阳性、跨租户泄露、凭证轮换丢失。

## 执行结果

### 阶段 B：离线联调（2026-09-29，通过）

执行环境与命令：

```bash
docker compose -p nike-mp-qa -f qa/compose.qa.hostnet.yaml up -d --build
docker compose -p nike-mp-qa -f qa/compose.qa.hostnet.yaml exec -T api python - < qa/seed_qa.py
apps/api/.venv/bin/python qa/smoke.py
```

结果（`SMOKE PASS`，21.7s）：

```
[1/6] store sync ok: 2 stores
[2/6] task created: 8e175a06-… (all stores)
[3/6] baseline scan succeeded: 2 stores
[4/6] matrix ok: 40=OUT_OF_STOCK, 42/44/46=IN_STOCK with price
[5/6] untrusted round honest: FAILED / unknown=2
[6/6] retry + events ok: [('QA001', '40'), ('QA002', '40')]; quota usage=44
```

关键观察：

- 迁移门禁生效：`migrate` 先行并成功退出后才启动 api/worker/scheduler/web；api 健康检查通过后才启动 web。
- 首轮只建立基线，不产生补货/售罄/变价事件。
- 不可信轮：门店标记 `UNKNOWN`、任务状态如实为 `FAILED`、**不产生任何库存类事件**，同时留下 2 条租户私有 `SCAN_FAILED` 诊断（断言按事件类型区分）。
- “仅重试失败门店”只重跑未完成门店；stub 场景切换后产生两条尺码 40 的 RESTOCK，门店/尺码与预期完全一致。
- 配额按真实请求逐笔增长（本轮基线 + 不可信 + 重试合计 44 笔）。

环境说明（真实发现，非代码缺陷）：本沙箱的 Docker 无法在两个容器之间建立桥接网络（同一网络内 `db:5432` TCP 超时，`ping`/`nc` 同样失败，跨容器 `ConnectionTimeout` 导致 `migrate` 退出 1），而主机→已发布端口正常。因此本轮改用 `qa/compose.qa.hostnet.yaml`（host 网络、端口 127.0.0.1、独立项目名与数据卷）执行；在容器网络正常的环境中应使用叠加方案 `-f compose.yaml -f qa/compose.qa.yaml`，两种方案的断言完全一致。

### 阶段 A/B 与自动化测试的对应

| 冒烟步骤 | 对应自动化回归 |
|---|---|
| 门店同步、凭证读取 | T02/T04 |
| 首轮只建基线、矩阵六态与价格 | T05/T06 |
| 不可信轮诚实降级、无库存事件 | T01/T06 |
| 仅重试未完成门店、检查点 | T03 |
| RESTOCK 共享事件与租户可见性 | T02/T05 |
| 配额逐笔计量 | T07 |

### 阶段 C–E

C（故障与并发注入）未执行，其关键分支已由 T03/T07 在真实 PostgreSQL 上覆盖（进程中止、租约抢占、并发扣量），其余注入项清单见上；D 仅完成预检（见上），受控真实冒烟与 E（24h 观察）未执行。

