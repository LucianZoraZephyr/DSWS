"""S6 · 编排器。

把 S4 的生成、S5 的策略与执行、S6 的校正接成一条**会发事件**的流水线：

    上下文 8 阶段  -> generate_sql -> validate_sql -> [correct_sql] -> run_sql
                                                                   -> result / error

三条不变式：

    1. **每一步都有真事**。上下文那 8 个阶段是 `OfflineContextBuilder` 的
       8 个真实阶段（`app.generate.context`），不是一个假事件。
    2. **每一种失败都有名字**。所有失败都变成 `ChannelError`，码来自
       `contracts/errors.yaml`；没有「失败但没人知道」。
    3. **流必须以 result 或 error 收尾**。任何异常都被兜住并变成
       `INTERNAL_ERROR`——绝不静默结束（旧系统最大的缺陷）。

取消也是一条路径：编排器在每一步之前检查取消开关，被取消时发一条
`CANCELLED` 错误事件就收尾，**后面的步骤不再开始**。
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from datetime import date, datetime
from typing import Any, Callable, Iterator, Protocol

from app.agent.cancel import CancelToken
from app.agent.correct import CorrectionProposal, SqlCorrector, propose_correction
from app.agent.errors import (
    CANCELLED_CODE,
    EMPTY_RESULT,
    INTERNAL_ERROR,
    LLM_REFUSAL,
    ChannelError,
    ErrorTable,
    build_error,
    load_error_table,
)
from app.agent.validate import DEFAULT_GATES, validate_sql
from app.generate.context import OfflineContextBuilder
from app.generate.models import GenerateContext
from app.generate.pipeline import draft_sql
from app.generate.provider import SqlGenerator
from app.security.executor import ExecutionResult, ReadOnlyExecutor

# 步骤 id（必须来自 contracts/steps.yaml）
GENERATE_STEP = "generate_sql"
VALIDATE_STEP = "validate_sql"
CORRECT_STEP = "correct_sql"
RUN_STEP = "run_sql"


class SqlExecutor(Protocol):
    """执行器接口。S5 的 `ReadOnlyExecutor` 就是它。"""

    def execute(self, sql: str) -> ExecutionResult:
        ...


# ---------------------------------------------------------------------------
# 结果负载
# ---------------------------------------------------------------------------


def _cell_type(value: Any) -> str:
    """单元格类型 → `contracts/sse.schema.json` 里的枚举。"""
    if value is None:
        return "null"
    if isinstance(value, bool):
        return "boolean"
    if isinstance(value, (int, float)):
        return "number"
    if isinstance(value, (datetime, date)):
        return "date"
    return "string"


def to_result_payload(execution: ExecutionResult) -> dict[str, Any]:
    """执行结果 → SSE `result` 事件的数据部分。

    列名来自执行器，行按列顺序摊成数组——`contracts/sse.schema.json`
    里的 `rows` 是二维数组，不是对象数组。

    列类型从**这一列第一个非空值**推断：整列都是 NULL 时才是 `null`。
    """
    columns = list(execution.columns)
    rows: list[list[Any]] = []
    for row in execution.rows:
        rows.append([row.get(name) for name in columns])

    def column_type(index: int) -> str:
        for row in rows:
            value = row[index]
            if value is not None:
                return _cell_type(value)
        return "null"

    return {
        "columns": [
            {"name": name, "type": column_type(index)}
            for index, name in enumerate(columns)
        ],
        "rows": rows,
    }


# ---------------------------------------------------------------------------
# 编排器
# ---------------------------------------------------------------------------


@dataclass
class AgentRunner:
    """一次问数的编排器。

    依赖全部是**注入**的：生成器、校正器、执行器、上下文构建器。这样
    整条链路（生成 → 校验 → 校正 → 执行 → 事件）都能离线测：只把 LLM
    和 MySQL 换掉，别的一行都不改。
    """

    query: str
    generator: SqlGenerator | None = None
    corrector: SqlCorrector | None = None
    executor: SqlExecutor | None = None
    context_factory: Callable[[str], OfflineContextBuilder] | None = None
    cancel: CancelToken | None = None
    now: datetime | None = None
    request_id: str | None = None
    table: ErrorTable = field(default_factory=load_error_table)

    #: 条件步骤 `correct_sql` 是否已经有了结论（跑过 或 显式 skipped）。
    _correct_resolved: bool = False

    # -- 事件构造 ---------------------------------------------------------

    def _progress(
        self,
        step: str,
        status: str,
        duration_ms: float | None = None,
    ) -> dict[str, Any]:
        return {
            "type": "progress",
            "step": step,
            "status": status,
            "duration_ms": duration_ms,
        }

    def _error_event(self, error: ChannelError) -> dict[str, Any]:
        return error.to_event()

    def _cancelled(self) -> bool:
        return self.cancel is not None and self.cancel.cancelled

    def _skip_correct(self) -> Iterator[dict[str, Any]]:
        """条件步骤必须**显式**说「未触发」。

        旧前端硬编码 12 个节点，`correct_sql` 永远灰着（审计 D4）。规则是：
        条件步骤要么跑（running → success/error），要么显式 skipped——
        **不能永远是一块灰的 pending**。所以流在中途结束（生成失败、
        取消）时，这里补上那一条 skipped。
        """
        if self._correct_resolved:
            return
        self._correct_resolved = True
        yield self._progress(CORRECT_STEP, "skipped")

    def _cancel_error(self) -> ChannelError:
        return build_error(
            CANCELLED_CODE,
            "本次问数已被用户取消。",
            step=None,
            request_id=self.request_id,
        )

    def _internal_error(self, message: str, step: str | None = None) -> ChannelError:
        return build_error(
            INTERNAL_ERROR,
            message,
            step=step,
            request_id=self.request_id,
        )

    # -- 主流程 -----------------------------------------------------------

    def events(self) -> Iterator[dict[str, Any]]:
        """产出这一次问数的全部 SSE 事件。

        流**一定**以 `result` 或 `error` 收尾：任何异常都被兜住。
        """
        try:
            yield from self._run()
        except Exception as exc:  # noqa: BLE001 - 兜底：绝不静默结束
            yield self._error_event(
                self._internal_error(f"问数过程中发生未预期的错误：{exc}")
            )

    def _run(self) -> Iterator[dict[str, Any]]:
        # --- 1. 上下文：8 个真实阶段 ---------------------------------------
        factory = self.context_factory or (
            lambda query: OfflineContextBuilder(query, now=self.now)
        )
        builder = factory(self.query)
        for phase in builder.phases():
            if self._cancelled():
                yield from self._skip_correct()
                yield self._error_event(self._cancel_error())
                return
            yield self._progress(phase.step, "running")
            started = time.perf_counter()
            try:
                phase.run()
            except Exception as exc:  # noqa: BLE001
                elapsed = round((time.perf_counter() - started) * 1000, 1)
                yield self._progress(phase.step, "error", elapsed)
                yield from self._skip_correct()
                yield self._error_event(
                    self._internal_error(
                        f"步骤「{phase.step}」执行失败：{exc}", step=phase.step
                    )
                )
                return
            elapsed = round((time.perf_counter() - started) * 1000, 1)
            yield self._progress(phase.step, "success", elapsed)

        context: GenerateContext = builder.result()

        # --- 2. 生成 --------------------------------------------------------
        yield self._progress(GENERATE_STEP, "running")
        started = time.perf_counter()
        try:
            draft = draft_sql(
                self.query,
                context=context,
                generator=self.generator,
                now=self.now,
            )
        except Exception as exc:  # noqa: BLE001
            elapsed = round((time.perf_counter() - started) * 1000, 1)
            yield self._progress(GENERATE_STEP, "error", elapsed)
            yield self._error_event(
                self._internal_error(f"生成阶段失败：{exc}", step=GENERATE_STEP)
            )
            return
        elapsed = round((time.perf_counter() - started) * 1000, 1)

        if draft.refusal is not None:
            yield self._progress(GENERATE_STEP, "error", elapsed)
            yield from self._skip_correct()
            yield self._error_event(
                build_error(
                    LLM_REFUSAL,
                    draft.refusal.reason,
                    step=GENERATE_STEP,
                    reason_code=draft.refusal.code,
                    request_id=self.request_id,
                )
            )
            return

        yield self._progress(GENERATE_STEP, "success", elapsed)

        # --- 3~5. 校验 → 校正 → 执行 ---------------------------------------
        sql = draft.sql or ""
        original_sql = sql
        corrected_sql: str | None = None
        attempts = 0
        correction_attempted = False
        max_attempts = self.table.correction.max_attempts

        while True:
            if self._cancelled():
                yield from self._skip_correct()
                yield self._error_event(self._cancel_error())
                return

            # --- 校验（S4 静态校验 + S5 安全策略）--------------------------
            yield self._progress(VALIDATE_STEP, "running")
            started = time.perf_counter()
            failure = validate_sql(sql, context, self.query, gates=DEFAULT_GATES)
            elapsed = round((time.perf_counter() - started) * 1000, 1)

            if failure is None:
                yield self._progress(VALIDATE_STEP, "success", elapsed)
                if not correction_attempted:
                    # 条件步骤必须**显式**说「未触发」，不能永远是一块灰的
                    # pending（审计 D4：旧前端硬编码 12 个节点，correct_sql
                    # 永远灰着）。
                    yield from self._skip_correct()
            else:
                yield self._progress(VALIDATE_STEP, "error", elapsed)
                if attempts >= max_attempts or not failure.retryable:
                    yield from self._skip_correct()
                    yield self._error_event(
                        self._decorate(
                            failure,
                            step=VALIDATE_STEP,
                            original_sql=original_sql,
                            corrected_sql=corrected_sql,
                            attempts=attempts,
                        )
                    )
                    return

                # --- 校正（最多一次）--------------------------------------
                correction_attempted = True
                proposal = yield from self._correct(
                    context, sql, failure, attempts
                )
                if not proposal.ok:
                    yield self._error_event(
                        self._decorate(
                            proposal.error or failure,
                            step=VALIDATE_STEP,
                            original_sql=original_sql,
                            corrected_sql=corrected_sql,
                            attempts=attempts,
                        )
                    )
                    return
                attempts += 1
                corrected_sql = proposal.sql
                sql = proposal.sql or sql
                # 回到循环顶部：**重新校验**校正后的 SQL
                continue

            # --- 执行 ------------------------------------------------------
            if self._cancelled():
                yield from self._skip_correct()
                yield self._error_event(self._cancel_error())
                return

            yield self._progress(RUN_STEP, "running")
            started = time.perf_counter()
            try:
                execution = self._execute(sql)
            except Exception as exc:  # noqa: BLE001 - 连不上库也算一种失败
                elapsed = round((time.perf_counter() - started) * 1000, 1)
                yield self._progress(RUN_STEP, "error", elapsed)
                yield self._error_event(
                    self._decorate(
                        self._internal_error(f"执行阶段失败：{exc}", step=RUN_STEP),
                        step=RUN_STEP,
                        original_sql=original_sql,
                        corrected_sql=corrected_sql,
                        attempts=attempts,
                    )
                )
                return
            elapsed = round((time.perf_counter() - started) * 1000, 1)

            if execution.ok:
                if self._cancelled():
                    # 取消发生在执行期间：这次问数不算成功——用户已经说了
                    # 「停」。结果照常丢弃，换成一条 CANCELLED。
                    yield from self._skip_correct()
                    yield self._error_event(self._cancel_error())
                    return
                yield self._progress(RUN_STEP, "success", elapsed)
                if not execution.rows:
                    # 空结果**不是失败**：库里真的没这批数据。步骤保持绿色，
                    # 界面给一张空态卡（审计 FP-8 的结论）。
                    yield self._error_event(
                        build_error(
                            EMPTY_RESULT,
                            "查询执行成功，但没有符合条件的数据。",
                            step=RUN_STEP,
                            sql=sql,
                            rows=0,
                            original_sql=original_sql,
                            corrected_sql=corrected_sql,
                            request_id=self.request_id,
                        )
                    )
                    return
                yield {"type": "result", "data": to_result_payload(execution)}
                return

            yield self._progress(RUN_STEP, "error", elapsed)
            failure = self._execution_error(execution, step=RUN_STEP)
            # 执行阶段的失败**不再校正**。
            #
            # 校正要修的是「生成出来的 SQL」，而能执行到这里说明它已经过了
            # 静态校验与安全策略。此刻的数据库错误（列在库里不存在、超时）
            # 不是「最小必要修正」能可靠修好的东西，再问一次模型只会得到
            # 一个说不准对不对的 SQL。
            #
            # 旧系统是从 `EXPLAIN` 失败触发校正的——在新系统里，`EXPLAIN`
            # 这个位置就是**校验阶段**，触发点并没有变，只是这一关比旧系统
            # 强得多（AST 语义 + 安全策略）。
            yield self._error_event(
                self._decorate(
                    failure,
                    step=RUN_STEP,
                    original_sql=original_sql,
                    corrected_sql=corrected_sql,
                    attempts=attempts,
                )
            )
            return

    # -- 校正步骤 ---------------------------------------------------------

    def _correct(
        self,
        context: GenerateContext,
        sql: str,
        error: ChannelError,
        attempts: int,
    ) -> Iterator[dict[str, Any]]:
        """跑一次校正，发 `correct_sql` 的事件，返回校正结论。"""
        self._correct_resolved = True
        yield self._progress(CORRECT_STEP, "running")
        started = time.perf_counter()
        proposal: CorrectionProposal = propose_correction(
            context,
            sql,
            error,
            corrector=self.corrector,
            table=self.table,
        )
        elapsed = round((time.perf_counter() - started) * 1000, 1)
        if proposal.ok:
            yield self._progress(CORRECT_STEP, "success", elapsed)
        else:
            yield self._progress(CORRECT_STEP, "error", elapsed)
        return proposal

    # -- 执行 -------------------------------------------------------------

    def _execute(self, sql: str) -> ExecutionResult:
        executor = self.executor or ReadOnlyExecutor()
        return executor.execute(sql)

    def _execution_error(self, execution: ExecutionResult, *, step: str) -> ChannelError:
        """执行器的错误 → 通道错误。

        执行器给的码如果不在错误码表里（自定义执行器可能给别的码），
        退到 `INTERNAL_ERROR`——**失败必须有名字**，不能因为码不认识就
        悄悄消失。
        """
        error = execution.error
        if error is None:
            return self._internal_error("执行失败但没有给出错误原因", step=step)
        if error.code not in self.table:
            return build_error(
                INTERNAL_ERROR,
                f"执行器返回了未登记的错误码 {error.code}：{error.message}",
                step=step,
                reason_code=error.code,
            )
        return build_error(
            error.code,
            error.message,
            step=step,
            reason_code=error.reason_code or error.code,
        )

    # -- 细节 -------------------------------------------------------------

    def _decorate(
        self,
        error: ChannelError,
        *,
        step: str,
        original_sql: str,
        corrected_sql: str | None,
        attempts: int,
    ) -> ChannelError:
        """给错误补上「原始 SQL / 校正后的 SQL / 重试次数」。

        这三样是「保留原始 SQL」这条要求的落点：校正之后仍然能看清
        最初那条 SQL 是什么样、模型改成了什么样、试了几次。
        """
        detail = dict(error.detail)
        detail.setdefault("original_sql", original_sql)
        if corrected_sql:
            detail.setdefault("corrected_sql", corrected_sql)
        detail.setdefault("attempts", attempts)
        if self.request_id:
            detail.setdefault("request_id", self.request_id)
        return ChannelError(
            code=error.code,
            message=error.message,
            step=error.step or step,
            detail=detail,
        )
