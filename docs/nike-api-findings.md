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
