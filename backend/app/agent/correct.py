"""S6 · 校正闭环。

路线图 S6 的第一件交付物：

    > `correct_sql`：最多 1 次重试、**校正后重新校验**、保留原始 SQL、
    > 清空旧 `error`

旧系统也有一个叫 `correct_sql` 的节点，但它有三个问题：

    1. **没有次数上限**——校验失败 → 校正 → 再校验 → 再校正 …… 可能绕圈。
    2. **校正结果不复查**——模型改完的 SQL 直接覆盖 `state["sql"]`，
       没人再过一次校验。
    3. **失败被吞掉**——节点里 `except` 之后 `raise`，但 `state["error"]`
       还留着上一轮的旧错误，界面同时显示「已校正」和「校验失败」。

这里的做法：

    - 次数上限写在契约里（`contracts/errors.yaml` 的 `correction` 段）；
    - 校正后的 SQL **必须重新过两道关**：S4 的静态校验（guard）与 S5 的
      安全策略（policy），由编排器在 `validate_sql` 步骤里重跑一遍
      （见 `app.agent.validate`）。策略这一关是重点：校正后的 SQL 同样
      不许写、不许多语句、不许出白名单——**校正不是绕过安全检查的后门**；
    - 原始 SQL 一直留着（`detail.original_sql`），校正后的也留着
      （`detail.corrected_sql`），谁都能对比；
    - 校正成功 → **清空旧 error**；校正失败 → 旧 error 仍然是根因。

安全策略的拒绝（`SQL_REJECTED`）**不参与校正**：拿一条被安全策略拒掉的
SQL 去问模型「改一下」，等于给注入开了重试的口子。
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Protocol

from app.agent.errors import ChannelError, ErrorTable, load_error_table
from app.generate.models import GenerateContext
from app.generate.output_schema import validate_payload
from app.generate.prompts import render_prompt
from app.generate.provider import dump_context, metric_infos, table_infos


@dataclass(frozen=True)
class CorrectionProposal:
    """一次校正尝试的结论。

    `ok` 为 True 时 `sql` 是校正器给出的**候选** SQL——它还没被重新校验，
    校验由编排器在 `validate_sql` 步骤里做（这样事件顺序才对：先
    「校正SQL」再「校验SQL」）。

    `ok` 为 False 时 `error` 是**仍然成立**的那个错误：校正器炸了、产物
    不合契约、或者它自己拒答了——根因没变，错误也不该变。
    """

    attempted: bool
    ok: bool
    sql: str | None = None
    error: ChannelError | None = None
    note: str = ""


class SqlCorrector(Protocol):
    """校正器接口。返回的 dict 必须符合 `sql_or_refusal`。"""

    name: str

    def correct(
        self,
        context: GenerateContext,
        prompt: str,
        sql: str,
        error: str,
    ) -> dict[str, Any]:
        ...


class LlmCorrector:
    """用真实模型校正。

    它直接复用 S4 的 `SqlGenerator` 接口：生成与校正的差别只是**提示词
    不同**（`generate_sql` vs `correct_sql`），产物形状与校验完全一样。
    换句话说，模型说的话不算数——校正结果也要过静态校验与安全策略。
    """

    name = "llm"

    def __init__(self, generator: Any) -> None:
        self._generator = generator

    def correct(
        self,
        context: GenerateContext,
        prompt: str,
        sql: str,
        error: str,
    ) -> dict[str, Any]:
        return self._generator.generate(context, prompt)


class FixtureCorrector:
    """离线校正器：返回预设产物。

    它存在的理由和 S4 的 `DeterministicGenerator` 一样——**验收命令必须
    在没有 API key 的机器上跑得通**。它也可以返回拒答，用来演练
    「校正失败」这条路径。
    """

    name = "fixture"

    def __init__(self, payload: dict[str, Any] | None) -> None:
        self._payload = dict(payload or {})

    def correct(
        self,
        context: GenerateContext,
        prompt: str,
        sql: str,
        error: str,
    ) -> dict[str, Any]:
        return dict(self._payload)


def build_correct_slots(
    context: GenerateContext,
    sql: str,
    error: str,
) -> dict[str, str]:
    """`correct_sql` 提示词的 7 个槽位。

    槽位声明在 `contracts/prompts/correct_sql.prompt` 的 frontmatter 里，
    少一个就抛 `PromptSlotError`——和生成阶段同一道守卫。
    """
    return {
        "query": context.query,
        "sql": sql,
        "error": error,
        "table_infos": dump_context(table_infos(context)),
        "metric_infos": dump_context(metric_infos(context)),
        "date_info": dump_context(dict(context.date_info)),
        "db_info": dump_context(dict(context.db_info)),
    }


def propose_correction(
    context: GenerateContext,
    sql: str,
    error: ChannelError,
    *,
    corrector: SqlCorrector | None,
    table: ErrorTable | None = None,
) -> CorrectionProposal:
    """校正一次（**最多一次**）。

    三种形态：

        attempted=False —— 没校正（没有校正器，或这个错误不可重试）
        ok=True         —— 校正器给出了一条候选 SQL（还没重新校验）
        ok=False        —— 校正失败，`error` 仍然是原来那个错误
    """
    contract = table or load_error_table()
    policy = contract.correction

    if corrector is None:
        return CorrectionProposal(
            attempted=False,
            ok=False,
            error=error,
            note="没有配置校正器",
        )
    if not error.retryable:
        return CorrectionProposal(
            attempted=False,
            ok=False,
            error=error,
            note=f"{error.code} 不可重试",
        )
    if policy.max_attempts < 1:
        return CorrectionProposal(
            attempted=False,
            ok=False,
            error=error,
            note="契约里没有允许校正",
        )

    slots = build_correct_slots(context, sql, error.message)
    try:
        prompt = render_prompt(policy.prompt, slots)
    except Exception as exc:  # noqa: BLE001 - 提示词契约坏了要说清楚
        return CorrectionProposal(
            attempted=True,
            ok=False,
            error=error,
            note=f"校正提示词渲染失败：{exc}",
        )

    try:
        payload = corrector.correct(context, prompt, sql, error.message)
    except Exception as exc:  # noqa: BLE001 - 校正器炸了不等于校正成功
        return CorrectionProposal(
            attempted=True,
            ok=False,
            error=error,
            note=f"校正器执行失败：{exc}",
        )

    problem = validate_payload(payload, "sql_or_refusal")
    if problem is not None:
        return CorrectionProposal(
            attempted=True,
            ok=False,
            error=error,
            note=f"校正产物不符合契约：{problem}",
        )

    if "refusal" in payload:
        detail = payload["refusal"]
        return CorrectionProposal(
            attempted=True,
            ok=False,
            error=error,
            note=(
                "校正被拒答："
                f"{detail.get('code')} {detail.get('reason')}".strip()
            ),
        )

    return CorrectionProposal(
        attempted=True,
        ok=True,
        sql=str(payload.get("sql") or ""),
        note="校正成功",
    )
