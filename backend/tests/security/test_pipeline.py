"""S5 · 查询入口的测试。

`secure_query` = S4 生成 + S5 执行。要盯住的是**失败的去向**：

    - 生成阶段拒答 → 不进数据库，outcome = refused
    - 策略拒绝     → outcome = rejected，error.code = SQL_REJECTED
    - 执行成功     → outcome = rows
    - 执行失败     → outcome = error

四种结局互不重叠，也**没有第五种**（「既没成功也没 error」在结构上不可能）。
"""

from __future__ import annotations

from typing import Any

from app.security.executor import ExecutionError, ExecutionResult
from app.security.pipeline import secure_execute, secure_query


class FakeExecutor:
    """只记录被执行的 SQL，返回固定结果。"""

    def __init__(self, result: ExecutionResult | None = None) -> None:
        self.result = result
        self.executed: list[str] = []

    def execute(self, sql: str) -> ExecutionResult:
        self.executed.append(sql)
        if self.result is not None:
            return self.result
        return ExecutionResult(ok=True, sql=sql, rows=({"a": 1},), columns=("a",))


def _rejected(reason_code: str = "SQL_MULTIPLE_STATEMENTS") -> ExecutionResult:
    return ExecutionResult(
        ok=False,
        sql="",
        error=ExecutionError(
            code="SQL_REJECTED",
            message="被拒",
            reason_code=reason_code,
        ),
    )


def test_successful_query_returns_rows() -> None:
    executor = FakeExecutor()
    result = secure_query("华北销售额", executor=executor)

    assert result.outcome == "rows"
    assert result.sql
    assert result.to_dict()["row_count"] == 1


def test_refused_query_never_reaches_the_database() -> None:
    """越界题在生成阶段就拒了，不该有任何数据库往返。"""
    executor = FakeExecutor()
    result = secure_query("2024 年销售额", executor=executor)

    assert result.outcome == "refused"
    assert result.execution is None
    assert executor.executed == []
    assert result.generation.refusal is not None
    assert result.generation.refusal.code == "OUT_OF_RANGE"


def test_policy_rejection_is_reported_as_rejected() -> None:
    executor = FakeExecutor(_rejected())
    result = secure_query("华北销售额", executor=executor)

    assert result.outcome == "rejected"
    assert result.execution is not None
    assert result.execution.error is not None
    assert result.execution.error.code == "SQL_REJECTED"


def test_execution_failure_is_reported_as_error() -> None:
    executor = FakeExecutor(
        ExecutionResult(
            ok=False,
            sql="SELECT 1",
            error=ExecutionError(code="TIMEOUT", message="超时"),
        )
    )
    result = secure_query("华北销售额", executor=executor)

    assert result.outcome == "error"
    assert result.execution is not None
    assert result.execution.error is not None
    assert result.execution.error.code == "TIMEOUT"


def test_secure_execute_runs_the_given_sql() -> None:
    executor = FakeExecutor()
    result = secure_execute("SELECT 1 FROM fact_order", executor=executor)

    assert result.outcome == "rows"
    assert executor.executed == ["SELECT 1 FROM fact_order"]


def test_result_payload_has_a_stable_shape() -> None:
    result = secure_query("华北销售额", executor=FakeExecutor())
    payload: dict[str, Any] = result.to_dict()

    assert set(payload) == {
        "query",
        "outcome",
        "sql",
        "refusal",
        "error",
        "columns",
        "row_count",
        "elapsed_ms",
    }
    assert payload["error"] is None
