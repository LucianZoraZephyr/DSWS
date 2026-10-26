"""S4 · 输出形状的契约校验。

`contracts/sql.output.schema.json` 是输出形状的唯一事实源。这里只做一件事：
把一份 payload 按 `sql_or_refusal` 校验一遍。

**为什么运行时也要校验**：模型（或任何生成器）都可能返回第三种形状——比如
旧系统里那句 `SELECT '无法生成SQL…' AS error_message`，把失败伪装成一行
数据。契约层不允许这种形状，所以要有一道**运行时**的校验，而不只是文档里
写一句「请输出 JSON」。
"""

from __future__ import annotations

from typing import Any

from jsonschema import Draft202012Validator

from app.generate.prompts import load_output_schema

# 生成器返回了不认识的形状时用的拒答码。
INVALID_OUTPUT = "INVALID_OUTPUT"


def _validator_for(schema_name: str) -> Draft202012Validator:
    root = load_output_schema()
    defs = root.get("$defs", {})
    if schema_name not in defs:
        raise KeyError(f"contracts/sql.output.schema.json 里没有 $defs.{schema_name}")
    return Draft202012Validator(
        {"$ref": f"#/$defs/{schema_name}", "$defs": defs}
    )


def validate_payload(payload: Any, schema_name: str = "sql_or_refusal") -> str | None:
    """按契约校验 payload。返回 None 表示通过，否则返回错误说明。"""
    validator = _validator_for(schema_name)
    errors = sorted(validator.iter_errors(payload), key=lambda e: list(e.path))
    if not errors:
        return None
    first = errors[0]
    location = "/".join(str(part) for part in first.path) or "<root>"
    return f"{location}: {first.message}"


def is_valid_payload(payload: Any, schema_name: str = "sql_or_refusal") -> bool:
    return validate_payload(payload, schema_name) is None
