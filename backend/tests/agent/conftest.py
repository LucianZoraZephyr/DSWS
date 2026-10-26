"""S6 · 测试用的替身。

只替换两个外部依赖：**模型**与**数据库**。校验、策略、事件、协议全都是
真代码——所以这里断言的东西，屏幕上也成立。
"""

from __future__ import annotations

from typing import Any

import pytest

from app.generate.context import build_offline_context
from app.security.executor import ExecutionError, ExecutionResult

#: 一条在「华北销售额」上下文里合法的 SQL。
GOOD_SQL = (
    "SELECT dim_region.region_name AS `地区`, "
    "SUM(fact_order.order_amount) AS `销售额` "
    "FROM fact_order "
    "JOIN dim_region ON fact_order.region_id = dim_region.region_id "
    "GROUP BY dim_region.region_name"
)

#: 括号不配对 —— 解析器读不出来。
BROKEN_SQL = "SELECT SUM(fact_order.order_amount FROM fact_order"

#: 引用了上下文之外的列。
UNKNOWN_COLUMN_SQL = (
    "SELECT dim_region.no_such_column AS `地区` "
    "FROM fact_order "
    "JOIN dim_region ON fact_order.region_id = dim_region.region_id"
)

#: 通过静态校验，但被安全策略拦下（UNION 是外带数据的载体）。
UNION_SQL = (
    "SELECT dim_region.region_name AS `地区`, "
    "SUM(fact_order.order_amount) AS `销售额` "
    "FROM fact_order "
    "JOIN dim_region ON fact_order.region_id = dim_region.region_id "
    "GROUP BY dim_region.region_name "
    "UNION "
    "SELECT dim_region.region_name, SUM(fact_order.order_amount) "
    "FROM fact_order "
    "JOIN dim_region ON fact_order.region_id = dim_region.region_id "
    "GROUP BY dim_region.region_name"
)

QUERY = "华北销售额"


class FakeGenerator:
    """返回固定产物的生成器。"""

    name = "fake"

    def __init__(self, payload: dict[str, Any]) -> None:
        self.payload = dict(payload)
        self.calls: list[str] = []

    def generate(self, context: Any, prompt: str) -> dict[str, Any]:
        self.calls.append(prompt)
        return dict(self.payload)


class FakeExecutor:
    """返回固定结果的执行器。"""

    def __init__(
        self,
        *,
        rows: tuple[dict[str, Any], ...] = (),
        columns: tuple[str, ...] = (),
        error: ExecutionError | None = None,
    ) -> None:
        self.rows = rows
        self.columns = columns
        self.error = error
        self.seen: list[str] = []

    def execute(self, sql: str) -> ExecutionResult:
        self.seen.append(sql)
        if self.error is not None:
            return ExecutionResult(ok=False, sql=sql, error=self.error)
        return ExecutionResult(
            ok=True,
            sql=sql,
            rows=self.rows,
            columns=self.columns,
        )


@pytest.fixture()
def context():
    """「华北销售额」的离线上下文。"""
    return build_offline_context(QUERY)


@pytest.fixture()
def good_rows() -> tuple[tuple[str, ...], tuple[dict[str, Any], ...]]:
    columns = ("地区", "销售额")
    rows = ({"地区": "华北", "销售额": 128400.0},)
    return columns, rows
