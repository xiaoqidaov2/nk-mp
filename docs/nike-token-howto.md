# 怎么自己拿一把「小程序渠道」的 access_token（不用向我们要）

> 结论先说：**不要把 token 传来传去**（那是账号凭据，等于交出账号）。按下面 6 步自己跑，
> 用**账号所有者手机收到的验证码**换 token，一人一份、随时可弃。
> 关键：**client_id + redirect_uri + scope 必须成套**——换错一套会拿到"另一个渠道"的 token，
> 打 `o2o/*`（门店库存）只会得到 `PRODUCT_NOT_BUYABLE`（这正是上一轮"全 NOT_BUYABLE"的原因）。

## 0. 我们生产在用的那一套（小程序渠道，契约版本 `2026-09-29/mp-phone-otc-pkce`）

| 参数 | 值 |
| --- | --- |
| 身份域 | `https://accounts.nike.com.cn` |
| `client_id` | `790b76033778d3e6f18743582900a4ae` |
| `redirect_uri` | `https://mp-static-assets.gc.nike.com/auth/wechat/index.html` |
| `scope` | `openid offline_access nike.digital profile phone flow` |
| PKCE | `S256`（`code_verifier` = base64url(32 随机字节)，`code_challenge` = base64url(sha256(verifier))）|
| 端口/头 | 全程带 cookie（同一条会话）；`User-Agent` 必须是浏览器/小程序 UA |

> 对照：网页端那套（`client_id=e5517bcc26532d959b2532654ac037dd` + `redirect_uri=https://www.nike.com.cn/auth/login`
> + 另一组 scope）**是另一个渠道**，用它拿到的 token 打门店接口会得到业务层拒绝。

## 1. 六步（每一步的精确请求体，照抄即可）

```http
# ① 开会话 + PKCE（200；若返回带 arg1 的挑战页，先解 acw_sc__v2 再重放）
GET /authorize/v1?client_id=<上面的 client_id>
   &redirect_uri=<上面的 redirect_uri>&response_type=code&scope=<上面的 scope>
   &state=<随机 16 位>&code_challenge=<S256>&code_challenge_method=S256

# ② 账号查询（200；404 = 该手机号没有 Nike 账号）
POST /credential_lookup/v1
{"credential": "+8613800000000", "client_id": "<client_id>"}
# 头：Content-Type: application/json、x-nike-ux-id: com.nike.unite

# ③ 发短信（204；429 = 频控）
POST /verification_code/send/v1
{"destination": "+8613800000000", "country": "CN", "type": "CHALLENGE",
 "client_id": "<client_id>", "language": "zh-Hans", "swoosh_login": false}

# ④ 校验验证码（200/204）★不要带 language 字段（带了必 400）
POST /challenge/code/v1
{"credential": "+8613800000000", "verification_code": "123456",
 "client_id": "<client_id>", "verification_code_type": "CHALLENGE"}

# ⑤ 完成会话，拿授权码（200；redirect_uri 里带 code）
POST /check/v1
{"client_id": "<client_id>", "redirect_uri": "<redirect_uri>", "scope": "<scope>",
 "code_challenge": "<同一个 challenge>", "code_challenge_method": "S256",
 "state": "<同一个 state>", "flow": "LOGIN"}
# → {"requiredStates": null, "redirect_uri": "https://...index.html?code=xxxx&state=..."}

# ⑥ 换 token（200）★必须是 form-urlencoded（发 JSON 会 400 unsupported_grant_type）
POST /token/v1
Content-Type: application/x-www-form-urlencoded
grant_type=authorization_code&code=<上一步的 code>&redirect_uri=<redirect_uri>
&client_id=<client_id>&code_verifier=<同一个 verifier>
```

**续期**（access token 一般 1 小时；我们生产靠这条常驻）：
```http
POST /token/v1
Content-Type: application/x-www-form-urlencoded
Referer: https://www.nike.com.cn/
grant_type=refresh_token&refresh_token=<上一步返回的>&client_id=<client_id>
```

## 2. 两个必踩的坑（我们都撞过）

1. `/challenge/code/v1` **不能带 `language`** —— 带 `"zh-Hans"` 稳定 `400`；去掉立刻通过。
2. `/token/v1` **只吃 form-urlencoded**（JSON → `400 unsupported_grant_type`）；
   web 渠道那套还额外要 `Origin: https://www.nike.com.cn`（我们小程序渠道用 `Referer` 即可）。

## 3. 用这把 token 打门店库存（`o2o`）

```http
PUT /o2o/buy/cart_reviews/v2/<uuid>
Authorization: Bearer <access_token>
appid: com.nike.nvs.comerce.wo.wechat.prod
nike-api-caller-id: nike:wechat:web:1.0
x-nike-partner: NDS
x-nike-region: <regions 返回的 id，如 NIKE0S070；**不能** 用 storeCode>
Content-Type: application/json; charset=UTF-8
{"request":{"country":"CN","currency":"CNY","locale":"zh_CN","promotionCodes":[],
 "items":[{"id":"<uuid>","skuId":"<14 位 GTIN>","quantity":1,"valueAddedServices":[],
  "fulfillmentDetails":{"type":"SHIP","location":{"type":"address/shipping","postalAddress":{"country":"CN"}}}}]}}
# → 202；随后 GET 同一个 uuid → 200（fulfillmentGroups=有货 / INVENTORY_INSUFFICIENT=缺货 / PRODUCT_NOT_BUYABLE=该店不卖）
```

**自检**（判定"我这把 token 是不是对的渠道"）：拿库里有真值的组合复跑 —— 例如
`store_code=3930 / region_id=NIKE0S094 / style=IM9441-600`（我们近 24h 实测是 `OUT_OF_STOCK`）。
如果返回 `PRODUCT_NOT_BUYABLE`，八成是 **token 渠道不对（用的小程序那一套吗？）**或
`x-nike-region` 不是 `regions` 的 `id`。

## 4. 想把这把 token 灌进我们平台（可选）

```bash
python -m app.cli import-credential --user-id <平台用户 uuid> \
  --access-token-file jwt_latest.txt --refresh-token-file mp_refresh.txt
```
（生产机：`docker compose exec -T api python -m app.cli import-credential …`；文件内容各一行。）

一用户多账号时用 `--credential-id <credential 行 uuid>` 指明要更新哪个账号；不填且该用户已有
多个账号时会拒绝执行（避免覆盖错行），没有账号则新建一条。
