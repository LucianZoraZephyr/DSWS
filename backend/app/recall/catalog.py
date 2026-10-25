"""S3 · 契约目录（只读）。

检索层需要一份「稳定、可复现」的词典：

    - 表名 / 列名 / 别名
    - 指标的 name / label / aliases
    - 契约里声明的真实枚举值

这些信息都来自 S1 的 `contracts/schema.contract.yaml`。它们不替代 Meta
MySQL；Meta 仍然是运行时的实体来源，本目录只负责：

    1. 做确定性关键词匹配（不调用 LLM、不使用 jieba）
    2. 给候选排序提供契约顺序
    3. 补齐 Meta 里没有的 nullable / references / 外键引用关系

这样 S3 的词法补偿不依赖网络，也不会把旧系统的 meta_config.yaml
（S1 的负向夹具）当成事实源。
"""

from __future__ import annotations

from dataclasses import dataclass
from functools import lru_cache
from typing import Any

from app.schema_contract import load_contract


@dataclass(frozen=True)
class ColumnSpec:
    id: str
    name: str
    table_id: str
    type: str
    role: str
    nullable: bool | None
    description: str
    aliases: tuple[str, ...]
    value_domain: dict[str, Any]
    order: int


@dataclass(frozen=True)
class TableSpec:
    name: str
    role: str
    description: str
    grain: str
    primary_key: str | None
    foreign_keys: tuple[dict[str, str], ...]
    column_ids: tuple[str, ...]
    order: int


@dataclass(frozen=True)
class MetricSpec:
    name: str
    label: str
    description: str
    formula: str
    relevant_columns: tuple[str, ...]
    aliases: tuple[str, ...]
    aggregation: str
    expected_value: float
    grain: str
    rejected_values: tuple[dict[str, Any], ...]
    order: int


class Catalog:
    """契约的只读视图；所有顺序都来自 YAML 的书写顺序。"""

    def __init__(self, contract: dict[str, Any]):
        self.contract = contract
        self.contract_version = int(contract.get("version", 0))
        self.dataset: dict[str, Any] = dict(contract.get("dataset") or {})

        self.tables: dict[str, TableSpec] = {}
        self.columns: dict[str, ColumnSpec] = {}
        self.metrics: dict[str, MetricSpec] = {}
        self._references: dict[str, str] = {}
        self._value_surfaces: list[tuple[str, str]] = []

        for table_order, table in enumerate(contract.get("tables", [])):
            table_name = str(table["name"])
            column_ids: list[str] = []
            for column_order, column in enumerate(table.get("columns", [])):
                column_id = f"{table_name}.{column['name']}"
                spec = ColumnSpec(
                    id=column_id,
                    name=str(column["name"]),
                    table_id=table_name,
                    type=str(column.get("type", "")),
                    role=str(column.get("role", "")),
                    nullable=column.get("nullable"),
                    description=str(column.get("description", "")),
                    aliases=tuple(str(alias) for alias in column.get("aliases", [])),
                    value_domain=dict(column.get("value_domain") or {}),
                    order=column_order,
                )
                self.columns[column_id] = spec
                column_ids.append(column_id)
                if spec.value_domain.get("kind") == "enum":
                    for value in spec.value_domain.get("values", []):
                        self._value_surfaces.append((str(value), column_id))

            foreign_keys = tuple(
                {
                    "column": str(item["column"]),
                    "references": str(item["references"]),
                }
                for item in table.get("foreign_keys", [])
            )
            for item in foreign_keys:
                self._references[f"{table_name}.{item['column']}"] = item["references"]

            self.tables[table_name] = TableSpec(
                name=table_name,
                role=str(table.get("role", "")),
                description=str(table.get("description", "")),
                grain=str(table.get("grain", "")),
                primary_key=table.get("primary_key"),
                foreign_keys=foreign_keys,
                column_ids=tuple(column_ids),
                order=table_order,
            )

        for metric_order, metric in enumerate(contract.get("metrics", [])):
            name = str(metric["name"])
            self.metrics[name] = MetricSpec(
                name=name,
                label=str(metric.get("label", name)),
                description=str(metric.get("description", "")),
                formula=str(metric.get("formula", "")),
                relevant_columns=tuple(
                    str(column_id)
                    for column_id in metric.get("relevant_columns", [])
                ),
                aliases=tuple(str(alias) for alias in metric.get("aliases", [])),
                aggregation=str(metric.get("aggregation", "")),
                expected_value=float(metric.get("expected_value", 0.0)),
                grain=str(metric.get("grain", "")),
                rejected_values=tuple(
                    dict(item) for item in metric.get("rejected_values", [])
                ),
                order=metric_order,
            )

    # ------------------------------------------------------------------
    # 查询辅助
    # ------------------------------------------------------------------

    def column(self, column_id: str) -> ColumnSpec | None:
        return self.columns.get(column_id)

    def table(self, table_name: str) -> TableSpec | None:
        return self.tables.get(table_name)

    def metric(self, name: str) -> MetricSpec | None:
        return self.metrics.get(name)

    def references(self, column_id: str) -> str | None:
        return self._references.get(column_id)

    def table_of_column(self, column_id: str) -> str | None:
        column = self.columns.get(column_id)
        return column.table_id if column else None

    def value_surfaces(self) -> tuple[tuple[str, str], ...]:
        """(真实枚举值, column_id) 列表；顺序固定为契约顺序。"""
        return tuple(self._value_surfaces)

    def column_payload(self, column_id: str) -> dict[str, Any] | None:
        """与 S2 Qdrant payload 保持同一组字段，供词法命中补候选。"""
        column = self.columns.get(column_id)
        if column is None:
            return None
        return {
            "id": column.id,
            "name": column.name,
            "type": column.type,
            "role": column.role,
            "examples": [],
            "description": column.description,
            "aliases": list(column.aliases),
            "table_id": column.table_id,
            "value_domain": dict(column.value_domain),
            "sync_values": False,
        }

    def metric_payload(self, name: str) -> dict[str, Any] | None:
        metric = self.metrics.get(name)
        if metric is None:
            return None
        return {
            "id": metric.name,
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
        }


@lru_cache(maxsize=1)
def default_catalog() -> Catalog:
    """进程内缓存；契约在 S3 运行期是只读的。"""
    return Catalog(load_contract())
