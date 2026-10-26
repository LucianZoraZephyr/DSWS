# 电商问数智能体 · 从零重建（教学版）

> 这个目录不是旧系统的重构，而是**按契约从零搭的第二套实现**。
> 旧系统（`../shopkeeper-agent` + `../shopkeeper-agent-frontend`）冻结为 `legacy`，作为**反例库**。

路线图见 `../shopkeeper-agent/docs/REBUILD-ROADMAP.md`。

从零搭建/验收手册见 [`docs/SETUP-FROM-SCRATCH.md`](docs/SETUP-FROM-SCRATCH.md)。

---

## 当前进度

| 阶段 | 状态 | 验收 |
|---|---|---|
| **S0 骨架** | ✅ 完成 | `make stage0` |
| **S1 Schema 契约** | ✅ 完成 | `make stage1` |
| **S2 元知识构建** | ✅ 完成 | `make stage2` |
| **S3 检索** | ✅ 完成 | `make stage3` |
| **S4 生成 SQL** | ✅ 完成 | `make stage4` |
| **S5 安全边界** | ✅ 完成 | `make stage5` |
| **S6 失败通道** | ✅ 完成 | `make stage6` |
| S7 前端收口 | ⬜ 待做 | `make stage7` |
| S8 性能 | ⬜ 待做 | `make stage8` |

---

## S0 做了什么

**一句话**：不接 LLM、不接数据库、不接检索，只把「一次问数」的契约跑通。

三件产物：

1. **`contracts/steps.yaml`** —— 12 个步骤的唯一事实源。
   前端启动时从 `GET /api/steps` 拉取，**渲染层零硬编码**。
   `correct_sql` 标了 `conditional: true`，后端显式发 `skipped`，前端显示「未触发」。

2. **`contracts/sse.schema.json`** —— 只允许三种事件：
   - `progress { step, status, duration_ms }`
   - `result { columns, rows }`
   - `error { code, message, step }`
   后端 schema + 前端运行时类型守卫**双重拒绝**：未知 type / status / step 一律报错（旧系统是静默忽略）。

3. **假 SSE 流** —— 严格按契约发 24 帧：11 个非条件步 ×（running + success）+ 1 个条件步 skipped + 1 个 result。

**刻意没做**：真实 SQL、LLM、检索、数据库。这些从 S1 开始一层层接。

### 评审修复（S0）

| 问题 | 修复 |
|---|---|
| `duration_ms` 没进 required，success 缺失也能过 | schema 加 `required` + `if/then/else`；5 条参数化测试 |
| 前端 `as AgentEvent` 是纯断言，未知事件渲染空红框 | `parseAgentEvent` 运行时类型守卫；未知 type/status/step 抛 `UNKNOWN_EVENT` / `UNKNOWN_STEP` |
| 失败/静默结束后 running 永久转圈 | error / `STREAM_INCOMPLETE` / 网络错误三处统一收敛为 error |
| SSE 解析不支持 CRLF、多行 data、尾帧 | `SseParser` 重写；Vitest 8 条用例 |
| UI 冒烟用 `text=华北` 命中的是提问气泡 | 对照 `/api/steps` 校验顺序/文案；断言 skipped 身份、结果值、零错误卡；自起临时端口 |
| OpenAPI 把 SSE 声明成 `application/json` | 显式声明 `text/event-stream` |

---

## S1 做了什么

**一句话**：把「表结构」变成可校验的契约，并让旧系统的三处口径矛盾**持续变红**。

DDL 只能说明「有哪些列」，说不清三件事：

| DDL 表达不了 | 契约字段 | 本项目的例子 |
|---|---|---|
| 一行代表什么 | `grain` | `fact_order` 一行一单；`dim_region` 只到省，**没有 city 列** |
| 度量怎么算 | `measures` | `order_quantity` 是件数，**不是订单数**；`order_amount` 是整单金额，**不是单价** |
| 数据可用到哪 | `value_domain` | `date_id ∈ [20250101, 20250331]`；品牌枚举 15 个 |

四件产物：

1. **`contracts/schema.contract.yaml`** —— 5 张表 / 24 列的语义契约。
   每列声明 `role`、`value_domain`、`aliases`；每表声明 `grain`、`foreign_keys`、`measures`；
   每个指标声明 `formula`、`relevant_columns`、`expected_value`。
2. **`backend/app/schema_contract.py`** —— 检查器。
   解析 DDL → 装入内存 SQLite → 校验结构、外键、值域、指标公式与真实值；
   同时把旧系统夹具当作**负向用例**，必须检出 8 个已知缺陷。
   > 路线图写的交付物是 `docs/schema_contract.py`；实际放在 `backend/app/`，
   > 这样 pytest 可以直接 import，CLI 仍是 `python -m app.schema_contract`。
3. **`docs/SCHEMA-CONTRACT.md`** —— 由契约自动生成的人读版，`--check` 会检查它是否过期。
4. **`fixtures/`** —— 冻结的 `dw.sql`（115 单真实数据）、旧 `meta_config.yaml`、旧功能清单。
   负向夹具是**冻结副本**，所以单独 clone `rebuild/` 也能自洽运行。

### 指标口径（唯一事实源）

| 指标 | 公式 | 值 |
|---|---|---:|
| GMV | `SUM(order_amount)` | 279159.5 |
| AOV | `SUM(order_amount) / COUNT(DISTINCT order_id)` | **2427.47** |
| 订单量 | `COUNT(DISTINCT order_id)` | 115 |
| 销量 | `SUM(order_quantity)` | 622 |
| 平均件单价 | `SUM(order_amount) / SUM(order_quantity)` | 448.81 |
| 平均每单件数 | `SUM(order_quantity) / COUNT(DISTINCT order_id)` | 5.41 |

同一个 AOV 在旧系统里能算出三种结果：

| 算法 | 值 | 它其实是什么 |
|---|---:|---|
| `SUM(amount) / COUNT(DISTINCT order_id)` | **2427.47** | ✅ AOV |
| `SUM(amount) / SUM(quantity)` | 448.81 | ❌ 平均件单价 |
| `SUM(quantity) / COUNT(DISTINCT order_id)` | 5.41 | ❌ 平均每单件数 |

### 红 / 绿演示

```bash
cd backend

# 当前冻结夹具：预期红（退出码 1）；按契约修复后同一命令退出码 0
uv run python -m app.schema_contract --check-legacy
# RED: 旧系统夹具检出 14 条问题
#   AOV_THREE_WAY / AOV_COLUMN_MISMATCH / AOV_ALIAS_MISMATCH
#   CITY_GRAIN_UNDECLARED / ORDER_COUNT_UNDEFINED / ALIAS_COLLISION
#   FEATURE_LIST_TABLE_DRIFT / FEATURE_LIST_ROW_DRIFT

# 新契约：必须绿（退出码 0）
uv run python -m app.schema_contract --check
# PASS: 5 张表 / 24 列 / 115 行 / GMV=279159.5 / 销量=622
# PASS: 旧系统 14 条问题 / 8 个代码（预期红）
# PASS: docs/SCHEMA-CONTRACT.md 与契约一致
```

`--check` 不是「忽略旧系统」，而是把「旧系统必须红」当成断言：
少一个代码，或者旧夹具意外变绿，`make stage1` 就失败。

### 为什么 S1 仍然不装 MySQL

`fixtures/dw.sql` 直接装进内存 `sqlite3`：不需要 MySQL、Docker、`sudo`，也不需要 LLM。
检查器本身只依赖标准库 + PyYAML；首次 `make install` 仍需联网装依赖，之后即可离线复现。

---

## S2 做了什么

**一句话**：把 S1 的契约变成三份可检索的元知识，并用 `build_manifest` 证明
「同一次构建可以重复执行，第二次零写入」。

> 输入不是旧系统的 `conf/meta_config.yaml`（那是 S1 的负向夹具），而是
> `contracts/schema.contract.yaml`。旧配置只用来证明检查器会红；S2 不再把
> 一份自相矛盾的 YAML 当事实源。

### 三个存储，目标名全部隔离

| 存储 | rebuild 目标 | 内容 | 本次计数 |
|---|---|---|---:|
| Meta MySQL | `meta_rebuild` | 表/列/指标/列指标关系 + manifest | 5 / 24 / 6 / 9 |
| Elasticsearch | `value_index_rebuild` | 字段真实取值 → `column_id` | 76 docs |
| Qdrant | `column_info_rebuild` | 字段名/描述/别名向量 + payload | 98 points |
| Qdrant | `metric_info_rebuild` | 指标名/描述/别名向量 + payload | 22 points |

旧系统的 `meta` / `value_index` / `column_info_collection` / `metric_info_collection`
完全不动。`--fresh` 只删除 rebuild 自己的库、索引和 collection。

> ES 比旧系统多 1 条：契约把 `dim_date.year` 声明为 `enum`，S2 按策略同步了
> `year=2025`；旧系统没有同步它。这是有意的差异，不是漂移。
>
> Meta 的 `examples` 是**排序后的前 10 条**（显式截断策略），不是完整值域；
> 完整可检索值在 ES。S3 不能把 `examples` 当完整值域用。

### 产物：`build_manifest`

每次成功构建写一行：

| 字段 | 作用 |
|---|---|
| `manifest_version` | manifest 格式版本（当前 1） |
| `build_id` / `built_at` | 这次构建的身份与时间 |
| `config_hash` | 契约语义 + 目标名 + embedding + 值域摘要的 SHA-256 |
| `contract_version` | 契约版本 |
| `embedding_provider` / `embedding_dim` | 向量来源与维度 |
| `meta_*` / `es_values` / `qdrant_*` | 各存储计数 |
| `targets_json` | 这次构建到底写了哪几个目标 |
| `status` / `error` | 成功或失败；失败也留痕 |

### 幂等不是「upsert 一下」

构建流程是：

1. 先校验 S1 契约，生成计划；任何存储都没碰。
2. 非 `--fresh` 时先读最新 manifest。
3. 如果 `config_hash`、`contract_version` 一致，且三存储实际计数与 manifest 完全一致
   → 直接 `NOOP: 0 writes`，连 `CREATE TABLE IF NOT EXISTS` 都不执行。
4. 只有需要修复/重建时才写；写之前重建 ES 索引和 Qdrant collection，避免旧值残留。
5. 最后写 manifest；manifest 是「这次构建成功」的唯一凭据。

另外，ES 文档 id 和 Qdrant point id 都是确定性的：

- ES：`dim_region.province.广东省`
- Qdrant：`uuid5(kind, object_id, text)`

即使走到写入路径，重复执行也不会产生重复文档/重复 point。

### 为什么 S2 用哈希向量

旧栈的 `embedding` 容器依赖 1.3GB 的 `bge-large-zh-v1.5` 权重；当前机器权重缺失，
容器一直 restarting。S2 的目标是验证「构建 + 三存储 + manifest + 幂等」，
不应该被模型下载卡住。

所以默认实现是 `hash-ngram-v1`：

- 中文按单字 + 相邻双字，英文/数字按 token 切；
- BLAKE2b 稳定哈希到 256 维，符号位决定加减，最后 L2 归一化；
- 同一文本在任何进程/机器上得到同一向量，不依赖网络/GPU。

它**不是语义模型**，召回质量有限。S3 先用 ES 关键词召回 + 哈希向量跑通链路；
之后要换真实 bge，只需实现同一个 `EmbeddingProvider` 接口并 `--fresh` 重建。

### 命令

```bash
# 前置：旧系统的 MySQL / ES / Qdrant 已在跑（S2 只写 *_rebuild 目标）
make stage2

# 或者逐步看
cd backend
uv run python -m app.build_meta_knowledge --dry-run          # 只算计划，0 写入
uv run python -m app.build_meta_knowledge --fresh            # 删 rebuild 目标后全量构建
uv run python -m app.build_meta_knowledge --verify           # 只读对账
uv run python -m app.build_meta_knowledge --assert-idempotent # 第二次必须 noop
```

`--verify` 会检查：

- manifest 存在、`status=success`、`config_hash` / `contract_version` / `targets` 一致；
- Meta 4 个计数、ES count、两个 Qdrant count 全部等于 manifest；
- ES `value.analyzer` 存在且与 manifest 一致；
- 两个 Qdrant collection 的向量维度等于配置；
- 至少一个 ES 值文档、一个 column point、一个 metric point 的内容/ payload 正确。

### 环境变量

默认值面向本机教学环境；换环境时覆盖：

| 变量 | 默认 |
|---|---|
| `REBUILD_MYSQL_HOST` / `REBUILD_MYSQL_PORT` | `127.0.0.1` / `3306` |
| `REBUILD_MYSQL_USER` / `REBUILD_MYSQL_PASSWORD` | `didilili` / `dili123` |
| `REBUILD_MYSQL_ROOT_USER` / `REBUILD_MYSQL_ROOT_PASSWORD` | `root` / `dili123`（仅建库/授权用） |
| `REBUILD_META_DB` | `meta_rebuild` |
| `REBUILD_ES_URL` / `REBUILD_ES_INDEX` | `http://127.0.0.1:9200` / `value_index_rebuild` |
| `REBUILD_QDRANT_URL` | `http://127.0.0.1:6333` |
| `REBUILD_COLUMN_COLLECTION` / `REBUILD_METRIC_COLLECTION` | `column_info_rebuild` / `metric_info_rebuild` |
| `REBUILD_VECTOR_SIZE` | `256` |
| `REBUILD_EMBEDDING_PROVIDER` | `hash-ngram-v1` |
| `REBUILD_MAX_VALUE_DOCS` | `1000` |

### 本次测试

- S0：19 条
- S1：40 条
- S2：16 条（计划计数、同步策略、examples 截断、确定性 ID/hash、坏契约拒绝、dry-run 等）
- S2 完成时合计：**75 条**；现在 `make stage2` 会收集全部 113 条
  （其中 23 条 S3 真实存储集成测试默认跳过）。

---

## S3 做了什么

**一句话**：不接 LLM，先用**确定性关键词 + 三存储批量召回**，把问题收窄成
「表 / 字段 / 指标 / 真实值 / 额外上下文」，并输出一份可逐字节复现的 YAML。

```
query
  -> extract_keywords
  -> recall_column / recall_value / recall_metric   （批量，一次往返）
  -> merge_retrieved_info                           （一次 Meta 快照）
  -> filter_table -> filter_metric                  （确定性裁剪）
  -> add_extra_context                              （日期 / 季度 / 数据集 / MySQL 版本）
  -> RecallState -> stable YAML
```

### 关键词不是「分词一下」

没有 jieba、没有 LLM，只有三层确定性规则：

1. **契约词典**：表名、列名、列别名、指标 name/label/alias、契约声明的枚举值。
   「销售额 → order_amount」这类映射由 `schema.contract.yaml` 给出，不靠模型猜。
2. **日期模式**：`2025 年 1 月`、`2025Q1`、`2025-01`、`20250101` 等正则识别，
   同时产生 `dim_date.year / month / quarter` 的提示。
3. **完整 query 兜底**：契约里没有的客户名/商品名，原文仍会进入检索。

单字别名（如 `省`）只作为字段提示，不直接当 ES 关键词；ES 兜底查询用
完整短语（走索引 analyzer）+ 确定性中文 2-gram（2-gram 固定 `analyzer: keyword`），
避免 IK 把 `省`、`1` 这类单字放大成噪声。

### 往返次数是硬约束，不是「尽量批量」

每道题只允许：

| 请求 | 次数 | 说明 |
|---|---:|---|
| ES `_msearch` | 1（重试时最多 2） | 所有关键词一次多查询 |
| Qdrant column `search/batch` | 1 | 一个批次搜完整列 collection |
| Qdrant metric `search/batch` | 1 | 一个批次搜完整指标 collection |
| Meta MySQL | 5 条 SQL | 4 张表 + `SELECT VERSION()`，同一连接 |

黄金集测试会断言这些计数，防止以后悄悄退化成逐候选 N+1。

### 候选合并与过滤

- Qdrant 多关键词结果按 payload `id` 去重，取最高向量分；
- 再用契约 name/alias/value 做**词法补偿分**——hash 向量只认字面重叠，
  没有这层补偿，同义改写会漏召；
- 最终字段保留线：词法分 ≥ `0.5`，或向量分 ≥ `0.72`，或命中真实值；
- `filter_table` 保留有直接证据的表，以及 join 路径上的主外键；
- `filter_metric` 只保留有词法证据的指标，再按 `relevant_columns` 补依赖列，
  最后重新裁掉不需要的 join 键；
- 所有输出按契约顺序或 `(-score, id)` 排序，不依赖 `set` 迭代顺序。

### YAML 是给 LLM 的上下文，不是调试 dump

- 契约字段全部保留：表级 `grain / measures`，列级 `name / type / role / description /
  aliases / value_domain`，指标级 `formula / expected_value / relevant_columns /
  rejected_values`；根节点还保留完整 `dataset`；
- Meta 的 `examples` 是 S2 截断后的前 10 条，因此每个字段显式写
  `examples_truncated: true` / `examples_limit: 10`，根节点再重复一次
  `examples_policy`；
- 完整命中值放在 `matched_values`，不把 examples 伪装成全集；
- `retrieval` 段记录往返次数、候选数、过滤前后计数，方便定位召回退化；
- CLI 的 YAML 只走 stdout，统计只走 stderr；渲染本身逐字节稳定，
  `current_date / weekday / quarter` 来自运行日，跨天会变，需要固定日期时可注入 `now`。

### 黄金集

`backend/tests/recall/golden.yaml` 固定 20 道题，覆盖单表/多表、指标、
真实值过滤、日期、季度、join 键与「无指标」问法。每题断言：

- 期望的表 / 字段 / 指标 / 真实值全部命中；
- 输出规模有上限、precision 有下限（防止「全召回」蒙混过关）；
- ES / Qdrant / Meta 往返次数符合上表。

当前 **20/20 通过**；集成测试默认跳过，只有 `REBUILD_INTEGRATION=1` 才连真实服务。

### 命令

```bash
make stage3

# 或者逐步看
cd backend
REBUILD_INTEGRATION=1 uv run pytest -q tests/recall   # 38 条：黄金集 + 往返 + 契约字段 + 单测
uv run python -m app.scripts.recall "华北销售额"        # stdout YAML / stderr 统计
```

### 本次测试

- S3：38 条（20 题黄金集、关键词、YAML、契约字段保真、额外上下文、往返次数、精度上限）
- 合计：**113 条**；`make stage3` 会跑 S2 对账 + 全部后端测试。

---

## S4 做了什么

**一句话**：把「召回上下文」翻译成**一条可被机器判定的 SQL**，或者一个**有名字的拒答**——
并把「只读 / 单语句 / 列存在 / 无常量投影 / 排名必带 ORDER BY」这五条规则
从提示词搬进**代码**。

```
query
  -> build_offline_context     （契约离线裁剪；或 context_from_recall_state 接 S3）
  -> build_slots               （7 个槽位）
  -> render_prompt             （缺槽位直接抛异常）
  -> generator.generate        （确定性实现 / 可选 LLM）
  -> 输出形状校验               （sql_or_refusal）
  -> 静态校验                   （sqlglot AST，5 条硬规则）
  -> GenerationResult
```

**S4 不执行 SQL**，边界到「生成 + 静态校验」为止；`run_sql` 留给 S5/S6。

### 三条硬规则写在两个地方

审计发现旧系统 7 个提示词里，`ORDER BY` / `排序` / `LIMIT` / `Top` **总命中 0**，
`拒绝` / `无法生成` 也**总命中 0**——也就是说「排名必带 ORDER BY」和
「越界必拒答」这两条规则**从来就没存在过**。

S4 让它们同时存在于两处，并且**代码层是最终裁判**：

| 硬规则 | 提示词里 | 代码里 |
|---|---|---|
| 排名必带 ORDER BY | `generate_sql.prompt` 的硬规则一 | `guard.py` → `SQL_MISSING_ORDER_BY` / `SQL_MISSING_LIMIT` |
| 越界必拒答 | 硬规则二 | `intent.py` → `OUT_OF_RANGE` / `UNSUPPORTED_INTENT` |
| 禁止常量 / NULL 投影 | 硬规则三 | `guard.py` → `SQL_CONSTANT_PROJECTION` |

生成器和校验器**共用** `app/generate/intent.py` 里同一份排名判断，所以不会出现
「生成器觉得该排、校验器觉得不用排」这种规则跑偏。

### 槽位校验是真的会报错

旧系统 7 个提示词的占位符「看起来」四方一致，但 langchain-core 会用模板自动
探测的结果**覆盖**节点里写的 `input_variables`——那 7 处声明是**死代码**，
拼错也不报错。

S4 反过来做：**frontmatter 的 `slots` 声明是唯一事实源**。

- 正文里出现但没声明的占位符 → 加载即报错；
- 声明了但正文里没出现的槽位 → 加载即报错；
- 渲染时少给一个槽 → `PromptSlotError`，**不静默补空串**。

`generate_sql` 正好 **7 个槽位**：`query / table_infos / column_infos /
metric_infos / value_infos / date_info / db_info`（旧系统只有 5 个，没有
`column_infos` 与 `value_infos`）。

### 输出只有两种形状

`contracts/sql.output.schema.json` 的 `sql_or_refusal` 只允许两种形状，
**没有第三种**：

```json
{"sql": "SELECT ..."}
{"refusal": {"code": "OUT_OF_RANGE", "reason": "..."}}
```

所以旧系统那句 `SELECT '无法生成SQL…' AS error_message`——把失败伪装成一行
数据——在契约层就不可能发生。静态校验不通过的 SQL **不会**以 `sql` 的形态
返回，一律降级成带违规码的拒答。

### 静态校验用 AST，不用正则

`guard.py` 用 sqlglot 把 SQL 解析成 AST 再判定，五条规则各自都有能复现它的
负例：

| 违规码 | 判定 |
|---|---|
| `SQL_MULTIPLE_STATEMENTS` | 解析出不止一条语句 |
| `SQL_NOT_READ_ONLY` | 根节点不是 SELECT；或出现 DML/DDL/INTO |
| `SQL_UNKNOWN_TABLE` | 引用了上下文之外的表 |
| `SQL_UNKNOWN_COLUMN` | 引用了上下文之外的字段 |
| `SQL_CONSTANT_PROJECTION` | SELECT 列表里投影了字面量常量或裸 NULL |
| `SQL_MISSING_ORDER_BY` / `SQL_MISSING_LIMIT` | 有排名意图却缺 ORDER BY / LIMIT |

「只读」额外有一道**词法防线**：AST 解析不了 `SELECT ... INTO OUTFILE`，
只有词法扫描能把它认成「不是只读」而不是「语法错误」——错误码是契约的一部分。

### 为什么 S4 有「确定性生成器」这一层

路线图要求 S4 的验收是「10 题基线 + `make baseline` 表格」。**表格要稳定可
复现**，就不能每次都问一次模型。所以 S4 同时交付两样东西：

- **提示词契约**：7 个 `.prompt` 文件，每个声明槽位与输出 schema
  （`generate_sql` 是唯一的运行时提示词，其余 6 个标注为旧系统对照资产）；
- **确定性实现**：纯代码、不调模型、产出**同一个形状**、过**同一套校验**。

真的想接 LLM 时，`HttpLlmGenerator` 已经留好接口（OpenAI 兼容端点，设置
`REBUILD_LLM_BASE_URL` / `REBUILD_LLM_API_KEY` / `REBUILD_LLM_MODEL` 才启用），
它产出的 SQL 一样要过静态校验——**换成真模型不会放宽任何一条硬规则**。

### 10 题基线

`backend/tests/generate/baseline.yaml`：8 题取自 S3 黄金集（同题在检索层和
生成层必须一致，有测试钉住），另加 2 道越界题。

```
| 问题               | 判定     |
| 华北销售额          | PASS     |
| 平均客单价          | PASS     |
| 各品类销量          | PASS     |
| 品牌销售额 Top 3    | PASS     |
| 黄金会员订单量       | PASS     |
| 哪个省销量最高       | PASS     |
| 各省平均订单金额      | PASS     |
| 2025 年 1 月销量     | PASS     |
| 2024 年销售额       | REFUSAL (OUT_OF_RANGE)        |
| 各城市销售额        | REFUSAL (UNSUPPORTED_INTENT)  |
```

当前 **10/10 通过**。

### S4 不需要 MySQL / ES / Qdrant

上下文由契约离线裁剪（`build_offline_context`），所以 `make stage4` 在一台
**没有 Docker、没有 API key、没有网络**的机器上就能跑完并给出同样结果。
真接 S3 召回时用 `--live`（或 `generate_with_recall`），服务不可用会**直接
报错**，不会悄悄退回离线上下文。

### 命令

```bash
make stage4

# 或者逐步看
cd backend
uv run pytest -q tests/generate                              # 113 条（108 + 5 跳过）
uv run python -m app.scripts.generate "华北销售额"           # stdout YAML / stderr 统计
uv run python -m app.scripts.generate --baseline             # 10 题表格
```

### 本次测试

- S4：108 条 + 5 条按需跳过（提示词契约 / 静态校验 / 生成 / 基线 / 三方对齐 / S3→S4 适配）
- S0–S4 合计：**226 条**（198 通过 / 28 跳过）；`make stage4` 会跑全量后端测试。

---

## S5 做了什么

**一句话**：把「只读」从提示词搬进代码——SQL 在**连库之前**就要过 AST 级策略，
执行用的数据库账号本身**只有 SELECT**。

```
SQL
  -> enforce（app/security/policy.py）
       ① 可执行注释 / 词法禁词    ← 解析器看不见的那些洞
       ② 单语句                   ← D6 现象 A
       ③ 只读                     ← D6 现象 B
       ④ 集合运算 / 危险函数
       ⑤ 表白名单 / 列白名单        ← 白名单来自契约
       ⑥ 去注释 + 自动 LIMIT
  -> ReadOnlyExecutor
       策略不通过 → 一次数据库往返都没有
       策略通过   → 只读账号 + max_execution_time + 不许多语句
  -> ExecutionResult
```

### 旧系统的两条实测证据

审计 D6（高·安全）在真实 MySQL 上实测过两件事，S5 把它们**原样复现成负例**：

| 旧系统行为 | 实测结果 | 新系统 |
|---|---|---|
| `validate()` = `f"explain {sql}"` | `UPDATE` / `DELETE` / `INSERT` **全部通过校验**（MySQL 8.0.18+ 支持 EXPLAIN DML） | `SQL_NOT_READ_ONLY` |
| `explain {sql}` 只解释第一句 | `SELECT 1; DROP TABLE x` **校验无异常，DROP 真执行了**（DDL 自动提交） | `SQL_MULTIPLE_STATEMENTS` |

这不是推测：`fixtures/injection/sql-injection.yaml` 里每条语料都记着旧系统的
实测行为与出处（`audit-sql-validation.md` §3.6）。

### 策略的判定顺序是刻意的

**从「解析器看不见的洞」开始查**，因为那些地方 AST 根本给不出结论：

| 原因码 | 判定 |
|---|---|
| `SQL_COMMENT_OBFUSCATION` | 含 MySQL 可执行注释 `/*! ... */`——服务端会执行它，sqlglot 会忽略它 |
| `SQL_NOT_READ_ONLY` | 写操作关键字 / 写节点 / 根节点不是 SELECT / `INTO OUTFILE` |
| `SQL_MULTIPLE_STATEMENTS` | 解析出不止一条语句 |
| `SQL_FORBIDDEN_CONSTRUCT` | 集合运算（UNION）、`SLEEP` / `BENCHMARK` / `LOAD_FILE` 等 |
| `SQL_TABLE_NOT_ALLOWED` | 表或库不在契约白名单里（含 `information_schema` / `mysql`） |
| `SQL_COLUMN_NOT_ALLOWED` | 列不在契约白名单里 |
| `SQL_SYNTAX` / `SQL_EMPTY` | 解析不了 / 空 |

对外只暴露**一个**通道码 `SQL_REJECTED`，具体原因放在 `reason_code` 里——
失败只有一种表达方式（审计根因 B）。

### 白名单不手抄第二份

策略的表 / 列白名单由 `contracts/schema.contract.yaml` **派生**
（`SqlWhitelist.from_catalog`），不在 `sql.policy.yaml` 里再抄一遍。
约束 3 说「schema 单一事实源，禁止手写第二份」——这里照做。

`sql.policy.yaml` 只声明**安全参数**：行数上限、超时、禁词、只读账号、错误码。
而且有一条测试盯着：**这些参数不能在代码里再写一遍**（`test_policy_alignment.py`
用 AST 扫常量），否则契约改了门禁还在绿。

### 执行的就是被校验的那条

策略最后把 AST **重新序列化**：注释被剥掉、LIMIT 被补上或截断。执行器跑的是
这个产物，不是原始文本——否则「校验的那条」和「跑的那条」就会是两条语句，
D6 的漏洞换个地方复发。

```
输入： SELECT SUM(fact_order.order_amount) FROM fact_order
执行： SELECT SUM(fact_order.order_amount) FROM fact_order LIMIT 1000
```

### 四道闸

| 闸 | 在哪 | 作用 |
|---|---|---|
| 策略 | `app/security/policy.py` | 拒绝危险 SQL（且**不连库**） |
| 只读账号 | `app/security/account.py` | 账号在 `dw` 上只有 SELECT——旧账号是 `GRANT ALL PRIVILEGES` |
| 语句超时 | `max_execution_time` + socket 读超时 | 一条没有 LIMIT 的全表扫描不该拖死服务 |
| 不许多语句 | 连接标志去掉 `CLIENT_MULTI_STATEMENTS` | 即使策略被绕过，一条连接也只跑一条语句 |

只读账号**不存密码在仓库里**（`REBUILD_DW_RO_PASSWORD` 由部署环境提供），
开通语句是 `DROP USER IF EXISTS` + `CREATE USER` + `GRANT SELECT`——所以它的
权限集合**恰好**是策略声明的那一项，不会攒下以前的 `INSERT`。

验证账号时**真的去写一次**（`UPDATE ... WHERE 1 = 0`，匹配不到任何行），
而不是只读 `SHOW GRANTS`：第 1 步是读权限表，第 2 步是问数据库「你到底
让不让我写」。

### 注入语料

`fixtures/injection/sql-injection.yaml`：**36 条必须拒绝** + **11 条必须放行**。

```
拒绝：多语句 3 · 注释绕过 2 · 写操作 18 · 集合运算与危险函数 8 · 白名单外 5
放行：聚合 / JOIN / GROUP BY / 中文别名 ORDER BY / 子查询 / 字符串里的分号 …
```

`allow` 段是**零误杀**语料——它们长得像正常查询，必须全部通过。语料还钉住了
「不能退化」：条目数下限、每条禁用关键字至少被一条语料覆盖、每条语料都必须
记录旧系统的行为与出处。

### 两屏对比

`make stage5-live` 会真的按**旧系统的方式**跑一遍（`explain {sql}` 然后直接
执行），用一张一次性的探针表 `_s5_probe` 证明两件事：

```
【旧系统】校验结果：通过（没有抛出任何异常）
         第二句真的执行了：是（探针表 已被 DROP）
【新系统】判定：拒绝（原因码 SQL_MULTIPLE_STATEMENTS）
         探针表：还在（数据库没被碰过）
```

### S5 的门禁不需要 MySQL

策略只依赖契约与 sqlglot，所以 `make stage5` 在一台**没有 Docker、没有数据库、
没有网络**的机器上就能跑完并给出同样结果。只读账号的开通与验证、两屏对比需要
MySQL，放在 `make stage5-live` 里。

### 命令

```bash
make stage5

# 或者逐步看
cd backend
uv run python -m app.scripts.security --check      # 注入语料 100% 拒绝 + 零误杀
uv run python -m app.scripts.security --corpus      # 语料判定表
uv run python -m app.scripts.security --policy "SELECT 1; DROP TABLE x"   # 单条判定
uv run pytest -q tests/security                    # 171 条（+ 2 条跳过）
```

需要 MySQL 的部分：

```bash
export REBUILD_DW_RO_PASSWORD=...          # 只读账号密码，不在仓库里
make stage5-live                            # 开通 + 验证 + 两屏对比
```

### 本次测试

- S5：171 条 + 2 条按需跳过（词法层 / 策略 / 语料 / 零误杀 / 执行器 / 账号 / 对齐 / Demo）
- 合计：**369 条**（369 通过 / 30 跳过）；`make stage5` 会跑全量后端测试。

---

## S6 做了什么

**一句话**：让每一种失败都有名字、有 UI、有测试——校正闭环 + 错误通道端到端打通。

```
一次问数
  -> 上下文 8 阶段   app/generate/context.py（真的 8 个阶段，不是假事件）
  -> generate_sql    app/generate/pipeline.py
  -> validate_sql    静态校验（AST）+ 安全策略（AST）
  -> [correct_sql]   可重试的错误才进；最多 1 次；改完**重新校验**
  -> run_sql         只读账号 + 超时
  -> result / error  流一定以其中之一收尾
```

S6 把 S0 的**假流**换成了真流水线。原来那条流永远成功，所以审计里最刺眼
的三类静默失败——伪 SQL 被当成一行数据、召回为空也算 success、错误被吞掉
——在它上面一个都测不出来。

### 错误码表：每个失败都有名字

`contracts/errors.yaml` 是唯一事实源。路线图点名的 7 个码是这张表的**下界**：

| 错误码 | 名称 | 类别 | 可重试 | 来自哪一步 |
|---|---|---|---|---|
| `SQL_REJECTED` | SQL 被安全策略拒绝 | failure | 否 | validate_sql |
| `SQL_SYNTAX` | SQL 语法错误 | failure | 是 | validate_sql |
| `SQL_UNKNOWN_COLUMN` | 引用了不存在的列 | failure | 是 | validate_sql |
| `EMPTY_RESULT` | 查询成功但没有数据 | **empty** | 否 | run_sql |
| `LLM_REFUSAL` | 模型明确拒答 | failure | 否 | generate_sql |
| `TIMEOUT` | 执行超时 | failure | 否 | run_sql |
| `CANCELLED` | 用户取消 | **cancelled** | 否 | 任意一步 |

另外 9 个码是「让每一种失败都有名字」补齐的：S4 静态校验的 9 个违规码
（`SQL_EMPTY` / `SQL_MULTIPLE_STATEMENTS` / `SQL_NOT_READ_ONLY` /
`SQL_UNKNOWN_TABLE` / `SQL_CONSTANT_PROJECTION` / `SQL_MISSING_ORDER_BY` /
`SQL_MISSING_LIMIT`），加上 S5 执行器的 `SQL_EXECUTION_ERROR` 与兜底的
`INTERNAL_ERROR`。少了它们，这些失败只能挤进一个通用码——那就等于又回到了
「所有失败长得一样」。

**一条映射规则**：拒答码在表里就是它自己，不在就是 `LLM_REFUSAL`。
于是 S4 的 9 个违规码各自保留名字，而生成器主动拒答的
`OUT_OF_RANGE` / `UNSUPPORTED_INTENT` / … 统一归到 `LLM_REFUSAL`
（原码留在 `detail.reason_code` 里）——它们是**模型的判断**，不是系统的
失败类别。

### 三种失败类别，长得不一样

`class` 决定界面怎么渲染，**不是**码决定：

| class | 界面 | 步骤条 |
|---|---|---|
| `failure` | 红框 + 错误码 + 可复制详情 | 失败步骤**红叉** |
| `empty` | 虚线空态卡 | 执行步骤**保持绿色** |
| `cancelled` | 中性卡 | 正在跑的步骤标「已取消」 |

`empty` 单独一类是刻意的。审计 FP-8 写得很清楚：

> ⚠️ 注意**不能**把空数组直接标成红色错误 —— `[]` 是**合法语义**
> （库里真的没这批数据），改成错误态会破坏正常场景。

所以空结果有**自己的码**（`EMPTY_RESULT`）和**自己的界面**，但它不标红：
库里没有这批数据不是系统的错。

### 校正闭环：不是绕过安全检查的后门

旧系统也有一个 `correct_sql` 节点，但有三个问题：没有次数上限、校正结果
不复查、失败被吞掉（`state["error"]` 还留着上一轮的旧错误）。

新的闭环：

```
校验失败（可重试的码）
  -> correct_sql：最多 1 次（契约里的 correction.max_attempts）
  -> 校正后的 SQL **重新过两道关**：静态校验 + 安全策略
  -> 通过 → 清空旧 error，继续执行
  -> 不通过 → 错误仍然是原来那个（根因没变，错误就不该变）
```

「重新过安全策略」这一关是重点：**校正不是绕过安全检查的后门**。
校正器返回一条带写操作的 SQL，闭环会把它挡下来；返回一条 UNION，同样挡下来
（`SQL_FORBIDDEN_CONSTRUCT`）。模型说的不算数，代码说了算。

`SQL_REJECTED` **不可重试**：拿一条被安全策略拒掉的 SQL 去问模型「改一下」，
等于给注入开了重试的口子。

原始 SQL 一直留着（`detail.original_sql`），校正后的也留着
（`detail.corrected_sql`），试了几次记在 `detail.attempts`。
「保留原始 SQL」不是口号，是这三个字段。

### 取消：状态是 cancelled，不是 done

旧系统把取消算成完成：

```tsx
// 旧系统 fe:src/App.tsx:149
status: isAbort ? "done" : "error",
```

用户点了停止，界面显示「已停止本次查询。」，但状态是 `done`——在旧系统的
眼里，一次被掐断的查询和一次成功返回 20 行的查询**长得一样**。

新系统里取消是**第三个状态**：

- 服务端：`POST /api/cancel` → 编排器在下一个步骤边界停下 → 发一条
  `CANCELLED` 错误事件；
- 前端：正在跑的步骤标成「已取消」，**后面的步骤不再开始**；
- 界面：状态徽章显示「已取消」，**不弹红框**（取消不是失败）。

### 6 类失败在浏览器里

`make stage6-ui` 起一个隔离的临时后端与前端，跑两个无头浏览器用例：

```
6 类失败 → 6 种互不相同的界面状态（错误码 / 类别 / 失败步骤）
  SQL_REJECTED       failure  validate_sql  红叉
  SQL_SYNTAX         failure  validate_sql  红叉
  SQL_UNKNOWN_COLUMN failure  validate_sql  红叉
  EMPTY_RESULT       empty    run_sql       绿（不标红）
  LLM_REFUSAL        failure  generate_sql  红叉
  TIMEOUT            failure  run_sql       红叉
取消 → 状态 = 已取消，步骤条停止推进
```

断言的不是「弹了个红框」，而是**每类失败有它自己的样子**：
错误码要看得见（不是只在 data 属性里）、失败步骤要对、空结果不许标红、
取消不许变成完成。

### 场景应用：只换掉两个外部依赖

浏览器测试需要**可复现**的失败——不能指望真模型恰好吐出一条坏 SQL。
所以有 `app/scenarios/`：一个与生产应用**共用同一套 HTTP / SSE 管道**
（`app.main.create_app`）的应用，只把编排器的两个外部依赖换成夹具里的替身。

| | 生产 | 场景 |
|---|---|---|
| 上下文构建 | 真 | 真 |
| 提示词渲染 | 真 | 真 |
| 静态校验（AST） | 真 | 真 |
| 安全策略（AST） | 真 | 真 |
| 校正闭环 | 真 | 真 |
| SSE 序列化 | 真 | 真 |
| **模型** | 真 / 确定性 | 夹具 |
| **数据库** | 只读账号 | 夹具 |

所以屏幕上的每一个红叉都对应一次**真实的判定**，而 6 类失败又**可复现**。
夹具在 `fixtures/failures/scenarios.yaml`，问题写成「场景:<id> <真实问题>」，
前缀剥掉后交给流水线的仍是**真实问题**——上下文因此是真的。

### S6 的门禁不需要 MySQL，也不需要 LLM

`make stage6` 在一台**没有 Docker、没有数据库、没有 API key** 的机器上就能跑完：

```bash
make stage6

# 或者逐步看
cd backend
uv run python -m app.scripts.failures --check       # 6 类失败各跑一遍
uv run python -m app.scripts.failures --table       # 错误码表
uv run python -m app.scripts.failures --scenarios   # 失败场景表
uv run pytest -q tests/agent                       # 93 条
```

浏览器验收需要 Chrome / Edge（Windows 上用 Edge 也行）：

```bash
make stage6-ui
```

### 命令

```bash
make stage6        # 离线门禁（不需要 MySQL / LLM / 浏览器）
make stage6-ui     # 浏览器验收：6 类失败 + 取消（需要 Chrome/Edge）
```

### 本次测试

- S6：93 条（错误码表 / 严格性 / 校正闭环 / 编排 / 事件 / API / 场景 / 对齐）
- 前端：23 条协议单测 + 2 个无头浏览器用例
- 合计：**462 条**（462 通过 / 30 跳过）；`make stage6` 会跑全量后端测试。

---

## 快速开始

```bash
cd code/rebuild

# 终端 1
make dev-backend      # http://127.0.0.1:8100

# 终端 2
make dev-frontend     # http://127.0.0.1:5273
```

打开 http://127.0.0.1:5273 ，输入任意问题，看步骤条依次点亮。

### 验收

```bash
make install          # 首次（uv sync --frozen + npm ci）
make stage0           # S0：后端契约测试 + 前端协议单测 + 构建（不需要浏览器）
make stage0-ui        # 可选：真实 Chrome 冒烟（自起临时端口 8101/5274，跑完清理）
make stage1           # S1：契约绿 + 负向夹具红 + 后端 S0/S1 测试（不需要 MySQL/Docker）
make stage2           # S2：dry-run + --fresh + --verify + 幂等（需要 MySQL/ES/Qdrant）
make stage3           # S3：S2 对账 + 20 题黄金集 + CLI YAML + 全部后端测试
make stage4           # S4：10 题基线 + 生成层测试 + 全部后端测试（不需要 MySQL/Docker）
make stage5           # S5：注入语料 100% 拒绝 + 零误杀 + 全部后端测试（不需要 MySQL）
make stage5-live      # S5 实战：开通只读账号 + 验证它写不了 + 两屏对比（需要 MySQL）
make stage6           # S6：错误码表 + 6 类失败 + 校正闭环 + 取消（不需要 MySQL/LLM）
make stage6-ui        # S6 浏览器验收：6 类失败各一个用例 + 取消（需要 Chrome/Edge）
make baseline         # 只看 S4 的基线表格：问题 / SQL / 判定
```

> `make stage2` 的前置是旧系统 docker compose 里的 MySQL / ES / Qdrant 已启动；
> 它只操作 `*_rebuild` 目标，不会删旧系统的数据卷。当前 embedding 容器坏了也没关系，
> S2 默认用确定性哈希向量。
>
> `make stage4` **不需要**任何外部服务：上下文由契约离线裁剪，基线因此可以在
> 任何一台机器上复现同样的结果。
>
> `make stage5` 同样**不需要**数据库：安全策略只依赖契约与 sqlglot。
> `make stage5-live` 才需要 MySQL，并且需要环境变量 `REBUILD_DW_RO_PASSWORD`
> （只读账号的密码不在仓库里）。
>
> `make stage6` **不需要** MySQL，也不需要 LLM：6 类失败由
> `fixtures/failures/scenarios.yaml` 驱动，只替换模型与数据库两个外部依赖，
> 校验、策略、校正、事件全是真代码。`make stage6-ui` 需要 Chrome / Edge。

> **Windows**：控制台默认代码页是 GBK，中文与 `↔` 之类符号会触发
> `UnicodeEncodeError`，导致 `make stage1` 直接崩。CLI 内部已经把 stdout/stderr
> 切到 UTF-8（`backend/app/cli.py`），但 **make 自己 echo 的那几行中文**仍按
> 控制台代码页渲染——先执行 `chcp 65001` 再跑 make，输出就是正常的。

---

## 目录

```
rebuild/
├── contracts/              # 契约（前后端唯一事实源）
│   ├── steps.yaml          # 12 个步骤 + conditional 标记
│   ├── sse.schema.json     # 三种 SSE 事件
│   ├── schema.contract.yaml # S1：5 表 / 24 列 / 6 指标的语义契约
│   ├── sql.output.schema.json # S4：sql_or_refusal + 拒答码表
│   ├── sql.policy.yaml     # S5：安全策略（行数上限/超时/禁词/只读账号/错误码）
│   ├── errors.yaml        # S6：错误码表 + 校正策略（16 个码 / 3 种类别）
│   └── prompts/            # S4：7 个提示词（frontmatter 声明槽位 + 输出 schema）
├── fixtures/               # S1 冻结数据与负向夹具（不依赖旧项目）
│   ├── dw.sql              # 物理 DDL + 115 单真实数据
│   ├── legacy/             # 旧 meta_config.yaml + 功能清单（必须红）
│   ├── injection/          # S5：36 条必须拒绝 + 11 条必须放行的注入语料
│   └── failures/           # S6：6 类失败 + 正常路径 + 校正闭环的场景夹具
├── docs/
│   ├── SCHEMA-CONTRACT.md  # 由契约自动生成（--write-doc）
│   └── screenshots/
├── backend/
│   ├── app/main.py         # S0 假流 + /api/steps
│   ├── app/cli.py          # CLI 公共入口（stdout 固定 UTF-8）
│   ├── app/sqllex.py       # S4/S5 共用词法层（注释/字面量/关键字）
│   ├── app/schema_contract.py # S1 检查器 + 文档生成器 + CLI
│   ├── app/build_meta_knowledge.py # S2 CLI
│   ├── app/build/          # S2 计划/embedding/三存储/manifest
│   ├── app/recall/         # S3 关键词/批量召回/合并过滤/YAML
│   ├── app/generate/       # S4 上下文/提示词/生成器/静态校验/编排
│   ├── app/security/       # S5 策略/执行器/只读账号/注入语料/Demo
│   ├── app/agent/          # S6 错误码表/校验关卡/校正闭环/编排器/取消
│   ├── app/scenarios/      # S6 场景应用（只替换模型与数据库）
│   ├── app/scripts/recall.py # S3 CLI（stdout YAML + stderr 统计）
│   ├── app/scripts/generate.py # S4 CLI（YAML / --baseline 表格）
│   ├── app/scripts/security.py # S5 CLI（--check / --corpus / --policy / --demo）
│   ├── app/scripts/failures.py # S6 CLI（--check / --table / --scenarios）
│   └── tests/              # S0 19 + S1 40 + S2 16 + S3 38 + S4 113 + S5 173 + S6 93 = 462 条
│       ├── recall/         # 20 题黄金集 + 关键词/渲染/往返测试
│       ├── generate/       # 10 题基线 + 提示词/静态校验/三方对齐测试
│       ├── security/       # 词法/策略/语料/零误杀/执行器/账号/对齐/Demo
│       └── agent/          # 错误码表/校正闭环/编排/取消/API/场景/对齐
├── frontend/
│   ├── src/App.tsx
│   ├── src/lib/api.ts                # 运行时类型守卫 + SSE 解析器
│   ├── src/lib/sse.test.ts           # Vitest 协议单测（18 条）
│   ├── src/components/StepRail.tsx   # 竖向导轨（响应式，含「已取消」态）
│   ├── src/components/ErrorCard.tsx  # 失败卡片（红框/空态/取消 + 可复制）
│   ├── tests/s0_smoke.mjs            # 无头 Chrome 冒烟（正常路径）
│   └── tests/s6_failures.mjs         # 无头 Chrome：6 类失败 + 取消
├── scripts/stage0-ui.sh    # 自起临时端口跑冒烟，跑完清理
├── scripts/stage6-ui.sh    # 自起场景应用 + 前端，跑 6 类失败的浏览器验收
└── Makefile
```

---

## 为什么 S0 不装数据库

审计（`../shopkeeper-agent/docs/audit-*.md`）把 30+ 条缺陷收敛到三个根因，第一条是：

> **契约只写在提示词、YAML 和文档里，没有一行代码强制它。**

如果先装数据库、再写检索、最后对前端，这个根因会被固化进每一层。
所以第一个可运行的系统先钉死契约，后面的阶段只能「填实现」，不能改契约。

---

## 与旧系统的端口隔离

| | 旧栈 | rebuild |
|---|---|---|
| 后端 | 8000 | **8100** |
| 前端 | 5173 | **5273** |

两套可以同时跑，方便并排对比（差分验收）。

---

## S0 截图

| 初始态 | 执行完成 |
|---|---|
| ![初始态](docs/screenshots/s0-initial.png) | ![执行完成](docs/screenshots/s0-result.png) |

左：12 步全部「等待」。右：11 步绿色勾 + 耗时，`校正SQL` 显示「未触发」，结果表 4 行。
