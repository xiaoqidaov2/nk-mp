# QA 隔离联调环境（阶段 B）

独立 Compose 项目 + 独立数据库卷 + 离线上游 stub。**绝不访问真实 Nike 接口。**

```bash
# 1. 启动（首次需要构建）
docker compose -p nike-mp-qa -f compose.yaml -f qa/compose.qa.yaml up -d --build

# 2. 写入种子数据（平台运维 + 客户租户 + 假凭证）
docker compose -p nike-mp-qa -f compose.yaml -f qa/compose.qa.yaml \
  exec -T api python - < qa/seed_qa.py

# 3. 运行冒烟：登录 → 创建任务 → 调度 → 采集 → 检查点 → 矩阵 → 变化流水 → 重试
apps/api/.venv/bin/python qa/smoke.py

# 4. 查看日志 / 停止
docker compose -p nike-mp-qa logs -f worker
docker compose -p nike-mp-qa down            # 保留数据卷
docker compose -p nike-mp-qa down -v         # 连数据卷一起删除
```

## 环境受限时的 host 网络变体

部分沙箱/CI 环境无法在容器之间建立桥接网络连接（表现为 `db:5432` TCP 超时，
`migrate` 服务以 `psycopg.errors.ConnectionTimeout` 退出）。此时使用自包含的
host 网络变体，隔离性（独立项目名、独立数据卷、独立端口）保持不变：

```bash
docker compose -p nike-mp-qa -f qa/compose.qa.hostnet.yaml up -d --build
docker compose -p nike-mp-qa -f qa/compose.qa.hostnet.yaml \
  exec -T api python - < qa/seed_qa.py
apps/api/.venv/bin/python qa/smoke.py
```

## 说明

- `stub_upstream.py` 模拟 regions / product_details / cart_reviews / token 接口，
  支持 `baseline`、`restock`、`untrusted` 三种场景（`POST /__scenario`）。
- 冒烟脚本断言：首轮只建基线；不可信轮不产生事件且门店标记 `UNKNOWN`；
  “仅重试失败门店”只重跑未完成门店并产生 RESTOCK；配额计数随真实请求增长。
- 该环境用于阶段 B（离线联调）与阶段 C（故障注入）的基础设施；
  真实接口冒烟（阶段 D）必须使用独立授权账号并单独评审。
