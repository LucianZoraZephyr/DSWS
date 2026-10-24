"""S2 · 从 S1 契约和冻结数仓生成「构建计划」。

这一层不碰网络，也不写任何存储。它负责把：
    contracts/schema.contract.yaml  (语义)
    fixtures/dw.sql                 (物理类型 + 真实值)
变成三组可写入的对象：
    TablePlan / ColumnPlan / MetricPlan  -> Meta MySQL
    ValueDoc                             -> ES
    VectorPoint                          -> Qdrant

这样 `--dry-run`、单元测试和真正构建可以共用同一份计划。
"""

from __future__ import annotations

import hashlib
import json
import sqlite3
import uuid
from collections.abc import Iterable, Sequence
from dataclasses import dataclass, field
from typing import Any

from app.build.config import BuildConfig
from app.build.embedding import EmbeddingProvider
from app.build.errors import BuildError
from app.schema_contract import Ddl, check_contract

# Qdrant point id 的命名空间；固定字符串保证跨进程可复现。
_POINT_NAMESPACE = uuid.uuid5(uuid.NAMESPACE_URL, "shopkeeper-rebuild/s2")

# Meta 里的 examples 是「排序后前 N 条」，不是完整值域；完整值域在 ES。
# 这是一个显式截断策略，S3 不应把 examples 当全集使用。
EXAMPLES_LIMIT = 10


def _sort_key(value: Any) -> tuple[str, str]:
    return (type(value).__name__, str(value))


def _display(value: Any) -> str:
    if isinstance(value, float) and value.is_integer():
        return str(int(value))
    return str(value)


def _jsonable(value: Any) -> Any:
    """把 YAML/SQLite 值转成可 JSON 序列化的结构。"""
    if isinstance(value, dict):
        return {str(key): _jsonable(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_jsonable(item) for item in value]
    if isinstance(value, (str, int, float, bool)) or value is None:
        return value
    return str(value)


def _dedupe_texts(parts: Iterable[str]) -> list[str]:
    seen: set[str] = set()
    result: list[str] = []
    for part in parts:
        text = str(part).strip()
        if text and text not in seen:
            seen.add(text)
            result.append(text)
    return result


@dataclass
class TablePlan:
    id: str
    name: str
    role: str
    description: str
    grain: str

    def as_payload(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "name": self.name,
            "role": self.role,
            "description": self.description,
            "grain": self.grain,
        }


@dataclass
class ColumnPlan:
    id: str
    name: str
    type: str
    role: str
    examples: list[Any]
    description: str
    aliases: list[str]
    table_id: str
    value_domain: dict[str, Any]
    sync_values: bool

    def as_payload(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "name": self.name,
            "type": self.type,
            "role": self.role,
            "examples": _jsonable(self.examples),
            "description": self.description,
            "aliases": list(self.aliases),
            "table_id": self.table_id,
            "value_domain": _jsonable(self.value_domain),
            "sync_values": self.sync_values,
        }


@dataclass
class MetricPlan:
    id: str
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

    def as_payload(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "name": self.name,
            "label": self.label,
            "description": self.description,
            "formula": self.formula,
            "relevant_columns": list(self.relevant_columns),
            "aliases": list(self.aliases),
            "aggregation": self.aggregation,
            "expected_value": self.expected_value,
            "grain": self.grain,
            "rejected_values": _jsonable(self.rejected_values),
        }


@dataclass
class ValueDoc:
    id: str
    value: str
    column_id: str

    def as_source(self) -> dict[str, Any]:
        return {"id": self.id, "value": self.value, "column_id": self.column_id}


@dataclass
class VectorPoint:
    id: str
    text: str
    payload: dict[str, Any]
    kind: str


@dataclass
class BuildPlan:
    contract_version: int
    config_hash: str
    tables: list[TablePlan]
    columns: list[ColumnPlan]
    metrics: list[MetricPlan]
    values: list[ValueDoc]
    column_points: list[VectorPoint]
    metric_points: list[VectorPoint]

    def counts(self) -> dict[str, int]:
        return {
            "meta_tables": len(self.tables),
            "meta_columns": len(self.columns),
            "meta_metrics": len(self.metrics),
            "meta_column_metrics": sum(
                len(metric.relevant_columns) for metric in self.metrics
            ),
            "es_values": len(self.values),
            "qdrant_columns": len(self.column_points),
            "qdrant_metrics": len(self.metric_points),
        }

    def column_metric_pairs(self) -> list[tuple[str, str]]:
        return [
            (column_id, metric.id)
            for metric in self.metrics
            for column_id in metric.relevant_columns
        ]


def _distinct(
    conn: sqlite3.Connection, table: str, column: str, limit: int | None = None
) -> list[Any]:
    """排序后的 distinct 值；SQLite 的 ORDER BY 对混合类型不够稳定，这里在 Python 里排。"""
    rows = conn.execute(
        f'SELECT DISTINCT "{column}" FROM "{table}" WHERE "{column}" IS NOT NULL'
    ).fetchall()
    values = [row[0] for row in rows]
    values.sort(key=_sort_key)
    if limit is not None:
        return values[:limit]
    return values


def _should_sync(
    column: dict[str, Any], distinct_count: int, max_value_docs: int
) -> bool:
    """哪些列的值值得进 ES？

    只同步「维度列 + 可枚举/低基数开放文本」，且基数不超过阈值：
      - 主键/外键不同步（值太多，且 ID 不用于自然语言检索）；
      - 度量不同步（金额/件数是连续值）；
      - range 不同步（month/day 这类数字枚举对检索帮助小，且旧系统也未同步）；
      - customer_name / product_name 这类 open 列只要基数可控，仍然同步。
    """
    if column.get("role") != "dimension":
        return False
    kind = (column.get("value_domain") or {}).get("kind")
    if kind not in {"enum", "open"}:
        return False
    return 0 < distinct_count <= max_value_docs


def _make_point(
    kind: str, object_id: str, text: str, payload: dict[str, Any]
) -> VectorPoint:
    point_id = str(uuid.uuid5(_POINT_NAMESPACE, f"{kind}:{object_id}:{text}"))
    return VectorPoint(id=point_id, text=text, payload=payload, kind=kind)


def _column_texts(column: ColumnPlan) -> list[str]:
    return _dedupe_texts([column.name, column.description, *column.aliases])


def _metric_texts(metric: MetricPlan) -> list[str]:
    return _dedupe_texts(
        [metric.name, metric.label, metric.description, *metric.aliases]
    )


def compute_config_hash(
    contract: dict[str, Any],
    config: BuildConfig,
    embedding: EmbeddingProvider,
    values: Sequence[ValueDoc],
) -> str:
    """配置 + 契约 + 数据值域 + 目标名，任何一项变化都触发重建。"""
    contract_semantics = json.dumps(
        contract,
        sort_keys=True,
        ensure_ascii=False,
        separators=(",", ":"),
        default=str,
    )
    value_digest = hashlib.sha256(
        "\n".join(
            f"{doc.id}={doc.value}" for doc in sorted(values, key=lambda item: item.id)
        ).encode("utf-8")
    ).hexdigest()
    payload = {
        "contract": contract_semantics,
        "targets": config.targets(),
        "embedding": {"name": embedding.name, "dim": embedding.dim},
        "max_value_docs": config.max_value_docs,
        "value_digest": value_digest,
    }
    return hashlib.sha256(
        json.dumps(
            payload,
            sort_keys=True,
            ensure_ascii=False,
            separators=(",", ":"),
        ).encode("utf-8")
    ).hexdigest()


def build_plan(
    contract: dict[str, Any],
    ddl: Ddl,
    config: BuildConfig,
    embedding: EmbeddingProvider,
) -> BuildPlan:
    """校验契约并生成完整计划；不写任何存储。"""
    issues = check_contract(contract, ddl)
    if issues:
        details = "\n".join(f"  - {issue}" for issue in issues)
        raise BuildError(f"契约校验失败，拒绝构建：\n{details}")

    tables: list[TablePlan] = []
    columns: list[ColumnPlan] = []
    values: list[ValueDoc] = []

    for table in contract.get("tables", []):
        table_name = table["name"]
        table_def = ddl.tables.get(table_name)
        if table_def is None:
            raise BuildError(f"契约表 {table_name} 在 DDL 中不存在")
        tables.append(
            TablePlan(
                id=table_name,
                name=table_name,
                role=table.get("role", ""),
                description=table.get("description", ""),
                grain=table.get("grain", ""),
            )
        )

        for column in table.get("columns", []):
            column_name = column["name"]
            column_def = table_def.column(column_name)
            if column_def is None:
                raise BuildError(f"契约列 {table_name}.{column_name} 在 DDL 中不存在")
            column_id = f"{table_name}.{column_name}"
            distinct = _distinct(ddl.conn, table_name, column_name)
            sync_values = _should_sync(column, len(distinct), config.max_value_docs)
            column_plan = ColumnPlan(
                id=column_id,
                name=column_name,
                type=column_def.type,
                role=column.get("role", ""),
                examples=distinct[:EXAMPLES_LIMIT],
                description=column.get("description", ""),
                aliases=[str(alias) for alias in column.get("aliases", [])],
                table_id=table_name,
                value_domain=column.get("value_domain") or {},
                sync_values=sync_values,
            )
            columns.append(column_plan)
            if sync_values:
                for value in distinct:
                    values.append(
                        ValueDoc(
                            id=f"{column_id}.{_display(value)}",
                            value=_display(value),
                            column_id=column_id,
                        )
                    )

    metrics: list[MetricPlan] = []
    for metric in contract.get("metrics", []):
        metrics.append(
            MetricPlan(
                id=metric["name"],
                name=metric["name"],
                label=metric.get("label", metric["name"]),
                description=metric.get("description", ""),
                formula=metric.get("formula", ""),
                relevant_columns=[
                    str(column_id) for column_id in metric.get("relevant_columns", [])
                ],
                aliases=[str(alias) for alias in metric.get("aliases", [])],
                aggregation=metric.get("aggregation", ""),
                expected_value=float(metric.get("expected_value", 0.0)),
                grain=metric.get("grain", ""),
                rejected_values=_jsonable(metric.get("rejected_values", [])),
            )
        )

    # 确定性 ID 是幂等的基础：同一对象 + 同一文本永远得到同一个 point id。
    column_points: list[VectorPoint] = []
    for column in columns:
        payload = column.as_payload()
        for text in _column_texts(column):
            point_payload = dict(payload)
            point_payload["embedding_text"] = text
            column_points.append(_make_point("column", column.id, text, point_payload))

    metric_points: list[VectorPoint] = []
    for metric in metrics:
        payload = metric.as_payload()
        for text in _metric_texts(metric):
            point_payload = dict(payload)
            point_payload["embedding_text"] = text
            metric_points.append(_make_point("metric", metric.id, text, point_payload))

    # 重复 ID 会让 ES/Qdrant 静默覆盖；在计划阶段就失败。
    for label, ids in (
        ("value", [doc.id for doc in values]),
        ("column point", [point.id for point in column_points]),
        ("metric point", [point.id for point in metric_points]),
    ):
        duplicates = sorted({item for item in ids if ids.count(item) > 1})
        if duplicates:
            raise BuildError(f"{label} ID 重复：{duplicates[:5]}")

    config_hash = compute_config_hash(contract, config, embedding, values)
    return BuildPlan(
        contract_version=int(contract.get("version", 0)),
        config_hash=config_hash,
        tables=tables,
        columns=columns,
        metrics=metrics,
        values=values,
        column_points=column_points,
        metric_points=metric_points,
    )
