"""S6 · 错误码表与通道错误。

这一层的命题：**每一种失败都有名字**。

所以这里查三件事：

    1. 表是严格的——少一个键、多一个键、类别非法，全部抛异常；
    2. 通道错误只有一个形状，且事件符合 `contracts/sse.schema.json`；
    3. 拒答码的映射规则是「在表里就是它自己，不在就是 LLM_REFUSAL」。
"""

from __future__ import annotations

import json
from pathlib import Path

import jsonschema
import pytest
import yaml

from app.agent.errors import (
    CANCELLED,
    EMPTY,
    FAILURE,
    LLM_REFUSAL,
    ROADMAP_CODES,
    ChannelError,
    ErrorTableError,
    build_error,
    channel_code_for_refusal,
    load_error_table,
    parse_error_table,
)

ROOT = Path(__file__).resolve().parents[3]
SSE_SCHEMA = json.loads(
    (ROOT / "contracts" / "sse.schema.json").read_text(encoding="utf-8")
)


def _valid_table() -> dict:
    return yaml.safe_load(
        (ROOT / "contracts" / "errors.yaml").read_text(encoding="utf-8")
    )


# ---------------------------------------------------------------------------
# 表本身
# ---------------------------------------------------------------------------


def test_table_loads() -> None:
    table = load_error_table()
    assert table.version == 1
    assert table.codes


def test_roadmap_codes_are_all_present() -> None:
    """路线图 S6 点名的 7 个码必须都在表里。"""
    table = load_error_table()
    missing = [code for code in ROADMAP_CODES if code not in table]
    assert not missing, f"错误码表缺少路线图要求的码：{missing}"


def test_classes_are_exactly_three() -> None:
    table = load_error_table()
    assert set(table.classes) == {FAILURE, EMPTY, CANCELLED}


def test_every_code_has_a_class_and_a_message() -> None:
    for spec in load_error_table().specs:
        assert spec.klass in load_error_table().classes
        assert spec.label
        assert spec.message


def test_security_rejection_is_never_retryable() -> None:
    """策略拒绝不能进校正闭环——那等于给注入开口子。"""
    table = load_error_table()
    assert table.get("SQL_REJECTED").retryable is False


# ---------------------------------------------------------------------------
# 严格性
# ---------------------------------------------------------------------------


def test_missing_key_is_rejected() -> None:
    data = _valid_table()
    data["codes"]["SQL_REJECTED"].pop("retryable")
    with pytest.raises(ErrorTableError):
        parse_error_table(data)


def test_unknown_key_is_rejected() -> None:
    data = _valid_table()
    data["codes"]["SQL_REJECTED"]["extra"] = 1
    with pytest.raises(ErrorTableError):
        parse_error_table(data)


def test_unknown_class_is_rejected() -> None:
    data = _valid_table()
    data["codes"]["SQL_REJECTED"]["class"] = "boom"
    with pytest.raises(ErrorTableError):
        parse_error_table(data)


def test_missing_roadmap_code_is_rejected() -> None:
    data = _valid_table()
    data["codes"].pop("CANCELLED")
    with pytest.raises(ErrorTableError):
        parse_error_table(data)


def test_correction_policy_is_validated() -> None:
    data = _valid_table()
    data["correction"]["max_attempts"] = 0
    with pytest.raises(ErrorTableError):
        parse_error_table(data)


def test_correction_gates_are_known() -> None:
    data = _valid_table()
    data["correction"]["revalidate_with"] = ["guard", "wat"]
    with pytest.raises(ErrorTableError):
        parse_error_table(data)


# ---------------------------------------------------------------------------
# 通道错误
# ---------------------------------------------------------------------------


def test_build_error_rejects_unknown_code() -> None:
    """码不在表里 → 直接抛异常。失败不能没有名字。"""
    with pytest.raises(ErrorTableError):
        build_error("NOT_A_REAL_CODE", "x")


def test_channel_error_event_matches_the_contract() -> None:
    error = build_error(
        "SQL_REJECTED",
        "SQL 未通过安全策略校验。",
        step="validate_sql",
        reason_code="SQL_TABLE_NOT_ALLOWED",
        original_sql="SELECT 1",
        attempts=0,
    )
    event = error.to_event()
    jsonschema.validate(instance=event, schema=SSE_SCHEMA)
    assert event["type"] == "error"
    assert event["code"] == "SQL_REJECTED"
    assert event["class"] == FAILURE
    assert event["step"] == "validate_sql"
    assert event["detail"]["reason_code"] == "SQL_TABLE_NOT_ALLOWED"
    assert event["detail"]["original_sql"] == "SELECT 1"


def test_error_event_without_class_is_rejected() -> None:
    """`class` 是必填——前端靠它区分「红框」「空态」「取消」。"""
    with pytest.raises(jsonschema.ValidationError):
        jsonschema.validate(
            instance={
                "type": "error",
                "code": "SQL_REJECTED",
                "message": "x",
                "step": None,
            },
            schema=SSE_SCHEMA,
        )


def test_empty_result_is_not_a_failure_class() -> None:
    """空数组是合法语义，不是失败（审计 FP-8）。"""
    error = build_error("EMPTY_RESULT")
    assert error.klass == EMPTY
    assert error.retryable is False


def test_cancelled_is_its_own_class() -> None:
    error = build_error("CANCELLED")
    assert error.klass == CANCELLED


def test_detail_drops_unknown_keys() -> None:
    """detail 的形状由契约定死，多余的键不能漏出去。"""
    error = ChannelError(
        code="SQL_REJECTED",
        message="x",
        detail={"nope": 1, "reason_code": "SQL_EMPTY"},
    )
    event = error.to_event()
    assert set(event["detail"]) == {"reason_code"}
    jsonschema.validate(instance=event, schema=SSE_SCHEMA)


# ---------------------------------------------------------------------------
# 映射规则
# ---------------------------------------------------------------------------


def test_refusal_code_in_table_maps_to_itself() -> None:
    assert channel_code_for_refusal("SQL_SYNTAX") == "SQL_SYNTAX"
    assert channel_code_for_refusal("SQL_UNKNOWN_COLUMN") == "SQL_UNKNOWN_COLUMN"


def test_refusal_code_outside_table_maps_to_llm_refusal() -> None:
    for code in (
        "OUT_OF_RANGE",
        "UNSUPPORTED_INTENT",
        "UNKNOWN_COLUMN",
        "UNKNOWN_TABLE",
        "INSUFFICIENT_CONTEXT",
        "INVALID_OUTPUT",
    ):
        assert channel_code_for_refusal(code) == LLM_REFUSAL
