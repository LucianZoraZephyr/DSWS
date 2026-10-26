# Docker 部署

把 rebuild 的前端、后端、MySQL、Elasticsearch、Qdrant 和一次性初始化任务
放进单机 Docker Compose。对外入口只有 `http://<host>:8080`。

## 1. 前置条件

- Docker Engine / Docker Desktop 可用，`docker compose version` 为 v2。
- 宿主机需要 GNU Make、Python 3、Bash 和 curl（Windows 上用 Git Bash）
  来运行 Make / smoke 命令。
- 首次构建需要网络：拉取基础镜像、npm 包、Python 包。
- 建议至少 8 GB 内存（ES 堆固定 512 MB）。
- 端口默认 `8080`（前端）、`8100`（后端）、`3306`、`9200`、`6333`；
  都可在 `.env` 的 `*_HOST_PORT` 中修改。其中只有前端 `8080` 监听所有网卡，
  其余端口都只绑定 `127.0.0.1`。

## 2. 首次部署

```bash
make docker-env    # 生成 .env；已有则跳过
make docker-up     # 构建并启动五服务 + 一次性 init
```

`make docker-up` 会等待：

1. MySQL / Elasticsearch healthy，Qdrant 可连接；
2. `init` 创建 `dw_reader` 并验证只有 SELECT；
3. `init` 校验 S2 元知识，缺失则 `--fresh` 构建后再校验；
4. 只有 `init` 退出 0，backend 才启动；
5. backend healthy 后 frontend 才启动。

检查：

```bash
curl -fsS http://127.0.0.1:8080/api/health
curl -fsS http://127.0.0.1:8080/api/steps
docker compose ps
```

完整验收：

```bash
make docker-smoke
```

它会检查五服务运行、12 个步骤、经 8080 的真实查询、只读账号、S2 对账，
并再次运行 `init` 证明“跳过重建”。

## 3. 日常运维

```bash
make docker-logs                  # 跟踪全部日志
docker compose logs -f backend    # 只看后端
make docker-down                  # 停止并删除容器，保留数据卷
make docker-up                    # 再次启动
```

`init` 是一次性任务，正常退出后 `docker compose ps -a` 会显示为 Exited (0)。
需要重新执行初始化时（三存储必须已在运行）：

```bash
docker compose run --rm --no-deps init
```

## 4. 密钥与 LLM

- `.env` 不入库；`.env.example` 是模板。
- `MYSQL_ROOT_PASSWORD` / `MYSQL_PASSWORD` 只注入 `init` 和 MySQL；
  `backend` 只拿 `REBUILD_DW_RO_PASSWORD`。
- **宿主 CLI**：`make stage2` / `make stage3` / `make stage5-live` 不会自动读
  `.env`；先 `source scripts/export-docker-env.sh`，它会把 Compose 的随机密码和
  宿主机端口导出为 `REBUILD_*` 变量，成功时不打印任何内容。
- 轮换只读密码：改 `.env` 的 `REBUILD_DW_RO_PASSWORD`，然后：

```bash
docker compose run --rm --no-deps init \
  python -m app.scripts.security --provision-ro-account
docker compose up -d --force-recreate backend
```

- 启用外部 LLM：在 `.env` 填 `REBUILD_LLM_BASE_URL`、`REBUILD_LLM_API_KEY`、
  `REBUILD_LLM_MODEL`，然后重启 backend：

```bash
docker compose up -d --force-recreate backend
```

  三个变量留空时使用确定性生成器；不会下载模型。

## 5. 数据与重置

命名卷：`mysql_data`、`es_data`、`qdrant_data`。Compose 会按项目名
`shopkeeper-rebuild` 加前缀，实际名称是
`shopkeeper-rebuild_mysql_data`、`shopkeeper-rebuild_es_data`、
`shopkeeper-rebuild_qdrant_data`。`make docker-down` 保留它们。

重置会删除全部业务数据：

```bash
make docker-reset   # 输入 yes 才执行 down -v
```

不要在没有确认的情况下执行 `docker compose down -v`。

## 6. 排障

| 症状 | 原因 | 处理 |
|---|---|---|
| 镜像拉取失败 | 默认镜像源不可达 | 改 `.env` 的 `*_IMAGE` 为可访问镜像源，再 `make docker-up` |
| `init` 退出 1 | MySQL/ES 未就绪、密码不一致、S2 校验失败 | `docker compose logs init`；必要时 `docker compose run --rm --no-deps init` 重跑 |
| `Access denied for user 'dw_reader'` | init 没跑完或密码已轮换 | `docker compose logs init`；重新执行 `init` |
| ES 容器反复退出（Linux） | `vm.max_map_count` 太小 | `sudo sysctl -w vm.max_map_count=262144` |
| 8080 被占用 | 宿主机端口冲突 | 在 `.env` 改 `FRONTEND_HOST_PORT` |
| 页面能开但 SSE 不流式 | Nginx 缓冲 | 确认 `docker/nginx.conf` 的 `proxy_buffering off`；重建 frontend |
| `S2 FAIL: ... unknown analyzer [ik_max_word]` | 官方 ES 没有 IK | S2 会自动回退 `standard`；若仍失败看 init 日志 |
| 查询一直 `INTERNAL_ERROR` | init 未成功，或只读账号不可用 | 先看 `docker compose ps` 与 `docker compose logs init` |

## 7. 边界

- backend 固定单 worker：取消注册表是进程内状态，不能多副本。
- ES 单节点、关闭鉴权，只适合单机；不要暴露到公网。
- `fixtures/dw.sql` 是当前数据源；替换真实数据不在本部署范围内。
- 不提供镜像仓库推送、多节点编排或高可用。
