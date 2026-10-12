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
7. **个人边界**：凭证、观测、最新态、变化事件、任务与扫描都归属到具体平台用户；最新态以
   `(owner_user_id, store_id, style_id, size_label)` 为键，并按当前 credential/generation 过滤读取。
   换绑后旧账号的数据不再展示，第一轮采集只重建基线、不产生变化事件。
8. **绑定冻结**：扫描任务在入队时冻结 `owner_user_id / credential_id / credential_generation`；
   任务的每次授权请求前重新校验绑定（guard），绑定被替换或解绑即终止任务（`NIKE_CREDENTIAL_CHANGED`），
   绝不静默切换到新账号继续。已发送的 HTTP 请求无法撤回，只阻止后续请求。

## 安全

- 密码使用 scrypt 加盐哈希；会话为 HttpOnly Cookie + 双提交 CSRF 头（`X-CSRF-Token`）。
- 采集凭证按用户独立保存，使用 AES-256-GCM 加密后存库，密钥来自 `NIKE_MP_CREDENTIAL_KEY`；
  未配置密钥时拒绝读写凭证。手机号与来源 IP 仅存 HMAC 指纹（`secret_fingerprint`）。
- 自助绑定：用户在本站输入手机号→接收 Nike 短信→输入验证码，服务端完成挑战求解、会话与
  PKCE 授权码兑换，落库为个人凭证（含 refresh token，自动续期）。发码/校验有滚动限流与
  15 分钟错误验证码预算（持久化，重登不解锁）；一次性授权码/验证码从不自动重放。
- 锁顺序统一为「owner 用户行 → credential 行 → auth attempt/limit 行」，凭证读写使用独立短事务。
- 角色：`ops_admin` / `ops_operator` / `tenant_admin` / `tenant_viewer`；前端隐藏入口，后端为最终边界。
  所有登录用户都可管理**自己**的 Nike 连接（包括只读角色），扫描/任务写入仍受 `require_write` 限制。
- 日志与接口响应不输出 token、JWT、Cookie、完整手机号或验证码。

## 发版（生产）

常规路径：`docker compose up -d --build api worker scheduler migrate`（`migrate` 跑 alembic 到 head）。

**已知约束（2026-10-14 实测）**：生产机访问 `registry-1.docker.io` **不通**（`HEAD manifest` 超时），
`--build` 会在 `load metadata for docker.io/library/python:3.12-slim` 处失败。绕行方案（已验证）：

```bash
# 1) 有 registry 的构建机（测试机）：同步源码后构建
docker compose build api
# 2) 搬运镜像：docker save | gzip | ssh 生产 'gunzip | docker load'
#    中转链路：构建机 → 本机 → 生产（两跳，约 130MB，耗时约 9 分钟）
# 3) 生产：打标签（api/worker/migrate/scheduler 同一镜像、同一 Dockerfile）+ 重建，**不加 --build**
docker tag nike-mp-api:latest nike-mp-worker:latest
docker tag nike-mp-api:latest nike-mp-migrate:latest
docker tag nike-mp-api:latest nike-mp-scheduler:latest
docker compose up -d migrate && docker compose up -d api worker scheduler
docker compose exec -T api alembic current     # 期望：<最新 revision> (head)
```

坑：`docker compose exec -T ...` 会**吃掉脚本自身的 stdin**，用 SSH 管道传脚本时必须
`< /dev/null` 或改成"先 `cat > /tmp/x.sh` 再 `bash /tmp/x.sh`"；`docker compose cp` 进容器的
文件是临时的（容器一重建就没了）——真正的修复要进镜像。

## 后续迭代（本版未实现）

通知推送（钉钉/企微）、审计日志、租户级公平队列与老化提权、异步大数据导出。

