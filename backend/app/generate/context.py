"""S4 · 生成上下文的构建。

`GenerateContext` 有两个来源，形状完全一样：

    1. `build_offline_context`      —— 只读契约，离线可跑，不需要三存储
    2. `context_from_recall_state` —— 接 S3 的真实召回结果

**为什么离线也能构建**：契约里已经声明了每张表的粒度、每个字段的角色、
每个枚举列的完整值域。这些信息足以裁出一份「够生成 SQL」的上下文，不需要
向量检索来补。向量检索解决的是「候选召回」问题，而 S4 需要的是「已被裁剪
好、可以直接写 SQL」的事实集合。

两个来源产出同一种上下文，是为了让 10 题基线可以在**没有 MySQL / ES /
Qdrant** 的机器上跑完整验收；真实召回的接入只是多一个入口，不是多一套语义。
"""

from __future__ import annotations

import re
from datetime import datetime

from app.recall.catalog import Catalog, default_catalog
from app.recall.keywords import extract_keywords, term_matches
from app.recall.models import RecallState
from app.generate.intent import is_time_series_dimension
from app.generate.models import (
    ColumnLite,
    GenerateContext,
    MetricLite,
    TableLite,
    ValueLite,
)

# 单字别名只在语义非常明确时参与匹配（与 S3 的规则保持一致）。
_SAFE_SINGLE_CHAR_ALIASES = {"省", "年", "月", "日", "天"}


def _compact(text: str) -> str:
    return re.sub(r"\s+", "", str(text).strip().casefold())


def _value_in_query(value: str, query: str) -> bool:
    return _compact(value) in _compact(query)


def _matched_surface(surfaces: tuple[str, ...], query: str) -> str | None:
    """命中的第一个词面；name 优先于 label，label 优先于 alias。"""
    for surface in surfaces:
        if surface and term_matches(surface, query):
            return surface
    return None


def _alias_matched(column, query: str) -> str | None:
    """字段是否被问题提到；返回命中的词面。"""
    if column.name and term_matches(column.name, query):
        return column.name
    for alias in column.aliases:
        compact = _compact(alias)
        if len(compact) == 1 and alias not in _SAFE_SINGLE_CHAR_ALIASES:
            continue
        if alias and term_matches(alias, query):
            return alias
    return None


def _date_id_to_iso(date_id: int | None) -> str | None:
    if not date_id:
        return None
    text = str(date_id)
    return f"{text[0:4]}-{text[4:6]}-{text[6:8]}"


def build_offline_context(
    query: str,
    *,
    catalog: Catalog | None = None,
    now: datetime | None = None,
) -> GenerateContext:
    """从契约 + 问题构建一份离线上下文（不连接任何存储）。"""
    cat = catalog or default_catalog()
    cleaned = str(query).strip()
    if not cleaned:
        raise ValueError("query 不能为空")

    keywords = extract_keywords(cleaned, cat)

    # --- 指标 ------------------------------------------------------------
    matched_metrics: list[tuple[MetricLite, str]] = []
    for spec in cat.metrics.values():
        surfaces = tuple(
            item for item in (spec.name, spec.label, *spec.aliases) if item
        )
        hit = _matched_surface(surfaces, cleaned)
        if hit is None:
            continue
        matched_metrics.append(
            (
                MetricLite(
                    name=spec.name,
                    label=spec.label,
                    description=spec.description,
                    formula=spec.formula,
                    relevant_columns=spec.relevant_columns,
                    aliases=spec.aliases,
                    aggregation=spec.aggregation,
                    expected_value=spec.expected_value,
                    grain=spec.grain,
                    rejected_values=spec.rejected_values,
                ),
                hit,
            )
        )

    # --- 命中取值（时间维度的取值单独处理，不当作过滤值）----------------
    matched_values: list[ValueLite] = []
    value_columns: set[str] = set()
    for value, column_id in cat.value_surfaces():
        column = cat.column(column_id)
        if column is None:
            continue
        if column.table_id == "dim_date":
            # 年 / 月 / 季度的枚举值由时间范围逻辑处理，
            # 否则「2025 年 1 月」会被当成一次 dim_date.year 等值过滤。
            continue
        if not _value_in_query(str(value), cleaned):
            continue
        matched_values.append(ValueLite(value=str(value), column_id=column_id))
        value_columns.add(column_id)

    # --- 分组维度 --------------------------------------------------------
    # 规则与生成器（app.generate.deterministic._pick_dimension）保持一致：
    #   role=dimension、不是过滤列、时间维度还要有「每月 / 按月」这类序列词。
    dimension_columns: list[str] = []
    for column_id, column in cat.columns.items():
        if column.role != "dimension":
            continue
        if column_id in value_columns:
            # 它的枚举值出现在问题里 → 这是过滤条件，不是分组维度。
            continue
        if column.table_id == "dim_date" and not is_time_series_dimension(
            column_id, cleaned
        ):
            continue
        if _alias_matched(column, cleaned) is not None:
            dimension_columns.append(column_id)

    # --- 需要的表与字段 --------------------------------------------------
    fact_table = next(
        (name for name, spec in cat.tables.items() if spec.role == "fact"),
        None,
    )

    # 核心字段：指标口径用到的列 + 过滤值列 + 分组维度列。
    core_column_ids: set[str] = set()
    for metric, _hit in matched_metrics:
        core_column_ids.update(metric.relevant_columns)
    core_column_ids.update(value_columns)
    core_column_ids.update(dimension_columns)

    tables: dict[str, TableLite] = {}
    columns: dict[str, ColumnLite] = {}

    def add_table(name: str) -> None:
        spec = cat.table(name)
        if spec is None or name in tables:
            return
        tables[name] = TableLite(
            name=spec.name,
            role=spec.role,
            grain=spec.grain,
            description=spec.description,
            primary_key=spec.primary_key,
            foreign_keys=spec.foreign_keys,
        )

    def add_column(column_id: str) -> None:
        spec = cat.column(column_id)
        if spec is None or column_id in columns:
            return
        columns[column_id] = ColumnLite(
            id=spec.id,
            name=spec.name,
            table_id=spec.table_id,
            type=spec.type,
            role=spec.role,
            description=spec.description,
            aliases=spec.aliases,
            value_domain=spec.value_domain,
        )

    for column_id in sorted(core_column_ids):
        add_column(column_id)
        table_name = cat.table_of_column(column_id)
        if table_name:
            add_table(table_name)

    # JOIN 需要的键：只拉**真正用到的维表**对应的那一对外键。
    #
    # 这里刻意不是「事实表的全部外键」——那会把 4 张维表全塞进上下文，
    # 于是「列存在」这条校验就形同虚设（上下文里有 dim_product，任何
    # dim_product.* 都会被放行）。只拉用到的维表，校验才有牙齿。
    if fact_table:
        fact_spec = cat.table(fact_table)
        fact_fks = fact_spec.foreign_keys if fact_spec else ()
        dim_tables = {
            cat.table_of_column(column_id)
            for column_id in core_column_ids
            if cat.table_of_column(column_id)
            and cat.table_of_column(column_id) != fact_table
        }
        for item in fact_fks:
            target = str(item["references"])
            target_table = target.split(".", 1)[0]
            if target_table not in dim_tables:
                continue
            source = f"{fact_table}.{item['column']}"
            add_column(source)
            add_column(target)
            add_table(target_table)

    # 字段召回命中的列：只保留其所属表已在上下文里的那些，
    # 避免出现「列在上下文里、但它的表不在」这种自相矛盾的裁剪。
    for hint in keywords.hints:
        if hint.kind != "column":
            continue
        for column_id in hint.target_ids:
            if column_id not in cat.columns:
                continue
            table_name = cat.table_of_column(column_id)
            if table_name in tables:
                add_column(column_id)

    # 时间过滤写在事实表的 date_id 上，不需要连 dim_date。
    if fact_table:
        date_column = f"{fact_table}.date_id"
        if cat.column(date_column) is not None:
            add_column(date_column)
            add_table(fact_table)

    dataset = dict(cat.dataset)
    date_range = dataset.get("date_range") or []
    start_id = date_range[0] if len(date_range) > 0 else None
    end_id = date_range[1] if len(date_range) > 1 else None

    current = now or datetime.now()
    date_info = {
        "current_date": current.date().isoformat(),
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
    }
    db_info = {
        "dialect": "MySQL",
        "version": None,
        "version_source": "unavailable",
    }

    return GenerateContext(
        query=cleaned,
        tables=tuple(tables[name] for name in sorted(tables)),
        columns=tuple(columns[cid] for cid in sorted(columns)),
        metrics=tuple(metric for metric, _hit in matched_metrics),
        values=tuple(matched_values),
        dataset=dataset,
        date_info=date_info,
        db_info=db_info,
    )


def context_from_recall_state(state: RecallState) -> GenerateContext:
    """把 S3 的 `RecallState` 适配成 `GenerateContext`。

    字段顺序与 S3 一致：表按契约顺序，字段按表内契约顺序。
    """
    tables: list[TableLite] = []
    columns: list[ColumnLite] = []
    for table in state.tables:
        tables.append(
            TableLite(
                name=table.name,
                role=table.role,
                grain=table.grain,
                description=table.description,
                primary_key=table.primary_key,
                foreign_keys=tuple(
                    {"column": item["column"], "references": item["references"]}
                    for item in table.foreign_keys
                ),
            )
        )
        for column in table.columns:
            columns.append(
                ColumnLite(
                    id=column.id,
                    name=column.name,
                    table_id=column.table_id,
                    type=column.type,
                    role=column.role,
                    description=column.description,
                    aliases=tuple(column.aliases),
                    value_domain=dict(column.value_domain),
                )
            )

    metrics = tuple(
        MetricLite(
            name=metric.name,
            label=metric.label,
            description=metric.description,
            formula=metric.formula,
            relevant_columns=tuple(metric.relevant_columns),
            aliases=tuple(metric.aliases),
            aggregation=metric.aggregation,
            expected_value=metric.expected_value,
            grain=metric.grain,
            rejected_values=tuple(metric.rejected_values),
        )
        for metric in state.metrics
    )

    values = tuple(
        ValueLite(value=value.value, column_id=value.column_id)
        for value in state.values
    )

    # 数据集的时间边界是**数据集级**的事实，不是「这次召回碰巧带回来的字段」。
    # 事实表的 date_id 因此始终可用：缺失时从契约补上。
    # 这样「接真实召回」与「离线裁剪」两条链路给出的上下文一致，
    # 生成的 SQL 也就一致（有测试钉住这条不变式）。
    for table in tables:
        if table.role != "fact":
            continue
        date_id = f"{table.name}.date_id"
        if any(column.id == date_id for column in columns):
            continue
        spec = default_catalog().column(date_id)
        if spec is None:
            continue
        columns.append(
            ColumnLite(
                id=spec.id,
                name=spec.name,
                table_id=spec.table_id,
                type=spec.type,
                role=spec.role,
                description=spec.description,
                aliases=spec.aliases,
                value_domain=spec.value_domain,
            )
        )

    dataset = dict(state.dataset)
    date_info = dict(state.extra_context.get("dataset") or {})
    db_info = dict(state.extra_context.get("database") or {})
    full_date_info = {
        "current_date": state.extra_context.get("current_date"),
        "dataset": date_info,
    }

    return GenerateContext(
        query=state.query,
        tables=tuple(tables),
        columns=tuple(columns),
        metrics=metrics,
        values=values,
        dataset=dataset,
        date_info=full_date_info,
        db_info=db_info,
    )
