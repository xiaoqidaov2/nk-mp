# Nike 接口实测情报（外部逆向交付，2026-10-13 吸收）

来源：外部爬虫/逆向 agent 的实测报告（`nike-api-re/`）。这里只记**与我们平台有关**并且
**会改变我们做法**的结论，以及我们据此做的改动。

## 一、上限与取值（直接决定我们的请求数）

| 项 | 实测结论 | 我们的动作 |
| --- | --- | --- |
| 一次请求的 GTIN 上限 | **50**（51 → `400 FIELD_INVALID`）| `NIKE_MP_AVAILABILITY_CHUNK_SIZE` **40 → 50**（同一门店多个货号共用批次，直接减少请求数）|
| 价格是否要单独拆批 | **不必**：价格随可用性响应一起回（`itemCosts.priceInfo`）；我们原来"3 个/批"是我方策略 | `NIKE_MP_PRICE_CHUNK_SIZE` **3 → 10**（只用于补"还缺价"的在货尺码）|
| `x-nike-region` 多店 | **不支持**（逗号/数组/重复头 → `INVALID_VALUE`）| 维持一店一请求（我们的设计本来如此）|
| `x-nike-region` 取值 | 必须是 `regions` 返回的 **`id`（regionId，如 `NIKE0S033`）**，不是 storeCode（3402 → `INVALID_VALUE`）| 我们本来就是"同一 store 行里的 `region_id`" ✓ 无需改动 |
| `skuId` 形态 | 必须 **14 位含前导零**；去零/13 位 EAN/`style-尺码` → `PRODUCT_SKU_NOT_FOUND` | 我们用结构接口给的 GTIN ✓ 无需改动 |

## 二、批量库存接口：**系统性证伪**（不要再找）

- `/o2o/*` 有可靠 oracle（网关纯文本 `404 Not found` = 路由不存在）：扫 **~330 个候选路径**，
  只有 `regions/v1|v2|v3`、`cart_reviews/v2`、`skus/v1` 存在；
- `/store/**` 带 token 后除 `home/storeNumber/{n}` 与 `product_details` 外全是 `404 code310`
  —— `store_availability`/`inventory`/`stock`/`store_locator`/`nearby` **都不存在**
  （无 token 时的 401 是全局鉴权过滤器，**不能**当作"路由存在"的证据）；
- `/svs/*`、`/cic/*` = S3 静态桶 / 网关 403，不是 API；
- 公开面（GitHub/CSDN/wxapkg/grep.app）零痕迹。

→ 结论：**"一个货号 × 多店一次返回"的接口不存在**。要压吞吐只能换思路（见第三节）。

## 三、新发现：`/discover` 免鉴权端点族（**两级探测的基石**）

```bash
GET https://api.nike.com.cn/discover/product_details_availability/v1/marketplace/CN/language/zh-Hans/\
consumerChannelId/d9a5bc42-4b9c-4976-858a-f159cf99c647/groupKey/lqwXsC
# 200，不需要 Authorization，0% WAF；一次返回整款 ~20 个尺码的
#   {gtin, isAvailable, ship(OOS/LOW)} —— 但是**渠道级**，没有门店维度
```
已排查：路径里插 `storeNumber` 全 400；`?locationId=` 只吃线上频道 UUID（多值 500/400）
→ **不能**当门店库存用。

**但它可以把"每轮 147 店 × 31 款"降到"每轮 31 次请求"**（两级探测）：
一级：每款打一次 `/discover`（免鉴权、0 WAF）→ 没变化的款**不展开**；
二级：只有探测到变化的款才展开 147 店跑 `cart_reviews`（我们现有链路不变）。

## 四、顺带交叉核对（与我们的实现一致 ✓）

`GET /o2o/buy/regions/v1` → 167 条 → `displayName` 含「换季优惠」148 条 → 按 `storeCode`
去重 **147 家**（同一家店常有两个 id，如 `xian`/`NIKE0S08` 同 storeCode 3875 — 这解释了 167≠147）。
与我们"默认只看换季优惠店"的判据、以及生产库 147 家的数字**完全一致** ✓。

## 五、一个已解释的疑点（对方测试的配对问题）

对方用 `storeCode=3402` + `region=NIKE0S033` 测出 18 条全 `PRODUCT_NOT_BUYABLE`。
我们的实现里，`x-nike-region` 取的是**同一个 store 行**的 `region_id`；把 A 店的 storeCode
配 B 店的 regionId 会命中错误的门店上下文 → 全"该店不卖"是预期行为。
建议他们从 `regions` 的**同一行**里取 `id` 复跑确认（无需改我们的代码）。

## 六、认证：两条链并存（**不要改线上那条**）

- 对方打通的是 **web 端 OAuth**：client `e5517bcc26532d959b2532654ac037dd`、
  `redirect_uri=https://www.nike.com.cn/auth/login`、`/token/v1` 必须
  `x-www-form-urlencoded` + `Origin: https://www.nike.com.cn`、`challenge/code` 不能带 `language`、
  手机号必须 E.164。
- 我们的**小程序契约**（`790b76033778d3e6f18743582900a4ae` + `2026-09-29/mp-phone-otc-pkce`）
  在生产**已跑通**（用户可自助绑定、token 正常续期）→ **保持不动**；上面的 web 链只作为
  另一条可用路径记录在案。


## 七、第二轮逆向交付的吸收（2026-10-13 晚）

### 1) `skus/v1` 判死（**纠正我方早先的记录**）
对方用三组对照证明它**到不了应用层**：大小写都 403、GET/POST/OPTIONS 同一份 403、`skus/v2`/`skusx/v1`
等同位路径全 404、带不带 token 完全一致，且 403 体**没有 `custom_acl` 标记**（与 `/store/product_details/*`
那种 CDN ACL 403 不同）→ 是**边缘精确 deny 规则**。`api-test` 上该路径 404。
→ **不要再去追"按 skuId 批量查门店库存"**。

### 2) 公开面彻底死路
全站 JS（35 个 chunk）里**没有** `o2o`/`store_number`/`x-nike-region`/门店/自提字样
→ **门店库存只存在于微信小程序**；grep.app / GitHub / CSDN / 搜狗 / Wayback 全无痕迹。
另发现网关回显 `x-nike-zhenghe-target-host`，泄漏 4 台上游 origin（我们已实测：**均不可直连**）。

### 3) `product_feed` 实测（免 token）
```bash
GET https://api.nike.com/product_feed/threads/v2?filter=marketplace(CN)&filter=language(zh-Hans)\
    &filter=channelId(d9a5bc42-4b9c-4976-858a-f159cf99c647)&anchor=0&count=100
# 200：CN 线上渠道共 118,000 个商品；每条 productInfo[0] 里带
#   merchProduct.styleColor、availability.available、availableSkus[]、merchPrice
```
**但**：`filter=styleColor/styleCode/productCode/searchTerms/query(...)` **全部 400**
→ **不能按货号查询**，要覆盖全量得翻 1,180 页。而且它是**线上渠道**数据。

### 4) 结论：**"一级门"（用线上信号决定要不要扫门店）不成立**（有硬证据）
`IB7111-005`：线上 7 个尺码 `True/MEDIUM`，我们 147 店里**在售 0**（5 缺货 / 155 不卖）
`IO7727-001`：多个尺码线上 `True/LOW`，门店在售 0。
→ 线上渠道是**独立库存池**（可能由仓或其他店供货），**门店级补货不会同步反映**。
**只有"加扫"方向的用途是安全的**：线上信号变化 → 把该款**提前插队**去扫门店（只增不减，不会漏）。

### 5) 那么吞吐只能靠这三样（已落地两项）
1. **批上限 50 / 补价批 10**（已上线 ✓）；
2. **合并采集**：同一 owner 的门店，把多个款合并成 `ceil(总 GTIN/50)` 个请求 —— 31 款 × 147 店从
   **4,557 次/轮 → 1,764 次/轮（2.6×）**；
3. **自适应最大间隔（新方案）**：任务设置从"固定间隔"改为"**最大间隔**"，实际节奏由算法在
   `[最小间隔, 最大间隔]` 内决定（预算感知 + 历史热度加权 + 线上信号只做"提前"），保证每款
   **至少每最大间隔被扫一次**（不漏），热款更快。


## 八、自查与对齐（第三轮，2026-10-13 夜）

### 1) `product_feed` 不能按货号过滤（已实测）
`filter=styleColor|styleCode|productCode|searchTerms|query(...)` **全部 400**；
CN 线上渠道共 **118,000** 商品（`count=100` → 1,180 页）。真实结构：
`objects[].productInfo[0].{merchProduct.styleColor, availability.available, availableSkus[], merchPrice}`。

### 2) 泄漏的 origin 主机：可直连，但**不是**旁路
| 主机 | 实测 |
| --- | --- |
| `api.test.o2o.origin.nike.com.cn` | 404（o2o 路由在该主机不可用）|
| `api.prod.o2o.origin.nike.com.cn` | 解析到 192.169.122.148；不带 Host → 404；**带 `Host: api.nike.com.cn` → 又回到阿里云 405 阻断页** |
| `nvsstore.test.commerce.origins.origin…` | 400（LB 无监听）|
| `svc.test.loyalty.origin…` | 200（无关页面）|

→ **origin 旁路不成立**（LB 按 Host 路由回同一套 edge）。`store/home` 响应里带
`x-amzn-requestid`/`x-amz-apigw-id`，说明门店服务在 AWS API Gateway 后面（但与我们的取数路径无关）。

### 3) 给逆向方的对齐样本（解开他们的 PRODUCT_NOT_BUYABLE）
我们生产**确实拿得到真值**（近 24h：`IN_STOCK` 1,955 / `OUT_OF_STOCK` 222,401 / `NOT_CARRIED` 248 万），
可直接复跑的两条：

```
store_code=3930 region_id=NIKE0S094 style=IM9441-600 尺码42/42.5/43/44/44.5 → OUT_OF_STOCK @2026-10-13 04:32
store_code=3630 region_id=NIKE0S070 style=DM2362-002 → OUT_OF_STOCK @2026-10-13 04:30
```
**头号假设**：他们用的是 **web 渠道 token**（client `e5517bcc…`）；`o2o/*` 是**小程序渠道**的接口，
渠道不匹配时业务层很可能统一回 `PRODUCT_NOT_BUYABLE`。**请用小程序渠道的 token 复跑**同一组合。

### 4) 设计：自适应采集（见 `docs/adaptive-cadence.md`）
合并采集（4,557 → 1,764 请求/轮）+ 最大间隔语义 + 预算内优先级调度；线上信号只做"提前"。


## 九、第三轮交付的吸收（2026-10-14）

### 1) 落地：跨款合批 + 免轮询
- **跨款合批**（50 GTIN 可混多个款、逐条给明细、不会全局吞结果）→ 见 `docs/adaptive-cadence.md` 第 1 步，
  已在 `scheduler`/`jobs`/`push` 实现（一个作业覆盖多任务，推送仍按任务归属）。
- **免轮询**：实测 PUT 后**立即 GET** 即 `COMPLETED`（0ms）→ `NIKE_MP_REVIEW_POLL_FIRST_SECONDS`
  默认 **0.5 → 0**（未就绪时仍按 `REVIEW_POLL_INTERVAL_SECONDS` 退避重试）。

### 2) **不能照抄**的两条
- **"价格零请求"**：`product_feed` / `/discover` 给的是**线上渠道价**；我们客户看的是**门店价**
  （`cart_reviews` 的 `itemCosts.priceInfo`）→ 两者不可替换，价格请求还得发（只补缺的）。
- **`api-test` 不能当路由存在性判据**：未知路径回 `401 code35`（全局过滤器）；真 oracle 是
  **prod + token → 404 code310**。

### 3) 两条硬约束（已核对我们的实现）
1. **`uuid` 一次性且与 body 哈希绑定**：同 uuid 换 body → `409 Request hash mismatch`，
   **但 GET 仍会返回上一轮旧快照** → 静默误报来源。我们的 `review()` **每次调用都生成新 uuid**
   ✓（`request_id = str(uuid.uuid4())`），且 GET 异常一律记为**未知** ✓ 不产生售罄结论。
2. **WAF 拦截是瞬时随机的**（同路径 30 发：63% 拦截；去掉 UA 65%）→ 结论：**接受随机拦截 +
   立即 0.2s 重试**（期望成本 ≈2.5×），这与我们已实现的策略一致。

### 4) WAF 路线③的负面结论（不再重复试）
直连上游 origin（43.109.70.x）**拦截率更高（70%）**；405 页**没有可解 JS**；
`nvs-store-ootd-api` 系列 0% 拦截但要求 **key=value 签名**（非 Bearer）→ 走不通。

## 十、第三方号池 token 的接入红线（2026-10-14 实测）

背景：客户租了一个"号池"（多个租户共用同一批 Nike 小程序账号），担心接入会**顶掉**别人的号。

**已实测的两条性质**（用池里 1 个 QUERY token 做过一次只读查询，未登录/未续期）：

1. **只读使用不会顶号** ✓：顶号/失效来自**登录或 refresh_token 轮换**（Nike 的 refresh 是一次性的），
   不来自只读 API 调用。我们持有的副本会在对方登录后**自然失效**（我们这边 401），
   但**不会反过来影响别人** ✓ —— 回查池台账 `status`/`updated_at` 均未被我们的调用改动 ✓。
2. **池的 token 寿命极短**：access_token 只有小时级；台账里全部 `INACTIVE` 的池
   （实测：102 个号、最近更新在 98 天前）拿到的 token 打过去只会得到
   `AUTH_REQUIRED: access token rejected` ✗ —— **死的池没有任何可用数据**。

**红线三条（违反任意一条就会顶号 / 破坏隔离）**：

- **绝不**把池里的 `refresh_token` 导入我们的凭据库 ✗ —— 我们的 `try_refresh` 会轮换它，
  直接顶掉其他租户 ✗✗；只用 `access_token`（无 refresh 的凭据我们本来就支持：
  过期即 `NEEDS_AUTHORIZATION`，`refresh_expiring` 会跳过它 ✓）。
- **绝不**用池里的号做登录/短信验证 ✗（那就是顶号的根因）。
- **绝不**写回池系统 ✗（只 `GET` 台账；`POST`/`PATCH`/`DELETE` 是对方的管理动作）。

**接入形态（若将来接活号池）**：不要塞进现有"每用户凭据"体系 ✗（那套是 owner-scoped +
AES-GCM + 自动续期，共享池会破坏租户隔离不变式 ✗）。应做成**独立只读采集侧**：
定时拉 `QUERY + ACTIVE` 的 access_token → 只读探测 → 首次 401/403 立即丢弃该 token →
严格限速（共用号的风控预算是别人的资产，别烧掉 ✓）→ **只取 access_token，`openid`/
`unionid`/`phone`/`id_card` 一律不落库**（PII ✓；平台对手机号本来就只存 HMAC 指纹）。

