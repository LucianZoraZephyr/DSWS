"""S7 · 从契约生成前端类型。

    cd backend && uv run python -m app.scripts.contracts_gen          # 生成
    cd backend && uv run python -m app.scripts.contracts_gen --check  # 校验新鲜度

生成物：

    frontend/src/generated/sse.ts           TS 类型（事件联合 + 派生别名）
    frontend/src/generated/sse.schema.json  SSE 契约的副本（前端运行时校验用）
    frontend/src/generated/contract.json    步骤目录 + 错误码表（给回放服务器用）

为什么要生成，而不是手写：

    审计的第一句结论是「契约只写在提示词、YAML 和文档里，没有一行代码
    强制它」。前端手写一份 TS 类型，等于把同一个契约抄了第二遍——抄本
    迟早和原文对不上。所以类型**从契约生成**，并且用 `--check` 盯着
    新鲜度：契约改了而没重新生成，`make stage7` 直接红。

生成器支持 JSON Schema 的一个子集（正好是 `contracts/sse.schema.json`
用到的那些）：

    $ref / $defs · type · enum · const · required · properties ·
    additionalProperties · items · pattern · minimum · oneOf · allOf(if/then/else)

契约里写了子集之外的关键字，生成器会**报错**而不是悄悄忽略——那正是
「契约没人管」的开始。
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from collections.abc import Sequence
from pathlib import Path
from typing import Any

import yaml

from app.cli import enable_utf8_stdio

REBUILD_ROOT = Path(__file__).resolve().parents[3]
CONTRACTS_DIR = REBUILD_ROOT / "contracts"
FRONTEND_GENERATED = REBUILD_ROOT / "frontend" / "src" / "generated"

SSE_SCHEMA_PATH = CONTRACTS_DIR / "sse.schema.json"
STEPS_PATH = CONTRACTS_DIR / "steps.yaml"
ERRORS_PATH = CONTRACTS_DIR / "errors.yaml"

#: 生成器认识的 JSON Schema 关键字。契约里出现别的关键字就报错。
SUPPORTED_KEYWORDS = frozenset(
    {
        "$schema",
        "$id",
        "$defs",
        "$ref",
        "title",
        "description",
        "type",
        "enum",
        "const",
        "required",
        "properties",
        "additionalProperties",
        "items",
        "pattern",
        "minimum",
        "maximum",
        "oneOf",
        "allOf",
        "if",
        "then",
        "else",
    }
)

#: 值是「用户起的名字 → schema」的映射型关键字。
MAP_VALUED_KEYWORDS = frozenset({"$defs", "properties"})


class GeneratorError(Exception):
    """生成器遇到看不懂的契约。"""


# ---------------------------------------------------------------------------
# 命名
# ---------------------------------------------------------------------------

#: 这些关键字不参与命名。
_IGNORED = frozenset({"type", "enum", "const", "items", "required"})


def _pascal(text: str) -> str:
    """`region_name` -> `RegionName`。"""
    parts = re.split(r"[^0-9A-Za-z]+", text)
    return "".join(part[:1].upper() + part[1:] for part in parts if part)


def _singular(text: str) -> str:
    """`columns` -> `column`。只做最朴素的去 s。"""
    if text.endswith("ss") or not text.endswith("s"):
        return text
    return text[:-1]


# ---------------------------------------------------------------------------
# 契约读取
# ---------------------------------------------------------------------------


def load_sse_schema() -> dict[str, Any]:
    return json.loads(SSE_SCHEMA_PATH.read_text(encoding="utf-8"))


def load_steps() -> dict[str, Any]:
    return yaml.safe_load(STEPS_PATH.read_text(encoding="utf-8"))


def load_errors() -> dict[str, Any]:
    return yaml.safe_load(ERRORS_PATH.read_text(encoding="utf-8"))


# ---------------------------------------------------------------------------
# 关键字检查
# ---------------------------------------------------------------------------


def _check_keywords(node: Any, path: str = "$") -> None:
    """契约里出现生成器不认识的关键字 → 报错。

    悄悄忽略一个关键字，等于让契约的一部分失去强制力——这正是整轮重建
    要消灭的毛病。
    """
    if isinstance(node, dict):
        for key, value in node.items():
            if key in MAP_VALUED_KEYWORDS:
                # 这些关键字下面的一层是**用户起的名字**（定义名 / 属性名），
                # 不是 JSON Schema 关键字。
                for name, sub in value.items():
                    _check_keywords(sub, f"{path}.{key}.{name}")
                continue
            if key not in SUPPORTED_KEYWORDS:
                raise GeneratorError(
                    f"{path} 出现了生成器不认识的 JSON Schema 关键字：{key!r}。"
                    f"请在 contracts_gen.py 里支持它，或改契约。"
                )
            _check_keywords(value, f"{path}.{key}")
    elif isinstance(node, list):
        for index, item in enumerate(node):
            _check_keywords(item, f"{path}[{index}]")


# ---------------------------------------------------------------------------
# TS 类型推导
# ---------------------------------------------------------------------------


class TypeBuilder:
    """把 JSON Schema 的一小段变成 TS 类型。

    命名规则（确定性，靠测试钉住）：

        顶层分支        <Def>Event           （progress -> ProgressEvent）
        嵌套对象        <Def><路径>          （result.data -> ResultData）
        数组的元素      <Def><路径><单数>    （result.data.columns[] -> ResultDataColumn）

    别名一律指代「**一个**东西的类型」：数组元素另起一个别名，于是
    `ResultDataColumn[]` 是列的数组，`ResultDataRow` 是「一个单元格」。
    """

    def __init__(self, schema: dict[str, Any]) -> None:
        self.schema = schema
        self.defs: dict[str, Any] = schema.get("$defs", {})
        #: 接口（对象）源码
        self.interfaces: dict[str, str] = {}
        #: 类型别名源码
        self.aliases: dict[str, str] = {}

    # -- 入口 -------------------------------------------------------------

    def build(self) -> tuple[list[str], str]:
        """返回（顶层接口的源码块，联合类型源码）。"""
        blocks: list[str] = []
        union: list[str] = []

        for ref in self.schema["oneOf"]:
            def_name = ref["$ref"].rsplit("/", 1)[-1]
            branch = self.defs[def_name]
            alias = f"{_pascal(def_name)}Event"
            body = self._object_interface(
                branch, alias, base=_pascal(def_name), path=[]
            )
            blocks.append(body)
            union.append(alias)

        union_type = " | ".join(union)
        return blocks, union_type

    # -- 内部 -------------------------------------------------------------

    def _object_interface(
        self, schema: dict[str, Any], alias: str, base: str, path: list[str]
    ) -> str:
        """一个 JSON Schema 对象 → 接口源码。"""
        properties: dict[str, Any] = schema.get("properties", {})
        required: list[str] = schema.get("required", [])

        lines: list[str] = []
        for name, sub in properties.items():
            ts_type = self._type_of(sub, base, path + [name])
            optional = "" if name in required else "?"
            description = sub.get("description")
            if description:
                text = description.replace("\n", " ").strip()
                if len(text) > 96:
                    text = text[:96] + "…"
                lines.append(f"  /** {text} */")
            lines.append(f"  {name}{optional}: {ts_type};")

        return f"export interface {alias} {{\n" + "\n".join(lines) + "\n}"

    def _register(self, alias: str, body: str) -> None:
        if alias in self.interfaces:
            return
        self.interfaces[alias] = body

    def _type_of(
        self,
        schema: dict[str, Any],
        base: str,
        path: list[str],
        forced_alias: str | None = None,
    ) -> str:
        """一段 schema → TS 类型文本。

        嵌套类型会**先**被注册，保证声明顺序在引用之前。
        """
        if "const" in schema:
            return _literal(schema["const"])

        if "enum" in schema:
            return " | ".join(_literal(item) for item in schema["enum"])

        schema_type = schema.get("type")
        types = (
            list(schema_type)
            if isinstance(schema_type, list)
            else ([schema_type] if schema_type else [])
        )

        if not types:
            # 没有 type 也没有 const/enum：只可能是 allOf 之类的组合。
            return "unknown"

        if "object" in types and "properties" in schema:
            alias = forced_alias or (
                base + "".join(_pascal(part) for part in path)
            )
            self._register(
                alias, self._object_interface(schema, alias, base, path)
            )
            return alias

        if "array" in types:
            item_schema = schema.get("items", {})
            element_name = _singular(path[-1])
            alias = forced_alias or (
                base
                + "".join(_pascal(part) for part in path[:-1])
                + _pascal(element_name)
            )
            item_is_array = "array" in (item_schema.get("type") or [])
            item_type = self._type_of(
                item_schema,
                base,
                path + [element_name],
                # 数组套数组：内层元素叫 <外层别名>Value
                # （rows 的一行是数组，一行里的东西是 value）。
                forced_alias=alias + "Value" if item_is_array else alias,
            )
            if item_type != alias and alias not in self.aliases:
                # 元素不是具名类型（标量联合）：给元素起一个别名，
                # 于是「一个单元格」也有名字（CellValue 就是从它派生的）。
                self.aliases[alias] = f"export type {alias} = {item_type};"
            return f"{alias}[]"

        # 标量
        mapping = {
            "string": "string",
            "number": "number",
            "integer": "number",
            "boolean": "boolean",
            "null": "null",
        }
        parts = [mapping.get(item, "unknown") for item in types]
        return " | ".join(parts)


def _literal(value: Any) -> str:
    if isinstance(value, str):
        return json.dumps(value, ensure_ascii=False)
    if value is None:
        return "null"
    if isinstance(value, bool):
        return "true" if value else "false"
    return json.dumps(value)


# ---------------------------------------------------------------------------
# 生成 TS
# ---------------------------------------------------------------------------


def render_ts(schema: dict[str, Any]) -> str:
    """生成 `frontend/src/generated/sse.ts` 的内容。"""
    builder = TypeBuilder(schema)
    blocks, union_type = builder.build()

    # 顶层事件名（用于派生别名）
    event_names = [
        ref["$ref"].rsplit("/", 1)[-1] for ref in schema["oneOf"]
    ]

    defs = schema.get("$defs", {})

    def _def_of(event_name: str) -> dict[str, Any]:
        return defs[event_name]

    progress_props = _def_of("progress")["properties"]
    result_props = _def_of("result")["properties"]
    error_props = _def_of("error")["properties"]

    status_enum = progress_props["status"]["enum"]
    column_type_enum = result_props["data"]["properties"]["columns"]["items"][
        "properties"
    ]["type"]["enum"]
    error_class_enum = error_props["class"]["enum"]

    event_types = [
        _def_of(name)["properties"]["type"]["const"] for name in event_names
    ]

    parts: list[str] = []
    parts.append(
        """/**
 * 由 `python -m app.scripts.contracts_gen` 从 `contracts/sse.schema.json` 生成。
 *
 * **不要手改这个文件。**
 *
 * 契约改了，就重跑生成器；`make stage7` 会用 `--check` 盯着新鲜度。
 * 手写一份 TS 类型等于把契约抄第二遍——抄本迟早和原文对不上，
 * 而前端类型对不上 = 界面按错误的契约渲染。
 *
 * 生成的类型：
 *   - 事件联合（判别联合，`type` 是判别字段）
 *   - 从联合**派生**出来的别名（ProgressStatus / ErrorClass / ...）
 *
 * 运行时校验不在这里：它由 `sse.schema.json` 的副本驱动
 * （`src/lib/json-schema.ts` 是一台通用 JSON Schema 校验器，
 * 契约本身仍然是唯一事实源）。
 */
"""
    )

    parts.append("\n".join(blocks))
    parts.append("")
    parts.append("\n".join(builder.interfaces.values()))
    parts.append("")
    parts.append("\n".join(builder.aliases.values()))
    parts.append("")
    parts.append(f"export type AgentEvent = {union_type};")
    parts.append("")
    parts.append(
        "/** 契约里声明的全部事件类型。收到不在这里面的 type 是「后端加了新事件」，"
    )
    parts.append(" *  不是「这条消息坏了」——前者忽略，后者报错。 */")
    parts.append(
        "export const EVENT_TYPES = ["
        + ", ".join(json.dumps(item, ensure_ascii=False) for item in event_types)
        + "] as const;"
    )
    parts.append("")
    parts.append("export type EventType = (typeof EVENT_TYPES)[number];")
    parts.append("")
    parts.append("/* ---- 从联合派生的别名：一个事实源，不抄第二遍 ---- */")
    parts.append('export type ProgressStatus = ProgressEvent["status"];')
    parts.append('export type ErrorClass = ErrorEvent["class"];')
    parts.append('export type ColumnType = ResultDataColumn["type"];')
    parts.append("export type CellValue = ResultDataRowValue;")
    parts.append("")

    # 类型别名（ProgressStatus 等）已经由联合派生，这里不再重复枚举，
    # 但把它们列出来是为了让读者看见「这些取值都来自契约」。
    parts.append("/* 契约里出现的枚举取值（只作阅读参考，类型请用上面的别名）： */")
    parts.append(
        f"// progress.status: {', '.join(json.dumps(v, ensure_ascii=False) for v in status_enum)}"
    )
    parts.append(
        f"// column.type:    {', '.join(json.dumps(v, ensure_ascii=False) for v in column_type_enum)}"
    )
    parts.append(
        f"// error.class:    {', '.join(json.dumps(v, ensure_ascii=False) for v in error_class_enum)}"
    )
    parts.append("")

    return "\n".join(parts).rstrip() + "\n"


# ---------------------------------------------------------------------------
# 生成 contract.json
# ---------------------------------------------------------------------------


def render_contract() -> str:
    """步骤目录 + 错误码表 → 一份 JSON，给回放服务器用。"""
    steps = load_steps()
    errors = load_errors()
    payload = {
        "steps": steps,
        "errors": {
            "version": errors["version"],
            "classes": errors["classes"],
            "codes": {
                code: {
                    "label": spec["label"],
                    "class": spec["class"],
                    "retryable": spec["retryable"],
                    "message": spec["message"],
                }
                for code, spec in errors["codes"].items()
            },
        },
    }
    return json.dumps(payload, ensure_ascii=False, indent=2) + "\n"


# ---------------------------------------------------------------------------
# 入口
# ---------------------------------------------------------------------------


def build_all() -> dict[str, str]:
    """生成全部文件：路径 -> 内容。"""
    schema = load_sse_schema()
    _check_keywords(schema)
    return {
        str(FRONTEND_GENERATED / "sse.ts"): render_ts(schema),
        str(FRONTEND_GENERATED / "sse.schema.json"): json.dumps(
            schema, ensure_ascii=False, indent=2
        )
        + "\n",
        str(FRONTEND_GENERATED / "contract.json"): render_contract(),
    }


def _write(files: dict[str, str]) -> None:
    for path, content in files.items():
        target = Path(path)
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(content, encoding="utf-8", newline="\n")


def _check(files: dict[str, str]) -> list[str]:
    """返回「已经过期」的文件列表。"""
    stale: list[str] = []
    for path, content in files.items():
        target = Path(path)
        if not target.exists():
            stale.append(f"{target} 不存在")
            continue
        if target.read_text(encoding="utf-8") != content:
            stale.append(f"{target} 与契约不同步")
    return stale


def main(argv: Sequence[str] | None = None) -> int:
    enable_utf8_stdio()
    parser = argparse.ArgumentParser(
        prog="python -m app.scripts.contracts_gen",
        description="S7：从 contracts/ 生成前端类型",
    )
    parser.add_argument(
        "--check",
        action="store_true",
        help="只校验新鲜度，不写文件（生成物过期就退出码 1）",
    )
    args = parser.parse_args(argv)

    try:
        files = build_all()
    except GeneratorError as exc:
        print(f"FAIL: {exc}", file=sys.stderr)
        return 1

    if args.check:
        stale = _check(files)
        if stale:
            print("FAIL: 生成物与契约不同步：", file=sys.stderr)
            for item in stale:
                print(f"  - {item}", file=sys.stderr)
            print(
                "\n请运行：cd backend && uv run python -m app.scripts.contracts_gen",
                file=sys.stderr,
            )
            return 1
        print(f"S7 CONTRACT GEN PASS（{len(files)} 个文件与契约一致）")
        return 0

    _write(files)
    for path in files:
        print(f"已生成 {Path(path).relative_to(REBUILD_ROOT).as_posix()}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
