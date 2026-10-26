# 从零搭建 rebuild（S0–S6 已验证版）

> **适用范围**：这份手册只覆盖当前已经实现的 **S0 骨架、S1 Schema 契约、S2 元知识构建、S3 检索、S4 生成 SQL、S5 安全边界、S6 失败通道**。
> **代码快照**：`tag s3` 指向 S3 核心实现提交 `93f61c9`（2026-10-25）；S4 / S5 与本手册位于 `main`。
> **不包含**：S7 前端收口、S8 性能。
>
> 这份手册刻意把三件事分开写：
>
> - ✅ **本机已验证**：命令真的跑过，退出码为 0；
> - ⚠️ **未在本 shell 验证**：按代码/配置文件整理，但没有端到端实跑；
> - ℹ️ **假设/边界**：当前实现不支持，或需要你按环境调整。

---

## 0. 先看结论

最短路径：

```bash
# 1) 进入 rebuild
cd "<你的目录>/A020-电商问数智能体实战教程fixed/code/rebuild"

# 2) 首次安装（需要网络）
make install

# 3) 不需要三存储的阶段
make stage0
make stage1

# 4) 启动 MySQL / ES / Qdrant（只启动这三个）
cd ../shopkeeper-agent/docker
docker compose up -d mysql elasticsearch qdrant
cd ../../rebuild

# 5) 构建元知识，再跑检索
make stage2
make stage3

# 6) S4：生成 SQL（不需要三存储）
make stage4

# 7) S5：安全边界（不需要三存储）
make stage5

# 8) S6：失败通道（不需要三存储，也不需要 LLM）
make stage6
```

如果上面的命令全部打印 `PASS`，说明 S0–S6 的代码级验收通过。

> S4 单独拿出来也能跑：它只到「生成 + 静态校验」，上下文由契约离线裁剪，
> **不需要 MySQL / ES / Qdrant，也不需要 API key 或网络**。
>
> S5 的门禁也一样不需要数据库：安全策略只依赖契约与 sqlglot。
> 只有只读账号的开通 / 验证与两屏对比需要 MySQL（`make stage5-live`）。
>
> S6 的门禁**既不需要数据库也不需要 LLM**：6 类失败由
> `fixtures/failures/scenarios.yaml` 驱动，只替换模型与数据库两个外部依赖，
> 校验、策略、校正、事件全是真代码。浏览器验收 `make stage6-ui` 需要 Chrome / Edge。

> ⚠️ 但“S3 通过”不等于“前端已经能真实问数”。`/api/query` 从 S6 起跑的是**真的**
> 流水线（上下文 → 生成 → 校验 → 校正 → 执行），但真实召回（S3）与真实数据库
> 还没接上去——没有 MySQL 时它会以一条诚实的 `INTERNAL_ERROR` 收尾，而不是编一份假数据。
> 详见第 2.10 节与第 7 节。

---

## 1. 你需要什么

### 1.1 软件版本

本机实际验证过的版本：

| 软件 | 本机版本 | 说明 |
|---|---|---|
| Python | 3.14.8（uv venv） | `backend/pyproject.toml` 要求 `>=3.12`；系统 `python3` 为 3.12.3 |
| uv | 0.13.1 | 用来安装/运行后端 |
| Node.js | 24.21.0 | 前端 Vite 6 + React 19；建议用当前 LTS |
| npm | 11.19.0 | 与 Node 一起安装 |
| Docker Engine | 29.1.3 | 三存储用 |
| Docker Compose | 2.40.3 | 使用 `docker compose`（v2） |
| Chrome | 已安装 | 只有可选的 `make stage0-ui` 需要 |
| GNU Make | 4.4.1（Windows 便携版）/ 4.x（Linux） | 所有 `make stageN` 验收命令都靠它 |

> ℹ️ 这些是“本机验证版本”，不是严格的最低版本。Python 最低按 `pyproject.toml` 是 3.12；Node 建议不要用过老的版本。

### 1.2 代码目录

当前 `rebuild/` 是一个**独立 git 仓库**，但**没有配置 remote**：

```bash
cd code/rebuild
git remote -v
# 输出为空
```

所以不要照抄“`git clone <repo>`”就以为能拿到全部依赖。你需要保证至少有两块：

```text
A020-电商问数智能体实战教程fixed/
├── code/
│   ├── rebuild/                  # S0–S3 的实现
│   └── shopkeeper-agent/
│       └── docker/               # S2/S3 需要的 MySQL/ES/Qdrant compose
```

- **只复制 `rebuild/`**：S0/S1 可以跑；S2/S3 需要你另外提供兼容的三存储。
- **复制整个 `fixed/`**：按本手册最省事。
- `shopkeeper-agent/` 是旧系统，**只读参考/负向夹具/compose 来源**，不要修改它。
- `rebuild/fixtures/legacy/` 是冻结的旧配置副本，S1 用它证明“旧系统必须红”；不需要旧系统源码也能跑 S1。

### 1.3 端口

| 端口 | 用途 | 备注 |
|---:|---|---|
| `3306` | MySQL | compose 里的 `mysql` |
| `9200` | Elasticsearch | compose 里的 `elasticsearch` |
| `6333` | Qdrant REST | compose 里的 `qdrant` |
| `6334` | Qdrant gRPC | 当前代码不用 |
| `8100` | rebuild 后端 | `make dev-backend` |
| `5273` | rebuild 前端 | `make dev-frontend` |
| `8101` / `5274` | 可选 UI 冒烟临时端口 | `make stage0-ui` |
| `8000` / `5173` | 旧系统 API / Web | **不要为了 S0–S3 启动** |
| `5601` | Kibana | 不需要 |
| `8081` | 旧 embedding 容器 | **不需要**，S2/S3 默认用哈希向量 |

> ℹ️ Vite 没有配置 `strictPort`。如果 `5273` 被占用，`make dev-frontend` 可能自动换到下一个端口；以终端输出为准。后端 `8100` 被占用时 uvicorn 会直接退出。

---

## 2. 从零开始

### 2.1 复制代码

如果拿到的是整个 `fixed/` 目录，直接复制到目标机器即可。下面假设目录为：

```bash
cd "<你的目录>/A020-电商问数智能体实战教程fixed/code/rebuild"
```

确认自己在正确的仓库：

```bash
git status --short
git log --oneline -1
# 期望看到 tag s3 对应的提交，或至少是 S3 之后的提交
```

> ✅ 本机已验证：S3 核心实现提交为 `93f61c9`，`tag s3` 指向该提交；本手册位于其后的 `main` 文档提交，工作区干净。

### 2.2 安装依赖

```bash
make install
```

它做两件事：

```bash
cd backend && uv sync --frozen
cd frontend && npm ci
```

- **首次安装需要网络**：uv 要下载 Python 包，npm 要下载前端依赖。
- 安装完成后，S0/S1 可以离线跑。
- 如果 `uv` 不存在，先安装 uv；如果 `npm ci` 失败，先确认 Node/npm 版本和网络。

> ✅ 本机已验证：`make install` 在 clean-room 里跑过；当前环境依赖已安装。
> ⚠️ 本轮没有重新执行一次全新的 `make install`，只做了现有环境的阶段验收。

### 2.3 先跑 S0 / S1（不需要三存储）

```bash
make stage0
make stage1
```

#### `make stage0` 验什么

- 后端 SSE 契约：`contracts/sse.schema.json`
- 步骤目录：`contracts/steps.yaml`
- 假 SSE 流：12 个步骤，其中条件步骤 `correct_sql` 跳过
- 前端运行时类型守卫 + SSE 解析
- 前端类型检查 + 生产构建

期望：

```text
19 passed
18 passed
S0 PASS
```

> ✅ 本机已验证：`make stage0` 退出码 0。

#### `make stage1` 验什么

- 契约 ↔ DDL/真实数据：5 张表 / 24 列 / 115 行
- 指标口径：GMV `279159.5`、销量 `622`
- 旧系统负向夹具必须红：8 个代码 / 14 条问题
- `docs/SCHEMA-CONTRACT.md` 必须与契约同步

期望：

```text
PASS: 5 张表 / 24 列 / 115 行 / GMV=279159.5 / 销量=622
PASS: 旧系统 14 条问题 / 8 个代码（预期红）
PASS: docs/SCHEMA-CONTRACT.md 与契约一致
S1 PASS
```

> ✅ 本机已验证：`make stage1` 退出码 0，S0+S1 共 59 条后端测试通过。

### 2.4 启动三存储（S2 / S3 的前置）

S2 和 S3 需要 **MySQL + Elasticsearch + Qdrant**。S3 不需要 LLM，也不需要旧的 embedding 容器。

```bash
cd ../shopkeeper-agent/docker

# 只启动这三个，不要 up 全部
docker compose up -d mysql elasticsearch qdrant
```

如果 `3306 / 9200 / 6333` 已经由旧系统 compose 启动，直接探活即可，不要重复 `up`。

首次启动可能比较久，并且需要网络（拉镜像/构建自定义 ES 镜像）：

- `elasticsearch` 会构建一个带 IK 分词插件的自定义镜像；
- MySQL / ES 有 healthcheck；
- Qdrant 没有 healthcheck，需要自己用 HTTP 探活。

等待就绪：

```bash
# MySQL
docker compose exec mysql mysqladmin ping -h 127.0.0.1 -uroot -pdili123

# Elasticsearch
curl -fsS http://127.0.0.1:9200/_cluster/health

# Qdrant
curl -fsS http://127.0.0.1:6333/collections
```

三个都返回成功后再回 `rebuild`：

```bash
cd ../../rebuild
```

#### 重要：不要启动全部服务

```bash
# ❌ 不要这样
docker compose up -d
```

原因：

- `embedding` 容器需要 1.3GB 的 bge 权重，当前环境可能一直 restarting；
- `api` / `web` 是旧系统的 8000 / 5173，不是 rebuild；
- `kibana` 对 S2/S3 没用；
- S2/S3 默认用 `hash-ngram-v1` 哈希向量，不需要 `embedding`。

#### 重要：不要 `down -v`

```bash
# ❌ 不要为了“重置 rebuild”执行
docker compose down -v
```

`down -v` 会删 compose 的 MySQL/ES/Qdrant 数据卷，旧系统数据也会一起没。
默认配置下，S2 的 `--fresh` 只删 **rebuild 自己的目标**，不会删数据卷；两者不是一回事。
如果把目标名覆盖成旧系统目标，`--fresh` 仍会删旧数据，见第 4 节。

> ⚠️ **未在本 shell 验证**：当前审计账户没有 Docker socket 权限，`docker ps` 报
> `permission denied ... /var/run/docker.sock`。上面的 compose 命令是按
> `code/shopkeeper-agent/docker/docker-compose.yaml` 静态整理；本机的 S2/S3 是针对
> **已经运行的三存储**验证的，不是本轮从零 `docker compose up` 验证的。
>
> 如果你遇到同样的权限错误，需要用有 Docker 权限的账号，或把当前用户加入
> `docker` 组后重新登录：
>
> ```bash
> sudo usermod -aG docker "$USER"
> # 重新登录后验证
> docker ps
> ```
>
> 这一步会改系统权限，按你的环境决定是否执行。

### 2.5 S2：构建元知识

```bash
make stage2
```

它内部依次执行：

```bash
cd backend
uv run python -m app.build_meta_knowledge --dry-run
uv run python -m app.build_meta_knowledge --fresh
uv run python -m app.build_meta_knowledge --verify
uv run python -m app.build_meta_knowledge --assert-idempotent
uv run pytest -q
```

#### 期望计数

| 存储 | 目标 | 计数 |
|---|---|---:|
| MySQL | `meta_rebuild` | tables 5 / columns 24 / metrics 6 / column_metrics 9 |
| Elasticsearch | `value_index_rebuild` | 76 docs |
| Qdrant | `column_info_rebuild` | 98 points，256 维 |
| Qdrant | `metric_info_rebuild` | 22 points，256 维 |

期望关键输出：

```text
BUILT: writes=241 ...
S2 VERIFY PASS
NOOP: 0 writes ...
S2 IDEMPOTENT PASS
S2 PASS
```

> ✅ 本机已验证：`make stage2` 退出码 0；`--fresh` 写入 241 条，第二次构建
> `NOOP: 0 writes`，S2 三存储计数与 manifest 一致。

#### `make stage2` 不是“零写入检查”

`make stage2` 每次都会跑 `--fresh`，所以它是一次**重建**，不是幂等检查。
真正的零写入断言是：

```bash
cd backend
uv run python -m app.build_meta_knowledge --assert-idempotent
# 期望：NOOP: 0 writes
```

#### S2 的输入不是旧配置

S2 的事实源是：

```text
contracts/schema.contract.yaml   # 语义契约
fixtures/dw.sql                  # 冻结数据，装进内存 SQLite 计算真实值
```

旧系统的 `conf/meta_config.yaml` 只是 S1 的**负向夹具**，S2 不拿它当事实源。
`fixtures/dw.sql` 也不需要手动导入 MySQL；S2 把它装进内存 SQLite 计算真实值。

#### `examples` 是截断值域

Meta 里的 `examples` 是**排序后的前 10 条**，不是完整值域：

- 完整可检索值在 ES（仅同步策略覆盖的列）；
- S3 输出会显式写 `examples_truncated: true` / `examples_limit: 10`；
- 真正命中的值放在 `matched_values`。

不要把 `examples` 当全集，也不要因为 `examples` 里没有某个值就判断“数据里没有”。

### 2.6 S3：检索

前置：S2 已成功构建，三存储可访问。

```bash
make stage3
```

它内部依次执行：

```bash
cd backend
uv run python -m app.build_meta_knowledge --verify
REBUILD_INTEGRATION=1 uv run pytest -q tests/recall
uv run python -m app.scripts.recall "华北销售额"
uv run pytest -q
```

期望：

```text
38 passed
recall stats: ... es_roundtrips=1 qdrant_roundtrips=2 ... meta_roundtrips=5 ...
90 passed, 23 skipped
S3 PASS
```

> ✅ 本机已验证：`make stage3` 退出码 0；20 题黄金集 20/20；单题 ES `_msearch`
> 1 次、Qdrant batch 2 次、Meta 5 条 SQL。
> ℹ️ 全量 `pytest` 里的 23 条 skip 是“需要真实三存储”的 S3 集成测试；
> `make stage3` 用 `REBUILD_INTEGRATION=1` 单独把它们跑起来。

#### 手动看 S3 的 YAML

```bash
cd backend
uv run python -m app.scripts.recall "华北销售额"
```

- **stdout**：LLM 将看到的 YAML；
- **stderr**：检索统计（ES/Qdrant/Meta 往返、候选数、最终保留数）。

示例统计：

```text
recall stats: query='华北销售额' keywords=3 es_roundtrips=1 qdrant_roundtrips=2
(column=1 metric=1) meta_roundtrips=5 candidates[columns=12 values=1 metrics=4]
final[tables=2 columns=5 metrics=1 values=1]
```

YAML 的关键结构：

```yaml
query: 华北销售额
contract_version: 1
dataset: ...                 # 完整契约 dataset
keywords: [...]
extra_context: ...           # 日期、星期、季度、数据库版本、examples 策略
tables:
  - name: fact_order
    role: fact
    grain: ...
    primary_key: order_id
    foreign_keys: [...]
    measures: [...]          # 表级口径，不能丢
    columns:
      - id: fact_order.order_amount
        name: order_amount
        type: FLOAT
        role: measure
        description: ...
        aliases: [...]
        value_domain: ...
        examples: [...]
        examples_truncated: true
        examples_limit: 10
        matched_values: [...]
metrics:
  - name: GMV
    formula: SUM(fact_order.order_amount)
    expected_value: 279159.5
    relevant_columns: [...]
    aliases: [...]
    rejected_values: [...]
values: [...]
retrieval: ...               # 调试统计；不是业务契约
examples_policy: ...
```

> ℹ️ `retrieval` 里的候选数/分数是调试信息，可能随 Qdrant 索引重建略有变化；
> 真正用于验收的是黄金集、最终上下文和契约字段保真测试。
>
> ℹ️ `extra_context.current_date / weekday / quarter` 来自运行日，跨天会变；
> 同一 `RecallState` 的 YAML 渲染本身是稳定的。需要固定日期时，代码里可以给
> `recall(..., now=...)` 注入时间；CLI 目前不暴露这个参数。

### 2.7 S4：生成 SQL（不需要三存储）

```bash
cd "<你的目录>/A020-电商问数智能体实战教程fixed/code/rebuild"
make stage4
```

`make stage4` 做三件事：

1. **10 题基线**：`python -m app.scripts.generate --baseline`，打印
   「问题 / SQL / 判定」表格。8 题取自 S3 黄金集，2 题是越界题。
2. **生成层测试**：提示词契约（槽位双向校验、缺槽报错）、sqlglot 静态校验
   （每个违规码都有能复现它的负例）、生成与拒答路径、契约/提示词/代码三方对齐。
3. **全量后端测试**。

单独看基线表格：

```bash
make baseline
```

单题：

```bash
cd backend
uv run python -m app.scripts.generate "华北销售额"   # stdout YAML / stderr 统计
```

S4 的输出只有两种形状：`{"sql": "..."}` 或
`{"refusal": {"code": "...", "reason": "..."}}`。静态校验不通过的 SQL 不会以
`sql` 的形态返回，一律降级成带违规码的拒答。

> **要接真实 LLM 时**：设置 `REBUILD_LLM_BASE_URL` / `REBUILD_LLM_API_KEY` /
> `REBUILD_LLM_MODEL` 后用 `HttpLlmGenerator`。它产出的 SQL 一样要过静态校验——
> 换成真模型不会放宽任何一条硬规则。默认路径是确定性生成器，不需要任何配置。

### 2.8 S5：安全边界（不需要三存储）

```bash
cd "<你的目录>/A020-电商问数智能体实战教程fixed/code/rebuild"
make stage5
```

`make stage5` 做四件事：

1. **安全策略门禁**：`python -m app.scripts.security --check`——
   36 条注入语料必须 **100% 被拒绝**（且原因码精确匹配），11 条正常查询必须
   **零误杀**，S4 的 10 题基线 SQL 也必须全部通过策略。
2. **语料判定表**：`python -m app.scripts.security --corpus`——
   逐条打印「期望 / 实际 / 判定」。
3. **安全层测试**：词法层、策略每条规则、执行器（假连接）、只读账号、
   契约 / 代码 / 语料三方对齐、Demo。
4. **全量后端测试**。

单看一条 SQL 的判定：

```bash
cd backend
uv run python -m app.scripts.security --policy "SELECT 1 FROM fact_order; DROP TABLE fact_order"
# 判定：拒绝（原因码 SQL_MULTIPLE_STATEMENTS）
```

#### 为什么 S5 不需要数据库

安全策略的输入只有两样：`contracts/schema.contract.yaml`（白名单）和
`contracts/sql.policy.yaml`（行数上限、超时、禁词、只读账号、错误码）。
两者都是仓库里的文件，所以门禁在任何一台机器上都给出同样的结果。

#### 需要数据库的部分：只读账号

S5 的底线是**执行器只用一个在 `dw` 上只有 SELECT 的账号**。
旧系统 `fixtures/dw.sql` 里那句 `GRANT ALL PRIVILEGES ON dw.* TO 'didilili'@'%'`
正是 D6 能发生的根本原因。

```bash
# 密码由你给，不在仓库里
export REBUILD_DW_RO_PASSWORD='<你自己的密码>'
make stage5-live
```

它做三件事：

1. `--provision-ro-account`：开通 `dw_reader`（`DROP USER IF EXISTS` +
   `CREATE USER` + `GRANT SELECT ON dw.*`——权限集合**恰好**是策略声明的那一项）；
2. `--verify-ro-account`：读 `SHOW GRANTS`，**并且真的去写一次**
   （`UPDATE ... WHERE 1 = 0`，匹配不到任何行），确认数据库确实拒绝；
3. `--demo`：两屏对比——同一段 payload，旧系统 `explain {sql}` 校验通过并
   执行了第二句的 `DROP`，新系统在连库之前就拒绝。

> ℹ️ `--demo` 用的探针表 `_s5_probe` 由它自己建、自己删，被 payload 里的
> `DROP` 删掉正是要演示的效果——它只影响这一张一次性的表。
>
> ⚠️ `--demo` 需要一个**有权限**的账号（默认用 `REBUILD_MYSQL_USER` /
> `REBUILD_MYSQL_PASSWORD`，即旧系统那个 ALL PRIVILEGES 账号），
> 因为它要复现旧系统的行为。新系统自己的执行路径用的是 `dw_reader`。

### 2.9 S6：失败通道（不需要三存储，也不需要 LLM）

```bash
cd "<你的目录>/A020-电商问数智能体实战教程fixed/code/rebuild"
make stage6
```

`make stage6` 做五件事：

1. **失败通道门禁**：`python -m app.scripts.failures --check`——
   6 类失败各跑一遍，结局必须与夹具里 `expect` 写的一致（错误码 / 类别 /
   失败步骤三项都对），外加「路线图点名的 7 个通道码都在错误码表里」。
2. **错误码表**：`python -m app.scripts.failures --table`——
   16 个码，带名称 / 类别 / 可否重试。
3. **失败场景表**：`python -m app.scripts.failures --scenarios`。
4. **失败层测试**：`pytest -q tests/agent`（93 条）+ 前端协议单测 + 构建。
5. **全量后端测试**。

单看一类失败：

```bash
cd backend
uv run python -m app.scripts.failures --scenarios
# | 场景 | 名称 | 期望 | 类别 | 失败步骤 |
# | `sql_rejected` | SQL 被安全策略拒绝 | SQL_REJECTED | failure | validate_sql |
# | `empty_result` | 查询成功但没有数据 | EMPTY_RESULT | empty | run_sql |
```

浏览器验收（需要 Chrome / Edge，Windows 上用 Edge 也行）：

```bash
make stage6-ui
```

它自起一个隔离的**场景应用**（默认 8102）与前端（5275），跑两个无头浏览器
用例：正常路径，以及 6 类失败各一个 + 取消。跑完自动清理。

#### 为什么 S6 不需要 MySQL，也不需要 LLM

6 类失败由 `fixtures/failures/scenarios.yaml` 驱动。**场景应用与生产应用共用
同一套 HTTP / SSE 管道**（`backend/app/main.py` 的 `create_app`），只把编排器的
两个外部依赖换成夹具里的替身：

| | 生产 | 场景 |
|---|---|---|
| 上下文 / 提示词 / 静态校验 / 安全策略 / 校正闭环 / SSE | 真 | 真 |
| 模型 | 真 / 确定性 | 夹具 |
| 数据库 | 只读账号 | 夹具 |

所以屏幕上的每一个红叉都对应一次**真实的判定**，而失败又**可复现**——
不能指望真模型恰好吐出一条坏 SQL。

#### 校正闭环的验收

`make stage6` 里最要紧的一条是：**校正后的 SQL 必须再次过 S5 策略**。
校正器返回一条带写操作的 SQL、或一条 UNION，闭环都必须挡下来
（`tests/agent/test_orchestrator.py` 里有对应的负例）。

### 2.10 启动开发服务（可选）

想看步骤条依次点亮、失败各有各的样子时才需要。

终端 1：

```bash
make dev-backend
# http://127.0.0.1:8100
```

终端 2：

```bash
make dev-frontend
# http://127.0.0.1:5273
```

验证：

```bash
curl -sS http://127.0.0.1:8100/api/health
# {"status":"ok","stage":"S6"}

curl -sS http://127.0.0.1:5273/api/health
# 通过 Vite 代理，同样返回 stage:S6
```

打开 `http://127.0.0.1:5273` 可以看到步骤条依次点亮。**S6 起 `/api/query` 跑的是
真的流水线**（上下文 → 生成 → 校验 → 校正 → 执行），不是 S0 的假流：

- 没配只读账号（`REBUILD_DW_RO_PASSWORD`）时，执行步骤会**失败**，流以一条
  `INTERNAL_ERROR` 收尾——**诚实的失败**，不是编一份假数据；
- 想看 6 类失败各是什么样子，用**场景应用**（只替换模型与数据库）：

```bash
# 终端 1
cd backend && uv run python -m app.scenarios.app --port 8102

# 终端 2
cd frontend && VITE_DEV_PROXY_TARGET=http://127.0.0.1:8102 npm run dev
```

然后在页面上提问「场景:sql_rejected 华北销售额」之类的话（场景前缀由场景应用
剥掉，流水线拿到的仍是真实问题）。

> ✅ 本机已验证：当前 `5273` 返回 200，`8100/api/health` 与 `5273/api/health` 都返回
> `{"status":"ok","stage":"S6"}`。
>
> ℹ️ Vite 配置了 `host: "0.0.0.0"`，所以局域网可以用
> `http://<宿主机IP>:5273` 打开；`/api` 由 Vite 在服务器侧代理到
> `127.0.0.1:8100`。后端本身默认只监听 `127.0.0.1`，不直接暴露到局域网。

---

## 3. 每个阶段到底验了什么

| 阶段 | 命令 | 需要三存储 | 核心验收 |
|---|---|:---:|---|
| S0 | `make stage0` | 否 | SSE 契约、步骤目录、前端解析/构建；19 后端 + 18 前端 |
| S1 | `make stage1` | 否 | 5 表/24 列/6 指标契约；旧系统 8 个错误码必须红；40 测试 |
| S2 | `make stage2` | 是 | 三存储构建、manifest、计数对账、`NOOP: 0 writes`；16 测试 |
| S3 | `make stage3` | 是 | 4 路批量召回 + merge/filter/context；20 题黄金集；38 测试 |
| S4 | `make stage4` | 否 | 7 个提示词槽位校验 + sqlglot AST 静态校验 + 10 题基线；113 测试 |
| S5 | `make stage5` | 否 | AST 级安全策略 + 36 条注入语料 100% 拒绝 + 11 条零误杀；173 测试 |
| S5 实战 | `make stage5-live` | 是（只要 MySQL） | 只读账号开通 / 验证 + 两屏对比（旧执行 / 新拒绝） |
| S6 | `make stage6` | 否 | 错误码表 + 6 类失败 + 校正闭环 + 取消；93 测试 |
| S6 浏览器 | `make stage6-ui` | 否（要浏览器） | 6 类失败各一个用例 + 取消，UI 状态互不相同 |

当前测试总数：

- 后端：`19 + 40 + 16 + 38 + 113 + 173 + 93 = 492`（其中 30 条按需跳过，
  实际执行的断言是 462 条）
- 前端：`23`（协议单测）+ 2 个无头浏览器用例
- 全量后端默认：`462 passed, 30 skipped`（23 条是 S3 真实存储集成测试，
  5 条是 S4 里「输出形状不是 sql_or_refusal 的提示词」的按需跳过，
  2 条是 S5 里「这一题是拒答，不产生 SQL」的按需跳过）
- `make stage3` 的 S3 集成：`38 passed`
- `make stage4` 的生成层：`108 passed, 5 skipped`
- `make stage5` 的安全层：`171 passed, 2 skipped`
- `make stage6` 的失败层：`93 passed`

---

## 4. 环境变量

默认值来自 `backend/app/build/config.py`，面向本机教学环境：

| 变量 | 默认 | 用途 |
|---|---|---|
| `REBUILD_MYSQL_HOST` | `127.0.0.1` | MySQL 主机 |
| `REBUILD_MYSQL_PORT` | `3306` | MySQL 端口 |
| `REBUILD_MYSQL_USER` | `didilili` | 普通用户 |
| `REBUILD_MYSQL_PASSWORD` | `dili123` | 普通用户密码 |
| `REBUILD_MYSQL_ROOT_USER` | `root` | 建库/授权用 |
| `REBUILD_MYSQL_ROOT_PASSWORD` | `dili123` | root 密码 |
| `REBUILD_META_DB` | `meta_rebuild` | Meta 库名 |
| `REBUILD_ES_URL` | `http://127.0.0.1:9200` | ES 地址 |
| `REBUILD_ES_INDEX` | `value_index_rebuild` | ES 索引名 |
| `REBUILD_QDRANT_URL` | `http://127.0.0.1:6333` | Qdrant 地址 |
| `REBUILD_COLUMN_COLLECTION` | `column_info_rebuild` | 字段 collection |
| `REBUILD_METRIC_COLLECTION` | `metric_info_rebuild` | 指标 collection |
| `REBUILD_VECTOR_SIZE` | `256` | 向量维度 |
| `REBUILD_EMBEDDING_PROVIDER` | `hash-ngram-v1` | 当前只支持这个值 |
| `REBUILD_MAX_VALUE_DOCS` | `1000` | 每列最多同步多少值到 ES |
| `REBUILD_REQUEST_TIMEOUT` | `15` | 外部请求超时（秒） |
| `REBUILD_DW_RO_PASSWORD` | **无默认** | S5 只读账号（`dw_reader`）的密码——**必须由部署环境提供**，仓库里不存秘密 |
| `REBUILD_LLM_BASE_URL` | 无 | S4 接真实 LLM 时的 OpenAI 兼容端点 |
| `REBUILD_LLM_API_KEY` | 无 | 同上 |
| `REBUILD_LLM_MODEL` | 无 | 同上 |

> `REBUILD_DW_RO_PASSWORD` 没有默认值是**故意的**：只读账号的密码属于秘密，
> 不进仓库、不进契约、不进任何生成物。缺它时 `make stage5-live` 会**直接报错**，
> 不会悄悄连成别的账号。

### 自定义目标时，S2 和 S3 必须用同一组变量

`make stage2` 和 `make stage3` 都从环境变量读目标名。如果你这样构建：

```bash
export REBUILD_META_DB=meta_rebuild_foo
export REBUILD_ES_INDEX=value_index_rebuild_foo
make stage2
```

那么跑 S3 时也必须保留同样的环境变量：

```bash
export REBUILD_META_DB=meta_rebuild_foo
export REBUILD_ES_INDEX=value_index_rebuild_foo
make stage3
```

否则 `--verify` 会去默认目标找数据，直接失败。

### 默认值为什么安全

默认目标都带 `_rebuild` 后缀，与旧系统隔离：

```text
旧系统：meta / value_index / column_info_collection / metric_info_collection
rebuild：meta_rebuild / value_index_rebuild / column_info_rebuild / metric_info_rebuild
```

但这只是**默认配置**的保证。代码没有强制“目标名必须以 `_rebuild` 结尾”：

```bash
# ❌ 绝对不要把目标指到旧系统
export REBUILD_META_DB=meta
export REBUILD_ES_INDEX=value_index
```

`--fresh` 会 `DROP DATABASE` / 删索引 / 删 collection。运行前先看：

```bash
cd backend
uv run python -m app.build_meta_knowledge --dry-run
```

确认输出的 `targets:` 是 rebuild 目标，再执行 `--fresh`。

---

## 5. 隔离检查

可选，但建议在第一次跑 S2 前后各看一次。当前本机已验证的计数：

| 存储 | 旧系统 | rebuild |
|---|---|---|
| MySQL | `meta`：5 / 24 / 2 / 2 | `meta_rebuild`：5 / 24 / 6 / 9 |
| ES | `value_index`：75 docs | `value_index_rebuild`：76 docs |
| Qdrant | `column_info_collection`：98 points，1024 维 | `column_info_rebuild`：98 points，256 维 |
| Qdrant | `metric_info_collection`：8 points，1024 维 | `metric_info_rebuild`：22 points，256 维 |

> ✅ 本机已验证：执行 `make stage2` / `make stage3` 后，旧系统计数没有变化。
>
> 注意：旧系统 Qdrant 是 1024 维，rebuild 是 256 维；这是两套独立 collection，
> 不要拿维度不同来判断“坏了”。

---

## 6. 常见故障

### Windows 相关

| 症状 | 原因 | 处理 |
|---|---|---|
| `UnicodeEncodeError: 'gbk' codec can't encode character` | Windows 控制台默认代码页是 GBK（cp936），项目里的中文与 `↔` 之类符号 print 不了 | CLI 内部已把 stdout/stderr 切到 UTF-8（`backend/app/cli.py`）；**make 自己 echo 的那几行中文**仍按控制台代码页渲染，先 `chcp 65001` 再跑 make |
| `make: command not found` | 没装 make | `winget install ezwinports.make`（便携版，不需要管理员），装完**重开一个终端**让 PATH 生效 |
| `docs\SCHEMA-CONTRACT.md 已过期` | 生成文档时用了 `os.sep`，Windows 下路径分隔符是 `\`，与仓库里 Linux 生成的版本对不上 | 已修（`schema_contract._path_label` 改用 `as_posix()`）。若仍出现，跑 `python -m app.schema_contract --write-doc` 重新生成 |

| 现象 | 原因 | 处理 |
|---|---|---|
| `permission denied ... /var/run/docker.sock` | 当前用户没有 Docker socket 权限 | 用有权限的账号，或加入 `docker` 组后重新登录 |
| `Connection refused` 到 3306/9200/6333 | 三存储没起，或端口没映射 | 按 2.4 启动，`curl` 探活 |
| `S2 FAIL: ... unknown analyzer [ik_max_word]` | 用了普通 ES 镜像，不是带 IK 的 compose 镜像 | 用 `code/shopkeeper-agent/docker` 里的 `elasticsearch` 构建 |
| `S2 FAIL: ... Access denied` | MySQL 账号/密码不对 | 检查 `REBUILD_MYSQL_*`；默认来自 compose |
| `S2 FAIL: ... count mismatch` | 目标里有旧数据，或 manifest 过期 | 先 `--dry-run` 确认 targets，再 `--fresh` |
| `S2 VERIFY FAIL` | S2 没构建好，或 S3 用了不同 env | 先 `make stage2`；确认 S2/S3 环境变量一致 |
| `make stage3` 报 `No rule to make target` | 代码是 S3 之前的旧 checkout | 更新到 `tag s3` |
| `ModuleNotFoundError: app.recall.pipeline` | 工作区只有未完成的 S3 脚手架 | 更新到 `tag s3` |
| `make dev-backend` 启动即退出 | 8100 被占用 | 停掉占用进程；或改 Makefile/uvicorn 端口 |
| 前端打开后 API 502 | 后端没起，或代理目标不对 | 先起 8100；检查 `vite.config.ts` 的代理 |
| 前端端口不是 5273 | 5273 被占用，Vite 自动换端口 | 看终端输出；或给 Vite 加 `--strictPort` |
| `examples` 里没有某个值 | `examples` 只保留排序后前 10 条 | 查契约 `value_domain` / ES / S3 `matched_values` |
| S3 候选统计和上次不同 | Qdrant 索引重建后调试统计变化 | 看黄金集和最终上下文；`retrieval` 不是契约 |
| `make stage5-live` 报「缺少环境变量 REBUILD_DW_RO_PASSWORD」 | 只读账号的密码**故意不在仓库里** | `export REBUILD_DW_RO_PASSWORD='<你自己的密码>'` 再跑 |
| `make stage5-live` 连不上 3306 | MySQL 没起 | 按 2.4 只启动 mysql；`dw` 库要由 `fixtures/dw.sql` 装载 |
| `make stage5-live` 的 `--demo` 报权限不足 | `--demo` 需要一个有权限的账号来复现旧系统行为 | 确认 `REBUILD_MYSQL_USER` / `REBUILD_MYSQL_PASSWORD` 是那个 ALL PRIVILEGES 账号 |
| `make stage6-ui` 报「找不到 Chrome/Edge」 | playwright-core 不自带浏览器 | 装 Chrome 或 Edge，或设 `CHROME_PATH=/path/to/chrome` |
| `make stage6-ui` 报端口被占用 | 8102 / 5275 被别的进程占了 | `API_PORT=8202 WEB_PORT=5375 make stage6-ui` |
| 页面上问什么都报 `INTERNAL_ERROR` | 没配只读账号，执行阶段连不上库 | 这是**诚实的失败**；配 `REBUILD_DW_RO_PASSWORD` 并跑 `make stage5-live` 开通账号，或用场景应用看 6 类失败 |

---

## 7. 已验证 / 未验证清单

### ✅ 本机已验证（2026-10-26，Windows 11 + Git Bash）

- `make stage0`：19 后端 + 18 前端 + 构建，PASS。
- `make stage1`：契约/DDL/数据 + 旧系统 8 个错误码 + 59 条 S0/S1 测试，PASS。
- `make stage4`：10 题基线 10/10（其中 2 道显式拒答）；生成层 108 passed /
  5 skipped；全量后端 198 passed / 28 skipped；PASS。
- S4 的静态校验：每个违规码都有能复现它的负例（多语句 / UPDATE / DELETE /
  DROP / INTO OUTFILE / 未知表 / 未知列 / 常量投影 / NULL 投影 / 缺 ORDER BY /
  缺 LIMIT / 语法错误 / 空 SQL）。
- `make stage5`：注入语料 36/36 按预期拒绝（原因码精确匹配）+ 零误杀 11/11 +
  S4 基线 8/8 通过策略；安全层 171 passed / 2 skipped；全量后端 369 passed /
  30 skipped；PASS。
- S5 的策略：每个原因码都有能复现它的语料（多语句 / 注释绕过 / 写操作 /
  集合运算 / 危险函数 / 白名单外表 / 白名单外列）；执行的 SQL 与被校验的 SQL
  是同一条（去注释 + 自动 LIMIT）。
- `make stage6`：6 类失败各跑一遍，结局与夹具逐项吻合（错误码 / 类别 / 失败
  步骤）；错误码表 16 个码，路线图点名的 7 个全在；失败层 93 passed；
  全量后端 462 passed / 30 skipped；PASS。
- `make stage6-ui`（真实浏览器，Edge）：6 类失败给出 **6 种互不相同**的界面状态
  （SQL_REJECTED / SQL_SYNTAX / SQL_UNKNOWN_COLUMN / EMPTY_RESULT /
  LLM_REFUSAL / TIMEOUT），空结果**不**标红，取消后状态是「已取消」且步骤条
  停止推进；同一轮里 S0 的正常路径冒烟也一起通过。
- S6 的校正闭环：校正后的 SQL **重新过**静态校验与安全策略——校正器返回写操作
  或 UNION 都被挡下（`SQL_REJECTED`），执行器一次都没被调用；校正最多 1 次。
- 依赖补全：`make`（winget `ezwinports.make` 4.4.1）、`frontend/node_modules`
  （`npm ci`）、后端新增 `sqlglot`（AST 校验）与 `jsonschema`（输出形状校验）。

### ✅ 本机已验证（2026-10-25，Linux）

- `make stage0`：19 后端 + 18 前端 + 构建，PASS。
- `make stage1`：契约/DDL/数据 + 旧系统 8 个错误码 + 59 条 S0/S1 测试，PASS。
- `make stage2`：`--fresh` 写入 241 条；计数 5/24/6/9、ES 76、Qdrant 98/22；
  `--verify` PASS；`--assert-idempotent` 输出 `NOOP: 0 writes`；PASS。
- `make stage3`：38 条 S3 测试（含 20 题黄金集）；ES 1 / Qdrant 2 / Meta 5；
  全量后端 90 passed / 23 skipped；PASS。
- 前端：`5273` 返回 200；`5273/api/health` 与 `8100/api/health` 返回 `stage:S0`。
- 旧系统计数未变（见第 5 节）。
- 代码：`tag s3` 指向 S3 核心实现 `93f61c9`；本手册提交在 `main` 上，工作区干净。

### ⚠️ 未在本 shell 端到端验证

- `docker compose up -d mysql elasticsearch qdrant` 的**从零启动**：当前 shell 没有
  Docker socket 权限；S2/S3 是针对已运行的三存储验证的。
- 完全空机器上的 `make install` + S0–S3 一条龙：S0/S1/S2 做过 clean-room，
  S3 是在当前环境验证的；没有在本轮重新做一次全新 clean-room。
- `make stage0-ui` 的真实 Chrome 冒烟：脚本存在，但本轮没有重新跑。
- 带鉴权的 ES/Qdrant：当前代码只发 URL，没有 API key/TLS 参数，不能只改 URL。
- 真实 bge embedding：当前只支持 `hash-ngram-v1`，S3 召回质量有限。
- 真实召回（S3）接入 `/api/query`：S6 起 `/api/query` 跑的是**真的**流水线
  （上下文 → 生成 → 校验 → 校正 → 执行），但上下文仍由契约**离线裁剪**，
  没有接 S3 的向量召回。接真召回需要三存储。
- 真实数据库接入 `/api/query`：默认执行器是 S5 的只读账号执行器；没有
  `REBUILD_DW_RO_PASSWORD` 时它会诚实地以 `INTERNAL_ERROR` 收尾。
  端到端跑通需要 MySQL + `make stage5-live`（本轮未跑）。
- S4 的真实 LLM 通路（`HttpLlmGenerator`）：接口与校验都在，但没有对着
  真实端点跑过；默认走的是确定性生成器。
- S5 的只读账号与两屏对比（`make stage5-live`）：当前 shell 没有运行中的
  MySQL，因此**没有**在这里端到端跑过。离线门禁（策略 / 语料 / 零误杀）
  与执行器、账号的假连接测试都已通过。
- S6 的真实 LLM 校正通路（`LlmCorrector` + 真实端点）：接口、提示词与校验都在，
  但没有对着真实端点跑过；默认走的是确定性生成器，校正器在没配 LLM 时**不假装
  能校正**——`correct_sql` 步骤会显式标成「未触发」。
- S7–S8：不存在。

### ℹ️ 明确的边界

- 不支持多轮对话、权限、多数据源（当前阶段刻意不做）。
- S3 不生成 SQL、不执行 SQL、不调用 LLM。
- S2 的 `examples` 是截断策略，不是完整值域。
- `make stage2` 是重建；零写入检查用 `--assert-idempotent`。
- `make stage3` 是只读验收；不会 `--fresh`。

---

## 8. 下一步

当前停点就是 S6。下一步是 **S7：前端收口**——「从契约生成，不从记忆硬编码」：

- 前端的 TS 类型从 `contracts/sse.schema.json` 自动生成，并加 diff 检查
  （现在还是手写版，靠 Vitest 用例 + 后端契约测试共同守住）；
- 步骤目录、错误码表都从 `/api/steps` / `/api/errors` 拉，前端零硬编码
  （S6 已经做到「码的含义不硬编码」，但**类型**还是手写的）；
- 13 个界面缺口按 `audit-frontend-contract.md` 逐条收口；
- 补齐 `docs/screenshots/` 里的失败态截图（路线图的 Demo 要录 5 种失败态）。

S6 已经把「只读 / 单语句 / 列存在 / 无常量投影 / 排名有 ORDER BY」做成代码级
守卫（S4），又叠了一层**安全策略**（S5：白名单、只读账号、超时、自动 LIMIT、
一整套注入语料），最后把它们**接进一条真的链路**（S6：生成 → 校验 → 校正 →
再校验 → 执行），每一种失败都有名字、有 UI、有测试。

> 但仍然不要承诺「前端能真实问数」：`/api/query` 跑的是真流水线，**上下文却是
> 契约离线裁剪的**，真实召回（S3）与真实数据库还没接上。前端要能问真问题，
> 还要等 S7 与 S8。
