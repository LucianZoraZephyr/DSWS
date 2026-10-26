"""S4 · 生成层的测试。

覆盖：上下文构建、确定性、拒答路径、口径来源、输出契约。
"""

from __future__ import annotations

import pytest

from app.generate.context import build_offline_context
from app.generate.deterministic import generate_sql
from app.generate.output_schema import is_valid_payload
from app.generate.pipeline import generate
from app.recall.catalog import default_catalog


# ---------------------------------------------------------------------------
# 上下文构建
# ---------------------------------------------------------------------------


def test_offline_context_picks_metric_from_contract() -> None:
    context = build_offline_context("华南销售额")
    assert [metric.name for metric in context.metrics] == ["GMV"]
    # 指标表达式直接来自契约的 formula，不是现编的
    assert context.metrics[0].formula == "SUM(fact_order.order_amount)"


def test_offline_context_picks_dimension_and_value() -> None:
    context = build_offline_context("华南销售额")
    assert "dim_region.region_name" in {c.id for c in context.columns}
    assert [value.value for value in context.values] == ["华南"]
    assert "fact_order" in context.table_names()
    assert "dim_region" in context.table_names()


def test_enum_value_makes_its_column_a_filter_not_a_grouping_dimension() -> None:
    """「食品饮料品类订单量」里 品类 是过滤条件，不是分组维度。"""
    context = build_offline_context("食品饮料品类订单量")
    assert [value.value for value in context.values] == ["食品饮料"]
    result = generate("食品饮料品类订单量")
    assert result.ok
    assert "GROUP BY" not in (result.sql or "")
    assert "dim_product.category = '食品饮料'" in (result.sql or "")


def test_alias_without_enum_value_is_a_grouping_dimension() -> None:
    """「各品类销量」里 品类 没有命中枚举值，所以是分组维度。"""
    result = generate("各品类销量")
    assert result.ok
    assert "GROUP BY dim_product.category" in (result.sql or "")


def test_time_dimension_is_not_a_grouping_dimension_without_series_marker() -> None:
    """「2025 年 1 月销量」里的 月 是过滤条件，不是分组维度。"""
    result = generate("2025 年 1 月销量")
    assert result.ok
    assert "GROUP BY" not in (result.sql or "")
    assert "BETWEEN 20250101 AND 20250131" in (result.sql or "")


def test_time_series_marker_turns_time_into_a_grouping_dimension() -> None:
    result = generate("每月销量")
    assert result.ok
    assert "GROUP BY dim_date.month" in (result.sql or "")
    assert "JOIN dim_date ON fact_order.date_id = dim_date.date_id" in (result.sql or "")


def test_context_contains_only_contract_tables_and_columns() -> None:
    catalog = default_catalog()
    context = build_offline_context("各省 GMV 排名")
    for table in context.tables:
        assert table.name in catalog.tables
    for column in context.columns:
        assert column.id in catalog.columns


# ---------------------------------------------------------------------------
# 确定性
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "query",
    ["华北销售额", "各品类销量", "品牌销售额 Top 3", "哪个省销量最高", "平均客单价"],
)
def test_same_question_always_produces_the_same_sql(query: str) -> None:
    """基线表格要能当验收标准，所以同题必须同解。"""
    first = generate(query)
    second = generate(query)
    assert first.to_payload() == second.to_payload()


def test_injected_now_does_not_change_the_sql() -> None:
    from datetime import datetime

    a = generate("华北销售额", now=datetime(2025, 3, 1, 12, 0, 0))
    b = generate("华北销售额", now=datetime(2026, 10, 26, 8, 30, 0))
    assert a.sql == b.sql


# ---------------------------------------------------------------------------
# 拒答路径
# ---------------------------------------------------------------------------


def test_out_of_range_year_is_refused() -> None:
    result = generate("2024 年销售额")
    assert not result.ok
    assert result.refusal.code == "OUT_OF_RANGE"
    assert "20250101" in result.refusal.reason


def test_out_of_range_month_is_refused() -> None:
    result = generate("2025 年 9 月销量")
    assert not result.ok
    assert result.refusal.code == "OUT_OF_RANGE"


def test_unsupported_grain_is_refused() -> None:
    result = generate("各城市销售额")
    assert not result.ok
    assert result.refusal.code == "UNSUPPORTED_INTENT"


def test_ranking_without_dimension_is_refused() -> None:
    """有排名意图但没有可分组的维度 → 必须拒答，不能瞎排。"""
    result = generate("GMV 排名")
    assert not result.ok
    assert result.refusal.code == "INSUFFICIENT_CONTEXT"


def test_empty_query_raises() -> None:
    with pytest.raises(ValueError):
        generate("   ")


# ---------------------------------------------------------------------------
# 输出契约
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "query",
    ["华北销售额", "2024 年销售额", "各城市销售额", "品牌销售额 Top 3"],
)
def test_payload_always_matches_the_output_contract(query: str) -> None:
    result = generate(query)
    assert is_valid_payload(result.to_payload())


def test_refusal_payload_has_code_and_reason() -> None:
    result = generate("2024 年销售额")
    payload = result.to_payload()
    assert set(payload) == {"refusal"}
    assert set(payload["refusal"]) == {"code", "reason"}
    assert payload["refusal"]["code"]


def test_sql_payload_has_only_sql() -> None:
    result = generate("华北销售额")
    payload = result.to_payload()
    assert set(payload) == {"sql"}
    assert payload["sql"].strip()


def test_prompt_is_rendered_from_all_seven_slots() -> None:
    result = generate("华北销售额")
    assert set(result.slots) == {
        "query",
        "table_infos",
        "column_infos",
        "metric_infos",
        "value_infos",
        "date_info",
        "db_info",
    }
    assert result.prompt.strip()
    for slot in result.slots:
        assert "{" + slot + "}" not in result.prompt


# ---------------------------------------------------------------------------
# 与契约的一致性
# ---------------------------------------------------------------------------


def test_generated_sql_only_uses_contract_columns() -> None:
    catalog = default_catalog()
    for query in ["华北销售额", "各品类销量", "黄金会员订单量", "2025 年 1 月销量"]:
        result = generate(query)
        assert result.ok
        for column_id in {
            column.id
            for column in catalog.columns.values()
            if column.id in (result.sql or "")
        }:
            assert column_id in catalog.columns


def test_aov_uses_the_contract_formula_not_a_rejected_variant() -> None:
    """AOV 有唯一口径；契约里明确说 448.81 / 5.41 不是 AOV。"""
    result = generate("平均客单价")
    assert result.ok
    sql = result.sql or ""
    assert "SUM(fact_order.order_amount) / COUNT(DISTINCT fact_order.order_id)" in sql
    assert "SUM(fact_order.order_quantity)" not in sql


def test_generator_uses_only_context_tables() -> None:
    context = build_offline_context("各品类销量")
    sql, refusal = generate_sql(context)
    assert refusal is None
    assert "dim_region" not in (sql or "")


# ---------------------------------------------------------------------------
# S3 → S4 的适配器
# ---------------------------------------------------------------------------


def _s3_state():
    """构造一份最小但形状真实的 S3 `RecallState`（不连任何存储）。"""
    from app.recall.models import (
        ColumnContext,
        MetricContext,
        RecallState,
        TableContext,
        ValueHit,
    )

    return RecallState(
        query="华北销售额",
        keywords=["华北销售额", "销售额", "华北"],
        tables=[
            TableContext(
                name="fact_order",
                role="fact",
                description="订单事实表；一行一单。",
                grain="一行一个 order_id（一单）",
                primary_key="order_id",
                foreign_keys=[
                    {"column": "region_id", "references": "dim_region.region_id"}
                ],
                measures=[],
                columns=[
                    ColumnContext(
                        id="fact_order.order_amount",
                        name="order_amount",
                        type="FLOAT",
                        role="measure",
                        description="整单成交金额",
                        aliases=("订单金额",),
                        value_domain={},
                        examples=[100.0],
                        table_id="fact_order",
                        nullable=True,
                        is_primary_key=False,
                        is_foreign_key=False,
                        references=None,
                    ),
                    ColumnContext(
                        id="fact_order.region_id",
                        name="region_id",
                        type="VARCHAR(20)",
                        role="foreign_key",
                        description="关联地区维度的外键",
                        aliases=(),
                        value_domain={},
                        examples=[],
                        table_id="fact_order",
                        nullable=True,
                        is_primary_key=False,
                        is_foreign_key=True,
                        references="dim_region.region_id",
                    ),
                ],
            ),
            TableContext(
                name="dim_region",
                role="dim",
                description="地区维度表。",
                grain="一行一个 region_id",
                primary_key="region_id",
                foreign_keys=[],
                measures=[],
                columns=[
                    ColumnContext(
                        id="dim_region.region_name",
                        name="region_name",
                        type="VARCHAR(50)",
                        role="dimension",
                        description="大区名称",
                        aliases=("地区",),
                        value_domain={},
                        examples=["华北"],
                        table_id="dim_region",
                        nullable=True,
                        is_primary_key=False,
                        is_foreign_key=False,
                        references=None,
                    ),
                    ColumnContext(
                        id="dim_region.region_id",
                        name="region_id",
                        type="VARCHAR(20)",
                        role="primary_key",
                        description="地区唯一标识",
                        aliases=(),
                        value_domain={},
                        examples=["R001"],
                        table_id="dim_region",
                        nullable=False,
                        is_primary_key=True,
                        is_foreign_key=False,
                        references=None,
                    ),
                ],
            ),
        ],
        metrics=[
            MetricContext(
                name="GMV",
                label="成交总额",
                description="所有订单的整单成交金额之和。",
                formula="SUM(fact_order.order_amount)",
                relevant_columns=["fact_order.order_amount"],
                aliases=("销售额",),
                aggregation="additive",
                expected_value=279159.5,
                grain="订单",
            )
        ],
        values=[ValueHit(id="dim_region.region_name:华北", value="华北", column_id="dim_region.region_name")],
        extra_context={
            "current_date": "2025-03-31",
            "database": {"dialect": "MySQL", "version": "8.0.36"},
        },
        dataset={"name": "dw", "date_range": [20250101, 20250331]},
        contract_version=1,
    )


def test_context_from_recall_state_keeps_the_shape() -> None:
    from app.generate.context import context_from_recall_state

    context = context_from_recall_state(_s3_state())
    assert context.query == "华北销售额"
    assert context.table_names() == {"fact_order", "dim_region"}
    assert [metric.name for metric in context.metrics] == ["GMV"]
    assert [value.value for value in context.values] == ["华北"]
    assert context.db_info.get("dialect") == "MySQL"


def test_recall_state_path_and_offline_path_agree() -> None:
    """接真实召回和离线裁剪必须给出同一条 SQL——否则两条链路会各说各话。"""
    from app.generate.context import context_from_recall_state

    recall_context = context_from_recall_state(_s3_state())
    from_recall = generate("华北销售额", context=recall_context)
    offline = generate("华北销售额")

    assert from_recall.ok, from_recall.refusal
    assert offline.ok, offline.refusal
    assert from_recall.sql == offline.sql
    assert from_recall.guard.ok


def test_context_without_the_date_column_still_generates_valid_sql() -> None:
    """上下文没给 date_id 时，不能写出引用集合外字段的 SQL。

    日期过滤是「上下文提供了这个字段」才写的——否则生成的 SQL 会被静态校验
    判成 SQL_UNKNOWN_COLUMN，白生成。
    """
    from dataclasses import replace

    context = build_offline_context("华北销售额")
    trimmed = replace(
        context,
        columns=tuple(
            column for column in context.columns if column.id != "fact_order.date_id"
        ),
    )
    result = generate("华北销售额", context=trimmed)
    assert result.ok, result.refusal
    assert "date_id" not in (result.sql or "")
    assert result.guard.ok
