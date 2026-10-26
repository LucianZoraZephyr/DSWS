"""S5 · Demo 的测试。

Demo 要证明的是一件具体的事：**同一段 payload，旧系统执行、新系统拒绝**。

这里用一个仿旧系统的假连接：带多语句标志，`explain {sql}` 只解释第一句
（所以不报错），`execute {sql}` 把整串语句都执行了（所以 DROP 真生效）。
这正是审计 D6 实测到的旧系统行为。

真连 MySQL 的那一次在 `python -m app.scripts.security --demo` 里；
这里是它的离线版本——逻辑一样，跑得快，而且不碰任何真库。
"""

from __future__ import annotations

import re
from typing import Any

from app.security.demo import PROBE_TABLE, run_demo


class FakeLegacyCursor:
    """仿旧系统的游标：语句按分号切开，一条一条执行。"""

    def __init__(self, connection: FakeLegacyConnection) -> None:
        self.connection = connection
        self._rows: list[tuple[Any, ...]] = []
        self._has_more = False

    def execute(self, sql: str) -> None:
        self.connection.statements.append(sql)
        self._rows = []
        self._has_more = False

        if sql.upper().startswith("SHOW TABLES LIKE"):
            pattern = sql.split("'")[1]
            rows = [
                (name,)
                for name in sorted(self.connection.tables)
                if re.fullmatch(pattern.replace("%", ".*"), name)
            ]
            self._rows = rows
            return

        # 旧系统的 validate()：explain 只解释第一句，后面的语句照样执行。
        payload = sql
        if payload.upper().startswith("EXPLAIN "):
            payload = payload[len("EXPLAIN ") :]

        for statement in payload.split(";"):
            text = statement.strip()
            if not text:
                continue
            upper = text.upper()
            if upper.startswith("CREATE TABLE"):
                self.connection.tables.add(text.split()[2])
            elif upper.startswith("DROP TABLE"):
                # DROP TABLE x / DROP TABLE IF EXISTS x —— 表名总是最后一个词
                self.connection.tables.discard(text.split()[-1])
            elif upper.startswith("SELECT"):
                self._rows = [(1,)]
                self._has_more = True

    def fetchall(self) -> list[tuple[Any, ...]]:
        return list(self._rows)

    def nextset(self) -> bool:
        if self._has_more:
            self._has_more = False
            return True
        return False

    def close(self) -> None:
        return None


class FakeLegacyConnection:
    def __init__(self) -> None:
        self.tables: set[str] = set()
        self.statements: list[str] = []

    def cursor(self) -> FakeLegacyCursor:
        return FakeLegacyCursor(self)


def test_demo_reproduces_the_old_behaviour_and_blocks_the_new_one() -> None:
    connection = FakeLegacyConnection()
    report = run_demo(connection)

    assert report.ok, report.problems
    # 旧系统：校验通过，而且第二句真的执行了
    assert report.legacy_validate.startswith("通过")
    assert report.legacy_executed
    assert not report.legacy_probe_alive
    # 新系统：策略拒绝，数据库根本没被碰过
    assert report.new_rejected
    assert report.new_reason_code == "SQL_MULTIPLE_STATEMENTS"
    assert report.new_probe_alive


def test_demo_cleans_up_the_probe_table() -> None:
    connection = FakeLegacyConnection()
    run_demo(connection)
    assert PROBE_TABLE not in connection.tables


def test_demo_uses_the_legacy_explain_validation() -> None:
    """复现必须用旧系统**真实**的校验方式，否则对比没有意义。"""
    connection = FakeLegacyConnection()
    run_demo(connection)

    explains = [
        statement
        for statement in connection.statements
        if statement.upper().startswith("EXPLAIN ")
    ]
    assert len(explains) == 1
    assert "DROP TABLE" in explains[0]
