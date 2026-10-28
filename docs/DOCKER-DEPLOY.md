# Docker 部署

把 rebuild 的前端、后端、MySQL、Elasticsearch、Qdrant 和一次性初始化任务
放进单机 Docker Compose。对外入口只有 `http://<host>:8080`。

## 1. 前置条件

- Docker Engine / Docker Desktop 可用，`docker compose version` 为 v2。
- 宿主机需要 GNU Make、Python 3.8+、Bash 和 curl（Windows 上用 Git Bash）
  来运行 Make / smoke 命令。宿主机上的两个脚本（`generate_docker_env.py`、
  `smoke_checks.py`）用到 `Path.unlink(missing_ok=True)`——那是 3.8 才有的；
  `scripts/find-python.sh` 会挑一个**版本够新**的解释器，太老的（2.7 / 3.6
  这类"跑得起来却跑不了脚本"的）会被跳过，并在报错里点名它的版本。
- 首次构建需要网络：拉取基础镜像、npm 包、Python 包。
- 建议至少 8 GB 内存（ES 堆固定 512 MB）。
- **Qdrant 镜像必须是 v1.5.0 或更新**：健康检查打的是 `/readyz`，这个端点
  v1.5.0 才有。老镜像上它一直返回 404，容器永远不 healthy，`init` 就不会
  启动，整个栈卡在"Qdrant 没就绪"。默认的 `QDRANT_IMAGE` 是 v1.12.4。
- 端口默认 `8080`（前端）、`8100`（后端）、`3306`、`9200`、`6333`；
  都可在 `.env` 的 `*_HOST_PORT` 中修改。其中只有前端 `8080` 监听所有网卡，
  其余端口都只绑定 `127.0.0.1`。
- 前端监听地址由 `FRONTEND_BIND` 控制，默认 `0.0.0.0`（局域网可访问）。
  只在本机使用时改成 `127.0.0.1`。
- **端口与本机开发服务冲突**：容器后端也映射到宿主机 `8100`，
  和 `make dev-backend` 是同一个端口。两者不要同时跑，见 2.1 节。
- 首次构建要拉 npm 包和 Python 包。默认走官方源；国内网络在 `.env` 里改
  `NPM_REGISTRY` / `UV_INDEX_URL`（`.env.example` 已给出可用的镜像地址）。

## 2. 首次部署

```bash
make docker-env    # 生成 .env；已有则跳过
make docker-up     # 构建并启动五服务 + 一次性 init
```

`make docker-up` 会等待：

1. MySQL / Elasticsearch healthy，Qdrant 的 `/readyz` 通过；
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

真实查询那一步看的是 SSE **是不是边生成边发**：检查脚本用裸 socket 记录
每个分片的**到达时刻**，首尾分片几乎同时到达就说明代理把响应攒起来了
（分片个数当不了判据——攒起来一次发的响应照样被拆成好几个分片）。
判定分三档：

| 判定条件 | 结论 | 处置 |
|---|---|---|
| 首尾分片的到达时间差 ≥ 50 毫秒 | 流式 | 通过 |
| 整个响应 < 200 毫秒就结束（时间差自然也小） | 太快，分辨不出来 | 重试几次；仍分辨不出就由 nginx 的生效配置定性 |
| 其余 | 攒完了一次性到达 | 失败，并提示查 `docker/nginx.conf` 的 `proxy_buffering off` |

"太快"这一档靠 nginx 的**生效配置**定性——那才是确定性的判据。要判的不是
"整份配置里有没有出现过 `proxy_buffering off`"，而是**处理 `/api/` 请求的
那个 location 的生效值**：

- 别的 location（比如 `/static/`）上写着 `off` 不算数；
- 注释掉的 `off` 不算数；
- 写在 `server` / `http` 块上、被 `include` 进来的文件里的 `off` 算数
  （nginx 的指令继承）。

拿不到这份配置时，这一档判**不通过**：没有证据说它被缓冲，不等于有证据说
它是流式——验收脚本最要命的失效是报 PASS，而它验的东西根本没验。

`DOCKER_BASE_URL` 写不写末尾的斜杠都行（`http://127.0.0.1:8080/` 也认）：
验收脚本会自己把它去掉。nginx 的代理配置转交的是**原始** URI，双斜杠原样
打到后端就是 404，而症状只表现为"请求失败"，地址写歪了这件事没人提。

这些检查放在 `scripts/smoke_checks.py` 里，且**不能用 `assert` 写**：
`python -O` 会把 `assert` 剥掉，验收脚本于是永远"通过"。

验收脚本在动手之前还有两道闸：

- **`.env` 里还是模板的占位密码（`change-me-*`）就直接判不通过**。Compose 的
  `${MYSQL_PASSWORD:?}` 只查"非空"，占位值照样让整个栈起来、验收照常通过——
  而那个密码就写在 `.env.example` 里，是公开的，谁都能连上库。先跑
  `python scripts/generate_docker_env.py` 换掉它。
- **拿不到完整的 nginx 生效配置就不判通过**：`nginx -T` 自己失败（退出码非 0），
  或者取回来的输出里没有 `# configuration file` 头（多半是取到一半就断了），
  都判不通过。SSE 那一档唯一的证据就是这份配置，半份配置比没有配置更糟。

### 2.1 与本机开发服务不要同时占用 8100

容器后端映射到宿主机 `127.0.0.1:8100`，`make dev-backend` 也用 `8100`——
两个都要跑时只有一个能起来，另一个会报 `address already in use`。

两种走法选一种：

| 场景 | 做法 |
|---|---|
| 只用 Docker | `make docker-up`，访问 `http://127.0.0.1:8080` |
| 只用本机开发 | `make dev-backend` + `make dev-frontend`，别起容器后端 |

想两者共存，把容器后端换到别的宿主机端口即可：

```bash
# .env
BACKEND_HOST_PORT=8110
```

前端不受影响：nginx 在容器网络里按 `backend:8100` 找后端，
`BACKEND_HOST_PORT` 只改宿主机这一侧的映射。

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

### 3.1 在容器里跑元知识 CLI

`backend` 只拿到只读账号，读不了 `meta_rebuild`；构建类 CLI 要用构建凭据，
所以要挂在 `init` 服务下跑（它持有 `MYSQL_PASSWORD` 与 root 密码）：

```bash
# 单题检索：YAML 到 stdout，统计到 stderr
docker compose run --rm --no-deps init python -m app.scripts.recall "华北销售额"

# S2 对账 / 幂等断言
docker compose run --rm --no-deps init python -m app.build_meta_knowledge --verify
docker compose run --rm --no-deps init python -m app.build_meta_knowledge --assert-idempotent
```

`--no-deps` 表示不自动拉起依赖，所以三存储必须已经在跑（`make docker-up` 会起）。
**别把这些凭据给 `backend`**：应用账号是 `didilili`，它在 `dw` 上有
ALL PRIVILEGES，正是 S5 要消除的那个越权入口；`backend` 只应该有 `dw_reader`。

## 4. 密钥与 LLM

- `.env` 不入库；`.env.example` 是模板。
- `MYSQL_ROOT_PASSWORD` / `MYSQL_PASSWORD` 只注入 `init` 和 MySQL；
  `backend` 只拿 `REBUILD_DW_RO_PASSWORD`。
- **宿主 CLI**：`make stage2` / `make stage3` / `make stage5-live` 不会自动读
  `.env`；先 `source scripts/export-docker-env.sh`，它会把 Compose 的随机密码和
  宿主机端口导出为 `REBUILD_*` 变量，成功时不打印任何内容。
- **取值规则**（`source` 之后 shell 和 Compose 读到的必须是同一个字符串，
  否则容器能连上库、宿主 CLI 却只收到一句 `Access denied`）：
  - **脚本导出的变量**（`REBUILD_*`）：`.env` 是这份部署的事实源。shell 里已经有
    同名变量、但和 `.env` 不一致时，以 `.env` 为准，并在 stderr 上告警
    （**只报变量名，不报值**）。`.env` 里是空值时，不覆盖 shell 里已有的非空值
    ——`.env` 的 LLM 三个变量默认就是空的，你自己 `export` 的 API key 不能被
    空值抹掉。
  - **脚本不导出的变量**（`MYSQL_PASSWORD` / `MYSQL_ROOT_PASSWORD`）：照抄 Compose
    的顺序——**已导出的 shell 值优先、再 `.env`**。脚本刻意**不** export 这两个名字：
    export 出去的话，shell 里那份快照会一直挡住 `.env` 里的新值，用户改了 `.env`
    （换密码）之后容器还在用旧密码。两边不一致时脚本告警并按 shell 的取值
    （和 Compose 一致）。
  - **"已导出"是硬条件**：Compose 是**另一个进程**，它只看得到 **export 出去**的
    变量。shell 里 `MYSQL_PASSWORD=xxx`（没 export）定义的值，Compose 根本看不见——
    这种情况下以 `.env` 为准才是和 Compose 一致的做法，脚本会单独告警一句
    "shell 里的 XXX 没有 export，docker compose 看不到它"。
  - **端口**按 Compose 的 `${VAR:-default}` 取值：已导出的 shell 值优先、再 `.env`、
    最后默认值。
  - **`.env` 里的 `$`**：值里的 `$name` / `${name}` 按 Compose 的规则展开——先查
    shell 里**已导出**的变量，再查 `.env` 里**前面**定义过的键；`${name:-默认值}`
    也支持。值里一个字面量 `$` 写成 `$$`（单引号里每个 `$` 都算成 `$$`）。
    `${` 没有配对的 `}` 是无效的 `.env`，Compose 会拒绝启动，脚本也会停下来报错。
- **改了 `.env` 要重新 `source` 一次**：脚本导出的是 `.env` 的一份快照，
  宿主 CLI 读的是这份快照。容器那边不用——Compose 每次都重新读 `.env`。
- 轮换只读密码：改 `.env` 的 `REBUILD_DW_RO_PASSWORD`，然后：

```bash
docker compose run --rm --no-deps init \
  python -m app.scripts.security --provision-ro-account
docker compose up -d --force-recreate backend
```

- **重新生成 `.env`**：`scripts/generate_docker_env.py` 默认**不覆盖**已存在的
  `.env`（`make docker-env` 在 `.env` 存在时也直接跳过）。要覆盖就显式说：

```bash
python scripts/generate_docker_env.py --dry-run        # 预览（密码显示为 ***）
python scripts/generate_docker_env.py --force          # 覆盖，但保留已有的密码和配置
python scripts/generate_docker_env.py --rotate-secrets # 换密码（见下）
```

  `--force` **保留**已有的值：已经在跑的栈不会因为重新生成 `.env` 就连不上库。
  覆盖写是**原子**的：新内容先写进同目录的临时文件（从创建那一刻就是 0600），
  再整体替换过去——旧的 `.env` 在新内容整个就位之前一直都在，不会出现
  "先删旧的、建新的，中间崩了，磁盘上一个 `.env` 都不剩"的情况（那份文件里
  还有用户的 LLM API Key，重生成不了）。
  模板（`.env.example`）里没有、旧 `.env` 里有的键也**原样保留**——最典型的是
  `COMPOSE_PROJECT_NAME`：丢掉它，Compose 的项目名就变回默认值，数据卷的名字
  跟着变，你的卷"消失"了，栈却从一个**空卷**重新起（MySQL 于是把 `dw.sql`
  又导一遍，旧数据还躺在老名字的卷里没人管）。

- **换 MySQL 密码必须先删数据卷**：MySQL 只在**首次**初始化 `mysql_data` 时用
  `MYSQL_ROOT_PASSWORD` 建账号；卷还在的话，容器里的密码仍然是旧的那个，
  新密码不生效——健康检查会一直失败，`init` 和 `backend` 于是都起不来。
  正确顺序：

```bash
make docker-reset                                   # 删掉三个数据卷（会提示输入 yes）
python scripts/generate_docker_env.py --rotate-secrets
source scripts/export-docker-env.sh                 # 宿主 CLI 要用新密码
make docker-up
```

  `--rotate-secrets` 会把这条警告打在屏幕上，别把它当成噪音。

  换完密码之后**一定要重新 `source` 一次**——脚本导出的是 `.env` 的快照，
  宿主 CLI 读的还是旧密码。容器那边不用管：Compose 每次都重新读 `.env`。

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

`make docker-reset` 会先逐个列出**当前项目名下**真正要删的卷名（项目名的优先级
和 Compose 一致：环境变量 `COMPOSE_PROJECT_NAME` > `.env` 里的同名键 >
`compose.yaml` 的 `name:`），核对无误再输 `yes`。提示里写错卷名，用户就不知道
自己到底要删什么——而真正被删掉的恰恰是当前项目名底下那三个卷。

列出来的卷名和真正删掉的卷名必须是同一批，所以脚本把项目名**显式**交给
Compose（`docker compose --project-name <项目名> down -v`）：不指定的话，
删掉的是 Compose 自己解析出来的那个项目，两边只要差一点，"你将被删除 X"和
"实际被删除 Y"就会同时成立。

`down -v` 删的不止是卷：容器和网络也一起没了，提示里写明了这一点。

`fixtures/dw.sql` 通过 MySQL 的 `docker-entrypoint-initdb.d` 挂载导入，
**只在数据卷为空的那一次**执行。卷里已经有数据时 MySQL 不会再跑这个脚本——
改了 `fixtures/dw.sql` 却不生效，多半就是这个原因。重新导入要先
`make docker-reset`（会删卷）。

## 6. 排障

| 症状 | 原因 | 处理 |
|---|---|---|
| 镜像拉取失败 | 默认镜像源不可达 | 改 `.env` 的 `*_IMAGE` 为可访问镜像源，再 `make docker-up` |
| `init` 退出 1 | MySQL/ES 未就绪、密码不一致、S2 校验失败 | `docker compose logs init`；必要时 `docker compose run --rm --no-deps init` 重跑 |
| `Access denied for user 'dw_reader'` | init 没跑完或密码已轮换 | `docker compose logs init`；重新执行 `init` |
| 容器连库正常，宿主 CLI 却 `Access denied` | shell 里的凭据和 `.env` 不一致 | 告警里会指出是哪个变量对不上。`REBUILD_*` 重新 `source scripts/export-docker-env.sh` 即可；`MYSQL_PASSWORD` / `MYSQL_ROOT_PASSWORD` 是**已导出的** shell 值优先（Compose 就这么读——没 export 的它看不见），`unset` 掉那个旧变量再重新 `source` |
| ES 容器反复退出（Linux） | `vm.max_map_count` 太小 | `sudo sysctl -w vm.max_map_count=262144` |
| 8080 被占用 | 宿主机端口冲突 | 在 `.env` 改 `FRONTEND_HOST_PORT` |
| 8100 被占用 | 容器后端与 `make dev-backend` 撞端口 | 见 2.1 节；或改 `.env` 的 `BACKEND_HOST_PORT` |
| 改了 `fixtures/dw.sql` 但库里的数据没变 | 导入脚本只在空卷上跑一次 | `make docker-reset` 后重新 `make docker-up` |
| 改了 `.env` 里的密码，栈却起不来 | MySQL 只在首次初始化数据卷时用密码建账号，卷还在则新密码不生效 | `make docker-reset` 后重新 `make docker-up`（见第 4 节） |
| 构建时 npm / pip 拉包失败或极慢 | 默认源不可达 | 改 `.env` 的 `NPM_REGISTRY` / `UV_INDEX_URL`，再 `make docker-up` |
| 页面能开但 SSE 不流式 | Nginx 缓冲 | 确认 `docker/nginx.conf` 的 `proxy_buffering off`；重建 frontend |
| `S2 FAIL: ... unknown analyzer [ik_max_word]` | 官方 ES 没有 IK | S2 会自动回退 `standard`；若仍失败看 init 日志 |
| 查询一直 `INTERNAL_ERROR` | init 未成功，或只读账号不可用 | 先看 `docker compose ps` 与 `docker compose logs init` |

## 7. 边界

- backend 固定单 worker：取消注册表是进程内状态，不能多副本。
- ES 单节点、关闭鉴权，只适合单机；不要暴露到公网。
- `fixtures/dw.sql` 是当前数据源；替换真实数据不在本部署范围内。
- 不提供镜像仓库推送、多节点编排或高可用。
