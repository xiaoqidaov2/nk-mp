# 需求 → 代码 → 用例 → 结果 → 缺陷 追踪表

> 状态口径：**已实现**（代码+自动化用例，未含真实接口验证）、**部分实现**、**未实现**、**明确延期**。
> 结果列只记录本轮实际执行过的命令与结论；“未运行真实接口”不是失败，但也不能记为通过。

## A. 缺陷修复追踪（审阅问题）

| 需求/问题 | 代码位置 | 用例 | 结果 | 状态 |
|---|---|---|---|---|
| P1-1 `/changes` 租户隔离 + 登出清缓存 | `api/changes.py`、`api/dashboard.py`、`web AuthContext.tsx` | T02 `test_changes_feed_shows_shared_and_own_events_only` | 通过 | 已实现 |
| P1-2 异常响应不得变为库存结论 | `nike/client.py` 解析加固 | T01 19 用例 | 通过 | 已实现 |
| P1-3 检查点/心跳独立持久化 + 租约所有权 | `services/jobs.py`、`worker.py` | T03 4 用例 | 通过 | 已实现 |
| P1-4 401 强制刷新与独立持久化 | `services/credentials.py` | T04 9 用例 | 通过 | 已实现 |
| P1-5 CSRF 刷新恢复 + ops 只读权限拆分 | `api/auth.py`、`api/deps.py`、`web client.ts` | T02 3 用例 + 前端 3 用例 | 通过 | 已实现 |
| P1-6 创建向导字段保持 + 全部门店语义 | `web TaskCreatePage.tsx`、`api/tasks.py`、`services/scheduler.py` | T08 2 用例 + 调度器集成 1 用例 | 通过 | 已实现 |
| P1-7 SSE 异步修复 | `api/scans.py` | 后端 5 用例 + 前端 2 用例 | 通过 | 已实现 |
| P1-8 配额真实计量 + 租户/全局闸 | `services/quota.py`、`nike/client.py`、`services/probe.py` | T07 6 用例（含并发） | 通过 | 已实现（真实承载未验证） |
| P1-9 基线断档/结构变化保护 | `services/observations.py`、`probe.py` | T06 6 用例 | 通过 | 已实现 |
| P1-10 共享观测与租户事件归属契约 | `services/observations.py`、`api/changes.py` | T02、T05 用例 | 通过 | 已实现（简化为共享事件模型，见差距） |
| P2-11 关键外键 + 扫描按 `task_id` 关联 | `models.py`、`api/scans.py`、`web TaskDetailPage.tsx` | T05 8 用例、T02 过滤用例 | 通过 | 部分实现（JSONB 数组未归一化） |
| P2-12 迁移显式化 + Compose 迁移门禁 | `alembic/versions/0001_initial_schema.py`、`compose.yaml`、`web/Dockerfile` | T09 用例、QA Compose 启动 | 通过 | 已实现 |

## B. 测试计划追踪（T01–T09）

| 组 | 用例 | 结果 | 状态 |
|---|---|---|---|
| T01 | `test_review_contract.py` 19 项、`test_review_parsing.py` 6 项 | 通过 | 已实现 |
| T02 | `integration/test_api_isolation.py` 9 项 | 通过 | 已实现 |
| T03 | `integration/test_job_recovery.py` 4 项 | 通过 | 已实现 |
| T04 | `test_credentials.py` 9 项 | 通过 | 已实现 |
| T05 | `integration/test_model_constraints.py` 8 项 | 通过 | 已实现 |
| T06 | `integration/test_baseline_and_prices.py` 6 项 | 通过 | 已实现 |
| T07 | `integration/test_quota_pg.py` 6 项 + `test_scheduler_dispatch.py` 1 项 | 通过 | 已实现 |
| T08 | 前端 7 项（向导/CSRF/SSE 回退） | 通过 | 部分实现（“无重复首轮扫描”“重试后进度”未单独用例） |
| T09 | `integration/test_migrations.py`、`test_security.py`、QA Compose | 通过 | 已实现 |

## C. 联调计划追踪（阶段 A–E）

| 阶段 | 内容 | 结果 | 状态 |
|---|---|---|---|
| A 冻结最小契约 | 全部门店=空列表/`all_stores`、尺码范围（未实现选择器）、CSRF 恢复、任务↔扫描 `task_id`、进度状态、最近成功时间 | 契约落代码与文档；尺码范围未实现 | 部分实现 |
| B 隔离环境离线联调 | `qa/`（stub、Compose 两种网络方案、种子、冒烟脚本）走通 登录→创建→调度→采集→检查点→矩阵→变化流水→重试 | **SMOKE PASS**（21.7s）：首轮仅基线；不可信轮无库存事件且有 SCAN_FAILED 诊断；重试产出预期 RESTOCK；配额 44 笔 | 已实现（阶段 B 通过） |
| C 故障与并发联调 | 真实 PG 上注入中止/租约抢占/并发扣量/不可信轮询（部分已在 T03/T07 覆盖）；数据库异常、token 轮换失败注入待补 | 部分 | 部分实现 |
| D 受控真实接口冒烟 | 单账号/单租户/单店/单货号/单周期 + 低预算 | 未执行（需授权账号与停旧脚本确认） | 未实现 |
| E 24h 持续观察 | 成功率、UNKNOWN 比例、401/405、用量、恢复、新鲜度 | 未执行 | 未实现 |

## D. 产品需求完成度（相对 requirements/ 文档）

| 需求 | 状态 | 说明 |
|---|---|---|
| 尺码级库存六态、可信度、未探测不造假 | 已实现 | 六态 + TRUSTED/UNTRUSTED，UNKNOWN 不入状态机 |
| 门店实付价/促销（≤3 件全可买才返回） | 已实现 | 价格阶段独立批次；缺价显示待补价 |
| 结构按店缓存（TTL 7 天）、结构变化诊断 | 已实现 | `gtin_structure` + `STRUCTURE_CHANGED` |
| 变化事件（补货/售罄/变价/到店上新/扫描失败） | 已实现 | 共享/私有归属见 P1-10 |
| 任务调度、断点恢复、仅重试未完成 | 已实现 | 检查点逐门店提交 |
| 配额（租户日额、平台日/小时闸） | 已实现 | 900/h、60k/天；20 TX/60s 突发线未硬限；600/h 为观察线 |
| 创建向导尺码选择 | **未实现** | requirements C 文档要求 |
| 请求量预估 | 部分实现 | 改为“预估不可用”，未提供可信估算 |
| 单店页面多货号 | **未实现** | 当前仅首个货号 |
| CSV 同步导出 | **未实现** | 计划外但列为需求 |
| 移动端只读限制 | **未实现** | |
| 事件通知（钉钉/企业微信等）、账号池、自动浏览器授权 | 明确延期 | 本轮不计入准入 |
| “当前库存状态”与“历史事件”的订阅分发 | 部分实现 | 共享观测事件对所有租户可见；未按订阅关系分发 |
| 最近成功/最近尝试时间 | 部分实现 | 矩阵显示最近成功（`checked_at`）；最近尝试/失败见扫描详情 |

## E. 关键未验证项（禁止标记为通过）

| 项 | 原因 |
|---|---|
| 真实 Nike 接口契约与只读边界 | 未执行受控真实冒烟 |
| 单账号 900 TX/h 与 60k/天承载 | 无真实运行数据 |
| Docker 冷启动全链路（迁移门禁→健康检查→web） | 见联调记录（QA 环境执行情况） |
| SSE 长连接在代理/弱网下行为 | 仅生成器级验证 |
| 覆盖清理/分区/备份恢复演练 | 未实现（后续迭代） |
