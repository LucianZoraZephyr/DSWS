# 电商问数智能体 · 从零重建（教学版）

> 按契约从零搭的第二套实现。旧系统（`../shopkeeper-agent` + `../shopkeeper-agent-frontend`）
> 冻结为 `legacy`，只作反例库与负向夹具来源。

- 从零搭建 / 排障 / 环境变量：[`docs/SETUP-FROM-SCRATCH.md`](docs/SETUP-FROM-SCRATCH.md)
- Docker 部署：[`docs/DOCKER-DEPLOY.md`](docs/DOCKER-DEPLOY.md)
- S7 设计记录：[`docs/S7-HANDOFF.md`](docs/S7-HANDOFF.md)
- 路线图：`../shopkeeper-agent/docs/REBUILD-ROADMAP.md`

## 当前进度

| 阶段 | 产出 | 验收 |
|---|---|---|
| **S0 骨架** | 12 步 SSE 契约 + 前端协议守卫 | `make stage0` / `make stage0-ui` |
| **S1 Schema 契约** | 5 表 / 24 列 / 6 指标；DDL 与真实值校验 | `make stage1` |
| **S2 元知识** | MySQL / ES / Qdrant 三份可检索元知识 + manifest 对账 + 幂等 | `make stage2` |
| **S3 检索** | 20 题黄金集；ES 1 次 `_msearch`、Qdrant 2 次批量、Meta 5 条 SQL | `make stage3` |
| **S4 生成 SQL** | 7 槽位提示词 + 确定性生成器 + AST 静态校验 | `make stage4` |
| **S5 安全边界** | 36 条注入语料全拒 + 11 条正常查询零误杀 + 只读账号 | `make stage5` / `make stage5-live` |
| **S6 失败通道** | 16 个错误码 / 6 类失败 / 校正闭环 / 取消 | `make stage6` / `make stage6-ui` |
| **S7 前端收口** | 契约生成物 + 14 条 SSE 夹具 + 13 条 F 缺陷闭环 | `make stage7` / `make stage7-ui` |
| **S8 性能** | 待做 | — |

当前验收数据：后端 **782 passed / 31 skipped**，前端 **40 passed**，SSE 夹具 **14 条**，
`tsc --noEmit` 零错误。

验收前置：`stage2` / `stage3` 需要三存储；`stage4`–`stage7` 完全离线（不需要 MySQL、LLM 或 API key）；
`stage0-ui` / `stage6-ui` / `stage7-ui` 需要 Chrome 或 Edge。

---

## 快速开始

前置：Python ≥ 3.12、uv、Node.js LTS + npm、GNU Make；Docker（S2/S3 与容器部署）。
Windows 先执行 `chcp 65001`，否则控制台 GBK 可能打断 make 的中文输出。

### 本地开发

```bash
make install          # uv sync --frozen + npm ci

# 终端 1
make dev-backend      # http://127.0.0.1:8100

# 终端 2
make dev-frontend     # http://127.0.0.1:5273
```

健康检查：`curl http://127.0.0.1:8100/api/health` → `{"status":"ok","stage":"S7"}`。

S2 / S3 需要先启动三存储（S0 / S1 / S4–S7 不需要）：

```bash
make docker-env
docker compose up -d mysql elasticsearch qdrant
source scripts/export-docker-env.sh   # 导出 Compose 随机密码；不跨终端保留
```

### Docker 一键部署（推荐）

```bash
make docker-env       # 生成 .env（随机密码；已存在则跳过）
make docker-up        # docker compose up -d --build
curl http://127.0.0.1:8080/api/health
make docker-smoke     # 8 步验收，输出 DOCKER SMOKE PASS
```

| 服务 | 镜像 | 端口 | 职责 |
|---|---|---|---|
| `mysql` | `mysql:8.0` | `127.0.0.1:3306` | 首次启动自动执行 `fixtures/dw.sql` |
| `elasticsearch` | `8.15.3` | `127.0.0.1:9200` | 元知识 + 检索 |
| `qdrant` | `v1.12.4` | `127.0.0.1:6333` | 向量元知识 |
| `init` | 与 backend 同镜像 | 无 | 一次性：等依赖健康 → 开通只读账号 → 校验/重建 S2 元知识 |
| `backend` | `shopkeeper-rebuild/backend:local` | `127.0.0.1:8100` | FastAPI + SSE |
| `frontend` | `shopkeeper-rebuild/frontend:local` | `0.0.0.0:8080` | nginx；唯一对外入口，`/api/` 反代 backend |

> `FRONTEND_BIND` 默认 `0.0.0.0`（要给别的机器访问）；只在本机用时改成 `127.0.0.1`。
> 其余端口只绑定 `127.0.0.1`，不对外暴露。

构建方式：

- **backend**：多阶段（uv 装依赖 → `python:3.12-slim`），非 root（uid 10001）；
  运行镜像只带虚拟环境、`backend/app`、`contracts`、`fixtures`（不含测试）；uvicorn 单 worker。
- **frontend**：`node:20-alpine` 构建 → `nginx:1.27-alpine` 托管 `dist`；
  `/api/` 关闭缓冲，保证 SSE 实时到达。
- **init**：`init` 非零退出会阻止 backend 启动（`service_completed_successfully`）；
  同一份元知识第二次构建必须 `NOOP: 0 writes`。
- 构建源与镜像地址由 `.env` 覆盖（`NPM_REGISTRY` / `UV_INDEX_URL` / `*_IMAGE`）。

常用命令：

```bash
make docker-up        # 构建并启动全部服务
make docker-down      # 停止并移除容器（保留数据卷）
make docker-logs      # 跟踪日志
make docker-smoke     # 完整验收：健康、步骤数、真实查询、nginx 缓冲、只读账号、S2 对账、init 幂等
make docker-reset     # 停容器 + 删数据卷（需输入 yes，不可恢复）；之后用 make docker-up 重建

docker compose up -d backend          # 只改了 .env（例如换 API Key）
docker compose up -d --build backend  # 改了后端源码
```

> `docker compose restart` **不会**读取新的环境变量；改 `.env` 必须 `up -d`。
> 不要执行 `docker compose down -v`（会删数据卷）；需要重建用 `make docker-reset`。

### LLM 接入（可选）

默认使用**确定性生成器**（离线、可复现）。下面三个变量同时非空时，切换为
OpenAI 兼容端点：

| 变量 | 说明 |
|---|---|
| `REBUILD_LLM_BASE_URL` | 端点根地址，生成器自动拼 `/chat/completions` |
| `REBUILD_LLM_API_KEY` | API Key |
| `REBUILD_LLM_MODEL` | 模型名 |
| `REBUILD_LLM_TIMEOUT` | 超时秒数，默认 30 |

示例（DeepSeek 兼容端点）：

```dotenv
REBUILD_LLM_BASE_URL=https://api.deepseek.com
REBUILD_LLM_MODEL=deepseek-flash
REBUILD_LLM_API_KEY=sk-...
```

```bash
docker compose up -d backend   # 重建容器才会读取新环境变量

# 确认生成器已切换（不打印 Key）
docker compose exec backend python -c \
  "from app.generate.provider import HttpLlmGenerator; g=HttpLlmGenerator.from_env(); print(g.model, g.base_url)"
```

- 模型产出的 SQL **仍要过同一套静态校验与安全策略**，不合格降级为拒答。
- 只有配置了 LLM 才会启用 `correct_sql`；否则该步骤显示「未触发」。
- 模型超时 / 非 2xx / 返回非 JSON → 落到既有错误码，流仍以 `result` 或 `error` 收尾。

---

## 重要源码

```text
rebuild/
├── compose.yaml                # 五服务 + 一次性 init
├── Makefile                    # 阶段验收 + Docker 命令
├── docker/                     # backend / frontend Dockerfile、nginx.conf
├── contracts/                  # 唯一事实源：步骤/SSE/Schema/策略/错误码/提示词
├── fixtures/                   # dw.sql、注入语料、失败场景、SSE 夹具
├── backend/
│   ├── app/main.py             # 应用工厂 + HTTP/SSE 入口
│   ├── app/schema_contract.py  # S1 契约检查器 + 文档生成
│   ├── app/build/              # S2 三存储构建 / embedding / manifest
│   ├── app/recall/             # S3 关键词、批量召回、过滤、YAML
│   ├── app/generate/           # S4 上下文、提示词、生成器、静态校验
│   ├── app/security/           # S5 策略、执行器、只读账号
│   ├── app/agent/              # S6 错误码、校正、编排、取消 + 值归一化
│   ├── app/scripts/            # 阶段 CLI + docker_init + 契约生成/录制
│   └── tests/                  # S0–S7 后端测试
├── frontend/
│   ├── src/generated/          # ★ 机器生成，不要手改
│   ├── src/lib/                # SSE 解析、JSON Schema、结果表语义
│   ├── src/components/         # StepRail / ErrorCard / PseudoErrorCard
│   └── tests/                  # 无头浏览器 + SSE 回放服务器
├── scripts/                    # docker-smoke / docker-reset / *-ui 验收脚本
└── docs/                       # 搭建手册、Schema 文档、部署与交接文档
```

HTTP 接口：`GET /api/health`、`GET /api/steps`、`GET /api/errors`、
`POST /api/query`（SSE）、`POST /api/cancel`。

---

## 核心设计（精简）

### 1. 契约是唯一事实源

| 文件 | 内容 |
|---|---|
| `contracts/steps.yaml` | 12 个步骤；`correct_sql` 标为 `conditional` |
| `contracts/sse.schema.json` | 三种事件：`progress` / `result` / `error` |
| `contracts/schema.contract.yaml` | 5 表 / 24 列 / 6 指标的语义契约 |
| `contracts/sql.output.schema.json` | 只允许 `sql` 或 `refusal` 两种形状 |
| `contracts/sql.policy.yaml` | 行数上限、超时、禁词、只读账号 |
| `contracts/errors.yaml` | 16 个错误码 / 3 类失败 / 校正策略 |
| `contracts/prompts/` | 7 个提示词；frontmatter 的 `slots` 是唯一声明 |

后端与前端都从契约派生：步骤目录与错误码由 API 下发，前端事件类型由
`contracts_gen` 生成。

### 2. 问数流水线

```
问题
  -> 上下文 8 阶段（契约离线裁剪；可选 S3 召回）
  -> generate_sql（确定性生成器 / LLM）
  -> validate_sql（AST 静态校验 + AST 安全策略）
  -> [correct_sql]（仅可重试错误；最多 1 次；改完重新校验）
  -> run_sql（只读账号 + 超时）
  -> result / error（流一定以其中之一收尾）
```

### 3. 失败有名字，且长得不一样

`class` 决定界面，不是错误码决定：

| `class` | 界面 | 步骤条 |
|---|---|---|
| `failure` | 红框 + 错误码 + 可复制详情 | 失败步骤红叉 |
| `empty` | 虚线空态卡；`[]` 是合法语义，不标红 | 执行步骤保持绿色 |
| `cancelled` | 中性卡 | 正在跑的步骤标「已取消」 |

### 4. 安全边界

SQL 在**连库之前**过六道检查：可执行注释/词法禁词 → 单语句 → 只读 →
集合运算/危险函数 → 契约派生的表列白名单 → 去注释 + 自动 LIMIT。
执行的是策略**重新序列化后的那条 SQL**；数据库账号 `dw_reader` 只有 `SELECT`，
并叠加 `max_execution_time` 与「不许多语句」。

### 5. 类型保真

`DECIMAL("10")` → `10`，`DECIMAL("10.5")` → `10.5`，`DATE` → ISO 字符串且列类型仍是
`date`；未知类型直接抛错变成 `INTERNAL_ERROR`。SSE 序列化刻意不传 `default=str`。

---

## 数据与存储

- `fixtures/dw.sql` 是**演示数据集**（合成数据，非生产数据）：`fact_order` 115 行、
  `dim_customer` 20、`dim_product` 15、`dim_region` 6、`dim_date` 90。
  Compose 首次初始化时灌入真实 MySQL，查询由只读账号真实执行并聚合。
- S2 只写 `meta_rebuild` / `value_index_rebuild` / `column_info_rebuild` /
  `metric_info_rebuild`，不碰旧系统的库与索引；embedding 默认确定性
  `hash-ngram-v1`，不依赖模型下载。
- 生产问数上下文由 `OfflineContextBuilder`（契约离线裁剪）生成；S3 的 ES/Qdrant
  召回只在 `stage3` 与集成测试中使用，不在线上问数主链路。
- 换真实数据 = 替换 `fixtures/dw.sql` + 同步 `contracts/schema.contract.yaml`，
  再重建 S2，属于数据建模改造而非配置项。

---

## 必须知道的约束

1. **契约先行**：改契约后重新生成前端类型
   `cd backend && uv run python -m app.scripts.contracts_gen`；`--check` 防止生成物过期。
2. **生成物不要手改**：`frontend/src/generated/` 是机器产物。
3. **只动 `*_rebuild` 目标**：S2/S3 不修改旧系统的库、索引与 collection；
   删除本项目数据卷只能用 `make docker-reset`（需输入 yes）。
4. **`.env` 不入库**：`make docker-env` 生成随机密码；改了 `.env` 要 `docker compose up -d`，
   `restart` 不生效。
5. **S2/S3 前置**：三存储已启动；ES 自动回退 `standard` analyzer，S2 默认哈希向量。
6. **S5 实战需要 MySQL**：`REBUILD_DW_RO_PASSWORD` 由环境提供，不在仓库里。
7. **浏览器验收需要 Chrome / Edge**：`playwright-core` 不自带浏览器。
8. **端口隔离**：后端 8100 / 前端 5273；临时验收 8101/5274（S0）、8102/5275（S6）、
   8103/5276（S7），可与旧系统 8000/5173 同时运行。
9. **排障**：先看 [`docs/SETUP-FROM-SCRATCH.md`](docs/SETUP-FROM-SCRATCH.md) 的常见故障表。
