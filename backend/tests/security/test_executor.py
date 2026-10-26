"""S5 · 执行器的测试。

执行器是「策略 → 连库 → 执行 → 取行」这一整段。整段都能离线测：
连接是注进去的假连接，所以不需要 MySQL。

要盯住的性质：

    1. **策略不通过就不连库** —— 没有数据库往返。
    2. **执行的是策略改写后的那条 SQL**，不是原始文本。
    3. 会话超时确实被设置。
    4. 数据库错误被映射成契约里的错误码。
"""

from __future__ import annotations

from typing import Any

import pytest

from app.security.executor import (
    EXECUTOR_ERROR_CODES,
    SQL_EXECUTION_ERROR,
    SQL_REJECTED,
    SQL_SYNTAX,
    SQL_UNKNOWN_COLUMN,
    SQL_UNKNOWN_TABLE,
    TIMEOUT,
    ReadOnlyExecutor,
)
from app.security.config import load_policy_config


class FakeCursor:
    def __init__(self, connection: FakeConnection) -> None:
        self.connection = connection
        self.description = (("gmv",),)
        self.closed = False

    def __enter__(self) -> FakeCursor:
        return self

    def __exit__(self, *args: object) -> bool:
        return False

    def execute(self, sql: str) -> None:
        self.connection.statements.append(sql)
        error = self.connection.error_for(sql)
        if error is not None:
            raise error

    def fetchall(self) -> list[tuple[Any, ...]]:
        return list(self.connection.rows)

    def close(self) -> None:
        self.closed = True


class FakeConnection:
    def __init__(
        self,
        rows: tuple[tuple[Any, ...], ...] = ((279159.5,),),
        error: Exception | None = None,
        error_on: str = "",
    ) -> None:
        self.rows = rows
        self.error = error
        self.error_on = error_on
        self.statements: list[str] = []
        self.closed = False

    def error_for(self, sql: str) -> Exception | None:
        if self.error is None:
            return None
        if self.error_on and self.error_on not in sql:
            return None
        return self.error

    def cursor(self) -> FakeCursor:
        return FakeCursor(self)

    def close(self) -> None:
        self.closed = True


class FakeMySQLError(Exception):
    """仿 pymysql 的错误形状：`args = (错误码, 消息)`。"""

    def __init__(self, number: int, message: str) -> None:
        super().__init__(number, message)


def _mysql_error(number: int, message: str) -> Exception:
    return FakeMySQLError(number, message)


# ---------------------------------------------------------------------------
# 1. 策略不通过就不连库
# ---------------------------------------------------------------------------


def test_rejected_sql_never_touches_the_database() -> None:
    calls: list[int] = []

    def connector() -> FakeConnection:
        calls.append(1)
        return FakeConnection()

    executor = ReadOnlyExecutor(connector=connector)
    result = executor.execute("SELECT 1 FROM fact_order; DROP TABLE fact_order")

    assert not result.ok
    assert result.error is not None
    assert result.error.code == SQL_REJECTED
    assert result.error.reason_code == "SQL_MULTIPLE_STATEMENTS"
    assert calls == [], "被拒的 SQL 不该有任何数据库往返"
    assert result.sql == ""


def test_rejected_sql_reports_the_reason_code() -> None:
    executor = ReadOnlyExecutor(connector=lambda: FakeConnection())
    result = executor.execute("UPDATE fact_order SET order_amount = 0")
    assert result.error is not None
    assert result.error.code == SQL_REJECTED
    assert result.error.reason_code == "SQL_NOT_READ_ONLY"


# ---------------------------------------------------------------------------
# 2. 执行的是改写后的那条 SQL
# ---------------------------------------------------------------------------


def test_executed_sql_is_the_rewritten_one() -> None:
    connection = FakeConnection()
    executor = ReadOnlyExecutor(connector=lambda: connection)
    result = executor.execute("SELECT SUM(fact_order.order_amount) FROM fact_order")

    assert result.ok
    executed = [
        statement
        for statement in connection.statements
        if statement.upper().startswith("SELECT")
    ]
    assert len(executed) == 1
    assert "LIMIT" in executed[0]
    assert executed[0] == result.sql


def test_session_timeout_is_set_before_running() -> None:
    connection = FakeConnection()
    executor = ReadOnlyExecutor(connector=lambda: connection)
    executor.execute("SELECT 1 FROM fact_order")

    config = load_policy_config()
    assert connection.statements[0] == (
        f"SET SESSION max_execution_time = {config.max_execution_time_ms}"
    )


def test_rows_are_returned_as_dicts() -> None:
    connection = FakeConnection(rows=((1.0,), (2.0,)))
    executor = ReadOnlyExecutor(connector=lambda: connection)
    result = executor.execute("SELECT fact_order.order_amount FROM fact_order")

    assert result.ok
    assert result.columns == ("gmv",)
    assert result.rows == ({"gmv": 1.0}, {"gmv": 2.0})


def test_connection_is_closed_even_on_failure() -> None:
    connection = FakeConnection(error=_mysql_error(1064, "syntax error"))
    executor = ReadOnlyExecutor(connector=lambda: connection)
    result = executor.execute("SELECT 1 FROM fact_order")

    assert not result.ok
    assert connection.closed


# ---------------------------------------------------------------------------
# 3. 数据库错误 → 契约里的错误码
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "number,expected",
    [
        (1064, SQL_SYNTAX),
        (1146, SQL_UNKNOWN_TABLE),
        (1054, SQL_UNKNOWN_COLUMN),
        (3024, TIMEOUT),
        (1205, TIMEOUT),
        (1317, TIMEOUT),
        (2013, TIMEOUT),
        (9999, SQL_EXECUTION_ERROR),
    ],
)
def test_database_errors_are_mapped_to_contract_codes(number: int, expected: str) -> None:
    connection = FakeConnection(error=_mysql_error(number, "boom"))
    executor = ReadOnlyExecutor(connector=lambda: connection)
    result = executor.execute("SELECT 1 FROM fact_order")

    assert not result.ok
    assert result.error is not None
    assert result.error.code == expected
    assert expected in EXECUTOR_ERROR_CODES


def test_every_mapped_code_is_declared() -> None:
    from app.security.executor import _ERROR_CODE_MAP

    for code in _ERROR_CODE_MAP.values():
        assert code in EXECUTOR_ERROR_CODES


# ---------------------------------------------------------------------------
# 4. 连接层：去掉多语句
# ---------------------------------------------------------------------------


def test_connection_does_not_allow_multiple_statements(monkeypatch) -> None:
    """即使策略被绕过，一条连接也只可能跑一条语句。"""
    import pymysql
    from pymysql.constants import CLIENT

    captured: dict[str, Any] = {}

    def fake_connect(**kwargs: Any) -> FakeConnection:
        captured.update(kwargs)
        return FakeConnection()

    monkeypatch.setattr(pymysql, "connect", fake_connect)
    monkeypatch.setenv("REBUILD_DW_RO_PASSWORD", "secret")

    executor = ReadOnlyExecutor()
    executor.execute("SELECT 1 FROM fact_order")

    flags = captured["client_flag"]
    assert not flags & CLIENT.MULTI_STATEMENTS
    assert captured["user"] == load_policy_config().account_user
    assert captured["database"] == load_policy_config().account_database
    assert captured["read_timeout"] == load_policy_config().read_timeout_s
    assert captured["connect_timeout"] == load_policy_config().connect_timeout_s


def test_missing_password_fails_loudly(monkeypatch) -> None:
    """密码不在仓库里；没给就报错，不能悄悄连成别的账号。"""
    from app.security.executor import ExecutorError

    monkeypatch.delenv("REBUILD_DW_RO_PASSWORD", raising=False)
    executor = ReadOnlyExecutor()
    with pytest.raises(ExecutorError):
        executor.execute("SELECT 1 FROM fact_order")
