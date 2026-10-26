"""S5 · 安全的查询入口。

把 S4 的生成和 S5 的执行接起来：

    question
      -> generate（S4：生成 + 静态校验）
      -> enforce（S5：策略）        ← 不通过就是 SQL_REJECTED
      -> execute（S5：只读账号 + 超时）

**这里不发明新的失败通道**。S4 的拒答（`OUT_OF_RANGE` 之类的生成阶段结论）
和 S5 的执行失败（`SQL_REJECTED` / `TIMEOUT`）是两类东西，所以结果对象把
两段结论都带着，而不是硬凑成一个码——把两者统一成 SSE 错误通道是 S6 的活
（路线图 S6：「错误码表 + UI 状态机」）。
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Any

from app.generate.models import GenerationResult
from app.generate.pipeline import generate
from app.security.executor import ExecutionResult, ReadOnlyExecutor


@dataclass(frozen=True)
class SecureResult:
    """一次「生成 + 安全执行」的完整结论。"""

    query: str
    generation: GenerationResult
    execution: ExecutionResult | None = None

    @property
    def outcome(self) -> str:
        """四选一：refused（生成阶段拒答）/ rows / rejected（策略拒绝）/ error。"""
        if not self.generation.ok:
            return "refused"
        if self.execution is None:
            return "refused"
        if self.execution.ok:
            return "rows"
        if self.execution.error and self.execution.error.code == "SQL_REJECTED":
            return "rejected"
        return "error"

    @property
    def sql(self) -> str | None:
        return self.execution.sql if self.execution else None

    def to_dict(self) -> dict[str, Any]:
        refusal = None
        if self.generation.refusal is not None:
            refusal = {
                "code": self.generation.refusal.code,
                "reason": self.generation.refusal.reason,
            }
        return {
            "query": self.query,
            "outcome": self.outcome,
            "sql": self.sql,
            "refusal": refusal,
            "error": self.execution.error.to_dict() if self.execution and self.execution.error else None,
            "columns": list(self.execution.columns) if self.execution else [],
            "row_count": len(self.execution.rows) if self.execution else 0,
            "elapsed_ms": self.execution.elapsed_ms if self.execution else 0,
        }


def secure_execute(
    sql: str,
    *,
    executor: ReadOnlyExecutor | None = None,
) -> SecureResult:
    """执行一条 SQL（策略 + 只读账号 + 超时）。

    生成阶段没有参与，所以 `generation` 是个空壳结论；这条入口给的是
    「已经拿到 SQL，只想安全地跑它」的场景（比如校正闭环里重跑一条 SQL）。
    """
    from app.generate.models import Refusal

    engine = executor or ReadOnlyExecutor()
    execution = engine.execute(sql)
    stub = GenerationResult(
        query="",
        outcome="sql" if execution.ok else "refusal",
        sql=execution.sql or None,
        refusal=(
            None
            if execution.ok
            else Refusal(
                code=execution.error.code if execution.error else "SQL_REJECTED",
                reason=execution.error.message if execution.error else "",
            )
        ),
    )
    return SecureResult(query="", generation=stub, execution=execution)


def secure_query(
    query: str,
    *,
    executor: ReadOnlyExecutor | None = None,
    now: datetime | None = None,
    context: Any = None,
) -> SecureResult:
    """生成一条 SQL 并安全地执行它。"""
    generated = generate(query, context=context, now=now)
    if not generated.ok:
        return SecureResult(query=query, generation=generated, execution=None)

    engine = executor or ReadOnlyExecutor()
    execution = engine.execute(generated.sql or "")
    return SecureResult(query=query, generation=generated, execution=execution)
