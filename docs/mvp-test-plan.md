# MVP 测试计划与执行记录

> 分层策略：pytest 单元测试 + PostgreSQL 集成测试 + HTTP 契约回放 + Vitest 组件/流程测试。
> 自动化测试全部 mock 上游，**禁止访问真实 Nike 接口**。
> 执行环境：Python 3.14 venv（`apps/api/.venv`）、本地 PostgreSQL 17（端口 55432，独立测试库 `nike_mp_test`，通过 `NIKE_MP_TEST_DATABASE_URL` 显式启用）、Node 24 + Vitest。

## 运行方式

```bash
# 后端单元测试（无数据库）
cd apps/api && .venv/bin/python -m pytest -q

# 后端集成测试（需要独立测试库；未设置时整个目录自动跳过）
NIKE_MP_TEST_DATABASE_URL="postgresql+psycopg://postgres:postgres@127.0.0.1:55432/nike_mp_test" \
  .venv/bin/python -m pytest -q

# 覆盖率
NIKE_MP_TEST_DATABASE_URL="..." .venv/bin/python -m pytest --cov=app --cov-report=term-missing

# 前端
cd apps/web && npm run typecheck && npm test
```

## 测试组与结果

| 测试组 | 场景 | 断言要点 | 用例文件 | 结果 |
|---|---|---|---|---|
| T01 上游契约与容错 | 空完成、`errors: []`、缺 `errors`、非对象错误、未知错误码、部分明细、数量限制、401/405/超时、限额监听 | 不确定数据不得变为有货/无货；失败原因可定位；重试与刷新逐笔计费 | `tests/test_review_contract.py`、`tests/test_review_parsing.py` | ✅ 19+6 通过 |
| T02 会话与租户隔离 | A/B 租户、只读角色、刷新后 CSRF、退出登录、跨租户任务/扫描 ID、运维只读 GET | 无越权；无缓存串租户；只读 GET 无需 CSRF；非法写被拒 | `tests/integration/test_api_isolation.py` | ✅ 9 通过 |
| T03 任务与事务恢复 | 单单元失败、进程中止、租约过期/被抢、两个 worker、仅重试未完成 | 已提交检查点不丢；续跑不重复写；旧执行者不能覆盖新结果 | `tests/integration/test_job_recovery.py` | ✅ 4 通过 |
| T04 凭证生命周期 | 提前过期、未到期 401、并发刷新、刷新后进程退出、refresh 失效 | 受控单次刷新；新凭证独立持久化；失败不无限重试 | `tests/test_credentials.py` | ✅ 9 通过 |
| T05 数据模型与事件 | 外键、唯一约束、跨租户事件可见性、观测追溯、乱序写入 | 约束阻止非法关联；事件可追溯到前后观测 | `tests/integration/test_model_constraints.py` | ✅ 8 通过 |
| T06 基线、价格与展示 | 首轮、8 天断档、GTIN 更换、结构变化、缺价后补价、失败保留旧状态 | 首轮/重建基线不误报；旧价不伪装新价 | `tests/integration/test_baseline_and_prices.py` | ✅ 6 通过 |
| T07 配额与调度 | PUT/GET/重试计数、租户共享桶、全局/小时闸、并发扣量、跨日 | 真实请求计量；总闸不可绕过；重试不漏计 | `tests/integration/test_quota_pg.py`、`test_scheduler_dispatch.py` | ✅ 6+1 通过 |
| T08 前端主流程 | 创建向导往返、提交字段完整、CSRF 刷新恢复、SSE/轮询回退 | 提交字段完整；进度可恢复 | `web/src/pages/TaskCreatePage.test.tsx`、`web/src/data/client.test.ts`、`web/src/data/scanProgress.test.ts` | ✅ 7 通过（“无重复首轮扫描”未单独用例，见追踪表） |
| T09 部署与可维护性 | 空库启动、重复迁移、漂移检查、降级重建、缺密钥拒绝 | 迁移版本固定；故障明确报错 | `tests/integration/test_migrations.py`、`tests/test_security.py` | ✅ 1+ 通过；Compose 冷启动见联调记录 |

## 覆盖率（核心判定逻辑）

| 模块 | 行覆盖率 |
|---|---|
| `services/quota.py` | 99% |
| `services/observations.py` | 91% |
| `services/credentials.py` | 85% |
| `services/probe.py` | 82% |
| `services/jobs.py` | 75% |
| 总计（含 API 层） | 71% |

> 覆盖率达到 ≥70% 的准入线；覆盖率不作为业务断言的替代——每项判定均有显式行为断言。

## 测试纪律

- 每个确认缺陷先写能暴露问题的回归用例，再修复（见提交历史与追踪表）。
- PostgreSQL 锁、事务、唯一约束、JSONB 行为全部在真实 PostgreSQL 上验证；FakeSession 仅用于纯函数与小型协作者。
- 集成测试通过 `NIKE_MP_TEST_DATABASE_URL` 显式启用，运行前 TRUNCATE 单库，绝不触碰运行时数据库。
- 测试凭证为专用伪值（`test-key-for-unit-tests`、`qa-access-token` 等），fixture 不含真实凭据。
- 时钟相关断言使用相对时间；跨日边界通过 monkeypatch 时钟验证。

## 未覆盖/未验证（如实记录）

- 真实 Nike 接口契约与账号承载、限流实际触发、24h 运行指标（阶段 D/E 前不得标记通过）。
- Playwright 端到端测试未引入；当前以 Vitest + API 级集成覆盖关键流程。
- SSE 在真实代理/断线场景下的长连接行为仅做了生成器级与前端回退级验证。
