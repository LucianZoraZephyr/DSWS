"""S5 · 只读账号的测试。

要盯住的性质：

    1. 开通语句**恰好**给出策略声明的那几项权限——不多不少。
    2. 密码不进任何打印路径。
    3. 验证不只是读权限表，还要**真的写一次**看数据库让不让。
"""

from __future__ import annotations

from typing import Any

import pytest

from app.security.account import (
    AccountSpec,
    show_grants,
    verify_account,
)
from app.security.config import load_policy_config


class FakeCursor:
    def __init__(self, connection: FakeConnection) -> None:
        self.connection = connection
        self.description = (("Grants for dw_reader@%",),)
        self._rows: list[tuple[Any, ...]] = []

    def __enter__(self) -> FakeCursor:
        return self

    def __exit__(self, *args: object) -> bool:
        return False

    def execute(self, sql: str) -> None:
        self.connection.statements.append(sql)
        if sql.upper().startswith("SHOW GRANTS"):
            self._rows = [(grant,) for grant in self.connection.grants]
            return
        if sql.upper().startswith("UPDATE"):
            if self.connection.write_error is not None:
                raise self.connection.write_error
            self.connection.write_succeeded = True
            return
        self._rows = []

    def fetchall(self) -> list[tuple[Any, ...]]:
        return list(self._rows)


class FakeConnection:
    def __init__(
        self,
        grants: tuple[str, ...] = (
            "GRANT USAGE ON *.* TO `dw_reader`@`%`",
            "GRANT SELECT ON `dw`.* TO `dw_reader`@`%`",
        ),
        write_error: Exception | None = None,
    ) -> None:
        self.grants = grants
        self.write_error = write_error
        self.write_succeeded = False
        self.statements: list[str] = []

    def cursor(self) -> FakeCursor:
        return FakeCursor(self)


class PermissionDenied(Exception):
    """仿 MySQL 的 1142。"""

    def __init__(self) -> None:
        super().__init__(1142, "UPDATE command denied to user 'dw_reader'@'%'")


# ---------------------------------------------------------------------------
# 开通语句
# ---------------------------------------------------------------------------


@pytest.fixture()
def spec() -> AccountSpec:
    return AccountSpec.from_config(
        load_policy_config(),
        password="s3cret",
    )


def test_statements_grant_exactly_select(spec: AccountSpec) -> None:
    statements = spec.statements()
    grants = [item for item in statements if item.startswith("GRANT")]
    assert grants == ["GRANT SELECT ON `dw`.* TO 'dw_reader'@'%'"]
    assert not any("ALL PRIVILEGES" in item for item in statements)


def test_statements_are_idempotent(spec: AccountSpec) -> None:
    """先 DROP 再 CREATE：这个账号的权限集合永远是确定的那一套。"""
    statements = spec.statements()
    assert statements[0] == "DROP USER IF EXISTS 'dw_reader'@'%'"
    assert statements[1].startswith("CREATE USER 'dw_reader'@'%'")
    assert statements[-1] == "FLUSH PRIVILEGES"


def test_account_name_comes_from_the_contract(spec: AccountSpec) -> None:
    config = load_policy_config()
    assert spec.user == config.account_user
    assert spec.database == config.account_database
    assert spec.grants == config.account_grants


def test_safe_repr_does_not_leak_the_password(spec: AccountSpec) -> None:
    assert "s3cret" not in spec.safe_repr()
    assert "dw_reader" in spec.safe_repr()


# ---------------------------------------------------------------------------
# 验证
# ---------------------------------------------------------------------------


def test_verify_passes_for_a_read_only_account(spec: AccountSpec) -> None:
    connection = FakeConnection(write_error=PermissionDenied())
    report = verify_account(connection, spec)

    assert report.ok, report.problems
    assert report.grants
    assert "拒绝" in report.write_attempt


def test_verify_detects_extra_privileges(spec: AccountSpec) -> None:
    connection = FakeConnection(
        grants=(
            "GRANT USAGE ON *.* TO `dw_reader`@`%`",
            "GRANT SELECT, INSERT, UPDATE ON `dw`.* TO `dw_reader`@`%`",
        ),
        write_error=PermissionDenied(),
    )
    report = verify_account(connection, spec)

    assert not report.ok
    assert any("不该有的权限" in item for item in report.problems)


def test_verify_detects_all_privileges(spec: AccountSpec) -> None:
    connection = FakeConnection(
        grants=("GRANT ALL PRIVILEGES ON `dw`.* TO `dw_reader`@`%`",),
        write_error=PermissionDenied(),
    )
    report = verify_account(connection, spec)

    assert not report.ok
    assert any("不该有的权限" in item for item in report.problems)


def test_verify_detects_a_writable_account(spec: AccountSpec) -> None:
    """写操作居然成功了 —— 这是最严重的一种事故。"""
    connection = FakeConnection(write_error=None)
    report = verify_account(connection, spec)

    assert not report.ok
    assert connection.write_succeeded
    assert any("放行" in item for item in report.problems)


def test_verify_rejects_a_write_failure_that_is_not_a_permission_error(
    spec: AccountSpec,
) -> None:
    """表不存在之类的错误不能当成「验证通过」——那说明验证没跑起来。"""
    connection = FakeConnection(write_error=Exception(1146, "Table doesn't exist"))
    report = verify_account(connection, spec)

    assert not report.ok
    assert any("不是权限不足" in item for item in report.problems)


def test_show_grants_returns_the_rows(spec: AccountSpec) -> None:
    connection = FakeConnection()
    grants = show_grants(connection, spec)
    assert len(grants) == 2
    assert "SELECT" in grants[1]
