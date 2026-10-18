# Nike 中国接口 + 我们的系统：完整简报（给爬虫/逆向 agent）

> 读这一份就够。**第 0 节是我们的目的**，第 2 节是"我们已在用的全部 Nike 接口"，
> 第 6 节是"怎么访问能 200"的最小可复现配方。

---

## 0. 我们的目的（要 agent 帮什么）

我们做的是一个**门店库存 / 价格监控平台**：盯着 Nike 中国的 **147 家"换季优惠"门店** ×
一批货号，一旦出现 **补货 / 首次上架 / 售罄 / 变价** 就把汇总消息推到客户的钉钉/企业微信。

**现在的问题**：上游接口是"**一家店一次请求**"的模型。客户有 **31 个任务 × 147 店 × 10 分钟一轮**，
折合 **~2.7 万次门店探测/小时**；我们实测吞吐只有 **5–6 千/小时**（再加机器/额度上限），
队列排长队、实时性上不去。

**请帮我们找**（按价值排序）：

1. **批量库存接口**：给"一个货号 + 一批门店（或全部门店）"，**一次返回各店各尺码的库存**。
   依据：小程序里"查看其他门店 / 附近门店有货"这类页面必然调这种接口。
   可能命名方向：`store_availability`、`inventory`、`stock`、`store_locator`、
   `product_details`（不带 `store_number`）、`cic/browse`、`svs`，或 Web 侧（`www.nike.com.cn`）的可用性接口。
2. **一次多店**：`cart_reviews` 的 `x-nike-region` 头能否接受**数组/多值**（一个请求查多店）。
3. **更大的批量**：`cart_reviews` 的 GTIN 上限是否 > 40？价格项上限是否 > 3？
4. **免鉴权只读路径**：`/o2o/*` 下有没有不校验 Bearer 的库存读接口。
5. **最理想**：把小程序"门店库存/更多门店"页面的**真实请求抓一份**（URL+方法+头+body+响应）。

**边界**：只读监控、用客户自己的 Nike 账号、**不碰客户端控制**（Frida/adb/模拟器/OCR）、
不写购物车、不做"绕过风控"的攻击性动作。我们要的是**小程序自己在用的正常接口**。

---

## 1. 我们的系统：怎么实现、实现了什么

**链路（一个用户）**：
```
绑定 Nike 账号（短信 OAuth，拿 access/refresh token）
  → 同步门店目录（拿 regionId / storeCode / 店名）
  → 对"门店 × 货号"取尺码表（拿到每个尺码的 GTIN，7 天缓存）
  → 每轮对每店发一次可用性请求（≤40 个 GTIN/请求）
  → 只对"在货"的尺码再发价格请求（3 个/批）
  → 落库（历史 + 最新状态）→ 与上一轮比较
  → 生成事件：补货 / 首次上架 / 售罄 / 变价
  → 聚合成一条消息推送到钉钉 / 企业微信 / 自定义 Webhook
```

**已实现的产品能力**（这些决定了我们对接口的要求）：

- **多租户 + 用户隔离**：每个用户用自己的 Nike 连接；门店/货号目录是按用户隔离的缓存。
- **事件判定**（防误报）：只有**可信**数据参与比较；不可信/被拦截/空响应一律记为未知，
  绝不当成"售罄/补货"；**GTIN 变化 = 重建基线**（不当成换货）；价格缺失清空旧价但不造假变价。
- **额度与风控**：平台级小时/日闸、账号级冷却（被限速或额度耗尽→暂停并到点自动继续）、
  失败门店自动补跑、405 挑战不计费、观测历史每 key 只保留最近 30 条。
- **实时性**：任务可选 30 秒–24 小时间隔；30–120 秒档只对少量门店真实成立（受额度/吞吐限制）。

---

## 2. 我们已在用的全部 Nike 接口（精确形状）

### A. 身份域（`accounts.nike.com.cn`，短信登录 + OAuth，契约 `2026-09-29/mp-phone-otc-pkce`）

| 方法/路径 | 作用 | 备注 |
| --- | --- | --- |
| `GET /authorize/v1?client_id=790b76033778d3e6f18743582900a4ae&response_type=code` | 授权页 | **返回 `acw_sc__v2` JS 挑战页**（`var arg1='...'`，可解，见第 5 节）|
| `POST /credential_lookup/v1` | 手机号是否已注册 | |
| `POST /verification_code/send/v1` | 发短信验证码 | 频控严格 |
| `POST /challenge/code/v1` | 校验验证码 | |
| `POST /check/v1` | 换取 `redirect_uri`（带 `code`）| |
| `POST /token/v1` | `authorization_code`（PKCE S256）/ `refresh_token` | 拿 access/refresh token |

拿到的 access token 就是下面 `api.nike.com.cn/o2o/*` 的 `Authorization: Bearer`。

### B. 门店目录（`api.nike.com.cn`）
```bash
GET /o2o/buy/regions/v1?anchor=0&count=200
# 返回全量门店：storeCode / regionId / 名称 / 地址
# ⚠️ 实测这条路径 ~100% 被 WAF 405 拦（见第 4 节），我们只在缓存过期时试，成功就缓存 24h
```

### C. 尺码表 / GTIN（`api-test.nike.com.cn`，**免鉴权，0% 被拦**）

> **2026-10-13 实测：批量端点已被上游收回**。曾经可用的
> `GET /store/product_details/internal/v1/.../store_number/{store}?productCodes=…`
> 现在无 token → **401**、带 token → **403**（公开的 `v1` 路径带 `productCodes`
> 返回 200 但空体，`style_color/{a,b}` 直接 400）。客户端现在**先试批量，非 200 即
> 逐款回落到下面的单款端点**；`gtin_structure` 的**负结果**只缓存
> `structure_missing_ttl_seconds`（默认 30 分钟），避免再出现"新货号全店缺尺码 7 天"。

```bash
GET /store/product_details/v1/marketplace/CN/language/zh-Hans/store_number/{store}/style_color/{style}
# 200 → productGroupings[0].products[0].sizes[]：
#   {"localizedLabel":"42","gtins":[{"gtin":"00198482740573"}]}
# 只取 sizes[].localizedLabel 与 sizes[].gtins[].gtin（GTIN 是**门店级**的，必须按店取）
# ⚠️ 这个域**没有真实库存**：商品可用性/价格字段是惰性的（恒 false / 恒 0），只能当尺码表用
```

### D. 门店信息（`api.nike.com.cn`，**0% 被拦、不需要 token**，但**不含库存**）
```bash
GET /store/home/v1/storeNumber/{store}
# 需要三个头：appid / nike-api-caller-id / x-nike-partner（见第 3 节）
# 200 → {"nodes":[{"type":"STORE_SWITCH"},{"type":"STORE_PROMOTION", ...}]}
```

### E. 可用性 + 价格（**核心，一店一请求，需要 Bearer token**）
两步：先 `PUT` 建请求（202），再 `GET` 轮询结果（200）。

```bash
PUT /o2o/buy/cart_reviews/v2/{客户端自己生成的 uuid}
{
  "request": {
    "country": "CN", "currency": "CNY", "locale": "zh_CN",
    "items": [
      {"id": "<uuid>", "skuId": "<GTIN>", "quantity": 1, "valueAddedServices": [],
       "fulfillmentDetails": {"type": "SHIP",
         "location": {"type": "address/shipping", "postalAddress": {"country": "CN"}}}}
    ],
    "promotionCodes": []
  }
}
# 门店由 **请求头 x-nike-region: <regionId>** 决定（body 里没有 store 字段）
# 我们一次放 ≤40 个 GTIN；价格：同一形状再发一次（我们 3 个/批），响应里 itemCosts.priceInfo 即价格

GET /o2o/buy/cart_reviews/v2/{同一个 uuid}
# 响应语义：
#   response.fulfillmentGroups[].items[] 出现该 skuId   → 有货
#   error.errors[].code = INVENTORY_INSUFFICIENT        → 门店在售但缺货
#   error.errors[].code = PRODUCT_NOT_BUYABLE           → 该店不卖这个款
#   两者都没有                                          → 未知（我们不计入结论）
```

### F. 我们试过但**不用**的
- `GET /o2o/buy/carts/v1`（购物车，~67% 被拦，且写操作我们不做）。
- `mp.nike.com.cn`（另一套 Spring Boot 服务，应用层全局鉴权，需要它自己签发的会话 token，拿不到）。
- `m.nike.com.cn`、`svs/unite/api2/api-uat/h5 .nike.com.cn`：无 DNS 或不路由 `/o2o/*`。

---

## 3. 请求头与鉴权（照抄就能通）

```http
appid: com.nike.nvs.comerce.wo.wechat.prod
nike-api-caller-id: nike:wechat:web:1.0
x-nike-partner: NDS
x-nike-region: <门店 regionId>            # 可用性请求用它指定门店
authorization: Bearer <小程序用户 access_token>
accept: application/json
content-type: application/json; charset=UTF-8
referer: https://servicewechat.com/wx74b301953dcdd7db/18/page-frame.html
user-agent: <微信小程序 UA>（Mozilla/5.0 ... MicroMessenger/8.0.x ... MiniProgramEnv/...）
```
- `store/home` 与测试域 `product_details` **不需要** `authorization`；
- `o2o/buy/cart_reviews` **必须**带 `authorization`；token 用第 2A 节的 OAuth 流程获取/续期。

---

## 4. WAF 实测事实（别再重复试这些）

`/o2o/*` 约 **40–60% 的请求**返回 **405 阻断页**（`<meta name="data-spm" content="a3c0e">`，
素材来自 `errors.aliyun.com`；`Server: Tengine`）——**页面里没有可解 JS**（没有 `arg1`/`acw_sc__v2`）。

我们已**实测证伪**（拦截率仍在 37–67%，谁都没用）：
出口 IP（12 个全新出口同分布，χ²≈6.4、p≈0.85；**大陆机房出口反而 100% 被拦**）、
TLS/JA3 指纹（`curl_cffi` ≈ `httpx`）、HTTP/1.1 vs HTTP/2、UA（Chrome vs 小程序）、
`x-nike-ux-id`、`acw_sc__v2` 票据 / `acw_tc`、`cdn_sec_tc` 预热 cookie、
请求间隔（1/s vs 10/s 仅 ±15pp）、keep-alive、路径/方法变形（大写、`%2F`、双斜杠、POST/PATCH）、
假 token（拦截发生在鉴权层之前）。

**路径差异才是关键**：`regions` ~100% 被拦；`cart_reviews` ~50–64%；`carts` ~67%；
**`store/home` 与测试域 `product_details` 0%**。

**唯一有效的技巧**：被拦就**立刻重试**（0.2s）——通过率是独立硬币，立即重试墙钟 0.43s vs 退避 1.00s。

---

## 5. `acw_sc__v2` 挑战解法（身份域用）

`accounts.nike.com.cn` 的挑战页里有 `var arg1='<40 位十六进制>'`：

```python
ACW_KEY = "3000176000856006061501533003690027800375"
TABLE = [0xF,0x23,0x1D,0x18,0x21,0x10,0x1,0x26,0xA,0x9,0x13,0x1F,0x28,0x1B,0x16,0x17,0x19,0xD,
         0x6,0xB,0x27,0x12,0x14,0x8,0xE,0x15,0x20,0x1A,0x2,0x1E,0x7,0x4,0x11,0x5,0x3,0x1C,0x22,0x25,0xC,0x24]
# 1) unsbox：对 arg1 的每一位，找到 TABLE 中等于 (位置+1) 的下标 j，结果[j] = arg1[位置]
# 2) hex 异或：unsbox(arg1) 与 ACW_KEY 逐字节 XOR，得到 40 位十六进制
# 3) 带 Cookie: acw_sc__v2=<结果> 重放 → 挑战消失（实测 200 → 302 放行）
```

---

## 6. 最小可复现"能 200"的配方（都已现场复验）

```bash
# ① 尺码表（免鉴权，0 拦截）
curl "https://api-test.nike.com.cn/store/product_details/v1/marketplace/CN/language/zh-Hans/store_number/3402/style_color/DN3707-202"
# → 200，8.7KB，18 个尺码 + GTIN

# ② 门店信息（三个头，免 token）
curl -H "appid: com.nike.nvs.comerce.wo.wechat.prod" \
     -H "nike-api-caller-id: nike:wechat:web:1.0" -H "x-nike-partner: NDS" \
     "https://api.nike.com.cn/store/home/v1/storeNumber/3402"
# → 200，4.9KB，真实 JSON

# ③ 库存（需要 Bearer token；PUT 202 → GET 200，可能抽到 405 就立刻重试）
#    形状见第 2E 节
```

---

## 7. 我们的成本模型（说明为什么必须找批量）

| 项 | 实测 |
| --- | --- |
| 每店每货号 | 1 × PUT（≤40 GTIN）+ 1–2 × GET 轮询；在货尺码再按 3/批发价格请求 |
| 单轮 147 店 × 1 货号 | **22–27 秒**（门店并发 16）|
| 客户现况 | 31 任务 × 147 店 × 10 分钟 ⇒ **~2.7 万门店探测/小时** |
| 平台实测吞吐 | **~5–6 千/小时** ⇒ 队列长期积压（这就是我们要批量的原因）|
| 额度口径 | 只有**到达业务层**的应答计费（405 挑战不计数），平台小时闸 6 万 |

**只要能找到"一货号 × 多门店"的批量接口，上面的数字会直接降一到两个数量级**——这是我们唯一的出路。
