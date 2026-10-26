"""S4 · 10 题基线。

路线图对 S4 的验收是：

    make stage4
    # 1) 10/10 生成合法 SQL（EXPLAIN 通过）
    # 2) 语义断言：单语句 / 只读 / 列存在 / 无常量投影 / 排名有 ORDER BY
    # 3) 越界题返回 refusal JSON，而不是空数组
    # 4) 槽位缺失时模板直接报错（不再静默）

第 1 条里的「EXPLAIN 通过」在本仓库里落成**静态校验全绿**（sqlglot 解析
+ 5 条语义断言）：S4 的边界是「生成 + 静态校验」，不执行 SQL，也就不需要
数据库在线。第 4 条在 test_prompts.py 里。

另外还断言一条跨阶段的一致性：基线里 8 道题来自 S3 黄金集，两边的题面
必须一模一样——检索层和生成层对同一个问题给出一致的理解，才谈得上
「端到端一致」。
"""

from __future__ import annotations

from pathlib import Path

import pytest
import yaml

from app.generate.output_schema import is_valid_payload
from app.generate.pipeline import generate

BASELINE_PATH = Path(__file__).with_name("baseline.yaml")
GOLDEN_PATH = Path(__file__).resolve().parents[1] / "recall" / "golden.yaml"


def _load(path: Path) -> dict:
    return yaml.safe_load(path.read_text(encoding="utf-8"))


def _cases() -> list[dict]:
    data = _load(BASELINE_PATH)
    return data["questions"]


CASES = _cases()
CASE_IDS = [case["id"] for case in CASES]


def test_baseline_has_ten_questions() -> None:
    assert len(CASES) == 10
    assert len(set(CASE_IDS)) == 10


@pytest.mark.parametrize("case", CASES, ids=CASE_IDS)
def test_baseline_case(case: dict) -> None:
    result = generate(case["question"])

    # 无论成功还是拒答，输出都必须符合 sql_or_refusal 契约
    assert is_valid_payload(result.to_payload()), result.to_payload()

    if case["expect"] == "refusal":
        assert not result.ok, f"{case['id']} 期望拒答，实际生成了 SQL"
        assert result.refusal.code == case["refusal_code"]
        assert result.refusal.reason.strip()
        # 拒答不允许带 SQL
        assert "sql" not in result.to_payload()
        return

    assert result.ok, f"{case['id']} 期望 SQL，实际拒答 {result.refusal}"
    assert result.guard.ok, result.guard.to_dict()

    sql = result.sql or ""
    for needle in case.get("sql_contains") or []:
        assert needle in sql, f"{case['id']}: SQL 缺少 {needle!r}"
    for needle in case.get("sql_not_contains") or []:
        assert needle not in sql, f"{case['id']}: SQL 不该包含 {needle!r}"


@pytest.mark.parametrize("case", CASES, ids=CASE_IDS)
def test_baseline_case_is_reproducible(case: dict) -> None:
    """同题必须同解，否则基线不能当验收标准。"""
    assert generate(case["question"]).to_payload() == generate(
        case["question"]
    ).to_payload()


def test_baseline_covers_both_outcomes() -> None:
    outcomes = {case["expect"] for case in CASES}
    assert outcomes == {"sql", "refusal"}


def test_baseline_covers_all_three_hard_rules() -> None:
    """三条硬规则都要有至少一道基线题覆盖。"""
    sql_cases = [case for case in CASES if case["expect"] == "sql"]
    joined = " ".join(
        needle for case in sql_cases for needle in (case.get("sql_contains") or [])
    )
    assert "ORDER BY" in joined, "缺少「排名必带 ORDER BY」的覆盖"
    assert "LIMIT" in joined, "缺少「排名必带 LIMIT」的覆盖"

    codes = {
        case["refusal_code"]
        for case in CASES
        if case["expect"] == "refusal"
    }
    assert "OUT_OF_RANGE" in codes, "缺少「越界必拒答」的覆盖"
    assert "UNSUPPORTED_INTENT" in codes

    # 「禁止常量/NULL 投影」由静态校验逐条把关（test_guard.py），
    # 基线里再确认没有任何一条 SQL 投影了常量。
    for case in sql_cases:
        sql = generate(case["question"]).sql or ""
        assert "SELECT 1 " not in sql
        assert "SELECT NULL" not in sql
        assert " AS NULL" not in sql


def test_baseline_questions_match_the_s3_golden_set() -> None:
    """跨阶段一致性：同一道题，检索层和生成层必须是同一道题。"""
    golden = {item["id"]: item["question"] for item in _load(GOLDEN_PATH)["questions"]}
    for case in CASES:
        golden_id = case.get("golden_id")
        if not golden_id:
            continue
        assert golden_id in golden, f"{case['id']}: S3 黄金集没有 {golden_id}"
        assert golden[golden_id] == case["question"], (
            f"{case['id']} 与 S3 黄金集 {golden_id} 的题面不一致"
        )
