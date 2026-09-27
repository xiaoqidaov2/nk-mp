# Nike 门店监控系统 —— 项目上下文（供需求采集子代理共用）

> 本文件是本轮「前后端项目需求采集」的统一输入。所有子代理先读本文件，再按各自方向展开。
> 工作目录：`C:\Users\qq289\Documents\nike_mp`（Windows，Python 3.12，httpx/requests 可用）

## 1. 项目背景

面向**多客户（10-20 家）的商业监听服务**：监控 Nike 中国线下门店（换季优惠店/NVS 渠道为主）的
**折扣价与库存**，按「门店 × 货号 × 尺码」粒度提供数据与变化提醒。

客户痛点（来自真实需求方验收）：
1. 找到所有门店（当前 100 家）
2. 指定货号在任意门店的**尺码级库存**（"灰的都是没有的"）
3. 门店折扣价/促销价
4. 变化提醒（补货/售罄/变价/上新）；理想状态还要"店里有什么货"

硬约束（用户明确、不可违背）：
- **只用纯 API**：不得占用/登录用户的微信客户端、不得用 adb/模拟器/Frida/OCR/截图
- **零人工维护**：日常无人值守（用户原话"把我从经常维护摘出去"）
- 数据必须与门店实际一致（验收标准）

## 2. 现有能力（已实测，2026-09-24）

### 2.1 接口层（全部纯 HTTP/2 + JWT，无需 Cookie/WAF 票据）

| 能力 | 接口 | 说明 |
|---|---|---|
| 门店清单(100家) | `GET https://api.nike.com.cn/o2o/buy/regions/v1?anchor=0&count=100` | 返回 id(regionId)/storeCode/displayName/storeAddress/concept |
| **尺码级库存+价格(核心)** | `PUT/GET https://api.nike.com.cn/o2o/buy/cart_reviews/v2/{uuid}` | 只读审核，不动购物车；门店由请求头 `x-nike-region` 决定 |
| GTIN 结构(尺码→GTIN) | `GET https://api-test.nike.com.cn/store/product_details/v1/.../store_number/{店号}/style_color/{货号}` | 匿名；**结构按门店可能不同**（实测同款两套 GTIN），必须逐店取 |
| 门店活动/预览池 | `GET https://api.nike.com.cn/store/home/v1/storeNumber/{店号}` | 匿名；每店"满减专区"轮换 10 件货号+规则文案 |
| 在线目录/上新 | `GET https://api.nike.com.cn/discover/product_wall/v1/...?path=/w/new-3n82y...` | 匿名；当前新品 480 件 |
| 在线尺码可用性 | `GET /discover/product_details_availability/v1/.../groupKey/{slug}` | 匿名；在线渠道（非门店），含 isAvailable+ship 水位 |

### 2.2 核心接口语义（cart_reviews，实测）

- 请求体：`{"request":{"country":"CN","currency":"CNY","locale":"zh_CN","items":[{"id":uuid,"skuId":GTIN,"quantity":1,"valueAddedServices":[],"fulfillmentDetails":{"type":"SHIP","location":{"type":"address/shipping","postalAddress":{"country":"CN"}}}}],"promotionCodes":[]}}`
- 202 PENDING → 轮询 GET 同 URL（约 1-2.5s 一次）→ COMPLETED
- **全部可买** → `response.fulfillmentGroups[].items[].itemCosts.priceInfo {price 标价, discount 促销额, total 实付}` + `promotionDiscounts[].displayName`（促销名）
- **有缺货** → 顶层 `error.errors[]` 列出不可买项（`INVENTORY_INSUFFICIENT`/`PRODUCT_NOT_BUYABLE`），未列出=有货（排除法）
- 规则：**价格批次 ≤3 件**（该店购物车上限）；**可用性检查可 40+ 件/批**；零副作用（已验证购物车不变）
- 错误语义：`INVENTORY_INSUFFICIENT`=在售但该尺码无货；`PRODUCT_NOT_BUYABLE`=该店不卖/不可购
- 已知数据质量坑：空完成响应（无 error 无明细）必须视为不可信；WAF 间歇挑战(405)需重试；JWT 1h 过期需中途自愈；网络 TLS EOF 需重试

### 2.3 认证与风控（已打通）

- 小程序 OAuth：`client_id=790b76033778d3e6f18743582900a4ae`，scope 含 `offline_access`
- **refresh_token 纯 HTTP 续期**（`POST accounts.nike.com.cn/token/v1`，自动轮换）；失效才需浏览器静默授权一次（数周~月一次），零微信依赖
- WAF 分水岭：**HTTP/1.1 → 非 GET 全 405；HTTP/2 (httpx http2=True) → 直通**；挑战间歇出现，需重试循环
- 账号风控：高频突发会触发冷却/401，需要限速与配额管理

### 2.4 现有脚本资产（Python，均已在 `nike_mp/`）

| 文件 | 作用 |
|---|---|
| `nike_store_probe.py` | 单店精查(`--store 3752 --code XXXX-XXX`) + **全国扫描**(`--scan --code`，逐店取结构、批量可用性、≤3件价格、断点 `--skip`、401/405/网络自愈) |
| `nike_auth.py` | 凭证引擎（refresh_token 纯 HTTP + 浏览器静默授权兜底；输出 jwt_latest.txt/mp_refresh.txt） |
| `nike_zero_touch.py` | 每小时零人工引擎（auth → 上新 → 门店活动 → 探针），Windows 计划任务 `NikeZeroTouchMonitor` |
| `nike_new_arrivals.py` | 在线新品墙轮询 + diff + 可选到店探测 |
| `nike_store_monitor.py` | 门店活动（满减专区预览池）监控 |
| `all_stores.json` / `regions_h2.json` | 100 家门店清单 |
| `gtin_cache.json` | 尺码→GTIN 缓存（按店） |
| `probe_state_{store}.json` | 单店探针状态与变化（补货★/售罄✗/变价¥） |
| `scan_0924_0808.json` / `watch.txt` | 扫描结果示例 / 关注货号清单 |
| `unpacked/` | 反编译的小程序源码（接口路径/参数依据） |

### 2.5 实测样例（可用于验收口径）

- `DN3707-202`（AJ3，¥1399）：全国 100 店扫描 → 7 家店有货，尺码分布各异（南昌明矾 40-45 共 9 码；九江庐山/北京京良 41-44.5；广州西环 41-44；武汉江夏 45,46；宁波江南 46,47.5；海口观澜湖 42.5）
- `IF1823-010`（Jordan Flight 夹克）：全国 100 店 0 家有货
- 促销示例：某款 ¥699 促销-314.55 → 实付 ¥384.45（"指定鞋/服/配件 55折"）
- 单店全量商品清单（"这个店一共多少件货"）：**纯接口不可达**（微信云容器+AWS_IAM+CDN ACL 三重墙，已代码级证伪）→ 可用 store/home 预览池（每店 10 件轮换）+ 货号探针替代

## 3. 本轮目标（要产出的东西）

一套**前后端项目需求**，供后续开发（Web 前端 + 后端服务），让上述能力产品化、可交付给 10-20 个客户使用，
且保持零人工维护。需求需覆盖：功能需求（FR）、非功能需求（NFR）、数据模型、角色与用户故事、验收标准、约束与风险。

## 4. 子代理工作要求

- **只读分析**：不运行任何网络请求/不修改现有文件；只写你被指定的输出文件
- 输出统一为 Markdown，条目化、可勾选，带优先级（P0/P1/P2）
- 不要臆造不可得的数据能力（见 2.5 不可达项）；区分"现成可做/需开发/不可得替代方案"
