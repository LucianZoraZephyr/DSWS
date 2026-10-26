# S7 交接文档 · 前端收口

> 状态：**已完成（2026-10-26 验收通过；已提交）**。下半部分（回放服务器、
> 13 条浏览器用例、`make stage7` / `stage7-ui`、后端契约测试、正式文档）均已落地。
>
> 这份文档既记录设计决定，也可作为交接材料：已经做完什么、为什么这么做。
> §4 保留的是**接手时的缺口清单**，现已全部关闭；验收证据见 §8。
> README / SETUP-FROM-SCRATCH 已同步 S7 章节。

---

## 0. 一句话

S7 的目标：**前端从契约生成，不从记忆硬编码**。路线图要 5 件事、13 条前端缺陷
（F1–F13）逐条闭环、3 条验收。

---

## 1. 规格出处

| 来源 | 位置 |
|---|---|
| 路线图 S7 | `packages/A020-电商问数智能体实战教程fixed-clean.zip` → `code/shopkeeper-agent/docs/REBUILD-ROADMAP.md` §5「S7 · 前端」 |
| 13 条前端缺陷 F1–F13 | 同上 zip → `docs/audit-frontend-contract.md` §5.1 |
| 后端配合项 B1–B4 | 同上 zip → `docs/audit-frontend-contract.md` §5.2 |
| 空结果语义 FP-8 | 同上 zip → `docs/audit-silent-failure.md` |

路线图 S7 的原文交付物 / 验收：

```
交付物
  - 事件类型联合（zod / TS 生成自 contracts/sse.schema.json）
  - 数字/字符串类型保真（Decimal 不再变字符串）
  - 空数组 → 显式"无数据"；全 null 列 → 显式标注
  - 步骤目录来自 API；conditional 步骤显示"未触发"
  - 去掉固定 820×780 画布，响应式

验收
  make stage7
  # 1) 13 条 F 缺陷各一个 Playwright 用例
  # 2) tsc --noEmit 零错误
  # 3) 用录制好的 SSE fixture 回放，UI 与真实流一致

不做：不改后端契约（除非契约本身有 bug）
Demo：重录三支视频（正常 / 示例 / 失败），与旧版逐帧对比
```

---

## 2. 已完成的部分

### 2.1 后端：数字/字符串类型保真 ✅

**新增 `backend/app/agent/values.py`**

| 数据库类型 | 归一化前 | 归一化后 | JSON |
|---|---|---|---|
| `DECIMAL` | `Decimal("10")` | `int` | `10`（**不是** `"10"`） |
| `DECIMAL` | `Decimal("10.5")` | `float` | `10.5` |
| `DATE` / `DATETIME` | `date(2025,3,1)` | ISO 8601 | `"2025-03-01"` |
| `BLOB` | `bytes` | utf-8 或 hex | 字符串 |
| 驱动没见过的类型 | — | **抛 TypeError** | 变成 `INTERNAL_ERROR` |

关键取舍（写在模块 docstring 里，别改）：

- `Decimal("10")` 走 `int` 而不是 `10.0`——`SUM(INT)` 在 MySQL 里就是 DECIMAL，
  用户心里的「10 件」应该显示成 `10`。
- 遇到不认识的类型**不降级成字符串**。降级就是审计 B4 那个缺陷本身；宁可
  让整次问数变成 `INTERNAL_ERROR`，也不要给用户一份被悄悄改过类型的数据。

**改动 `backend/app/agent/orchestrator.py`**

- `_cell_type()`：`Decimal` 现在推成 `number`（之前落进 `string` 分支）。
- `to_result_payload()`：**先从原始值推列类型，再归一化单元格**。顺序不能反——
  归一化之后 `date` 变成字符串，类型就推成 `string` 了，而契约说的是「这列本来是日期」。

**改动 `backend/app/main.py`**

- `_sse()` 刻意**不传 `default=str`**（旧系统传了，于是 Decimal 静悄悄变字符串）。
  现在漏网的类型会炸成 `INTERNAL_ERROR`，而不是悄悄改类型。

> ⚠️ 顺带修掉一个真 bug：S6 之前 `json.dumps` 没有 `default`，MySQL 的 `Decimal`
> 会让 `json.dumps` 直接抛 `TypeError` → 整次问数变成 `INTERNAL_ERROR`。
> 也就是说 **S6 的生产路径接真库时本来就会炸**。S7 修好了。

### 2.2 契约 → TypeScript 生成 ✅

**新增 `backend/app/scripts/contracts_gen.py`**

```bash
cd backend && uv run python -m app.scripts.contracts_gen          # 生成
cd backend && uv run python -m app.scripts.contracts_gen --check  # 校验新鲜度
```

生成物（**都在仓库里，不要手改**）：

| 文件 | 内容 | 谁用 |
|---|---|---|
| `frontend/src/generated/sse.ts` | 事件联合 + 派生别名 | 前端全部代码 |
| `frontend/src/generated/sse.schema.json` | `contracts/sse.schema.json` 的副本 | 前端运行时校验 |
| `frontend/src/generated/contract.json` | 步骤目录 + 错误码表 | 回放服务器（`frontend/tests/`） |

**生成器的三条规矩**（改生成器前先读）：

1. **只认 JSON Schema 的一个子集**。契约里出现子集之外的关键字 → 生成器**报错**，
   不悄悄忽略。支持的：`$ref`/`$defs`、`type`、`enum`、`const`、`required`、
   `properties`、`additionalProperties`、`items`、`pattern`、`minimum`、
   `maximum`、`oneOf`、`allOf`（含 `if`/`then`/`else`）。
2. **命名规则是确定的**，靠测试钉住：
   - 顶层分支 → `<Def>Event`（`progress` → `ProgressEvent`）
   - 嵌套对象 → `<Def><路径>`（`result.data` → `ResultData`）
   - 数组元素 → `<Def><路径><单数>`（`result.data.columns[]` → `ResultDataColumn`）
   - 数组套数组 → 内层叫 `<外层>Value`（`rows` → `ResultDataRow` → `ResultDataRowValue`）
   - 映射型关键字（`$defs` / `properties`）下面那层是**用户起的名字**，不参与关键字检查。
3. **派生别名从联合推出来**，不抄第二遍：
   ```ts
   export type ProgressStatus = ProgressEvent["status"];
   export type ErrorClass = ErrorEvent["class"];
   export type ColumnType = ResultDataColumn["type"];
   export type CellValue = ResultDataRowValue;
   ```

**前端**（`frontend/src/types.ts`）现在只做**转出**，事件相关的类型全部来自生成物。
手写的只剩不在 SSE 契约里的东西：`StepDef` / `RunStatus` / `ErrorCodeSpec`
（它们各有各的契约文件 `steps.yaml` / `errors.yaml`，由后端通过 API 下发）。

### 2.3 前端运行时校验 ✅

**新增 `frontend/src/lib/json-schema.ts`**：一台**通用** JSON Schema 校验器
（draft 2020-12 的子集），不含任何业务知识。契约数据来自
`generated/sse.schema.json`——**契约仍然是唯一事实源**，这台机器只是通用引擎。

- 支持：`$ref`/`$defs`、`type`（含 `"number" | "null"` 这种数组形式）、`enum`、
  `const`、`required`、`properties`、`additionalProperties: false`、`items`、
  `pattern`、`minimum`、`maximum`、`oneOf`（恰好一个）、`allOf` + `if/then/else`。
- **报错带路径**：`$.data.rows[0][1] 类型应当是 number | null`。
  旧系统只给一句 `str(e)`。
- `oneOf` 不匹配时**挑「type 对得上」的那一支**报它的错误——用户看到的是
  「`$.duration_ms` 缺少必填字段」，不是「没匹配上任何一种」。

### 2.4 前端事件处理：契约驱动 ✅

**`frontend/src/lib/api.ts` 重写**

- `parseAgentEvent(value)` 现在返回**三种**结局（不是两种）：

  | 结局 | 含义 | 前端怎么做 |
  |---|---|---|
  | `{ok: true, event}` | 合法事件 | 处理 |
  | `{ok: false, kind: "unknown_type"}` | **后端加了新事件类型** | **忽略** + `console.warn`（F3） |
  | `{ok: false, kind: "invalid"}` | 已知类型但字段不合法 | 抛 `PROTOCOL_ERROR` → 红框 |

  旧系统的兜底分支是「progress / result / **其余全当 error**」。后端将来加一个
  `type:"meta"`，用户就会看到一次莫名的失败，而红框还**不显示**（`message` 是
  `undefined`）。F3 的修法就是：契约长了一条腿 ≠ 这条消息坏了。

- `SseParser` 多了一个 `onUnknownType` 回调。
- **HTTP 分级文案**（F11）：`httpError(status, detail)` 把
  400/422 → `VALIDATION_ERROR`「问题格式不对」、404 → `NOT_FOUND`、
  5xx → `SERVER_ERROR`「问数服务异常」、网络不可达 → `NETWORK_ERROR`。
  旧系统一律「无法连接问数接口。」

**`frontend/src/App.tsx`**

- 事件处理改成 `switch (event.type)`，不再有「其余全当 error」的兜底。
- 未知事件类型会记一笔并显示一条 `.notice`（`data-testid="unknown-type-notice"`）。
- 结果是「伪 SQL 行」时不渲染表格，改渲染 `PseudoErrorCard`（F5）。
- 阶段标识改成 S7。

### 2.5 结果表：F4 / F6 / F10 / F13 ✅

**新增 `frontend/src/lib/result.ts`**（纯函数，可单测）

| 函数 | 作用 |
|---|---|
| `describeColumns(data)` | 每列：整列是否全 NULL / 有无「数字样式的字符串」 / **契约说 number 但收到字符串** |
| `isNumericString(value)` | `"10"`、`"-3.5"` 算数字样式的字符串；`"1,000"` 不算 |
| `buildCopyPayload(data)` | 复制的是 **JSON**（`{columns, rows}`），数字就是数字 |
| `looksLikePseudoError(data)` | 伪 SQL 行止损：单行 + 单列 + 列名像错误文案 + 值是文本 |

**`frontend/src/components/ResultTable.tsx` 重写**

- F4：`rows` 为空 → `.result-empty` 空态块（`data-testid="result-empty"`），
  整张表**不消失**。
- F6：整列全 NULL → 列头加「无数据」标记（`data-testid="col-all-null"`）+
  顶部一条说明（`data-testid="null-columns-note"`），并说清「这些列全是 NULL，不是 0」。
- F10：数字列走 `Intl.NumberFormat`（千分位、两位小数）；「数字样式的字符串」
  单独标注（`.cell-numeric-string`，带 tooltip），不假装它是数字。
- F13：复制按钮输出 **JSON**（`{columns:[{name,type}], rows:[[...]]}`）。
  旧系统复制 `JSON.stringify(rows)`，而它的行里混着 `{"销量": "10"}`——复制出去
  下游拿到的就是字符串。JSON 至少能把「这是数字」和「这是字符串」如实记下来。

**新增 `frontend/src/components/PseudoErrorCard.tsx`**（F5 止损）

- 刻意**不编造错误码**：契约里的码只有一份（`contracts/errors.yaml`），前端硬编一个
  码出来等于又抄了一份契约。真正的路径上错误码由后端带着 `type:"error"` 发过来。
- 卡片带 `data-pseudo-error="true"`，红框长相。
- 判据刻意保守（宁可漏判，也别把真的 1 行 1 列的表误判成失败）。

### 2.6 SSE 夹具录制 ✅

**新增 `backend/app/scripts/record_sse.py`**

```bash
cd backend && uv run python -m app.scripts.record_sse           # 录制
cd backend && uv run python -m app.scripts.record_sse --check   # 对账
```

- 录的是**真的字节流**：走场景应用（`app.main.create_app`）的同一条 HTTP/SSE 管道，
  模型与数据库是夹具里的替身，其余全是真代码。用 `fastapi.testclient.TestClient`
  拿 `response.text`——就是后端吐出来的字节。
- **只归一化两处**（为了能对账）：`duration_ms` → 固定 `12.5`；`detail.request_id` → 删掉。
  除此以外一个字节都不改。
- `--check` 重新录一遍并与仓库里的文件逐字比对 → 不一致就退出码 1。

**`fixtures/sse/` 共 14 条夹具**：

| id | 来源 | 用途 |
|---|---|---|
| `happy` | 录制 | 正常路径（12 步 + 4 行结果） |
| `corrected` | 录制 | 校正闭环成功（校验SQL 先红后绿） |
| `empty_result` | 录制 | `EMPTY_RESULT`（类别 empty，不标红） |
| `sql_rejected` | 录制 | `SQL_REJECTED` |
| `sql_syntax` | 录制 | `SQL_SYNTAX` |
| `sql_unknown_column` | 录制 | `SQL_UNKNOWN_COLUMN` |
| `llm_refusal` | 录制 | `LLM_REFUSAL` |
| `timeout` | 录制 | `TIMEOUT` |
| `slow` | 录制 | 慢查询（`delay_before_last_ms: 3000`，用来点「停止」） |
| `cancelled` | 录制 | 真实的 `CANCELLED` 事件（取消开关在第一个步骤边界就被拉下） |
| `pseudo_row` | 手写 | **旧系统的伪 SQL 行**（新后端会拒掉它，所以只能手写） |
| `unknown_event` | 手写 | 契约之外的事件类型 |
| `empty_rows` | 手写 | 旧系统的 `data:[]` |
| `null_column` | 手写 | 整列全 NULL（F6；场景夹具的结果都有真值，只能手写） |

夹具格式：

```json
{
  "id": "happy",
  "title": "正常查询",
  "source": "recorded",
  "note": "……",
  "delay_before_last_ms": 0,
  "frames": ["data: {...}", "data: {...}"]
}
```

### 2.7 样式 ✅

`frontend/src/styles.css` 新增：`.result-block`、`.result-note`、`.result-actions`、
`.result-hint`、`.col-flag`、`.col-all-null`、`.cell-numeric-string`、`.notice`、
`.error-card--failure`。

---

## 3. 当前可验证状态

```
cd backend && uv run pytest -q                    → 521 passed, 30 skipped
cd backend && uv run python -m app.scripts.contracts_gen --check
                                                  → S7 CONTRACT GEN PASS（3 个文件）
cd backend && uv run python -m app.scripts.record_sse --check
                                                  → S7 SSE RECORD PASS（14 个夹具）
cd frontend && npm run test                       → 40 passed（26 + 14）
cd frontend && npx tsc --noEmit                   → 零错误
```

---

## 4. 还差什么（接手的人从这里开始）

> 以下 4.1–4.6 的「未写 / 未改」是交接时的状态；现已全部完成。
> 4.7 的两条 commit 已落在 `main` 上。

### 4.1 回放服务器 `frontend/tests/replay-server.mjs`（**已完成**）

一个 Node `http` 服务器，把录制好的夹具按 SSE 吐出来，让前端对着它跑。

- 路由：
  - `GET /api/health` → `{"status":"ok","stage":"S7"}`
  - `GET /api/steps` → 读 `frontend/src/generated/contract.json` 的 `steps`
  - `GET /api/errors` → 读同一文件的 `errors`
  - `POST /api/query` → 按 query 里的 `场景:<id>` 找夹具，逐帧吐出去
  - `POST /api/cancel` → `{"cancelled": true}`
- **约定**：query 写成 `场景:<夹具 id> <真实问题>`（和场景应用同一套约定）。
  找不到夹具 → 422。
- 帧之间 sleep；最后一帧之前 sleep `delay_before_last_ms`（`slow` 夹具靠这个
  让浏览器测试有时间点「停止」）。
- 响应头带 `X-Request-Id`。
- 要能模拟 HTTP 错误：约定 `场景:http_500` / `场景:http_422` → 直接返回对应状态码。

### 4.2 13 条 F 缺陷的浏览器用例 `frontend/tests/s7_frontend.mjs`（**已完成**）

每个 F 一条断言。映射表（**照这张表写**）：

| # | 缺陷 | 怎么测 | 用哪个夹具 |
|---|---|---|---|
| F1 | 取消被算成「完成」 | `[data-testid="run-status"]` 必须是「已取消」，且**不能**出现结果表 | `slow`（点「停止」） |
| F2 | 取消时 running 步骤被标红叉 | `.rail-cancelled` 存在，`.rail-error` 数量为 0 | `slow` |
| F3 | 未知事件类型被当成错误 | 界面必须**正常出结果**，且出现「未知事件类型」的提示条，**没有** `.error-card` | `unknown_event` |
| F4 | 空数组 → 表格消失 | `.result-empty` 可见，文案里有「没有数据」，**没有** `table` | `empty_rows` |
| F5 | 伪 SQL 行被当成功 | `[data-pseudo-error="true"]` 可见，里面是那段文字，**没有** `table` | `pseudo_row` |
| F6 | 全 NULL 列像正常表 | `data-testid="col-all-null"` 存在 + `data-testid="null-columns-note"` 存在 | 造一条（`happy` 没有全 NULL 列）→ **需要在回放服务器里加一条夹具，或用 `happy` 之外的一条** |
| F7 | 错误不带失败步骤 | 错误卡里能看到「失败步骤：校验SQL」（中文名来自 `/api/steps`） | `sql_rejected` |
| F8 | 固定 820×780 画布 | 视口缩到 420px，`document.documentElement.scrollWidth <= innerWidth + 1` | 任意 |
| F9 | 条件步永远灰着 | `.rail-skipped` 存在，文案是「未触发」 | `happy` |
| F10 | 数字不格式化 | 首行「销售额」显示 `128,400`；列头有 `number` 类型标记 | `happy` |
| F11 | 4xx/5xx 一律同一句话 | 5xx → 文案含「服务异常」；422 → 文案含「问题格式不对」 | `http_500` / `http_422` |
| F12 | 步骤条不显示耗时 | `.rail-duration` 存在且非空 | `happy` |
| F13 | 复制把 `"10"` 原样复制 | 单元格里没有「数字样式的字符串」标记（`data-testid="cell-numeric-string"` 不存在） | `happy` |

> F13 的「复制内容本身」用 **Vitest 单测**断言（`buildCopyPayload`），浏览器里
> 断言剪贴板不可靠（无头环境没有剪贴板权限）。这条已在
> `frontend/src/lib/result.test.ts` 里写好了。

> F6 的夹具已补：`record_sse.py` 的 `HANDWRITTEN` 里新增 `null_column`
> （`columns: [{name:"地区",type:"string"},{name:"退款额",type:"null"}]`，
> `rows: [["华北", null], ["华南", null]]`），夹具总数因此是 14 条。

### 4.3 `scripts/stage7-ui.sh`（**已完成**）

照抄 `scripts/stage6-ui.sh` 的结构（隔离端口、按 pid 清理、**不要** `pkill`）：

- 起回放服务器（Node，`node tests/replay-server.mjs --port 8103`）
- 起前端（`VITE_DEV_PROXY_TARGET=http://127.0.0.1:8103 npm run dev -- --port 5276`）
- 跑 `npm run test:ui:s7`

### 4.4 `Makefile`（**已完成**）

```make
stage7:
	@echo "== S7 · 契约生成物新鲜度 =="
	cd backend && uv run python -m app.scripts.contracts_gen --check
	@echo "== S7 · SSE 夹具与真实输出一致 =="
	cd backend && uv run python -m app.scripts.record_sse --check
	@echo "== S7 · 前端契约测试 =="
	cd backend && uv run pytest -q tests/frontend
	@echo "== S7 · 前端协议单测 =="
	cd frontend && npm run test
	@echo "== S7 · 前端类型检查 + 构建 =="
	cd frontend && npm run build
	@echo "== S7 · 全部后端测试 =="
	cd backend && uv run pytest -q
	@echo "S7 PASS"

stage7-ui:
	./scripts/stage7-ui.sh
```

> 路线图把「13 条 Playwright 用例」写在 `make stage7` 里，但浏览器要装 Chrome/Edge。
> 仓库的既有惯例是拆成 `stageN`（离线）+ `stageN-ui`（浏览器），S6 就是这么做的。
> **照惯例拆**，并在文档里说明这个偏离。

### 4.5 后端契约测试 `backend/tests/frontend/`（**已完成**）

建议三个文件：

- `test_contract_gen.py`
  - 生成物与契约一致（跑 `contracts_gen --check`）
  - 生成物里**不出现**手写的枚举副本（`"failure" | "empty" | "cancelled"` 只在
    `ErrorEvent["class"]` 里定义，别处不许再抄）
  - 契约里出现生成器不认识的**约束**关键字 → 生成器报错（造一个假 schema 试）
  - `sse.schema.json` 的 `error.class` 枚举 == `contracts/errors.yaml` 的 `classes`
- `test_sse_fixtures.py`
  - `fixtures/sse/*.json` 每一条帧都是合法 JSON
  - 每一条帧都过 `contracts/sse.schema.json`（`jsonschema`）
  - 帧里的 `step` 都在 `contracts/steps.yaml` 里
  - 帧里的 `error.code` 都在 `contracts/errors.yaml` 里
  - 手写的夹具（`source: "handwritten"`）也要过契约
- `test_type_fidelity.py`
  - `Decimal("10")` 经 `to_result_payload` 后是 `int` 10，`json.dumps` 出来是 `10` 不是 `"10"`
  - `date` → ISO 字符串，列类型仍然是 `date`
  - 整条 SSE 响应体可以 `json.dumps`（**没有** Decimal / date 漏网）
  - 布尔不被当成 int（`isinstance(True, int)` 是 True，这个坑要钉住）

### 4.6 正式文档（**已完成**）

- `README.md`：S7 章节（路线图 S7 那一节的展开）+ 进度表 S7 → ✅
- `docs/SETUP-FROM-SCRATCH.md`：加 §2.10（S7）、阶段表、测试数、排障表、
  已验证 / 未验证清单；§8 指向 S8
- 数字要同步：`462 passed` → 加完前端契约测试后的实际数字

### 4.7 提交（**未做**）

沿用仓库惯例，两条 commit（在 `main` 上）：

1. `S7: 契约驱动前端 + 类型保真 + 13 条缺陷闭环`（实现）
2. `docs: 增加 S7 章节并更新从零搭建与验收手册`（文档）

---

## 5. 设计决定（不要推翻）

| 决定 | 理由 |
|---|---|
| 类型**生成**而不是手写 | 审计第一句结论：「契约只写在提示词、YAML 和文档里，没有一行代码强制它」。前端手写一份 TS 就是抄第二遍 |
| 运行时校验用**通用 JSON Schema 引擎**驱动**契约副本** | 契约仍是唯一事实源；引擎不含业务知识，所以不算「抄第二份」 |
| 未知事件类型 → **忽略**，不报错 | F3。后端加个 `type:"meta"` 不该让用户看到一次莫名失败 |
| 伪 SQL 行止损**不编造错误码** | 前端硬编一个码 = 又抄一份契约。真正的码由后端带 `type:"error"` 发过来 |
| 复制用 **JSON** 而不是 TSV | TSV 丢类型；JSON 至少如实记下「这是数字 / 这是字符串」 |
| `Decimal` → `int`（整数时）而不是 `float` | `SUM(INT)` 在 MySQL 里是 DECIMAL，用户心里的「10 件」该显示成 `10` |
| 未知类型**抛错**而不是降级成字符串 | 降级就是审计 B4 本身；宁可 `INTERNAL_ERROR`，也不要给一份被改过类型的数据 |
| 录制夹具只归一化 `duration_ms` 与 `request_id` | 别的都不动——「录下来的就是后端吐出来的」 |
| 手写夹具只写**真系统产生不了**的三条 | `pseudo_row` / `unknown_event` / `empty_rows`；其余 10 条都是录的 |

---

## 6. 坑（踩过的，别再踩）

1. **`json.dumps` 没有 `default`**：MySQL 的 `Decimal` / `date` 会直接抛 `TypeError`。
   值必须在 `app/agent/values.py` 里归一化，**不要**在 `json.dumps` 上打补丁。
2. **列类型要从原始值推**：先推类型、再归一化值。顺序反了，`date` 就变成 `string` 了。
3. **JSON Schema 的映射型关键字**（`$defs` / `properties`）下面那层是**用户起的名字**，
   不是关键字——生成器的关键字检查要跳过它们，否则第一个属性名就报错。
4. **数组套数组的命名**：`rows` 是「数组的数组」。元素别名按路径推会得到
   `ResultDataRowsRow` 这种鬼名字——所以内层强制叫 `<外层>Value`。
5. **SSE 帧是 `data: {json}` 一行**，`_split_frames` 按 `\n\n` 切；最后一段可能没有结尾空行。
6. **录制的取消夹具**：取消开关必须在**编排器构造之前**就拉下，否则会真的跑一遍
   上下文（`cancel.cancel()` 写在 factory 里，不是写在 runner 之后）。
7. **回放服务器不能有 `pkill`**：只按自己记下的 pid 清理（`scripts/stage6-ui.sh` 是范本）。
8. **`--no-sandbox` 只在 Linux 加**：Windows 上加了没有意义，还关掉一层保护
   （`frontend/tests/chrome.mjs` 的 `launchArgs()` 已经处理好）。
9. **中文输出在 git-bash 里是 GBK**：打印长中文段落会花掉。要看内容就写进文件用
   Read 工具读，不要直接 `cat` 到终端。
10. **`pytest` 的 `ROOT` 层级**：`backend/tests/agent/*.py` 用 `parents[3]`，
    `backend/tests/*.py` 用 `parents[2]`。
11. **导入 conftest**：`backend/tests/` 下没有 `__init__.py`，要 `from conftest import ...`
    （不是 `from .conftest import ...`）。

---

## 7. 涉及文件清单

### 新增
```
backend/app/agent/values.py                     单元格值归一化
backend/app/scripts/contracts_gen.py            契约 → TS 生成器
backend/app/scripts/record_sse.py               SSE 字节流录制器
frontend/src/generated/sse.ts                   ★ 生成物
frontend/src/generated/sse.schema.json          ★ 生成物
frontend/src/generated/contract.json            ★ 生成物
frontend/src/lib/json-schema.ts                 通用 JSON Schema 校验器
frontend/src/lib/result.ts                      结果表语义分析
frontend/src/lib/result.test.ts                 ★ 单测（14）
frontend/src/components/PseudoErrorCard.tsx     F5 止损卡
fixtures/sse/*.json                             14 条 SSE 夹具
docs/S7-HANDOFF.md                              本文件
```

### 改动
```
backend/app/agent/orchestrator.py               _cell_type / to_result_payload
backend/app/main.py                             _sse 不再用 default=str
frontend/src/types.ts                           改成转出生成物
frontend/src/lib/api.ts                         契约驱动的事件解析 + HTTP 分级
frontend/src/lib/sse.test.ts                    按新 API 重写（26）
frontend/src/components/ResultTable.tsx         F4/F6/F10/F13
frontend/src/App.tsx                            switch 事件处理（F3）+ 伪 SQL 止损
frontend/src/styles.css                         新样式
```

### 尚未改动（接手时要动）
```
backend/tests/frontend/                         ★ 新建
backend/tests/agent/test_values.py              ★ 新建
frontend/tests/replay-server.mjs                ★ 新建
frontend/tests/s7_frontend.mjs                  ★ 新建
frontend/package.json                           加 test:ui:s7
scripts/stage7-ui.sh                            ★ 新建
Makefile                                       加 stage7 / stage7-ui
README.md / docs/SETUP-FROM-SCRATCH.md          ★ S7 章节
```

---

## 8. 完成 S7 的验收清单

- [x] `make stage7` 退出码 0（全量后端 521 passed / 30 skipped）
- [x] `make stage7-ui` 退出码 0（14 条断言覆盖 F1–F13；真实浏览器 Edge）
- [x] `tsc --noEmit` 零错误
- [x] 录制夹具回放，界面与真实流一致（14 条夹具；`record_sse --check` PASS）
- [x] `make stage0` / `stage4` / `stage5` / `stage6` 仍然全绿（**不能**把旧阶段跑坏）
- [x] README + SETUP 文档更新，测试数与实际一致（521 后端 / 40 前端 / 14 夹具）
- [x] 两条 commit 落在 `main` 上（实现 0a2d2f3，文档随本次提交）
