"""S3 黄金集测试。

普通 `pytest` 只跑结构校验（不需要 MySQL/ES/Qdrant）；真实检索测试必须：

    REBUILD_INTEGRATION=1 uv run pytest -q tests/recall

集成测试断言：
  - 20 题 expected_tables / expected_columns / expected_metrics / expected_values
    全部出现在最终 YAML 上下文里
  - must_not 里的错误指标/字段/表不能出现
  - 每题 ES `_msearch` <= 2 次；Qdrant 字段/指标各 1 次 batch search
  - recall@k 与 precision：允许额外候选，但输出规模和 precision 有上限
    （防止「把所有表列指标都塞进去」的全召回作弊）
"""

from __future__ import annotations

import os
from statistics import mean
from typing import Any

import pytest
import yaml

from app.recall.catalog import default_catalog
from app.recall.clients import RecallStack
from app.recall.pipeline import recall
from app.recall.render import render

GOLDEN_PATH = os.path.join(os.path.dirname(__file__), "golden.yaml")
with open(GOLDEN_PATH, encoding="utf-8") as handle:
    GOLDEN = yaml.safe_load(handle)
CASES: list[dict[str, Any]] = GOLDEN["questions"]

RUN_INTEGRATION = os.getenv("REBUILD_INTEGRATION") == "1"


def _precision(expected: list[str], actual: list[str]) -> float:
    if not actual:
        return 1.0 if not expected else 0.0
    return len(set(expected) & set(actual)) / len(actual)


def _recall_at_k(expected: list[str], actual: list[str], k: int) -> float:
    if not expected:
        return 1.0
    return len(set(expected) & set(actual[:k])) / len(expected)


def _must_not_check(case: dict[str, Any], state, catalog) -> None:
    actual_columns = set(state.column_ids)
    actual_metrics = set(state.metric_names)
    actual_tables = set(state.table_names)
    for token in case.get("must_not", []):
        if token in catalog.columns:
            assert token not in actual_columns, (case["id"], token)
        elif token in catalog.metrics:
            assert token not in actual_metrics, (case["id"], token)
        elif token in catalog.tables:
            assert token not in actual_tables, (case["id"], token)
        else:
            # 契约里不存在的列（例如 dim_region.city / dim_product.price）
            # 也必须被明确排除；它们本来就不该被幻觉出来。
            assert token not in actual_columns, (case["id"], token)
            assert token not in actual_metrics, (case["id"], token)
            assert token not in actual_tables, (case["id"], token)


# ---------------------------------------------------------------------------
# 结构校验：不需要任何服务
# ---------------------------------------------------------------------------


def test_golden_has_twenty_unique_questions() -> None:
    assert len(CASES) == 20
    ids = [case["id"] for case in CASES]
    assert len(ids) == len(set(ids))
    for case in CASES:
        for field in (
            "question",
            "expected_tables",
            "expected_columns",
            "expected_metrics",
            "expected_values",
        ):
            assert field in case, (case.get("id"), field)
            if field != "question":
                assert isinstance(case[field], list)


def test_golden_expected_ids_exist_in_contract() -> None:
    catalog = default_catalog()
    for case in CASES:
        for table in case["expected_tables"]:
            assert table in catalog.tables, (case["id"], table)
        for column in case["expected_columns"]:
            assert column in catalog.columns, (case["id"], column)
        for metric in case["expected_metrics"]:
            assert metric in catalog.metrics, (case["id"], metric)


def test_golden_expected_values_are_real_contract_values() -> None:
    catalog = default_catalog()
    real_values = {value for value, _column_id in catalog.value_surfaces()}
    for case in CASES:
        for value in case["expected_values"]:
            assert value in real_values, (case["id"], value)


# ---------------------------------------------------------------------------
# 真实检索：只有 REBUILD_INTEGRATION=1 才跑
# ---------------------------------------------------------------------------


@pytest.fixture(scope="module")
def recalled() -> dict[str, Any]:
    if not RUN_INTEGRATION:
        pytest.skip("设置 REBUILD_INTEGRATION=1 才运行真实 ES/Qdrant/Meta 集成测试")

    stack = RecallStack.open()
    try:
        health = stack.ping()
        if not all(health.values()):
            pytest.fail(
                "REBUILD_INTEGRATION=1 但服务不可用："
                f"{health}；请先启动 MySQL/ES/Qdrant 并 make stage2"
            )
        return {
            case["id"]: recall(case["question"], stack=stack)
            for case in CASES
        }
    finally:
        stack.close()


@pytest.mark.parametrize("case", CASES, ids=[case["id"] for case in CASES])
def test_golden_expected_sets_hit(case, recalled) -> None:
    state = recalled[case["id"]]
    catalog = default_catalog()

    actual_tables = set(state.table_names)
    actual_columns = set(state.column_ids)
    actual_metrics = set(state.metric_names)
    actual_values = set(state.value_strings)

    assert set(case["expected_tables"]) <= actual_tables
    assert set(case["expected_columns"]) <= actual_columns
    assert set(case["expected_metrics"]) <= actual_metrics
    assert set(case["expected_values"]) <= actual_values
    _must_not_check(case, state, catalog)

    # 允许额外候选，但不能把候选表/列整片塞进最终上下文。
    assert len(actual_tables) <= len(case["expected_tables"]) + 1
    assert len(actual_columns) <= len(case["expected_columns"]) + 4
    assert len(actual_metrics) <= max(1, len(case["expected_metrics"]))
    assert len(actual_values) <= len(case["expected_values"]) + 3

    # 最终上下文 precision：分母包含必需主外键，仍然要求足够高。
    assert _precision(case["expected_tables"], state.table_names) >= 0.75
    assert _precision(case["expected_columns"], state.column_ids) >= 0.75
    assert _precision(case["expected_metrics"], state.metric_names) >= 0.75
    assert _precision(case["expected_values"], state.value_strings) >= 0.5

    # recall@k：最终上下文按契约顺序输出，k 取 12；期望项必须全部在内。
    assert _recall_at_k(case["expected_columns"], state.column_ids, 12) == 1.0
    assert _recall_at_k(case["expected_metrics"], state.metric_names, 3) == 1.0
    assert _recall_at_k(case["expected_values"], state.value_strings, 10) == 1.0

    # 额外诊断：字段召回候选的 recall@12（只统计非主外键的语义列）。
    semantic_expected = [
        column_id
        for column_id in case["expected_columns"]
        if catalog.columns[column_id].role not in {"primary_key", "foreign_key"}
    ]
    if semantic_expected:
        top_columns = [candidate.id for candidate in state.column_candidates[:12]]
        assert _recall_at_k(semantic_expected, top_columns, 12) >= 0.5


def test_golden_roundtrips_are_batched(recalled) -> None:
    for case in CASES:
        trace = recalled[case["id"]].trace
        assert trace["es_roundtrips"] <= 2, case["id"]
        assert trace["qdrant_column_roundtrips"] == 1, case["id"]
        assert trace["qdrant_metric_roundtrips"] == 1, case["id"]
        assert trace["qdrant_roundtrips"] == 2, case["id"]
        assert trace["meta_roundtrips"] == 5, case["id"]


def test_golden_aggregate_recall_and_precision(recalled) -> None:
    column_precisions: list[float] = []
    table_precisions: list[float] = []
    metric_precisions: list[float] = []
    value_precisions: list[float] = []
    candidate_recalls: list[float] = []

    catalog = default_catalog()
    for case in CASES:
        state = recalled[case["id"]]
        column_precisions.append(_precision(case["expected_columns"], state.column_ids))
        table_precisions.append(_precision(case["expected_tables"], state.table_names))
        metric_precisions.append(_precision(case["expected_metrics"], state.metric_names))
        value_precisions.append(_precision(case["expected_values"], state.value_strings))

        semantic_expected = [
            column_id
            for column_id in case["expected_columns"]
            if catalog.columns[column_id].role not in {"primary_key", "foreign_key"}
        ]
        if semantic_expected:
            top_columns = [candidate.id for candidate in state.column_candidates[:12]]
            candidate_recalls.append(
                _recall_at_k(semantic_expected, top_columns, 12)
            )

    assert mean(column_precisions) >= 0.9
    assert mean(table_precisions) >= 0.9
    assert mean(metric_precisions) >= 0.9
    assert mean(value_precisions) >= 0.9
    assert mean(candidate_recalls) >= 0.85


def test_golden_yaml_preserves_contract_fields(recalled) -> None:
    """真实检索 YAML 里每个入选实体的契约字段都必须原样保留。"""
    catalog = default_catalog()
    state = recalled["q01"]
    payload = yaml.safe_load(render(state))

    assert payload["contract_version"] == catalog.contract_version
    assert payload["dataset"] == catalog.dataset

    for table_payload in payload["tables"]:
        table_spec = catalog.table(table_payload["name"])
        assert table_spec is not None
        assert table_payload["role"] == table_spec.role
        assert table_payload["description"] == table_spec.description
        assert table_payload["grain"] == table_spec.grain
        assert table_payload["primary_key"] == table_spec.primary_key
        assert table_payload["measures"] == [
            dict(item) for item in table_spec.measures
        ]
        # 过滤层允许裁掉不需要的 join 边，但保留下来的 FK 必须是契约里的。
        contract_fks = [dict(item) for item in table_spec.foreign_keys]
        for foreign_key in table_payload["foreign_keys"]:
            assert foreign_key in contract_fks

        for column_payload in table_payload["columns"]:
            column_spec = catalog.column(column_payload["id"])
            assert column_spec is not None
            assert column_payload["name"] == column_spec.name
            assert column_payload["table_id"] == column_spec.table_id
            assert column_payload["type"] == column_spec.type
            assert column_payload["role"] == column_spec.role
            assert column_payload["nullable"] == column_spec.nullable
            assert column_payload["description"] == column_spec.description
            assert column_payload["aliases"] == list(column_spec.aliases)
            assert column_payload["value_domain"] == column_spec.value_domain
            assert column_payload["references"] == catalog.references(
                column_payload["id"]
            )
            assert column_payload["examples_truncated"] is True
            assert column_payload["examples_limit"] == 10

    for metric_payload in payload["metrics"]:
        metric_spec = catalog.metric(metric_payload["name"])
        assert metric_spec is not None
        assert metric_payload["label"] == metric_spec.label
        assert metric_payload["description"] == metric_spec.description
        assert metric_payload["formula"] == metric_spec.formula
        assert metric_payload["relevant_columns"] == list(
            metric_spec.relevant_columns
        )
        assert metric_payload["aliases"] == list(metric_spec.aliases)
        assert metric_payload["aggregation"] == metric_spec.aggregation
        assert metric_payload["expected_value"] == metric_spec.expected_value
        assert metric_payload["grain"] == metric_spec.grain
        assert metric_payload["rejected_values"] == [
            dict(item) for item in metric_spec.rejected_values
        ]
