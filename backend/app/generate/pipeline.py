"""S4 · 生成编排。

    query
      -> build_offline_context（契约裁剪）   [或 context_from_recall_state]
      -> build_slots（7 个槽位）
      -> render_prompt（缺槽位直接抛异常）
      -> generator.generate（确定性 / 可选 LLM）
      -> 输出形状校验（sql_or_refusal）
      -> 静态校验（sqlglot AST 的 5 条硬规则）
      -> GenerationResult

两道校验都是**运行时**的，顺序也是刻意的：先看形状对不对，再看内容合不合法。
形状不对的产物根本进不到静态校验那一步——这正是审计根因 B 说的那句
「失败没有可表达的通道」的解法：失败只有一种表达方式，就是 `refusal`。

**没有静默降级**：上下文构建失败、槽位缺失、模型返回非法 JSON，全部都会
显式变成拒答或异常，绝不悄悄换成「空结果」或「伪造的一行数据」。
"""

from __future__ import annotations

from datetime import datetime

from app.generate.context import build_offline_context, context_from_recall_state
from app.generate.guard import check_sql
from app.generate.models import (
    GenerateContext,
    GenerationResult,
    GuardReport,
    Refusal,
)
from app.generate.output_schema import INVALID_OUTPUT, validate_payload
from app.generate.provider import DeterministicGenerator, SqlGenerator, build_slots
from app.generate.prompts import render_prompt

GENERATE_PROMPT = "generate_sql"


def generate(
    query: str,
    *,
    context: GenerateContext | None = None,
    generator: SqlGenerator | None = None,
    now: datetime | None = None,
) -> GenerationResult:
    """生成一条 SQL 或一个拒答。"""
    cleaned = str(query).strip()
    if not cleaned:
        raise ValueError("query 不能为空")

    ctx = context or build_offline_context(cleaned, now=now)
    engine = generator or DeterministicGenerator()

    # 槽位渲染：少一个槽就抛 PromptSlotError（绝不静默补空串）。
    slots = build_slots(ctx)
    prompt = render_prompt(GENERATE_PROMPT, slots)

    try:
        payload = engine.generate(ctx, prompt)
    except ValueError as exc:
        # 生成器没能给出契约形状的产物（例如模型返回了非 JSON）。
        refusal = Refusal(INVALID_OUTPUT, str(exc))
        return GenerationResult(
            query=cleaned,
            outcome="refusal",
            refusal=refusal,
            guard=GuardReport(False),
            prompt=prompt,
            slots=slots,
        )

    problem = validate_payload(payload, "sql_or_refusal")
    if problem is not None:
        refusal = Refusal(INVALID_OUTPUT, f"输出不符合 sql_or_refusal 契约：{problem}")
        return GenerationResult(
            query=cleaned,
            outcome="refusal",
            refusal=refusal,
            guard=GuardReport(False),
            prompt=prompt,
            slots=slots,
        )

    if "refusal" in payload:
        detail = payload["refusal"]
        refusal = Refusal(
            code=str(detail.get("code") or INVALID_OUTPUT),
            reason=str(detail.get("reason") or ""),
        )
        return GenerationResult(
            query=cleaned,
            outcome="refusal",
            refusal=refusal,
            guard=GuardReport(True),
            prompt=prompt,
            slots=slots,
        )

    sql = str(payload.get("sql") or "")
    report = check_sql(sql, ctx, cleaned)

    if not report.ok:
        first = report.violations[0]
        refusal = Refusal(
            code=first.code,
            reason=f"{first.message}{('：' + first.detail) if first.detail else ''}",
        )
        return GenerationResult(
            query=cleaned,
            outcome="refusal",
            refusal=refusal,
            guard=report,
            prompt=prompt,
            slots=slots,
        )

    return GenerationResult(
        query=cleaned,
        outcome="sql",
        sql=sql,
        guard=report,
        prompt=prompt,
        slots=slots,
    )


def generate_with_recall(
    query: str,
    *,
    generator: SqlGenerator | None = None,
    now: datetime | None = None,
) -> GenerationResult:
    """接 S3 的真实召回再生成。

    需要 MySQL / ES / Qdrant 全部就绪；服务不可用时**直接报错**，不会悄悄
    退回离线上下文——那会让「真实链路」和「离线链路」的验收混为一谈。
    """
    from app.recall.pipeline import recall

    state = recall(query, now=now)
    context = context_from_recall_state(state)
    return generate(query, context=context, generator=generator, now=now)
