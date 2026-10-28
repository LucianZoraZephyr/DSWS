"""
S2 计划层测试（不依赖 MySQL / ES / Qdrant）

验收重点：
  1. 计划计数与契约/数据一致：5 表 / 24 列 / 6 指标 / 9 条列指标关系
  2. 值同步策略可解释：只同步维度列，且基数 <= 阈值
  3. ID 与 config_hash 确定性：同一输入两次计划完全一致
  4. 契约被改坏时拒绝构建
  5. dry-run 不连接任何存储
"""

from __future__ import annotations

import copy
from collections import Counter
from dataclasses import replace

import pytest

from app.build.config import BuildConfig
from app.build.embedding import HashEmbeddingProvider
from app.build.errors import BuildError
from app.build.pipeline import build, load_plan, manifest_counts
from app.build.plan import EXAMPLES_LIMIT, build_plan, compute_config_hash
from app.schema_contract import load_contract, load_ddl


@pytest.fixture(scope="module")
def config() -> BuildConfig:
    return BuildConfig.from_env()


@pytest.fixture(scope="module")
def plan(config):
    plan, _ = load_plan(config)
    return plan


# ---------------------------------------------------------------------------
# 计划计数：与 S1 契约对齐
# ---------------------------------------------------------------------------


def test_plan_counts(plan) -> None:
    assert plan.counts() == {
        "meta_tables": 5,
        "meta_columns": 24,
        "meta_metrics": 6,
        "meta_column_metrics": 9,
        "es_values": 76,
        "qdrant_columns": 98,
        "qdrant_metrics": 22,
    }
    assert plan.contract_version == 1


def test_value_sync_policy(plan) -> None:
    sync = {column.id: column.sync_values for column in plan.columns}
    # 维度枚举 / 低基数开放文本 -> 同步
    for column_id in (
        "dim_region.province",
        "dim_region.region_name",
        "dim_region.country",
        "dim_customer.customer_name",
        "dim_customer.gender",
        "dim_customer.member_level",
        "dim_product.product_name",
        "dim_product.category",
        "dim_product.brand",
        "dim_date.year",
        "dim_date.quarter",
    ):
        assert sync[column_id] is True, column_id

    # 主键/外键/度量/range 不同步
    for column_id in (
        "fact_order.order_id",
        "fact_order.customer_id",
        "fact_order.order_amount",
        "fact_order.order_quantity",
        "dim_date.date_id",
        "dim_date.month",
        "dim_date.day",
    ):
        assert sync[column_id] is False, column_id


def test_examples_are_sorted_and_truncated(plan) -> None:
    """examples 是显式截断策略：排序后前 10 条，不是随机抽样。"""
    for column in plan.columns:
        assert len(column.examples) <= EXAMPLES_LIMIT
        keys = [(type(value).__name__, str(value)) for value in column.examples]
        assert keys == sorted(keys), column.id


def test_values_match_real_data(plan) -> None:
    counts = Counter(doc.column_id for doc in plan.values)
    assert counts == {
        "dim_customer.customer_name": 20,
        "dim_product.product_name": 15,
        "dim_product.brand": 15,
        "dim_region.province": 6,
        "dim_product.category": 6,
        "dim_region.region_name": 5,
        "dim_customer.member_level": 4,
        "dim_customer.gender": 2,
        "dim_region.country": 1,
        "dim_date.year": 1,
        "dim_date.quarter": 1,
    }
    assert any(
        doc.column_id == "dim_date.year" and doc.value == "2025" for doc in plan.values
    )
    assert not any(doc.column_id == "fact_order.order_amount" for doc in plan.values)


def test_column_metric_pairs_are_valid(plan) -> None:
    columns = {column.id for column in plan.columns}
    metrics = {metric.id for metric in plan.metrics}
    pairs = plan.column_metric_pairs()
    assert len(pairs) == 9
    assert all(column_id in columns for column_id, _ in pairs)
    assert all(metric_id in metrics for _, metric_id in pairs)


# ---------------------------------------------------------------------------
# 确定性：ID / hash
# ---------------------------------------------------------------------------


def test_plan_is_deterministic(config, plan) -> None:
    again, _ = load_plan(config)
    assert again.config_hash == plan.config_hash
    assert [doc.id for doc in again.values] == [doc.id for doc in plan.values]
    assert [point.id for point in again.column_points] == [
        point.id for point in plan.column_points
    ]
    assert [point.id for point in again.metric_points] == [
        point.id for point in plan.metric_points
    ]


def test_ids_are_unique(plan) -> None:
    for ids in (
        [doc.id for doc in plan.values],
        [point.id for point in plan.column_points],
        [point.id for point in plan.metric_points],
    ):
        assert len(ids) == len(set(ids))


def test_config_hash_changes_with_contract(config, plan) -> None:
    contract = load_contract()
    changed = copy.deepcopy(contract)
    changed["metrics"][0]["description"] += "（变更）"
    embedding = HashEmbeddingProvider(config.vector_size)
    assert (
        compute_config_hash(changed, config, embedding, plan.values) != plan.config_hash
    )


def test_config_hash_changes_with_target(config, plan) -> None:
    embedding = HashEmbeddingProvider(config.vector_size)
    changed = replace(config, es_index="value_index_other")
    assert (
        compute_config_hash(load_contract(), changed, embedding, plan.values)
        != plan.config_hash
    )


def test_config_hash_changes_with_value_docs(config, plan) -> None:
    embedding = HashEmbeddingProvider(config.vector_size)
    changed_values = list(plan.values) + [
        plan.values[0].__class__(
            id="dim_region.province.不存在",
            value="不存在",
            column_id="dim_region.province",
        )
    ]
    assert (
        compute_config_hash(load_contract(), config, embedding, changed_values)
        != plan.config_hash
    )


# ---------------------------------------------------------------------------
# 向量与 payload
# ---------------------------------------------------------------------------


def test_hash_embedding_is_deterministic_and_normalized() -> None:
    provider = HashEmbeddingProvider(dim=64)
    first = provider.embed("华北销售额")
    second = provider.embed("华北销售额")
    assert first == second
    assert len(first) == 64
    assert abs(sum(value * value for value in first) - 1.0) < 1e-9
    assert provider.embed("华南销售额") != first
    assert any(value != 0.0 for value in provider.embed(""))


def test_point_payloads_are_self_describing(plan) -> None:
    assert plan.column_points
    assert plan.metric_points
    column_ids = {column.id for column in plan.columns}
    for point in plan.column_points:
        assert point.payload["id"] in column_ids
        assert point.payload["embedding_text"] == point.text
        assert point.payload["table_id"] in {table.id for table in plan.tables}
    for point in plan.metric_points:
        assert point.payload["embedding_text"] == point.text
        assert point.payload["formula"]
        assert "expected_value" in point.payload


# ---------------------------------------------------------------------------
# 失败与 dry-run
# ---------------------------------------------------------------------------


def test_invalid_contract_is_rejected(config) -> None:
    contract = load_contract()
    broken = copy.deepcopy(contract)
    broken["metrics"][0]["formula"] = "SUM(fact_order.order_quantity)"
    ddl = load_ddl()
    try:
        with pytest.raises(BuildError, match="契约校验失败"):
            build_plan(
                broken,
                ddl,
                config,
                HashEmbeddingProvider(config.vector_size),
            )
    finally:
        ddl.close()


def test_dry_run_does_not_touch_stores(config) -> None:
    result = build(config, dry_run=True)
    assert result.status == "dry-run"
    assert result.writes == 0
    assert result.plan.counts()["es_values"] == 76


def test_manifest_counts_roundtrip(plan) -> None:
    manifest = {
        **plan.counts(),
        "build_id": "x",
        "status": "success",
    }
    assert manifest_counts(manifest) == plan.counts()


def test_config_repr_hides_passwords(config) -> None:
    text = config.safe_repr()
    assert config.meta_db.endswith("_rebuild")
    assert config.es_index.endswith("_rebuild")


def test_config_repr_hides_the_actual_password(monkeypatch) -> None:
    """`safe_repr()` 里不许出现真实口令。

    盯的是**这个配置里正在用的那个口令**，不是某个写死的字符串——
    换了口令，这条测试照样拦得住。
    """
    monkeypatch.setenv("REBUILD_MYSQL_PASSWORD", "a-very-secret-value")
    monkeypatch.setenv("REBUILD_MYSQL_ROOT_PASSWORD", "another-secret-value")
    text = BuildConfig.from_env().safe_repr()
    assert "a-very-secret-value" not in text
    assert "another-secret-value" not in text
