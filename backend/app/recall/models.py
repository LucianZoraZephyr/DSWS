"""S3 · 检索层的中间状态。

这里刻意不引入任何图框架：4 个召回节点 + merge/filter/add_extra_context
都是普通的确定性函数，输入/输出都能直接单测。`RecallState` 只是把这些
片段收在一起，方便 CLI 和测试观察。

所有列表都有确定顺序：要么按契约顺序，要么按 (-score, id) 排序，
绝不依赖 set 的迭代顺序。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

# Meta 的 examples 是 S2 的显式截断策略；S3 只读取，不把它当全集。
EXAMPLES_LIMIT = 10


@dataclass
class KeywordHint:
    """关键词抽取阶段产生的确定性提示。

    `kind` 只允许 column / metric / value / table。召回节点用它给
    Qdrant 候选补词法分，也用它保证「契约里明确写出的别名」不会因为
    hash 向量太弱而漏召。
    """

    term: str
    kind: str
    target_ids: list[str]
    weight: float
    source: str


@dataclass
class Candidate:
    """Qdrant 召回的一个字段/指标候选（已按 object id 去重）。"""

    id: str
    payload: dict[str, Any]
    vector_score: float = 0.0
    lexical_score: float = 0.0
    matched_keywords: list[str] = field(default_factory=list)
    lexical_terms: list[str] = field(default_factory=list)

    @property
    def score(self) -> float:
        """向量分 + 确定性词法补偿分。

        hash-ngram 向量只保证「字面重叠」，同义改写召回很弱；词法分把
        契约里的 name/alias 与 query 的直接命中补回来。两项都固定，
        所以排序可复现。
        """
        return round(self.vector_score + self.lexical_score, 6)


@dataclass
class ValueHit:
    """ES 取值召回的一条真实值。"""

    id: str
    value: str
    column_id: str
    score: float = 0.0
    matched_keywords: list[str] = field(default_factory=list)


@dataclass
class ColumnContext:
    """合并后、准备写进 YAML 的字段上下文。"""

    id: str
    name: str
    type: str
    role: str
    description: str
    aliases: list[str]
    value_domain: dict[str, Any]
    examples: list[Any]
    table_id: str
    nullable: bool | None
    is_primary_key: bool
    is_foreign_key: bool
    references: str | None
    matched_values: list[str] = field(default_factory=list)
    recall_score: float | None = None
    vector_score: float = 0.0
    lexical_score: float = 0.0
    matched_keywords: list[str] = field(default_factory=list)
    sources: list[str] = field(default_factory=list)
    examples_truncated: bool = True
    examples_limit: int = EXAMPLES_LIMIT

    @property
    def is_key(self) -> bool:
        return self.is_primary_key or self.is_foreign_key


@dataclass
class TableContext:
    """按表组织的字段上下文。"""

    name: str
    role: str
    description: str
    grain: str
    primary_key: str | None
    foreign_keys: list[dict[str, str]] = field(default_factory=list)
    measures: list[dict[str, Any]] = field(default_factory=list)
    columns: list[ColumnContext] = field(default_factory=list)


@dataclass
class MetricContext:
    """按指标组织的口径上下文。"""

    name: str
    label: str
    description: str
    formula: str
    relevant_columns: list[str]
    aliases: list[str]
    aggregation: str
    expected_value: float
    grain: str
    rejected_values: list[dict[str, Any]] = field(default_factory=list)
    dependencies: list[dict[str, Any]] = field(default_factory=list)
    recall_score: float | None = None
    matched_keywords: list[str] = field(default_factory=list)
    sources: list[str] = field(default_factory=list)

    @property
    def id(self) -> str:
        return self.name


@dataclass
class RecallState:
    """一次检索的完整状态；`trace` 只放统计，不放耗时（耗时不稳定）。"""

    query: str
    keywords: list[str] = field(default_factory=list)
    keyword_hints: list[KeywordHint] = field(default_factory=list)
    column_candidates: list[Candidate] = field(default_factory=list)
    value_candidates: list[ValueHit] = field(default_factory=list)
    metric_candidates: list[Candidate] = field(default_factory=list)
    tables: list[TableContext] = field(default_factory=list)
    metrics: list[MetricContext] = field(default_factory=list)
    values: list[ValueHit] = field(default_factory=list)
    extra_context: dict[str, Any] = field(default_factory=dict)
    dataset: dict[str, Any] = field(default_factory=dict)
    trace: dict[str, Any] = field(default_factory=dict)
    contract_version: int = 0

    @property
    def columns(self) -> list[ColumnContext]:
        return [column for table in self.tables for column in table.columns]

    @property
    def column_ids(self) -> list[str]:
        return [column.id for column in self.columns]

    @property
    def table_names(self) -> list[str]:
        return [table.name for table in self.tables]

    @property
    def metric_names(self) -> list[str]:
        return [metric.name for metric in self.metrics]

    @property
    def value_strings(self) -> list[str]:
        return [value.value for value in self.values]
