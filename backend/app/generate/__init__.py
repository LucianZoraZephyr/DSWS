"""S4 · 生成层。

    context.py       上下文构建（离线契约裁剪 / 接 S3 召回）
    prompts.py       提示词契约的加载与槽位校验
    provider.py      生成器接口 + 确定性实现 + 可选 LLM 实现
    deterministic.py 确定性 SQL 生成
    guard.py         sqlglot AST 静态校验（5 条硬规则）
    output_schema.py 输出形状的契约校验
    pipeline.py      编排
"""

from app.generate.guard import check_sql
from app.generate.models import (
    ColumnLite,
    GenerateContext,
    GenerationResult,
    GuardReport,
    GuardViolation,
    MetricLite,
    Refusal,
    TableLite,
    ValueLite,
)
from app.generate.pipeline import generate, generate_with_recall
from app.generate.provider import (
    DeterministicGenerator,
    HttpLlmGenerator,
    build_slots,
)
from app.generate.prompts import (
    PromptError,
    PromptSlotError,
    get_prompt,
    load_prompts,
    render_prompt,
)

__all__ = [
    "ColumnLite",
    "DeterministicGenerator",
    "GenerateContext",
    "GenerationResult",
    "GuardReport",
    "GuardViolation",
    "HttpLlmGenerator",
    "MetricLite",
    "PromptError",
    "PromptSlotError",
    "Refusal",
    "TableLite",
    "ValueLite",
    "build_slots",
    "check_sql",
    "generate",
    "generate_with_recall",
    "get_prompt",
    "load_prompts",
    "render_prompt",
]
