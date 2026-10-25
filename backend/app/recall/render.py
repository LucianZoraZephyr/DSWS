"""S3 · 把 `RecallState` 渲染成稳定 YAML。

YAML 是「LLM 将看到的上下文」，不是内部调试 dump：

    - 字段 / 指标 / 表的 name、type、role、description、aliases、
      value_domain、grain、formula、expected_value、relevant_columns、
      rejected_values 全部保留，不截断。
    - examples 来自 Meta，按 S2 的显式策略只保留前 10 条；因此每个字段都
      写 `examples_truncated: true` 与 `examples_limit: 10`，完整命中值放在
      `matched_values`，绝不把 examples 伪装成全集。
    - 根节点再用 `examples_policy` 重复一次策略，避免下游只读根节点时误解。

所有 mapping 的 key 顺序固定为契约字段顺序；列表在节点层已经排序。
"""

from __future__ import annotations

from typing import Any

import yaml

from app.recall.models import (
    EXAMPLES_LIMIT,
    ColumnContext,
    MetricContext,
    RecallState,
    TableContext,
)


def _score(value: float | None) -> float | None:
    return round(value, 6) if value is not None else None


def column_to_dict(column: ColumnContext) -> dict[str, Any]:
    return {
        "id": column.id,
        "name": column.name,
        "table_id": column.table_id,
        "type": column.type,
        "role": column.role,
        "nullable": column.nullable,
        "description": column.description,
        "aliases": list(column.aliases),
        "value_domain": dict(column.value_domain),
        "examples": list(column.examples),
        "examples_truncated": column.examples_truncated,
        "examples_limit": column.examples_limit,
        "is_primary_key": column.is_primary_key,
        "is_foreign_key": column.is_foreign_key,
        "references": column.references,
        "matched_values": list(column.matched_values),
        "recall_score": _score(column.recall_score),
        "vector_score": _score(column.vector_score),
        "lexical_score": _score(column.lexical_score),
        "matched_keywords": list(column.matched_keywords),
        "sources": list(column.sources),
    }


def table_to_dict(table: TableContext) -> dict[str, Any]:
    return {
        "name": table.name,
        "role": table.role,
        "description": table.description,
        "grain": table.grain,
        "primary_key": table.primary_key,
        "foreign_keys": [
            {"column": item["column"], "references": item["references"]}
            for item in table.foreign_keys
        ],
        "measures": [dict(item) for item in table.measures],
        "columns": [column_to_dict(column) for column in table.columns],
    }


def metric_to_dict(metric: MetricContext) -> dict[str, Any]:
    return {
        "name": metric.name,
        "label": metric.label,
        "description": metric.description,
        "formula": metric.formula,
        "relevant_columns": list(metric.relevant_columns),
        "aliases": list(metric.aliases),
        "aggregation": metric.aggregation,
        "expected_value": metric.expected_value,
        "grain": metric.grain,
        "rejected_values": [dict(item) for item in metric.rejected_values],
        "dependencies": [dict(item) for item in metric.dependencies],
        "recall_score": _score(metric.recall_score),
        "matched_keywords": list(metric.matched_keywords),
        "sources": list(metric.sources),
    }


def state_to_dict(state: RecallState) -> dict[str, Any]:
    trace = state.trace
    payload: dict[str, Any] = {
        "query": state.query,
        "contract_version": state.contract_version,
        "dataset": dict(state.dataset),
        "keywords": list(state.keywords),
        "extra_context": dict(state.extra_context),
        "tables": [table_to_dict(table) for table in state.tables],
        "metrics": [metric_to_dict(metric) for metric in state.metrics],
        "values": [
            {
                "id": value.id,
                "value": value.value,
                "column_id": value.column_id,
                "score": _score(value.score),
                "matched_keywords": list(value.matched_keywords),
            }
            for value in state.values
        ],
        "retrieval": {
            "targets": dict(trace.get("targets") or {}),
            "es_roundtrips": trace.get("es_roundtrips"),
            "qdrant_roundtrips": trace.get("qdrant_roundtrips"),
            "qdrant_column_roundtrips": trace.get("qdrant_column_roundtrips"),
            "qdrant_metric_roundtrips": trace.get("qdrant_metric_roundtrips"),
            "meta_roundtrips": trace.get("meta_roundtrips"),
            "keywords": trace.get("keyword_count"),
            "candidates": {
                "columns": trace.get("column_candidates"),
                "values": trace.get("value_candidates"),
                "metrics": trace.get("metric_candidates"),
            },
            "final": {
                "tables": len(state.tables),
                "columns": len(state.columns),
                "metrics": len(state.metrics),
                "values": len(state.values),
            },
            "filters": {
                "table_removed_tables": trace.get("filter_table_removed_tables"),
                "table_removed_columns": trace.get("filter_table_removed_columns"),
                "metric_removed_tables": trace.get("filter_metric_removed_tables"),
                "metric_removed_columns": trace.get("filter_metric_removed_columns"),
                "metric_fallback": trace.get("filter_metric_fallback"),
            },
        },
        "examples_policy": {
            "examples_truncated": True,
            "examples_limit": EXAMPLES_LIMIT,
            "note": "examples 是 Meta 中排序后的前 10 条；完整命中值见 matched_values。",
        },
    }
    return payload


def render(state: RecallState) -> str:
    """稳定 YAML：同一状态在任何进程中都得到相同字符串。"""
    return yaml.safe_dump(
        state_to_dict(state),
        allow_unicode=True,
        sort_keys=False,
        default_flow_style=False,
        width=1000,
    )
