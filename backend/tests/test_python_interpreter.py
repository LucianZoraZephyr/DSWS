"""宿主机 Python 解释器的探测必须是"真的能跑"，不能只看名字在不在 PATH 上。

Windows 上 ``C:\\Users\\<user>\\AppData\\Local\\Microsoft\\WindowsApps\\python3``
是 Microsoft Store 的转发器（AppInstallerPythonRedirector.exe）：文件存在，
``command -v python3`` 查得到，但一执行就以 49 退出、什么也不输出。

只按"名字存在"挑解释器，坏的是两个地方：

* ``make docker-env`` —— 直接以 ``Error 49`` 失败，看不出是解释器的问题；
* ``scripts/docker-smoke.sh`` —— 挑中这个假解释器后，
  ``/api/health`` 的 180 次重试全部失败，最后报"360 秒内未就绪"，
  报错指向健康检查，真正的原因（解释器根本跑不起来）被埋在日志里。

所以判定标准统一成：**实际执行一次**，能跑通才算数。
"""

from __future__ import annotations

import os
import shutil
import subprocess
from pathlib import Path

import pytest

from shellkit import (
    fake_program,
    path_without_python,
    run_in_sh,
    to_shell_path,
    usable_bash,
    usable_sh,
)

ROOT = Path(__file__).resolve().parents[2]
FIND_PYTHON = ROOT / "scripts" / "find-python.sh"
MAKEFILE = ROOT / "Makefile"
GENERATE_ENV = ROOT / "scripts" / "generate_docker_env.py"

BASH = usable_bash()
SH = usable_sh()


def _fake_python(directory: Path, name: str, *, exit_code: int) -> Path:
    """造一个"存在但行为固定"的解释器桩。"""
    return fake_program(directory, name, f"exit {exit_code}")


def _fake_python_with_version(directory: Path, name: str, version: str) -> Path:
    """造一个"跑得起来、但版本是 X"的解释器桩。

    find-python.sh 是拿 ``python -c '...'`` 去问版本的，桩得按被问的
    东西给不同的答案：命令行里提到 version_info 就报这个版本，
    并且让版本检查以非 0 收场（"太老"就是用退出码表达的）。
    """
    return fake_program(
        directory,
        name,
        f'case "$*" in *version_info*) echo "{version}"; exit 1 ;; esac\n'
        "exit 0",
    )


# ---------------------------------------------------------------------------
# scripts/find-python.sh
# ---------------------------------------------------------------------------


@pytest.mark.skipif(SH is None, reason="没有可用的 sh 来跑这个脚本")
def test_skips_an_interpreter_that_exists_but_cannot_run(tmp_path: Path) -> None:
    """Store 转发器存在但一执行就退 49——必须被跳过。"""
    assert FIND_PYTHON.exists(), "缺少 scripts/find-python.sh"
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    _fake_python(bin_dir, "python3", exit_code=49)
    good = _fake_python(bin_dir, "python", exit_code=0)

    result = run_in_sh(SH or "sh", FIND_PYTHON, bin_dir)

    assert result.returncode == 0, result.stderr
    chosen = result.stdout.strip()
    assert chosen, "没有挑出解释器"
    assert Path(chosen).name == good.name, f"挑中了不能跑的 {chosen}"
    # 桩解释器是个 sh 脚本，交给 shell 跑它——用原生 Python 去执行
    # MSYS 风格的 /c/... 路径是跑不起来的。
    probe = subprocess.run(
        [SH or "sh", chosen, "-c", "import sys"],
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert probe.returncode == 0


@pytest.mark.skipif(SH is None, reason="没有可用的 sh 来跑这个脚本")
def test_fails_when_no_interpreter_can_run(tmp_path: Path) -> None:
    assert FIND_PYTHON.exists(), "缺少 scripts/find-python.sh"
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    _fake_python(bin_dir, "python3", exit_code=49)
    _fake_python(bin_dir, "python", exit_code=49)

    result = run_in_sh(SH or "sh", FIND_PYTHON, bin_dir)

    assert result.returncode != 0
    assert result.stdout.strip() == ""


@pytest.mark.skipif(SH is None, reason="没有可用的 sh 来跑这个脚本")
def test_the_failure_explains_which_interpreters_were_tried(tmp_path: Path) -> None:
    """失败时要给出**看得懂的**提示，而不是只留一个退出码。"""
    assert FIND_PYTHON.exists(), "缺少 scripts/find-python.sh"
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    _fake_python(bin_dir, "python3", exit_code=49)
    _fake_python(bin_dir, "python", exit_code=49)

    result = run_in_sh(SH or "sh", FIND_PYTHON, bin_dir)

    assert result.returncode != 0
    assert "python" in result.stderr.lower(), result.stderr


@pytest.mark.skipif(SH is None, reason="没有可用的 sh 来跑这个脚本")
def test_prefers_python3_when_it_works(tmp_path: Path) -> None:
    assert FIND_PYTHON.exists(), "缺少 scripts/find-python.sh"
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    _fake_python(bin_dir, "python3", exit_code=0)
    _fake_python(bin_dir, "python", exit_code=0)

    result = run_in_sh(SH or "sh", FIND_PYTHON, bin_dir)

    assert result.returncode == 0, result.stderr
    assert Path(result.stdout.strip()).name == "python3"


# ---------------------------------------------------------------------------
# 版本太老的解释器
#
# "跑得起来"不等于"跑得了这个仓库的脚本"：装过老 Python 的机器上，
# `python` 常常指向 2.7 或 3.6。它 `import sys` 是成功的（探活会通过），
# 但仓库的脚本用了 f-string 和 `Path.unlink(missing_ok=True)`（3.8 才有）
# ——挑中它之后，用户看到的是一句 SyntaxError 或 AttributeError，
# 指向的是脚本，而不是"你的 Python 太老了"。
# ---------------------------------------------------------------------------


@pytest.mark.skipif(SH is None, reason="没有可用的 sh 来跑这个脚本")
def test_skips_an_interpreter_that_is_too_old(tmp_path: Path) -> None:
    """太老的解释器要被跳过——哪怕它"能跑"。"""
    assert FIND_PYTHON.exists(), "缺少 scripts/find-python.sh"
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    _fake_python_with_version(bin_dir, "python3", "3.6")
    good = _fake_python(bin_dir, "python", exit_code=0)

    result = run_in_sh(SH or "sh", FIND_PYTHON, bin_dir)

    assert result.returncode == 0, result.stderr
    chosen = result.stdout.strip()
    assert chosen, "没有挑出解释器"
    assert Path(chosen).name == good.name, f"挑中了太老的 {chosen}"


@pytest.mark.skipif(SH is None, reason="没有可用的 sh 来跑这个脚本")
def test_the_failure_names_the_version_it_found(tmp_path: Path) -> None:
    """只有一个太老的解释器时，报错要说清楚"版本太老"。

    只说"找不到可用的 Python"是不够的：用户会以为解释器没装，
    于是去装一个新的——装了还是同样的错。
    """
    assert FIND_PYTHON.exists(), "缺少 scripts/find-python.sh"
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    _fake_python_with_version(bin_dir, "python3", "3.6")
    _fake_python_with_version(bin_dir, "python", "2.7")

    result = run_in_sh(SH or "sh", FIND_PYTHON, bin_dir)

    assert result.returncode != 0
    assert result.stdout.strip() == ""
    assert "3.6" in result.stderr, result.stderr
    assert "3.8" in result.stderr, result.stderr


@pytest.mark.skipif(BASH is None, reason="没有可用的 bash 来跑这个脚本")
def test_smoke_surfaces_why_the_interpreter_was_rejected(tmp_path: Path) -> None:
    """解释器被拒的**原因**要出现在验收脚本的报错里。

    验收脚本原来把 find-python.sh 的诊断丢进了 /dev/null：用户只看到
    "找不到可用的 Python 解释器"，看不到"版本太老（3.6）"——于是去装
    一个新的，装完还是同样的错。
    """
    smoke = _smoke_fixture(tmp_path)
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    _fake_python_with_version(bin_dir, "python3", "3.6")
    _fake_python_with_version(bin_dir, "python", "3.6")
    fake_program(bin_dir, "docker", "exit 0")

    result = run_in_sh(BASH or "bash", smoke, bin_dir)

    assert result.returncode != 0
    assert "3.6" in result.stderr, result.stderr


# ---------------------------------------------------------------------------
# scripts/docker-smoke.sh 的预检
# ---------------------------------------------------------------------------

def _smoke_fixture(tmp_path: Path) -> Path:
    """把验收脚本复制到一个**自足**的目录里，返回复制后的脚本路径。

    为什么不能直接跑仓库里的 scripts/docker-smoke.sh：那个脚本第一件事
    就是检查仓库根的 .env，而 .env 是 gitignore 掉的文件——干净克隆和 CI
    上根本没有它。直接跑仓库里的脚本，这个用例就依赖了开发机上的
    .env：本地通过、CI 上因为"错误的原因"失败（脚本在"缺少 .env"这里
    就退出了，根本没走到解释器预检）。

    复制到临时目录之后，脚本的 ROOT 也就是临时目录，仓库里的一切
    （包括真实的 .env）都跟它无关了。
    """
    repo = tmp_path / "repo"
    (repo / "scripts").mkdir(parents=True)
    for name in ("docker-smoke.sh", "find-python.sh", "smoke_checks.py"):
        shutil.copy(ROOT / "scripts" / name, repo / "scripts" / name)
    return repo / "scripts" / "docker-smoke.sh"


@pytest.mark.skipif(BASH is None, reason="没有可用的 bash 来跑这个脚本")
def test_smoke_reports_unusable_interpreter_before_touching_docker(
    tmp_path: Path,
) -> None:
    """预检必须在动 Docker 之前失败——否则用户等 6 分钟才看到错误的指向。

    这里的 docker 是假的：真跑一次 ``docker compose up`` 既慢又会改动用户的
    容器栈，而且失败时还会去 dump 容器日志（可能带出口令）。
    """
    smoke = _smoke_fixture(tmp_path)
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    _fake_python(bin_dir, "python3", exit_code=49)
    _fake_python(bin_dir, "python", exit_code=49)
    docker_log = tmp_path / "docker.log"
    fake_program(bin_dir, "docker", f'echo "$@" >> "{to_shell_path(docker_log)}"')
    # 万一走到了健康检查循环，sleep 得是空的，否则测试要等 6 分钟。
    fake_program(bin_dir, "sleep", "exit 0")

    result = run_in_sh(BASH or "bash", smoke, bin_dir)

    assert result.returncode != 0
    assert "python3" in result.stderr, result.stderr
    calls = docker_log.read_text(encoding="utf-8") if docker_log.exists() else ""
    assert "up" not in calls.split(), calls


@pytest.mark.skipif(BASH is None, reason="没有可用的 bash 来跑这个脚本")
def test_smoke_reports_the_interpreter_problem_even_without_a_dotenv(
    tmp_path: Path,
) -> None:
    """解释器的问题要**先于** .env 的问题报出来。

    解释器跑不起来的时候，报"缺少 .env"会把人引到错误的方向：
    make docker-env 同样需要解释器，照着提示做只会再撞一次墙。

    断言的是"缺少 .env"这条**报错**没出现，而不是整个输出里没有
    ".env"三个字——find-python.sh 的诊断里会提到"生成 .env 需要
    Python 3.8 以上"，那是另一回事：它说的是 .env **怎么来**，不是
    ".env 缺了"。用前者做断言会把正确的诊断也一起判死。
    """
    smoke = _smoke_fixture(tmp_path)
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    _fake_python(bin_dir, "python3", exit_code=49)
    _fake_python(bin_dir, "python", exit_code=49)
    fake_program(bin_dir, "docker", "exit 0")

    result = run_in_sh(BASH or "bash", smoke, bin_dir)

    assert result.returncode != 0
    assert "python3" in result.stderr, result.stderr
    assert "缺少 .env" not in result.stderr, result.stderr


@pytest.mark.skipif(BASH is None, reason="没有可用的 bash 来跑这个脚本")
def test_smoke_still_reports_a_missing_dotenv(tmp_path: Path) -> None:
    """解释器没问题时，缺 .env 依然要报"缺少 .env"。"""
    smoke = _smoke_fixture(tmp_path)
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    # 解释器是好的：预检要能放行它，好让 .env 的检查真的被走到。
    _fake_python(bin_dir, "python3", exit_code=0)
    fake_program(bin_dir, "curl", "exit 0")
    fake_program(bin_dir, "docker", "exit 0")

    result = run_in_sh(BASH or "bash", smoke, bin_dir)

    assert result.returncode != 0
    assert ".env" in result.stderr, result.stderr


@pytest.mark.skipif(BASH is None, reason="没有可用的 bash 来跑这个脚本")
def test_smoke_does_not_double_the_slash_in_the_base_url(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """DOCKER_BASE_URL 末尾带斜杠时，请求的 URL 里不能出现双斜杠。

    nginx 的代理配置是 ``proxy_pass $backend_upstream$request_uri;``——
    $request_uri 是**原样**的请求行。双斜杠被原样转给后端，
    后端回 404，于是这一步报的是"请求失败"，真正的原因（地址写歪了）
    一个字都不提。
    """
    smoke = _smoke_fixture(tmp_path)
    (tmp_path / "repo" / ".env").write_text(
        "FRONTEND_HOST_PORT=8080\n", encoding="utf-8"
    )
    monkeypatch.setenv("DOCKER_BASE_URL", "http://127.0.0.1:8080/")

    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    _fake_python(bin_dir, "python3", exit_code=0)
    url_log = tmp_path / "urls.log"
    fake_program(bin_dir, "curl", f'echo "$@" >> "{to_shell_path(url_log)}"')
    fake_program(bin_dir, "docker", "exit 0")

    run_in_sh(BASH or "bash", smoke, bin_dir)

    urls = url_log.read_text(encoding="utf-8") if url_log.exists() else ""
    assert urls, "脚本一个 URL 都没请求过，用例没验到东西"
    assert "//api" not in urls, urls


# ---------------------------------------------------------------------------
# Makefile 的 docker-env
# ---------------------------------------------------------------------------


def _usable_make() -> str | None:
    found = shutil.which("make")
    if not found:
        return None
    try:
        result = subprocess.run(
            [found, "--version"], capture_output=True, text=True, timeout=30
        )
    except (OSError, subprocess.SubprocessError):
        return None
    return found if result.returncode == 0 else None


MAKE = _usable_make()


@pytest.mark.skipif(MAKE is None, reason="没有可用的 make")
def test_make_docker_env_reports_unusable_interpreter(tmp_path: Path) -> None:
    """``make docker-env`` 在解释器不能跑时要给出**看得懂的**提示。

    旧实现会以 ``Error 49`` 收场——那个 49 是 Store 转发器的退出码，
    用户完全看不出是自己的 python3 是假的。
    """
    repo = tmp_path / "repo"
    (repo / "scripts").mkdir(parents=True)
    shutil.copy(MAKEFILE, repo / "Makefile")
    shutil.copy(GENERATE_ENV, repo / "scripts" / "generate_docker_env.py")
    if FIND_PYTHON.exists():
        shutil.copy(FIND_PYTHON, repo / "scripts" / "find-python.sh")
    shutil.copy(ROOT / ".env.example", repo / ".env.example")

    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    _fake_python(bin_dir, "python3", exit_code=49)
    _fake_python(bin_dir, "python", exit_code=49)

    env = dict(os.environ)
    # bin_dir 里那个"存在但不能跑"的 python3 必须在 PATH 上：
    # 旧实现会挑中它、以 Error 49 收场；新实现必须跳过它并给出明确提示。
    env["PATH"] = os.pathsep.join([str(bin_dir), *path_without_python()])
    result = subprocess.run(
        [MAKE or "make", "docker-env"],
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        env=env,
        cwd=str(repo),
        timeout=120,
    )

    assert result.returncode != 0
    assert "python" in result.stderr.lower(), result.stderr
    assert not (repo / ".env").exists()
