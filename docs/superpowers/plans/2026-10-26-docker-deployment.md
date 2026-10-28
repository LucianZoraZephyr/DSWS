# Docker 容器化部署 Implementation Plan

> ⚠️ **这份计划已经实施完毕，是历史记录，不是当前实现的说明。**
>
> 实际落地的文件以仓库里的为准：`compose.yaml`、`docker/backend.Dockerfile`、
> `docker/frontend.Dockerfile`、`docker/nginx.conf`、
> `backend/app/scripts/docker_init.py`、`scripts/docker-smoke.sh`、
> `scripts/generate_docker_env.py`、`scripts/export-docker-env.sh`。
> 面向使用者的说明在 [`docs/DOCKER-DEPLOY.md`](../../DOCKER-DEPLOY.md)。
>
> 实施过程中与本计划不一致的地方（**以实现为准**）：
>
> - backend 镜像的 `WORKDIR` 是 `/app/backend`（计划里写的是 `/build`）；
> - `compose.yaml` 多了 qdrant 的 healthcheck（镜像里没有 curl / wget，
>   用 `bash` 的 `/dev/tcp` 打 `/readyz`），ES 的 healthcheck 改成断言
>   JSON 里的 `status` 字段；
> - `scripts/docker-smoke.sh` 重写过：SSE 检查改用裸 socket 看 HTTP 分片的
>   **到达时刻**（分片个数不能当判据——被代理攒起来一次性发出的响应也会被
>   拆成好几个分片，"分片 ≥ 2"照样过）；整个响应快到 200 ms 内就结束时判为
>   "分辨不出是否流式"，不据此判失败，重试 3 次；并且失败时会把 `init` 的
>   日志打出来；
> - `.env.example` / `compose.yaml` 多了 `FRONTEND_BIND`；
> - 仓库根多了 `.gitattributes`（Windows 上 bash 脚本不能带 CRLF）；
> - `backend/app/build/config.py` 去掉了硬编码的密码兜底值——
>   缺凭据要显式报错，而不是拿一个写进仓库的口令去连库。

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 把前端、后端、MySQL、Elasticsearch、Qdrant 和一次性初始化任务打包进单机 Docker Compose，一条命令完成部署、导入 `fixtures/dw.sql`、创建 `dw_reader` 并构建 S2 元知识。

**Architecture:** Compose 启动三个存储后，由一个复用 backend 镜像的 `init` 一次性容器等待依赖、开通只读账号并校验/按需重建 S2 元知识；`init` 成功退出后 backend 才启动，frontend 由 Nginx 托管构建产物并反向代理 `/api`。所有应用代码烘焙进镜像，运行期不挂载源码；密钥只来自 `.env`，backend 只拿只读账号密码。

**Tech Stack:** Docker Compose v2、Python 3.12 + uv、FastAPI/Uvicorn、React 19 + Vite 6、Nginx 1.27、MySQL 8.0、Elasticsearch 8.15.3、Qdrant v1.12.4。

**Spec:** `docs/superpowers/specs/2026-10-26-docker-deployment-design.md`

## Global Constraints

- 单机 Docker Compose；不引入 Swarm / Kubernetes / 镜像仓库。
- 外部镜像名全部可通过 `.env` 覆盖；默认值使用当前网络可拉取的镜像源。
- `.env` 不入库；`.env.example` 入库；Compose 必需变量用 `${VAR:?}` 快速失败。
- `backend` 容器只获得 `REBUILD_DW_RO_PASSWORD`；root 与普通账号密码只给 `init`。
- backend 固定单 worker：`CancelRegistry` 是进程内状态，不能横向扩容。
- 初始化幂等：先 `build_meta_knowledge --verify`，失败才 `--fresh`，fresh 后必须再 verify。
- 只操作 `*_rebuild` 目标；不修改旧系统；不执行 `down -v` 除非用户明确要求。
- `frontend/src/generated/` 是生成物，不得手改。
- SSE 经过 Nginx 必须关闭缓冲：`proxy_buffering off`、`proxy_cache off`、HTTP/1.1、`Connection ""`、长读超时。
- MySQL 8 默认 `caching_sha2_password`，backend 依赖必须包含 `cryptography`。
- 不主动 commit / push；只有用户明确要求时才提交。

---

## File Structure

| 文件 | 职责 |
|---|---|
| `backend/app/scripts/docker_init.py` | init 容器入口：等待依赖 → 开通/校验只读账号 → 校验/重建 S2 |
| `backend/tests/test_docker_init.py` | init 编排的单元测试（不连真实存储） |
| `scripts/generate_docker_env.py` | 从 `.env.example` 生成带随机密码的 `.env` |
| `scripts/docker-smoke.sh` | 端到端验收：五服务 + health + 12 步 + 真实查询 + 账号/元知识 + init 幂等 |
| `docker/backend.Dockerfile` | backend/init 共用镜像，非 root、单 worker |
| `docker/frontend.Dockerfile` | Node 构建 + Nginx 运行镜像 |
| `docker/nginx.conf` | SPA 回退、`/api` 反向代理、SSE 配置 |
| `compose.yaml` | 五长驻服务 + `init` 一次性任务、卷、健康检查、依赖 |
| `.env.example` | 部署配置模板；密码为占位值 |
| `.dockerignore` | 排除开发产物与密钥，保留 `contracts/`、`fixtures/` |
| `docs/DOCKER-DEPLOY.md` | 部署、运维、排障手册 |
| `Makefile` | `docker-env/up/down/logs/smoke/reset` |
| `README.md` | Docker 快速开始与约束更新 |
| `docs/SETUP-FROM-SCRATCH.md` | 把旧 compose 路径改为仓库根 `compose.yaml` |
| `backend/pyproject.toml`、`backend/uv.lock` | 增加 `cryptography` |

---

### Task 1: init 编排逻辑与 MySQL 8 驱动依赖

**Files:**
- Create: `backend/app/scripts/docker_init.py`
- Create: `backend/tests/test_docker_init.py`
- Modify: `backend/pyproject.toml`
- Modify: `backend/uv.lock`

**Interfaces:**
- Consumes: `app.build.config.BuildConfig.from_env()`、`app.cli.enable_utf8_stdio()`。
- Produces:
  - `initialize(*, runner: Runner = subprocess.run, wait: Callable[[], None] | None = None) -> None`
  - `wait_for_services(*, timeout_s: float = 180.0, interval_s: float = 2.0) -> None`
  - `main(argv: Sequence[str] | None = None) -> int`
  - CLI 模块入口：`python -m app.scripts.docker_init`

- [ ] **Step 1: Write the failing tests**

Create `backend/tests/test_docker_init.py`:

```python
"""Docker init 编排的单元测试（不连接真实存储）。"""

from __future__ import annotations

from subprocess import CompletedProcess

import pytest

from app.scripts import docker_init


class FakeRunner:
    def __init__(self, returncodes: list[int]) -> None:
        self._codes = list(returncodes)
        self.commands: list[list[str]] = []

    def __call__(self, command: list[str], **_: object) -> CompletedProcess[str]:
        self.commands.append(list(command))
        return CompletedProcess(command, self._codes.pop(0))


def _flags(runner: FakeRunner) -> list[str]:
    return [command[-1] for command in runner.commands]


def test_initialize_skips_rebuild_when_meta_verify_passes() -> None:
    runner = FakeRunner([0, 0, 0])

    docker_init.initialize(runner=runner, wait=lambda: None)

    assert _flags(runner) == [
        "--provision-ro-account",
        "--verify-ro-account",
        "--verify",
    ]


def test_initialize_rebuilds_after_failed_verify() -> None:
    runner = FakeRunner([0, 0, 1, 0, 0])

    docker_init.initialize(runner=runner, wait=lambda: None)

    assert _flags(runner) == [
        "--provision-ro-account",
        "--verify-ro-account",
        "--verify",
        "--fresh",
        "--verify",
    ]


def test_initialize_stops_on_rebuild_failure() -> None:
    runner = FakeRunner([0, 0, 1, 1])

    with pytest.raises(SystemExit) as error:
        docker_init.initialize(runner=runner, wait=lambda: None)

    assert error.value.code == 1
    assert _flags(runner) == [
        "--provision-ro-account",
        "--verify-ro-account",
        "--verify",
        "--fresh",
    ]
```

- [ ] **Step 2: Run the tests to verify they fail**

Run:

```bash
cd backend && uv run pytest -q tests/test_docker_init.py
```

Expected: FAIL with `ModuleNotFoundError: No module named 'app.scripts.docker_init'`.

- [ ] **Step 3: Add `cryptography` to backend dependencies**

Run:

```bash
cd backend && uv add cryptography
```

Expected: `pyproject.toml` gains a `cryptography>=...` dependency and `uv.lock` is updated. Then run:

```bash
cd backend && uv sync --frozen
```

Expected: exit 0.

- [ ] **Step 4: Implement the init module**

Create `backend/app/scripts/docker_init.py`:

```python
"""Docker 初始化：等待依赖 → 开通只读账号 → 校验/重建 S2 元知识。

失败即非零退出；compose 的
`depends_on: init: condition: service_completed_successfully`
会因此阻止 backend 启动。

    python -m app.scripts.docker_init
"""

from __future__ import annotations

import argparse
import subprocess
import sys
import time
from collections.abc import Callable, Sequence

import httpx

from app.build.config import BuildConfig
from app.cli import enable_utf8_stdio

Runner = Callable[..., subprocess.CompletedProcess[str]]


def _command(module: str, flag: str) -> list[str]:
    return [sys.executable, "-m", module, flag]


def _run(command: list[str], runner: Runner) -> None:
    print("+ " + " ".join(command), flush=True)
    completed = runner(command)
    if completed.returncode != 0:
        raise SystemExit(completed.returncode)


def _connect_mysql() -> None:
    import pymysql

    config = BuildConfig.from_env()
    connection = pymysql.connect(**config.mysql_dsn(database=None, root=True))
    connection.close()


def _check_http(url: str) -> None:
    response = httpx.get(url, timeout=3.0)
    if response.status_code < 200 or response.status_code >= 300:
        raise RuntimeError(f"{url} -> HTTP {response.status_code}")


def _wait_until(
    label: str,
    check: Callable[[], None],
    *,
    timeout_s: float,
    interval_s: float,
) -> None:
    deadline = time.monotonic() + timeout_s
    last_error: Exception | None = None
    while time.monotonic() < deadline:
        try:
            check()
        except Exception as exc:  # noqa: BLE001  # 连接拒绝/超时都要继续轮询
            last_error = exc
        else:
            print(f"{label}: ready", flush=True)
            return
        time.sleep(interval_s)
    raise SystemExit(f"{label}: 等待超时（{timeout_s:.0f}s）：{last_error}")


def wait_for_services(
    *, timeout_s: float = 180.0, interval_s: float = 2.0
) -> None:
    """轮询三个存储；任何一个不可用就继续等，直到总超时。"""
    config = BuildConfig.from_env()
    _wait_until(
        "mysql",
        _connect_mysql,
        timeout_s=timeout_s,
        interval_s=interval_s,
    )
    _wait_until(
        "elasticsearch",
        lambda: _check_http(
            f"{config.es_url}/_cluster/health"
            "?wait_for_status=yellow&timeout=1s"
        ),
        timeout_s=timeout_s,
        interval_s=interval_s,
    )
    _wait_until(
        "qdrant",
        lambda: _check_http(f"{config.qdrant_url}/collections"),
        timeout_s=timeout_s,
        interval_s=interval_s,
    )


def initialize(
    *,
    runner: Runner = subprocess.run,
    wait: Callable[[], None] | None = None,
) -> None:
    """执行一次性初始化；任何一步失败都以非零退出。"""
    (wait or wait_for_services)()

    _run(
        _command("app.scripts.security", "--provision-ro-account"),
        runner,
    )
    _run(
        _command("app.scripts.security", "--verify-ro-account"),
        runner,
    )

    verify_command = _command("app.build_meta_knowledge", "--verify")
    print("+ " + " ".join(verify_command), flush=True)
    verified = runner(verify_command)
    if verified.returncode == 0:
        print("meta: verify passed; skip rebuild", flush=True)
        return

    print("meta: verify failed; rebuilding", flush=True)
    _run(_command("app.build_meta_knowledge", "--fresh"), runner)
    _run(verify_command, runner)


def main(argv: Sequence[str] | None = None) -> int:
    enable_utf8_stdio()
    parser = argparse.ArgumentParser(
        prog="python -m app.scripts.docker_init",
        description="Docker 一次性初始化：只读账号 + S2 元知识",
    )
    parser.add_argument(
        "--timeout",
        type=float,
        default=180.0,
        help="等待每个存储就绪的秒数（默认 180）",
    )
    args = parser.parse_args(argv)

    initialize(wait=lambda: wait_for_services(timeout_s=args.timeout))
    print("DOCKER INIT PASS", flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
```

- [ ] **Step 5: Run the focused tests**

Run:

```bash
cd backend && uv run pytest -q tests/test_docker_init.py
```

Expected: `3 passed`.

- [ ] **Step 6: Run the full backend suite**

Run:

```bash
cd backend && uv run pytest -q
```

Expected: all tests pass (baseline is `521 passed / 30 skipped`; the new file adds 3 passing tests).

- [ ] **Step 7: Checkpoint**

Report the changed files and test counts. Do **not** commit unless the user explicitly asks.

---

### Task 2: `.env` 生成器与配置模板

**Files:**
- Create: `.env.example`
- Create: `scripts/generate_docker_env.py`
- Modify: `.gitignore`

**Interfaces:**
- Consumes: `.env.example` at repository root.
- Produces:
  - `python scripts/generate_docker_env.py [--force] [--dry-run]`
  - generated `.env` with random values for `MYSQL_ROOT_PASSWORD`, `MYSQL_PASSWORD`, `REBUILD_DW_RO_PASSWORD`; all other keys copied from `.env.example`.

- [ ] **Step 1: Write `.env.example`**

Create `.env.example`:

```dotenv
# Docker Compose 部署配置。
# 复制为 .env 并替换密码，或在仓库根目录运行 `make docker-env` 自动生成。
# .env 已被 .gitignore 忽略，不要提交。

MYSQL_ROOT_PASSWORD=change-me-root
MYSQL_PASSWORD=change-me-app
REBUILD_DW_RO_PASSWORD=change-me-ro

# 外部镜像（当前网络可拉取的默认值；换环境时按需覆盖）
MYSQL_IMAGE=docker.m.daocloud.io/library/mysql:8.0
ELASTICSEARCH_IMAGE=docker.m.daocloud.io/elastic/elasticsearch:8.15.3
QDRANT_IMAGE=docker.m.daocloud.io/qdrant/qdrant:v1.12.4
PYTHON_IMAGE=docker.m.daocloud.io/library/python:3.12-slim
NODE_IMAGE=docker.m.daocloud.io/library/node:20-alpine
NGINX_IMAGE=docker.m.daocloud.io/library/nginx:1.27-alpine
UV_IMAGE=ghcr.io/astral-sh/uv:0.8.22

# 构建源（国内网络）
NPM_REGISTRY=https://registry.npmmirror.com
UV_INDEX_URL=https://mirrors.aliyun.com/pypi/simple

# 宿主机端口（默认值即文档端口；冲突时改这里）
MYSQL_HOST_PORT=3306
ES_HOST_PORT=9200
QDRANT_HOST_PORT=6333
BACKEND_HOST_PORT=8100
FRONTEND_HOST_PORT=8080

# 可选：外部 OpenAI 兼容端点。
# 三个都非空才启用 LLM；留空时使用确定性生成器。
REBUILD_LLM_BASE_URL=
REBUILD_LLM_API_KEY=
REBUILD_LLM_MODEL=
REBUILD_LLM_TIMEOUT=30
```

- [ ] **Step 2: Implement the generator**

Create `scripts/generate_docker_env.py`:

```python
#!/usr/bin/env python3
"""从 .env.example 生成带随机密码的 .env。"""

from __future__ import annotations

import argparse
import re
import secrets
import sys
from collections.abc import Sequence
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
EXAMPLE = ROOT / ".env.example"
DEFAULT_TARGET = ROOT / ".env"

SECRET_KEYS = (
    "MYSQL_ROOT_PASSWORD",
    "MYSQL_PASSWORD",
    "REBUILD_DW_RO_PASSWORD",
)


def _replace(text: str, key: str, value: str) -> str:
    pattern = re.compile(rf"^{re.escape(key)}=.*$", re.MULTILINE)
    if pattern.search(text) is None:
        raise SystemExit(f".env.example 缺少 {key}= 行")
    return pattern.sub(f"{key}={value}", text, count=1)


def render(example: str) -> str:
    """把三个占位密码替换成随机值；其他行原样保留。"""
    text = example
    for key in SECRET_KEYS:
        text = _replace(text, key, secrets.token_urlsafe(24))
    return text


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="生成 Docker Compose 的 .env（随机密码）"
    )
    parser.add_argument(
        "--force",
        action="store_true",
        help="覆盖已存在的 .env",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="只打印生成结果，不写文件",
    )
    parser.add_argument(
        "--output",
        type=Path,
        default=DEFAULT_TARGET,
        help="输出路径（默认仓库根 .env）",
    )
    args = parser.parse_args(argv)

    if not EXAMPLE.is_file():
        print(f"缺少模板：{EXAMPLE}", file=sys.stderr)
        return 1

    text = render(EXAMPLE.read_text(encoding="utf-8"))
    if args.dry_run:
        print(text, end="")
        return 0

    if args.output.exists() and not args.force:
        print(
            f"{args.output} 已存在；如需覆盖请加 --force",
            file=sys.stderr,
        )
        return 1

    args.output.write_text(text, encoding="utf-8")
    try:
        args.output.chmod(0o600)
    except OSError:
        # Windows 上 chmod 不保证生效；文件内容仍是正确的。
        pass
    print(f"已生成 {args.output}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
```

- [ ] **Step 3: Ignore `.env` and generated runtime files**

Modify `.gitignore` by adding these lines after the `# 本地` block:

```gitignore
# Docker 部署密钥（模板 .env.example 入库）
.env
.env.*
!.env.example
```

- [ ] **Step 4: Verify the renderer**

Run:

```bash
python scripts/generate_docker_env.py --dry-run > /tmp/rebuild-env.preview
python - <<'PY'
from pathlib import Path

text = Path("/tmp/rebuild-env.preview").read_text(encoding="utf-8")
for key in (
    "MYSQL_ROOT_PASSWORD",
    "MYSQL_PASSWORD",
    "REBUILD_DW_RO_PASSWORD",
):
    line = next(line for line in text.splitlines() if line.startswith(key + "="))
    value = line.split("=", 1)[1]
    assert value and value != "change-me-root", line
    assert value not in {"change-me-app", "change-me-ro"}, line
for key in ("MYSQL_IMAGE", "FRONTEND_HOST_PORT", "REBUILD_LLM_BASE_URL"):
    assert f"{key}=" in text, key
print("env render PASS")
PY
```

Expected: `env render PASS`.

- [ ] **Step 5: Verify the existing-file guard and `--force`**

Run:

```bash
tmpdir="$(mktemp -d)"
python scripts/generate_docker_env.py --output "$tmpdir/.env"
test -f "$tmpdir/.env"

if python scripts/generate_docker_env.py --output "$tmpdir/.env" \
    2>"$tmpdir/refusal.err"; then
  echo "expected refusal did not happen" >&2
  exit 1
fi
grep -q "已存在" "$tmpdir/refusal.err"

python scripts/generate_docker_env.py --output "$tmpdir/.env" --force
rm -rf "$tmpdir"
echo "env guard PASS"
```

Expected: `env guard PASS`.

- [ ] **Step 6: Checkpoint**

Report the generated preview and changed files. Do not commit.

---

### Task 3: backend/init 镜像

**Files:**
- Create: `docker/backend.Dockerfile`
- Create: `.dockerignore`

**Interfaces:**
- Produces image `shopkeeper-rebuild/backend:local`.
- Runtime layout:
  - working dir `/app/backend`
  - app package `/app/backend/app`
  - virtualenv `/app/backend/.venv`
  - contracts `/app/contracts`
  - fixtures `/app/fixtures`
- Image runs as UID `10001`; default command is single-worker Uvicorn on `8100`.
- `init` overrides the command with `python -m app.scripts.docker_init`.

- [ ] **Step 1: Confirm the build fails before the Dockerfile exists**

Run:

```bash
docker build -f docker/backend.Dockerfile -t shopkeeper-rebuild/backend:test .
```

Expected: FAIL with `failed to read dockerfile: ... docker/backend.Dockerfile: no such file or directory`.

- [ ] **Step 2: Create `.dockerignore`**

Create `.dockerignore`:

```text
.git
.gitignore
.env
.env.*
!.env.example

backend/.venv
backend/.pytest_cache
backend/tests
**/__pycache__
**/*.py[cod]

frontend/node_modules
frontend/dist
frontend/tests

docs
scripts
*.log
.DS_Store

# 构建需要保留：contracts/、fixtures/、backend/app/、backend/pyproject.toml、backend/uv.lock
```

- [ ] **Step 3: Create the backend Dockerfile**

Create `docker/backend.Dockerfile`:

```dockerfile
# syntax=docker/dockerfile:1.7

ARG PYTHON_IMAGE=python:3.12-slim
ARG UV_IMAGE=ghcr.io/astral-sh/uv:0.8.22

FROM ${UV_IMAGE} AS uv
FROM ${PYTHON_IMAGE} AS builder

COPY --from=uv /uv /uvx /bin/

ARG UV_INDEX_URL=https://pypi.org/simple
ENV UV_DEFAULT_INDEX=${UV_INDEX_URL} \
    UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy

WORKDIR /build
COPY backend/pyproject.toml backend/uv.lock ./
RUN --mount=type=cache,target=/root/.cache/uv \
    uv sync --frozen --no-dev --no-install-project

COPY backend/app ./app

FROM ${PYTHON_IMAGE} AS runtime

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PATH=/app/backend/.venv/bin:$PATH

RUN groupadd --system --gid 10001 app \
    && useradd --system --uid 10001 --gid app --home-dir /app \
       --shell /usr/sbin/nologin app \
    && mkdir -p /app/backend \
    && chown -R app:app /app

WORKDIR /app/backend

COPY --from=builder --chown=app:app /build/.venv /app/backend/.venv
COPY --from=builder --chown=app:app /build/app /app/backend/app
COPY --chown=app:app contracts /app/contracts
COPY --chown=app:app fixtures /app/fixtures

USER app
EXPOSE 8100

CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8100", "--workers", "1"]
```

- [ ] **Step 4: Build the image**

Run:

```bash
docker build \
  --build-arg PYTHON_IMAGE=docker.m.daocloud.io/library/python:3.12-slim \
  --build-arg UV_IMAGE=ghcr.io/astral-sh/uv:0.8.22 \
  -f docker/backend.Dockerfile \
  -t shopkeeper-rebuild/backend:test .
```

Expected: build succeeds.

If the build fails downloading locked PyPI artifacts, rerun with the mirror index:

```bash
docker build \
  --build-arg PYTHON_IMAGE=docker.m.daocloud.io/library/python:3.12-slim \
  --build-arg UV_IMAGE=ghcr.io/astral-sh/uv:0.8.22 \
  --build-arg UV_INDEX_URL=https://mirrors.aliyun.com/pypi/simple \
  -f docker/backend.Dockerfile \
  -t shopkeeper-rebuild/backend:test .
```

If it still fails, record the exact failing package URL; do not silently drop `--frozen` or hand-edit `uv.lock`.

- [ ] **Step 5: Verify import, non-root user, and init CLI**

Run:

```bash
docker run --rm shopkeeper-rebuild/backend:test \
  python -c "import app.main; print('import ok')"
docker run --rm --entrypoint id shopkeeper-rebuild/backend:test -u
docker run --rm shopkeeper-rebuild/backend:test \
  python -m app.scripts.docker_init --help
```

Expected:

- `import ok`
- `10001`
- argparse help containing `Docker 一次性初始化`

- [ ] **Step 6: Checkpoint**

Report image size and the three checks. Do not commit.

---

### Task 4: frontend 镜像与 Nginx SSE 配置

**Files:**
- Create: `docker/frontend.Dockerfile`
- Create: `docker/nginx.conf`

**Interfaces:**
- Produces image `shopkeeper-rebuild/frontend:local`.
- Nginx serves `/usr/share/nginx/html`, proxies `/api/` to `http://backend:8100`.
- `/healthz` returns `200 ok`.

- [ ] **Step 1: Confirm the build fails before the Dockerfile exists**

Run:

```bash
docker build -f docker/frontend.Dockerfile -t shopkeeper-rebuild/frontend:test .
```

Expected: FAIL with `failed to read dockerfile: ... docker/frontend.Dockerfile: no such file or directory`.

- [ ] **Step 2: Create the Nginx config**

Create `docker/nginx.conf`:

```nginx
server {
    listen 80;
    server_name _;

    root /usr/share/nginx/html;
    index index.html;

    location = /healthz {
        access_log off;
        add_header Content-Type text/plain;
        return 200 "ok\n";
    }

    # SSE：关闭缓冲/缓存，HTTP/1.1，清空 Connection，读超时拉长。
    location /api/ {
        proxy_pass http://backend:8100;
        proxy_http_version 1.1;
        proxy_set_header Host $host;
        proxy_set_header X-Real-IP $remote_addr;
        proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;
        proxy_set_header X-Forwarded-Proto $scheme;
        proxy_set_header Connection "";

        proxy_buffering off;
        proxy_cache off;
        gzip off;
        proxy_read_timeout 3600s;
        proxy_send_timeout 3600s;
        chunked_transfer_encoding on;
    }

    location / {
        try_files $uri $uri/ /index.html;
    }
}
```

- [ ] **Step 3: Create the frontend Dockerfile**

Create `docker/frontend.Dockerfile`:

```dockerfile
# syntax=docker/dockerfile:1.7

ARG NODE_IMAGE=node:20-alpine
ARG NGINX_IMAGE=nginx:1.27-alpine

FROM ${NODE_IMAGE} AS build

ARG NPM_REGISTRY=https://registry.npmjs.org
WORKDIR /app

COPY frontend/package.json frontend/package-lock.json ./
RUN npm ci --registry="${NPM_REGISTRY}"

COPY frontend/ ./
RUN npm run build

FROM ${NGINX_IMAGE} AS runtime

COPY docker/nginx.conf /etc/nginx/conf.d/default.conf
COPY --from=build /app/dist /usr/share/nginx/html

EXPOSE 80
```

- [ ] **Step 4: Build the image**

Run:

```bash
docker build \
  --build-arg NODE_IMAGE=docker.m.daocloud.io/library/node:20-alpine \
  --build-arg NGINX_IMAGE=docker.m.daocloud.io/library/nginx:1.27-alpine \
  --build-arg NPM_REGISTRY=https://registry.npmmirror.com \
  -f docker/frontend.Dockerfile \
  -t shopkeeper-rebuild/frontend:test .
```

Expected: build succeeds and `npm run build` prints `tsc`/Vite success.

- [ ] **Step 5: Verify static serving, health endpoint, and SSE config**

Run:

```bash
docker rm -f rebuild-frontend-test >/dev/null 2>&1 || true
docker run -d --name rebuild-frontend-test -p 18080:80 \
  shopkeeper-rebuild/frontend:test
sleep 2
curl -fsS http://127.0.0.1:18080/healthz
curl -fsS http://127.0.0.1:18080/ | grep -q '<div id="root">'
docker exec rebuild-frontend-test nginx -T 2>&1 | grep -q 'proxy_buffering off'
docker rm -f rebuild-frontend-test
```

Expected:

- `ok`
- `grep` exits 0 for the app shell
- `grep` exits 0 for `proxy_buffering off`
- own test container removed

- [ ] **Step 6: Checkpoint**

Report build result and checks. Do not commit.

---

### Task 5: Compose 五服务 + init + 端到端 smoke

**Files:**
- Create: `scripts/docker-smoke.sh`
- Create: `compose.yaml`

**Interfaces:**
- Consumes: `.env` generated in Task 2; images from Tasks 3/4.
- Produces: `docker compose up -d --build` starts `mysql`, `elasticsearch`, `qdrant`, `init`, `backend`, `frontend`.
- Host entry: `http://127.0.0.1:${FRONTEND_HOST_PORT:-8080}`.
- Smoke script exits 0 only if all seven acceptance checks pass.

- [ ] **Step 1: Write the smoke script first**

Create `scripts/docker-smoke.sh`:

```bash
#!/usr/bin/env bash
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT"

BASE="${DOCKER_BASE_URL:-http://127.0.0.1:8080}"

fail() {
  echo "DOCKER SMOKE FAIL: $*" >&2
  docker compose ps >&2 || true
  docker compose logs --tail=80 init backend frontend >&2 || true
  exit 1
}

if [ ! -f .env ]; then
  fail "缺少 .env；先运行 make docker-env"
fi

echo "== 1/7 启动 stack（含一次性 init） =="
docker compose up -d --build

echo "== 2/7 等待 ${BASE}/api/health =="
healthy=0
for attempt in $(seq 1 180); do
  if curl -fsS "$BASE/api/health" 2>/dev/null \
    | python -c 'import json,sys; d=json.load(sys.stdin); assert d.get("status")=="ok" and d.get("stage")=="S7"' \
    >/dev/null 2>&1; then
    healthy=1
    break
  fi
  sleep 2
done
if [ "$healthy" -ne 1 ]; then
  fail "360 秒内 /api/health 未就绪"
fi
echo "health: $(curl -fsS "$BASE/api/health")"

echo "== 3/7 五长驻服务运行 + /api/steps = 12 =="
running="$(docker compose ps --status running --services)"
for service in mysql elasticsearch qdrant backend frontend; do
  if ! grep -qx "$service" <<<"$running"; then
    fail "服务未运行：$service"
  fi
done
curl -fsS "$BASE/api/steps" \
  | python -c 'import json,sys; d=json.load(sys.stdin); assert len(d["steps"])==12; print("steps=%d" % len(d["steps"]))'

echo "== 4/7 经 8080 发起真实查询（SSE 必须 result 且行数 > 0） =="
python - "$BASE" <<'PY'
import json
import sys
import urllib.request

base = sys.argv[1]
payload = json.dumps({"query": "华北销售额"}, ensure_ascii=False).encode("utf-8")
request = urllib.request.Request(
    base + "/api/query",
    data=payload,
    headers={"Content-Type": "application/json; charset=utf-8"},
)
with urllib.request.urlopen(request, timeout=180) as response:
    body = response.read().decode("utf-8")

events = []
for block in body.split("\n\n"):
    for line in block.splitlines():
        if line.startswith("data:"):
            events.append(json.loads(line[len("data:"):].strip()))

assert events, "没有收到任何 SSE 事件"
last = events[-1]
assert last["type"] == "result", f"最后一条不是 result：{last}"
rows = last["data"]["rows"]
assert rows, "result.rows 为空"
print(f"result rows={len(rows)} columns={len(last['data']['columns'])}")
PY

echo "== 5/7 只读账号验证 =="
docker compose exec -T backend \
  python -m app.scripts.security --verify-ro-account

echo "== 6/7 S2 元知识对账（init 配置一次性执行） =="
docker compose run --rm --no-deps init \
  python -m app.build_meta_knowledge --verify

echo "== 7/7 init 幂等（必须跳过重建） =="
init_output="$(docker compose run --rm --no-deps init)"
printf '%s\n' "$init_output"
grep -q "meta: verify passed; skip rebuild" <<<"$init_output" \
  || fail "init 没有走跳过重建路径"

echo "DOCKER SMOKE PASS"
```

Make it executable:

```bash
chmod +x scripts/docker-smoke.sh scripts/generate_docker_env.py
```

- [ ] **Step 2: Run the smoke script to verify it fails before Compose exists**

Run:

```bash
test -f .env || python scripts/generate_docker_env.py
./scripts/docker-smoke.sh
```

Expected: FAIL at `docker compose up -d --build` with
`no configuration file provided: not found` (or equivalent missing `compose.yaml`).

- [ ] **Step 3: Create `compose.yaml`**

Create `compose.yaml`:

```yaml
name: shopkeeper-rebuild

x-backend-build: &backend-build
  context: .
  dockerfile: docker/backend.Dockerfile
  args:
    PYTHON_IMAGE: ${PYTHON_IMAGE:?}
    UV_IMAGE: ${UV_IMAGE:?}
    UV_INDEX_URL: ${UV_INDEX_URL:-https://pypi.org/simple}

services:
  mysql:
    image: ${MYSQL_IMAGE:?}
    restart: unless-stopped
    environment:
      - MYSQL_ROOT_PASSWORD=${MYSQL_ROOT_PASSWORD:?}
      - MYSQL_ROOT_HOST=%
      - MYSQL_DATABASE=dw
      - MYSQL_USER=didilili
      - MYSQL_PASSWORD=${MYSQL_PASSWORD:?}
    command:
      - --character-set-server=utf8mb4
      - --collation-server=utf8mb4_general_ci
    ports:
      - "127.0.0.1:${MYSQL_HOST_PORT:-3306}:3306"
    volumes:
      - mysql_data:/var/lib/mysql
      - ./fixtures/dw.sql:/docker-entrypoint-initdb.d/10-dw.sql:ro
    healthcheck:
      test:
        - CMD-SHELL
        - mysqladmin ping -h 127.0.0.1 -uroot -p$$MYSQL_ROOT_PASSWORD --silent
      interval: 5s
      timeout: 5s
      retries: 60
      start_period: 30s

  elasticsearch:
    image: ${ELASTICSEARCH_IMAGE:?}
    restart: unless-stopped
    environment:
      - discovery.type=single-node
      - xpack.security.enabled=false
      - ES_JAVA_OPTS=-Xms512m -Xmx512m
    ports:
      - "127.0.0.1:${ES_HOST_PORT:-9200}:9200"
    volumes:
      - es_data:/usr/share/elasticsearch/data
    healthcheck:
      test:
        - CMD-SHELL
        - curl -fsS 'http://localhost:9200/_cluster/health?wait_for_status=yellow&timeout=5s' >/dev/null
      interval: 10s
      timeout: 10s
      retries: 30
      start_period: 30s

  qdrant:
    image: ${QDRANT_IMAGE:?}
    restart: unless-stopped
    ports:
      - "127.0.0.1:${QDRANT_HOST_PORT:-6333}:6333"
    volumes:
      - qdrant_data:/qdrant/storage

  init:
    image: shopkeeper-rebuild/backend:local
    build: *backend-build
    restart: "no"
    command: ["python", "-m", "app.scripts.docker_init"]
    environment:
      - REBUILD_MYSQL_HOST=mysql
      - REBUILD_MYSQL_PORT=3306
      - REBUILD_MYSQL_USER=didilili
      - REBUILD_MYSQL_PASSWORD=${MYSQL_PASSWORD:?}
      - REBUILD_MYSQL_ROOT_USER=root
      - REBUILD_MYSQL_ROOT_PASSWORD=${MYSQL_ROOT_PASSWORD:?}
      - REBUILD_DW_RO_PASSWORD=${REBUILD_DW_RO_PASSWORD:?}
      - REBUILD_ES_URL=http://elasticsearch:9200
      - REBUILD_QDRANT_URL=http://qdrant:6333
    depends_on:
      mysql:
        condition: service_healthy
      elasticsearch:
        condition: service_healthy
      qdrant:
        condition: service_started

  backend:
    image: shopkeeper-rebuild/backend:local
    build: *backend-build
    restart: unless-stopped
    command:
      - uvicorn
      - app.main:app
      - --host
      - 0.0.0.0
      - --port
      - "8100"
      - --workers
      - "1"
    environment:
      - REBUILD_MYSQL_HOST=mysql
      - REBUILD_MYSQL_PORT=3306
      - REBUILD_DW_RO_PASSWORD=${REBUILD_DW_RO_PASSWORD:?}
      - REBUILD_ES_URL=http://elasticsearch:9200
      - REBUILD_QDRANT_URL=http://qdrant:6333
      - REBUILD_LLM_BASE_URL=${REBUILD_LLM_BASE_URL:-}
      - REBUILD_LLM_API_KEY=${REBUILD_LLM_API_KEY:-}
      - REBUILD_LLM_MODEL=${REBUILD_LLM_MODEL:-}
      - REBUILD_LLM_TIMEOUT=${REBUILD_LLM_TIMEOUT:-30}
    ports:
      - "127.0.0.1:${BACKEND_HOST_PORT:-8100}:8100"
    depends_on:
      init:
        condition: service_completed_successfully
    healthcheck:
      test:
        - CMD
        - python
        - -c
        - import urllib.request; urllib.request.urlopen('http://127.0.0.1:8100/api/health', timeout=3)
      interval: 10s
      timeout: 5s
      retries: 12
      start_period: 10s

  frontend:
    image: shopkeeper-rebuild/frontend:local
    build:
      context: .
      dockerfile: docker/frontend.Dockerfile
      args:
        NODE_IMAGE: ${NODE_IMAGE:?}
        NGINX_IMAGE: ${NGINX_IMAGE:?}
        NPM_REGISTRY: ${NPM_REGISTRY:-https://registry.npmjs.org}
    restart: unless-stopped
    ports:
      - "${FRONTEND_HOST_PORT:-8080}:80"
    depends_on:
      backend:
        condition: service_healthy
    healthcheck:
      test:
        - CMD-SHELL
        - wget -qO- http://127.0.0.1/healthz >/dev/null
      interval: 10s
      timeout: 5s
      retries: 12
      start_period: 5s

volumes:
  mysql_data:
  es_data:
  qdrant_data:
```

- [ ] **Step 4: Validate the Compose model**

Run:

```bash
docker compose config --quiet
```

Expected: exit 0 with no output.

- [ ] **Step 5: Free the host ports before starting the stack**

The Compose stack publishes `127.0.0.1:8100`; a local dev backend on 8100
will make `backend` fail to bind. Stop the dev backend, or temporarily set
`BACKEND_HOST_PORT=8101` in `.env` (the smoke test only uses `8080`, so the
frontend path is unaffected). The final acceptance run should use 8100.

Check the five host ports:

```bash
for port in 3306 9200 6333 8100 8080; do
  if command -v netstat >/dev/null 2>&1; then
    netstat -ano | grep -E "[:.]${port}[[:space:]]" || true
  fi
done
```

Expected: no unexpected listener for 3306/9200/6333/8100/8080. If 8100 is the
dev backend started earlier in this repository, stop it with `Ctrl+C` in that
terminal (or the harness task running it) before continuing.

- [ ] **Step 6: Run the smoke test**

Run:

```bash
test -f .env || python scripts/generate_docker_env.py
./scripts/docker-smoke.sh
```

Expected: `DOCKER SMOKE PASS`; all seven sections print success.

If this is the first run and a previous partial stack created volumes, reset only the stack created by this plan:

```bash
docker compose down -v
./scripts/docker-smoke.sh
```

Expected: fresh MySQL imports `fixtures/dw.sql`; `init` creates `dw_reader`, builds S2, exits 0; backend starts; smoke passes.

- [ ] **Step 7: Verify a second `init` does not rebuild**

Run:

```bash
docker compose run --rm --no-deps init | tee /tmp/rebuild-init-second.log
grep -q "meta: verify passed; skip rebuild" /tmp/rebuild-init-second.log
```

Expected: `grep` exits 0.

- [ ] **Step 8: Checkpoint**

Report `docker compose ps`, smoke output summary, and init idempotency result. Do not commit.

---

### Task 6: Makefile 入口与部署文档

**Files:**
- Modify: `Makefile`
- Create: `docs/DOCKER-DEPLOY.md`
- Modify: `README.md`
- Modify: `docs/SETUP-FROM-SCRATCH.md`

**Interfaces:**
- Consumes: `compose.yaml`, `.env.example`, `scripts/generate_docker_env.py`, `scripts/docker-smoke.sh`.
- Produces Make targets:
  - `docker-env` — create `.env` only if absent
  - `docker-up` — `docker compose up -d --build`
  - `docker-down` — `docker compose down` (keeps volumes)
  - `docker-logs` — follow logs
  - `docker-smoke` — run end-to-end smoke
  - `docker-reset` — explicit `yes` confirmation, then `down -v`

- [ ] **Step 1: Add Make targets**

Modify the `.PHONY` line to include the Docker targets:

```make
.PHONY: install dev-backend dev-frontend stage0 stage0-ui stage1 stage2 stage3 stage4 stage5 stage5-live stage6 stage6-ui stage7 stage7-ui baseline docker-env docker-up docker-down docker-logs docker-smoke docker-reset
```

Append this block at the end of `Makefile`:

```make
# ---------------------------------------------------------------------------
# Docker 部署（单机 Compose；详见 docs/DOCKER-DEPLOY.md）
# ---------------------------------------------------------------------------

docker-env:
	@test -f .env || python scripts/generate_docker_env.py

docker-up: docker-env
	docker compose up -d --build

docker-down:
	docker compose down

docker-logs:
	docker compose logs -f --tail=100

docker-smoke: docker-env
	./scripts/docker-smoke.sh

docker-reset:
	@printf '这会删除 mysql_data、es_data、qdrant_data。输入 yes 继续：'; \
	read -r answer; \
	test "$$answer" = "yes"
	docker compose down -v
```

- [ ] **Step 2: Verify Make target expansion**

Run:

```bash
make -n docker-env
make -n docker-up
make -n docker-smoke
make -n docker-reset
```

Expected: each prints the expected commands; `docker-reset` prints the confirmation loop and `docker compose down -v`.

- [ ] **Step 3: Create the deployment guide**

Create `docs/DOCKER-DEPLOY.md`:

```markdown
# Docker 部署

把 rebuild 的前端、后端、MySQL、Elasticsearch、Qdrant 和一次性初始化任务
放进单机 Docker Compose。对外入口只有 `http://<host>:8080`。

## 1. 前置条件

- Docker Engine / Docker Desktop 可用，`docker compose version` 为 v2。
- 首次构建需要网络：拉取基础镜像、npm 包、Python 包。
- 建议至少 8 GB 内存（ES 堆固定 512 MB）。
- 端口默认 `8080`（前端）、`8100`（后端）、`3306`、`9200`、`6333`；
  都可在 `.env` 的 `*_HOST_PORT` 中修改。

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

`init` 是一次性任务，正常退出后 `docker compose ps` 会显示为 exited 0。
需要重新执行初始化时：

```bash
docker compose run --rm --no-deps init
```

## 4. 密钥与 LLM

- `.env` 不入库；`.env.example` 是模板。
- `MYSQL_ROOT_PASSWORD` / `MYSQL_PASSWORD` 只注入 `init` 和 MySQL；
  `backend` 只拿 `REBUILD_DW_RO_PASSWORD`。
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

命名卷：`mysql_data`、`es_data`、`qdrant_data`。`make docker-down` 保留它们。

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
```

- [ ] **Step 4: Add the README Docker section**

Modify `README.md`:

1. In the top link list, add:

```markdown
- Docker 部署：[`docs/DOCKER-DEPLOY.md`](docs/DOCKER-DEPLOY.md)
```

2. After the `快速开始` code block and before the Windows note, add:

```markdown
### Docker 一键部署

前置：Docker Compose v2；首次构建需要网络。

```bash
make docker-env      # 生成 .env（随机密码；已存在则跳过）
make docker-up       # 构建并启动五服务 + 一次性 init
curl http://127.0.0.1:8080/api/health
make docker-smoke    # 完整验收
```

详见 [`docs/DOCKER-DEPLOY.md`](docs/DOCKER-DEPLOY.md)。
```

3. Replace the S2 command block：

```bash
# 前置：三存储在运行；已有则跳过。不要 up 全部，也不要 down -v。
(cd ../shopkeeper-agent/docker && docker compose up -d mysql elasticsearch qdrant)
```

with:

```bash
# 本地开发：在仓库根目录只启动三存储（首次需 make docker-env）
docker compose up -d mysql elasticsearch qdrant
```

4. Replace constraint 4：

```markdown
4. **S2/S3 的外部前置**：旧系统 compose 里的 MySQL / ES / Qdrant 已启动；
   ES 需要带 IK 分词插件的镜像。S2 默认哈希向量，embedding 容器坏了不影响。
```

with:

```markdown
4. **S2/S3 的外部前置**：仓库根 `compose.yaml` 可只启动
   `mysql elasticsearch qdrant`；ES 使用官方镜像，S2 自动回退 `standard`
   analyzer，IK 是后续可选增强。S2 默认哈希向量，不需要 embedding 容器。
```

- [ ] **Step 5: Update the setup guide's old compose path**

Modify `docs/SETUP-FROM-SCRATCH.md`:

1. Replace the standalone quick-start block:

```bash
# 4) 启动 MySQL / ES / Qdrant（只启动这三个）
cd ../shopkeeper-agent/docker
docker compose up -d mysql elasticsearch qdrant
cd ../../rebuild
```

with:

```bash
# 4) 启动 MySQL / ES / Qdrant（只启动这三个；首次需 make docker-env）
make docker-env
docker compose up -d mysql elasticsearch qdrant
```

2. Replace the section 2.4 opening:

```bash
cd ../shopkeeper-agent/docker

# 只启动这三个，不要 up 全部
docker compose up -d mysql elasticsearch qdrant
```

with:

```bash
# 在仓库根目录执行
make docker-env
docker compose up -d mysql elasticsearch qdrant
```

3. Replace the “首次启动可能比较久” bullet list with:

```markdown
- Elasticsearch 使用官方 8.15.3 镜像，不含 IK；S2 会自动回退 `standard` analyzer；
- MySQL / ES 有 healthcheck；
- Qdrant 没有 healthcheck，`init` 会自己轮询 `/collections`。
```

4. Replace `docker compose exec mysql mysqladmin ping -h 127.0.0.1 -uroot -p<旧口令>`
with:

```bash
docker compose exec mysql sh -c \
  'mysqladmin ping -h 127.0.0.1 -uroot -p"$MYSQL_ROOT_PASSWORD"'
```

5. In section 2.4, replace the line `cd ../../rebuild` with:

```bash
# 仍在仓库根目录
```

6. Replace the “重要：不要启动全部服务” section body with:

```markdown
`compose.yaml` 里的 `backend` / `frontend` 是 rebuild 自己的服务；如果只是给
本机 S2/S3 准备存储，按上面的命令只启动三个。不要用旧系统的 compose，
也不要在旧系统目录执行 `down -v`。

S2/S3 默认用 `hash-ngram-v1` 哈希向量，不需要 embedding 容器。
```

7. Replace the row：

```markdown
| `S2 FAIL: ... unknown analyzer [ik_max_word]` | 用了普通 ES 镜像，不是带 IK 的 compose 镜像 | 用 `code/shopkeeper-agent/docker` 里的 `elasticsearch` 构建 |
```

with：

```markdown
| `S2 FAIL: ... unknown analyzer [ik_max_word]` | 官方 ES 没有 IK；S2 应自动回退 `standard` | 若仍失败，看 `init` 日志并确认 `es_store.create()` 的回退分支 |
```

8. Replace the row：

```markdown
| `make stage5-live` 连不上 3306 | MySQL 没起 | 按 2.4 只启动 mysql；`dw` 库要由 `fixtures/dw.sql` 装载 |
```

with：

```markdown
| `make stage5-live` 连不上 3306 | MySQL 没起 | 按 2.4 启动 mysql；首次启动会由 entrypoint 导入 `fixtures/dw.sql` |
```

- [ ] **Step 7: Verify docs commands and links**

Run:

```bash
make -n docker-up
make -n docker-smoke
test -f docs/DOCKER-DEPLOY.md
grep -q "Docker 一键部署" README.md
grep -q "docs/DOCKER-DEPLOY.md" README.md
grep -q "compose.yaml" docs/SETUP-FROM-SCRATCH.md
```

Expected: all commands exit 0.

- [ ] **Step 8: Checkpoint**

Report the Make target dry-runs and doc links. Do not commit.

---

### Task 7: 最终验收

**Files:**
- No new files.
- Verify all files from Tasks 1–6.

**Interfaces:**
- Consumes: entire Docker stack and host dev environment.
- Produces: recorded acceptance evidence; no code changes unless a check fails.

- [ ] **Step 1: Validate Compose and image builds**

Run:

```bash
docker compose config --quiet
docker compose build
```

Expected: both exit 0.

- [ ] **Step 2: Run the full Docker smoke**

Run:

```bash
make docker-smoke
```

Expected: `DOCKER SMOKE PASS`.

- [ ] **Step 3: Run the host regression suite**

Run:

```bash
make stage7
```

Expected: `S7 PASS`; backend tests include the three new `test_docker_init.py` cases.

- [ ] **Step 4: Confirm the stack state**

Run:

```bash
docker compose ps
```

Expected:

- `mysql`, `elasticsearch`, `qdrant`, `backend`, `frontend` are `running` /
  `healthy`;
- `init` is `exited (0)`.

- [ ] **Step 5: Final checkpoint**

Report:

- `docker compose ps` summary;
- smoke output (health, steps, result rows, RO verify, S2 verify, skip rebuild);
- host test totals;
- any skipped checks and why.

Do not commit or push unless the user explicitly asks.

---

## Self-Review Notes

- Spec coverage: five services + init → Task 5; backend/frontend images → Tasks 3/4;
  secrets → Task 2; MySQL import + `dw_reader` → Tasks 1/5; S2 idempotency → Task 1;
  Nginx SSE → Task 4; tests → Tasks 1/5/7; docs → Task 6.
- Placeholder scan: no `TBD`/`TODO`; every code file has a complete draft.
- Type consistency: `initialize` uses `runner` and `wait` consistently in tests and
  implementation; `verify_command` is reused for the post-fresh verification;
  `wait_for_services` is referenced by `main`.
- Deliberate deviation: backend does not receive the regular/root MySQL passwords.
  Meta verification in smoke runs through the `init` service with a one-off command,
  which keeps runtime least privilege.
