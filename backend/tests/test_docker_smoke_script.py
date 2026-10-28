"""``scripts/docker-smoke.sh`` 必须问 Compose frontend 实际发布在哪个端口。

自己 ``sed`` 一遍 ``.env`` 拿 ``FRONTEND_HOST_PORT`` 是靠不住的：Compose 解析
``${FRONTEND_HOST_PORT:-8080}`` 是"shell 优先、再 .env"。用户在 shell 里
``export`` 过这个变量时，容器发布在另一个端口上，而验收脚本还在戳 ``.env``
里那个端口——于是报"360 秒内 /api/health 未就绪"，真正的原因（端口不对）
一个字都不提。

唯一的权威是 Compose 自己：``docker compose port frontend 80`` 报的就是
实际发布在宿主机的端口。

这里的 docker / curl / python 都是假的：脚本只跑到"用哪个 URL 去戳"这一步，
不碰真栈。
"""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

import pytest

from shellkit import fake_program, run_in_sh, to_shell_path, usable_sh

ROOT = Path(__file__).resolve().parents[2]
SMOKE = ROOT / "scripts" / "docker-smoke.sh"

SH = usable_sh()

# 假 docker 报的端口：和 .env / shell 里写的都不一样，才测得出脚本问的是谁。
PUBLISHED_PORT = 18080

# 假的 docker：只回答验收脚本会问的那几个问题。
FAKE_DOCKER = """
case "$*" in
  *"port frontend 80"*)
    echo "0.0.0.0:__PORT__"
    ;;
  *"ps -a --format"*)
    printf 'mysql\\trunning\\thealthy\\t0\\n'
    printf 'elasticsearch\\trunning\\thealthy\\t0\\n'
    printf 'qdrant\\trunning\\thealthy\\t0\\n'
    printf 'init\\texited\\t\\t0\\n'
    printf 'backend\\trunning\\thealthy\\t0\\n'
    printf 'frontend\\trunning\\thealthy\\t0\\n'
    ;;
  *"nginx -T"*)
    cat "__NGINX_FILE__"
    exit __NGINX_EXIT__
    ;;
  *"run --rm --no-deps init"*)
    echo "meta: verify passed; skip rebuild"
    ;;
esac
exit 0
"""

# 假的 curl：把每次请求的地址记下来（验收脚本问的是哪个端口，看这个就知道）。
FAKE_CURL = """
echo "$@" >> "__LOG__"
echo '{"status":"ok","stage":"S7"}'
exit 0
"""

# 假的 python：验收脚本只关心它成功退出。
#
# 例外是 nginx 那个子命令：它要读 stdin 上的生效配置，而"生效配置怎么判"
# 正是要验的东西——所以那一个交给**真**脚本跑，其余子命令一律成功退出。
# 每次调用都记下参数：验收脚本有没有把 nginx 的生效配置交给 SSE 那一步，
# 看这个就知道。
FAKE_PYTHON = """
echo "$@" >> "__LOG__"
for arg in "$@"; do
  if [ "$arg" = "nginx" ]; then
    exec "__REAL_PYTHON__" "__SCRIPT__" nginx
  fi
done
exit 0
"""

# 假的 nginx 生效配置：默认是一份正常的配置（/api/ 关着缓冲）。
#
# 真的 nginx -T 的输出里，每一份被读进来的配置文件前面都有
# "# configuration file <路径>:" 这一行——这是"这是一份完整的生效配置"
# 的标志。验收脚本要认它。
DEFAULT_NGINX_CONF = """
# configuration file /etc/nginx/nginx.conf:
server {
    location /api/ {
        proxy_pass http://backend:8100;
        proxy_buffering off;
    }
    location / {
        try_files $uri $uri/ /index.html;
    }
}
"""

# 同样的配置，但没有 "# configuration file" 头——取到一半断了的输出。
DUMP_WITHOUT_FILE_MARKERS = """
server {
    location /api/ {
        proxy_pass http://backend:8100;
        proxy_buffering off;
    }
    location / {
        try_files $uri $uri/ /index.html;
    }
}
"""


def _prepare(tmp_path: Path, env_body: str) -> Path:
    """在临时目录里摆一套 scripts/ 和 .env，返回验收脚本的路径。"""
    scripts_dir = tmp_path / "scripts"
    scripts_dir.mkdir(exist_ok=True)
    for name in ("docker-smoke.sh", "find-python.sh", "smoke_checks.py"):
        source = ROOT / "scripts" / name
        assert source.exists(), f"缺少 scripts/{name}"
        (scripts_dir / name).write_text(
            source.read_text(encoding="utf-8"), encoding="utf-8", newline="\n"
        )
    (tmp_path / ".env").write_text(env_body, encoding="utf-8", newline="\n")
    return scripts_dir / "docker-smoke.sh"


def _run(
    tmp_path: Path,
    env_body: str,
    nginx_conf: str = DEFAULT_NGINX_CONF,
    nginx_exit: int = 0,
) -> tuple[subprocess.CompletedProcess, str]:
    """跑一次验收脚本，返回 (结果, 假 curl 收到的地址们)。"""
    script = _prepare(tmp_path, env_body)
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    log = tmp_path / "curl.log"
    nginx_file = tmp_path / "nginx.conf"
    nginx_file.write_text(nginx_conf, encoding="utf-8", newline="\n")

    fake_program(
        bin_dir,
        "docker",
        FAKE_DOCKER.replace("__PORT__", str(PUBLISHED_PORT))
        .replace("__NGINX_FILE__", to_shell_path(nginx_file))
        .replace("__NGINX_EXIT__", str(nginx_exit)),
    )
    fake_program(bin_dir, "curl", FAKE_CURL.replace("__LOG__", to_shell_path(log)))
    fake_python = (
        FAKE_PYTHON.replace("__REAL_PYTHON__", to_shell_path(sys.executable))
        .replace(
            "__SCRIPT__", to_shell_path(tmp_path / "scripts" / "smoke_checks.py")
        )
        .replace("__LOG__", to_shell_path(tmp_path / "python.log"))
    )
    fake_program(bin_dir, "python3", fake_python)
    fake_program(bin_dir, "python", fake_python)

    result = run_in_sh(SH or "sh", script, bin_dir)
    calls = log.read_text(encoding="utf-8") if log.exists() else ""
    return result, calls


@pytest.mark.skipif(SH is None, reason="没有可用的 sh 来跑这个脚本")
def test_it_probes_the_port_compose_actually_published(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """脚本要戳 Compose 实际发布的端口，不是 .env 里写的那个。

    .env 里写 8080、Compose 实际发布在 18080——脚本按 .env 戳，
    就是对着一个没人监听的端口问健康，得到的结论毫无意义。
    """
    monkeypatch.delenv("DOCKER_BASE_URL", raising=False)
    monkeypatch.delenv("FRONTEND_HOST_PORT", raising=False)

    result, calls = _run(tmp_path, "FRONTEND_HOST_PORT=8080\n")

    assert result.returncode == 0, result.stderr
    assert f":{PUBLISHED_PORT}/api/health" in calls, calls
    assert ":8080/api/health" not in calls, calls


@pytest.mark.skipif(SH is None, reason="没有可用的 sh 来跑这个脚本")
def test_it_asks_compose_instead_of_parsing_the_env_file(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """shell 里设过端口时，脚本也不能自己去读 .env 拿另一个。

    Compose 的规则是"shell 优先、再 .env"；验收脚本自己实现一套解析，
    拿到的就可能是另一个端口。
    """
    monkeypatch.delenv("DOCKER_BASE_URL", raising=False)
    monkeypatch.setenv("FRONTEND_HOST_PORT", "9999")

    result, calls = _run(tmp_path, "FRONTEND_HOST_PORT=8080\n")

    assert result.returncode == 0, result.stderr
    assert f":{PUBLISHED_PORT}/api/health" in calls, calls
    assert ":8080/api/health" not in calls, calls
    assert ":9999/api/health" not in calls, calls


@pytest.mark.skipif(SH is None, reason="没有可用的 sh 来跑这个脚本")
def test_it_honours_an_explicit_base_url(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """DOCKER_BASE_URL 明确指定了就用它——那是用户自己说的地址。"""
    monkeypatch.setenv("DOCKER_BASE_URL", "http://127.0.0.1:12345")

    result, calls = _run(tmp_path, "FRONTEND_HOST_PORT=8080\n")

    assert result.returncode == 0, result.stderr
    assert "http://127.0.0.1:12345/api/health" in calls, calls


# ---------------------------------------------------------------------------
# nginx 的缓冲判据必须落在**处理 /api/ 的那个 location** 上
#
# 原来那条 `grep -q "proxy_buffering off;"` 扫的是 `nginx -T` 的**全部**输出：
# 只要配置里**任何地方**出现过这句话就算过。于是把 /api/ 的缓冲改成 on、
# 而另一个 location（比如 /static/）留着 off，这一条照样"通过"——SSE 实际
# 已经被攒成整包，验收却报 PASS。
# ---------------------------------------------------------------------------

# 缓冲只在别的 location 上关着，/api/ 反而开着——这正是原来那条 grep
# 会放过去的情形。
BUFFERED_API_LOCATION = """
server {
    location /static/ {
        proxy_buffering off;
    }
    location /api/ {
        proxy_pass http://backend:8100;
        proxy_buffering on;
    }
}
"""


@pytest.mark.skipif(SH is None, reason="没有可用的 sh 来跑这个脚本")
def test_it_fails_when_the_api_location_still_buffers(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """处理 /api/ 的 location 还开着缓冲，验收必须失败。"""
    monkeypatch.delenv("DOCKER_BASE_URL", raising=False)

    result, _ = _run(
        tmp_path, "FRONTEND_HOST_PORT=8080\n", nginx_conf=BUFFERED_API_LOCATION
    )

    assert result.returncode != 0, (
        "SSE 的 location 还开着缓冲，验收却通过了：\n"
        + result.stdout
        + result.stderr
    )


@pytest.mark.skipif(SH is None, reason="没有可用的 sh 来跑这个脚本")
def test_it_passes_when_the_api_location_has_buffering_off(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """正常配置要过——判据不是"有没有 off"，是"处理 /api/ 的那个有没有 off"。"""
    monkeypatch.delenv("DOCKER_BASE_URL", raising=False)

    result, _ = _run(tmp_path, "FRONTEND_HOST_PORT=8080\n")

    assert result.returncode == 0, result.stderr


@pytest.mark.skipif(SH is None, reason="没有可用的 sh 来跑这个脚本")
def test_the_sse_check_is_handed_the_nginx_configuration(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """SSE 那一步要拿到 nginx 的生效配置。

    响应快到 200 毫秒内就结束的时候，从时间上分辨不出是否流式——那一档
    唯一的证据就是这份配置。脚本不给它，检查就只能判不通过。
    """
    monkeypatch.delenv("DOCKER_BASE_URL", raising=False)

    result, _ = _run(tmp_path, "FRONTEND_HOST_PORT=8080\n")
    assert result.returncode == 0, result.stderr

    calls = (tmp_path / "python.log").read_text(encoding="utf-8")
    sse_calls = [line for line in calls.splitlines() if " sse " in line]
    assert sse_calls, calls
    assert "--nginx-conf" in sse_calls[0], sse_calls[0]


# ---------------------------------------------------------------------------
# nginx 的生效配置必须是一份**完整**的
#
# 原来那行是 `docker compose exec -T frontend nginx -T >file 2>/dev/null || true`：
# 退出码被 `|| true` 吞了。于是 nginx -T 自己失败（配置有问题、容器里的
# nginx 根本没起来）时，脚本照样往下走——拿半份输出当"生效配置"。
#
# 而这份配置是 SSE 那一档**唯一**的证据：拿不到证据时该判不通过，
# 拿一份残缺的当证据则更糟——它会让验收报 PASS。
# ---------------------------------------------------------------------------


@pytest.mark.skipif(SH is None, reason="没有可用的 sh 来跑这个脚本")
def test_it_fails_when_the_nginx_dump_could_not_be_read(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """nginx -T 退出非 0——不能拿它留下的半份输出当证据。

    这里假 docker 退出码是 1，但**打印的是一份完整的好配置**：旧脚本
    照样通过（它不看退出码），这正是要堵的那个洞。
    """
    monkeypatch.delenv("DOCKER_BASE_URL", raising=False)

    result, _ = _run(tmp_path, "FRONTEND_HOST_PORT=8080\n", nginx_exit=1)

    assert result.returncode != 0, (
        "nginx -T 失败了，验收却通过了：\n" + result.stdout + result.stderr
    )


@pytest.mark.skipif(SH is None, reason="没有可用的 sh 来跑这个脚本")
def test_it_fails_when_the_nginx_output_is_not_a_configuration_dump(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """拿到的不是 nginx -T 的输出——多半是取到一半就断了。

    真的 nginx -T 每一份被读进来的配置前面都有 "# configuration file" 这一行。
    没有它，说明这不是一份完整的生效配置。
    """
    monkeypatch.delenv("DOCKER_BASE_URL", raising=False)

    result, _ = _run(
        tmp_path,
        "FRONTEND_HOST_PORT=8080\n",
        nginx_conf=DUMP_WITHOUT_FILE_MARKERS,
    )

    assert result.returncode != 0, (
        "nginx 的生效配置是残缺的，验收却通过了：\n" + result.stdout + result.stderr
    )


@pytest.mark.skipif(SH is None, reason="没有可用的 sh 来跑这个脚本")
def test_it_refuses_to_certify_a_stack_that_still_uses_the_placeholder_passwords(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """.env 里还是模板的占位密码——这份部署的密码是公开的。

    ``.env.example`` 里的 ``change-me-*`` 会被 ``${MYSQL_PASSWORD:?}``
    当成合法值（它只查"非空"），栈于是照常起来、验收照常通过——而
    数据库的密码谁都知道。验收脚本不该给这样的部署盖章。
    """
    monkeypatch.delenv("DOCKER_BASE_URL", raising=False)

    result, _ = _run(
        tmp_path,
        "FRONTEND_HOST_PORT=8080\nMYSQL_PASSWORD=change-me-app\n",
    )

    assert result.returncode != 0, (
        "密码还是模板里的占位值，验收却通过了：\n" + result.stdout + result.stderr
    )


@pytest.mark.skipif(SH is None, reason="没有可用的 sh 来跑这个脚本")
def test_it_passes_with_real_generated_passwords(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """正常的 .env（generate_docker_env.py 生成的随机密码）要过。"""
    monkeypatch.delenv("DOCKER_BASE_URL", raising=False)

    result, _ = _run(
        tmp_path,
        "FRONTEND_HOST_PORT=8080\nMYSQL_PASSWORD=aG7kQ2xLp9Zt\n",
    )

    assert result.returncode == 0, result.stderr
