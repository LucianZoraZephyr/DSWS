"""S3 · 检索编排。

    query
      -> extract_keywords
      -> recall_column / recall_value / recall_metric（全部批量）
      -> merge_retrieved_info（一次 Meta 快照）
      -> filter_table / filter_metric（确定性）
      -> add_extra_context（日期 / 季度 / MySQL 版本）
      -> RecallState

这一层不生成 SQL、不调用 LLM；CLI 只用 `render()` 把状态写成稳定 YAML。
"""

from __future__ import annotations

from dataclasses import replace
from datetime import datetime

from app.recall.catalog import Catalog, default_catalog
from app.recall.clients import RecallStack
from app.recall.errors import RecallError
from app.recall.models import RecallState
from app.recall.nodes import (
    add_extra_context,
    extract_keywords,
    filter_metric,
    filter_table,
    merge_retrieved_info,
    recall_column,
    recall_metric,
    recall_value,
)


def recall(
    query: str,
    *,
    stack: RecallStack | None = None,
    catalog: Catalog | None = None,
    now: datetime | None = None,
) -> RecallState:
    """执行一次完整检索；不调用 LLM，不写任何存储。"""
    cleaned = str(query).strip()
    if not cleaned:
        raise RecallError("query 不能为空")

    owned = stack is None
    runtime = stack or RecallStack.open(catalog=catalog or default_catalog())
    try:
        # 每次 recall 都重新计数：trace 里的往返次数是「这道题」的，不是进程累计。
        runtime.es.roundtrips = 0
        runtime.column_qdrant.roundtrips = 0
        runtime.metric_qdrant.roundtrips = 0
        runtime.meta.roundtrips = 0
        snapshot = runtime.meta.load_snapshot()
        state = RecallState(
            query=cleaned,
            trace={
                "targets": runtime.targets(),
                "contract_version": runtime.catalog.contract_version,
            },
        )
        state = extract_keywords(state, runtime.catalog)
        state = recall_column(state, runtime.catalog, runtime)
        state = recall_value(state, runtime)
        state = recall_metric(state, runtime.catalog, runtime)
        state = merge_retrieved_info(state, runtime.catalog, snapshot)
        state = filter_table(state, runtime.catalog)
        state = filter_metric(state, runtime.catalog)
        state = add_extra_context(
            state,
            runtime.catalog,
            database_version=snapshot.version,
            now=now,
        )
        state = replace(
            state,
            trace={
                **state.trace,
                "meta_roundtrips": snapshot.roundtrips,
                "meta_tables": len(snapshot.tables),
                "meta_columns": len(snapshot.columns),
                "meta_metrics": len(snapshot.metrics),
                "es_roundtrips": runtime.es.roundtrips,
                "qdrant_column_roundtrips": runtime.column_qdrant.roundtrips,
                "qdrant_metric_roundtrips": runtime.metric_qdrant.roundtrips,
                "qdrant_roundtrips": (
                    runtime.column_qdrant.roundtrips
                    + runtime.metric_qdrant.roundtrips
                ),
            },
        )
        return state
    finally:
        if owned:
            runtime.close()
