# Nike 门店库存与价格监控平台

把仓库根目录已有的 Nike 门店监控脚本（纯 API、只读）产品化为可交付给多客户的 Web 平台：
监控指定货号在指定门店的**尺码级有货状态**与**门店实付价/促销**，并在补货、售罄、变价时记录变化事件。

平台不占用任何人的微信客户端，不使用 adb / 模拟器 / Frida / OCR / 截图，采集全部走 HTTP/2 只读接口。

## 仓库结构

```
apps/api    FastAPI 服务、采集 worker、调度器、数据库模型与迁移
apps/web    React + Ant Design 前端（Vite）
requirements 需求与健壮性设计文档（产品输入）
wmpf        微信小程序调试工具源码（研究用途，不属于生产链路）
*.py        原有 CLI 脚本（历史资产，见 AGENTS.md 的边界说明）
```

## 快速开始（WSL + Docker Compose）

```bash
cp .env.example .env
# 生成加密密钥并写入 .env 的 NIKE_MP_CREDENTIAL_KEY
python3 -c "import os,base64;print(base64.urlsafe_b64encode(os.urandom(32)).decode())"

docker compose up -d --build

# 建表 + 创建服务方管理员（首次）
docker compose exec api python -m app.cli bootstrap \
  --email admin@example.com --password 'change-me-please'

# 导入采集凭证（复用现有脚本产出的 token 文件）
docker compose exec api python -m app.cli import-credential \
  --access-token-file /srv/../jwt_latest.txt

# 同步全国门店清单
docker compose exec api python -m app.cli sync-stores
docker compose exec api python -m app.cli add-style DN3707-202
```

前端：<http://localhost:5173> ・ API 文档：<http://localhost:8000/api/docs>

## 数据口径

状态六态：`有货 / 无货 / 不售 / 未探测 / 缺少尺码结构 / 有货待补价`。

- 接口返回空结果、轮询超时、被风控拦截、批次不完整时，结果记为**未探测**，绝不记为「无货」。
- 价格只有在该批次「全部可买且不超过 3 件」时才会返回，缺价显示「待补价」，不显示 ¥0。
- 未探测、不可信、缺少尺码结构的结果不进入变化状态机，也不会产生补货或售罄结论。
- 单店全量商品清单无法通过接口获取，页面只展示已监控货号与满减专区预览池。

## 本地开发

后端：

```bash
cd apps/api
python -m venv .venv && . .venv/bin/activate
pip install -e ".[dev]"
pytest
ruff check .
```

前端：

```bash
cd apps/web
npm install
npm run dev
npm run typecheck
npm test
```

## 验证状态

本轮完成了代码审阅、P1/P2 修复与三层测试（pytest 单元 + PostgreSQL 集成 + Vitest），并新增隔离的 QA 联调环境（`qa/`，离线上游 stub）。

- 后端：`ruff check app tests` 通过；`pytest`（单元 + PostgreSQL 集成）**91 项通过**；核心判定逻辑覆盖率 71%（配额 99%、观测 91%、凭证 85%）。
- 前端：`npm run typecheck` 通过；`npm test` 15 项通过。
- 迁移：空库升级、重复执行、`alembic check` 漂移检查、降级重建全部通过。
- QA 环境：`docker compose -p nike-mp-qa -f compose.yaml -f qa/compose.qa.yaml up -d --build` 后运行 `qa/smoke.py`（详见 `qa/README.md` 与 `docs/mvp-integration-plan.md`）。

**尚未执行、不得视为通过**：真实 Nike 接口冒烟、真实账号限流承载、24 小时持续观察。首次使用真实凭证前，请确认旧的 Windows 计划任务 `NikeZeroTouchMonitor` 已停止，避免两个系统并发轮换同一个 refresh token。

> 服务器上运行测试前必须显式设置 `NIKE_MP_TEST_DATABASE_URL` 指向一次性测试库；未设置时集成测试会自动跳过，绝不会向运行库写入测试数据。
