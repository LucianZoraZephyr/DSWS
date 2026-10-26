"""S7 · 单元格值归一化（`app.agent.values`）。

规则只有一条：**显式**归一化，不降级。每一种类型都有自己的去处；
没见过的类型抛 `TypeError`，让调用方把它变成 `INTERNAL_ERROR`——
宁可整次问数失败，也不要交给用户一份被悄悄改过类型的数据。
"""

from __future__ import annotations

import datetime as dt
from decimal import Decimal

import pytest

from app.agent.values import normalize_cell, normalize_value


def test_none_and_scalars_pass_through() -> None:
    assert normalize_cell(None) is None
    assert normalize_cell("华北") == "华北"
    assert normalize_cell(10) == 10
    assert normalize_cell(10.5) == 10.5


def test_bool_is_preserved_as_bool() -> None:
    """`isinstance(True, int)` 是 True——布尔必须原样保留。"""
    assert normalize_cell(True) is True
    assert normalize_cell(False) is False


def test_decimal_integral_becomes_int() -> None:
    value = normalize_cell(Decimal("10"))
    assert value == 10
    assert isinstance(value, int)
    assert not isinstance(value, bool)


def test_decimal_negative_integral_becomes_int() -> None:
    value = normalize_cell(Decimal("-3"))
    assert value == -3
    assert isinstance(value, int)


def test_decimal_fraction_becomes_float() -> None:
    value = normalize_cell(Decimal("10.5"))
    assert value == 10.5
    assert isinstance(value, float)


def test_date_becomes_iso_string() -> None:
    assert normalize_cell(dt.date(2025, 3, 1)) == "2025-03-01"


def test_datetime_becomes_iso_string() -> None:
    assert (
        normalize_cell(dt.datetime(2025, 3, 1, 12, 30, 5))
        == "2025-03-01T12:30:05"
    )


def test_time_becomes_iso_string() -> None:
    assert normalize_cell(dt.time(12, 30, 5)) == "12:30:05"


def test_timedelta_becomes_seconds() -> None:
    assert normalize_cell(dt.timedelta(seconds=90)) == 90.0


def test_bytes_utf8_becomes_text() -> None:
    assert normalize_cell("北".encode("utf-8")) == "北"


def test_bytes_non_utf8_becomes_hex() -> None:
    assert normalize_cell(b"\xff\x00") == "ff00"


def test_nested_structures_are_normalized() -> None:
    value = normalize_cell(
        [Decimal("1"), {"date": dt.date(2025, 3, 1), "rest": [Decimal("2.5")]}]
    )
    assert value == [1, {"date": "2025-03-01", "rest": [2.5]}]


def test_unknown_type_raises_instead_of_degrading() -> None:
    with pytest.raises(TypeError, match="无法归一化的单元格类型"):
        normalize_cell(object())


def test_normalize_value_uses_the_same_rules() -> None:
    assert normalize_value(Decimal("10")) == 10
