"""S5 · 注入语料的门禁。

路线图 S5 的验收第 1、2 条：

    1) 注入语料 100% 拒绝
    2) 旧系统能跑通的 UPDATE / 多语句，新系统全部拒绝
    3) 正常 10 题零误杀

语料本身是 `fixtures/injection/sql-injection.yaml`。这里除了跑语料，还要
检查**语料没有退化**：条目数、原因码、legacy 证据都不能少。

「旧系统能跑通」这件事不是猜的——每条 legacy 记录都指向审计报告里的实测
结论（`audit-sql-validation.md` D6）。所以语料里 `legacy` 字段不能空。
"""

from __future__ import annotations

import pytest

from app.security.corpus import load_corpus
from app.security.policy import enforce

# 语料条数下限。少一条就说明有人把语料删瘦了——这正是负向夹具退化的样子。
MIN_REJECT_CASES = 25
MIN_ALLOW_CASES = 8

# 审计 D6 实测过的旧系统行为：这些载荷旧系统**确实**能跑通。
LEGACY_EXPLOITABLE = ("explain_passes", "multi_statement_runs")


@pytest.fixture(scope="module")
def corpus():
    return load_corpus()


# ---------------------------------------------------------------------------
# 1) 100% 拒绝
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("case", load_corpus().reject, ids=lambda case: case.id)
def test_every_injection_case_is_rejected(case) -> None:
    decision = enforce(case.sql)
    assert not decision.ok, f"{case.id} 竟然被放行了"
    assert decision.reason_code == case.reason_code, (
        f"{case.id} 的原因码不符：期望 {case.reason_code}，"
        f"实际 {decision.reason_code}"
    )


@pytest.mark.parametrize("case", load_corpus().allow, ids=lambda case: case.id)
def test_no_false_positive(case) -> None:
    decision = enforce(case.sql)
    assert decision.ok, (
        f"{case.id} 被误杀：{decision.reason_code} —— "
        f"{decision.violations[0].message if decision.violations else ''}"
    )


# ---------------------------------------------------------------------------
# 2) 旧系统能跑通的，新系统必须拒绝
# ---------------------------------------------------------------------------


def test_corpus_covers_the_legacy_exploits(corpus) -> None:
    """审计 D6 实测过的两类旧系统漏洞，语料里必须都有。"""
    legacy = {case.legacy for case in corpus.reject}
    for kind in LEGACY_EXPLOITABLE:
        assert kind in legacy, f"语料里没有覆盖旧系统的 {kind} 行为"


def test_legacy_exploitable_cases_are_all_rejected(corpus) -> None:
    """标记为「旧系统能跑通」的，一条都不许漏。"""
    for case in corpus.reject:
        if case.legacy in LEGACY_EXPLOITABLE:
            decision = enforce(case.sql)
            assert not decision.ok, f"{case.id} 旧系统能跑通，新系统必须拒绝"
            assert case.evidence, f"{case.id} 缺少旧系统行为的出处"


def test_every_reject_case_documents_the_legacy_behavior(corpus) -> None:
    for case in corpus.reject:
        assert case.legacy, f"{case.id} 没有记录旧系统的行为"
        assert case.evidence, f"{case.id} 没有记录证据出处"


# ---------------------------------------------------------------------------
# 3) 语料不能退化
# ---------------------------------------------------------------------------


def test_corpus_is_big_enough(corpus) -> None:
    assert len(corpus.reject) >= MIN_REJECT_CASES, (
        f"注入语料只剩 {len(corpus.reject)} 条（要求 >= {MIN_REJECT_CASES}）；"
        f"语料被删瘦就等于防线退化"
    )
    assert len(corpus.allow) >= MIN_ALLOW_CASES, (
        f"零误杀语料只剩 {len(corpus.allow)} 条（要求 >= {MIN_ALLOW_CASES}）"
    )


def test_corpus_covers_every_forbidden_construct(corpus) -> None:
    """契约里声明的每个禁用关键字，语料里至少有一条用到它。

    用词边界匹配，不用子串——`INTO` 不该被 `INSERT INTO` 之外的巧合命中，
    而 `INSERT` 也不该被别的词里蹭到。
    """
    import re

    from app.security.config import load_policy_config

    config = load_policy_config()
    corpus_sql = " ".join(case.sql.upper() for case in corpus.reject)
    for keyword in config.forbid_write_keywords + config.forbid_dangerous_keywords:
        pattern = rf"\b{re.escape(keyword)}\b"
        assert re.search(pattern, corpus_sql), (
            f"注入语料没有覆盖禁用关键字 {keyword}"
        )
