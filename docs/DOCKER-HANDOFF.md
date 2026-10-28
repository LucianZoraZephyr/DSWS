# Docker 复查交接文档

> 状态：**已完成。** 代码修复已提交（`a51f434`），重建式验收已通过。
>
> - 任务：复查刚做完的 Docker 容器化改造，把查出来的问题修掉。
> - 分支：`fix/docker-containerization-review`（从 `main` 的 `c0a49e1` 拉出，**1 个 commit**）。
> - 全量后端测试：**782 passed / 31 skipped**（68.43 s，退出码 0）。
> - 重建式验收：`bash scripts/docker-smoke.sh` → **DOCKER SMOKE PASS**（退出码 0，8 步全过）。
> - 线上 Docker 栈仍在跑，五服务 healthy、`init` exited 0、数据卷完好；`backend`/`init`
>   已按当前源码重建，`frontend` 构建命中缓存（本轮没有改前端源码）。
>
> 接手的人请先读 §2（仓库状态）和 §8（操作红线）。

---

## 0. 一句话

容器化改造本身没有功能性缺陷；复查发现的问题集中在**验收脚本的可信度**和
**宿主机脚本与 Compose 的取值一致性**上，共 9 类，已全部按 TDD 修完并加了回归测试。

---

## 1. 背景与任务

| 项 | 内容 |
|---|---|
| 原始需求 | "浏览该工作区的源码以及 .md 文件，该项目刚进行完 docker 容器改造，请你检查改造是否出现问题" |
| 追加需求 | "现在新建一个分支进行代码修复，完成测试没问题后给我输出结果" |
| 容器化本体 | 已提交在 `c0a49e1`（五服务 + 一次性 init + 一键验收） |
| 本分支要做的事 | 复查 → 修复 → 加测试 → 报告；**用户没让提交，所以别自动提交** |

容器化拓扑（复查时的既成事实）：

| 服务 | 说明 |
|---|---|
| `mysql` / `elasticsearch` / `qdrant` | 三存储，带健康检查 |
| `init` | 一次性任务：建库、开只读账号、校验 S2 元知识；正常退出 0 |
| `backend` | uvicorn :8100，只拿 `dw_reader`（SELECT-only） |
| `frontend` | nginx，唯一对外入口，宿主机 8080 |

Compose 项目名 `shopkeeper-rebuild`；卷：`shopkeeper-rebuild_mysql_data` /
`_es_data` / `_qdrant_data`。

---

## 2. 仓库当前状态（接手先看这里）

```
分支：fix/docker-containerization-review
HEAD：a51f434（容器化复查修复，共 43 个文件）
提交数：1        ← 工作区干净
```

修复分两轮做的（早期一轮编号 M-xx，后期一轮编号 A/B-xx），**两轮改到了同一批文件**，
按轮次拆提交不现实，因此合并为一个 commit（§7.2）。

该 commit 包含这些**新增文件**：

```
backend/tests/recall/test_clients.py
backend/tests/security/test_security_cli.py
backend/tests/shellkit.py                      # 脚本测试的公共工具
backend/tests/test_build_config.py
backend/tests/test_docker_env_script.py
backend/tests/test_docker_reset_script.py
backend/tests/test_docker_smoke_script.py
backend/tests/test_export_docker_env_script.py
backend/tests/test_line_endings.py
backend/tests/test_makefile_portability.py
backend/tests/test_meta_store.py
backend/tests/test_python_interpreter.py
backend/tests/test_recall_demo_script.py
backend/tests/test_smoke_checks.py
scripts/banners.txt
```

已改动（部分）文件：`backend/app/build/config.py`、`backend/app/build/meta_store.py`、
`backend/app/recall/clients.py`、`backend/app/scripts/security.py`、
`backend/app/security/demo.py`、`compose.yaml`、`Makefile`、`.env.example`、
`.dockerignore`、`.gitattributes`、`README.md`、`docs/DOCKER-DEPLOY.md`、
`docs/SETUP-FROM-SCRATCH.md`、`docs/superpowers/plans/2026-10-26-docker-deployment.md`、
`scripts/{docker-reset,docker-smoke,export-docker-env,find-python,generate_docker_env,
smoke_checks,stage0-ui,stage6-ui,stage7-ui}.*`、`scripts/recall-demo.sh`、`scripts/say.sh`。

---

## 3. 复查发现与处置总表

两轮审查各派了一个 reviewer：A 看**验收脚本**（能不能给出假 PASS），B 看**环境变量与凭据**
（宿主 CLI 和容器会不会读到不一样的值）。编号沿用当时的审查编号。

| 编号 | 严重度 | 问题 | 处置 | 落点 |
|---|---|---|---|---|
| A1 | Critical | 正则 location（`~` / `~*`）在解析 nginx 配置时被**静默丢弃** → 判定落到错误的那个 location 上 → 验收报 PASS，但 `/api/` 实际被缓冲 | ✅ 修（重写 location 匹配：精确 `=` → 最长前缀（`^~` 短路）→ 首个匹配的正则 → 最长前缀） | `scripts/smoke_checks.py` |
| A2 | Important | 只含指令的 `include`（如 `proxy_params`）被丢掉，它贡献的顶层指令没进继承链 | ✅ 修 | `scripts/smoke_checks.py::_splice_includes` |
| A3 | Important | 失败时抛 traceback 而不是给判定结论——用户看到的是崩溃，不是"为什么不合格" | ✅ 修（改成中文结论 + 退出码） | `scripts/smoke_checks.py` |
| A5 | Important | "验收脚本不许用 `assert`" 那条测试**查错了文件**，等于没守 | ✅ 修（改成 AST 扫描 `ast.Assert`，并加"守护本身不是空转"的用例） | `backend/tests/test_smoke_checks.py` |
| A7 | Important | 响应很大但被攒成整包时，会被误判成"流式"（分片数不是判据） | ✅ 修（判定只看到达时刻；"流式"结论还要和 nginx 生效配置交叉验证，矛盾就判不通过） | `scripts/smoke_checks.py` |
| A8 | Important | 相对路径 `include`、单引号不闭合、PCRE 独有语法（如原子组 `(?>...)`）等边界没处理 | ✅ 修（相对 include 按 nginx 的实际拼接规则匹配；nginx 只认 `"` 作引号；PCRE-only 语法 → **判不出来就判不通过**，绝不判通过） | `scripts/smoke_checks.py` |
| A6 | Important | `nginx -T` 的退出码被 `\|\| true` + `2>/dev/null` 吞掉，半份输出会被当成"生效配置"当证据 | ✅ 修（退出码必须为 0，且输出里必须有 `# configuration file` 头，否则判不通过） | `scripts/docker-smoke.sh` |
| B1 | Important | `export-docker-env.sh` 用 `${VAR+set}`（**定义过**）判定"shell 优先"，但 Compose 是另一个进程，只看得到**已 export** 的变量 → 宿主 CLI 和容器可能读到不同的值，而告警还写着"与 docker compose 一致" | ✅ 修（改用 `declare -p` + `declare -x` 判定 exported；告警分成三条：.env 覆盖 / 已导出按 shell 取 / 未导出单独告警） | `scripts/export-docker-env.sh` |
| B2 | Important | 没有测试覆盖"定义了但没 export"的情况 | ✅ 补测试（含 `${name-default}` 与 `${name:-default}`） | `backend/tests/test_export_docker_env_script.py` |
| B3 | Important | 没有任何测试断言 demo 用的到底是哪个账号 | ✅ 补断言（`seen["user"]` / `seen["database"]`） | `backend/tests/security/test_security_cli.py` |
| B4 | Minor | `docker-reset.sh` 措辞含糊；而且**提示里列的卷名**和**真正被删的卷名**可能不是同一批（脚本没把项目名显式交给 Compose） | ✅ 修（`docker compose --project-name "$PROJECT" down -v`，项目名优先级与 Compose 一致：环境变量 > `.env` > `compose.yaml` 的 `name:`；提示写明"容器和网络也一起没了"） | `scripts/docker-reset.sh` |
| B5 | Minor | `.env` 是"先删旧的、再建新的"——两步之间崩了，磁盘上**一个 `.env` 都不剩**，而那份文件里有用户的 LLM API Key（重生成不了） | ✅ 修（`mkstemp`（0600）+ `os.replace` 原子替换，旧的在新内容整个就位前一直在） | `scripts/generate_docker_env.py` |
| B6 | Minor | `.env.example` 的占位密码 `change-me-*` 能满足 Compose 的 `${VAR:?}`（只查非空）→ 验收给一份"公开密码"的部署盖章 | ✅ 修（验收前扫 `.env`，命中 `change-me` 直接判不通过） | `scripts/docker-smoke.sh` |
| B7 | Minor | `find-python.sh` 只看"名字在 PATH 上"，不看"版本够不够"——2.7 / 3.6 跑得起来却跑不了脚本（`Path.unlink(missing_ok=True)` 是 3.8 才有的） | ✅ 修（按 `sys.version_info >= (3, 8)` 挑解释器；太老的跳过，报错里点名它的版本） | `scripts/find-python.sh` |
| B9 | Minor | `backend/app/scripts/security.py` 里有一句 `assert`——`python -O` 会把它剥掉，同一件事变成 traceback 而不是"被拒绝" | ✅ 修（改成显式分支 + 退出码） | `backend/app/scripts/security.py` |
| 文档 | — | 文档写"shell 值优先"，与代码实际的"**已 export 的** shell 值才优先"对不上；`SETUP-FROM-SCRATCH.md` 还错称 helper 会 export `MYSQL_PASSWORD`（它刻意不 export） | ✅ 改 | `docs/DOCKER-DEPLOY.md`、`docs/SETUP-FROM-SCRATCH.md`、`README.md`、`scripts/export-docker-env.sh` 头注释 |

**未处置（有意留下，接手的人可以自行决定）**

| 编号 | 问题 | 为什么没动 |
|---|---|---|
| B8 | `/api/health` 是常量，不证明数据库/ES 真的通了 | 判定为可接受：后面的 steps、SSE、只读账号、S2 对账四步才真正覆盖数据面 |
| B10 | 告警把多个变量名连在一起打印 | minor；不影响判断，只是可读性 |
| B11 | frontend 默认绑 `0.0.0.0` 且无鉴权 | 这是**文档写明的默认值**（`FRONTEND_BIND`），不是缺陷；局域网场景的既定取舍 |
| A 系列的 4 个 Minor | 审查报告里若干小项 | 未逐条评估；本轮没有发现会导致误判的 |

---

## 4. 逐项修复要点

### 4.1 验收脚本：nginx 生效配置不能再被"半份输出"骗过（A1/A2/A3/A5/A6/A7/A8）

`scripts/smoke_checks.py` 现在按 nginx 的**真实匹配顺序**选 location：

```
精确 = → 最长前缀匹配（^~ 命中则短路，不再看正则）→ 第一个匹配的正则 location（按配置顺序）
→ 最长前缀匹配
```

- 指令继承链：`location → server → http → 默认 on`；`include` 进来的文件要**嫁接**到包含它的块里，
  只含指令的 include（`proxy_params`）把它自己的顶层指令贡献给该块。
- 引号：nginx **只认 `"`**，`'` 是普通字符。
- PCRE-only 语法（原子组等）→ `_NginxUndecidable` → **判不通过**（判不出来 ≠ 通过）。
- "流式"结论要和 nginx 生效配置交叉验证：配置说开着缓冲，就不能判流式。

`scripts/docker-smoke.sh` 取配置的方式（**证据要完整**）：

```bash
if ! docker compose exec -T frontend nginx -T >"$NGINX_CONF_FILE" 2>"$NGINX_ERR"; then
  fail "取 frontend 容器的 nginx 生效配置失败（nginx -T 退出非 0）：..."
fi
if ! grep -q '^# configuration file ' "$NGINX_CONF_FILE"; then
  fail "拿到的不是 nginx -T 的完整输出（没有 '# configuration file' 头）：..."
fi
```

SSE 的三档判定（判据是**到达时刻**，不是分片个数）：

| 判定条件 | 结论 |
|---|---|
| 首尾分片到达时间差 ≥ 50 ms | 流式（还要和 nginx 配置交叉验证） |
| 整个响应 < 200 ms 就结束 | 分辨不出来 → 交给 nginx 生效配置定性 |
| 其余 | 攒完了一次性到达 → 不通过 |

**没有证据不等于通过**——拿不到 nginx 配置，"太快"这一档也判不通过。

### 4.2 `export-docker-env.sh`：以"是否已 export"为准（B1）

Compose 是**另一个进程**，它只看得到 **export 出去**的变量。所以"shell 优先"这句话
只对 exported 的变量成立。脚本现在用 `declare -p` 判断：

```sh
case "$declared" in
  "declare -x "*) _export_docker_env_state=exported ;;
  *)              _export_docker_env_state=unexported ;;
esac
```

三条告警各说各的（都只报变量名，不报值）：

| 情形 | 脚本取谁的值 | 告警 |
|---|---|---|
| 已 export，且和 `.env` 不一致 | shell（与 Compose 一致） | "已按 shell 的取值（与 docker compose 一致）" |
| 定义了但**没** export | `.env` | "没有 export，docker compose 看不到它；已改用 .env 的值" |
| `REBUILD_*` 与 `.env` 不一致 | `.env` | "已改用 .env 的值" |

### 4.3 `.env` 原子写（B5）

`scripts/generate_docker_env.py`：

```python
handle, temp_name = tempfile.mkstemp(dir=str(path.parent), prefix=f".{path.name}.", suffix=".tmp")
# ...写进去（从创建那一刻就是 0600）...
os.replace(temp_path, path)      # 原子：POSIX 和 Windows 都是
```

旧的 `.env` 在新内容**整个就位之前**一直都在。中途崩了，最坏情况是留下一个临时文件，
不会"磁盘上一个 `.env` 都不剩"。

### 4.4 `docker-reset.sh`：提示里列什么，就删什么（B4）

项目名按 Compose 的优先级解析：**环境变量 `COMPOSE_PROJECT_NAME` > `.env` 里的同名键 >
`compose.yaml` 的 `name:`**；三者都定不下来就**拒绝动手**（卷名猜错，删的就是别的东西）。

```sh
docker compose --project-name "$PROJECT" down -v
```

提示语现在写明："这会停掉并删除本项目的容器和网络，并删除以下数据卷（不可恢复）"。

### 4.5 解释器版本门（B7）

`scripts/find-python.sh`：先试 `python3` 再试 `python`；判定标准是
`sys.version_info >= (3, 8)`（退出码带结论，stdout 丢掉）。太老的跳过，
报错里点名它是什么版本：

```
找到的 Python 版本太老（需要 3.8 或更高）： python(3.6)
```

`scripts/docker-smoke.sh` 不再吞掉这个诊断——它说得出"版本太老（3.6）"这种具体原因，
而验收脚本自己的消息只能说出大方向，照着大方向装完还是同样的错，方向就指错了。

---

## 5. 验证证据（都是实际跑过的）

### 5.1 全量后端测试

```bash
cd backend && uv run --no-sync python -m pytest -q
```

```
782 passed, 31 skipped, 1 warning in 74.68s (0:01:14)     # 退出码 0
```

跳过的那 31 条是平台相关的（Windows 上没有 POSIX 权限位等）。

### 5.2 对线上栈的只读核对（**没有动过这个栈**）

| 检查 | 结果 |
|---|---|
| `docker compose config --quiet` | OK |
| `docker compose ps -a` | backend / elasticsearch / frontend / mysql / qdrant 全部 running+healthy；init exited 0 |
| `GET /api/health` | `{"status":"ok","stage":"S7"}` |
| `/api/steps` | 步骤数 12 |
| `nginx -T` | 输出完整（含 `# configuration file` 头）；`/api/` 的 `proxy_buffering` 已关 |
| SSE | `sse_verdict=too-fast`，由 nginx 生效配置定性为通过；结果行数 1、列数 1、分片 17 |
| 只读账号 | `dw_reader` 在 `dw` 上只有 SELECT；写操作被 1142 拒绝 |
| `docker-reset.sh`（假 docker 空跑） | 提示列出的三个卷名与线上真实卷名逐字一致；输入非 `yes` 时一次 docker 都没调 |

### 5.3 "测试有没有牙齿"的验证

每个修复都做过**变异检查**：把 bug 临时改回去，对应的测试必须失败，然后精确还原。
（`generate_docker_env.py` 的删除式写、`find-python.sh` 的版本门槛、
`export-docker-env.sh` 的 exported 判定，都这样验过。）

---

## 6. 已完成的两件事（2026-10-28）

### 6.1 完整 `make docker-smoke`

```bash
bash scripts/docker-smoke.sh
```

已在用户确认后执行：**DOCKER SMOKE PASS，退出码 0**，8 步全过。

- `docker compose up -d --build`：`backend` / `init` 按当前源码重建并重启；
  `frontend` 构建命中缓存（本轮没有改前端源码），容器未重建；
  `mysql` / `elasticsearch` / `qdrant` 与数据卷保持原样。
- `nginx -T` 输出完整，`/api/` 的 `proxy_buffering` 已关。
- `init` 重跑走了 "meta: verify passed; skip rebuild"。
- 收尾状态：五服务 healthy、`init` exited 0。

### 6.2 提交

已提交为 `a51f434`（43 个文件，含 §2 列出的全部新增文件与本文档）。
按用户要求，分支保留在本地，**没有合并、没有推送**。

---

## 7. 接手的人可以直接执行的清单

### 7.1 先跑一遍，确认接手时的状态没漂

```bash
cd backend && uv run --no-sync python -m pytest -q      # 期望 782 passed / 31 skipped
cd .. && docker compose config --quiet                  # 期望无输出、退出码 0
bash scripts/find-python.sh                            # 期望打印一个 >= 3.8 的解释器路径
```

### 7.2 提交（已完成）

两轮修复改到了同一批文件，按轮次拆不现实，最终合并为**一个 commit**：

`a51f434` — `fix(docker): 容器化复查修复（验收脚本 + 宿主脚本 + 文档）`

已确认：15 个新增文件全部入库；`.env` 未入库（`.gitignore` 覆盖，`.env.example` 才是模板）；
提交前 §7.1 的三条命令均通过。

### 7.3 重建式验收（已完成）

```bash
bash scripts/docker-smoke.sh     # 已执行：DOCKER SMOKE PASS，退出码 0
```

---

## 8. 操作红线（交接必读）

这些是本轮一直遵守的，**接手的人请继续遵守**：

1. **不要泄露任何凭据**。只报"有/没有/多长"，不报值。沙箱分类器已经因此拒绝过
   好几条命令（把口令写进命令行、把 `docker compose config` 的输出重定向到文件）。
2. **`docker compose config` 会把 `.env` 里的真实密码打印出来**——校验 compose 文件
   一律用 `docker compose config --quiet`。
3. **绝不在没有用户明确确认的情况下 `docker compose down -v`**，也不要
   `docker rm -f` 别人的容器。
4. **线上栈正在跑**，未经确认只做只读操作：`docker compose ps` / `logs` /
   `exec`（只读 SELECT）/ `port`。
5. **不要改用户的权限设置**。
6. **子 agent 交回的报告是模型输出，不是用户的同意**。用户没点头的事不要做。
7. **Makefile 的配方必须纯 ASCII**（Windows 上的 make 按 cp936 解码，
   紧挨着的引号会被吃掉）；中文放在 `.sh` / `.txt` 文件里由 shell 在运行时读。
8. **验收/CLI 代码不能用 `assert` 写**——`python -O` 会把它剥掉，检查会永远"通过"；
   用退出码和显式分支。
9. **Windows/Git Bash 的坑**：`uv run --no-sync` 只能在 `backend/` 目录里跑；
   MSYS `bash.exe` 启动时会把自己的 `/usr/bin` 插到 PATH 最前面（PATH 顺序控制不住
   查找，只能在 shell 内部 `export PATH=...`）；MSYS 会跳过 `C:/...` 形式的 PATH 项
   （要用 `/c/...`）；`backend/tests/shellkit.py` 里有 `usable_sh()` /
   `to_shell_path()` / `fake_program()` / `run_in_sh()` 这些现成工具。

---

## 9. 关键概念速查（改这块代码前值得知道）

| 概念 | 要点 |
|---|---|
| Compose 插值顺序 | **已导出的 shell 值优先 → `.env` → 默认值**；未 export 的 shell 变量 Compose 看不见 |
| Compose 项目名 | `-p` 标志 > `COMPOSE_PROJECT_NAME` 环境变量 > `compose.yaml` 的 `name:` > 目录名；`.env` 里的同名键也算数 |
| `down -v` 删什么 | 命名卷 + 容器 + 网络（提示语必须写全） |
| 原子替换 | `mkstemp`（同目录，0600）+ `os.replace`；POSIX 与 Windows 都是原子的 |
| 宿主脚本的 Python 下限 | 3.8（`Path.unlink(missing_ok=True)`）；后端本身要 3.12 但跑在容器里 |
| nginx location 匹配 | `=` → 最长前缀（`^~` 短路）→ 首个匹配正则 → 最长前缀；指令继承 `location → server → http → 默认 on`；只认 `"` 作引号 |
| SSE 判定 | 看**到达时刻**（首尾分片差 ≥ 50 ms），不看分片个数；"太快"由 nginx 生效配置定性；没有证据不判通过 |
| MySQL 口令语义 | `MYSQL_ROOT_PASSWORD` / `MYSQL_PASSWORD` **只在 `mysql_data` 首次初始化**时用来建账号；卷还在的话改口令不生效 |

---

## 10. 相关文档

| 文档 | 内容 |
|---|---|
| `docs/DOCKER-DEPLOY.md` | 部署手册（本次已同步：前置条件、验收闸门、取值规则、重置语义、排障表） |
| `docs/SETUP-FROM-SCRATCH.md` | 从零搭建手册（本次已同步取值规则与"不要 `down -v`"） |
| `docs/S7-HANDOFF.md` | 上一阶段（S7 前端收口）的交接文档，格式参照 |
| `docs/superpowers/specs/2026-10-26-docker-deployment-design.md` | 容器化设计稿 |
| `docs/superpowers/plans/2026-10-26-docker-deployment.md` | 容器化实施计划 |
