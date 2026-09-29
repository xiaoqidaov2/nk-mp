# MVP 代码审阅报告（修复后复核版）

> 审阅对象：`codex/mvp`（提交 `a595f61`）后续修复；范围：MVP 应用代码、数据库模型、迁移与部署配置。
> 本报告在原“存在阻断问题、不可上线”的审阅结论基础上，逐项记录修复与验证证据。
> 结论：**原 10 项 P1 阻断问题已全部修复并有回归测试；P2 中迁移与部署门禁已修复，任务目标关系按 JSONB+校验的过渡方案实现。上线前仍需完成真实接口冒烟（阶段 D）与 24h 观察（阶段 E），本报告不宣称已通过。**

## 一、P1 问题修复对照

| # | 问题 | 修复 | 证据 | 状态 |
|---|---|---|---|---|
| 1 | `/changes` 缺租户隔离；登出后前端缓存串租户 | 共享事件（`tenant_id IS NULL`）与本租户私有事件（扫描失败）可见，其他租户私有事件不可见；前端登录/退出 `queryClient.clear()` | `app/api/changes.py:24-40`、`app/api/dashboard.py:44-54`、`web/src/auth/AuthContext.tsx`；T02 用例 | ✅ |
| 2 | 空/畸形错误响应被当成“全部有货” | `_parse_review` 校验 payload/response/groups 形状；`_parse_error_review` 要求非空、格式正确、索引在范围内的错误列表；未知错误码/条目非对象/冲突/空列表一律 `UNTRUSTED` | `app/nike/client.py:301-426`；T01 共 19 用例（含参数化畸形输入） | ✅ |
| 3 | 检查点和心跳未独立持久化；旧执行者可覆盖新结果 | 每个门店单元完成即 `commit`；心跳提交后探测；`leased_by` + 条件更新 + 写结果前租约校验，租约丢失即 `rollback` 并退出，不写任何结果 | `app/services/jobs.py`（`heartbeat/_lease_held/run_job`）、`app/worker.py`；T03 4 用例 | ✅ |
| 4 | 被拒绝但未到期的 token 无法续期 | 401 时以“被拒绝的 token 版本”为条件强制刷新；已被其他 worker 轮换则直接复用；进程内单飞 + 行锁；刷新结果独立提交；失败冷却防止重试风暴 | `app/services/credentials.py:79-199`；T04 9 用例 | ✅ |
| 5 | 刷新页面后 CSRF 丢失（写操作 403）；运维 GET 也要求 CSRF | 登录同时下发可读 `nike_mp_csrf` Cookie，前端刷新后回读；`require_ops`（只读，仅角色校验）与 `require_ops_write`（CSRF+角色）拆分 | `app/api/auth.py:66-89`、`app/api/deps.py:83-97`、`web/src/data/client.ts`；T02 3 用例 | ✅ |
| 6 | 创建向导确认页丢失提交字段；“不选门店=全部”与后端不一致 | 表单单实例跨步骤常驻（未挂载丢失问题消除），确认页提交 `validateFields()` 结果；后端将空门店列表存为 `all_stores`，调度时解析全部活跃门店 | `web/src/pages/TaskCreatePage.tsx`、`app/api/tasks.py:62-102`、`app/services/scheduler.py:31-70`；T08 2 用例 + 调度器集成用例 | ✅ |
| 7 | SSE 进度流立即结束 | 改为异步生成器：`await request.is_disconnected()`、线程池读取、`asyncio.sleep`，终态发送 `done` | `app/api/scans.py:185-247`；后端 5 用例 + 前端订阅/轮询回退 2 用例 | ✅ |
| 8 | 配额按 GTIN 计数、库存与价格各用一份完整额度、全局闸未生效 | 按真实事务（PUT/GET/重试）逐笔计量并即时提交；租户共享单一日额度；平台日闸（60,000）+ 小时闸（900）；超过 600/h 记录慢速观察日志 | `app/services/quota.py`、`app/nike/client.py:130-137`、`app/services/probe.py`；T07 6 用例（含 8 线程并发原子性） | ✅（真实账号承载未验证） |
| 9 | 长断档与结构变化未保护基线 | `max_gap_seconds`（默认 7 天，且 ≥3×任务周期）内才比较；同尺码 GTIN 变化视为身份变化；结构变化轮仅重建基线并记录 `STRUCTURE_CHANGED` 诊断事件 | `app/services/observations.py`、`app/services/probe.py`；T06 6 用例 | ✅ |
| 10 | 全局当前态与租户事件归属不匹配 | 契约冻结：**库存变化事件属于共享观测**（`tenant_id=NULL`，全体租户可见）；**扫描失败事件属于发起租户**；`/changes` 与概览按“共享 + 本租户”口径统计 | `app/services/observations.py:106-120`；T02、T05 用例 | ✅（订阅分发按共享事件模型简化，见下） |

## 二、P2 修复对照

| # | 问题 | 修复 | 证据 | 状态 |
|---|---|---|---|---|
| 11 | 关键关联缺失；任务详情按 `kind` 过滤 | 新增外键：`Observation.job_id`、`MonitorTask.created_by`、`ScanJob.requested_by`、`ChangeEvent.before/after_observation_id`、`ObservationLatest.last_observation_id`；扫描列表支持 `task_id` 过滤并在前端用于“该任务的扫描记录”；任务创建校验门店存在性并登记货号 | `app/models.py`、`app/api/scans.py:115-128`、`web/src/pages/TaskDetailPage.tsx`；T05 8 用例、T02 用例 | ✅（JSONB 数组保留，见下） |
| 12 | 迁移使用 `create_all`；Compose 无迁移门禁 | `0001_initial_schema` 改为冻结的显式迁移（自动生成后固定，`alembic check` 无漂移）；新增 `migrate` 服务，api/worker/scheduler 等待其成功；api 健康检查，web 等待 api 健康；镜像安装改为 `npm ci` | `alembic/versions/0001_initial_schema.py`、`compose.yaml`、`apps/web/Dockerfile`；T09 用例 | ✅（Docker 冷启动冒烟于 QA 环境执行） |

## 三、实现过程中额外发现并修复的缺陷

| 缺陷 | 说明 | 修复 |
|---|---|---|
| 前端类型检查在本环境不可复现 | 缺 `@types/node`，`vite.config.ts` 报错 | 增加 devDependency 与 tsconfig types（并因此暴露 Dashboard 事件标签缺项） |
| 旧价伪装新价 | 价格缺失轮次保留旧价格字段 | 价格缺失即清空缓存价并置 `MISSING`（T06 用例） |
| “数据不完整”门店无法重试 | 重试仅覆盖 `FAILED` | 重试覆盖 `FAILED + UNKNOWN`（后端 + 前端按钮条件），T03 用例 |
| 门店处理顺序不确定 | `ScanJobStore` 无排序导致断点续跑顺序漂移 | `order_by(ScanJobStore.id)`，T03 用例固定崩溃点 |
| Web 镜像安装不可复现 | 未使用锁文件 | `COPY package-lock.json` + `npm ci` |

## 四、验证证据（本轮实际执行）

- 后端：`ruff check app tests` 通过；单元 + 集成 **92 项全部通过**（单元 57 + PostgreSQL 集成 35）。
- 覆盖率（`pytest --cov=app`）：总计 71%；核心判定逻辑——配额 99%、观测/事件 91%、凭证 85%、探测 82%、任务执行 75%、上游客户端 66%。
- 前端：`npm run typecheck` 通过；`npm test` 11 项通过（组件 4、CSRF 3、SSE 回退 2、创建向导 2）。
- 迁移：空库 `upgrade head`、重复执行、`alembic check` 漂移检查、`downgrade base` 再升级全部通过（T09）。
- QA 隔离环境（阶段 B）：`docker compose -p nike-mp-qa -f qa/compose.qa.hostnet.yaml up -d --build` → 迁移门禁 → 种子 → `qa/smoke.py` **SMOKE PASS**（登录→创建任务→调度→采集→检查点→矩阵→变化流水→重试，21.7s；含不可信轮诚实降级与 RESTOCK 事件断言）。详见 `docs/mvp-integration-plan.md`。
- 未执行（不得标记为通过）：真实 Nike 接口冒烟、真实账号承载与限流验证、24h 持续观察。

## 五、明确保留的差距与后续项

- 任务门店/货号仍为 JSONB 数组（已加存在性校验与 API 契约），未归一化为关联表——作为后续演进项，期间无法在数据库层保证引用完整性。
- 事件订阅按“共享观测事件对所有租户可见”的简化模型实现，未按 (门店,货号) 订阅关系做逐租户分发；对 MVP 的“同店同款共享事实”语义是自洽的，但小租户会看到与其任务无关的公共事件。
- 每小时突发线（20 TX/60s）为观测指标，未做硬限流；900 TX/h 与 600 TX/h 分别以硬闸与日志观察实现。
- 尺码范围选择、单店页面首货号限制、CSV 导出、移动端只读、最近尝试时间展示：未实现（后续迭代）。
- 外部通知、账号池、自动浏览器授权：按计划明确延期，不计入本轮准入。
