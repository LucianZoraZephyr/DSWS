"""S4 · 契约 / 提示词 / 代码三方对齐。

这个仓库的核心命题是：

> 契约只写在提示词、YAML 和文档里，没有一行代码强制它。

所以 S4 必须**自己先做到**三方一致：拒答码在契约、提示词、代码里必须是同一
批字面量。少一个方向，契约就开始漂移——而漂移正是这个项目要消灭的东西。

这里用源码扫描把这条不变式钉死：

    contracts/sql.output.schema.json  →  拒答码的权威列表
    contracts/prompts/*.prompt        →  提示词里逐字列出的拒答码
    app/generate/*.py                 →  代码里实际发出的拒答码

三者必须是同一个集合。
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

from app.generate.guard import check_sql
from app.generate.output_schema import load_output_schema
from app.generate.prompts import get_prompt, load_prompts

GENERATE_DIR = Path(__file__).resolve().parents[2] / "app" / "generate"

# 代码里发出拒答 / 违规码的两处构造点。
# 码既可能是字面量（Refusal("OUT_OF_RANGE", ...)），也可能是常量
# （Refusal(INVALID_OUTPUT, ...)），两种都要扫到。
_REFUSAL_RE = re.compile(
    r'Refusal\(\s*(?:"([A-Z][A-Z0-9_]*)"|([A-Z][A-Z0-9_]*))'
)
_VIOLATION_RE = re.compile(r'GuardViolation\(\s*"([A-Z][A-Z0-9_]*)"')

# 提示词里列拒答码的两种写法：「- CODE：说明」和「CODE / CODE」。
_PROMPT_CODE_RE = re.compile(r"\b([A-Z][A-Z0-9_]{3,})\b")


def _contract_codes() -> set[str]:
    defs = load_output_schema()["$defs"]
    enum = defs["refusal_codes"]["items"]["properties"]["code"]["enum"]
    return set(enum)


def _code_codes() -> set[str]:
    codes: set[str] = set()
    for path in sorted(GENERATE_DIR.glob("*.py")):
        text = path.read_text(encoding="utf-8")
        for literal, identifier in _REFUSAL_RE.findall(text):
            codes.add(literal or identifier)
        codes.update(_VIOLATION_RE.findall(text))
    return codes


def _prompt_codes() -> set[str]:
    """所有 `sql_or_refusal` 提示词里逐字出现过的码。"""
    codes: set[str] = set()
    for spec in load_prompts().values():
        if spec.output_schema != "sql_or_refusal":
            continue
        codes.update(_PROMPT_CODE_RE.findall(spec.body))
    return codes


def test_contract_lists_the_full_code_set() -> None:
    codes = _contract_codes()
    # 生成器主动拒答
    assert {
        "OUT_OF_RANGE",
        "UNSUPPORTED_INTENT",
        "UNKNOWN_COLUMN",
        "UNKNOWN_TABLE",
        "INSUFFICIENT_CONTEXT",
    } <= codes
    # 静态校验判定
    assert {
        "SQL_MULTIPLE_STATEMENTS",
        "SQL_NOT_READ_ONLY",
        "SQL_UNKNOWN_TABLE",
        "SQL_UNKNOWN_COLUMN",
        "SQL_CONSTANT_PROJECTION",
        "SQL_MISSING_ORDER_BY",
        "SQL_MISSING_LIMIT",
        "SQL_SYNTAX",
        "SQL_EMPTY",
    } <= codes
    assert "INVALID_OUTPUT" in codes


def test_every_code_emitted_by_code_is_in_the_contract() -> None:
    """代码里发出的每一个码，契约里都必须有。"""
    emitted = _code_codes()
    assert emitted, "没扫到任何拒答码，扫描规则可能失效了"
    missing = emitted - _contract_codes()
    assert not missing, f"代码里发出了契约未定义的拒答码：{sorted(missing)}"


def test_every_contract_code_is_used_somewhere() -> None:
    """契约里的每一个码，要么代码会发出来，要么提示词会用到——不能是摆设。"""
    used = _code_codes() | _prompt_codes()
    unused = _contract_codes() - used
    assert not unused, f"契约里定义了但没人用的拒答码：{sorted(unused)}"


@pytest.mark.parametrize("name", sorted(load_prompts()))
def test_prompt_mentions_every_contract_code(name: str) -> None:
    """提示词必须逐字列出契约里的每一个拒答码。"""
    spec = get_prompt(name)
    if spec.output_schema != "sql_or_refusal":
        pytest.skip(f"{name} 的输出形状不是 sql_or_refusal")
    body_codes = set(_PROMPT_CODE_RE.findall(spec.body))
    missing = _contract_codes() - body_codes
    assert not missing, f"{name} 的提示词里没有提到拒答码：{sorted(missing)}"


def test_guard_emits_exactly_the_declared_sql_codes() -> None:
    """静态校验发出的违规码就是契约里那批 SQL_* 码。"""
    from app.generate.context import build_offline_context

    context = build_offline_context("销售额")
    probes = [
        ("SELECT 1 AS a FROM fact_order", "SQL_CONSTANT_PROJECTION"),
        ("SELECT fact_order.nope FROM fact_order", "SQL_UNKNOWN_COLUMN"),
        ("SELECT nope.a FROM nope", "SQL_UNKNOWN_TABLE"),
        ("UPDATE fact_order SET order_amount = 0", "SQL_NOT_READ_ONLY"),
        ("SELECT 1 FROM fact_order; SELECT 2 FROM fact_order", "SQL_MULTIPLE_STATEMENTS"),
        ("SELEC 1 FROM fact_order", "SQL_SYNTAX"),
        ("", "SQL_EMPTY"),
    ]
    for sql, expected in probes:
        report = check_sql(sql, context, "销售额")
        codes = {item.code for item in report.violations}
        assert expected in codes, f"{sql!r} 期望 {expected}，实际 {codes}"
