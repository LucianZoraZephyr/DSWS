"""`scripts/export-docker-env.sh` 的行为测试。

这个脚本把 `.env` 里的值变成宿主 CLI 用的 `REBUILD_*` 变量。三个坑：

1. `.env` 的值可能被引号包着（`MYSQL_PASSWORD="xxx"`）——引号必须去掉，
   否则密码里会多出两个字符，宿主机 CLI 就会认证失败。
2. LLM 的三个变量不导出的话，宿主 CLI 就用不了外部模型，
   和容器里的行为对不上。
3. **解析规则必须和 Compose 一致。** 同一份 `.env`，Compose 读出来的值和
   这个脚本读出来的值要是同一个——不然容器里能连上库、宿主 CLI 却拿到一句
   ``Access denied``，而两边都"没写错"。这条最容易踩的是行内注释：
   ``MYSQL_PASSWORD=abc # 备注`` 在 Compose 眼里是 ``abc``（``#`` 前面有
   空格，注释开始），手写解析器很容易把整行都当成密码。
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
SCRIPT = ROOT / "scripts" / "export-docker-env.sh"


def _bash_candidates() -> list[str]:
    candidates: list[str] = []
    if os.name == "nt":
        # Windows 上裸 "bash" 会被 System32 里的 WSL 启动器截胡，
        # 所以显式找 Git Bash。
        for variable in ("ProgramFiles", "ProgramW6432"):
            base = os.environ.get(variable)
            if base:
                candidates.append(str(Path(base) / "Git" / "bin" / "bash.exe"))
        candidates.append(str(Path("C:/Program Files/Git/bin/bash.exe")))
    for path in ("/bin/bash", "/usr/bin/bash"):
        if Path(path).exists():
            candidates.append(path)
    found = shutil.which("bash")
    if found:
        candidates.append(found)
    return candidates


def _usable_bash() -> str | None:
    """找一个真的能跑的 bash——WSL 的启动器不算。"""
    for candidate in _bash_candidates():
        try:
            result = subprocess.run(
                [candidate, "-c", "true"],
                capture_output=True,
                text=True,
                timeout=30,
            )
        except (OSError, subprocess.SubprocessError):
            continue
        if result.returncode == 0:
            return candidate
    return None


BASH = _usable_bash()

pytestmark = pytest.mark.skipif(BASH is None, reason="没有可用的 bash 来 source 这个脚本")

BASE_ENV = """\
MYSQL_ROOT_PASSWORD=rootpw
MYSQL_PASSWORD=apppw
REBUILD_DW_RO_PASSWORD=ropw
MYSQL_HOST_PORT=3306
ES_HOST_PORT=9200
QDRANT_HOST_PORT=6333
"""


# Compose 解析 ${VAR} 的顺序是：**先看当前 shell 的环境变量，再看 .env 文件**。
# 这个脚本只读 .env。所以两边要能比，就必须先把 shell 里那几个变量清掉——
# 否则比的是"shell 的值"和".env 的值"，压根不是一回事。
#
# 少了这一步，`source scripts/export-docker-env.sh && make stage3` 会红：
# 脚本导出过 REBUILD_DW_RO_PASSWORD / REBUILD_LLM_API_KEY，
# Compose 于是优先用 shell 里的旧值，而脚本读的是临时 .env 的新值。
_SHADOWING_KEYS = (
    "MYSQL_PASSWORD",
    "MYSQL_ROOT_PASSWORD",
    "REBUILD_DW_RO_PASSWORD",
    "REBUILD_LLM_BASE_URL",
    "REBUILD_LLM_API_KEY",
    "REBUILD_LLM_MODEL",
    "REBUILD_LLM_TIMEOUT",
    "MYSQL_HOST_PORT",
    "ES_HOST_PORT",
    "QDRANT_HOST_PORT",
)

# Compose 自己也会读这几个环境变量（换个 compose 文件、额外注入 .env 等），
# 泄漏进来同样会让两边读的不是同一份输入。
_COMPOSE_KEYS = (
    "COMPOSE_FILE",
    "COMPOSE_ENV_FILES",
    "COMPOSE_PROFILES",
    "COMPOSE_PROJECT_NAME",
    "COMPOSE_PATH_SEPARATOR",
)


def _clean_env(extra: dict[str, str] | None = None) -> dict[str, str]:
    """一个不被当前 shell 的凭据污染的环境。"""
    env = {
        key: value
        for key, value in os.environ.items()
        if key not in _SHADOWING_KEYS and key not in _COMPOSE_KEYS
    }
    if extra:
        env.update(extra)
    return env


def _prepare(tmp_path: Path, env_body: str) -> Path:
    """在临时目录里放一份 .env 与脚本，返回脚本路径。"""
    scripts_dir = tmp_path / "scripts"
    scripts_dir.mkdir(exist_ok=True)
    target = scripts_dir / "export-docker-env.sh"
    target.write_text(SCRIPT.read_text(encoding="utf-8"), encoding="utf-8")
    (tmp_path / ".env").write_text(env_body, encoding="utf-8")
    return target


def _run_source(
    target: Path, env: dict[str, str] | None = None
) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [
            BASH or "bash",
            "-c",
            # 用 POSIX 风格的路径：Windows 的反斜杠在 bash 里是转义符。
            f'source "{target.as_posix()}" || exit 1; env',
        ],
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        check=False,
        env=_clean_env() if env is None else env,
    )


def _parse_env(text: str) -> dict[str, str]:
    """把 ``env`` 的输出拆成 名字 -> 值。"""
    exported: dict[str, str] = {}
    for line in text.splitlines():
        key, _, value = line.partition("=")
        exported[key] = value
    return exported


def _source(
    tmp_path: Path, env_body: str, env: dict[str, str] | None = None
) -> tuple[int, dict[str, str]]:
    """在临时目录里放一份 .env 与脚本，source 之后把环境变量捞回来。"""
    result = _run_source(_prepare(tmp_path, env_body), env)
    return result.returncode, _parse_env(result.stdout)


def _run_source_unexported(
    target: Path, assignments: list[str]
) -> subprocess.CompletedProcess[str]:
    """在 shell 里设置**没有 export** 的变量，再 source 脚本。

    bash 里 ``VAR=value``（没有 export）只是一个 shell 变量：它在自己
    这个 shell 里可见，**不在环境里**。docker compose 是另一个进程，
    只看得到已导出的变量——所以它看不见这个值。
    """
    prefix = "; ".join(assignments)
    return subprocess.run(
        [
            BASH or "bash",
            "-c",
            f'{prefix}; source "{target.as_posix()}" || exit 1; env',
        ],
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        check=False,
        env=_clean_env(),
    )


def test_exports_credentials_and_urls(tmp_path: Path) -> None:
    code, exported = _source(tmp_path, BASE_ENV)
    assert code == 0
    assert exported["REBUILD_MYSQL_PASSWORD"] == "apppw"
    assert exported["REBUILD_MYSQL_ROOT_PASSWORD"] == "rootpw"
    assert exported["REBUILD_DW_RO_PASSWORD"] == "ropw"
    assert exported["REBUILD_ES_URL"] == "http://127.0.0.1:9200"
    assert exported["REBUILD_QDRANT_URL"] == "http://127.0.0.1:6333"


def test_strips_surrounding_quotes(tmp_path: Path) -> None:
    body = BASE_ENV + 'MYSQL_PASSWORD="quoted-pw"\n'
    code, exported = _source(tmp_path, body)
    assert code == 0
    assert exported["REBUILD_MYSQL_PASSWORD"] == "quoted-pw"


def test_strips_surrounding_whitespace(tmp_path: Path) -> None:
    body = BASE_ENV.replace("MYSQL_PASSWORD=apppw", "MYSQL_PASSWORD=  apppw  ")
    code, exported = _source(tmp_path, body)
    assert code == 0
    assert exported["REBUILD_MYSQL_PASSWORD"] == "apppw"


def test_exports_llm_settings(tmp_path: Path) -> None:
    body = (
        BASE_ENV
        + "REBUILD_LLM_BASE_URL=https://api.example.com/v1\n"
        + "REBUILD_LLM_API_KEY=sk-test\n"
        + "REBUILD_LLM_MODEL=qwen-max\n"
        + "REBUILD_LLM_TIMEOUT=45\n"
    )
    code, exported = _source(tmp_path, body)
    assert code == 0
    assert exported["REBUILD_LLM_BASE_URL"] == "https://api.example.com/v1"
    assert exported["REBUILD_LLM_API_KEY"] == "sk-test"
    assert exported["REBUILD_LLM_MODEL"] == "qwen-max"
    assert exported["REBUILD_LLM_TIMEOUT"] == "45"


def test_fails_when_required_key_missing(tmp_path: Path) -> None:
    body = "MYSQL_PASSWORD=apppw\n"
    code, _exported = _source(tmp_path, body)
    assert code != 0


# ---------------------------------------------------------------------------
# 解析规则要和 Compose（compose-go 的 dotenv）一致
# ---------------------------------------------------------------------------


def test_drops_an_inline_comment_after_the_value(tmp_path: Path) -> None:
    """``#`` 前面有空格 → 后面是行内注释，Compose 只取空格前的部分。"""
    body = BASE_ENV.replace("MYSQL_PASSWORD=apppw", "MYSQL_PASSWORD=apppw # 备注")
    code, exported = _source(tmp_path, body)
    assert code == 0
    assert exported["REBUILD_MYSQL_PASSWORD"] == "apppw"


def test_keeps_a_hash_that_has_no_space_before_it(tmp_path: Path) -> None:
    """``#`` 前面没有空格 → 它就是值的一部分（Compose 的规则）。"""
    body = BASE_ENV.replace("MYSQL_PASSWORD=apppw", "MYSQL_PASSWORD=ap#pw")
    code, exported = _source(tmp_path, body)
    assert code == 0
    assert exported["REBUILD_MYSQL_PASSWORD"] == "ap#pw"


def test_keeps_the_hash_inside_single_quotes(tmp_path: Path) -> None:
    """单引号里的一切都是字面量，包括 ``#``。"""
    body = BASE_ENV.replace("MYSQL_PASSWORD=apppw", "MYSQL_PASSWORD='a # b'")
    code, exported = _source(tmp_path, body)
    assert code == 0
    assert exported["REBUILD_MYSQL_PASSWORD"] == "a # b"


def test_keeps_the_hash_inside_double_quotes(tmp_path: Path) -> None:
    body = BASE_ENV.replace("MYSQL_PASSWORD=apppw", 'MYSQL_PASSWORD="a # b"')
    code, exported = _source(tmp_path, body)
    assert code == 0
    assert exported["REBUILD_MYSQL_PASSWORD"] == "a # b"


def test_ignores_everything_after_the_closing_quote(tmp_path: Path) -> None:
    """闭合引号之后的内容 Compose 一律丢弃。"""
    body = BASE_ENV.replace("MYSQL_PASSWORD=apppw", 'MYSQL_PASSWORD="apppw" 尾巴')
    code, exported = _source(tmp_path, body)
    assert code == 0
    assert exported["REBUILD_MYSQL_PASSWORD"] == "apppw"


def test_double_quoted_escapes_match_compose(tmp_path: Path) -> None:
    """双引号里 Compose 会处理 ``\\"`` ``\\\\`` ``\\n`` ``\\t`` ``\\r``。"""
    body = BASE_ENV.replace("MYSQL_PASSWORD=apppw", 'MYSQL_PASSWORD="a\\"b"')
    code, exported = _source(tmp_path, body)
    assert code == 0
    assert exported["REBUILD_MYSQL_PASSWORD"] == 'a"b'

    body = BASE_ENV.replace("MYSQL_PASSWORD=apppw", 'MYSQL_PASSWORD="a\\tb"')
    code, exported = _source(tmp_path, body)
    assert code == 0
    assert exported["REBUILD_MYSQL_PASSWORD"] == "a\tb"


def test_single_quoted_value_is_literal(tmp_path: Path) -> None:
    """单引号里没有转义：``\\n`` 就是反斜杠加 n。"""
    body = BASE_ENV.replace("MYSQL_PASSWORD=apppw", "MYSQL_PASSWORD='a\\nb'")
    code, exported = _source(tmp_path, body)
    assert code == 0
    assert exported["REBUILD_MYSQL_PASSWORD"] == "a\\nb"


def test_unquoted_backslash_is_kept(tmp_path: Path) -> None:
    """不加引号时反斜杠没有特殊含义，原样保留。"""
    body = BASE_ENV.replace("MYSQL_PASSWORD=apppw", "MYSQL_PASSWORD=a\\b")
    code, exported = _source(tmp_path, body)
    assert code == 0
    assert exported["REBUILD_MYSQL_PASSWORD"] == "a\\b"


def test_accepts_the_export_prefix(tmp_path: Path) -> None:
    """Compose 允许 ``export KEY=value`` 这种写法。"""
    body = BASE_ENV.replace("MYSQL_PASSWORD=apppw", "export MYSQL_PASSWORD=apppw")
    code, exported = _source(tmp_path, body)
    assert code == 0
    assert exported["REBUILD_MYSQL_PASSWORD"] == "apppw"


def test_ignores_a_comment_line_that_starts_with_spaces(tmp_path: Path) -> None:
    body = "   # 这是注释\n" + BASE_ENV
    code, exported = _source(tmp_path, body)
    assert code == 0
    assert exported["REBUILD_MYSQL_PASSWORD"] == "apppw"


def test_leaves_no_helper_functions_behind(tmp_path: Path) -> None:
    """source 完不留下一堆 ``_export_docker_env_*`` 的函数。"""
    scripts_dir = tmp_path / "scripts"
    scripts_dir.mkdir(exist_ok=True)
    target = scripts_dir / "export-docker-env.sh"
    target.write_text(SCRIPT.read_text(encoding="utf-8"), encoding="utf-8")
    (tmp_path / ".env").write_text(BASE_ENV, encoding="utf-8")

    result = subprocess.run(
        [
            BASH or "bash",
            "-c",
            f'source "{target.as_posix()}" >/dev/null 2>&1 || exit 1; declare -F',
        ],
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        check=False,
    )

    assert result.returncode == 0
    assert "_export_docker_env" not in result.stdout, result.stdout


# ---------------------------------------------------------------------------
# 和真正的 Compose 对账
# ---------------------------------------------------------------------------

MINIMAL_COMPOSE = """\
services:
  app:
    image: busybox
    environment:
      - MYSQL_PASSWORD=${MYSQL_PASSWORD}
      - MYSQL_ROOT_PASSWORD=${MYSQL_ROOT_PASSWORD}
      - REBUILD_DW_RO_PASSWORD=${REBUILD_DW_RO_PASSWORD}
      - REBUILD_LLM_API_KEY=${REBUILD_LLM_API_KEY}
"""


def _docker_compose_available() -> bool:
    docker = shutil.which("docker")
    if not docker:
        return False
    try:
        result = subprocess.run(
            [docker, "compose", "version"],
            capture_output=True,
            text=True,
            timeout=120,
        )
    except (OSError, subprocess.SubprocessError):
        return False
    return result.returncode == 0


DOCKER_COMPOSE = _docker_compose_available()

# .env 的键 -> 脚本导出的变量
_CROSS_CHECK_KEYS = (
    ("MYSQL_PASSWORD", "REBUILD_MYSQL_PASSWORD"),
    ("MYSQL_ROOT_PASSWORD", "REBUILD_MYSQL_ROOT_PASSWORD"),
    ("REBUILD_DW_RO_PASSWORD", "REBUILD_DW_RO_PASSWORD"),
    ("REBUILD_LLM_API_KEY", "REBUILD_LLM_API_KEY"),
)

# 这些值都是"手改 .env 时真的会写出来"的形态
_CROSS_CHECK_VALUES = (
    "apppw",
    "app pw",
    "ap#pw",
    "apppw # 备注",
    'quoted "pw"',
    "a\\b",
    "  padded  ",
    # 带 $ 的：Compose 会展开，脚本也得展开成同一个字符串
    "pa$$word",
    "pa${NOPE}word",
    "${NOPE:-fallback}",
    "pa$",
    "'pa$$word'",
    '"pa$$word"',
    '"pa\\$word"',
)


@pytest.mark.skipif(not DOCKER_COMPOSE, reason="没有可用的 docker compose")
@pytest.mark.parametrize("value", _CROSS_CHECK_VALUES)
@pytest.mark.parametrize("env_key,exported_key", _CROSS_CHECK_KEYS)
def test_the_parser_agrees_with_docker_compose(
    tmp_path: Path, value: str, env_key: str, exported_key: str
) -> None:
    """同一份 .env，脚本读出来的值必须和 Compose 读出来的一模一样。

    这是整条链路里最容易出错、也最难查的一环：两边都"没写错"，
    但拿到的密码不同，宿主 CLI 就只能收到一句 ``Access denied``。
    """
    (tmp_path / "compose.yaml").write_text(MINIMAL_COMPOSE, encoding="utf-8")
    values = {
        "MYSQL_ROOT_PASSWORD": "rootpw",
        "MYSQL_PASSWORD": "apppw",
        "REBUILD_DW_RO_PASSWORD": "ropw",
        "MYSQL_HOST_PORT": "3306",
        "ES_HOST_PORT": "9200",
        "QDRANT_HOST_PORT": "6333",
        "REBUILD_LLM_API_KEY": "sk-test",
    }
    values[env_key] = value
    body = "".join(f"{key}={item}\n" for key, item in values.items())
    # 先把 .env 落到磁盘：docker compose 要读它。
    (tmp_path / ".env").write_text(body, encoding="utf-8")

    compose = subprocess.run(
        ["docker", "compose", "config", "--format", "json"],
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        cwd=str(tmp_path),
        check=False,
        timeout=180,
        # 两边必须读同一份输入：Compose 会优先用当前 shell 的环境变量，
        # 不清干净的话，它读的是 shell 的值，脚本读的是 .env 的值。
        env=_clean_env(),
    )
    assert compose.returncode == 0, compose.stderr
    compose_value = json.loads(compose.stdout)["services"]["app"]["environment"][
        env_key
    ]

    code, exported = _source(tmp_path, body)

    assert code == 0
    assert exported[exported_key] == compose_value


# ---------------------------------------------------------------------------
# 和 shell 里已有的值对账
#
# Compose 解析 ${VAR} 是"shell 优先、再 .env"，脚本只读 .env。
# 两边取法不一致时，容器和宿主 CLI 就会拿到不同的密码——
# 两边都"没写错"，宿主 CLI 却只能收到一句 Access denied。
# ---------------------------------------------------------------------------


def test_keeps_a_shell_value_that_the_env_file_leaves_empty(tmp_path: Path) -> None:
    """`.env` 里是空值时，不能把用户自己 export 的值抹掉。

    典型场景：.env 的 LLM 三个变量默认是空的（不接外部模型），
    用户在自己的 shell 里 export REBUILD_LLM_API_KEY 接外部模型。
    脚本要是用 .env 的空值覆盖，宿主 CLI 就没有模型可用了。
    """
    body = BASE_ENV + "REBUILD_LLM_API_KEY=\n"
    code, exported = _source(
        tmp_path, body, env=_clean_env({"REBUILD_LLM_API_KEY": "sk-from-shell"})
    )
    assert code == 0
    assert exported["REBUILD_LLM_API_KEY"] == "sk-from-shell"


def test_the_env_file_wins_over_a_conflicting_shell_value(tmp_path: Path) -> None:
    """.env 是这份部署的事实源；shell 里那个不一样的值要被它盖掉。

    不盖掉的话，脚本导出的值和 .env 里的值不是同一个字符串，
    而 Compose 读到的又是脚本导出的那个——容器和宿主 CLI 就对不上了。
    """
    body = BASE_ENV
    code, exported = _source(
        tmp_path, body, env=_clean_env({"REBUILD_DW_RO_PASSWORD": "stale-ro"})
    )
    assert code == 0
    assert exported["REBUILD_DW_RO_PASSWORD"] == "ropw"


def test_it_says_out_loud_when_it_overrides_a_shell_value(tmp_path: Path) -> None:
    """静默覆盖会让人以为部署用的是自己 export 的值——必须出声。

    告警里只能出现变量名，不能出现值（值是密码）。
    """
    target = _prepare(tmp_path, BASE_ENV)
    result = _run_source(
        target, env=_clean_env({"REBUILD_DW_RO_PASSWORD": "stale-ro"})
    )
    assert result.returncode == 0, result.stderr
    assert "REBUILD_DW_RO_PASSWORD" in result.stderr
    assert "stale-ro" not in result.stderr
    assert "ropw" not in result.stderr


# ---------------------------------------------------------------------------
# 不导出 Compose 插值用的名字
#
# Compose 解析 ${MYSQL_PASSWORD} 是"shell 优先、再 .env"。脚本要是把
# MYSQL_PASSWORD 也 export 出去，shell 里那份就**挡住**了 .env 里的新值：
# 用户改了 .env（轮换密码、手工改端口）之后，容器读到的还是旧值，
# 宿主 CLI 读的是新值——两边对不上，症状只有一句 Access denied。
# ---------------------------------------------------------------------------


def test_it_does_not_export_the_names_compose_reads(tmp_path: Path) -> None:
    """脚本不导出 MYSQL_PASSWORD / MYSQL_ROOT_PASSWORD。

    导出了的话，shell 里那份快照会一直挡着 .env 的新值——改完 .env
    重新起容器，容器用的还是旧密码。
    """
    code, exported = _source(tmp_path, BASE_ENV)
    assert code == 0
    assert "MYSQL_PASSWORD" not in exported
    assert "MYSQL_ROOT_PASSWORD" not in exported


def test_the_shell_value_wins_over_the_env_file_for_the_mysql_password(
    tmp_path: Path,
) -> None:
    """Compose 读 ``${MYSQL_PASSWORD}`` 是"shell 优先、再 .env"——脚本照抄。

    脚本不导出 MYSQL_PASSWORD，所以 shell 里那个值是用户自己设的；
    Compose 会用它，脚本也必须用它，两边才对得上。
    """
    code, exported = _source(
        tmp_path, BASE_ENV, env=_clean_env({"MYSQL_PASSWORD": "from-shell"})
    )
    assert code == 0
    assert exported["REBUILD_MYSQL_PASSWORD"] == "from-shell"


def test_the_env_file_wins_when_the_shell_does_not_set_the_mysql_password(
    tmp_path: Path,
) -> None:
    """shell 没设的时候用 .env 的值——这是最常见的情况。"""
    code, exported = _source(tmp_path, BASE_ENV)
    assert code == 0
    assert exported["REBUILD_MYSQL_PASSWORD"] == "apppw"
    assert exported["REBUILD_MYSQL_ROOT_PASSWORD"] == "rootpw"


def test_it_says_out_loud_when_the_shell_and_the_env_file_disagree(
    tmp_path: Path,
) -> None:
    """两边不一样时必须出声——而且只能出现变量名，不能出现值。"""
    target = _prepare(tmp_path, BASE_ENV)
    result = _run_source(
        target, env=_clean_env({"MYSQL_PASSWORD": "from-shell"})
    )
    assert result.returncode == 0, result.stderr
    assert "MYSQL_PASSWORD" in result.stderr
    assert "from-shell" not in result.stderr
    assert "apppw" not in result.stderr


# ---------------------------------------------------------------------------
# $ 展开
#
# Compose 解析 .env 时会展开值里的 $name / ${name}。不照抄的话，
# .env 里写了 MYSQL_PASSWORD=${SALT}-pw，容器拿到的是展开后的值，
# 宿主 CLI 拿到的是没展开的字面量——两边不是同一个字符串。
# 规则是对着 docker compose v5 实测出来的。
# ---------------------------------------------------------------------------


def test_expands_a_variable_defined_earlier_in_the_env_file(tmp_path: Path) -> None:
    body = BASE_ENV.replace(
        "MYSQL_PASSWORD=apppw", "SALT=pepper\nMYSQL_PASSWORD=${SALT}-pw"
    )
    code, exported = _source(tmp_path, body)
    assert code == 0
    assert exported["REBUILD_MYSQL_PASSWORD"] == "pepper-pw"


def test_expands_a_variable_from_the_shell(tmp_path: Path) -> None:
    """查找顺序和 Compose 一致：shell 优先，再 .env 里前面定义过的键。"""
    body = BASE_ENV.replace(
        "MYSQL_PASSWORD=apppw", "SALT=fromfile\nMYSQL_PASSWORD=${SALT}-pw"
    )
    code, exported = _source(
        tmp_path, body, env=_clean_env({"SALT": "fromshell"})
    )
    assert code == 0
    assert exported["REBUILD_MYSQL_PASSWORD"] == "fromshell-pw"


def test_an_undefined_variable_expands_to_nothing(tmp_path: Path) -> None:
    body = BASE_ENV.replace("MYSQL_PASSWORD=apppw", "MYSQL_PASSWORD=pa${NOPE}word")
    code, exported = _source(tmp_path, body)
    assert code == 0
    assert exported["REBUILD_MYSQL_PASSWORD"] == "paword"


def test_a_forward_reference_expands_to_nothing(tmp_path: Path) -> None:
    """后面才定义的键不算数——值是读到那一行时就算出来的。"""
    body = BASE_ENV.replace("MYSQL_PASSWORD=apppw", "MYSQL_PASSWORD=pa${LATER}word")
    body += "LATER=zzz\n"
    code, exported = _source(tmp_path, body)
    assert code == 0
    assert exported["REBUILD_MYSQL_PASSWORD"] == "paword"


def test_expands_a_default_value(tmp_path: Path) -> None:
    body = BASE_ENV.replace("MYSQL_PASSWORD=apppw", "MYSQL_PASSWORD=${NOPE:-fallback}")
    code, exported = _source(tmp_path, body)
    assert code == 0
    assert exported["REBUILD_MYSQL_PASSWORD"] == "fallback"


def test_a_literal_dollar_is_written_as_two(tmp_path: Path) -> None:
    """Compose 把值里一个字面量 ``$`` 表示成 ``$$``——脚本也得是 ``$$``。"""
    body = BASE_ENV.replace("MYSQL_PASSWORD=apppw", "MYSQL_PASSWORD=pa$$word")
    code, exported = _source(tmp_path, body)
    assert code == 0
    assert exported["REBUILD_MYSQL_PASSWORD"] == "pa$$word"


def test_a_lone_dollar_is_written_as_two(tmp_path: Path) -> None:
    body = BASE_ENV.replace("MYSQL_PASSWORD=apppw", "MYSQL_PASSWORD=pa$")
    code, exported = _source(tmp_path, body)
    assert code == 0
    assert exported["REBUILD_MYSQL_PASSWORD"] == "pa$$"


def test_a_single_quoted_value_does_not_expand(tmp_path: Path) -> None:
    """单引号里没有展开——但每个 ``$`` 都表示成 ``$$``。"""
    body = BASE_ENV.replace("MYSQL_PASSWORD=apppw", "MYSQL_PASSWORD='pa$$word'")
    code, exported = _source(tmp_path, body)
    assert code == 0
    assert exported["REBUILD_MYSQL_PASSWORD"] == "pa$$$$word"


def test_it_refuses_an_env_file_with_an_unclosed_brace(tmp_path: Path) -> None:
    """``${`` 没有配对的 ``}`` 是无效的 .env——Compose 自己会拒绝启动。

    脚本也得停下来报错：猜一个值继续，只会让容器和宿主 CLI 拿着
    两个不同的密码，而两边都"没写错"。
    """
    body = BASE_ENV.replace("MYSQL_PASSWORD=apppw", "MYSQL_PASSWORD=pa${word")
    code, _exported = _source(tmp_path, body)
    assert code != 0


def test_the_shell_port_wins_over_the_env_file(tmp_path: Path) -> None:
    """端口按 Compose 的 ${VAR:-default} 取值：shell 优先，再 .env，最后默认值。

    Compose 用 ${ES_HOST_PORT:-9200} 发布端口，宿主 CLI 连的是同一个端口；
    两边取法不一样就会连到另一个服务上。
    """
    code, exported = _source(
        tmp_path, BASE_ENV, env=_clean_env({"ES_HOST_PORT": "9201"})
    )
    assert code == 0
    assert exported["REBUILD_ES_URL"] == "http://127.0.0.1:9201"


# ---------------------------------------------------------------------------
# "shell 优先"只对**已导出**的变量成立
#
# docker compose 是另一个进程：它只看得到宿主 shell **导出**到环境里的
# 变量。shell 里 `MYSQL_PASSWORD=xxx`（没有 export）的话，compose 根本
# 看不见这个名字——它用的是 .env 里的值。
#
# 脚本原来用 `${VAR+set}` 判"shell 里有没有"，那判的是**定义过**，
# 不是**导出过**。于是：用户没 export，容器用 .env 的密码、宿主 CLI 用
# shell 里的密码，两个字符串不同——症状只有一句 Access denied，而两边
# 都"没写错"。脚本还会在告警里说"已按 shell 的取值（与 docker compose
# 一致）"，那句话此时是假的。
# ---------------------------------------------------------------------------


def test_a_password_that_was_never_exported_does_not_win(tmp_path: Path) -> None:
    """没 export 的密码不算数——脚本得用 .env 里的那个，和容器一致。"""
    target = _prepare(tmp_path, BASE_ENV)
    result = _run_source_unexported(target, ["MYSQL_PASSWORD=from-shell"])
    assert result.returncode == 0, result.stderr

    exported = _parse_env(result.stdout)
    assert exported["REBUILD_MYSQL_PASSWORD"] == "apppw"


def test_the_root_password_that_was_never_exported_does_not_win(
    tmp_path: Path,
) -> None:
    """MYSQL_ROOT_PASSWORD 同理。"""
    target = _prepare(tmp_path, BASE_ENV)
    result = _run_source_unexported(target, ["MYSQL_ROOT_PASSWORD=from-shell"])
    assert result.returncode == 0, result.stderr

    exported = _parse_env(result.stdout)
    assert exported["REBUILD_MYSQL_ROOT_PASSWORD"] == "rootpw"


def test_a_port_that_was_never_exported_does_not_win(tmp_path: Path) -> None:
    """端口也是"shell 优先、再 .env"——但 shell 优先只认已导出的值。

    用户在 shell 里敲了 ``ES_HOST_PORT=9201`` 却没有 export，容器照样
    发布在 9200：脚本也得连 9200，否则宿主 CLI 连的是另一个服务。
    """
    target = _prepare(tmp_path, BASE_ENV)
    result = _run_source_unexported(target, ["ES_HOST_PORT=9201"])
    assert result.returncode == 0, result.stderr

    exported = _parse_env(result.stdout)
    assert exported["REBUILD_ES_URL"] == "http://127.0.0.1:9200"


def test_it_says_out_loud_that_an_unexported_value_is_invisible_to_compose(
    tmp_path: Path,
) -> None:
    """没 export 的值要出声提醒，而且**不能**说"与 docker compose 一致"。

    那句"一致"只对真正已导出的变量成立——对一个没导出的值说它和
    compose 一致，是在告诉用户一个假结论。
    """
    target = _prepare(tmp_path, BASE_ENV)
    result = _run_source_unexported(target, ["MYSQL_PASSWORD=from-shell"])
    assert result.returncode == 0, result.stderr

    assert "MYSQL_PASSWORD" in result.stderr
    # 只能出现变量名，不能出现值。
    assert "from-shell" not in result.stderr
    assert "apppw" not in result.stderr
    # 关键：不能声称"和 compose 一致"。
    assert "docker compose 一致" not in result.stderr
    assert "docker compose" in result.stderr


def test_an_exported_value_still_wins_and_still_gets_the_consistency_warning(
    tmp_path: Path,
) -> None:
    """真正导出的值还是"shell 优先"——而且这时候"与 compose 一致"是真的。"""
    target = _prepare(tmp_path, BASE_ENV)
    result = _run_source(
        target, env=_clean_env({"MYSQL_PASSWORD": "from-shell"})
    )
    assert result.returncode == 0, result.stderr

    exported = _parse_env(result.stdout)
    assert exported["REBUILD_MYSQL_PASSWORD"] == "from-shell"
    assert "docker compose 一致" in result.stderr
