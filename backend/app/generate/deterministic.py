"""S4 · 确定性 SQL 生成器。

它是 `generate_sql` 提示词的**离线可复现实现**：不调用任何模型，只按契约
把「召回上下文」翻译成一条 SQL，或者给出一个有名字的拒答。

为什么要有它：
    路线图要求 S4 的验收是「10 题基线 + `make baseline` 表格」。表格要稳定
    可复现，就不能每次都问一次模型。同一条问题必须永远得到同一条 SQL——
    否则基线无法当验收标准用。

    所以 S4 的产物是**两个东西**：提示词契约（给未来的真实 LLM 用）与确定性
    实现（给验收用）。两者产出同一个形状（`sql_or_refusal`），并且都要过
    同一套静态校验。

生成规则全部来自契约，不来自模型：
    - 指标表达式 = 契约里的 `formula`（它本来就是可执行 SQL）
    - 表 / 字段 = 契约里声明的表和字段
    - 维度分组 = 契约里 role=dimension 且问题明确按其分组的字段
    - 过滤 = 契约声明的真实枚举值 + 数据集的时间边界
    - 排名 = 问题里有排名意图时必须有 ORDER BY（`app.generate.intent`）
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date

from app.generate.intent import (
    has_ranking_intent,
    is_time_series_dimension,
    month_in_range,
    parse_time_scope,
    ranking_direction,
    ranking_limit,
    unsupported_concept,
    year_in_range,
)
from app.generate.models import (
    ColumnLite,
    GenerateContext,
    MetricLite,
    Refusal,
)

# 明细查询最多投影几个字段，避免把一张宽表整列倒出来。
_DETAIL_COLUMN_LIMIT = 8


def _quote_literal(value: str) -> str:
    """SQL 字符串字面量；单引号按 SQL 标准翻倍。"""
    return "'" + str(value).replace("'", "''") + "'"


def _quote_identifier(name: str) -> str:
    """SQL 标识符。中文别名必须加反引号，否则 MySQL 解析不稳定。"""
    return "`" + str(name).replace("`", "``") + "`"


def _days_in_month(year: int, month: int) -> int:
    if month == 12:
        return 31
    return (date(year, month + 1, 1) - date(year, month, 1)).days


def _quarter_bounds(year: int, quarter: int) -> tuple[int, int]:
    start_month = (quarter - 1) * 3 + 1
    end_month = start_month + 2
    start = year * 10000 + start_month * 100 + 1
    end = year * 10000 + end_month * 100 + _days_in_month(year, end_month)
    return start, end


@dataclass(frozen=True)
class _Plan:
    """生成前的中间决策。"""

    fact_table: str
    metric: MetricLite | None
    metric_alias: str | None
    dimension: ColumnLite | None
    dimension_alias: str | None
    value_filters: tuple[tuple[str, str], ...]
    date_column: str | None
    date_start: int | None
    date_end: int | None
    joins: tuple[tuple[str, str, str], ...]
    ranking: bool
    limit: int | None
    direction: str


def _display_alias(
    surfaces: tuple[str, ...],
    aliases: tuple[str, ...],
    fallback: str,
    query: str,
) -> str:
    """结果列的展示名。

    优先用**问题里用到的那个词**（用户问「销售额」就列名「销售额」）；单字
    词太短，退回契约里的第一个别名；再退回字段名。
    """
    from app.recall.keywords import term_matches

    matched = [item for item in surfaces if item and term_matches(item, query)]
    for item in matched:
        if len(item) >= 2:
            return item
    if aliases:
        return aliases[0]
    return fallback


def _metric_alias(metric: MetricLite, query: str) -> str:
    surfaces = tuple(
        item for item in (metric.name, metric.label, *metric.aliases) if item
    )
    matched = _display_alias(surfaces, metric.aliases, metric.name, query)
    return matched


def _column_alias(column: ColumnLite, query: str) -> str:
    surfaces = tuple(item for item in (column.name, *column.aliases) if item)
    return _display_alias(surfaces, column.aliases, column.name, query)


def _fact_table(context: GenerateContext) -> str | None:
    """事实表：优先取指标公式里出现的表，其次取上下文里唯一的度量表。"""
    for metric in context.metrics:
        for column_id in metric.relevant_columns:
            table = column_id.split(".", 1)[0]
            if table in context.table_names():
                return table
    measure_tables = [
        column.table_id
        for column in context.columns
        if column.role == "measure" and column.table_id in context.table_names()
    ]
    unique = list(dict.fromkeys(measure_tables))
    if len(unique) == 1:
        return unique[0]
    return None


def _value_columns(context: GenerateContext) -> set[str]:
    return {value.column_id for value in context.values}


def _pick_dimension(
    context: GenerateContext,
    filter_columns: set[str],
) -> ColumnLite | None:
    """分组维度：role=dimension、不是过滤列、且问题确实按它分组。"""
    candidates: list[ColumnLite] = []
    for column in context.columns:
        if column.role != "dimension":
            continue
        if column.id in filter_columns:
            continue
        if column.is_time_dimension and not is_time_series_dimension(
            column.id, context.query
        ):
            continue
        candidates.append(column)
    if not candidates:
        return None
    # 多个候选时按字段 id 取最小的一个：这样生成结果只取决于**集合**，
    # 不取决于上下文的字段顺序。接真实召回与离线裁剪两条链路因此能给出
    # 同一条 SQL（项目一贯的要求：不依赖 set / 迭代序）。
    return min(candidates, key=lambda column: column.id)


def _join_for(
    context: GenerateContext,
    fact_table: str,
    dim_table: str,
) -> tuple[str, str, str] | None:
    """找到事实表指向该维表的外键。返回 (维表, 事实表外键, 维表主键)。"""
    fact = context.table(fact_table)
    if fact is None:
        return None
    for item in fact.foreign_keys:
        references = str(item.get("references") or "")
        if references.startswith(dim_table + "."):
            return (
                dim_table,
                f"{fact_table}.{item['column']}",
                references,
            )
    return None


def _date_bounds(
    context: GenerateContext,
) -> tuple[int | None, int | None, str | None]:
    """算出 WHERE 里要用的 date_id 区间，以及越界时的拒答原因。"""
    date_range = context.dataset.get("date_range") or []
    if len(date_range) < 2:
        return None, None, None
    dataset_start = int(date_range[0])
    dataset_end = int(date_range[1])

    scope = parse_time_scope(context.query)

    if scope.date_id is not None:
        if not dataset_start <= scope.date_id <= dataset_end:
            return None, None, (
                f"数据只覆盖 {dataset_start} ~ {dataset_end}，"
                f"问题里的 {scope.date_id} 超出范围。"
            )
        return scope.date_id, scope.date_id, None

    if scope.year is not None and not year_in_range(
        scope.year, dataset_start, dataset_end
    ):
        return None, None, (
            f"数据只覆盖 {dataset_start} ~ {dataset_end}，"
            f"问题里的 {scope.year} 年超出数据边界。"
        )

    if scope.month is not None and not month_in_range(
        scope.month, dataset_start, dataset_end
    ):
        return None, None, (
            f"数据只覆盖 {dataset_start} ~ {dataset_end}，"
            f"问题里的 {scope.month} 月超出数据边界。"
        )

    start, end = dataset_start, dataset_end
    if scope.year is not None:
        if scope.quarter is not None:
            quarter_start, quarter_end = _quarter_bounds(scope.year, scope.quarter)
            start, end = quarter_start, quarter_end
        elif scope.month is not None:
            month_start = scope.year * 10000 + scope.month * 100 + 1
            month_end = (
                scope.year * 10000
                + scope.month * 100
                + _days_in_month(scope.year, scope.month)
            )
            start, end = month_start, month_end
        else:
            start, end = scope.year * 10000 + 101, scope.year * 10000 + 1231

    start = max(start, dataset_start)
    end = min(end, dataset_end)
    if start > end:
        return None, None, (
            f"问题要求的时间区间与数据范围 {dataset_start} ~ {dataset_end} 无交集。"
        )
    return start, end, None


def _plan(context: GenerateContext) -> _Plan | Refusal:
    """把上下文翻译成生成计划。返回 Refusal 表示应该拒答。"""
    query = context.query

    unsupported = unsupported_concept(query)
    if unsupported is not None:
        term, reason = unsupported
        return Refusal(
            "UNSUPPORTED_INTENT",
            f"契约里没有「{term}」这个概念：{reason}",
        )

    metric = context.metrics[0] if context.metrics else None

    filter_columns = _value_columns(context)
    dimension = _pick_dimension(context, filter_columns)

    ranking = has_ranking_intent(query)
    if ranking and metric is None:
        return Refusal(
            "INSUFFICIENT_CONTEXT",
            "问题有排名意图，但上下文里没有可用指标，无法确定按什么排序。",
        )
    if ranking and dimension is None:
        return Refusal(
            "INSUFFICIENT_CONTEXT",
            "问题有排名意图，但上下文里没有可分组的维度，无法确定按什么排名。",
        )
    if metric is None and dimension is None and not filter_columns:
        return Refusal(
            "INSUFFICIENT_CONTEXT",
            "上下文里既没有指标也没有可用的字段，无法判断该查什么。",
        )

    fact_table = _fact_table(context)
    if fact_table is None:
        return Refusal(
            "INSUFFICIENT_CONTEXT",
            "上下文里没有事实表，无法生成查询。",
        )

    date_start, date_end, out_of_range = _date_bounds(context)
    if out_of_range is not None:
        return Refusal("OUT_OF_RANGE", out_of_range)

    date_column = f"{fact_table}.date_id"
    # 日期过滤只在上下文**确实提供了**该字段时才写。
    # 上下文是「生成器被允许用的字段集合」——引用集合外的字段会被静态校验
    # 判成 SQL_UNKNOWN_COLUMN，那条 SQL 就白生成了。
    if date_column not in {column.id for column in context.columns}:
        date_column = None

    # 需要 JOIN 的维表：分组维度所在表 + 过滤值所在表。
    # 按表名排序，保证 JOIN 顺序不随上下文的字段顺序漂移。
    dim_tables: set[str] = set()
    for column in context.columns:
        if column.id in filter_columns or (dimension and column.id == dimension.id):
            if column.table_id != fact_table and column.table_id in context.table_names():
                dim_tables.add(column.table_id)
    if dimension is not None and dimension.table_id != fact_table:
        dim_tables.add(dimension.table_id)
    ordered_dim_tables = sorted(dim_tables)

    joins: list[tuple[str, str, str]] = []
    for table_name in ordered_dim_tables:
        join = _join_for(context, fact_table, table_name)
        if join is None:
            return Refusal(
                "UNKNOWN_TABLE",
                f"上下文里的 {table_name} 与事实表 {fact_table} 之间没有可用的外键。",
            )
        if join not in joins:
            joins.append(join)

    value_filters: list[tuple[str, str]] = []
    for value in context.values:
        value_filters.append((value.column_id, value.value))

    metric_alias = _metric_alias(metric, query) if metric is not None else None
    dimension_alias = (
        _column_alias(dimension, query) if dimension is not None else None
    )

    limit = ranking_limit(query) if ranking else None

    return _Plan(
        fact_table=fact_table,
        metric=metric,
        metric_alias=metric_alias,
        dimension=dimension,
        dimension_alias=dimension_alias,
        value_filters=tuple(value_filters),
        date_column=date_column,
        date_start=date_start if date_column else None,
        date_end=date_end if date_column else None,
        joins=tuple(joins),
        ranking=ranking,
        limit=limit,
        direction=ranking_direction(query),
    )


def _detail_columns(context: GenerateContext, fact_table: str) -> list[ColumnLite]:
    """明细查询可投影的字段：上下文里事实表的非键字段，按字段 id 排序。"""
    candidates = sorted(
        (
            column
            for column in context.columns
            if column.table_id == fact_table and column.role != "foreign_key"
        ),
        key=lambda column: column.id,
    )
    return candidates[:_DETAIL_COLUMN_LIMIT]


def build_sql(context: GenerateContext, plan: _Plan) -> str:
    """按计划拼出 SQL。"""
    projections: list[str] = []
    group_by: list[str] = []

    if plan.dimension is not None:
        projections.append(
            f"{plan.dimension.id} AS {_quote_identifier(plan.dimension_alias or plan.dimension.name)}"
        )
        group_by.append(plan.dimension.id)

    if plan.metric is not None and plan.metric_alias:
        projections.append(
            f"{plan.metric.formula} AS {_quote_identifier(plan.metric_alias)}"
        )
    elif plan.metric is None:
        for column in _detail_columns(context, plan.fact_table):
            projections.append(
                f"{column.id} AS {_quote_identifier(_column_alias(column, context.query))}"
            )

    if not projections:
        projections.append(f"{plan.fact_table}.order_id")

    lines: list[str] = []
    lines.append("SELECT " + ", ".join(projections))
    lines.append(f"FROM {plan.fact_table}")
    for dim_table, source, target in plan.joins:
        lines.append(f"JOIN {dim_table} ON {source} = {target}")

    conditions: list[str] = []
    for column_id, value in plan.value_filters:
        conditions.append(f"{column_id} = {_quote_literal(value)}")
    if plan.date_column and plan.date_start is not None and plan.date_end is not None:
        if plan.date_start == plan.date_end:
            conditions.append(f"{plan.date_column} = {plan.date_start}")
        else:
            conditions.append(
                f"{plan.date_column} BETWEEN {plan.date_start} AND {plan.date_end}"
            )
    if conditions:
        lines.append("WHERE " + " AND ".join(conditions))

    if group_by:
        lines.append("GROUP BY " + ", ".join(group_by))

    # 只要有分组就带 ORDER BY：没有 ORDER BY 的 GROUP BY 行序不确定，
    # 那是 S3 明确禁止的那类不确定性。
    if group_by and plan.metric_alias:
        lines.append(f"ORDER BY {_quote_identifier(plan.metric_alias)} {plan.direction}")
    elif plan.ranking and plan.metric_alias:
        lines.append(f"ORDER BY {_quote_identifier(plan.metric_alias)} {plan.direction}")

    if plan.limit is not None:
        lines.append(f"LIMIT {plan.limit}")

    return "\n".join(lines)


def generate_sql(context: GenerateContext) -> tuple[str | None, Refusal | None]:
    """确定性生成。返回 (sql, None) 或 (None, refusal)。"""
    plan = _plan(context)
    if isinstance(plan, Refusal):
        return None, plan
    return build_sql(context, plan), None
