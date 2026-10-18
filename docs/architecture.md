# 架构说明

## 组件

| 组件 | 职责 |
|---|---|
| `web` | React 前端，仅访问同源 `/api/v1`，不直接调用 Nike 接口、不保存任何 token。生产用 `vite build` 产物 + `vite preview` 静态服务（**不跑开发服务器**：开发服务器按需转换、依赖预构建会中途作废响应——实测 caddy 报 `aborting with incomplete response`，iPhone/微信真机拿不到 JS 分片而白屏；一个页面从上百个模块请求降到 5 个静态资源） |
| `api` | FastAPI 服务：会话鉴权、任务与扫描编排、矩阵与流水查询、SSE 进度 |
| `worker` | 领取排队中的扫描任务并执行采集，带租约与心跳；崩溃后由其它 worker 接管。可多副本（默认 1）。执行期间由作业心跳续写节点心跳（作业是分钟级，不能只在空闲间隙上报，否则监控会把在跑活的节点判成掉线） |
| `scheduler` | 按 cadence 把启用的监控任务转换为扫描任务；可多副本，事务级 advisory lock（`dispatch_lock_key`）保证同一拍只有一个实例派发（抢不到就跳过 = 主备自动接替，无需 leader 状态） |
| `monitor` | 独立调度监控进程：每拍写自己的心跳、核对 scheduler/worker 心跳与迟到任务，异常只告警（`/ops/scheduler-health` 提供同样的实时报告）。报告只列**最近 15 分钟出现过**的实例（掉线实例在窗口内可见，之后自动退场），并按保留期（24h）回收心跳行 |
| `db` | PostgreSQL 17，同时承载业务数据与任务队列（`FOR UPDATE SKIP LOCKED`） |

前后端通过同源 `/api/v1` 通信，容器内由 Vite 开发服务器反代到 `api:8000`。

## 采集链路

```
scheduler → 按用户选号（未冷却账号里最近最少使用）→ 冻结 owner/credential/generation
          → enqueue ScanJob + ScanJobStore（同账号、同节奏、同门店/尺码集合才合并）
worker    → lease job（租约 120s，心跳 = max(5s, 租约/3)）
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
9. **一用户多账号**：一个平台用户可以绑定多个 Nike 账号（`credential` 多行，`owner_user_id` 是
   普通索引）；派发时按「未冷却、最近最少使用」选一个账号并推进其 `last_used_at`（轮换；
   `last_attempt_at` 仍是续期退避的时间戳，派发不碰），同一拍同一用户的任务共用该账号；
   一个作业只冻结一个账号，**不同账号绝不合并进同一作业**。
   同一 Nike 手机号仍然只能绑一个平台用户（`uq_credential_phone_active`）。
10. **账号级冷却与不可用**：冷却、风控阶梯、额度暂停都落在单个 `credential` 行上；只有该用户
   **所有**账号都不可用（冷却/断开）时才暂停其任务，并等到最早一个冷却窗口结束；任一账号可用时
   任务照常派发（含作业收尾时不顺延到命中账号的窗口），页面不显示冷却。
11. **多节点**：调度派发由事务级 advisory lock 互斥（抢不到即跳过本拍），bootstrap 计划与扫描
   作业都用 `leased_by` + 租约归属校验（只有持有者能续租/写状态），实例存活以 `service_heartbeat`
   为准。

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
# 3) 生产：打标签（api/worker/migrate/scheduler/monitor 同一镜像、同一 Dockerfile）+ 重建，**不加 --build**
docker tag nike-mp-api:latest nike-mp-worker:latest
docker tag nike-mp-api:latest nike-mp-migrate:latest
docker tag nike-mp-api:latest nike-mp-scheduler:latest
docker tag nike-mp-api:latest nike-mp-monitor:latest
docker compose up -d migrate && docker compose up -d api worker scheduler monitor
docker compose exec -T api alembic current     # 期望：<最新 revision> (head)
```

多副本（可选）：`docker compose up -d --scale worker=2 --scale scheduler=2`；`monitor` 保持单实例。
没有 monitor 实例时 `/ops/scheduler-health` 仍可用（报告是实时查询，心跳由 scheduler/worker 自己写）。

### 磁盘与日志（2026-10-15 生产事故的防复发）

事故：多次发版累积的旧镜像 + 构建缓存 + **无上限的容器日志**把 40G 盘写满 →
Postgres 写不了 `postmaster.pid` → `db` 崩溃循环 → 全站登录 500（约 10 分钟）。

现在有三道闸：

1. **容器日志封顶**：`compose.yaml` 的 `x-logging` 锚点（`json-file`，`max-size=20m`、
   `max-file=3`）挂到每个服务 → 单个服务日志最多 60MB；重建容器后生效。
2. **源头降量**：worker/scheduler 把 `httpx` 调到 WARNING（原来每请求一行 INFO，实测单进程
   300MB+）；上游响应改成 `jobs._log_upstream_stats` **每轮一行**汇总
   （`job <id> upstream: 200=..,202=..,405=.. attempts=.. challenged_405=.. (..%)`），
   405 挑战占比这个关键指标保留。
3. **每日回收 + 水位告警**：`/etc/cron.daily/nk-mp-docker-gc` 清未使用镜像/构建缓存并把
   before/after 写进 `/var/log/nk-mp-docker-gc.log`，磁盘 ≥85% 时写 syslog；
   `monitor` 报告里带根分区水位（`disk`），≥85% 会给管理员入队一条系统通知
   （同类 6 小时最多一条），前端 `/ops`「调度与节点」卡直接显示。

发版前/后各看一眼 `df -h /`；不足 5G 先回收（`docker image prune -af`）。

坑：`docker compose exec -T ...` 会**吃掉脚本自身的 stdin**，用 SSH 管道传脚本时必须
`< /dev/null` 或改成"先 `cat > /tmp/x.sh` 再 `bash /tmp/x.sh`"；`docker compose cp` 进容器的
文件是临时的（容器一重建就没了）——真正的修复要进镜像。

## 后续迭代（本版未实现）

通知推送（钉钉/企微）、审计日志、租户级公平队列与老化提权、异步大数据导出。

