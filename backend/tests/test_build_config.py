"""S2 · 构建配置的凭据行为测试。

回归目标：仓库里**不能**再出现硬编码的 MySQL 密码兜底值。

早先 `app/build/config.py` 写死过一个 MySQL 兜底口令。容器化之后 `.env`
生成的是随机密码，兜底值于是永远匹配不上——错误表现为一条毫无线索的
``Access denied for user 'didilili'@'172.20.0.5'``，而不是"你少配了
一个环境变量"。

现在的约定与 `app/scripts/security.py` 一致：**密码只由环境变量提供**，
缺失时立刻抛出一条点名环境变量的错误。

护栏盯的是"**任何**密码类环境变量都不能有非空默认值"，而不是某一个
具体字符串——所以下一个兜底值也会被拦住，不必先知道它是什么。
（也因此，这个仓库里不保留那个旧口令的明文。）
"""

from __future__ import annotations

import ast
import inspect
import tempfile
from pathlib import Path

import pytest

from app.build.config import BuildConfig
from app.build.errors import BuildError

ROOT = Path(__file__).resolve().parents[2]
APP = ROOT / "backend" / "app"

_CREDENTIAL_ENV = (
    "REBUILD_MYSQL_HOST",
    "REBUILD_MYSQL_PORT",
    "REBUILD_MYSQL_USER",
    "REBUILD_MYSQL_PASSWORD",
    "REBUILD_META_DB",
    "REBUILD_MYSQL_ROOT_USER",
    "REBUILD_MYSQL_ROOT_PASSWORD",
)


@pytest.fixture(autouse=True)
def _clean_env(monkeypatch: pytest.MonkeyPatch) -> None:
    """这些测试只关心凭据行为，先把宿主环境里的同名变量清干净。"""
    for name in _CREDENTIAL_ENV:
        monkeypatch.delenv(name, raising=False)


def _password_defaults(source: str) -> list[str]:
    """源码里"给密码类环境变量写了非空默认值"的地方。

    认的是**形状**不是内容：只要有一个调用的第一个参数是名字里带
    ``PASSWORD`` 的字符串常量，而后面的某个参数是非空字符串常量，
    就说明有人给密码留了兜底值。
    """
    tree = ast.parse(source)
    offenders: list[str] = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call) or not node.args:
            continue
        first = node.args[0]
        if not isinstance(first, ast.Constant) or not isinstance(first.value, str):
            continue
        if "PASSWORD" not in first.value.upper():
            continue
        for argument in node.args[1:]:
            if isinstance(argument, ast.Constant) and isinstance(argument.value, str):
                if argument.value:
                    offenders.append(
                        first.value + " 的默认值不是空串（第 " + str(argument.lineno) + " 行）"
                    )
    return offenders


# ---------------------------------------------------------------------------
# 兜底值：不允许有
# ---------------------------------------------------------------------------


def test_from_env_has_no_password_fallback() -> None:
    config = BuildConfig.from_env()
    assert config.mysql_password == ""
    assert config.mysql_root_password == ""


def _password_defaults_in_tree(directory: Path) -> list[str]:
    """扫一个目录下的所有 .py：兜底值写到别的模块里也要认出来。"""
    offenders: list[str] = []
    for path in sorted(directory.rglob("*.py")):
        source = path.read_text(encoding="utf-8")
        for item in _password_defaults(source):
            offenders.append(f"{path}: {item}")
    return offenders


def test_config_source_has_no_hardcoded_password() -> None:
    """源码级护栏：别把任何明文密码再写回配置层。

    用 AST 扫，不靠"某个具体口令字符串"：凡是拿密码类环境变量
    （名字里带 ``PASSWORD``）去取一个**非空**默认值的写法，都算违规。
    写成别的口令、写成 `os.getenv`——都拦得住。
    """
    source = Path(inspect.getsourcefile(BuildConfig) or "").read_text(encoding="utf-8")
    offenders = _password_defaults(source)
    assert not offenders, "；".join(offenders)


def test_no_module_under_app_has_a_password_fallback() -> None:
    """整棵 app/ 都得守住这条规矩，不只是 config.py。

    兜底值写到别的模块里（某个 CLI 自己 getenv 一次）时，
    只扫 config.py 的护栏是看不见的。
    """
    offenders = _password_defaults_in_tree(APP)
    assert not offenders, "；".join(offenders)


def test_the_tree_guard_also_catches_other_modules() -> None:
    """整目录的护栏不能是空壳：别的模块里的兜底值也要被认出来。

    没有这一条，把扫描范围从 config.py 扩大到整棵 app/ 的那条改动
    就没法证明它真的看得见别的模块。
    """
    with tempfile.TemporaryDirectory() as directory:
        path = Path(directory) / "other_module.py"
        path.write_text(
            'x = os.getenv("REBUILD_MYSQL_PASSWORD", "leaked-fallback")\n',
            encoding="utf-8",
        )
        offenders = _password_defaults_in_tree(Path(directory))

    assert offenders, "别的模块里的兜底口令没被认出来"


def test_the_guard_itself_is_not_a_no_op() -> None:
    """护栏不能是空壳：拿有兜底口令的源码喂给它，它必须报错。

    没有这一条，护栏写错了（比如正则写坏、AST 形状判断写反）也照样绿。
    """
    bad = 'x = os.getenv("REBUILD_MYSQL_PASSWORD", "some-hardcoded-value")\n'
    assert _password_defaults(bad), "带兜底口令的源码没被认出来"
    good = 'x = _env("REBUILD_MYSQL_PASSWORD", "")\n'
    assert not _password_defaults(good), "空默认值被误判成兜底口令"


# ---------------------------------------------------------------------------
# 缺失凭据：必须点名环境变量
# ---------------------------------------------------------------------------


def test_mysql_dsn_reports_missing_password() -> None:
    config = BuildConfig.from_env()
    with pytest.raises(BuildError) as excinfo:
        config.mysql_dsn()
    assert "REBUILD_MYSQL_PASSWORD" in str(excinfo.value)


def test_mysql_dsn_reports_missing_root_password() -> None:
    config = BuildConfig.from_env()
    with pytest.raises(BuildError) as excinfo:
        config.mysql_dsn(root=True)
    assert "REBUILD_MYSQL_ROOT_PASSWORD" in str(excinfo.value)


def test_missing_password_message_does_not_leak_secrets() -> None:
    config = BuildConfig.from_env()
    with pytest.raises(BuildError) as excinfo:
        config.mysql_dsn()
    message = str(excinfo.value)
    # 消息里只该出现"缺哪个环境变量"，不该出现任何口令内容。
    assert "REBUILD_MYSQL_PASSWORD" in message
    assert config.mysql_password == ""


# ---------------------------------------------------------------------------
# 有凭据：正常组装 DSN
# ---------------------------------------------------------------------------


def test_mysql_dsn_uses_environment_password(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("REBUILD_MYSQL_USER", "someone")
    monkeypatch.setenv("REBUILD_MYSQL_PASSWORD", "s3cret")
    config = BuildConfig.from_env()
    dsn = config.mysql_dsn(database="meta_rebuild")
    assert dsn["user"] == "someone"
    assert dsn["password"] == "s3cret"
    assert dsn["database"] == "meta_rebuild"


def test_root_dsn_uses_root_password(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("REBUILD_MYSQL_ROOT_USER", "root")
    monkeypatch.setenv("REBUILD_MYSQL_ROOT_PASSWORD", "rootpw")
    config = BuildConfig.from_env()
    dsn = config.mysql_dsn(root=True)
    assert dsn["user"] == "root"
    assert dsn["password"] == "rootpw"


# ---------------------------------------------------------------------------
# 不连库的路径（计划 / dry-run）不该被凭据拦住
# ---------------------------------------------------------------------------


def test_from_env_does_not_require_credentials() -> None:
    """`from_env()` 本身不抛错：算计划、做 dry-run 都不需要数据库。

    但"不需要凭据"不等于"会自己编一个"：兜底密码回来的时候
    `from_env()` 同样不抛错，这条测试照样绿，名字却还在骗人。
    所以这里把"凭据是空的"也钉住——门在 `mysql_dsn()` 那一步，
    `from_env()` 只负责如实报告"我没有密码"。
    """
    config = BuildConfig.from_env()
    assert config.mysql_password == ""
    assert config.mysql_root_password == ""
    assert config.meta_db.endswith("_rebuild")
    assert config.safe_repr()
