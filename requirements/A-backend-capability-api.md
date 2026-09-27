# 方向 A：现有能力盘点 → 后端服务功能需求（FR）

> 依据：`requirements/00-project-context.md` + 只读代码分析（2026-09-23/24 版本）
> 分析对象：`nike_store_probe.py`(390 行)、`nike_auth.py`(207 行)、`nike_zero_touch.py`(71 行)、
> `nike_new_arrivals.py`(125 行)、`nike_store_monitor.py`(123 行)，旁证 `nike_store_oracle.py`(290 行，遗留)
> 本文范围：**后端服务**需要具备什么。前端交互、权限角色细节、NFR 具体指标以对应方向文档为准，本文只给接口契约。
> 结论口径：只写"已验证可得"和"需开发"；不把已证伪能力写进需求（见 §1.3）。

---

## 0. 术语

| 术语 | 含义 |
|---|---|
| regionId | `x-nike-region` 请求头取值，决定"在哪个门店"询价（如 `NIKE0S088`、`xian`） |
| storeCode | 门店号（如 `3752`），GTIN 结构接口使用 |
| styleCode | 货号（如 `IO5031-600`） |
| GTIN | 尺码条码（如 `00198487629903`），探针最小单位 |
| 观察(observation) | 一次"门店×货号×尺码 → 有货/价格"的快照 |
| 事件(event) | 两次观察的差异（补货/售罄/变价/上新） |
| 轮次(round) | 一次完整采集周期（现为每小时） |

---

## 1. 现有能力盘点

### 1.1 能力总表

状态载体列：**F=json 文件**，**S=stdout 即弃**，**M=内存**。

| ID | 能力 | 触发方式 | 输入 | 输出 | 依赖 | 状态载体 | 成熟度 |
|---|---|---|---|---|---|---|---|
| C-01 | 门店清单拉取（100 家） | `--list-stores` 手工 | 无 | 门店 id/storeCode/displayName/storeAddress/concept | JWT + HTTP/2 指纹头 | **F** `all_stores.json`、`regions_h2.json` | 可用，未处理分页（响应含 `pages.next anchor=100`，仅取前 100） |
| C-02 | 尺码→GTIN 结构获取（按店） | 探针内部隐式调用 | storeCode + styleCode | `{title, sizes:[{size,gtin}], fetchedAt}` | api-test 匿名 GET | **F** `gtin_cache.json`（键 `{store}:{style}`，34 条/16 店） | 可用；**无 TTL、命中即永久复用**；结构按店不同必须逐店取 |
| C-03 | 单店批量可用性审核（排除法） | `--store X --code Y` | regionId + GTIN 列表 | 有货 GTIN 集合 | JWT + cart_reviews | **M** → 汇总落 F | 已实测；`CHUNK=40` 件/批 |
| C-04 | 价格/促销获取 | 探针 Phase B 自动 | 有货 GTIN 子集 | `{price 标价, discount 促销额, total 实付, promos[]}` | 同上 | **M** → F | 已实测；**必须"整批全可买"且 ≤3 件**才返回价格，需迭代收缩 |
| C-05 | 空完成响应防护 | 自动（轮询内） | GET 响应 | 丢弃不可信空态 | 无 | **M** | 已实现：无 `error` 且无 `fulfillmentGroups` → 继续轮询，`POLL_TRIES=12` 用尽判失败 |
| C-06 | WAF 405 自愈 | 自动 | 405 且 body 含 `data-spm` | 重试 | 无 | **M/S** | 已实测；退避 `2 + i*1.5s`，`tries=7` |
| C-07 | JWT 401 自愈 | 自动 | 401 | 强制续期后重试 | `nike_auth.full_refresh` | **F** `jwt_latest.txt` | 已实现；`auth_retries<2` |
| C-08 | 网络抖动自愈 | 自动 | TransportError/TLS EOF | 重试 | 无 | **S** | `sleep(1.5 + i*1.0)` |
| C-09 | 全国门店扫描 | `--scan --code` | 货号列表 + `--pace` `--skip` | 有货门店 × 尺码 | 上述全部 | **F** `scan_{mmdd_HHMM}.json` | 已跑通（DN3707-202 → 100 店/7 店有货）；**断点=按索引 skip，失败店不记录不重试** |
| C-10 | 变化检测（补货★/售罄✗/变价¥/新监控＋） | 探针结束自动 | 本次结果 + 上次快照 | 变化文本 | 无 | **F** `probe_state_{store}.json`；变化本身仅 **S** | 已实现；**只比 `price.total`；只存最新快照无历史；变化文本随进程丢失** |
| C-11 | 凭证续期 L1（纯 HTTP） | 每次探针/每轮 | `mp_refresh.txt` | 新 access_token + **轮换后** refresh_token | accounts.nike.com.cn | **F** `jwt_latest.txt`/`mp_refresh.txt` | 已实测，数周~月一次 |
| C-12 | 凭证续期 L2（浏览器静默授权兜底） | L1 失败后 | 浏览器会话 + PKCE | 授权码 → token | **外部 CLI** `tabbit-cli.exe`（`--timeout-ms 150000`，subprocess 180s） | **F** 同上 | 可用但**强依赖本机 GUI/浏览器**，非服务化 |
| C-13 | WAF 票据与 UA 采集 | L2 顺带 | 浏览器 cookie | `acw_sc__v2`、浏览器 UA | 同上 | **F** `waf_token.txt`/`ua.txt` | 已实现；主链路（HTTP/2）实际不需要 |
| C-14 | 在线上新监控 | `--loop N` / 每轮 | 无（匿名） | 新增货号 + 标题/现价/原价/折扣/PDP | product_wall 公开接口 | **F** `new_arrivals_state.json`（480 条全量） | 可用；`MAX_PAGES=6`、每页 50、`anchor+=50`、页间 `sleep 2`；**只能发现新增，下线不可知** |
| C-15 | 门店活动/预览池监控 | `--loop`（600s±30 抖动） | storeCode 列表（**硬编码 `["3752"]`**） | 活动标题/副标题 + 10 件预览货号 | store/home 匿名接口 | **F** `state.json` | 可用；diff 出活动变更与预览增减 |
| C-16 | 上新到店探测 | `nike_new_arrivals --store --region` | 新货号（**上限 20 个**） | 门店有货尺码 + 门店价 | 复用 oracle/cart_reviews | F（同一个 `gtin_cache.json`） | 可用；上限为"保护 WAF"的硬编码 |
| C-17 | 零人工每小时编排 | Windows 计划任务 `NikeZeroTouchMonitor` 或 `--loop 3600` | 固定单店 `3752` + `watch.txt` | 各步骤 stdout | subprocess（单步超时 1800s） | **S**（仅打印**末 1500 字符**） | 可用；**顺序执行、无重试、无任务记录、输出被截断** |
| C-18 | 三层故障自诊断 | 异常时打印 | HTTP 响应 | 诊断文案 | 无 | **S** | 可用：区分 CDN ACL / 鉴权 401 / AWS 路由缺失；另含 SSLError(代理自签 CA) → `verify=False` 回退 |
| C-19 | 购物车写探测（遗留） | `nike_store_oracle.py` | storeCode+货号 | 加购成功=有货 + 门店价 | PATCH carts/v2 | **F** `oracle_state_{store}.json` | **已弃用**：会写购物车（加购后清理），`pace` 默认 95s，已被只读 cart_reviews 取代 |

### 1.2 状态载体清单（"库 / files 承载"现状）

现有系统的**全部持久状态**都是工作目录下的 json 文本文件，无数据库、无版本、无并发控制：

| 文件 | 承载 | 写入方 | 读取方 | 关键风险 |
|---|---|---|---|---|
| `all_stores.json` / `regions_h2.json` | 门店清单 100 家 | `--list-stores` | 全部脚本 | 无刷新时间戳、无变更历史；**同店多个 regionId 并存**（`xian` 与 `NIKE0S08` 同为 3875；`NIKE0S001`…）无权威去重规则 |
| `gtin_cache.json` | 尺码↔GTIN 结构（含 title） | 探针/oracle/上新 | 探针/oracle | **无 TTL、无失效标记**；全量 `json.dump` 覆盖写 → 并发进程互相清掉；34 条（覆盖 16 店） |
| `probe_state_{store}.json` | 单店各货号最新快照 + `checkedAt` | `probe_store`→`diff_and_save` | 下一轮 diff | **仅最新态，无历史**；无"首次观察基线"标记；货号消失不清理 |
| `oracle_state_{store}.json` | 遗留写探测快照 | oracle | oracle | 与 probe_state 双份语义重叠 |
| `state.json` | 门店活动/预览池快照（`store_3752`） | store_monitor | store_monitor | 单文件承载全部门店；键名 `store_{sn}` 无 schema |
| `new_arrivals_state.json` | 全量在线新品（480 条，129KB） | new_arrivals | new_arrivals | 每轮全量重写；**无"下架"检测**；无更新时间线 |
| `scan_{mmdd_HHMM}.json` | 某次全国扫描结果 | scan_stores | 人工 | **文件名即任务 ID**，同名分钟级碰撞即覆盖；无失败店清单 |
| `watch.txt` | 关注货号（3 条） | 人工 | 探针 | **全局一份，无客户维度**；无门店/客户绑定 |
| `jwt_latest.txt` / `mp_refresh.txt` / `web_access_token.txt` | 凭证 | `nike_auth` | 探针/oracle | **明文落盘、无加密、无权限控制**；单一账号；刷新无锁 |
| `waf_token.txt` / `ua.txt` | WAF 票据 / UA 指纹 | `nike_auth` L2 | oracle | 主链路不使用；陈旧值可能误导 |

### 1.3 已证伪 / 不可达（禁止写入需求）

- ❌ **单店全量商品清单**（"这个店一共多少件货"）：微信云容器 + AWS_IAM + CDN ACL 三重墙，已代码级证伪。
  替代方案只能：`store/home` 预览池（每店 10 件轮换）+ 指定货号探针 + 在线目录货号全集回扫。
- ❌ 依赖微信客户端/adb/模拟器/Frida/OCR/截图的任何路径（含 `scan*.sh`、`nike_emulator_harvest.py` 内存搜 refreshToken 一类，一律不作为服务方案）。
- ⚠️ 在线目录只能给"在线渠道"的库存/价格语义，**不等于门店库存**，不可替代门店探针（详 §5-R10）。

---

## 2. 从脚本到服务的差距清单

> 判据：多客户（10-20 家）× 并发（手动+定时+多任务）× 7×24（无人值守）。

| ID | 差距 | 现状证据 | 后果 |
|---|---|---|---|
| G-01 | **单账号单凭证** | 仅 `jwt_latest.txt` 一份；`CLIENTS` 里 mp/web 两个 client 但不同时用 | 10-20 客户共享一个账号配额，风控一挂全停 |
| G-02 | **无并发与隔离** | 全程单进程顺序；`gtin_cache.json` 先读后全量覆盖写 | 两个扫描并发 → 缓存/状态互相覆盖；手动任务与定时任务抢同一 JWT |
| G-03 | **无数据库，状态在 json** | `probe_state_*.json`、`state.json`、`new_arrivals_state.json` | 无历史/无索引/无事务；"什么时候补的货、持续多久"无法回答；文件损坏即数据丢失 |
| G-04 | **无队列/无优先级** | `nike_zero_touch.run_once` 顺序 `subprocess` 调 3 个脚本 | 手动全国扫描插不进来；长任务阻塞后续轮次 |
| G-05 | **无限速与配额治理** | `pace`/`CHUNK`/`sleep` 全是代码常量，无全局令牌桶 | 无法约束总请求量；账号突发触发冷却/401 只能被动等 |
| G-06 | **断点续跑≈无** | `--skip` 只按门店**索引**跳过 | 失败店既不记录也不重试，扫描结果不完整且无人知 |
| G-07 | **无通知** | 全仓库无 webhook/钉钉/企微/短信代码（已 grep 确认） | 变化只 print，且被 `nike_zero_touch` 的"末 1500 字符"截断 → 客户永远收不到提醒 |
| G-08 | **无审计/无结构化日志** | 只有 `print`；无 `logging`、无 request-id、无 task-id | 出事无法追溯；无法回答"这条数据是哪次任务、哪个账号、哪个批次产生的" |
| G-09 | **数据正确性防线薄弱** | ① GTIN 缓存无 TTL；② `scan_stores` 用 `sn or "3752"` 兜底结构；③ 变价只比 `price.total`；④ 上新只检测新增 | 静默错数据（把钱/缺货判断错），违反"数据与门店实际一致"的验收标准 |
| G-10 | **无健康检查/无观测** | 无 `/healthz`、无指标、无失败率统计；`run_script` 超时只 print | 7×24 下"静默死亡"不可发现 |
| G-11 | **配置非产品化** | 门店硬编码 `3752`；`watch.txt` 全局；无客户/门店/货号关系模型 | 无法给不同客户不同监控范围 |
| G-12 | **外部 GUI 依赖未隔离** | L2 兜底 `subprocess` 调 `tabbit-cli.exe`（本机浏览器） | 服务器/容器化部署时兜底路径失效且无替代 |
| G-13 | **接口契约无守护** | 直接解析未公开接口字段，无契约测试 | 上游字段变更 → 静默产出脏数据而非报错 |
| G-14 | **请求量级未评估** | 在线目录实测 3273~4719 码；单货号全国扫描 = 100 店 ×(1 PUT + 2~5 GET) + 价格 Phase B | 上百货号 × 多客户时请求量爆炸（估算见 §5-R1） |

---

## 3. 后端功能需求（FR）

优先级：**P0 = 不做就无法交付给客户**；P1 = 影响可用性/成本，首个迭代后补齐；P2 = 增强。

### M1 凭证与账号管理

| ID | P | 需求 | 验收口径 |
|---|---|---|---|
| FR-M1-01 | P0 | 凭证存取集中化：access_token / refresh_token 由服务统一持有，进程内不落明文文件，DB 中加密存储 | DB 中无明文 token；`grep -r "eyJ"` 工作目录无命中；服务重启后凭证可用 |
| FR-M1-02 | P0 | refresh_token 续期原子性：新 rt 写入成功后才替换旧 rt（先写新、再切换指针、保留上一版） | 注入"写库失败"故障后，仍能用旧 rt 成功续期；不出现"rt 丢失需人工登录" |
| FR-M1-03 | P0 | 提前续期 + 单飞（single-flight）：access_token 剩余 <5 分钟自动续期；并发 N 个任务同时需要续期时只有 1 次真实 token 请求 | 并发 10 任务触发续期，上游 token 请求计数 = 1；JWT 永不在有效期内被使用过期 |
| FR-M1-04 | P0 | 401 强制续期重试 ≤2 次，其后进入冷却；连续失败达阈值（默认 3）→ 全局熔断，暂停所有上游任务并告警 | 模拟上游持续 401：请求次数被限制在阈值内，任务标记 `blocked`，告警送达 |
| FR-M1-05 | P0 | 风控退避：连续 401/403/405 触发指数退避（含抖动）+ 冷却期；冷却期内自动降速（降低并发与批次频率） | 触发后 10 分钟内上游请求速率下降 ≥50%；冷却结束自动恢复，无需人工 |
| FR-M1-06 | P0 | 失效告警与状态可见：L1 续期失败即告警（企微/钉钉）；暴露"剩余有效期/上次续期时间/结果/账号标识" | `GET /credentials/status` 返回字段齐全；人为删除 rt 后 5 分钟内收到告警 |
| FR-M1-07 | P0 | L2 兜底解耦：不阻塞主服务；生成一次性授权链接 + `待人工授权` 状态 + 通知；人工完成后续期自动回填恢复 | 服务在 L2 不可用时仍可启动与响应查询 API（降级为"数据陈旧"标记）；恢复后无需重启 |
| FR-M1-08 | P1 | 多账号池：N 个账号，按门店/轮次哈希分配；单账号配额耗尽或熔断时自动切换 | 池内 2 账号，人为禁用 1 个，采集任务在 1 个轮次内自动全量转移 |
| FR-M1-09 | P1 | 备用客户端凭证（web client）并行维护，作为 mp client 失效时的降级通道 | 主凭证失效超阈值后自动尝试备用凭证，恢复一次成功后回到主用 |
| FR-M1-10 | P2 | 凭证到期预测（基于历史有效期）与提前 N 天预警 | 预警时间与实际失效时间偏差 ≤ 1 天 |

### M2 任务调度

| ID | P | 需求 | 验收口径 |
|---|---|---|---|
| FR-M2-01 | P0 | 任务类型化：`single_store_probe` / `national_scan` / `new_arrivals_poll` / `store_promotion_poll` / `gtin_structure_refresh` / `store_list_refresh` | 每类任务有独立参数 schema 与结果 schema；非法参数被拒绝（400） |
| FR-M2-02 | P0 | 队列 + 对上游串行执行（上游为同一账号，写/读打散由限速器统一控制）；任务状态机 `queued → running → succeeded/failed/partial/cancelled` | 提交 20 个任务，`running` 并发数不超过配置上限；任意时刻无两个任务使用同一 reviewId |
| FR-M2-03 | P0 | 优先级：手动任务 > 定时任务 > 批量回填；低优先级不得饿死（老化提权） | 持续提交低优先级任务时，高优先级任务 P95 入队等待 < 60s；低优先级终会执行 |
| FR-M2-04 | P0 | 定时调度：cron/间隔表达式按任务独立配置（替代 Windows 计划任务）；支持时区；错过（服务停机）后按"补一次不连发"策略恢复 | 停机 3 小时后重启：每个定时任务最多补跑 1 次，日志可见补跑原因 |
| FR-M2-05 | P0 | 失败重试：任务级可配（次数/间隔/退避）；**部分失败**（部分门店失败）标记 `partial` 并保存失败门店清单 | `partial` 任务可在 API 中查到 failed_stores 列表（门店号 + 错误码 + 尝试次数） |
| FR-M2-06 | P0 | 断点续跑：任务持久化游标（已完成门店集合），重跑只处理未完成项；替代"按索引 skip" | 中途 kill 进程后重跑，已完成门店不重复请求（请求数对比可验证） |
| FR-M2-07 | P0 | 幂等入队：同一（任务类型 × 门店集合 × 货号集合）在去重窗口（默认 5 分钟）内只入队一次 | 连点 5 次提交，队列仅 1 条；返回同一 task_id |
| FR-M2-08 | P1 | 全局速率闸：令牌桶控制上游 RPS/每分钟请求数，扫描任务与探针任务共享配额 | 突发提交多任务时，上游分钟请求数不超过配置上限（监控指标可验证） |
| FR-M2-09 | P1 | 任务超时与取消：超时上限可配；取消后立即停止后续批次并释放配额 | 取消一个 60/100 店的扫描，10s 内状态变 `cancelled`，完成数不再增长 |
| FR-M2-10 | P2 | 编排 DAG：`凭证 → 结构 → 可用性 → 价格 → 落库 → 事件 → 通知`，单步可重入 | 单步失败可从该步重跑而不重跑前置步骤 |

### M3 探针执行器

| ID | P | 需求 | 验收口径 |
|---|---|---|---|
| FR-M3-01 | P0 | 可用性大批量策略：单批 ≤40 件可配；用"不可买清单 + 排除法"判定有货 | 与逐件单件探测结果一致（抽样 20 组比对，差异 = 0） |
| FR-M3-02 | P0 | 价格小批量策略：仅对"在售且有货"子集，按 ≤3 件/批取价；迭代收缩剔除已失效项 | 对 7 店有货样例（DN3707-202，¥1399）价格与门店一致；不因混入无货项而整批丢价 |
| FR-M3-03 | P0 | 空完成响应防护：无 `error` 且无 `fulfillmentGroups` 的完成态视为不可信 → 重试；用尽重试记 `failed`，**不写库、不产生事件** | 注入空响应后：任务记为失败，DB 无该批次观察记录，不产生"售罄"误报 |
| FR-M3-04 | P0 | WAF 405 重试：仅当 405 且响应含 `data-spm` 才判定为挑战；指数退避 + 上界；超限记录并告警 | 注入 405 挑战：请求自动重试并最终成功；重试次数不超过配置上限 |
| FR-M3-05 | P0 | 网络异常重试：连接重置/TLS EOF/超时均重试；重试采用新 reviewId，保证幂等无副作用 | 断网 30s 内重试成功后数据完整；购物车内容（人工抽查）无任何变化 |
| FR-M3-06 | P0 | 结果结构规范化：每条观察含 `storeCode/regionId/styleCode/size/gtin/inStock` + `price/fullPrice/discount/promos[]` + `checkedAt` + `taskId` + `qualityFlag` | API 返回字段与契约一致；无字段可空歧义（缺价格时显式 `price: null` + 原因） |
| FR-M3-07 | P0 | 数据校验闸：① GTIN 格式（纯数字、长度与结构一致）② 尺码集合与结构 plan 对齐（返回 skuId 必须属于本次提交集合）③ 价格合理性 `0 < total ≤ price`，折扣异常（total > price 或 < 0.1×price）标 `suspect` ④ `suspect` 数据不进入事件流 | 人为构造越界价格：记录入库但标记 `suspect`，不产生通知；看板可见 suspect 计数 |
| FR-M3-08 | P0 | 禁止跨店结构兜底：结构缺失即标记该（门店×货号）为 `no_structure`，**不得**回退到其他门店结构 | 移除某店结构后探测该店：返回 `no_structure`，不产生任何库存/价格数据 |
| FR-M3-09 | P0 | 零副作用自检：仅使用只读 `cart_reviews`；启动时自检不包含任何购物车写路径（`PATCH/POST /o2o/buy/carts`） | 代码扫描 + 启动自检通过；审计日志中无 carts 写请求 |
| FR-M3-10 | P1 | 限速可配且可观测：每店间隔、每批间隔、轮询次数与间隔、并发度均可配置；实时暴露已用配额 | 修改配置无需重启（或秒级生效）；配额看板与上游实际请求数一致（误差 ≤1%） |
| FR-M3-11 | P1 | 结果缓存：同（门店×货号）在 TTL（默认 5 分钟）内命中缓存，避免重复探测 | 5 分钟内重复请求返回同 `checkedAt`；上游请求数不增加 |
| FR-M3-12 | P1 | 接口契约守护：对 cart_reviews / product_details / store_home 的响应做 schema 断言；字段缺失即告警并降级（标记 `contract_broken`） | 人为删字段后：任务标记 `contract_broken`，产出脏数据被拦截，告警送达 |
| FR-M3-13 | P2 | 备用探测通道：cart_reviews 不可用时，降级用在线 availability + 门店预览池做"部分替代"，并在数据上标注来源与置信度 | 主通道关闭时系统仍能返回带 `source=fallback` 标记的结果，不静默混同 |

### M4 数据落库与变更检测

| ID | P | 需求 | 验收口径 |
|---|---|---|---|
| FR-M4-01 | P0 | 观察表：每次探测按 门店×货号×尺码 落一条（含价格/促销/时间/任务），追加式不覆盖 | 单次全国扫描 100 店 × 1 货号 18 码 → 可查到 ≤1800 条（含无货）观察记录 |
| FR-M4-02 | P0 | 当前态表：门店×货号×尺码的"最新状态"（便于查询），由观察表推导维护 | 查询 API 响应时间 P95 < 300ms（100 店 × 常用货号规模下） |
| FR-M4-03 | P0 | diff 引擎：产出 4 类事件 `restock`（补货）/`soldout`（售罄）/`price_change`（变价，含原价→现价、促销文案变化）/`new_arrival`（上新到店）；每条含 before/after | 对样例 IO5031-600 制造"41 码售罄"→ 产生 1 条 `soldout`（size=41），可查 before/after |
| FR-M4-04 | P0 | 事件幂等：同（门店×货号×尺码×类型×值）不重复生成；重复运行同一批数据事件数不增长 | 同一份数据跑 2 次 diff：第 2 次事件数 = 0 |
| FR-M4-05 | P0 | 事件持久化与可查询：事件落库（不依赖 stdout），带 `event_id/occurred_at/task_id/source` | 进程被杀后重启，事件仍可查；stdout 清空不影响事件（当前是反例） |
| FR-M4-06 | P0 | 首次观察基线：新加入监控的（门店×货号）只写基线，不产生"补货/售罄"噪声 | 新增 5 个货号监控：事件数 = 0（或仅 `baseline` 类型），并有 `is_baseline` 标记 |
| FR-M4-07 | P1 | 保真度标记：数据陈旧（超过新鲜度阈值）时，当前态与事件均带 `stale=true` | 停止采集 30 分钟后查询：`stale=true`，前端可显示"数据可能过期" |
| FR-M4-08 | P1 | 保留与归档：观察记录保留 N 天（默认 90）后归档；事件长期保留；归档任务可观测 | 归档任务日志可见条数与耗时；归档后历史查询仍可用（走归档表） |
| FR-M4-09 | P1 | 尺码粒度取消监控：货号在门店下架/结构消失时，标记 `discontinued` 而非静默保留 | 移除某货号后，其当前态标记 `discontinued`，不再产生误报变化 |
| FR-M4-10 | P2 | 数据质量统计入库：每轮 success / failed / empty / suspect / no_structure 计数 | 看板可按任务/门店查看质量趋势 |

### M5 查询与聚合（FR 层，接口见 §4）

| ID | P | 需求 | 验收口径 |
|---|---|---|---|
| FR-M5-01 | P0 | 门店列表与详情查询（按城市/concept/关键字过滤，分页） | 100 家门店 ≥1 页可完整返回，含 `lastCheckedAt` |
| FR-M5-02 | P0 | 单店×多货号 尺码级库存 + 价格/促销查询（批量） | 一次请求返回 N 个货号的 `sizesInStock/sizesTotal/price`；样例 IO5031-600 与实测一致 |
| FR-M5-03 | P0 | 货号维度的门店分布查询（"哪些店有货"） | DN3707-202 返回 7 家有货门店且尺码分布与实测一致（南昌明矾 40-45 共 9 码；九江庐山/北京京良 41-44.5；广州西环 41-44；武汉江夏 45,46；宁波江南 46,47.5；海口观澜湖 42.5） |
| FR-M5-04 | P0 | 变化事件流查询（时间范围/类型/门店/货号/客户维度，分页） | 可按 `from/to + type=restock` 过滤；返回含 before/after 与事件时间 |
| FR-M5-05 | P0 | 扫描任务状态与结果查询（进度、失败门店、结果明细） | 任务运行中可查 `progress=63/100`；结束后可导明细 |
| FR-M5-06 | P0 | 所有响应携带数据新鲜度（`lastCheckedAt` / `stale`），无数据时明确 404/空集而非 500 | 未监控货号返回空集 + `reason=not_monitored` |
| FR-M5-07 | P1 | 门店活动/预览池查询（活动规则文案 + 当前预览 10 件 + 历史变更） | 与 `store/home` 实测一致（如"满减专区 / 鞋、服、配件 55折/7折；鞋、配件 6折"） |
| FR-M5-08 | P1 | 上新产品列表查询（在线新品墙 480 件基线 + 增量） | 可按时间倒序返回新增货号与标题/现价/原价 |
| FR-M5-09 | P1 | 监控范围（watchlist）管理：按客户维护"门店集合 × 货号集合"，替代全局 `watch.txt` | 客户 A/B 各有独立清单，互不可见、互不影响采集调度 |
| FR-M5-10 | P2 | 导出与聚合统计：CSV/XLSX 导出、按门店/货号的库存覆盖统计 | 导出文件行数与查询结果一致 |

### M6 通知推送

| ID | P | 需求 | 验收口径 |
|---|---|---|---|
| FR-M6-01 | P0 | 订阅规则：客户维度 = 门店集合 × 货号集合 × 事件类型（补货/售罄/变价/上新） | 客户仅订阅"A 店"时，B 店事件不推送（抽样验证） |
| FR-M6-02 | P0 | 至少 1 个可用通道（推荐企微/钉钉机器人 webhook）+ 通道健康检查 | 触发测试事件，30s 内到达；通道不可用时状态为 `degraded` 并告警 |
| FR-M6-03 | P0 | 去重与静默期：同（客户×门店×货号×尺码×类型）在静默期（默认 30 分钟，可配）内只推一次；数据 `suspect` 不推 | 同一事件重复入库 3 次：仅收到 1 条通知 |
| FR-M6-04 | P0 | 发送失败重试 + 死信记录 + 可查询发送审计 | 人为让 webhook 返回 500：重试 ≥3 次后进入死信，审计可见失败原因 |
| FR-M6-05 | P1 | 摘要合并：同一轮次、同一客户的多条事件合并为 1 条消息（如"3 个货号补货、1 个变价"） | 一轮 10 条事件 → 客户收到 ≤3 条消息（可配阈值） |
| FR-M6-06 | P1 | 免打扰与优先级：客户可配时段；高优先级货号（如限量款）可越级 | 免打扰时段内事件入队不推送，时段结束后合并补发 1 条 |
| FR-M6-07 | P2 | 短信/电话升级通道（仅高优先级客户/货号） | 触发升级链路可在审计中看到 `channel=sms` 记录 |
| FR-M6-08 | P2 | 通知模板可配（变量：门店名/货号/标题/尺码/价格/时间/PDP 链接） | 改模板不重启即生效 |

### M7 权威数据源管理

| ID | P | 需求 | 验收口径 |
|---|---|---|---|
| FR-M7-01 | P0 | 门店清单定时刷新（每日 1 次）+ **分页遍历**（处理 `pages.next`）+ 变更历史（新增/下线门店告警） | 刷新任务完成且记录页数；门店数变化时产生 `store_added/store_removed` 记录 |
| FR-M7-02 | P0 | regionId↔storeCode 权威映射：同店多 regionId 去重（主键选定规则），保留别名表 | 查询门店 3875 只返回 1 条门店记录，别名 `xian/NIKE0S08` 可解析到同一门店 |
| FR-M7-03 | P0 | GTIN 结构缓存 TTL 与失效：按店缓存，TTL 可配（建议 24h~7d），过期重取；结构变化产生 `structure_changed` 事件 | 人为改 TTL=0：下次探测重新拉结构；结构变化可见事件 |
| FR-M7-04 | P0 | 逐店结构独立性：同货号在不同门店可有不同 GTIN 集合，分别存储、分别探测、不得互相复用 | DN3707-202（两套 GTIN）在 2 家店分别探测，结果各自独立且与实测一致 |
| FR-M7-05 | P0 | 结构缺失与不可达登记：`no_structure`、`store_not_sell`（PRODUCT_NOT_BUYABLE）等归类并可视化 | 在线目录有货但门店 0 有货（IF1823-010 全国 0 店）→ 明确记 `no_stock_anywhere` 而非报错 |
| FR-M7-06 | P1 | 货号元数据独立来源：标题/图片/原价来自在线目录，与门店结构解耦（结构缺标题时仍可展示） | 门店结构无 title 时展示在线目录 title |
| FR-M7-07 | P1 | 不可达能力策略登记（产品文档化）：门店全量商品清单不可得 → 用"预览池 + 高频货号探针 + 在线目录全集回扫"替代，并在 API 明确说明口径 | 前端在"店里有什么货"入口展示替代口径说明与采样范围 |
| FR-M7-08 | P2 | 结构数据源降级：api-test 不可用时复用旧结构并标 `structure_stale`，同时告警 | 断开 api-test：探测仍可进行但结果标 `structure_stale=true` |

### M8 运维与观测

| ID | P | 需求 | 验收口径 |
|---|---|---|---|
| FR-M8-01 | P0 | 结构化日志（JSON）：含 `task_id/request_id/store/style/action/retry/upstream_status/duration`；凭证与 token 脱敏 | 任意一条数据可回溯到 task_id 与上游状态码；日志中无 token 明文 |
| FR-M8-02 | P0 | 健康检查：`/health` 返回各依赖状态（凭证有效性、上游连通性、队列积压、最近成功轮次时间、DB） | 凭证失效或队列积压超阈值时 `health=degraded` 并指明原因 |
| FR-M8-03 | P0 | 上游指标：405/401/空响应/超时/契约破坏 分类计数与比率；按门店与账号维度 | 看板可显示"今日 405 占比 x%、空响应 y 次" |
| FR-M8-04 | P0 | 异常告警：任务连续失败 ≥N、凭证失效、扫描成功率 < X%、上游契约破坏 → 告警（与 M6 同通道） | 制造连续失败：告警在 1 个轮次内送达 |
| FR-M8-05 | P1 | 配额看板：今日请求数（按任务/门店/账号）、剩余配额估算、限速命中次数 | 看板数值与上游实际调用计数偏差 ≤1% |
| FR-M8-06 | P1 | 每日运行报告：轮次成功率、观察数、事件数、失败门店 TOP、suspect 数 | 每日定时产出且可查询历史 30 天 |
| FR-M8-07 | P1 | 配置热更新：限速、静默期、watchlist、TTL 不重启生效 | 改配置后 60s 内生效（日志可验证新值） |
| FR-M8-08 | P1 | 备份与恢复：DB 定期备份 + 恢复演练；凭证加密备份与恢复 | 从备份恢复后，凭证可用、事件与观察数据完整（行数比对一致） |
| FR-M8-09 | P2 | 部署形态隔离：L2 授权兜底与主服务分离（可选人工工单），主服务可容器化、无 GUI 依赖 | 容器内主服务启动不依赖 `tabbit-cli.exe` 存在 |

---

## 4. 接口草案（REST 级）

约定：
- 前缀 `/api/v1`；认证：`Authorization: Bearer <服务侧 API Key / 客户 JWT>`（P0）
- 分页：`?page=1&pageSize=50`，响应 `{items, page, pageSize, total}`；时间统一 ISO8601（+08:00）
- 所有库存/价格响应携带 `lastCheckedAt` 与 `stale`；错误体 `{code, message, details}`

| # | Method & Path | 关键参数 | 返回要点 | P |
|---|---|---|---|---|
| 1 | `GET /api/v1/stores` | `city, concept, q, page, pageSize` | 门店列表：`storeCode, regionId, displayName, storeAddress, concept, lastCheckedAt` | P0 |
| 2 | `GET /api/v1/stores/{storeCode}` | — | 门店详情 + 别名 regionId 列表 + 最新活动摘要 + 数据新鲜度 | P0 |
| 3 | `GET /api/v1/stores/{storeCode}/stock` | `styleCodes=A,B,C`（≤20） | 每货号：`title, sizesInStock[], sizesTotal, price{price,fullPrice,discount,total,promos[]}, lastCheckedAt` | P0 |
| 4 | `POST /api/v1/inventory/query` | body: `storeCodes[] × styleCodes[]`（矩阵查询） | 结果矩阵 + 每格 `{inStock sizes, price, lastCheckedAt, stale}`；超限返回 400 与上限说明 | P0 |
| 5 | `GET /api/v1/styles/{styleCode}/availability` | `city?, storeCodes?, onlyInStock=true` | 有货门店列表 + 各店尺码集合 + 门店价；`no_stock_anywhere=true` 时明确标注 | P0 |
| 6 | `GET /api/v1/styles/{styleCode}/history` | `storeCode, from, to, granularity` | 尺码库存与价格时间线（来自观察表） | P1 |
| 7 | `GET /api/v1/changes` | `type=restock\|soldout\|price_change\|new_arrival, storeCode, styleCode, from, to, customerId, page` | 事件流：`eventId, type, storeCode, styleCode, size, before, after, occurredAt, isBaseline` | P0 |
| 8 | `GET /api/v1/new-arrivals` | `from, to, onlyNew=true, page` | 上新货号 + 标题/现价/原价/折扣/PDP 链接 + 是否已到店 | P1 |
| 9 | `GET /api/v1/stores/{storeCode}/promotions` | — | 活动规则（title/subtitle）+ 预览池货号 + 变更历史 | P1 |
| 10 | `POST /api/v1/scan-tasks` | body: `styleCodes[], targetStores?（默认全国）, priority, pace?` | `taskId, status=queued, estimatedRequests, deduped` | P0 |
| 11 | `GET /api/v1/scan-tasks/{taskId}` | — | `status, progress{done,total}, succeeded, failedStores[{storeCode,errorCode,tries}], startedAt, finishedAt` | P0 |
| 12 | `GET /api/v1/scan-tasks/{taskId}/results` | `onlyInStock, page` | 有货门店 × 尺码 + 价格 + 每店 `no_structure` 标记 | P0 |
| 13 | `GET /api/v1/scan-tasks` | `status, type, from, to, page` | 任务列表（含 `partial` 与失败摘要） | P0 |
| 14 | `POST /api/v1/scan-tasks/{taskId}/retry` | body: `onlyFailed=true` | 重试失败门店 → 返回新 taskId 或原任务续跑游标 | P0 |
| 15 | `POST /api/v1/scan-tasks/{taskId}/cancel` | — | 取消并返回已完成数量 | P1 |
| 16 | `POST /api/v1/probe-tasks` | body: `storeCodes[], styleCodes[], priority=high` | 手动即时探针（优先级高、可插队）→ `taskId` | P1 |
| 17 | `GET /api/v1/watchlist` / `POST` / `DELETE /{id}` | `customerId, storeCodes[], styleCodes[], notifyTypes[]` | 监控范围 CRUD（替代全局 `watch.txt`） | P0 |
| 18 | `GET /api/v1/credentials/status` | — | `accounts[{accountId, tokenRemainingSec, lastRefreshAt, lastRefreshResult, state}]`、`wafState` | P0 |
| 19 | `POST /api/v1/credentials/refresh` | body: `accountId?, client=mp\|web, force?` | 触发续期（单飞）→ 结果；用于人工兜底后回填 | P1 |
| 20 | `GET /api/v1/subscriptions` / `POST` / `PUT /{id}` / `DELETE /{id}` | `customerId, channel(wecom\|dingtalk\|sms), quietPeriodMin, quietHours` | 通知规则 CRUD | P0 |
| 21 | `GET /api/v1/notifications` | `from, to, customerId, channel, status=sent\|failed\|dead` | 发送审计：内容摘要、结果、重试次数 | P1 |
| 22 | `GET /api/v1/datasources/status` | — | 门店清单最后刷新时间/门店数、GTIN 缓存命中率、结构 stale 数、上游错误率 | P0 |
| 23 | `GET /api/v1/health` | — | `{status: ok\|degraded, checks:{credentials, upstream, queue, db, lastRoundAt}}` | P0 |
| 24 | `GET /api/v1/metrics/usage` | `from, to, groupBy=task\|store\|account` | 请求量、限速命中、405/401/空响应计数、配额余量估算 | P1 |
| 25 | `GET /api/v1/export` | `type=stock\|changes, 过滤参数, format=csv\|xlsx` | 导出文件流（异步任务化，超限转 taskId 轮询） | P2 |

---

## 5. 风险与开放问题

| ID | 风险/待决策 | 说明与建议 |
|---|---|---|
| R-01 | **单账号请求量能否支撑 10-20 客户** | 估算：单货号全国扫描 ≈ 100 店 ×(1 PUT + 2~5 GET) ≈ 300~600 请求，加价格 Phase B 约 +200~600 → **单货号 ≈ 0.5~1.2k 请求**。20 客户 × 20 货号/天 = 400 货号 → **20 万~50 万请求/天**。需实测账号上限并决策：多账号池（FR-M1-08）是否提前到 P0 |
| R-02 | **L2 浏览器兜底的归属** | 现状依赖本机 `tabbit-cli.exe`（GUI）。必须决策：① 保留半自动人工工单 ② 服务器侧 headless 浏览器（合规/封号风险）③ 完全依赖 L2 前置的长期 refresh_token 管理 |
| R-03 | **数据新鲜度 SLA** | 每小时轮次是否满足客户？"补货提醒"的时效要求（小时级 vs 分钟级）直接决定请求量与成本，需客户书面确认 |
| R-04 | **cart_reviews 是未公开接口** | 随时可能变更/加签，属**供应商风险**。缓解：契约守护（FR-M3-12）+ 备用通道（FR-M3-13）+ 结果回归测试（用 DN3707-202 / IF1823-010 固定样例做金标准） |
| R-05 | **GTIN 结构 TTL 取值** | 结构变化频率未知（新配色/季节切换）。建议先 24h + 结构变化事件观察一周再定 |
| R-06 | **"店里有什么货"的客户预期** | 已证伪全量清单。必须产品化地告知替代口径（预览池 10 件 + 指定货号探针），否则验收必然扯皮 |
| R-07 | **尺码级通知的噪音量** | 100 店 × 多货号 × 多尺码的事件量可能极高。需先量化（建议用现有 probe_state 历史重放估算），再定静默期与摘要策略（FR-M6-05）默认值 |
| R-08 | **多客户数据隔离模型** | 采集数据是全局共享的（同一份门店数据服务所有客户），隔离只应在"订阅范围 + 通知"层。若客户要求数据独占，成本将按门店/货号线性上升 → 需明确商业模式 |
| R-09 | **门店主键与同店多 regionId** | `all_stores.json` 中 3875 同时存在 `xian` 与 `NIKE0S08`，`NIKE0S001` 等格式不统一。需定义 storeCode 为唯一主键 + regionId 别名表，否则会出现重复门店与重复通知 |
| R-10 | **"有货"语义与验收口径** | `INVENTORY_INSUFFICIENT` = 在售但该尺码无货；`PRODUCT_NOT_BUYABLE` = 该店不卖。线上 availability（isAvailable/ship 水位）与门店口径不同，**不可混用**。验收需明确"有货"= 该店该尺码可正常下单 |
| R-11 | **部署形态与 IP 稳定性** | 本机 Windows vs 云主机：IP 变更是否触发风控未知（现状本机实测稳定）。建议先保留固定出口 IP 的方案 |
| R-12 | **HTTP/2 依赖与代理干扰** | 主链路依赖 `httpx(http2=True)`，HTTP/1.1 下非 GET 全 405；本机存在 Reqable 自签 CA 导致需要 `verify=False`。服务化需固定 TLS/代理策略（建议 tun/无代理 + 证书校验开启），否则 `verify=False` 属安全债 |
| R-13 | **是否保留购物车写探测** | `nike_store_oracle.py` 会写购物车（加购+清理），有账号与合规风险。建议明确弃用并从服务代码路径中移除，仅作历史参考 |
| R-14 | **watch.txt → 多客户 watchlist 的迁移** | 现状 3 个货号 + 硬编码门店 3752。需先确定"客户初始监控范围"如何产生（人工录入 vs 从门店预览池/在线折扣目录自动推荐） |
| R-15 | **上新监控的"下线"盲区** | `product_wall` 只能发现新增，货号下线不可知（状态文件全量覆盖）。是否需要"在线下架"事件？若要，需保存每轮全量墙快照并做双向 diff（存储成本上升） |
