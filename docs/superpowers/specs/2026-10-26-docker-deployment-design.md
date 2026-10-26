# Docker 容器化部署设计

日期：2026-10-26
状态：已确认（方案 A）

## 1. 目标

把 rebuild 的前端、后端、MySQL、Elasticsearch、Qdrant 全部封装进 Docker，
通过单机 Docker Compose 一条命令部署。MySQL 由容器自带，首次启动自动导入
`fixtures/dw.sql`，并自动创建只读账号 `dw_reader` 与 S2 元知识。

不修改旧系统，不引入 Swarm / Kubernetes。

## 2. 已确认决策

| 决策 | 选择 |
|---|---|
| Compose 范围 | 全量五服务 + 一次性 init |
| 初始化 | 独立 init 任务；成功后 backend 才启动 |
| 前端交付 | Node 构建，Nginx 静态托管，`/api` 反向代理 |
| LLM | 不内置模型；外部 OpenAI 兼容端点，未配置时确定性生成器回退 |
| 部署形态 | 单机 Docker Compose |
| 密钥 | `.env` + `${VAR:?}` 必需变量校验；`make docker-env` 生成随机密码 |
| 镜像分发 | 本地构建，不要求镜像仓库 |

## 3. 服务拓扑

| 服务 | 来源 | 宿主机端口 | 持久化 | 依赖 |
|---|---|---|---|---|
| `mysql` | `mysql:8.0` | `127.0.0.1:3306` | `mysql_data` | 无 |
| `elasticsearch` | `elasticsearch:8.15.3` | `127.0.0.1:9200` | `es_data` | 无 |
| `qdrant` | `qdrant:v1.12.4` | `127.0.0.1:6333` | `qdrant_data` | 无 |
| `init` | backend 镜像 | 无 | 无 | MySQL/ES healthy；Qdrant 由 init 轮询 |
| `backend` | 本地构建 | `127.0.0.1:8100` | 无 | init 成功退出 |
| `frontend` | 本地构建 | `0.0.0.0:8080` | 无 | backend healthy |

- 对外入口只有 `http://<host>:8080`。
- 其他端口只绑定 `127.0.0.1`，供本机排障与既有验收脚本使用。
- 服务间通过 Compose 默认网络按服务名访问。

## 4. 镜像设计

### 4.1 backend

- 基础镜像 `python:3.12-slim`。
- 构建阶段使用 `ghcr.io/astral-sh/uv` 执行 `uv sync --frozen --no-dev`。
- 运行镜像只包含虚拟环境、`backend/app`、`contracts/`、`fixtures/`。
- 非 root 用户运行；设置 `PYTHONUNBUFFERED=1`、`PYTHONDONTWRITEBYTECODE=1`。
- 启动命令固定单 worker：
  `uvicorn app.main:app --host 0.0.0.0 --port 8100 --workers 1`。
- 健康检查使用 Python 标准库请求 `/api/health`，不额外安装 curl。
- 依赖补充 `cryptography`，支持 MySQL 8 默认 `caching_sha2_password`。

### 4.2 frontend

- 构建阶段使用 `node:20-alpine` 执行 `npm ci` 与 `npm run build`。
- 运行阶段使用 `nginx:1.27-alpine`，只复制 `dist` 与自定义配置。
- `location /api/` 代理到 `http://backend:8100`。
- SSE 必需配置：`proxy_buffering off`、`proxy_cache off`、
  `proxy_http_version 1.1`、`proxy_set_header Connection ""`、长读超时。
- 其余路径 SPA 回退到 `index.html`；提供 `/healthz`。

### 4.3 外部镜像

- ES 使用官方 8.15.3，单节点、关闭 security、512 MB 堆。
- Qdrant 使用 v1.12.4。
- 所有基础镜像与外部镜像名都可通过 `.env` 覆盖，默认值针对当前网络环境
  使用可拉取的镜像源。

## 5. 初始化流程

`init` 容器复用 backend 镜像，执行 `python -m app.scripts.docker_init`：

1. 轮询 MySQL、Elasticsearch、Qdrant，直到全部可连接。
2. 执行 `python -m app.scripts.security --provision-ro-account`
   创建/重置 `dw_reader`，只授予 `dw` 上的 `SELECT`。
3. 执行 `--verify-ro-account`，确认写操作被拒绝。
4. 执行 `python -m app.build_meta_knowledge --verify`：
   - 通过：跳过重建，保证重复部署不重复写。
   - 失败：执行 `--fresh` 重建，再执行 `--verify`。
5. 任何一步失败以非零退出；backend 的
   `depends_on: init: condition: service_completed_successfully` 阻止其启动。

MySQL 首次创建数据卷时，官方 entrypoint 按以下顺序工作：

- `MYSQL_DATABASE=dw`、`MYSQL_USER=didilili`、`MYSQL_PASSWORD` 创建库与普通账号。
- 挂载 `fixtures/dw.sql` 到 `/docker-entrypoint-initdb.d/` 导入表与数据。

数据卷已存在时不会重复导入。

## 6. 配置与密钥

- `.env.example` 入库；`.env` 不入库。
- `make docker-env` 调用 `scripts/generate_docker_env.py`：
  - 已存在 `.env` 时拒绝覆盖，除非显式 `--force`；
  - 生成三个随机密码：`MYSQL_ROOT_PASSWORD`、`MYSQL_PASSWORD`、
    `REBUILD_DW_RO_PASSWORD`；
  - 写入镜像名、npm/PyPI 镜像与可选 LLM 变量占位。
- Compose 对必需变量使用 `${VAR:?message}`，缺失即失败。
- `init` 获得 root 密码与普通账号密码；`backend` 只获得只读账号密码。
- LLM 变量只在 `backend` 注入：
  `REBUILD_LLM_BASE_URL`、`REBUILD_LLM_API_KEY`、`REBUILD_LLM_MODEL`、
  `REBUILD_LLM_TIMEOUT`。

## 7. 数据与运维

- 命名卷：`mysql_data`、`es_data`、`qdrant_data`。
- 长驻服务 `restart: unless-stopped`；`init` 为 `restart: "no"`。
- `docker compose down` 保留数据；`down -v` 删除全部数据，文档明确警告。
- `make docker-reset` 必须二次确认后才执行 `down -v`。
- Makefile 新增：`docker-env`、`docker-up`、`docker-down`、`docker-logs`、
  `docker-smoke`、`docker-reset`。

## 8. 测试与验收

先写 `scripts/docker-smoke.sh`，确认在实现前失败，再实现 Compose。

脚本验收项：

1. `mysql`、`elasticsearch`、`qdrant`、`backend`、`frontend` 均处于运行/健康状态。
2. `http://127.0.0.1:8080/api/health` 返回 `{"status":"ok","stage":"S7"}`。
3. `http://127.0.0.1:8080/api/steps` 返回 12 个步骤。
4. 经 `8080` 发起 `华北销售额`，SSE 以 `result` 收尾且行数大于 0。
5. `backend` 内 `python -m app.scripts.security --verify-ro-account` 退出 0。
6. 以 init 配置一次性运行 `python -m app.build_meta_knowledge --verify` 退出 0。
   `backend` 不持有普通账号/root 密码，保持最小权限。
7. 再次运行 `init`，验证走“跳过重建”路径。

Python 初始化逻辑增加单元测试：verify 成功不重建、失败触发 fresh、fresh 后
再次 verify、失败向上传播。

## 9. 文档

- 新增 `docs/DOCKER-DEPLOY.md`：前置条件、首次部署、日常启停、日志、
  健康检查、数据重置、端口、密钥、资源要求、排障。
- README 增加 Docker 快速开始，并保留本机开发方式。
- 文档说明 ES 使用官方镜像、S2 自动回退 `standard` analyzer；IK 作为后续可选增强。

## 10. 边界与风险

- 后端单 worker：取消注册表是进程内状态，不支持横向扩容。
- 首次构建需要网络拉取镜像与依赖；运行期不需要外网（LLM 除外）。
- ES 单节点、无鉴权，仅适合单机部署；不暴露到公网。
- MySQL 数据卷删除即丢失；重置流程必须显式确认。
- `fixtures/dw.sql` 是当前数据源；真实业务数据替换不在本次范围。
- 不提供镜像仓库推送与多节点编排。
