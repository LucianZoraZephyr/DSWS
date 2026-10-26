"""S5 · 策略契约的加载与校验。

`contracts/sql.policy.yaml` 是「SQL 能不能执行」的唯一事实源。这里把它读成
一个**严格的**配置对象：

    - 少一个键 → 抛异常（缺键意味着某条规则悄悄失效了）
    - 多一个键 → 抛异常（多出来的键说明契约与代码漂移了）
    - 值不合理 → 抛异常（比如 default_limit > max_limit）

严格是有意的。策略参数如果可以被「写错但不报错」，那么它就不是契约，
只是一段建议——而这正是这个仓库要消灭的东西。
"""

from __future__ import annotations

from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path
from typing import Any

import yaml

REBUILD_ROOT = Path(__file__).resolve().parents[3]
POLICY_PATH = REBUILD_ROOT / "contracts" / "sql.policy.yaml"


class PolicyConfigError(Exception):
    """策略契约本身有问题（缺键、多键、值不合理）。"""


@dataclass(frozen=True)
class PolicyConfig:
    """`contracts/sql.policy.yaml` 的运行时视图。"""

    version: int

    # 语句规则
    single_statement: bool
    allowed_roots: tuple[str, ...]
    forbid_set_operations: bool
    forbid_executable_comments: bool
    forbid_write_keywords: tuple[str, ...]
    forbid_dangerous_keywords: tuple[str, ...]

    # 行数上限
    default_limit: int
    max_limit: int

    # 超时
    max_execution_time_ms: int
    read_timeout_s: int
    connect_timeout_s: int

    # 只读账号
    account_user: str
    account_host: str
    account_database: str
    account_grants: tuple[str, ...]

    # 错误码
    channel_code: str
    error_codes: tuple[str, ...]
    reason_codes: tuple[str, ...]


def _section(data: dict[str, Any], name: str) -> dict[str, Any]:
    value = data.get(name)
    if not isinstance(value, dict):
        raise PolicyConfigError(f"策略契约缺少 {name} 段")
    return value


def _exact_keys(section: dict[str, Any], name: str, expected: set[str]) -> None:
    actual = set(section)
    missing = sorted(expected - actual)
    unknown = sorted(actual - expected)
    if missing:
        raise PolicyConfigError(f"策略契约 {name} 段缺少键：{missing}")
    if unknown:
        raise PolicyConfigError(f"策略契约 {name} 段出现未定义的键：{unknown}")


def _strings(section: dict[str, Any], name: str, key: str) -> tuple[str, ...]:
    value = section.get(key)
    if not isinstance(value, list) or not value:
        raise PolicyConfigError(f"策略契约 {name}.{key} 必须是非空列表")
    items = tuple(str(item) for item in value)
    if len(set(items)) != len(items):
        raise PolicyConfigError(f"策略契约 {name}.{key} 有重复项")
    return items


def _positive_int(section: dict[str, Any], name: str, key: str) -> int:
    value = section.get(key)
    if not isinstance(value, int) or isinstance(value, bool) or value <= 0:
        raise PolicyConfigError(f"策略契约 {name}.{key} 必须是正整数")
    return value


def parse_policy_config(data: dict[str, Any]) -> PolicyConfig:
    """校验并转成 `PolicyConfig`。任何不合契约的输入都抛异常。"""
    if not isinstance(data, dict):
        raise PolicyConfigError("策略契约不是 YAML 映射")
    _exact_keys(
        data,
        "根",
        {"version", "statement", "limits", "timeout", "account", "errors"},
    )
    if data.get("version") != 1:
        raise PolicyConfigError("策略契约 version 必须为 1")

    statement = _section(data, "statement")
    _exact_keys(
        statement,
        "statement",
        {
            "single_statement",
            "allowed_roots",
            "forbid_set_operations",
            "forbid_executable_comments",
            "forbid_write_keywords",
            "forbid_dangerous_keywords",
        },
    )
    if not isinstance(statement.get("single_statement"), bool):
        raise PolicyConfigError("策略契约 statement.single_statement 必须是布尔值")
    allowed_roots = _strings(statement, "statement", "allowed_roots")
    for flag in ("forbid_set_operations", "forbid_executable_comments"):
        if not isinstance(statement.get(flag), bool):
            raise PolicyConfigError(f"策略契约 statement.{flag} 必须是布尔值")
    forbid_write_keywords = _strings(statement, "statement", "forbid_write_keywords")
    forbid_dangerous_keywords = _strings(
        statement, "statement", "forbid_dangerous_keywords"
    )
    overlap = sorted(set(forbid_write_keywords) & set(forbid_dangerous_keywords))
    if overlap:
        raise PolicyConfigError(
            f"策略契约里同一个关键字不能既算写操作又算危险函数：{overlap}"
        )

    limits = _section(data, "limits")
    _exact_keys(limits, "limits", {"default_limit", "max_limit"})
    default_limit = _positive_int(limits, "limits", "default_limit")
    max_limit = _positive_int(limits, "limits", "max_limit")
    if default_limit > max_limit:
        raise PolicyConfigError(
            f"策略契约 limits.default_limit({default_limit}) "
            f"不能大于 max_limit({max_limit})"
        )

    timeout = _section(data, "timeout")
    _exact_keys(
        timeout,
        "timeout",
        {"max_execution_time_ms", "read_timeout_s", "connect_timeout_s"},
    )
    max_execution_time_ms = _positive_int(timeout, "timeout", "max_execution_time_ms")
    read_timeout_s = _positive_int(timeout, "timeout", "read_timeout_s")
    connect_timeout_s = _positive_int(timeout, "timeout", "connect_timeout_s")

    account = _section(data, "account")
    _exact_keys(
        account,
        "account",
        {"user", "host", "database", "grants"},
    )
    account_user = str(account.get("user") or "")
    account_host = str(account.get("host") or "")
    account_database = str(account.get("database") or "")
    if not account_user or not account_database:
        raise PolicyConfigError("策略契约 account.user / account.database 不能为空")
    account_grants = _strings(account, "account", "grants")
    unexpected = [grant for grant in account_grants if grant.upper() != "SELECT"]
    if unexpected:
        raise PolicyConfigError(
            f"只读账号只允许 SELECT 权限，契约里出现了：{unexpected}"
        )

    errors = _section(data, "errors")
    _exact_keys(errors, "errors", {"channel_code", "codes", "reason_codes"})
    channel_code = str(errors.get("channel_code") or "")
    if not channel_code:
        raise PolicyConfigError("策略契约 errors.channel_code 不能为空")
    error_codes = _strings(errors, "errors", "codes")
    reason_codes = _strings(errors, "errors", "reason_codes")
    if channel_code not in error_codes:
        raise PolicyConfigError(
            f"策略契约 errors.channel_code({channel_code}) 必须出现在 codes 里"
        )

    return PolicyConfig(
        version=1,
        single_statement=bool(statement["single_statement"]),
        allowed_roots=allowed_roots,
        forbid_set_operations=bool(statement["forbid_set_operations"]),
        forbid_executable_comments=bool(statement["forbid_executable_comments"]),
        forbid_write_keywords=forbid_write_keywords,
        forbid_dangerous_keywords=forbid_dangerous_keywords,
        default_limit=default_limit,
        max_limit=max_limit,
        max_execution_time_ms=max_execution_time_ms,
        read_timeout_s=read_timeout_s,
        connect_timeout_s=connect_timeout_s,
        account_user=account_user,
        account_host=account_host,
        account_database=account_database,
        account_grants=account_grants,
        channel_code=channel_code,
        error_codes=error_codes,
        reason_codes=reason_codes,
    )


@lru_cache(maxsize=1)
def load_policy_config(path: Path = POLICY_PATH) -> PolicyConfig:
    """读 `contracts/sql.policy.yaml` 并校验。"""
    data = yaml.safe_load(path.read_text(encoding="utf-8"))
    return parse_policy_config(data)
