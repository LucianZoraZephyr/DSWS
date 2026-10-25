"""S3 · 8 个确定性节点。

节点顺序与 `contracts/steps.yaml` 完全一致：

    extract_keywords
    recall_column / recall_value / recall_metric
    merge_retrieved_info
    filter_table
    filter_metric
    add_extra_context

四个召回节点全部批量请求：
  - 字段 / 指标：Qdrant `points/search/batch`，所有关键词一次 HTTP
  - 取值：ES `_msearch`，所有关键词一次 HTTP
  - Meta：一次连接批量读 4 张表 + `SELECT VERSION()`

merge 之后只做确定性裁剪，不调用 LLM；主外键、指标 relevant_columns、
formula / expected_value / rejected_values 等契约关键字段不会被过滤掉。
"""

from __future__ import annotations

from dataclasses import replace
from datetime import datetime
from typing import Any

from app.recall.catalog import Catalog
from app.recall.clients import MetaSnapshot, QdrantHit, RecallStack
from app.recall.errors import RecallError
from app.recall.keywords import extract_keywords as extract_keywords_terms
from app.recall.keywords import term_matches
from app.recall.models import (
    EXAMPLES_LIMIT,
    Candidate,
    ColumnContext,
    KeywordHint,
    MetricContext,
    RecallState,
    TableContext,
    ValueHit,
)

# 召回参数：只影响候选数量，不影响确定性。
COLUMN_SEARCH_LIMIT = 12
METRIC_SEARCH_LIMIT = 8
VALUE_SEARCH_LIMIT = 5
MAX_VALUE_HITS = 30

# 最终保留阈值：词法命中的权重都 >= 2.6；hash 向量只有非常接近时才单独保留。
LEXICAL_KEEP = 0.5
VECTOR_KEEP = 0.72

_SOURCE_ORDER = {
    "value_recall": 0,
    "column_recall": 1,
    "metric_dependency": 2,
    "table_key": 3,
}


def _trace(state: RecallState, **updates: Any) -> RecallState:
    return replace(state, trace={**state.trace, **updates})


def _hint_weights(
    state: RecallState,
    *,
    kinds: set[str],
    target_ids: set[str],
) -> dict[str, float]:
    weights: dict[str, float] = {}
    for hint in state.keyword_hints:
        if hint.kind not in kinds:
            continue
        if not target_ids.intersection(hint.target_ids):
            continue
        current = weights.get(hint.term, 0.0)
        weights[hint.term] = max(current, hint.weight)
    return weights


def _ordered_terms(weights: dict[str, float]) -> list[str]:
    return [
        term
        for term, _weight in sorted(
            weights.items(), key=lambda item: (-item[1], item[0])
        )
    ]


def _column_lexical(
    payload: dict[str, Any], state: RecallState
) -> tuple[float, list[str]]:
    """契约别名 + 真实值 + 表名的确定性词法分。"""
    column_id = str(payload.get("id", ""))
    weights = _hint_weights(
        state, kinds={"column", "value"}, target_ids={column_id}
    )

    table_id = str(payload.get("table_id", ""))
    if table_id:
        for term, weight in _hint_weights(
            state, kinds={"table"}, target_ids={table_id}
        ).items():
            weights.setdefault(term, weight)

    # 契约里同一份 name/alias 已经变成 hint；这里补的是 payload 里可能有、
    # 但 hint 没有覆盖的文本（例如以后 Meta 里新增的别名）。
    surfaces = [str(payload.get("name", ""))]
    surfaces.extend(str(alias) for alias in payload.get("aliases", []))
    domain = payload.get("value_domain") or {}
    surfaces.extend(str(value) for value in domain.get("values", []))
    for term in surfaces:
        if not term or term in weights:
            continue
        if term_matches(term, state.query):
            weights[term] = 0.8

    total = round(min(10.0, sum(weights.values())), 6)
    return total, _ordered_terms(weights)


def _metric_lexical(
    payload: dict[str, Any], state: RecallState
) -> tuple[float, list[str]]:
    metric_id = str(payload.get("id", ""))
    weights = _hint_weights(
        state, kinds={"metric"}, target_ids={metric_id}
    )

    surfaces = [str(payload.get("name", "")), str(payload.get("label", ""))]
    surfaces.extend(str(alias) for alias in payload.get("aliases", []))
    for term in surfaces:
        if not term or term in weights:
            continue
        if term_matches(term, state.query):
            weights[term] = 1.0

    total = round(min(10.0, sum(weights.values())), 6)
    return total, _ordered_terms(weights)


def extract_keywords(state: RecallState, catalog: Catalog) -> RecallState:
    result = extract_keywords_terms(state.query, catalog)
    return _trace(
        replace(
            state,
            keywords=result.keywords,
            keyword_hints=result.hints,
            contract_version=catalog.contract_version,
            dataset=dict(catalog.dataset),
        ),
        keyword_count=len(result.keywords),
        keyword_value_terms=len(result.value_terms),
        keyword_date_terms=len(result.date_terms),
    )


# ---------------------------------------------------------------------------
# 4 个召回节点
# ---------------------------------------------------------------------------


def _aggregate_candidates(
    hits: list[QdrantHit],
    state: RecallState,
    *,
    kind: str,
    catalog: Catalog,
) -> list[Candidate]:
    by_id: dict[str, Candidate] = {}
    for hit in hits:
        object_id = str(hit.payload.get("id", ""))
        if not object_id:
            continue
        current = by_id.get(object_id)
        if current is None:
            by_id[object_id] = Candidate(
                id=object_id,
                payload=dict(hit.payload),
                vector_score=hit.score,
                matched_keywords=[hit.keyword],
            )
        else:
            current.vector_score = max(current.vector_score, hit.score)
            if hit.keyword not in current.matched_keywords:
                current.matched_keywords.append(hit.keyword)

    # 词法补偿：即使 Qdrant 完全没召回，契约 alias/value 命中也要成为候选。
    if kind == "column":
        for column in catalog.columns.values():
            payload = catalog.column_payload(column.id)
            if payload is None:
                continue
            lexical, matched = _column_lexical(payload, state)
            if lexical <= 0.0 and column.id not in by_id:
                continue
            candidate = by_id.get(column.id)
            if candidate is None:
                candidate = Candidate(id=column.id, payload=payload)
                by_id[column.id] = candidate
            candidate.lexical_score = max(candidate.lexical_score, lexical)
            for term in matched:
                if term not in candidate.lexical_terms:
                    candidate.lexical_terms.append(term)
                if term not in candidate.matched_keywords:
                    candidate.matched_keywords.append(term)
    else:
        for metric in catalog.metrics.values():
            payload = catalog.metric_payload(metric.name)
            if payload is None:
                continue
            lexical, matched = _metric_lexical(payload, state)
            if lexical <= 0.0 and metric.name not in by_id:
                continue
            candidate = by_id.get(metric.name)
            if candidate is None:
                candidate = Candidate(id=metric.name, payload=payload)
                by_id[metric.name] = candidate
            candidate.lexical_score = max(candidate.lexical_score, lexical)
            for term in matched:
                if term not in candidate.lexical_terms:
                    candidate.lexical_terms.append(term)
                if term not in candidate.matched_keywords:
                    candidate.matched_keywords.append(term)

    def order(item: Candidate) -> tuple[float, int, str]:
        if kind == "column":
            spec = catalog.column(item.id)
            position = spec.order if spec else 10_000
        else:
            spec = catalog.metric(item.id)
            position = spec.order if spec else 10_000
        return (-item.score, position, item.id)

    return sorted(by_id.values(), key=order)


def recall_column(
    state: RecallState, catalog: Catalog, stack: RecallStack
) -> RecallState:
    vectors = stack.embedding.embed_many(state.keywords)
    hits = stack.column_qdrant.batch_search(
        vectors, state.keywords, limit=COLUMN_SEARCH_LIMIT
    )
    candidates = _aggregate_candidates(
        hits, state, kind="column", catalog=catalog
    )
    return _trace(
        replace(state, column_candidates=candidates),
        qdrant_column_roundtrips=stack.column_qdrant.roundtrips,
        column_candidates=len(candidates),
        column_vector_hits=len(hits),
        column_lexical_candidates=sum(
            1 for candidate in candidates if candidate.lexical_score > 0
        ),
    )


def recall_value(state: RecallState, stack: RecallStack) -> RecallState:
    raw_hits = stack.es.msearch(
        state.keywords,
        size=VALUE_SEARCH_LIMIT,
        fallback_query=state.query,
    )
    values = [
        ValueHit(
            id=hit.id,
            value=hit.value,
            column_id=hit.column_id,
            score=round(hit.score, 6),
            matched_keywords=list(hit.matched_keywords),
        )
        for hit in raw_hits
    ]
    values.sort(key=lambda item: (-item.score, item.id))
    values = values[:MAX_VALUE_HITS]
    return _trace(
        replace(state, value_candidates=values, values=list(values)),
        es_roundtrips=stack.es.roundtrips,
        value_candidates=len(values),
    )


def recall_metric(
    state: RecallState, catalog: Catalog, stack: RecallStack
) -> RecallState:
    vectors = stack.embedding.embed_many(state.keywords)
    hits = stack.metric_qdrant.batch_search(
        vectors, state.keywords, limit=METRIC_SEARCH_LIMIT
    )
    candidates = _aggregate_candidates(
        hits, state, kind="metric", catalog=catalog
    )
    return _trace(
        replace(state, metric_candidates=candidates),
        qdrant_metric_roundtrips=stack.metric_qdrant.roundtrips,
        qdrant_roundtrips=(
            stack.column_qdrant.roundtrips + stack.metric_qdrant.roundtrips
        ),
        metric_candidates=len(candidates),
        metric_vector_hits=len(hits),
        metric_lexical_candidates=sum(
            1 for candidate in candidates if candidate.lexical_score > 0
        ),
    )


# ---------------------------------------------------------------------------
# 合并
# ---------------------------------------------------------------------------


def _dedupe(items: list[str]) -> list[str]:
    return list(dict.fromkeys(item for item in items if item))


def _merge_sources(sources: set[str]) -> list[str]:
    return sorted(sources, key=lambda item: (_SOURCE_ORDER.get(item, 9), item))


def _column_context_from_meta(
    column_id: str,
    *,
    snapshot: MetaSnapshot,
    catalog: Catalog,
    candidate: Candidate | None,
    value_hits: list[ValueHit],
    sources: set[str],
    hint_weights: dict[str, float],
) -> ColumnContext:
    row = snapshot.columns.get(column_id)
    if row is None:
        raise RecallError(f"Meta column_info 缺少字段：{column_id}")
    spec = catalog.column(column_id)
    if spec is None:
        raise RecallError(f"契约缺少字段：{column_id}")

    lexical_from_hints = max(hint_weights.values(), default=0.0)
    lexical_score = max(
        candidate.lexical_score if candidate else 0.0,
        lexical_from_hints,
    )
    vector_score = candidate.vector_score if candidate else 0.0
    matched_keywords = list(candidate.lexical_terms) if candidate else []
    for hit in value_hits:
        for keyword in hit.matched_keywords:
            if keyword not in matched_keywords:
                matched_keywords.append(keyword)

    matched_values = [
        hit.value
        for hit in sorted(value_hits, key=lambda item: (-item.score, item.id))
    ]

    score: float | None
    if candidate is not None or matched_values:
        score = round(vector_score + lexical_score, 6)
    else:
        score = None

    return ColumnContext(
        id=column_id,
        name=str(row["name"]),
        type=str(row["type"]),
        role=str(row["role"]),
        description=str(row.get("description") or ""),
        aliases=[str(alias) for alias in row.get("aliases") or []],
        value_domain=dict(row.get("value_domain") or {}),
        examples=list(row.get("examples") or []),
        table_id=str(row["table_id"]),
        nullable=spec.nullable,
        is_primary_key=str(row["role"]) == "primary_key",
        is_foreign_key=str(row["role"]) == "foreign_key",
        references=catalog.references(column_id),
        matched_values=list(dict.fromkeys(matched_values)),
        recall_score=score,
        vector_score=round(vector_score, 6),
        lexical_score=round(lexical_score, 6),
        matched_keywords=_dedupe(matched_keywords),
        sources=_merge_sources(sources),
        examples_truncated=True,
        examples_limit=EXAMPLES_LIMIT,
    )


def _dependency_dict(column: ColumnContext) -> dict[str, Any]:
    return {
        "id": column.id,
        "name": column.name,
        "table_id": column.table_id,
        "type": column.type,
        "role": column.role,
        "description": column.description,
        "aliases": list(column.aliases),
        "value_domain": dict(column.value_domain),
        "examples": list(column.examples),
        "examples_truncated": True,
        "examples_limit": EXAMPLES_LIMIT,
        "matched_values": list(column.matched_values),
    }


def merge_retrieved_info(
    state: RecallState, catalog: Catalog, snapshot: MetaSnapshot
) -> RecallState:
    column_candidates = {candidate.id: candidate for candidate in state.column_candidates}
    metric_candidates = {candidate.id: candidate for candidate in state.metric_candidates}

    value_by_column: dict[str, list[ValueHit]] = {}
    for value in state.value_candidates:
        value_by_column.setdefault(value.column_id, []).append(value)

    metric_rows: dict[str, dict[str, Any]] = {}
    column_ids: list[str] = []
    column_sources: dict[str, set[str]] = {}

    def add_column(column_id: str, source: str) -> None:
        if column_id not in column_ids:
            column_ids.append(column_id)
        column_sources.setdefault(column_id, set()).add(source)

    for candidate in state.column_candidates:
        add_column(candidate.id, "column_recall")
    for value in state.value_candidates:
        add_column(value.column_id, "value_recall")

    for candidate in state.metric_candidates:
        row = snapshot.metrics.get(candidate.id)
        if row is None:
            raise RecallError(f"Meta metric_info 缺少指标：{candidate.id}")
        metric_rows[candidate.id] = row
        for column_id in row.get("relevant_columns") or []:
            add_column(str(column_id), "metric_dependency")

    # 主外键补齐：先补齐候选表自己的 PK/FK，filter_table 再按实际 join 裁剪。
    table_ids: list[str] = []
    for column_id in column_ids:
        row = snapshot.columns.get(column_id)
        if row is None:
            raise RecallError(f"Meta column_info 缺少字段：{column_id}")
        table_id = str(row["table_id"])
        if table_id not in table_ids:
            table_ids.append(table_id)
    table_ids.sort(key=lambda name: (catalog.table(name).order if catalog.table(name) else 10_000, name))

    for table_id in table_ids:
        table = catalog.table(table_id)
        if table is None:
            raise RecallError(f"契约缺少表：{table_id}")
        if table.primary_key:
            add_column(f"{table_id}.{table.primary_key}", "table_key")
        for foreign_key in table.foreign_keys:
            add_column(f"{table_id}.{foreign_key['column']}", "table_key")

    hint_weights_by_column: dict[str, dict[str, float]] = {}
    for column_id in column_ids:
        hint_weights_by_column[column_id] = _hint_weights(
            state,
            kinds={"column", "value"},
            target_ids={column_id},
        )

    columns: dict[str, ColumnContext] = {}
    for column_id in column_ids:
        columns[column_id] = _column_context_from_meta(
            column_id,
            snapshot=snapshot,
            catalog=catalog,
            candidate=column_candidates.get(column_id),
            value_hits=value_by_column.get(column_id, []),
            sources=column_sources.get(column_id, set()),
            hint_weights=hint_weights_by_column[column_id],
        )

    grouped: dict[str, list[ColumnContext]] = {}
    for column in columns.values():
        grouped.setdefault(column.table_id, []).append(column)

    tables: list[TableContext] = []
    for table_id in sorted(
        grouped,
        key=lambda name: (catalog.table(name).order if catalog.table(name) else 10_000, name),
    ):
        spec = catalog.table(table_id)
        row = snapshot.tables.get(table_id)
        if spec is None or row is None:
            raise RecallError(f"Meta table_info / 契约缺少表：{table_id}")
        ordered_columns = sorted(
            grouped[table_id],
            key=lambda column: (
                catalog.column(column.id).order if catalog.column(column.id) else 10_000,
                column.id,
            ),
        )
        tables.append(
            TableContext(
                name=str(row["name"]),
                role=str(row["role"]),
                description=str(row.get("description") or ""),
                grain=str(row.get("grain") or ""),
                primary_key=spec.primary_key,
                foreign_keys=[dict(item) for item in spec.foreign_keys],
                columns=ordered_columns,
            )
        )

    metrics: list[MetricContext] = []
    for candidate in sorted(
        state.metric_candidates,
        key=lambda item: (
            catalog.metric(item.id).order if catalog.metric(item.id) else 10_000,
            item.id,
        ),
    ):
        row = metric_rows.get(candidate.id)
        if row is None:
            continue
        relevant_columns = [str(column_id) for column_id in row.get("relevant_columns") or []]
        dependencies = [
            _dependency_dict(columns[column_id])
            for column_id in relevant_columns
            if column_id in columns
        ]
        metrics.append(
            MetricContext(
                name=str(row["name"]),
                label=str(row.get("label") or row["name"]),
                description=str(row.get("description") or ""),
                formula=str(row.get("formula") or ""),
                relevant_columns=relevant_columns,
                aliases=[str(alias) for alias in row.get("aliases") or []],
                aggregation=str(row.get("aggregation") or ""),
                expected_value=float(row.get("expected_value") or 0.0),
                grain=str(row.get("grain") or ""),
                rejected_values=[
                    dict(item) for item in row.get("rejected_values") or []
                ],
                dependencies=dependencies,
                recall_score=candidate.score,
                matched_keywords=list(candidate.lexical_terms),
                sources=["metric_recall"],
            )
        )

    values = sorted(state.value_candidates, key=lambda item: (-item.score, item.id))
    return _trace(
        replace(state, tables=tables, metrics=metrics, values=values),
        meta_roundtrips=snapshot.roundtrips,
        merged_tables=len(tables),
        merged_columns=len(columns),
        merged_metrics=len(metrics),
        merged_values=len(values),
        metric_dependency_columns=len(
            {
                column_id
                for row in metric_rows.values()
                for column_id in row.get("relevant_columns") or []
            }
        ),
    )


# ---------------------------------------------------------------------------
# 过滤
# ---------------------------------------------------------------------------


def _is_direct_column(column: ColumnContext, *, include_dependency: bool) -> bool:
    if column.matched_values:
        return True
    if column.lexical_score >= LEXICAL_KEEP:
        return True
    if column.vector_score >= VECTOR_KEEP:
        return True
    if include_dependency and "metric_dependency" in column.sources:
        return True
    return False


def filter_table(state: RecallState, catalog: Catalog) -> RecallState:
    """保留有直接证据的表 + join 路径上的主外键，删掉噪声字段。"""
    kept_tables = {
        table.name
        for table in state.tables
        if any(_is_direct_column(column, include_dependency=True) for column in table.columns)
    }

    tables: list[TableContext] = []
    removed_tables: list[str] = []
    removed_columns = 0
    for table in state.tables:
        if table.name not in kept_tables:
            removed_tables.append(table.name)
            removed_columns += len(table.columns)
            continue

        columns: list[ColumnContext] = []
        for column in table.columns:
            keep = False
            if _is_direct_column(column, include_dependency=True):
                keep = True
            elif column.is_primary_key:
                keep = True
            elif column.is_foreign_key and column.references:
                referenced_table = column.references.split(".", 1)[0]
                keep = referenced_table in kept_tables
            if keep:
                columns.append(column)
            else:
                removed_columns += 1

        if not columns:
            removed_tables.append(table.name)
            continue

        foreign_keys = [
            item
            for item in table.foreign_keys
            if f"{table.name}.{item['column']}" in {column.id for column in columns}
            and item["references"].split(".", 1)[0] in kept_tables
        ]
        tables.append(
            TableContext(
                name=table.name,
                role=table.role,
                description=table.description,
                grain=table.grain,
                primary_key=table.primary_key,
                foreign_keys=foreign_keys,
                columns=columns,
            )
        )

    tables.sort(
        key=lambda item: (
            catalog.table(item.name).order if catalog.table(item.name) else 10_000,
            item.name,
        )
    )
    return _trace(
        replace(state, tables=tables),
        filtered_tables=len(tables),
        filter_table_removed_tables=removed_tables,
        filter_table_removed_columns=removed_columns,
    )


def _prune_after_metric_filter(
    state: RecallState,
    catalog: Catalog,
    selected: list[MetricContext],
) -> tuple[list[TableContext], int, list[str]]:
    selected_dependencies = {
        column_id
        for metric in selected
        for column_id in metric.relevant_columns
    }

    base_tables: list[TableContext] = []
    removed_columns = 0
    for table in state.tables:
        columns: list[ColumnContext] = []
        for column in table.columns:
            if _is_direct_column(column, include_dependency=False):
                columns.append(column)
                continue
            if column.id in selected_dependencies:
                columns.append(column)
                continue
            if column.is_primary_key or column.is_foreign_key:
                # 主外键稍后按最终保留表再统一裁剪。
                columns.append(column)
                continue
            removed_columns += 1
        if columns:
            base_tables.append(
                TableContext(
                    name=table.name,
                    role=table.role,
                    description=table.description,
                    grain=table.grain,
                    primary_key=table.primary_key,
                    foreign_keys=list(table.foreign_keys),
                    columns=columns,
                )
            )

    # 只有主键、没有任何直接证据/依赖的表没有意义。
    kept_tables = {
        table.name
        for table in base_tables
        if any(
            _is_direct_column(column, include_dependency=False)
            or column.id in selected_dependencies
            for column in table.columns
        )
    }

    final_tables: list[TableContext] = []
    removed_tables: list[str] = []
    for table in base_tables:
        if table.name not in kept_tables:
            removed_tables.append(table.name)
            removed_columns += len(table.columns)
            continue
        columns = [
            column
            for column in table.columns
            if not column.is_foreign_key
            or (
                column.references
                and column.references.split(".", 1)[0] in kept_tables
            )
        ]
        if not columns:
            removed_tables.append(table.name)
            continue
        column_ids = {column.id for column in columns}
        foreign_keys = [
            item
            for item in table.foreign_keys
            if f"{table.name}.{item['column']}" in column_ids
            and item["references"].split(".", 1)[0] in kept_tables
        ]
        final_tables.append(
            TableContext(
                name=table.name,
                role=table.role,
                description=table.description,
                grain=table.grain,
                primary_key=table.primary_key,
                foreign_keys=foreign_keys,
                columns=columns,
            )
        )

    final_tables.sort(
        key=lambda item: (
            catalog.table(item.name).order if catalog.table(item.name) else 10_000,
            item.name,
        )
    )
    return final_tables, removed_columns, removed_tables


def filter_metric(state: RecallState, catalog: Catalog) -> RecallState:
    """只保留有词法证据的指标；没有证据时最多保留一个高分向量候选。"""
    selected_candidates = [
        candidate
        for candidate in state.metric_candidates
        if candidate.lexical_score >= LEXICAL_KEEP
    ]
    fallback = False
    if not selected_candidates and state.metric_candidates:
        top = max(
            state.metric_candidates,
            key=lambda item: (item.score, item.id),
        )
        if top.score >= 0.8:
            selected_candidates = [top]
            fallback = True

    selected_names = {candidate.id for candidate in selected_candidates}
    selected = [
        metric for metric in state.metrics if metric.name in selected_names
    ]
    selected.sort(
        key=lambda item: (
            catalog.metric(item.name).order if catalog.metric(item.name) else 10_000,
            item.name,
        )
    )

    tables, removed_columns, removed_tables = _prune_after_metric_filter(
        state, catalog, selected
    )
    final_column_ids = {
        column.id for table in tables for column in table.columns
    }
    values = [
        value for value in state.values if value.column_id in final_column_ids
    ]
    return _trace(
        replace(state, metrics=selected, tables=tables, values=values),
        filtered_metrics=len(selected),
        filter_metric_fallback=fallback,
        filter_metric_removed_tables=removed_tables,
        filter_metric_removed_columns=removed_columns,
        filtered_values=len(values),
    )


# ---------------------------------------------------------------------------
# 额外上下文
# ---------------------------------------------------------------------------


def _date_id_to_iso(value: Any) -> str | None:
    text = str(value)
    if len(text) != 8 or not text.isdigit():
        return None
    return f"{text[:4]}-{text[4:6]}-{text[6:8]}"


def add_extra_context(
    state: RecallState,
    catalog: Catalog,
    *,
    database_version: str | None,
    now: datetime | None = None,
) -> RecallState:
    current = now or datetime.now()
    weekday = ("星期一", "星期二", "星期三", "星期四", "星期五", "星期六", "星期日")[
        current.weekday()
    ]
    quarter = f"{current.year}Q{(current.month - 1) // 3 + 1}"

    dataset = state.dataset or dict(catalog.dataset)
    date_range = dataset.get("date_range") or []
    start_id = date_range[0] if len(date_range) > 0 else None
    end_id = date_range[1] if len(date_range) > 1 else None
    extra_context: dict[str, Any] = {
        "current_date": current.date().isoformat(),
        "weekday": weekday,
        "quarter": quarter,
        "timezone": "local",
        "dataset": {
            "name": dataset.get("name"),
            "date_range": {
                "start_id": start_id,
                "end_id": end_id,
                "start": _date_id_to_iso(start_id),
                "end": _date_id_to_iso(end_id),
            },
            "note": dataset.get("note"),
        },
        "database": {
            "dialect": "MySQL",
            "version": database_version,
            "version_source": "SELECT VERSION()" if database_version else "unavailable",
        },
        "examples_policy": {
            "examples_truncated": True,
            "examples_limit": EXAMPLES_LIMIT,
            "note": "Meta examples 是排序后前 10 条；完整命中值见 matched_values。",
        },
    }
    return _trace(
        replace(state, extra_context=extra_context),
        extra_context_ready=True,
        database_version=database_version,
    )
