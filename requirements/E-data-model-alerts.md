# 方向 E：数据模型 + 变更检测/告警规则需求

> 上游：`requirements/00-project-context.md`。本文只覆盖**后端数据库与规则引擎**要支撑的东西。
> 证据来源（只读实测）：`gtin_cache.json`、`probe_state_{store}.json`、`scan_0924_0716.json`、`scan_0924_0808.json`、
> `nike_store_probe.py`、`state.json`、`new_arrivals_state.json`、`regions_h2.json`、`sale_check.log`。
> 记号：**P0** 交付阻断 / **P1** 首版应做 / **P2** 可延后。字段类型以 PostgreSQL 语义书写。

---

## 0. 三条决定模型形状的实测事实（先读，否则设计会错）

| # | 事实 | 证据 | 对模型的影响 |
|---|---|---|---|
| F1 | **GTIN 结构按门店不同**：同一货号 `DN3707-202` 在店 3752 是 18 码（40–44.5，GTIN 前缀 `00198486…`），在店 3875 是 26 码（35.5–54.5，GTIN 前缀 `00196149…`） | `gtin_cache.json`（`3752:DN3707-202` vs `3875:DN3707-202`）；`probe_state_3875.json` 显示 `sizesTotal: 26` | **GTIN 不是全局 SKU 主键，是"店级"属性**。跨店唯一可比的键只有 `(style_color, size_label)`。必须有"结构版本(StructureVariant)"实体 + 结构缺失时禁止推断缺货 |
| F2 | **价格只在"全可买"的 ≤3 件批次里返回**；有缺货时无价格字段（`price: null`） | `nike_store_probe.py` Phase B 注释（第 210–231 行）；`probe_state_3752.json` 中 `IF1823-010` `price: null` | 价格是**条件可得**的稀疏数据：模型必须允许 `price IS NULL` 且区分"无价" vs "价格为 0"；价格事件必须带 `price_confidence` |
| F3 | **观测可信度会翻转**：`scan_0924_0716.json`（07:16）显示店 3460 的 `IF1823-010` 6 个尺码全部有货，同店 07:20 单店精查 `probe_state_3460.json` 却为 0 码有货；08:08 全国扫描 0 店有货 | `scan_0924_0716.json` vs `probe_state_3460.json` vs `scan_0924_0808.json` | 必须把"**不可信响应**"与"缺货"分开建模，否则会批量误报售罄。这是 DQ 规则的直接依据 |

另有门店别名事实：`regions/v1` 对**同一门店返回两条记录**（`id: "xian"` 与 `id: "NIKE0S08"`，`storeCode` 同为 `3875`，名称地址相同）→ 门店主键必须是 `storeCode`，`regionId` 是可多值的**别名**（而 `x-nike-region` 请求头只能吃 regionId）。

---

## 1. 实体清单与字段

### 1.1 租户与权限

**Tenant（客户租户）**
| 字段 | 类型 | 约束 |
|---|---|---|
| id | uuid | PK |
| name | text | NOT NULL, UNIQUE |
| contact_email | citext | NOT NULL |
| plan | text | CHECK IN ('trial','basic','pro')，P1 |
| status | text | CHECK IN ('active','suspended','trial') |
| settings | jsonb | 默认静默时段、聚合窗口、通知渠道偏好 |
| created_at / updated_at | timestamptz | NOT NULL default now() |

索引：`UNIQUE(name)`；`INDEX(status)`。

**User（用户）**
| 字段 | 类型 | 约束 |
|---|---|---|
| id | uuid | PK |
| tenant_id | uuid | FK→Tenant, NULL 表示平台账号 |
| email | citext | NOT NULL |
| password_hash | text | NULL（支持后续 SSO/OIDC 时留空） |
| role | text | CHECK IN ('platform_admin','platform_ops','tenant_owner','tenant_admin','tenant_analyst','tenant_viewer') |
| is_active | bool | default true |
| last_login_at | timestamptz | NULL |
| created_at | timestamptz | NOT NULL |

索引：`UNIQUE(tenant_id, email)`；`INDEX(tenant_id, role)`。P1：`role_permission(role, perm_code)` 矩阵表；P2：审计表 `audit_log(actor_user_id, action, entity, entity_id, before, after, at)`。

**权限边界（P0 语义，必须有测试）**：租户间数据完全隔离（观测结果是全局共享的，但**监控任务/事件订阅/通知记录按 tenant_id 隔离**）；平台管理员可看全局采集健康度但默认不看客户监控清单。客户侧一律只读，不允许写观测。

### 1.2 凭证 / 账号池

**Account（Nike 侧账号 + 凭证）** —— 对应 `nike_auth.py` 的 jwt/refresh 机制
| 字段 | 类型 | 约束 |
|---|---|---|
| id | uuid | PK |
| label | text | UNIQUE，如 `mp-primary` |
| oauth_client_id | text | default `790b76033778d3e6f18743582900a4ae` |
| refresh_token_enc | bytea | NOT NULL，**加密存储**（AES-GCM，密钥走 KMS/环境变量） |
| access_token_enc | bytea | NULL |
| access_token_expires_at | timestamptz | NULL（实测 1h，剩余 <5min 需续期） |
| refresh_rotated_at | timestamptz | NULL（refresh_token 自动轮换，必须原子写回） |
| status | text | CHECK IN ('ok','refreshing','needs_reauth','cooldown','banned') |
| cooldown_until | timestamptz | NULL（401/风控冷却） |
| consecutive_failures | int | default 0 |
| owner_tenant_id | uuid | NULL = 共享池 |
| last_refresh_at | timestamptz | NULL |

索引：`INDEX(status, cooldown_until)`。**P0 并发要求**：同一 account 的 refresh 必须分布式互斥（`SELECT … FOR UPDATE SKIP LOCKED`），否则轮换会互相作废（refresh_token 单次有效）。

**QuotaUsage（配额用量）**
| 字段 | 类型 | 约束 |
|---|---|---|
| id | bigserial | PK |
| account_id | uuid | FK→Account |
| tenant_id | uuid | NULL（可归因到客户，用于公平排队） |
| metric | text | CHECK IN ('REQUESTS','REVIEW_STARTS','POLLS','STYLE_FETCHES','STORES_SCANNED','TOKEN_REFRESH') |
| window_start | timestamptz | 按分钟截断 |
| window_size | text | CHECK IN ('1m','1h','1d') |
| used / limit_ | int | limit_ 为 NULL = 无上限 |
| throttled_count / cooldown_events | int | default 0 |

索引：`UNIQUE(account_id, metric, window_start, window_size)`；`INDEX(tenant_id, window_start DESC)`。用途：多客户共用一个账号池时的**加权公平排队**与"本轮被限速"告警。

**SourceHealth（数据源健康度）**
| 字段 | 类型 | 约束 |
|---|---|---|
| id | bigserial | PK |
| source | text | CHECK IN ('REGIONS','CART_REVIEWS','STORE_DETAIL','STORE_HOME','PRODUCT_WALL','ONLINE_AVAILABILITY','TOKEN_ENDPOINT') |
| window_start | timestamptz | 分钟截断 |
| requests, ok_count, err_4xx, err_5xx | int | |
| waf_405_count, tls_eof_count, empty_completed_count, poll_timeout_count | int | 直接对应实测坑：WAF 间歇 405、TLS EOF、空完成态 |
| p50_ms, p95_ms | int | |
| success_rate | numeric(5,4) | 生成列 |
| status | text | CHECK IN ('HEALTHY','DEGRADED','DOWN')：success_rate<0.9→DEGRADED，<0.5 连续 3 窗→DOWN |
| last_error_at | timestamptz / last_error_sample_ref | NULL / FK→RawResponseArchive |

索引：`UNIQUE(source, window_start)`；`INDEX(source, window_start DESC)`。

### 1.3 门店与商品

**Store（门店）** —— 主键用 storeCode，regionId 拆别名表
| 字段 | 类型 | 约束 |
|---|---|---|
| id | uuid | PK |
| store_code | text | **NOT NULL, UNIQUE**（`3752` 形式，实测唯一） |
| canonical_region_id | text | NOT NULL，探测时默认使用的 `x-nike-region`（优先 `NIKE0S***` 形式，避开 `xian` 类别名） |
| display_name | text | NOT NULL |
| concept | text | `NVS` / `SHANGHAI_001` / …（用于筛选"换季优惠店"） |
| province / city / district | text | 由 `storeAddress` 解析，P1（用于"按城市监控"） |
| address | text | NOT NULL |
| shipping_tip | text | NULL（如 `实付满¥600免运费`） |
| activity_finished | bool | default false（`regions/v1` 原字段） |
| status | text | CHECK IN ('active','temporarily_closed','closed') |
| first_seen_at / last_seen_at | timestamptz | 门店清单轮询时更新；`last_seen_at` 变旧 → 门店下线事件 |

索引：`UNIQUE(store_code)`；`INDEX(concept, status)`；`INDEX(city, status)`；`INDEX(canonical_region_id)`。

**StoreRegionAlias（regionId 别名）**
`region_id text PK`、`store_id uuid FK NOT NULL`、`is_primary bool`、`source text default 'regions/v1'`、`first_seen_at / last_seen_at`。
唯一性：`UNIQUE(region_id)`；`UNIQUE(store_id) WHERE is_primary`（每店仅一个主 region）。
**P0 去重规则**：`regions/v1` 拉回后按 `store_code` 归并，别名只入别名表；UI 与"100 家门店"计数一律走 Store 表（否则会显示 100+ 家并重复扫描，直接翻倍 API 成本）。

**Style（货号/商品）**
| 字段 | 类型 | 约束 |
|---|---|---|
| style_color | text | PK（`DN3707-202`） |
| title | text | NOT NULL，来源 store detail fullTitle 或在线目录 |
| subtitle / category / gender | text | NULL |
| online_full_price | numeric(10,2) | NULL（在线目录原价，仅参考，非门店价） |
| image_url | text | NULL |
| is_monitored_by_any | bool | 生成/维护列，P2 优化 |
| first_seen_at / last_seen_at | timestamptz | |

索引：`INDEX(category)`；`INDEX USING gin (title gin_trgm_ops)`（模糊搜索）。

**StyleSize（款 × 尺码定义，跨店共享的标签集合）**
`id bigserial PK`、`style_color text FK`、`size_label text`（`41`/`42.5`/`S`/`3XL`）、`size_order numeric`（排序用）、`size_system text CHECK IN ('EU','ALPHA','US')`、`is_active bool`。
唯一性：`UNIQUE(style_color, size_label)`；`INDEX(style_color, size_order)`。
**注意**：同一 `size_label` 在鞋款里是 EU 码、在服饰里是 ALPHA 码，`size_system` 不能省；排序必须按 `size_order`，不能按字符串（否则 `42.5 < 44` 会排错）。

### 1.4 GTIN 与"结构按店不同"的建模（本文最关键的一节）

**问题陈述**：GTIN 是"店 × 款 × 尺码"三元组的属性，不是 SKU 的全局身份。店 3875 的 `DN3707-202` 有 26 个尺码/26 个 GTIN（前缀 `00196149…`），店 3752 只有 18 个（前缀 `00198486…`）。同一 `size_label` 在两店的 GTIN 不同、尺码集合也不同。若把 GTIN 当全局 SKU 用，会导致：跨店错误合并（假变价/假补货）、门店缺结构时被当成"无货"（**假售罄**）。

**GtinStructure（结构版本）** —— 用哈希把"结构"变成一等实体
| 字段 | 类型 | 约束 |
|---|---|---|
| id | uuid | PK |
| store_id | uuid | FK NOT NULL |
| style_color | text | FK NOT NULL |
| structure_hash | char(64) | sha256(normalize(sizes[]))，**结构指纹** |
| size_count | int | NOT NULL |
| size_labels | text[] | 有序数组（便于快速比对与展示 `sizesTotal`） |
| gtin_prefix_sample | text | 前 3 个 GTIN 前 8 位（人工排查用） |
| source_url | text | 记录取结构的完整请求（含 store_number） |
| fetched_at | timestamptz | NOT NULL |
| status | text | CHECK IN ('OK','EMPTY','ERROR')（`EMPTY` = 返回无 productGroupings） |
| raw_ref | bigint | FK→RawResponseArchive |

唯一性：`UNIQUE(store_id, style_color, fetched_at)`；`INDEX(store_id, style_color, fetched_at DESC)`；`INDEX(structure_hash)`。
**读取规则（P0）**：探测时取 `fetched_at` 最新的 OK 结构；超过 `structure_ttl`（默认 7 天，可配）则必须刷新，刷新失败时**沿用旧结构但把观测标为 `STRUCTURE_STALE`**（降置信度，绝不改状态）。

**StoreSku（店级 SKU：店 × 款 × 尺码 → GTIN）**
| 字段 | 类型 | 约束 |
|---|---|---|
| id | bigserial | PK |
| store_id | uuid | FK NOT NULL |
| style_color | text | FK NOT NULL |
| size_label | text | FK→StyleSize NOT NULL |
| gtin | text | NOT NULL（`00198486327022`，保留前导零，**必须 text 不能 bigint**） |
| structure_id | uuid | FK→GtinStructure NOT NULL（本 GTIN 出自哪版结构） |
| first_seen_at / last_seen_at | timestamptz | |
| is_active | bool | default true（结构换代后旧映射置 false，不删） |

唯一性：`UNIQUE(store_id, gtin)`（同店同 GTIN 不得映射到两个尺码）；`UNIQUE(store_id, style_color, size_label) WHERE is_active`（同店同款同尺码仅一个有效 GTIN）；`INDEX(style_color, size_label)`（全网按款×码聚合用，**唯一允许的跨店连接键**）。
**硬规则**：任何跨店查询、任何"全网"聚合，一律 `GROUP BY (style_color, size_label)` 或 `(style_color)`，**禁止 `GROUP BY gtin`**。

**StyleGtinGlobalHint（可选，P2）**：当同一 `(style_color, size_label)` 在 ≥N 家店出现同一 GTIN 时，记为全局提示（实测 `IF1823-010` 在 16 家店结构完全一致），用于**结构缺失时的降级探测**（带着"该 GTIN 在该店大概率适用"的提示去试，命中则回填结构）。命中率与回填必须打标，不得当作已验证结构。

### 1.5 观测与扫描

**ScanJob（扫描任务）**
| 字段 | 类型 | 约束 |
|---|---|---|
| id | uuid | PK |
| tenant_id | uuid | NULL（NULL=系统定时任务） |
| watch_task_id | uuid | NULL，FK→WatchTask |
| kind | text | CHECK IN ('STORE_PROBE','NATIONAL_SCAN','STRUCTURE_REFRESH','NEW_ARRIVAL_PROBE','ONLINE_WALL','STORE_HOME','PRUNE') |
| trigger | text | CHECK IN ('SCHEDULE','MANUAL','RETRY','ON_NEW_STYLE') |
| requested_by | uuid | NULL，FK→User |
| scope | jsonb | `{style_colors:[], size_labels:[], store_ids:[] or {concept,city}, pacing_ms}` |
| store_count / style_count / cell_count | int | 规模预估 |
| status | text | CHECK IN ('QUEUED','RUNNING','PARTIAL','SUCCEEDED','FAILED','CANCELLED')（**必须支持 PARTIAL**：实测全国扫描常有单店失败） |
| progress_total / progress_done / progress_failed | int | 断点续扫依据（`--skip` 的数据库化） |
| effective_region_id / account_id | text/uuid | 审计：实际用的 regionId 与账号 |
| quota_cost | int | 消耗请求数 |
| started_at / finished_at / created_at | timestamptz | |

索引：`INDEX(tenant_id, created_at DESC)`；`INDEX(status, created_at) WHERE status IN ('QUEUED','RUNNING')`（worker 抢占）；`INDEX(kind, created_at DESC)`。

**ScanJobFailure（失败明细）**
`id bigserial PK`、`scan_job_id`、`store_id`、`style_color`、`size_label NULL`、`stage text CHECK IN ('STRUCTURE','AVAILABILITY','PRICE','POLL','AUTH')`、`http_status int`、`error_code text`（枚举见 §3.6）、`retry_count int`、`raw_ref bigint`、`occurred_at`。
索引：`INDEX(scan_job_id, stage)`；`INDEX(store_id, occurred_at DESC)`；`INDEX(error_code, occurred_at DESC)`。
**P0**：`error_code` 必须能区分 `EMPTY_COMPLETED`（空完成态）与 `INVENTORY_INSUFFICIENT`（真缺货）——二者混淆就是 F3 那类假售罄的根因。

**Observation（观察记录，核心事实表）**
| 字段 | 类型 | 约束 |
|---|---|---|
| id | bigserial | PK |
| round_at | timestamptz | NOT NULL，本轮次开始时间截断到秒（**去重桶**） |
| scan_job_id | uuid | FK NOT NULL |
| store_id | uuid | FK NOT NULL |
| style_color | text | FK NOT NULL |
| size_label | text | FK NOT NULL |
| gtin | text | NULL（结构缺失时为空，且此时不得判定缺货） |
| stock_state | text | CHECK IN ('IN_STOCK','OUT_OF_STOCK','NOT_CARRIED','UNKNOWN','STRUCTURE_MISSING') |
| in_stock | bool | NULL 允许（UNKNOWN 时） |
| stock_method | text | CHECK IN ('EXCLUSION','EXPLICIT')（排除法：未进 error.errors 即视为有货 / 明细法：fulfillmentGroups 明确返回） |
| price | numeric(10,2) | NULL |
| discount_amount | numeric(10,2) | NULL |
| total_price | numeric(10,2) | NULL（**实付**，价格比较主口径） |
| currency | char(3) | default 'CNY' |
| promo_names | text[] | 默认 `{}`（`promotionDiscounts[].displayName`） |
| price_confidence | text | CHECK IN ('EXACT','PARTIAL','STALE','NONE')（EXACT=≤3 件全可买批次直读） |
| price_is_stale | bool | 无货时沿用上次可信价 → true |
| trust | text | CHECK IN ('TRUSTED','SUSPECT','UNTRUSTED') |
| untrusted_reason | text | NULL / 'EMPTY_COMPLETED' / 'STRUCTURE_MISSING' / 'HTTP_405_WAF' / 'HTTP_401' / 'TLS_EOF' / 'POLL_TIMEOUT' / 'PARSE_ERROR' / 'QUANTITY_LIMIT_MASKED' |
| structure_id | uuid | FK NULL，本观测依据的结构版本 |
| structure_age_sec | int | 结构新鲜度（陈旧 → 降置信） |
| gap_from_prev_sec | int | 与上一次可信观测的间隔（判断基线是否可信） |
| http_status | int / latency_ms int | |
| raw_ref | bigint | FK→RawResponseArchive（**假阳性/假阴性追溯锚点**） |
| observed_at | timestamptz | NOT NULL，实际取到数据的时间 |

唯一性（**两侧都要**）：
- 轮内去重：`UNIQUE(scan_job_id, store_id, style_color, size_label)` —— 防止 Phase A/Phase B 两次审核各写一行（实测同轮会对同一尺码提交两次）。
- 跨轮去重（题目要求口径）：`UNIQUE(store_id, gtin, round_at) WHERE gtin IS NOT NULL`。
- 落库策略：**变更即写 + 每日全量快照**（P0，见 §4 容量）。未变化的尺码不写新行，仅更新 `observation_latest` 并把 `last_verified_at` 前移。

索引建议：
`INDEX(store_id, style_color, observed_at DESC) INCLUDE (total_price, price, discount_amount, in_stock)` —— 价格曲线主索引；
`INDEX(style_color, observed_at DESC)`；
`INDEX(style_color, size_label, observed_at DESC) WHERE in_stock` —— 全网有货查询；
`INDEX BRIN(observed_at)` —— 冷数据大范围扫描；
`INDEX(scan_job_id)`；
`INDEX(trust, observed_at DESC) WHERE trust <> 'TRUSTED'` —— DQ 报表专用部分索引。

**ObservationLatest（最新状态物化表，P0 性能件）**
`store_id, style_color, size_label` 为 PK；字段：`gtin, stock_state, in_stock, total_price, price, discount_amount, promo_names, price_is_stale, trust, round_at, last_verified_at, structure_hash_used`。
索引：`INDEX(style_color, in_stock) WHERE in_stock`；`INDEX(store_id, in_stock) WHERE in_stock`；`INDEX(style_color, size_label) WHERE in_stock`。
理由：观测明细按 §4 估算达百万行/天，"全网有货门店快照"若扫历史表无法在 UI SLA 内返回。

**RawResponseArchive（原始响应留档）**
`id bigserial PK`、`source text`、`request_id uuid`（`cart_reviews/v2/{uuid}` 的 uuid，**追溯主键**）、`store_id`、`style_color`、`request_fingerprint char(64)`（请求体规范化哈希，识别重复请求）、`http_status int`、`body_snippet text`（压缩+截断，默认 ≤32KB）、`body_hash char(64)`、`trust_verdict text`、`error_codes text[]`、`created_at / expires_at timestamptz`。
索引：`INDEX(request_id)`；`INDEX(body_hash)`（同源响应去重）；`INDEX(expires_at)`（清理任务）。
**P0 隐私/合规**：只存响应体，**绝不存 Authorization/refresh_token**；仓库层拦截器强制脱敏。

### 1.6 监控与告警

**WatchTask（监控任务）**
| 字段 | 类型 | 约束 |
|---|---|---|
| id | uuid | PK |
| tenant_id | uuid | FK NOT NULL |
| name | text | NOT NULL |
| enabled | bool | default true |
| store_scope_mode | text | CHECK IN ('ALL','CITY','CONCEPT','EXPLICIT') |
| store_filter | jsonb | `{cities:[], concepts:['NVS'], store_ids:[]}` |
| style_filter | jsonb | `{style_colors:[], categories:[], from_online_new:bool}` |
| size_labels | text[] | 空 = 全部尺码（与结构取交集） |
| cadence_sec | int | default 3600，CHECK ≥300（低于则触发风控） |
| priority | smallint | 1..9，参与账号池排队 |
| notify_on | text[] | 事件类型白名单，默认 `{RESTOCK,SOLD_OUT,PROMO_START,NEW_ARRIVAL_AT_STORE}` |
| quiet_hours | jsonb | `{tz:'Asia/Shanghai', windows:[['23:00','08:00']]}` |
| aggregation | jsonb | `{window_sec:300, group_by:'STORE', max_events_per_msg:20}` |
| cooldown_sec | int | default 1800 |
| max_alerts_per_day | int | default 50，超出折叠 |
| price_alert | jsonb | `{below_total:null, discount_pct_gte:null, min_delta_abs:1, min_delta_pct:1.0}` |
| created_by / created_at / updated_at | | |
| last_run_at / next_run_at / last_status | | |

唯一性：`UNIQUE(tenant_id, name)`；`INDEX(enabled, next_run_at)`（调度扫描）。

**MonitorTarget（监控目标：款 × 尺码范围 × 门店范围）**
| 字段 | 类型 | 约束 |
|---|---|---|
| id | bigserial | PK |
| watch_task_id | uuid | FK NOT NULL |
| tenant_id | uuid | **冗余列**（为跨任务唯一性约束服务） |
| style_color | text | FK NOT NULL |
| size_labels | text[] | 空 = 全部 |
| store_scope_hash | char(64) | sha256(规范化后的门店集合 + size_labels)，门店集合解析后固化 |
| store_ids | uuid[] | 解析结果快照（门店清单变化时重建 hash） |
| added_at / expires_at | timestamptz | |
| is_active | bool | default true |

唯一性（**题目要求的 `(tenant, style)` 监控唯一**）：`UNIQUE(tenant_id, style_color, store_scope_hash) WHERE is_active`
→ 同一租户、同款、同门店范围只允许一条有效监控目标，重复提交走幂等 upsert（返回已存在 id，不重复计费、不重复探测）。
`INDEX(style_color)`；`INDEX(tenant_id)`。
**P0 成本收敛**：调度器以 `(store_id, style_color)` 为**探针单元**全局去重（`ProbeUnit` 视图/表），一轮内同一单元只发一次请求，观测结果扇出到所有订阅租户。

**ChangeEvent（变更事件）**
| 字段 | 类型 | 约束 |
|---|---|---|
| id | bigserial | PK |
| event_type | text | CHECK IN ('RESTOCK','SOLD_OUT','PRICE_CHANGE','PROMO_START','PROMO_END','NEW_ARRIVAL_AT_STORE','SCAN_FAILED','STRUCTURE_CHANGED','CATALOG_CHANGED') |
| subtype | text | NULL / 'BASE_PRICE' / 'PROMO_CHANGE' / 'PROMO_SWITCH' / 'BELOW_THRESHOLD' |
| store_id | uuid | FK NOT NULL |
| style_color | text | FK NOT NULL |
| size_scope | text | 具体 `size_label`，或 `'*'` = 款/店级聚合事件 |
| size_labels | text[] | 聚合事件的明细尺码集 |
| before_value / after_value | jsonb | `{stock_state, in_stock, price, discount, total, promos[]}` |
| delta_total | numeric(10,2) | 生成列（价格类事件） |
| delta_pct | numeric(6,2) | 生成列 |
| prev_observation_id / curr_observation_id | bigint | FK NULL（可回溯到原始观测） |
| gtin | text | NULL（记录当时使用的 GTIN，便于人查） |
| structure_hash_before / _after | char(64) | NULL（结构漂移判定） |
| fingerprint | char(64) | NOT NULL, **UNIQUE**（见 §2.3） |
| confidence | text | CHECK IN ('HIGH','MEDIUM','LOW') |
| severity | text | CHECK IN ('INFO','WARN','CRITICAL') |
| detected_at / effective_from | timestamptz | detected_at = 首个检出时刻；effective_from = 依据观测的 round_at |
| first_seen_at / last_seen_at | timestamptz | 去重窗口内的合并区间 |
| merged_count | int | default 1（被合并的重复次数） |
| is_suppressed / suppress_reason | bool / text | NULL / 'QUIET_HOURS' / 'JITTER_MERGE' / 'DEDUPE_WINDOW' / 'COOLDOWN' / 'BELOW_THRESHOLD' / 'UNTRUSTED_SOURCE' / 'DAILY_CAP' |
| tenant_id | uuid | NULL（全局事件）；租户专属规则生成的事件带 tenant_id |

索引：`UNIQUE(fingerprint)`；`INDEX(store_id, event_type, detected_at DESC)`；`INDEX(style_color, detected_at DESC)`；`INDEX(event_type, detected_at DESC)`；部分索引 `INDEX(store_id, detected_at DESC) WHERE event_type='RESTOCK'`（近 7 天补货流水）；`INDEX(tenant_id, detected_at DESC) WHERE tenant_id IS NOT NULL`。

**NotificationRule（告警规则）**
`id uuid PK`、`tenant_id uuid NULL`（NULL=系统默认模板）、`watch_task_id NULL`、`name`、`enabled bool`、`event_types text[]`、`scope jsonb`（store/style/city/concept 过滤）、`conditions jsonb`（如 `{"min_sizes_restocked":2,"discount_pct_gte":30,"only_first_time":true}`）、`channels text[]`、`quiet_hours jsonb`、`aggregation jsonb`、`cooldown_sec int`、`max_per_day int`、`priority smallint`、`created_at/updated_at`。
索引：`INDEX(tenant_id, enabled)`。

**Notification（通知记录）**
| 字段 | 类型 | 约束 |
|---|---|---|
| id | uuid | PK |
| tenant_id | uuid | FK NOT NULL |
| rule_id | uuid | FK NULL |
| channel | text | CHECK IN ('EMAIL','WEBHOOK','WECHAT_WORK','FEISHU','DINGTALK','IN_APP') |
| target | text | 收件地址/机器人 URL（URL 需加密或脱敏存储） |
| digest_window_start / end | timestamptz | 聚合窗口区间 |
| event_count | int | |
| payload_rendered | text | 实际发出的正文（模板渲染结果） |
| payload_hash | char(64) | 去重 |
| status | text | CHECK IN ('PENDING','SENT','FAILED','SUPPRESSED','DEAD_LETTER') |
| attempts / next_retry_at | int / timestamptz | 指数退避，默认 5 次 |
| provider_message_id / error | text | |
| sent_at / delivered_at / created_at | timestamptz | |

索引：`INDEX(tenant_id, created_at DESC)`；`UNIQUE(tenant_id, channel, payload_hash, digest_window_start)`（**同一窗口同内容不重复发送**）；`INDEX(status, next_retry_at) WHERE status IN ('PENDING','FAILED')`。
关联表 `notification_event(notification_id, event_id)`，PK 复合，`INDEX(event_id)`。

**OnlineListing（在线目录快照，P1）** —— 支撑"上新"与线上/门店价差核对
`id bigserial PK`、`style_color`、`channel text`（`discover/product_wall` 的 consumerChannelId）、`price / full_price / discount numeric(10,2)`、`url text`、`is_new bool`、`snapshot_at timestamptz`。
唯一性：`UNIQUE(style_color, channel, snapshot_at)`；`INDEX(style_color, snapshot_at DESC)`。

---

## 2. 关系与唯一性约束汇总

### 2.1 关系图（文字版）
```
Tenant 1─n User
Tenant 1─n WatchTask 1─n MonitorTarget ─(style_color)─> Style
Tenant 1─n NotificationRule
WatchTask 1─n ScanJob 1─n ScanJobFailure
ScanJob 1─n Observation n─1 Store
Store 1─n StoreRegionAlias
Store 1─n StoreSku n─1 Style ; Style 1─n StyleSize ; StoreSku n─1 GtinStructure
Observation n─1 GtinStructure (structure_id)
Observation 1─n ChangeEvent (prev/curr)
ChangeEvent n─m Notification (via notification_event)
Account 1─n QuotaUsage ; Account 1─n ScanJob
SourceHealth 1─n RawResponseArchive (sample) ; Observation/ScanJobFailure n─1 RawResponseArchive
```

### 2.2 唯一性/完整性约束清单（可直接落成 DDL 校验项）
| 约束 | 作用 |
|---|---|
| `UQ(store_code)`、`UQ(region_id)` @ StoreRegionAlias | 150 条清单归并成 100 家（F1 别名事实） |
| `UQ(store_id, gtin)` @ StoreSku | 同店同 GTIN 只能一个尺码映射 |
| `UQ(store_id, style_color, size_label) WHERE is_active` @ StoreSku | 同店同款同码唯一有效 GTIN（结构换代留历史） |
| `UQ(store_id, style_color, fetched_at)` @ GtinStructure | 结构快照可追溯 |
| `UQ(scan_job_id, store_id, style_color, size_label)` @ Observation | 轮内不重复（Phase A/B 双写） |
| `UQ(store_id, gtin, round_at) WHERE gtin IS NOT NULL` @ Observation | 跨轮去重（题目口径） |
| `UQ(tenant_id, style_color, store_scope_hash) WHERE is_active` @ MonitorTarget | **(tenant, style) 监控唯一**，幂等，防重复计费 |
| `UQ(tenant_id, name)` @ WatchTask | 同名任务不可重 |
| `UQ(fingerprint)` @ ChangeEvent | **事件去重核心** |
| `UQ(tenant_id, channel, payload_hash, digest_window_start)` @ Notification | 同一聚合窗口同内容不重发 |
| `UQ(account_id, metric, window_start, window_size)` @ QuotaUsage | 配额计量幂等 |
| `CHECK (NOT (in_stock = true AND stock_state = 'UNKNOWN'))` @ Observation | 状态自洽 |
| `CHECK (total_price IS NULL OR total_price <= price)` @ Observation | 实付不得高于标价（防解析错位） |
| `CHECK (event_type <> 'SOLD_OUT' OR before_value->>'in_stock' = 'true')` @ ChangeEvent | 售罄事件必须有在库前值 |
| `CHECK (size_scope <> '*' OR size_labels IS NOT NULL)` @ ChangeEvent | 聚合事件必须带明细尺码集 |

### 2.3 事件去重指纹算法（P0，防重复提醒的核心）
```
fingerprint = sha256_hex( join("|", [
  "v1",
  event_type,                       # RESTOCK / SOLD_OUT / PRICE_CHANGE / ...
  subtype 或 "-",                    # BASE_PRICE / PROMO_CHANGE / ...
  store_id,                         # 门店级隔离（门店不同 = 不同事件）
  style_color,
  coalesce(size_scope, "*"),        # 尺码级事件用具体 size_label
  normalize(before_value),          # 键排序 + 数值定点化(2位小数) + promos 排序去重
  normalize(after_value),
  time_bucket(detected_at)          # 见下表
]))
```
| 事件类型 | `time_bucket` | 理由 |
|---|---|---|
| RESTOCK / SOLD_OUT | 按天（`YYYY-MM-DD`），**但同一尺码在合并窗口内只算一次** | 一天内多次抖动不该重复提醒；抖动由 `JITTER_MERGE` 处理 |
| PRICE_CHANGE / PROMO_START / PROMO_END | 按天 + `delta_total` 归一（四舍五入到 0.01） | 同一天涨回原价再降 → 视为同一事件 |
| NEW_ARRIVAL_AT_STORE | 不设桶（永久唯一） | 一款到店只提醒一次，除非售罄后再次到店（那时 before_value 已不同） |
| SCAN_FAILED | 按小时 + `error_code` | 故障是运维事件，按小时聚 |
| STRUCTURE_CHANGED | 用 `structure_hash_before+after` 替代桶 | 结构变化本身就是唯一键 |

**去重与合并执行规则（按顺序判定）**：
1. 计算 `fingerprint`，若在 `dedupe_window_sec`（默认 6h，可配）内已存在同 fingerprint 事件 → **不新建、不发通知**；把既有事件 `merged_count += 1`、`last_seen_at = now()`。
2. 超出 dedupe 窗口后同 fingerprint 再次出现 → 视为**新事件**（当天第二波补货应当再提醒），新行 fingerprint 加 `#2` 后缀并写 `supersedes_event_id`。
3. 抖动合并：同 `(store, size_label, style_color)` 在 `jitter_window_sec`（默认 1800s）内出现 **RESTOCK→SOLD_OUT**（或反向）→ 两条都置 `is_suppressed=true, suppress_reason='JITTER_MERGE'`，另生成一条 `CATALOG_CHANGED`/`INFO` 的"库存抖动"摘要（`merged_count` = 抖动次数）。
4. 只有 `trust='TRUSTED'` 的观测才参与指纹计算；`SUSPECT/UNTRUSTED` 一律走 SCAN_FAILED 分支（**这是 F3 假售罄的防线**）。

---

## 3. 变更检测规则引擎需求

### 3.0 状态机前置（P0）
判定任何事件前，先把 `(store, style_color, size_label)` 归入状态：
`IN_STOCK` / `OUT_OF_STOCK`（在售但该码无货，`INVENTORY_INSUFFICIENT`）/ `NOT_CARRIED`（该店不卖，`PRODUCT_NOT_BUYABLE`）/ `UNKNOWN`（响应不可信）/ `STRUCTURE_MISSING`（取不到 GTIN 结构）。
**规则**：`UNKNOWN`、`NOT_CARRIED`、`STRUCTURE_MISSING` **永不参与**补货/售罄判定；`NOT_CARRIED → IN_STOCK` 只能产出 `NEW_ARRIVAL_AT_STORE`，不能产出 `RESTOCK`。
**基线有效性**：`prev` 观测需满足 `gap_from_prev_sec ≤ max_gap_sec`（默认 7 天，可配）。超期视为**基线失效**，本轮只重建基线并生成 `INFO/CATALOG_CHANGED` 事件（confidence=LOW），不发补货/售罄提醒。

### 3.1 RESTOCK（补货）
- 触发：`prev.stock_state='OUT_OF_STOCK'` ∧ `curr.stock_state='IN_STOCK'`，且 `prev.trust=TRUSTED` ∧ `curr.trust=TRUSTED`。
- 粒度：**尺码级**（`size_scope=size_label`）。
- 升格为款/店级：同一 `(store, style_color)` 在 `aggregation_window_sec`（默认 300s）内 ≥ `min_sizes_restocked`（默认 2）个尺码补货 → 合并为一条 `size_scope='*'` 的补货事件，`size_labels` 列出明细。
- 结构一致性：`structure_hash_before != structure_hash_after` → confidence 降为 LOW，并在正文标注"（门店尺码结构有变化）"。**实测教训**：店 3875 的 `DN3707-202` 从 18 码变 26 码时，17 个"新尺码"会同时触发补货 → 必须靠这条规则压成一条结构变更说明，而不是刷屏 17 条。
- 抑制：静默期（`QUIET_HOURS`）；同 `(store, style, size)` 在 `cooldown_sec`（默认 1800s）内已发过补货；抖动合并。
- 首轮特例：`prev` 不存在（新建监控目标）→ **不发补货**，只发一条"已开始监控：当前 X/Y 码有货"（与 `nike_store_probe.py:diff_and_save` 的 `＋` 分支一致）。

### 3.2 SOLD_OUT（售罄）
- 触发：`prev.stock_state='IN_STOCK'` ∧ `curr.stock_state='OUT_OF_STOCK'`，两侧均 TRUSTED，且 `gap_from_prev_sec ≤ max_gap_sec`。
- **绝不触发**：`curr.untrusted_reason ∈ {EMPTY_COMPLETED, HTTP_405_WAF, HTTP_401, TLS_EOF, POLL_TIMEOUT, STRUCTURE_MISSING}`；`curr.stock_state='NOT_CARRIED'`；结构缺失；`gtin IS NULL`。
- `curr.stock_state='NOT_CARRIED'` 而 `prev='IN_STOCK'` → 生成 `CATALOG_CHANGED`（下架/不可购）而非售罄，severity=WARN，需人工复核标记位。
- 粒度与聚合同补货；抖动合并同 §2.3 规则 3。

### 3.3 PRICE_CHANGE（变价，含促销开始/结束）
**比较口径（P0 决策，需客户确认，见 §7-Q1）**
| 口径 | 定义 | 用途 |
|---|---|---|
| `price` 标价 | `itemCosts.priceInfo.price` | 吊牌价变动（如 ¥1399 → ¥1099） |
| `discount` 促销额 | `priceInfo.discount` | 促销力度 |
| `total` 实付 | `priceInfo.total` | **主口径**：客户真正付的钱 |
| `promo_names` | `promotionDiscounts[].displayName` | 促销活动名（如"指定鞋/服/配件 55折"） |

**判定规则**
1. 主比较用 `total`（实付）。`price` 或 `discount` 变化但 `total` 不变 → 记 `subtype='PROMO_CHANGE'`，severity=INFO，**默认抑制**（`suppress_reason='BELOW_THRESHOLD'`）。
2. `discount` 从 `0`/NULL 变为 `>0`（或 `total < price`）→ `PROMO_START`（severity=WARN，含促销名与折扣率）。
3. `discount` 从 `>0` 变为 `0`（或 `total == price`）→ `PROMO_END`（severity=INFO）。
4. **促销名变化算不算变价**：仅促销名变化而 `total`/`price` 均不变 → **不告警**（记 INFO 事件，默认 `is_suppressed=true`）。理由：实测 `promos` 是活动文案（"满减专区"轮换），改名不代表价格变化，误报会迅速消耗客户信任。租户可用 `notify_on_promo_name_change=true` 打开（P2）。
5. 阈值抑制：`|Δtotal| < max(min_delta_abs(默认1元), min_delta_pct(默认1%)×prev.total)` → 记事件但抑制（`BELOW_THRESHOLD`）。
6. `price_confidence` 门槛：任一侧 `price_confidence ∈ {PARTIAL, NONE}` 或 `price_is_stale=true` → **不产生价格事件**，改发 `SCAN_FAILED(stage='PRICE')` 供运维；`NULL → 数值` 的跃变**不得**当作变价（无货时价格本就缺失，F2）。
7. 门店隔离：价格按店取，**禁止跨店比较生成"变价"**；跨店同款价差仅作为"门店价格对比"查询能力（P1），不产生事件。
8. 聚合：同一 `(store, style_color)` 内所有有货尺码在同一轮次 `total` 同向同幅变化 → 合并为 `size_scope='*'` 一条；否则按尺码分行（≤ 阈值时再合并为"N 个尺码变价"摘要）。

### 3.4 NEW_ARRIVAL_AT_STORE（上新到店）
- 触发（两种路径）：
  a) `(store, style)` 历史状态为 `NOT_CARRIED` 或 `STRUCTURE_MISSING`（此前该店不卖此款），首次出现 `IN_STOCK`；
  b) 该款在**所有**门店均无任何在库记录，某店首次出现 `IN_STOCK`（全网首店到货）。
- 粒度：**款/店级**（`size_scope='*'`，`size_labels` = 到货尺码集）；同款多店在 `aggregation_window` 内到货 → 合并为"N 家门店上新到店"一条（最多列 10 家，其余折叠计数）。
- 数据来源限定：必须来自门店探针（STORE_PROBE/NATIONAL_SCAN）的可信观测；**在线新品墙（`discover/product_wall`）只能生成 `CATALOG_CHANGED`（在线上新），不得冒充"到店"**。二者可关联（先在线新品 → 后到店），但语义分开。
- 与 RESTOCK 互斥：`NOT_CARRIED → IN_STOCK` 只发 NEW_ARRIVAL_AT_STORE。

### 3.5 上新/目录类事件（P1）
`CATALOG_CHANGED`：在线新品墙或 `store/home` 预览池出现新 `style_color`（对应 `nike_new_arrivals.py` 的 diff 与 `state.json` 的 promotion nodes）；severity=INFO；可作为"触发探测"信号（`trigger='ON_NEW_STYLE'`）自动为订阅租户建探针任务，但**入队前必须过配额检查**。

### 3.6 SCAN_FAILED（扫描失败）
- 粒度：`(store_id, style_color, stage)` 或 `(source, error_code)` 级聚合，**绝不映射到库存状态**。
- `error_code` 枚举（与实测坑一一对应）：
| error_code | 触发 | 证据 |
|---|---|---|
| `EMPTY_COMPLETED` | 响应无 `error` 也无 `fulfillmentGroups`，轮询用尽 | `nike_store_probe.py:116-118`（空完成态注释："不可信，继续轮询，轮满按失败处理"） |
| `STRUCTURE_MISSING` | store detail 无 `productGroupings`/`products`/`sizes` | `ensure_gtins()` 返回 None 分支 |
| `HTTP_405_WAF` | 405 且 body 含 `data-spm`，重试仍失败 | `req()` 重试逻辑 |
| `HTTP_401` | 续期后仍 401（账号冷却） | `load_jwt(force=True)` 后仍失败 |
| `TLS_EOF` | `httpx.TransportError` 重试耗尽 | `req()` net 分支 |
| `POLL_TIMEOUT` | PUT 202 后 12 次轮询未得终态 | `POLL_TRIES=12` |
| `PARSE_ERROR` | JSON 解析失败/字段缺失 |
| `QUANTITY_LIMIT_MASKED` | 批次命中 `ITEM_QUANTITY_LIMIT`，可用性结论不可信 | `review()` 第 124–127 行：该错误不算缺货，但会**遮蔽**同批其他项结论 |
| `PARTIAL_PRICE` | 有货但取不到价格（混入失效项/超 3 件） | Phase B 迭代收缩 4 次仍失败 |

- 通知抑制（防告警风暴）：仅当满足其一才通知 —— 单轮失败率 > `failure_rate_threshold`（默认 20%）；同一 `(store, style)` 连续失败 ≥ `consecutive_failures`（默认 3）；`SourceHealth.status` 变为 DOWN；账号 `status='needs_reauth'`（这是唯一需要人工的 P0 事件）。
- `SCAN_FAILED` 事件必须携带 `affected_cells` 估算（本轮有多少 `(store,style,size)` 未更新），并在客户报表中显示"本轮置信度"。

### 3.7 通知聚合策略（P0，防轰炸）
| 层级 | 规则 |
|---|---|
| 单条消息内 | 按 `group_by`（默认 `STORE`：一店一条）聚合；同类事件（同 event_type）合并；`max_events_per_msg`（默认 20）上限，超出写"…另有 N 条，详见链接" |
| 跨店同款 | 同 `(style, event_type)` 在 `aggregation.window_sec`（默认 300s）内 ≥3 店 → 合并为一条"多店汇总"，按门店优先级（客户在 `WatchTask` 里标记的优先级/城市）排序，正文列前 10 家 + 计数 |
| 跨事件类型 | 同店同款的补货+变价同时发生 → 合并为一条"门店动态"，分 block 展示 |
| 时间窗 | 所有事件先入 `pending_digest`，窗口结束统一渲染发送；静默时段内的事件**顺延不丢弃**，窗口结束/次日 08:00 汇总一条 |
| 冷却 | 同 `(store, style, event_type)` 在 `cooldown_sec`（默认 1800s）内不二次发送 |
| 日上限 | `max_alerts_per_day`（默认 / 租户，可配）用尽后折叠为一条"今日告警已折叠，共 N 条" |
| 幂等 | 发送前再次核对 `payload_hash + digest_window`，已发过则跳过（防止 worker 重试重复投递） |
| 失败重试 | 指数退避 1m/5m/30m/2h/6h，5 次后 `DEAD_LETTER` 并生成一条运维告警 |

---

## 4. 历史保留与查询需求

### 4.1 保留期（默认，可按租户覆盖）
| 表 | 热存 | 冷存/汇总 | 说明 |
|---|---|---|---|
| Observation（明细） | 90 天 | `ObservationDaily` 汇总 **24 个月** | 汇总字段：`min/max/avg total`、`in_stock_days`、`last_in_stock_at`、`promo_names` 集合、`out_of_stock_days` |
| ObservationLatest | 永久（仅当前状态） | — | 每次扫描 upsert，支撑秒级快照查询 |
| ChangeEvent | 24 个月 | 之后仅保留聚合计数 | 客户对账/举证需要 |
| Notification | 元数据 12 个月，正文 90 天 | — | 正文含价格快照，压缩归档 |
| ScanJob | 12 个月 | — | |
| ScanJobFailure | 180 天 | — | |
| RawResponseArchive | **14 天**（可配 ≤30 天） | — | 假阳性/假阴性追溯窗口；含明文响应必须短期 |
| SourceHealth | 小时级 90 天 + 天级 13 个月 | — | |
| GtinStructure | 永久（含历史版本） | — | 结构换代审计必需 |
| StoreSku | 永久（`is_active` 软失效） | — | |
| QuotaUsage | 分钟级 7 天 / 小时级 90 天 / 天级 13 个月 | — | |

**容量估算与写放大控制（P0）**：100 店 × 30 款 × 20 码 ≈ **6 万格/轮**；小时级 → 144 万格/天 ≈ **5.2 亿/年**，全量入库不可接受。
落库策略：
1. **变更即写**：`stock_state` 或价格三元组或 `promo_names` 任一变化才写 Observation 新行。
2. **每日 1 次全量快照**（同日同轮次 `round_at` 固定为 `00:00` 或任务起始时刻），保证"某天某店某码有货但一天无变化"仍可举证。
3. 其余轮次仅更新 `ObservationLatest.last_verified_at`（行数 = 6 万，恒定）。
预期年写入量：变更 ~1–3% → ~200 万行/年 + 每日快照 2190 万行/年 → 约 2400 万行/年，单表可承受（按月分区 + `observed_at` 分区裁剪）。
**分区建议 P1**：Observation 按 `RANGE(observed_at)` 月分区；`ChangeEvent` 按 `detected_at` 月分区；`RawResponseArchive` 按 `created_at` 周分区（便于 `DROP PARTITION` 清理）。

### 4.2 典型查询与索引
| 查询 | SQL 形态（要点） | 索引/物化 |
|---|---|---|
| Q1 某货号某店的价格曲线 | `SELECT observed_at, price, discount_amount, total_price, promo_names FROM observation WHERE style_color=$1 AND store_id=$2 AND trust='TRUSTED' AND observed_at BETWEEN $3 AND $4 ORDER BY observed_at` | `INDEX(store_id, style_color, observed_at DESC) INCLUDE (price, discount_amount, total_price)`；建议再建 `mv_price_daily(style_color, store_id, day, min_total, max_total, last_total)` |
| Q2 某店近 7 天补货流水 | `SELECT detected_at, style_color, size_labels FROM change_event WHERE store_id=$1 AND event_type='RESTOCK' AND detected_at > now()-interval '7 days' AND NOT is_suppressed ORDER BY detected_at DESC` | 部分索引 `(store_id, detected_at DESC) WHERE event_type='RESTOCK'` |
| Q3 某货号全网有货门店快照（**最热**，UI 首屏） | `SELECT store_id, array_agg(size_label ORDER BY size_order) FROM observation_latest ol JOIN style_size ss USING(style_color, size_label) WHERE ol.style_color=$1 AND ol.in_stock AND ol.trust='TRUSTED' GROUP BY store_id` | `INDEX(style_color, in_stock) WHERE in_stock` @ ObservationLatest（**禁止扫 Observation 历史表**） |
| Q4 某货号某尺码在哪些店有货 | 同上加 `AND size_label=$2` | `INDEX(style_color, size_label, in_stock) WHERE in_stock` |
| Q5 某店当前全部在库（"店里有什么货"替代方案） | `SELECT style_color, count(*) FILTER (WHERE in_stock) FROM observation_latest WHERE store_id=$1 GROUP BY 1 HAVING bool_or(in_stock)` | `INDEX(store_id, in_stock) WHERE in_stock`；**注意语义**：只能显示"被监控货号"，非全量清单（`00-project-context.md §2.5` 已证伪全量可达） |
| Q6 门店价格对比（P1） | 按 `(style_color, size_label)` 横向比 `total_price`，**不聚合 GTIN** | `ObservationLatest(style_color, size_label, total_price)` |
| Q7 数据质量报表 | `SELECT date_trunc('hour',observed_at), untrusted_reason, count(*) FROM observation WHERE trust<>'TRUSTED' AND observed_at>now()-interval '7 days' GROUP BY 1,2` | 部分索引 `(trust, observed_at DESC) WHERE trust<>'TRUSTED'` |
| Q8 未送达通知 | `SELECT * FROM notification WHERE status IN ('FAILED','PENDING') AND next_retry_at<now()` | `INDEX(status, next_retry_at)` |
| Q9 结构漂移审计 | `SELECT store_id, size_count, fetched_at FROM gtin_structure WHERE style_color=$1 ORDER BY store_id, fetched_at DESC` | `INDEX(store_id, style_color, fetched_at DESC)` |

**P0 SLA 建议**：Q1/Q2/Q3/Q4 P95 < 800ms（走 ObservationLatest + 月分区）；Q7 允许 5s 级（后台报表）；UI 首屏一律走 `ObservationLatest`，历史走曲线接口（预聚合到天）。

---

## 5. 数据质量规则

### 5.1 可信度判定（每次观测落库前计算，P0）
`trust` 判定顺序（任一失败即降级）：
1. HTTP 200 且 JSON 可解析，否则 `UNTRUSTED(PARSE_ERROR/http_* )`。
2. **空完成态**：无 `error` 且 `fulfillmentGroups` 为空 → 先重试轮询；用尽仍为空 → `UNTRUSTED(EMPTY_COMPLETED)`，**不更新任何库存状态**。
3. 可用性结论必须二选一有据：要么有 `error.errors[]`（逐项 `field=/items/{i}` 定位不可买项），要么有 `fulfillmentGroups[].items[]` 明细。两者皆无 → `UNTRUSTED`。
4. 批次合规：可用性批次 ≤40 件；**价格批次必须 ≤3 件**。价格批次 >3 件 → 价格字段不可信（F2），`price_confidence='NONE'`。
5. `ITEM_QUANTITY_LIMIT` 出现在可用性批次 → 整批 `SUSPECT(QUANTITY_LIMIT_MASKED)`，该批结论不入状态机、触发即时重跑（小批）。
6. 结构有效性：`gtin IS NOT NULL` ∧ `structure.status='OK'` ∧ `structure_age_sec ≤ structure_ttl`；结构陈旧/缺失 → `trust ≤ SUSPECT`，`stock_state` 只能是 `UNKNOWN`/`STRUCTURE_MISSING`。
7. 基线有效性：`gap_from_prev_sec > max_gap_sec` → 本轮为"重建基线"轮，禁止产出补货/售罄。

### 5.2 置信度（写进报表与通知正文）
`confidence = f(source, method, trust, structure_age, gap, price_confidence)`，映射为 `HIGH/MEDIUM/LOW`：
| 级别 | 条件 | UI 呈现 |
|---|---|---|
| HIGH | trust=TRUSTED ∧ structure_age ≤ 24h ∧ gap ≤ 2×cadence ∧ (价格事件: price_confidence='EXACT') | 绿色"已核验" |
| MEDIUM | trust=TRUSTED ∧ (structure_age ≤ 7d ∨ gap ≤ 7d) ∨ 结构由全局提示回填 | 黄色"参考" |
| LOW | trust=SUSPECT ∨ 结构漂移过渡轮 ∨ 基线重建轮 ∨ 价格 stale | 灰色"待确认"，**且不进通知主渠道**（只进日报） |

**P0**：`confidence` 必须持久化在 Observation 与 ChangeEvent 上（不用时计算），这样客户点开一条提醒能立刻看到"这条有多可信"。

### 5.3 假阳性/假阴性可追溯（P0）
- 每条 Observation 带 `raw_ref` → RawResponseArchive；每条 ChangeEvent 带 `prev/curr_observation_id`。**给定任意一条提醒，能回放两侧原始响应片段**。
- `RawResponseArchive` 存 `request_id`（`cart_reviews/v2/{uuid}` 的 uuid，与实测一致）、`error_codes text[]`、`body_hash`。同一 `body_hash` 反复出现 → 说明请求被缓存/异常，进 DQ 报表。
- **假阳性（误报补货/售罄）** 归因标签（落 `dq_incident` 表，P1）：`EMPTY_COMPLETED_MISREAD`、`STRUCTURE_MISSING_MISREAD`、`QUANTITY_LIMIT_MASKED`、`STALE_BASELINE`、`STRUCTURE_DRIFT`、`PHASE_B_PARTIAL_PRICE`。
- **假阴性（漏报）** 归因：`PROBE_NOT_SCHEDULED`（配额/限速跳过）、`REGION_ALIAS_MISS`（用错 regionId → 门店漏扫）、`GTIN_MISS`（结构缺失未用全局提示回填）、`BATCH_ORDER`（前一项失败导致后项未提交）。
- `dq_incident` 字段：`id, kind(FALSE_POSITIVE/FALSE_NEGATIVE), event_id NULL, observation_id NULL, reason_code, detected_by(USER_REPORT/AUTO_CROSSCHECK), status(OPEN/CONFIRMED/FIXED), reporter_user_id, note, created_at`。客户可对单条提醒点"报错"，直接落库（这是验收期最重要的反馈闭环）。
- **自动交叉核验（P1）**：一轮内若 `(store, style)` 出现"同一尺码同时有 EXCLUSION 结论与 EXPLICIT 结论不一致"，或多轮结果翻转率 > `flip_rate_threshold`（默认 30%），自动开 `FALSE_POSITIVE`/`FLAPPING` 事件并抑制通知。
  - 实测依据 F3：`scan_0924_0716.json`（店 3460 `IF1823-010` 6 码全有货）与 `probe_state_3460.json`（同店 0 码有货）矛盾 → 这类矛盾必须被**记录为 incident 并降置信**，而不是静默地生成一条"售罄"。
- **数据质量指标（客户可见）**：每租户一张健康卡 —— 最近 24h 扫描覆盖率（成功格/计划格）、`UNTRUSTED` 占比、事件翻转率、通知送达率、每源 success_rate、账号续期状态。任一指标低于阈值要在前端显著标黄（承接"数据必须与门店实际一致"的验收标准）。

### 5.4 报表口径中的置信度体现（P0）
- 所有库存/价格接口返回 `confidence` + `as_of`（`last_verified_at`）字段，前端必须显示"数据时间"与可信度徽标。
- "全网有货门店"结果若含 `NOT_CARRIED` 或 `UNKNOWN` 的门店，**明确区分**"确认无货（N 家）"与"未知（M 家）"，不得合并为一个数字（否则就是 §5.3 的假阴性来源）。

---

## 6. 示例数据断言（验收用）

统一前置：`trust` 判定与 §5.1 一致；门店集合以 `Store` 表（100 家去重后）为准；`size_labels` 一律 `store-specific`（F1）。

**A1（IF1823-010 全网无货不误报）**
- **Given** `IF1823-010` 在 100 家门店最近 N=3 轮扫描中 `stock_state` 全为 `OUT_OF_STOCK`（`INVENTORY_INSUFFICIENT`），每轮每店结构 `status='OK'`、`size_count=6`（S,M,L,XL,XXL,3XL），全部 `trust='TRUSTED'`；
- **When** 变更检测对新一轮观测执行；
- **Then** 对 `IF1823-010` 产生的 `RESTOCK`/`PROMO_START` 事件数 = **0**；`ObservationLatest` 中该款 100 店 × 6 码共 600 格全为 `in_stock=false, stock_state='OUT_OF_STOCK'`；快照接口返回"0 家门店有货"且 `sizesTotal=6`（非"未知"），无 `NOT_CARRIED` 混入。

**A2（结构缺失 → 不允许变成假售罄）**
- **Given** 门店 X 对 `IF1823-010` 的结构请求返回空（`GtinStructure.status='EMPTY'`），前一轮该店该款为 `IN_STOCK`（假设值：M/L 有货）；
- **When** 本轮扫描完成；
- **Then** 该店该款的新观测 `trust` 必须为 `UNTRUSTED` / `stock_state='STRUCTURE_MISSING'`；**不产生 `SOLD_OUT`**；产生 1 条 `SCAN_FAILED(stage='STRUCTURE', error_code='STRUCTURE_MISSING')` 且 `affected_cells` 计入统计；快照接口该店呈现为"数据未更新（上次核验 时间 T）"，而非"无货"。

**A3（DN3707-202 多店尺码各异 + 结构按店不同）**
- **Given** 全国扫描结果与 `scan_0924_0808.json` 一致：7 家店有货（宁波江南 46/47.5；南昌明矾 40–45 共 9 码；九江庐山/北京京良 41–44.5；广州西环 41–44；武汉江夏 45/46；海口观澜湖 42.5）；且店 3875 的该款结构为 26 码（GTIN 前缀 `00196149…`）、店 3752 为 18 码（`00198486…`）；
- **When** 调用"某货号全网有货门店快照"（Q3）；
- **Then** 返回**恰好 7 家**门店，各店 `sizes[]` 与观测一致且按 `size_order` 排序；`sizesTotal` 取**本店结构**值（18 或 26，非全局常量）；查询计划走 `observation_latest` 部分索引，`EXPLAIN` 不含 Observation 全表扫描；且对 `(store=3752, style=DN3707-202, size='35.5')` 的解析必须返回"该店结构不含此尺码"（不生成探针请求、不生成 `SOLD_OUT`），而 `(store=3875, size='35.5')` 正常解析到 GTIN。

**A4（补货事件唯一 + 抖动合并）**
- **Given** 店 3792 的 `DN3707-202` 在 08:10 轮次为 `OUT_OF_STOCK`（`probe_state_3792.json`），下一轮变为 41/42/42.5/43/44 有货（对照 `probe_state_3754.json` 的尺码形状），`delta_total=0`；
- **When** 连续 3 轮扫描（间隔 1h）重复得到同一有货状态；
- **Then** 仅产生 **1 条** `RESTOCK` 事件（`fingerprint` 在 `dedupe_window_sec=6h` 内命中既有行 → `merged_count` 递增，不新建、不发第二条通知），`size_scope='*'` 且 `size_labels=['41','42','42.5','43','44']`，`confidence='HIGH'`；
- **并且** 若这 5 个尺码在 30 分钟内又全部回到无货，则 `RESTOCK` 与 `SOLD_OUT` 两条事件均 `is_suppressed=true, suppress_reason='JITTER_MERGE'`，客户实际只收到 **1 条**"库存抖动"摘要通知。

**A5（价格口径与阈值抑制）**
- **Given** `DN3707-202` 在 5 家店的连续两次可信观测均为 `price=1399, discount=0, total=1399, promos=[]`，`price_confidence='EXACT'`；同轮另一款 `IO5031-600` 观测为 `price=699, discount=314.55, total=384.45, promos=['指定鞋/服/配件 55折']`（对照 `probe_state_3752.json`），上一轮该款 `discount=0, total=699`；另有一款 `total` 从 `384.45` 变为 `384.00`（Δ=-0.45）；
- **When** 价格变更检测执行；
- **Then** `DN3707-202` 的 `PRICE_CHANGE`/`PROMO_*` 事件数 = **0**；`IO5031-600` 产生 **1 条 `PROMO_START`**（`subtype='PROMO_CHANGE'`，`discount` 0→314.55，`delta_pct≈-45%`，含促销名）；Δ=-0.45 的事件被记录但 `is_suppressed=true, suppress_reason='BELOW_THRESHOLD'`（未达 `max(1元, 1%)`）；且**没有任何事件因 `size_scope='*'` 而跨店混算**（每店独立成事件）。

---

## 7. 开放问题（需客户/产品确认，标注建议默认值）

| # | 问题 | 建议默认 | 影响 |
|---|---|---|---|
| Q1 | 变价口径：客户要"标价"还是"实付"？促销名变化要不要通知？ | 主口径 `total`（实付）；促销名单独变化不通知（仅记录） | 直接决定 PRICE_CHANGE 误报率 |
| Q2 | 静默期/聚合窗口默认值是否可接受？ | 聚合 300s、抖动 1800s、指纹去重 6h、夜间 23:00–08:00 顺延汇总 | 客户可感知的提醒及时性与噪音量 |
| Q3 | 无货时价格取不到（接口限制，F2），是否接受"沿用最后一次可信价并标 stale"？ | 接受，UI 显式标"历史价（日期）" | 影响价格事件的可用素材；不接受则只能在有货时才提醒变价 |
| Q4 | 历史保留期：24 个月价格曲线 vs 90 天原始观测，是否满足对账/举证需求？是否有合规留存要求？ | 90 天原始 + 24 个月日汇总 | 存储成本与法务风险 |
| Q5 | "某天某店某码一直有货但无变化"是否需要逐轮留痕？（现方案是每日 1 次快照） | 日快照 + 变更留痕 | 若需逐轮留痕，年写入量从 2400 万涨到 5 亿行，需加预算 |
| Q6 | 多租户共享观测结果是否允许？（相同 Nike 数据服务多客户，成本可降 5–20 倍）是否会泄露客户监控意图？ | 允许共享观测、隔离监控清单与通知；调度器全局去重探针单元 | 成本模型与隐私条款 |
| Q7 | 通知渠道与 SLA：邮件/企微/飞书/Webhook 哪些必须首版？失败重试与"必达"要求？ | 首版 邮件 + Webhook；重试 5 次退避 | 交付范围 |
| Q8 | 结构漂移（同店同款 GTIN 结构变化，如 18→26 码）是否要给客户告警？ | 客户侧仅一条 INFO 提示，细节进运维视图 | 客户体验（避免刷屏） |
| Q9 | 是否允许人工标注/纠错（"这条提醒是错的"）？纠错是否回流成规则调整？ | 允许客户一键报错 → `dq_incident`，人工复核后调参 | 影响验收期的信任建立 |
| Q10 | 账号池配额归属：单账号全局限速下，多客户间如何公平排队？（按租户额度/优先级/先到先得） | 按 `plan` 权重 + `WatchTask.priority` 加权轮询，配额耗尽时降 cadence 并通知 | 直接决定"零人工"能否稳住风控 |
| Q11 | 门店"下线/关闭"（`regions/v1` 不再返回）时，历史数据与监控目标如何处理？ | Store 置 `closed`，保留历史；监控目标自动停用并通知一次 | 影响覆盖率报表口径 |
| Q12 | 是否需要"客户自助添加货号"的即时探测（交互式，非定时）？配额与排队如何？ | P1：允许，单次探针限 1 店 × ≤40 格，进低优先级队列 | 影响 API 成本与前端交互设计 |
