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

S6 起，上下文构建被拆成 **8 个阶段**，阶段 id 就是 `contracts/steps.yaml`
里那 8 个上下文步骤（extract_keywords … add_extra_context）。这样步骤条上
的每一个节点背后都有**真实发生过的工作**，而不是发完事件再假装做过。
`build_offline_context` 的行为与 S4 完全一致——它就是按顺序跑完这 8 个
阶段。
"""

from __future__ import annotations

import re
from collections.abc import Callable
from dataclasses import dataclass
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


@dataclass(frozen=True)
class ContextPhase:
    """上下文构建的一个阶段。

    `step` 必须是 `contracts/steps.yaml` 里的 step id——步骤条上的那个节点
    背后真的有这么一件事。`run` 就是那件事本身。
    """

    step: str
    run: Callable[[], None]


class OfflineContextBuilder:
    """离线上下文构建器：8 个阶段，每阶段一件真事。

    阶段划分不是为了好看，而是为了让 SSE 的 progress 事件有对应的**真实
    工作**。审计缺陷 D4 说的就是「前端硬编码 12 个节点，correct_sql 永远
    灰着」——反过来，如果后端发了 8 个事件却没有 8 件事在做，那只是把
    同一个问题搬到了后端。

    阶段与步骤的对应：

        extract_keywords     抽关键词
        recall_column        找出问题里提到的字段
        recall_value         找出问题里提到的取值
        recall_metric        找出问题里提到的指标
        merge_retrieved_info 把三类召回合并成「本次要用的字段集合」
        filter_table         裁出要用的表与外键
        filter_metric        裁出要暴露给生成器的指标
        add_extra_context    补上时间与数据库环境
    """

    def __init__(
        self,
        query: str,
        *,
        catalog: Catalog | None = None,
        now: datetime | None = None,
    ) -> None:
        self.catalog = catalog or default_catalog()
        self.query = str(query).strip()
        if not self.query:
            raise ValueError("query 不能为空")
        self.now = now

        # 各阶段的产物
        self.keywords: object = None
        self.dimension_candidates: list[str] = []
        self.matched_values: list[ValueLite] = []
        self.value_columns: set[str] = set()
        self.metric_candidates: list[tuple[MetricLite, str]] = []
        self.metrics: tuple[MetricLite, ...] = ()
        self.dimension_columns: list[str] = []
        self.core_column_ids: set[str] = set()
        self.fact_table: str | None = None
        self.tables: dict[str, TableLite] = {}
        self.columns: dict[str, ColumnLite] = {}
        self.dataset: dict = {}
        self.date_info: dict = {}
        self.db_info: dict = {}

    # -- 阶段 -------------------------------------------------------------

    def _phase_extract_keywords(self) -> None:
        self.keywords = extract_keywords(self.query, self.catalog)

    def _phase_recall_column(self) -> None:
        """问题里提到的维度字段。

        注意：这里**不**排除被当成过滤值的字段——那一步在
        `merge_retrieved_info` 里做（值先召回，维度候选后合并）。
        """
        candidates: list[str] = []
        for column_id, column in self.catalog.columns.items():
            if column.role != "dimension":
                continue
            if column.table_id == "dim_date" and not is_time_series_dimension(
                column_id, self.query
            ):
                continue
            if _alias_matched(column, self.query) is not None:
                candidates.append(column_id)
        self.dimension_candidates = candidates

    def _phase_recall_value(self) -> None:
        """问题里命中的真实取值。"""
        matched: list[ValueLite] = []
        value_columns: set[str] = set()
        for value, column_id in self.catalog.value_surfaces():
            column = self.catalog.column(column_id)
            if column is None:
                continue
            if column.table_id == "dim_date":
                # 年 / 月 / 季度的枚举值由时间范围逻辑处理，
                # 否则「2025 年 1 月」会被当成一次 dim_date.year 等值过滤。
                continue
            if not _value_in_query(str(value), self.query):
                continue
            matched.append(ValueLite(value=str(value), column_id=column_id))
            value_columns.add(column_id)
        self.matched_values = matched
        self.value_columns = value_columns

    def _phase_recall_metric(self) -> None:
        """问题里命中的指标（候选）。"""
        candidates: list[tuple[MetricLite, str]] = []
        for spec in self.catalog.metrics.values():
            surfaces = tuple(
                item for item in (spec.name, spec.label, *spec.aliases) if item
            )
            hit = _matched_surface(surfaces, self.query)
            if hit is None:
                continue
            candidates.append(
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
        self.metric_candidates = candidates

    def _phase_merge_retrieved_info(self) -> None:
        """把三类召回合并成「本次要用的字段集合」。

        维度候选里要排除被当成过滤值的字段：命中的取值说明这个字段是
        过滤条件，不是分组维度。
        """
        self.dimension_columns = [
            column_id
            for column_id in self.dimension_candidates
            if column_id not in self.value_columns
        ]

        core: set[str] = set()
        for metric, _hit in self.metric_candidates:
            core.update(metric.relevant_columns)
        core.update(self.value_columns)
        core.update(self.dimension_columns)
        self.core_column_ids = core

    def _phase_filter_table(self) -> None:
        """裁出要用的表、字段与外键。"""
        cat = self.catalog
        self.fact_table = next(
            (name for name, spec in cat.tables.items() if spec.role == "fact"),
            None,
        )

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

        for column_id in sorted(self.core_column_ids):
            add_column(column_id)
            table_name = cat.table_of_column(column_id)
            if table_name:
                add_table(table_name)

        # JOIN 需要的键：只拉**真正用到的维表**对应的那一对外键。
        #
        # 这里刻意不是「事实表的全部外键」——那会把 4 张维表全塞进上下文，
        # 于是「列存在」这条校验就形同虚设（上下文里有 dim_product，任何
        # dim_product.* 都会被放行）。只拉用到的维表，校验才有牙齿。
        if self.fact_table:
            fact_spec = cat.table(self.fact_table)
            fact_fks = fact_spec.foreign_keys if fact_spec else ()
            dim_tables = {
                cat.table_of_column(column_id)
                for column_id in self.core_column_ids
                if cat.table_of_column(column_id)
                and cat.table_of_column(column_id) != self.fact_table
            }
            for item in fact_fks:
                target = str(item["references"])
                target_table = target.split(".", 1)[0]
                if target_table not in dim_tables:
                    continue
                source = f"{self.fact_table}.{item['column']}"
                add_column(source)
                add_column(target)
                add_table(target_table)

        # 字段召回命中的列：只保留其所属表已在上下文里的那些，
        # 避免出现「列在上下文里、但它的表不在」这种自相矛盾的裁剪。
        for hint in getattr(self.keywords, "hints", ()):
            if hint.kind != "column":
                continue
            for column_id in hint.target_ids:
                if column_id not in cat.columns:
                    continue
                table_name = cat.table_of_column(column_id)
                if table_name in tables:
                    add_column(column_id)

        # 时间过滤写在事实表的 date_id 上，不需要连 dim_date。
        if self.fact_table:
            date_column = f"{self.fact_table}.date_id"
            if cat.column(date_column) is not None:
                add_column(date_column)
                add_table(self.fact_table)

        self.tables = tables
        self.columns = columns

    def _phase_filter_metric(self) -> None:
        """裁出要暴露给生成器的指标。

        契约里可能有很多指标，但只有问题真的提到的那些才进上下文——
        生成器只该看到它有权用的口径。
        """
        self.metrics = tuple(metric for metric, _hit in self.metric_candidates)

    def _phase_add_extra_context(self) -> None:
        """补上时间与数据库环境。"""
        cat = self.catalog
        dataset = dict(cat.dataset)
        date_range = dataset.get("date_range") or []
        start_id = date_range[0] if len(date_range) > 0 else None
        end_id = date_range[1] if len(date_range) > 1 else None

        current = self.now or datetime.now()
        self.dataset = dataset
        self.date_info = {
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
        self.db_info = {
            "dialect": "MySQL",
            "version": None,
            "version_source": "unavailable",
        }

    # -- 对外 -------------------------------------------------------------

    def phases(self) -> tuple[ContextPhase, ...]:
        """8 个阶段，按 `contracts/steps.yaml` 的顺序。"""
        return (
            ContextPhase("extract_keywords", self._phase_extract_keywords),
            ContextPhase("recall_column", self._phase_recall_column),
            ContextPhase("recall_value", self._phase_recall_value),
            ContextPhase("recall_metric", self._phase_recall_metric),
            ContextPhase(
                "merge_retrieved_info", self._phase_merge_retrieved_info
            ),
            ContextPhase("filter_table", self._phase_filter_table),
            ContextPhase("filter_metric", self._phase_filter_metric),
            ContextPhase("add_extra_context", self._phase_add_extra_context),
        )

    def result(self) -> GenerateContext:
        """跑完所有阶段之后的结果。"""
        return GenerateContext(
            query=self.query,
            tables=tuple(self.tables[name] for name in sorted(self.tables)),
            columns=tuple(self.columns[cid] for cid in sorted(self.columns)),
            metrics=self.metrics,
            values=tuple(self.matched_values),
            dataset=self.dataset,
            date_info=self.date_info,
            db_info=self.db_info,
        )


def build_offline_context(
    query: str,
    *,
    catalog: Catalog | None = None,
    now: datetime | None = None,
) -> GenerateContext:
    """从契约 + 问题构建一份离线上下文（不连接任何存储）。

    它就是「按顺序跑完 8 个阶段」——S6 的编排器会一个阶段一个阶段地跑，
    这样每一步都有真实耗时；这里一次性跑完，行为完全一致。
    """
    builder = OfflineContextBuilder(query, catalog=catalog, now=now)
    for phase in builder.phases():
        phase.run()
    return builder.result()


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
