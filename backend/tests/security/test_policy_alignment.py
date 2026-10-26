"""S5 · 契约 / 代码 / 语料三方对齐。

这个仓库的核心命题是：

> 契约只写在提示词、YAML 和文档里，没有一行代码强制它。

所以 S5 必须**自己先做到**一致：策略契约、代码、注入语料必须是同一批字面量。
少一个方向，契约就开始漂移——而漂移正是这个项目要消灭的东西。

三方分别是：

    contracts/sql.policy.yaml          策略契约（唯一事实源）
    app/security/*.py                  代码里实际发出的码与参数
    fixtures/injection/*.yaml          注入语料里断言的码

另外还查一件更要紧的事：**策略参数不能在代码里再写一遍**。
`default_limit` 写在 YAML 里，代码里就不该出现字面量 `1000`——
否则契约改了就地失效，而门禁还在绿。
"""

from __future__ import annotations

import ast
import re
from pathlib import Path

from app.generate.output_schema import load_output_schema
from app.security.config import load_policy_config
from app.security.corpus import load_corpus
from app.security.executor import EXECUTOR_ERROR_CODES, SQL_REJECTED

SECURITY_DIR = Path(__file__).resolve().parents[2] / "app" / "security"

# 代码里发出原因码的唯一构造点。
_VIOLATION_RE = re.compile(r'PolicyViolation\(\s*"([A-Z][A-Z0-9_]*)"')


def _security_sources() -> list[Path]:
    return sorted(SECURITY_DIR.glob("*.py"))


def _code_reason_codes() -> set[str]:
    codes: set[str] = set()
    for path in _security_sources():
        codes.update(_VIOLATION_RE.findall(path.read_text(encoding="utf-8")))
    return codes


def _s4_vocabulary() -> set[str]:
    defs = load_output_schema()["$defs"]
    return set(defs["refusal_codes"]["items"]["properties"]["code"]["enum"])


# ---------------------------------------------------------------------------
# 原因码
# ---------------------------------------------------------------------------


def test_code_emits_exactly_the_contract_reason_codes() -> None:
    declared = set(load_policy_config().reason_codes)
    emitted = _code_reason_codes()
    assert emitted, "没扫到任何原因码，扫描规则可能失效了"
    missing = declared - emitted
    assert not missing, f"契约声明了但代码不会发出：{sorted(missing)}"
    extra = emitted - declared
    assert not extra, f"代码发出了契约未声明的原因码：{sorted(extra)}"


def test_executor_error_codes_match_the_contract() -> None:
    declared = set(load_policy_config().error_codes)
    assert set(EXECUTOR_ERROR_CODES) == declared, (
        f"执行器错误码与契约不一致："
        f"多出 {sorted(set(EXECUTOR_ERROR_CODES) - declared)}，"
        f"缺少 {sorted(declared - set(EXECUTOR_ERROR_CODES))}"
    )


def test_channel_code_is_the_rejection_code() -> None:
    """契约里的通道码必须就是代码里那个常量。"""
    assert load_policy_config().channel_code == SQL_REJECTED


def test_shared_reason_codes_exist_in_the_s4_vocabulary() -> None:
    """与 S4 共用的原因码，S4 契约里也得有——一件事只能有一个定义。"""
    s4 = _s4_vocabulary()
    shared = {
        "SQL_EMPTY",
        "SQL_SYNTAX",
        "SQL_MULTIPLE_STATEMENTS",
        "SQL_NOT_READ_ONLY",
    }
    assert shared <= set(load_policy_config().reason_codes)
    assert shared <= s4, f"S4 契约里缺少这些码：{sorted(shared - s4)}"


def test_s5_only_codes_are_not_in_the_s4_vocabulary() -> None:
    """S5 独有的码不能混进 S4 的词表——那会让 S4 的提示词对齐测试误判。"""
    s4 = _s4_vocabulary()
    s5_only = set(load_policy_config().reason_codes) - {
        "SQL_EMPTY",
        "SQL_SYNTAX",
        "SQL_MULTIPLE_STATEMENTS",
        "SQL_NOT_READ_ONLY",
    }
    assert not (s5_only & s4), (
        f"这些码同时出现在两个契约里，含义会各说各话：{sorted(s5_only & s4)}"
    )


def test_corpus_reason_codes_are_all_in_the_contract() -> None:
    """语料断言的原因码必须在契约里（load_corpus 已经查过，这里再钉一次）。"""
    declared = set(load_policy_config().reason_codes)
    for case in load_corpus().reject:
        assert case.reason_code in declared


# ---------------------------------------------------------------------------
# 策略参数不能在代码里再写一遍
# ---------------------------------------------------------------------------


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


def test_policy_parameters_are_read_from_the_contract() -> None:
    """契约里的每个参数都必须被代码读到。

    没人读的参数就是死参数：契约里写着「超时 5000 毫秒」，代码却没读它，
    那么改契约不会有任何效果——这正是本仓库要消灭的那种漂移。
    """
    sources = "\n".join(
        path.read_text(encoding="utf-8") for path in _security_sources()
    )
    parameters = (
        "single_statement",
        "allowed_roots",
        "forbid_set_operations",
        "forbid_executable_comments",
        "forbid_write_keywords",
        "forbid_dangerous_keywords",
        "default_limit",
        "max_limit",
        "max_execution_time_ms",
        "read_timeout_s",
        "connect_timeout_s",
        "account_user",
        "account_host",
        "account_database",
        "account_grants",
        "channel_code",
    )
    for name in parameters:
        assert f".{name}" in sources, f"契约里的 {name} 没有被任何代码读到"


def test_distinctive_policy_values_are_not_hardcoded_in_the_code() -> None:
    """有辨识度的策略值不能在代码里再写一遍。

    只查账号名、库名、超时毫秒这几个**不会撞车**的。行数上限 1000 不查：
    它同时也是「秒 → 毫秒」的换算系数，按值扫会误伤；那一项由上面的
    「参数被读到」覆盖。
    """
    config = load_policy_config()
    forbidden = {
        "max_execution_time_ms": str(config.max_execution_time_ms),
        "account_user": config.account_user,
        "account_database": config.account_database,
    }
    for path in _security_sources():
        constants = _module_constants(path)
        for name, value in forbidden.items():
            assert value not in constants, (
                f"{path.name} 里硬编码了 {name}={value!r}；"
                f"策略参数只能来自 contracts/sql.policy.yaml"
            )


def test_account_grants_are_only_select() -> None:
    """只读账号的授权只能是 SELECT——别的权限就是配置事故。"""
    config = load_policy_config()
    assert config.account_grants == ("SELECT",)
