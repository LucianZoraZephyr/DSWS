# 从零搭建 rebuild（S0–S3 已验证版）

> **适用范围**：这份手册只覆盖当前已经实现的 **S0 骨架、S1 Schema 契约、S2 元知识构建、S3 检索**。
> **代码快照**：`tag s3` 指向 S3 核心实现提交 `93f61c9`（2026-10-25）；本手册是 tag 之后的文档提交，位于 `main`。
> **不包含**：S4 生成 SQL、S5 安全、S6 失败通道、S7 前端收口、S8 性能。
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
```

如果上面的命令全部打印 `PASS`，说明 S0–S3 的代码级验收通过。

> ⚠️ 但“S3 通过”不等于“前端已经能真实问数”。当前 `/api/query` 仍是 S0 的假 SSE，S3 只通过 CLI 调用。详见第 7 节。

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

### 2.7 启动开发服务（可选）

只有想看 S0 的假 SSE / 步骤条时才需要。

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
# {"status":"ok","stage":"S0"}

curl -sS http://127.0.0.1:5273/api/health
# 通过 Vite 代理，同样返回 stage:S0
```

打开 `http://127.0.0.1:5273` 可以看到步骤条依次点亮，但结果是 **S0 假数据**，不是 S3 检索结果。

> ✅ 本机已验证：当前 `5273` 返回 200，`8100/api/health` 和 `5273/api/health` 都返回
> `{"status":"ok","stage":"S0"}`。
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

当前测试总数：

- 后端：`19 + 40 + 16 + 38 = 113`
- 前端：`18`
- 全量后端默认：`90 passed, 23 skipped`（23 条是 S3 真实存储集成测试）
- `make stage3` 的 S3 集成：`38 passed`

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

---

## 7. 已验证 / 未验证清单

### ✅ 本机已验证（2026-10-25）

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
- S3 接入 `/api/query`：尚未接入，前端仍是 S0 假流。
- S4–S8：不存在。

### ℹ️ 明确的边界

- 不支持多轮对话、权限、多数据源（当前阶段刻意不做）。
- S3 不生成 SQL、不执行 SQL、不调用 LLM。
- S2 的 `examples` 是截断策略，不是完整值域。
- `make stage2` 是重建；零写入检查用 `--assert-idempotent`。
- `make stage3` 是只读验收；不会 `--fresh`。

---

## 8. 下一步

当前停点就是 S3。下一步是 **S4：生成 SQL**：

- `generate_sql` + 7 个提示词槽位；
- 输出 JSON schema：`{sql}` 或 `{refusal: {code, reason}}`；
- 10 题基线；
- 静态校验：单语句、只读、列存在、无常量/NULL 投影、排名有 ORDER BY。

在 S4 落地之前，不要承诺“前端能真实问数”，也不要执行不存在的 `make stage4`。
