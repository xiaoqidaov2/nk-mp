# 架构说明

## 组件

| 组件 | 职责 |
|---|---|
| `web` | React 前端，仅访问同源 `/api/v1`，不直接调用 Nike 接口、不保存任何 token |
| `api` | FastAPI 服务：会话鉴权、任务与扫描编排、矩阵与流水查询、SSE 进度 |
| `worker` | 领取排队中的扫描任务并执行采集，带租约与心跳；崩溃后由其它 worker 接管 |
| `scheduler` | 按 cadence 把启用的监控任务转换为扫描任务 |
| `db` | PostgreSQL 17，同时承载业务数据与任务队列（`FOR UPDATE SKIP LOCKED`） |

前后端通过同源 `/api/v1` 通信，容器内由 Vite 开发服务器反代到 `api:8000`。

## 采集链路

```
scheduler → enqueue ScanJob + ScanJobStore
worker    → lease job（租约 120s / 心跳 20s）
          → 每店：取尺码结构（按店缓存，TTL 7 天）
          → 可用性审核（每批 ≤40 件，排除法）
          → 价格审核（仅对在售且 ≤3 件的小批）
          → 可信结果写 observation，并 diff 出变化事件
          → 更新 ScanJobStore 检查点
```

失败门店只影响自己那一行：状态为 `FAILED`（可重试）或 `UNKNOWN`（数据不完整，不重试语义），
两者都不会被当成「无货」，也不会写入可信基线。

## 关键不变式

1. **可信度闸门**：`trust=UNTRUSTED` 的观测不写 `observation_latest`、不参与事件检测。
2. **单调更新**：`observation_latest` 只接受 `checked_at` 更新的观测，避免旧轮次覆盖新结果。
3. **店级结构**：`gtin_structure` 以 `(store_id, style_id)` 为唯一键，禁止跨店复用尺码结构。
4. **门店去重**：门店主键为 `store_code`，`regionId` 作为别名表 `store_region_alias`，避免重复扫描与重复通知。
5. **幂等事件**：`change_event.dedupe_key` 唯一，重复执行同一轮次不会产生重复事件。
6. **实时 DOM**：矩阵表格使用真实文本节点，保证浏览器 Ctrl+F 能命中货号与门店名。

## 安全

- 密码使用 scrypt 加盐哈希；会话为 HttpOnly Cookie + 双提交 CSRF 头（`X-CSRF-Token`）。
- 采集凭证使用 AES-256-GCM 加密后存库，密钥来自 `NIKE_MP_CREDENTIAL_KEY`；未配置密钥时拒绝读写凭证。
- 角色：`ops_admin` / `ops_operator` / `tenant_admin` / `tenant_viewer`；前端隐藏入口，后端为最终边界。
- 日志与接口响应不输出 token、JWT 或 Cookie。

## 后续迭代（本版未实现）

通知推送（钉钉/企微）、账号池与多账号调度、真实浏览器兜底授权（服务端 Chromium）、
审计日志、租户级公平队列与老化提权、异步大数据导出。
