"""S7 · 数字 / 日期类型保真（审计 B4）。

旧系统用 `json.dumps(..., default=str)` 兜底，于是 MySQL 的 `Decimal`
与 `date` 在进 JSON 时被悄悄改成字符串：同一行里出现
`{"销量": "10", "销售额": 62190.0}`。屏幕上看不出差别，复制出去才发现
数字变成了字符串。

这里钉住三件事：

    1. `Decimal("10")` → `10`（int），不是 `"10"`，也不是 `10.0`
    2. `date` → ISO 字符串，但列类型仍然是 `date`（类型从原始值推）
    3. 整条 result 负载可以**不带** `default=` 直接 json.dumps
"""

from __future__ import annotations

import datetime as dt
import json
from decimal import Decimal
from typing import Any

from app.agent.orchestrator import to_result_payload
from app.security.executor import ExecutionResult


def _payload(
    columns: tuple[str, ...], rows: tuple[dict[str, Any], ...]
) -> dict[str, Any]:
    execution = ExecutionResult(
        ok=True,
        sql="SELECT 1",
        columns=columns,
        rows=rows,
    )
    return to_result_payload(execution)


def test_decimal_integer_becomes_json_int() -> None:
    payload = _payload(
        ("地区", "销量"),
        ({"地区": "华北", "销量": Decimal("10")},),
    )

    value = payload["rows"][0][1]
    assert isinstance(value, int)
    assert not isinstance(value, bool)
    assert value == 10

    # 关键：不带 default= 也能序列化，而且出来的是 10 不是 "10"。
    encoded = json.dumps(payload, ensure_ascii=False)
    assert '"rows": [["华北", 10]]' in encoded


def test_decimal_fraction_becomes_float() -> None:
    payload = _payload(("销售额",), ({"销售额": Decimal("10.5")},))
    assert payload["rows"][0][0] == 10.5
    assert isinstance(payload["rows"][0][0], float)


def test_decimal_column_type_is_number() -> None:
    payload = _payload(("销量",), ({"销量": Decimal("10")},))
    assert payload["columns"][0]["type"] == "number"


def test_date_keeps_date_column_type() -> None:
    """先推类型、再归一化值——顺序反了日期就会变成 string。"""
    payload = _payload(("日期",), ({"日期": dt.date(2025, 3, 1)},))
    assert payload["columns"][0]["type"] == "date"
    assert payload["rows"][0][0] == "2025-03-01"


def test_datetime_keeps_date_column_type() -> None:
    payload = _payload(
        ("时间",), ({"时间": dt.datetime(2025, 3, 1, 12, 30, 5)},)
    )
    assert payload["columns"][0]["type"] == "date"
    assert payload["rows"][0][0] == "2025-03-01T12:30:05"


def test_bool_is_not_treated_as_number() -> None:
    """`isinstance(True, int)` 是 True，这个坑必须钉住。"""
    payload = _payload(("有退款",), ({"有退款": True},))
    assert payload["columns"][0]["type"] == "boolean"
    assert payload["rows"][0][0] is True


def test_all_null_column_is_typed_null() -> None:
    payload = _payload(("退款额",), ({"退款额": None},))
    assert payload["columns"][0]["type"] == "null"
    assert payload["rows"][0][0] is None


def test_whole_payload_serializes_without_default() -> None:
    """没有 Decimal / date 漏网：整条负载可以直接进 `_sse`。"""
    payload = _payload(
        ("地区", "销量", "日期", "二进制"),
        (
            {
                "地区": "华北",
                "销量": Decimal("10"),
                "日期": dt.date(2025, 3, 1),
                "二进制": b"\xe5\x8c\x97",
            },
        ),
    )
    encoded = json.dumps(payload, ensure_ascii=False)
    assert json.loads(encoded) == payload
