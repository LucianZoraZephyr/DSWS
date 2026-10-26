"""S5 · 零误杀：S4 的 10 题基线必须全部通过安全策略。

路线图 S5 的验收第 3 条：「正常 10 题零误杀」。

这条测试是把两层接在一起验的：S4 生成出来的 SQL，直接喂给 S5 的策略。
如果策略太严（比如把 ORDER BY 里的中文别名当成未知列），这里就会红。

两题是拒答题（越界 / 契约里没有的粒度），它们**根本不产生 SQL**，
也就谈不上被策略误杀——所以真正参与比对的是 8 题。
"""

from __future__ import annotations

from pathlib import Path

import pytest
import yaml

from app.generate.pipeline import generate
from app.security.policy import enforce

BASELINE_PATH = (
    Path(__file__).resolve().parents[1] / "generate" / "baseline.yaml"
)

MIN_SQL_CASES = 8


@pytest.fixture(scope="module")
def cases() -> list[dict]:
    data = yaml.safe_load(BASELINE_PATH.read_text(encoding="utf-8"))
    return list(data.get("questions") or [])


def test_baseline_has_enough_sql_cases(cases: list[dict]) -> None:
    """参与比对的题不能太少——否则「零误杀」就是空话。"""
    produced = sum(1 for case in cases if generate(str(case["question"])).ok)
    assert produced >= MIN_SQL_CASES, (
        f"只有 {produced} 题能生成 SQL（要求 >= {MIN_SQL_CASES}）；"
        f"零误杀的结论没有样本支撑"
    )


@pytest.mark.parametrize(
    "case",
    yaml.safe_load(BASELINE_PATH.read_text(encoding="utf-8"))["questions"],
    ids=lambda case: case["id"],
)
def test_baseline_sql_passes_the_security_policy(case: dict) -> None:
    result = generate(str(case["question"]))
    if not result.ok:
        pytest.skip(f"这一题是拒答（{result.refusal.code}），不产生 SQL")

    decision = enforce(result.sql or "")
    assert decision.ok, (
        f"{case['id']} 的基线 SQL 被安全策略误杀："
        f"{decision.reason_code} —— "
        f"{decision.violations[0].message if decision.violations else ''}"
    )


def test_baseline_sql_gets_a_limit(cases: list[dict]) -> None:
    """基线 SQL 进入执行层时都带上了 LIMIT。"""
    checked = 0
    for case in cases:
        result = generate(str(case["question"]))
        if not result.ok:
            continue
        decision = enforce(result.sql or "")
        assert decision.applied_limit is not None
        assert "LIMIT" in decision.sql
        checked += 1
    assert checked >= MIN_SQL_CASES
