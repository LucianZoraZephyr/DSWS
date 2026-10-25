"""S3 YAML 渲染单元测试（不连接任何服务）。

验收重点：
  1. 契约关键字段一个不少：name/type/role/description/aliases/
     value_domain/grain/formula/expected_value/relevant_columns/rejected_values
  2. examples 显式标成截断：examples_truncated: true / examples_limit: 10
  3. aliases / value_domain / relevant_columns / rejected_values 不被截断
  4. 同一状态重复渲染字节级一致
  5. add_extra_context 的日期、星期、季度、数据库版本可复现
"""

from __future__ import annotations

from datetime import datetime

import yaml

from app.recall.catalog import default_catalog
from app.recall.models import (
    ColumnContext,
    MetricContext,
    RecallState,
    TableContext,
    ValueHit,
)
from app.recall.nodes import add_extra_context
from app.recall.render import render


def make_column() -> ColumnContext:
    return ColumnContext(
        id="fact_order.order_amount",
        name="order_amount",
        type="FLOAT",
        role="measure",
        description="整单成交金额（已含件数），不是单价。",
        aliases=[f"金额别名{i}" for i in range(15)],
        value_domain={"kind": "range", "min": 30.0, "max": 9499.0},
        examples=[100.0, 105.0, 1198.0],
        table_id="fact_order",
        nullable=True,
        is_primary_key=False,
        is_foreign_key=False,
        references=None,
        matched_values=["华北销售额命中值"],
        recall_score=1.23456789,
        vector_score=0.5,
        lexical_score=0.73456789,
        matched_keywords=["销售额"],
        sources=["column_recall"],
        examples_truncated=True,
        examples_limit=10,
    )


def make_state() -> RecallState:
    column = make_column()
    table = TableContext(
        name="fact_order",
        role="fact",
        description="订单事实表；一行一单。",
        grain="一行一个 order_id（一单）。",
        primary_key="order_id",
        foreign_keys=[
            {"column": "region_id", "references": "dim_region.region_id"}
        ],
        columns=[column],
    )
    metric = MetricContext(
        name="GMV",
        label="成交总额",
        description="所有订单的整单成交金额之和。",
        formula="SUM(fact_order.order_amount)",
        relevant_columns=[f"fact_order.col{i}" for i in range(12)],
        aliases=[f"指标别名{i}" for i in range(15)],
        aggregation="additive",
        expected_value=279159.5,
        grain="订单",
        rejected_values=[{"value": i, "meaning": f"错误口径{i}"} for i in range(12)],
        dependencies=[
            {
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
                "examples_limit": 10,
                "matched_values": list(column.matched_values),
            }
        ],
        recall_score=2.5,
        matched_keywords=["销售额"],
        sources=["metric_recall"],
    )
    return RecallState(
        query="华北销售额",
        keywords=["华北销售额", "华北", "销售额"],
        tables=[table],
        metrics=[metric],
        values=[
            ValueHit(
                id="dim_region.region_name.华北",
                value="华北",
                column_id="dim_region.region_name",
                score=3.5,
                matched_keywords=["华北"],
            )
        ],
        extra_context={"current_date": "2026-10-25"},
        contract_version=1,
        trace={
            "targets": {"es_index": "value_index_rebuild"},
            "es_roundtrips": 1,
            "qdrant_roundtrips": 2,
            "qdrant_column_roundtrips": 1,
            "qdrant_metric_roundtrips": 1,
            "meta_roundtrips": 5,
            "keyword_count": 3,
            "column_candidates": 11,
            "value_candidates": 1,
            "metric_candidates": 6,
            "filter_metric_fallback": False,
        },
    )


def test_render_keeps_every_contract_field() -> None:
    payload = yaml.safe_load(render(make_state()))

    assert payload["query"] == "华北销售额"
    assert payload["contract_version"] == 1
    assert payload["keywords"] == ["华北销售额", "华北", "销售额"]

    table = payload["tables"][0]
    assert table["name"] == "fact_order"
    assert table["role"] == "fact"
    assert table["description"]
    assert table["grain"]
    assert table["primary_key"] == "order_id"
    assert table["foreign_keys"] == [
        {"column": "region_id", "references": "dim_region.region_id"}
    ]

    column = table["columns"][0]
    assert column["name"] == "order_amount"
    assert column["type"] == "FLOAT"
    assert column["role"] == "measure"
    assert column["description"]
    assert len(column["aliases"]) == 15
    assert column["value_domain"] == {"kind": "range", "min": 30.0, "max": 9499.0}
    assert column["examples_truncated"] is True
    assert column["examples_limit"] == 10
    assert column["matched_values"] == ["华北销售额命中值"]

    metric = payload["metrics"][0]
    assert metric["name"] == "GMV"
    assert metric["formula"] == "SUM(fact_order.order_amount)"
    assert metric["expected_value"] == 279159.5
    assert len(metric["aliases"]) == 15
    assert len(metric["relevant_columns"]) == 12
    assert len(metric["rejected_values"]) == 12
    assert metric["dependencies"][0]["examples_truncated"] is True

    assert payload["examples_policy"]["examples_truncated"] is True
    assert payload["examples_policy"]["examples_limit"] == 10


def test_render_reports_retrieval_stats_and_values() -> None:
    payload = yaml.safe_load(render(make_state()))
    retrieval = payload["retrieval"]
    assert retrieval["es_roundtrips"] == 1
    assert retrieval["qdrant_roundtrips"] == 2
    assert retrieval["qdrant_column_roundtrips"] == 1
    assert retrieval["qdrant_metric_roundtrips"] == 1
    assert retrieval["meta_roundtrips"] == 5
    assert retrieval["candidates"] == {
        "columns": 11,
        "values": 1,
        "metrics": 6,
    }
    assert payload["values"][0]["value"] == "华北"
    assert payload["values"][0]["column_id"] == "dim_region.region_name"


def test_render_is_byte_stable() -> None:
    state = make_state()
    assert render(state) == render(state)


def test_add_extra_context_is_deterministic_for_fixed_now() -> None:
    state = add_extra_context(
        make_state(),
        default_catalog(),
        database_version="8.0.46",
        now=datetime(2026, 10, 25, 15, 30),
    )
    context = state.extra_context
    assert context["current_date"] == "2026-10-25"
    assert context["weekday"] == "星期日"
    assert context["quarter"] == "2026Q4"
    assert context["database"] == {
        "dialect": "MySQL",
        "version": "8.0.46",
        "version_source": "SELECT VERSION()",
    }
    assert context["dataset"]["date_range"] == {
        "start_id": 20250101,
        "end_id": 20250331,
        "start": "2025-01-01",
        "end": "2025-03-31",
    }
    assert context["examples_policy"]["examples_truncated"] is True
    assert context["examples_policy"]["examples_limit"] == 10
