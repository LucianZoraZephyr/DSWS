"""S3 关键词抽取单元测试（不连接任何服务）。

验收重点：
  1. 无 LLM / 无 jieba；纯契约词典 + 日期正则 + 完整 query 兜底
  2. 表名 / 列名 / 别名 / 真实枚举值都能命中
  3. 日期模式产生 dim_date 的确定性提示
  4. 「省」这类单字别名只做列提示，不进入 ES 关键词，避免把省份枚举拉进来
  5. 同一输入重复执行结果完全一致
"""

from __future__ import annotations

import sys

import pytest

from app.recall.catalog import default_catalog
from app.recall.keywords import extract_keywords


@pytest.fixture(scope="module")
def catalog():
    return default_catalog()


def targets(result, kind: str) -> set[str]:
    found: set[str] = set()
    for hint in result.hints:
        if hint.kind == kind:
            found.update(hint.target_ids)
    return found


def column_targets(result) -> set[str]:
    """value hint 的 target 也是 column_id；检索层两种都会用。"""
    return targets(result, "column") | targets(result, "value")


def test_full_query_is_always_the_fallback(catalog) -> None:
    result = extract_keywords("完全不存在的新客户名", catalog)
    assert result.keywords[0] == "完全不存在的新客户名"


def test_contract_lexicon_maps_column_alias_and_metric(catalog) -> None:
    result = extract_keywords("华北销售额", catalog)
    assert set(result.keywords) >= {"华北销售额", "华北", "销售额"}
    assert "dim_region.region_name" in column_targets(result)
    assert "GMV" in targets(result, "metric")

    result = extract_keywords("各品类销量", catalog)
    assert "dim_product.category" in column_targets(result)
    assert "销量" in targets(result, "metric")


def test_real_values_map_to_their_columns(catalog) -> None:
    cases = {
        "华东订单量": "dim_region.region_name",
        "黄金会员订单量": "dim_customer.member_level",
        "女性客户 GMV": "dim_customer.gender",
        "华为品牌销售额": "dim_product.brand",
        "食品饮料品类订单量": "dim_product.category",
    }
    for query, column_id in cases.items():
        result = extract_keywords(query, catalog)
        assert column_id in column_targets(result), query
        assert any(
            hint.source == "value" and column_id in hint.target_ids
            for hint in result.hints
        ), query


def test_date_patterns_produce_dim_date_hints(catalog) -> None:
    result = extract_keywords("2025 年 1 月销量", catalog)
    assert "dim_date.year" in targets(result, "column")
    assert "dim_date.month" in targets(result, "column")
    assert "2025" in result.keywords
    assert "1月" in result.keywords

    result = extract_keywords("华南 2025Q1 销售额", catalog)
    assert "dim_date.year" in targets(result, "column")
    assert "dim_date.quarter" in targets(result, "column")
    assert "Q1" in result.keywords


def test_single_char_alias_is_hint_only(catalog) -> None:
    result = extract_keywords("哪个省销量最高", catalog)
    assert "dim_region.province" in targets(result, "column")
    # 单字「省」不进入 ES 关键词：省份枚举会被它整片误召回。
    assert "省" not in result.keywords


def test_english_table_and_column_names(catalog) -> None:
    result = extract_keywords("fact_order 的 order_amount", catalog)
    assert "fact_order" in result.keywords
    assert "dim_region.region_name" not in targets(result, "column")
    assert "fact_order.order_amount" in targets(result, "column")
    assert "fact_order" in targets(result, "table")


def test_extraction_is_deterministic(catalog) -> None:
    first = extract_keywords("华东男性客户订单量", catalog)
    second = extract_keywords("华东男性客户订单量", catalog)
    assert first == second
    assert first.keywords == [
        "华东男性客户订单量",
        "华东",
        "男",
        "订单量",
    ]


def test_no_llm_or_segmentation_dependency(catalog) -> None:
    extract_keywords("华北销售额", catalog)
    for forbidden in ("jieba", "langchain", "langchain_core", "openai", "anthropic"):
        assert forbidden not in sys.modules, forbidden
