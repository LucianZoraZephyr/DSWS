"""S7 · 契约生成物与契约本身必须一致。

这里防的是「抄本分家」：生成物一旦和契约不同步，前端就会按一份过期的
契约渲染。`contracts/` 是唯一事实源，生成物只是它的投影。
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from app.scripts import contracts_gen as cg

ROOT = Path(__file__).resolve().parents[3]
GENERATED_DIR = ROOT / "frontend" / "src" / "generated"
GENERATED_TS = GENERATED_DIR / "sse.ts"


def test_generated_files_are_fresh() -> None:
    """仓库里的生成物必须等于「现在重新生成」的结果。"""
    for path, expected in cg.build_all().items():
        actual = Path(path).read_text(encoding="utf-8")
        assert actual == expected, (
            f"{path} 与契约不同步。请运行："
            "cd backend && uv run python -m app.scripts.contracts_gen"
        )


def test_runtime_schema_is_contract_copy() -> None:
    """前端运行时校验用的 schema 必须是契约的逐字副本。"""
    generated = json.loads(
        (GENERATED_DIR / "sse.schema.json").read_text(encoding="utf-8")
    )
    assert generated == cg.load_sse_schema()


def test_error_class_enum_comes_from_errors_yaml() -> None:
    """SSE 契约里的 `error.class` 枚举 == 错误码表的 `classes`。"""
    schema = cg.load_sse_schema()
    class_enum = schema["$defs"]["error"]["properties"]["class"]["enum"]
    assert class_enum == cg.load_errors()["classes"]


def test_contract_json_matches_steps_and_errors() -> None:
    """给回放服务器用的 contract.json 必须来自 steps.yaml / errors.yaml。"""
    payload = json.loads(
        (GENERATED_DIR / "contract.json").read_text(encoding="utf-8")
    )
    assert payload["steps"] == cg.load_steps()
    assert set(payload["errors"]["codes"]) == set(cg.load_errors()["codes"])
    assert payload["errors"]["classes"] == cg.load_errors()["classes"]


def test_generated_ts_does_not_copy_the_class_enum_twice() -> None:
    """`failure | empty | cancelled` 只在 ErrorEvent 里出现一次。

    `ErrorClass` 必须从联合派生（`ErrorEvent["class"]`），不能手写第二份
    枚举——那正是「契约只写在文档里」的老毛病。
    """
    text = GENERATED_TS.read_text(encoding="utf-8")
    enum_literal = '"failure" | "empty" | "cancelled"'

    assert text.count(enum_literal) == 1, "枚举字面量被抄了不止一份"
    assert 'export type ErrorClass = ErrorEvent["class"];' in text
    assert 'export type ErrorClass = "failure"' not in text


def test_unknown_keyword_is_rejected() -> None:
    """生成器不认识的 JSON Schema 关键字 → 报错，而不是悄悄忽略。"""
    with pytest.raises(cg.GeneratorError, match="maxLength"):
        cg._check_keywords({"type": "object", "maxLength": 2})


def test_supported_keywords_pass() -> None:
    """子集之内的关键字必须正常通过（别把检查写得太严）。"""
    cg._check_keywords(
        {
            "type": "object",
            "properties": {
                "name": {"type": "string", "pattern": "^[a-z]+$"},
            },
        }
    )
