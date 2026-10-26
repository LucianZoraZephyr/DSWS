"""S4 · 提示词契约的加载与槽位校验。

这一层直接对应审计里最刺眼的一条缺陷：

> 旧系统 7 个提示词的占位符契约**四方看起来一致**（模板占位符 = 代码声明的
> `input_variables` = `ainvoke` 传入的键 = 磁盘文件占位符），但**没有任何运行时
> 保障**——langchain-core 会用模板自动探测的结果**覆盖**节点里写的
> `input_variables`，那 7 处声明是**死代码**，拼错也不报错。

这里的做法相反：**槽位声明是唯一事实源，缺一个槽就直接抛异常**。

    contracts/prompts/*.prompt
      ├── frontmatter.slots        声明的输入槽位（唯一事实源）
      └── 正文里的 {占位符}         必须与 slots **完全一致**（双向校验）

两条不变式，加载时全部检查：

    1. 正文里出现的每个 `{占位符}`，都必须在 frontmatter 的 slots 里声明；
    2. frontmatter 里声明的每个 slot，都必须在正文里出现。

渲染时再验一次：**少一个槽 → `PromptSlotError`，不静默**。

提示词文件本身是资产，不是运行时依赖：7 个提示词里只有 `generate_sql` 的
`runtime: true`，其余 6 个标注为对照资产（旧系统的提示词，供审计与迁移
追溯）。它们照样被加载、照样被槽位校验——「不调用」不等于「不校验」。
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path
from typing import Any

import yaml

REBUILD_ROOT = Path(__file__).resolve().parents[3]
PROMPTS_DIR = REBUILD_ROOT / "contracts" / "prompts"
OUTPUT_SCHEMA_PATH = REBUILD_ROOT / "contracts" / "sql.output.schema.json"

# 正文里的占位符：必须是合法标识符形态，避免把 JSON 的 `{` 误判成槽位。
_PLACEHOLDER_RE = re.compile(r"\{([A-Za-z_][A-Za-z0-9_]*)\}")

_FRONTMATTER_RE = re.compile(
    r"\A---\r?\n(?P<meta>.*?)\r?\n---\r?\n(?P<body>.*)\Z",
    re.DOTALL,
)


class PromptError(Exception):
    """提示词契约本身有问题（文件缺字段、槽位与正文对不上等）。"""


class PromptSlotError(Exception):
    """渲染时缺槽位 / 多余槽位。旧系统在这里是静默的。"""


@dataclass(frozen=True)
class PromptSpec:
    """一个提示词的全部契约信息。"""

    name: str
    slots: tuple[str, ...]
    output_schema: str
    runtime: bool
    description: str
    hard_rules: tuple[str, ...]
    body: str
    path: Path

    def placeholders(self) -> set[str]:
        return set(_PLACEHOLDER_RE.findall(self.body))


def _parse_frontmatter(path: Path) -> tuple[dict[str, Any], str]:
    text = path.read_text(encoding="utf-8")
    match = _FRONTMATTER_RE.match(text)
    if not match:
        raise PromptError(f"{path.name} 缺少 YAML frontmatter（--- 包裹）")
    meta = yaml.safe_load(match.group("meta"))
    if not isinstance(meta, dict):
        raise PromptError(f"{path.name} 的 frontmatter 不是映射")
    return meta, match.group("body")


def _spec_from_file(path: Path) -> PromptSpec:
    meta, body = _parse_frontmatter(path)
    name = str(meta.get("name") or path.stem)
    if name != path.stem:
        raise PromptError(f"{path.name} 的 name={name!r} 与文件名不一致")

    slots = meta.get("slots")
    if not isinstance(slots, list) or not slots:
        raise PromptError(f"{path.name} 缺少非空的 slots 声明")
    slot_names = tuple(str(slot) for slot in slots)
    if len(set(slot_names)) != len(slot_names):
        raise PromptError(f"{path.name} 的 slots 有重复项")

    output_schema = str(meta.get("output_schema") or "")
    if not output_schema:
        raise PromptError(f"{path.name} 缺少 output_schema 声明")

    hard_rules = meta.get("hard_rules") or []
    if not isinstance(hard_rules, list):
        raise PromptError(f"{path.name} 的 hard_rules 必须是列表")

    return PromptSpec(
        name=name,
        slots=slot_names,
        output_schema=output_schema,
        runtime=bool(meta.get("runtime", False)),
        description=str(meta.get("description") or ""),
        hard_rules=tuple(str(item) for item in hard_rules),
        body=body,
        path=path,
    )


def _validate_spec(spec: PromptSpec) -> None:
    """槽位与正文必须双向一致；这是旧系统完全缺失的那道守卫。"""
    declared = set(spec.slots)
    used = spec.placeholders()

    missing_in_body = sorted(declared - used)
    if missing_in_body:
        raise PromptError(
            f"{spec.name}: slots 声明了但正文里没出现：{missing_in_body}"
        )

    undeclared = sorted(used - declared)
    if undeclared:
        raise PromptError(
            f"{spec.name}: 正文里出现未声明的占位符：{undeclared}"
        )

    known = known_output_schemas()
    if spec.output_schema not in known:
        raise PromptError(
            f"{spec.name}: output_schema={spec.output_schema!r} 不在 "
            f"contracts/sql.output.schema.json 的 $defs 里"
        )


@lru_cache(maxsize=1)
def load_output_schema() -> dict[str, Any]:
    """`contracts/sql.output.schema.json` 原文。"""
    return json.loads(OUTPUT_SCHEMA_PATH.read_text(encoding="utf-8"))


@lru_cache(maxsize=1)
def known_output_schemas() -> frozenset[str]:
    """契约里定义的全部输出形状名。"""
    defs = load_output_schema().get("$defs", {})
    return frozenset(defs)


@lru_cache(maxsize=1)
def load_prompts() -> dict[str, PromptSpec]:
    """加载并校验全部提示词；任何一个不合格都直接抛异常。"""
    if not PROMPTS_DIR.is_dir():
        raise PromptError(f"提示词目录不存在：{PROMPTS_DIR}")

    specs: dict[str, PromptSpec] = {}
    for path in sorted(PROMPTS_DIR.glob("*.prompt")):
        spec = _spec_from_file(path)
        _validate_spec(spec)
        specs[spec.name] = spec
    if not specs:
        raise PromptError(f"{PROMPTS_DIR} 下没有任何 .prompt 文件")
    return specs


def get_prompt(name: str) -> PromptSpec:
    specs = load_prompts()
    if name not in specs:
        raise PromptError(f"没有名为 {name!r} 的提示词；可用：{sorted(specs)}")
    return specs[name]


def render_prompt(name: str, values: dict[str, Any]) -> str:
    """渲染提示词。**缺槽位直接抛异常**，绝不静默补空串。

    多余的键是允许的（调用方常常一次准备一整套上下文），但声明过的槽位
    一个都不能少。
    """
    spec = get_prompt(name)

    missing = [slot for slot in spec.slots if slot not in values]
    if missing:
        raise PromptSlotError(
            f"{spec.name}: 缺少槽位 {missing}；声明的槽位是 {list(spec.slots)}"
        )

    rendered = spec.body
    for slot in spec.slots:
        value = values[slot]
        rendered = rendered.replace("{" + slot + "}", "" if value is None else str(value))
    return rendered


def prompt_slots(name: str) -> tuple[str, ...]:
    """某个提示词声明的槽位（按声明顺序）。"""
    return get_prompt(name).slots


def runtime_prompts() -> tuple[PromptSpec, ...]:
    """运行时真正会被调用的提示词。"""
    return tuple(
        spec for spec in load_prompts().values() if spec.runtime
    )
