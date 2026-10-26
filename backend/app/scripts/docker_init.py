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


def _check_es_ready(url: str) -> None:
    """ES 的 `wait_for_status=yellow` 在 red 集群上也会返回 200。

    必须读 JSON 里的 `status`，否则 red 会被当成就绪，导致 init 过早
    跑一次没必要的 `--fresh`。
    """
    response = httpx.get(url, timeout=3.0)
    if response.status_code < 200 or response.status_code >= 300:
        raise RuntimeError(f"{url} -> HTTP {response.status_code}")
    status = response.json().get("status")
    if status not in {"yellow", "green"}:
        raise RuntimeError(f"{url} -> cluster status {status!r}")


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
    """轮询三个存储；每个存储各自最多等待 `timeout_s`，全部就绪才返回。"""
    config = BuildConfig.from_env()
    _wait_until(
        "mysql",
        _connect_mysql,
        timeout_s=timeout_s,
        interval_s=interval_s,
    )
    _wait_until(
        "elasticsearch",
        lambda: _check_es_ready(
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
