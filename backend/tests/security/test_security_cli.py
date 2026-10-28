"""S5 · 安全 CLI 的凭据约定。

CLI 拿不到密码时必须**就地停下**并点名环境变量，而不是拿着空密码去撞一次库：
后者只会得到一句 ``Access denied for user 'didilili'@'172.20.0.1'``，
既看不出是哪个变量没配，也白等一次网络往返。

`--execute` / `--provision-ro-account` 早就是这个约定（`_ro_password()` /
`_privileged_password()`）；`--demo` 走的是应用账号，之前是漏网的。
"""

from __future__ import annotations

import pytest

from app.scripts import security


def test_demo_names_the_missing_app_password(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("REBUILD_MYSQL_PASSWORD", raising=False)

    with pytest.raises(SystemExit) as excinfo:
        security._demo()

    message = str(excinfo.value)
    assert "REBUILD_MYSQL_PASSWORD" in message, message


def test_demo_does_not_connect_without_the_app_password(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """缺密码时**一次连接都不发起**。

    光断言退出码是不够的：拿空密码去连，MySQL 会回一句 Access denied，
    退出码照样非零——测试会绿，运维还是查不出是哪个变量没配。
    """
    monkeypatch.delenv("REBUILD_MYSQL_PASSWORD", raising=False)

    attempts: list[dict[str, object]] = []

    def _refuse(**kwargs: object) -> None:
        attempts.append(kwargs)
        raise AssertionError("凭据缺失时不该尝试连接 MySQL")

    monkeypatch.setattr("pymysql.connect", _refuse)

    with pytest.raises(SystemExit):
        security._demo()

    assert attempts == [], attempts


def test_demo_uses_the_password_from_the_environment(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """有密码时用它去连——不能被"约定"改写成别的东西。"""
    monkeypatch.setenv("REBUILD_MYSQL_PASSWORD", "app-secret")

    seen = _capture_connect(monkeypatch)

    security._demo()

    assert seen["password"] == "app-secret"


def test_demo_connects_with_the_legacy_application_account(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Demo 要连的是**旧系统的应用账号**，不是新系统的只读账号。

    这场对比的立论是"旧系统用的那个账号权限太大"——连错账号，演示的
    前提就没了：只读账号 DROP 不掉探针表，"旧系统执行了第二句"那屏
    就成了假的（真跑起来会报"旧系统没有真的执行第二句"，但那是等到
    有人手动跑 demo 才发现）。

    账号名来自 REBUILD_MYSQL_USER，默认是旧系统那个 didilili
    （它在 dw 上是 ALL PRIVILEGES）；库是 dw——只读账号和旧账号连的
    是同一个库。
    """
    monkeypatch.setenv("REBUILD_MYSQL_PASSWORD", "app-secret")
    monkeypatch.delenv("REBUILD_MYSQL_USER", raising=False)

    seen = _capture_connect(monkeypatch)

    security._demo()

    assert seen["user"] == "didilili"
    assert seen["database"] == "dw"


def test_the_demo_account_can_be_overridden_by_the_environment(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """REBUILD_MYSQL_USER 能改账号——部署里就是这么配的。"""
    monkeypatch.setenv("REBUILD_MYSQL_PASSWORD", "app-secret")
    monkeypatch.setenv("REBUILD_MYSQL_USER", "someone-else")

    seen = _capture_connect(monkeypatch)

    security._demo()

    assert seen["user"] == "someone-else"


def test_execute_reports_a_missing_error_instead_of_raising(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """结果说"不 ok"却没带错误信息时，如实报出来，别抛断言。

    原来那句是 ``assert result.error is not None``——它只给类型检查看的，
    运行时什么也不保证；而且 ``python -O`` 会把它剥掉，于是同一件事变成
    一句 AttributeError 的 traceback：调用方看到的是崩溃，不是"这条被
    拒绝了"。
    """
    monkeypatch.setenv("REBUILD_DW_RO_PASSWORD", "ro-secret")

    class _Result:
        ok = False
        sql = "SELECT 1"
        rows: tuple[object, ...] = ()
        columns: tuple[str, ...] = ()
        error = None
        elapsed_ms = 1
        decision = None

    class _Executor:
        def __init__(self, **kwargs: object) -> None:
            pass

        def execute(self, sql: str) -> _Result:
            return _Result()

    monkeypatch.setattr(security, "ReadOnlyExecutor", _Executor)

    assert security._execute("SELECT 1") == 1


def _capture_connect(monkeypatch: pytest.MonkeyPatch) -> dict[str, object]:
    """把 pymysql.connect 的参数截下来，返回那个字典。

    demo 的 run_demo 也一并换成假的——这里要验的是"用哪个账号去连"，
    真的连库既慢又会动到运行中的数据库。
    """
    seen: dict[str, object] = {}

    class _Cursor:
        def __enter__(self):  # noqa: D105
            return self

        def __exit__(self, *exc: object) -> bool:  # noqa: D105
            return False

    class _Conn:
        def cursor(self):  # noqa: D102
            return _Cursor()

        def close(self) -> None:  # noqa: D102
            seen["closed"] = True

    def _fake_connect(**kwargs: object) -> _Conn:
        seen.update(kwargs)
        return _Conn()

    monkeypatch.setattr("pymysql.connect", _fake_connect)
    monkeypatch.setattr(security, "run_demo", lambda _connection: _report())
    return seen


def _report() -> object:
    class _Report:
        payload = "payload"
        legacy_validate = "ok"
        legacy_executed = True
        legacy_probe_alive = False
        new_rejected = True
        new_reason_code = "E1"
        new_probe_alive = True
        problems: list[str] = []
        ok = True

    return _Report()
