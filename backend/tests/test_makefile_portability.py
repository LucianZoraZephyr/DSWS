"""Makefile 的配方必须保持 ASCII：中文 Windows 上，配方里的中文会被当成 GBK 读。

mingw-make 构造 Windows 命令行时，会把配方里的字节按**系统 ANSI 代码页**解码一次
（中文 Windows 是 cp936 / GBK）。UTF-8 的中文字节因此被当成 GBK 读：

    "·"（U+00B7 = C2 B7）  →  "路"（U+8DEF，GBK 码位 C2B7）
    "后端契约测试"        →  一串乱码

更糟的是：解码失败时那一段字节会被整段替换掉，**连带的引号一起被吃掉**，
shell 于是看到一个不配对的引号，配方直接报语法错：

    >&2;: -c: line 4: unexpected EOF while looking for matching `"'

所以规矩是：**中文只能待在 .sh / .txt 文件里**（shell 直接读文件，字节原样输出，
不经过 Windows 命令行这一层），Makefile 配方一律保持 ASCII。

这不是"锦上添花"：仓库里 38 条横幅现在印出来全是乱码，还有 3 条配方
（docker-env 的两条诊断、docker-reset 的确认提示）直接让 shell 报语法错。
"""

from __future__ import annotations

import os
import shutil
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
MAKEFILE = ROOT / "Makefile"
SAY = ROOT / "scripts" / "say.sh"
BANNERS = ROOT / "scripts" / "banners.txt"


def _usable_sh() -> str | None:
    """找一个真的能跑的 sh。"""
    candidates = [shutil.which("sh"), shutil.which("bash"), "/bin/sh", "/usr/bin/sh"]
    for candidate in candidates:
        if not candidate:
            continue
        try:
            result = subprocess.run(
                [candidate, "-c", "true"], capture_output=True, text=True, timeout=30
            )
        except (OSError, subprocess.SubprocessError):
            continue
        if result.returncode == 0:
            return candidate
    return None


SH = _usable_sh()


def _recipe_lines() -> list[tuple[int, str]]:
    """Makefile 里所有配方行（以 tab 开头的行），带上行号。"""
    text = MAKEFILE.read_text(encoding="utf-8")
    return [
        (number, line)
        for number, line in enumerate(text.splitlines(), 1)
        if line.startswith("\t")
    ]


def test_makefile_recipes_are_ascii_only() -> None:
    """配方里不能有非 ASCII 字符——它们会被 ANSI 代码页重新解码。"""
    offenders = [
        (number, line)
        for number, line in _recipe_lines()
        if not line.isascii()
    ]
    assert not offenders, "这些 Makefile 配方行含非 ASCII 字符（Windows 上会乱码/引号被吃掉）：\n" + "\n".join(
        f"  Makefile:{number}: {line.strip()[:90]}" for number, line in offenders
    )


@pytest.mark.skipif(SH is None, reason="没有可用的 sh 来跑这个脚本")
def test_banner_script_prints_the_exact_text() -> None:
    """横幅必须逐字节等于原文——它是被用户读到的输出。"""
    assert SAY.exists(), "缺少 scripts/say.sh"
    assert BANNERS.exists(), "缺少 scripts/banners.txt"
    samples = {
        "s0-backend-contract": "== S0 · 后端契约测试 ==",
        "s1-negative-fixture": "== S1 · 负向夹具（必须红） ==",
        "s6-error-table": "== S6 · 错误码表 ==",
    }
    for key, expected in samples.items():
        result = subprocess.run(
            [SH or "sh", str(SAY), key],
            capture_output=True,
            timeout=30,
        )
        assert result.returncode == 0, f"{key}: {result.stderr!r}"
        assert result.stdout.decode("utf-8") == expected + "\n"


@pytest.mark.skipif(SH is None, reason="没有可用的 sh 来跑这个脚本")
def test_banner_script_rejects_an_unknown_key() -> None:
    """键不存在时必须**报错退出**，不能静默打印空行——那会让横幅悄悄消失。"""
    assert SAY.exists(), "缺少 scripts/say.sh"
    result = subprocess.run(
        [SH or "sh", str(SAY), "no-such-banner"],
        capture_output=True,
        timeout=30,
    )
    assert result.returncode != 0
    assert b"no-such-banner" in result.stderr, result.stderr
    assert result.stdout == b""


@pytest.mark.skipif(SH is None, reason="没有可用的 sh 来跑这个脚本")
def test_a_dot_in_the_key_does_not_match_a_different_banner(tmp_path: Path) -> None:
    """键里的 `.` 是正则里的"任意字符"——不转义就会去匹配**别的**横幅。

    查找横幅用的是 ``sed -n "s/^${key}|//p"``：``.`` 能匹配任意一个字符，
    于是键 ``a.b`` 会把 ``aXb`` 一起匹上，印出来的是别人的横幅。
    """
    scripts = tmp_path / "scripts"
    scripts.mkdir()
    shutil.copy(SAY, scripts / "say.sh")
    (scripts / "banners.txt").write_text(
        "aXb|这是另一个键的横幅\n"
        "a.b|这是点号键的横幅\n",
        encoding="utf-8",
    )

    result = subprocess.run(
        [SH or "sh", str(scripts / "say.sh"), "a.b"],
        capture_output=True,
        timeout=30,
    )

    assert result.returncode == 0, result.stderr
    assert result.stdout.decode("utf-8") == "这是点号键的横幅\n"


def test_the_ui_scripts_agree_on_where_their_logs_go() -> None:
    """三个 UI 脚本找临时日志目录的方式必须一致，而且不能写死某一台机器的目录。

    写死过的版本是 ``/tmp/opencode/...``——换台机器 ``make stage0-ui`` 第一步
    就死在 mktemp 上，报的还是一句 "failed to create directory via template"，
    看不出是路径写死了。
    """
    scripts = sorted((ROOT / "scripts").glob("stage*-ui.sh"))
    assert scripts, "找不到 stage*-ui.sh"

    for path in scripts:
        text = path.read_text(encoding="utf-8")
        assert "TMPDIR" in text, f"{path.name} 没有用 TMPDIR 定位临时目录"

        lines = text.splitlines()
        mktemp_lines = [
            (number, line) for number, line in enumerate(lines, 1) if "mktemp" in line
        ]
        assert mktemp_lines, f"{path.name} 里没有 mktemp"

        for number, line in mktemp_lines:
            assert '"$LOG_ROOT/' in line, (
                f"{path.name}:{number} 的 mktemp 模板没有用 $LOG_ROOT：{line.strip()}"
            )

        mkdir_line = next(
            (number for number, line in enumerate(lines, 1) if "mkdir -p" in line), None
        )
        assert mkdir_line is not None, f"{path.name} 没有 mkdir -p 临时目录"
        for number, _ in mktemp_lines:
            assert mkdir_line < number, (
                f"{path.name}:{number} 在 mkdir 之前就用 mktemp 建目录"
            )


def test_the_ui_scripts_do_not_assume_setsid_exists() -> None:
    """`setsid` 在 Windows 的 git-bash 里没有——直接调用会报 command not found。

    脚本要像 stage6-ui.sh / stage7-ui.sh 那样：先 ``command -v setsid``，
    没有就退化成普通后台进程。
    """
    for path in sorted((ROOT / "scripts").glob("stage*-ui.sh")):
        text = path.read_text(encoding="utf-8")
        if "setsid" not in text:
            continue
        assert "command -v setsid" in text, (
            f"{path.name} 用了 setsid 却没有先检查它存不存在"
        )


def test_the_scripts_do_not_depend_on_seq() -> None:
    """`seq` 在 macOS/BSD 上不存在（那边是 `jot`）。

    ``for _ in $(seq 1 60)`` 在那儿展开成空串——循环体一次都不执行，
    脚本立刻失败，报的还是"后端启动失败"，真正的原因（seq 不存在）
    一个字都不提。
    """
    for path in sorted((ROOT / "scripts").glob("*.sh")):
        lines = path.read_text(encoding="utf-8").splitlines()
        for number, line in enumerate(lines, 1):
            stripped = line.strip()
            if stripped.startswith("#"):
                # 注释里提到 seq 是在解释为什么不用它。
                continue
            assert "$(seq" not in line and "`seq" not in line, (
                f"{path.name}:{number} 用了 seq——它在 macOS/BSD 上不存在：{stripped}"
            )


def test_banner_keys_and_makefile_stay_in_sync() -> None:
    """Makefile 用到的每个键都要在 banners.txt 里，banners.txt 里也不能有没人用的键。"""
    assert BANNERS.exists(), "缺少 scripts/banners.txt"
    used: set[str] = set()
    for number, line in _recipe_lines():
        stripped = line.strip()
        if "scripts/say.sh" in stripped:
            used.add(stripped.split()[-1])

    defined: set[str] = set()
    for number, line in enumerate(
        BANNERS.read_text(encoding="utf-8").splitlines(), 1
    ):
        if not line.strip() or line.lstrip().startswith("#"):
            continue
        key, separator, text = line.partition("|")
        assert separator, f"banners.txt:{number} 缺少 '|' 分隔符：{line!r}"
        assert text.strip(), f"banners.txt:{number} 横幅正文是空的：{line!r}"
        assert key not in defined, f"banners.txt:{number} 键重复：{key}"
        defined.add(key)

    assert used == defined, (
        "Makefile 与 scripts/banners.txt 的键对不上："
        f"Makefile 多出 {sorted(used - defined)}，"
        f"banners.txt 多出 {sorted(defined - used)}"
    )


# ---------------------------------------------------------------------------
# 只读栈的目标不许顺手生成一份新的 .env
#
# .env 里是这份部署的凭据。生成器会写进**新的随机密码**——卷里 MySQL 的账号
# 还是老密码，容器按新的 .env 连库就 Access denied，栈起不来。
#
# 所以：只有"第一次把栈搭起来"该生成 .env（make docker-up / make docker-env）；
# 关栈、看日志、验收、重置这些**只读或收尾**的动作，缺 .env 就该直接报错，
# 而不是替用户造一份新的出来。
# ---------------------------------------------------------------------------


def _make() -> str | None:
    for candidate in ("make", "mingw32-make", "mingw-make"):
        found = shutil.which(candidate)
        if found:
            return found
    return None


MAKE = _make()


def _dry_run(target: str) -> str:
    """`make -n`：只打印会执行什么，不执行（绝不真去 down -v）。"""
    assert MAKE is not None
    result = subprocess.run(
        [MAKE, "-n", target],
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        cwd=str(ROOT),
        timeout=120,
    )
    assert result.returncode == 0, result.stderr
    return result.stdout


@pytest.mark.skipif(MAKE is None, reason="没有可用的 make")
@pytest.mark.parametrize(
    "target", ["docker-down", "docker-logs", "docker-smoke", "docker-reset"]
)
def test_the_read_only_targets_never_generate_a_new_env_file(target: str) -> None:
    """这些目标缺 .env 时该报错，不该替用户造一份新的出来。

    新造的那份带的是**新的随机密码**：卷里的 MySQL 账号还是老密码，
    容器按新 .env 连库就是 Access denied——而且用户看不出 .env 是什么时候
    被换掉的。
    """
    printed = _dry_run(target)
    assert "generate_docker_env.py" not in printed, (
        f"make {target} 会顺手生成 .env：\n{printed}"
    )


@pytest.mark.skipif(MAKE is None, reason="没有可用的 make")
def test_docker_up_still_generates_the_env_file() -> None:
    """第一次把栈搭起来还是要生成 .env——那是"搭起来"的入口。"""
    printed = _dry_run("docker-up")
    assert "generate_docker_env.py" in printed, printed


@pytest.mark.skipif(MAKE is None, reason="没有可用的 make")
def test_the_env_file_requirement_fails_when_the_env_file_is_missing(
    tmp_path: Path,
) -> None:
    """缺 .env 时要**失败**：不能默默生成一份新的。"""
    result = subprocess.run(
        [MAKE, "-f", str(MAKEFILE), "docker-env-require"],
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        cwd=str(tmp_path),
        timeout=120,
    )
    assert result.returncode != 0, result.stdout
    assert "No rule to make target" not in result.stderr, result.stderr
    assert ".env" in result.stdout + result.stderr, result.stdout + result.stderr
    assert not (tmp_path / ".env").exists(), "缺 .env 时不该生成新的 .env"


@pytest.mark.skipif(MAKE is None, reason="没有可用的 make")
def test_the_env_file_requirement_passes_when_the_env_file_exists(
    tmp_path: Path,
) -> None:
    (tmp_path / ".env").write_text("MYSQL_PASSWORD=x\n", encoding="utf-8")
    result = subprocess.run(
        [MAKE, "-f", str(MAKEFILE), "docker-env-require"],
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        cwd=str(tmp_path),
        timeout=120,
    )
    assert result.returncode == 0, result.stdout + result.stderr

