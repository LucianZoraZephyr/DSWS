"""S6 · 错误码表与各层词表的对齐。

这个仓库的核心命题：

> 契约只写在提示词、YAML 和文档里，没有一行代码强制它。

所以 S6 必须**自己先做到一致**。这里查五张表是不是同一批字面量：

    contracts/errors.yaml            错误码表（唯一事实源）
    app/generate/guard.py            S4 静态校验发出的码
    contracts/prompts/*.prompt       提示词里告诉模型的那批码
    app/security/executor.py         S5 执行器发出的码
    contracts/sql.policy.yaml        S5 策略契约声明的码

外加一条更要紧的：**校正参数不能在代码里再写一遍**。`max_attempts` 写在
契约里，代码里就不该出现字面量 `1`——否则契约改了就地失效，而门禁还在绿。
"""

from __future__ import annotations

import ast
import re
from pathlib import Path

from app.agent.errors import LLM_REFUSAL, channel_code_for_refusal, load_error_table
from app.generate.output_schema import load_output_schema
from app.security.config import load_policy_config
from app.security.executor import EXECUTOR_ERROR_CODES

BACKEND = Path(__file__).resolve().parents[2]
REPO = BACKEND.parent
AGENT_DIR = BACKEND / "app" / "agent"
PROMPTS_DIR = REPO / "contracts" / "prompts"

# 代码里发出违规码的唯一构造点。
_GUARD_VIOLATION_RE = re.compile(r'GuardViolation\(\s*"([A-Z][A-Z0-9_]*)"')

# 提示词正文里的码。
_PROMPT_CODE_RE = re.compile(r"\bSQL_[A-Z0-9_]+\b")


def _guard_codes() -> set[str]:
    source = (BACKEND / "app" / "generate" / "guard.py").read_text(encoding="utf-8")
    codes = set(_GUARD_VIOLATION_RE.findall(source))
    assert codes, "没扫到任何违规码，扫描规则可能失效了"
    return codes


def _prompt_codes() -> set[str]:
    codes: set[str] = set()
    for path in sorted(PROMPTS_DIR.glob("*.prompt")):
        codes.update(_PROMPT_CODE_RE.findall(path.read_text(encoding="utf-8")))
    return codes


def _refusal_codes() -> set[str]:
    defs = load_output_schema()["$defs"]
    return set(defs["refusal_codes"]["items"]["properties"]["code"]["enum"])


def _module_constants(path: Path) -> set[str]:
    """模块里的字面量常量（不含文档字符串）。"""
    tree = ast.parse(path.read_text(encoding="utf-8"))
    docstring_ids: set[int] = set()
    for node in ast.walk(tree):
        if isinstance(
            node,
            (ast.Module, ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef),
        ):
            body = getattr(node, "body", [])
            if (
                body
                and isinstance(body[0], ast.Expr)
                and isinstance(body[0].value, ast.Constant)
                and isinstance(body[0].value.value, str)
            ):
                docstring_ids.add(id(body[0].value))

    values: set[str] = set()
    for node in ast.walk(tree):
        if not isinstance(node, ast.Constant):
            continue
        if id(node) in docstring_ids:
            continue
        if isinstance(node.value, (str, int, float)) and not isinstance(
            node.value, bool
        ):
            values.add(str(node.value))
    return values


# ---------------------------------------------------------------------------
# 词表对齐
# ---------------------------------------------------------------------------


def test_guard_codes_are_all_in_the_error_table() -> None:
    """S4 静态校验发出的每一个码，错误码表里都得有。"""
    table = load_error_table()
    missing = sorted(_guard_codes() - set(table.codes))
    assert not missing, f"错误码表缺少 S4 的码：{missing}"


def test_prompt_codes_match_the_guard_codes() -> None:
    """提示词里告诉模型的那批码，必须就是校验器真的会发的那批。"""
    assert _prompt_codes() == _guard_codes()


def test_executor_codes_are_all_in_the_error_table() -> None:
    table = load_error_table()
    missing = sorted(set(EXECUTOR_ERROR_CODES) - set(table.codes))
    assert not missing, f"错误码表缺少 S5 执行器的码：{missing}"


def test_policy_contract_codes_are_all_in_the_error_table() -> None:
    """S5 策略契约声明的码也必须在同一张表里——一件事只能有一个定义。"""
    table = load_error_table()
    missing = sorted(set(load_policy_config().error_codes) - set(table.codes))
    assert not missing, f"错误码表缺少 S5 策略契约的码：{missing}"


def test_validation_codes_are_exactly_the_guard_codes() -> None:
    """错误码表与 S4 拒答码表的交集，必须正好是那 9 个静态校验码。

    交集之外的那些（OUT_OF_RANGE / UNSUPPORTED_INTENT / …）是**模型自己
    的判断**，它们统一映射到 LLM_REFUSAL，不占通道码。
    """
    table = load_error_table()
    shared = set(table.codes) & _refusal_codes()
    assert shared == _guard_codes(), (
        f"交集与静态校验码不一致：多出 {sorted(shared - _guard_codes())}，"
        f"缺少 {sorted(_guard_codes() - shared)}"
    )


def test_remaining_refusal_codes_map_to_llm_refusal() -> None:
    table = load_error_table()
    for code in _refusal_codes() - set(table.codes):
        assert channel_code_for_refusal(code) == LLM_REFUSAL


def test_every_error_code_is_reachable() -> None:
    """表里不该有「永远不会被发出」的码——那说明契约写多了。

    唯一的例外是 CANCELLED：它由取消接口触发，不经过生成与执行。
    """
    reachable = (
        _guard_codes()
        | set(EXECUTOR_ERROR_CODES)
        | {LLM_REFUSAL, "EMPTY_RESULT", "INTERNAL_ERROR", "CANCELLED"}
    )
    table = load_error_table()
    unreachable = sorted(set(table.codes) - reachable)
    assert not unreachable, f"这些码没有任何地方会发出：{unreachable}"


# ---------------------------------------------------------------------------
# SSE 契约
# ---------------------------------------------------------------------------


def test_sse_class_enum_matches_the_error_table() -> None:
    """前端按 class 决定怎么渲染，两边必须一致。"""
    import json

    schema = json.loads(
        (REPO / "contracts" / "sse.schema.json").read_text(encoding="utf-8")
    )
    enum = set(schema["$defs"]["error"]["properties"]["class"]["enum"])
    assert enum == set(load_error_table().classes)


# ---------------------------------------------------------------------------
# 校正参数不能在代码里再写一遍
# ---------------------------------------------------------------------------


def test_correction_parameters_are_read_from_the_contract() -> None:
    """契约里的每个参数都必须被代码读到。

    没人读的参数就是死参数：契约里写着「最多校正 1 次」，代码却没读它，
    那么改契约不会有任何效果。
    """
    sources = "\n".join(
        path.read_text(encoding="utf-8") for path in sorted(AGENT_DIR.glob("*.py"))
    )
    for name in ("max_attempts", "prompt", "revalidate", "revalidate_with"):
        assert f".{name}" in sources, f"契约里的 {name} 没有被任何代码读到"


def test_distinctive_correction_values_are_not_hardcoded() -> None:
    """有辨识度的值不能在代码里再写一遍。

    只查 `correct.py`——**渲染校正提示词**的只有它。`orchestrator.py` 里
    那个同名的常量是**步骤 id**（来自 steps.yaml），是两回事：旧系统管那个
    节点也叫 correct_sql，名字撞上了而已。
    """
    policy = load_error_table().correction
    constants = _module_constants(AGENT_DIR / "correct.py")
    assert policy.prompt not in constants, (
        f"correct.py 里硬编码了提示词名 {policy.prompt!r}；"
        f"校正参数只能来自 contracts/errors.yaml"
    )


def test_step_ids_used_by_the_orchestrator_come_from_the_catalog() -> None:
    """编排器用的步骤 id 必须是 steps.yaml 里声明过的。"""
    import yaml

    catalog = {
        step["id"]
        for step in yaml.safe_load(
            (REPO / "contracts" / "steps.yaml").read_text(encoding="utf-8")
        )["steps"]
    }
    from app.agent import orchestrator

    used = {
        orchestrator.GENERATE_STEP,
        orchestrator.VALIDATE_STEP,
        orchestrator.CORRECT_STEP,
        orchestrator.RUN_STEP,
    }
    assert used <= catalog, f"这些步骤 id 不在 steps.yaml 里：{sorted(used - catalog)}"
