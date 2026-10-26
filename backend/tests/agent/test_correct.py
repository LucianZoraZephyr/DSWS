"""S6 · 校正闭环。

路线图 S6 的验收：

    > 校正后 SQL 必须再次过 S5 策略

所以这里最要紧的一条是：**校正不是绕过安全检查的后门**。校正器返回一条
带写操作的 SQL，闭环必须把它挡下来。

另外三条不变式：

    - 最多 1 次重试；
    - 保留原始 SQL；
    - 校正成功 → 旧 error 被清空。
"""

from __future__ import annotations

from app.agent.correct import (
    FixtureCorrector,
    LlmCorrector,
    build_correct_slots,
    propose_correction,
)
from app.agent.errors import build_error
from app.generate.prompts import get_prompt, render_prompt

from conftest import BROKEN_SQL, GOOD_SQL, UNKNOWN_COLUMN_SQL


def _syntax_error():
    return build_error("SQL_SYNTAX", "SQL 无法被解析", step="validate_sql")


# ---------------------------------------------------------------------------
# 槽位
# ---------------------------------------------------------------------------


def test_slots_match_the_prompt_contract(context) -> None:
    """槽位必须与 `correct_sql.prompt` 声明的完全一致。"""
    slots = build_correct_slots(context, BROKEN_SQL, "炸了")
    declared = set(get_prompt("correct_sql").slots)
    assert set(slots) == declared


def test_prompt_renders_with_the_slots(context) -> None:
    slots = build_correct_slots(context, BROKEN_SQL, "炸了")
    rendered = render_prompt("correct_sql", slots)
    assert BROKEN_SQL in rendered
    assert "炸了" in rendered


def test_correct_sql_prompt_is_runtime_enabled() -> None:
    """S6 起 `correct_sql` 真正进入运行时——它是闭环的一半。"""
    assert get_prompt("correct_sql").runtime is True


# ---------------------------------------------------------------------------
# 校正
# ---------------------------------------------------------------------------


def test_correction_proposes_a_new_sql(context) -> None:
    proposal = propose_correction(
        context,
        BROKEN_SQL,
        _syntax_error(),
        corrector=FixtureCorrector({"sql": GOOD_SQL}),
    )
    assert proposal.attempted is True
    assert proposal.ok is True
    assert proposal.sql == GOOD_SQL


def test_no_corrector_means_no_correction(context) -> None:
    proposal = propose_correction(
        context, BROKEN_SQL, _syntax_error(), corrector=None
    )
    assert proposal.attempted is False
    assert proposal.ok is False
    assert proposal.error is not None


def test_non_retryable_error_is_not_corrected(context) -> None:
    """策略拒绝不进闭环——拿被拒的 SQL 去问模型等于给注入开口子。"""
    rejected = build_error("SQL_REJECTED", "被拒了", reason_code="SQL_NOT_READ_ONLY")
    proposal = propose_correction(
        context,
        UNKNOWN_COLUMN_SQL,
        rejected,
        corrector=FixtureCorrector({"sql": GOOD_SQL}),
    )
    assert proposal.attempted is False
    assert proposal.error is rejected


def test_corrector_refusal_keeps_the_original_error(context) -> None:
    """校正失败 → 根因没变，错误也不该变。"""
    error = _syntax_error()
    proposal = propose_correction(
        context,
        BROKEN_SQL,
        error,
        corrector=FixtureCorrector(
            {"refusal": {"code": "INVALID_OUTPUT", "reason": "不会改"}}
        ),
    )
    assert proposal.ok is False
    assert proposal.error is error
    assert "不会改" in proposal.note


def test_corrector_crash_keeps_the_original_error(context) -> None:
    class Boom:
        name = "boom"

        def correct(self, context, prompt, sql, error):
            raise RuntimeError("炸了")

    error = _syntax_error()
    proposal = propose_correction(context, BROKEN_SQL, error, corrector=Boom())
    assert proposal.ok is False
    assert proposal.error is error
    assert "炸了" in proposal.note


def test_corrector_with_invalid_payload_keeps_the_original_error(context) -> None:
    error = _syntax_error()
    proposal = propose_correction(
        context, BROKEN_SQL, error, corrector=FixtureCorrector({"sql": 123})
    )
    assert proposal.ok is False
    assert proposal.error is error


def test_llm_corrector_reuses_the_generator_interface(context) -> None:
    """校正器与生成器共用一个接口——差别只是提示词。"""
    calls: list[str] = []

    class Fake:
        name = "fake"

        def generate(self, ctx, prompt):
            calls.append(prompt)
            return {"sql": GOOD_SQL}

    corrector = LlmCorrector(Fake())
    payload = corrector.correct(context, "PROMPT", BROKEN_SQL, "炸了")
    assert payload == {"sql": GOOD_SQL}
    assert calls == ["PROMPT"]
