"""S4 · 提示词契约的测试。

这一组测试守的是审计里最刺眼的那条缺陷：**槽位声明是死代码**。

旧系统里 7 个提示词的占位符「看起来」四方一致，但 langchain 会用模板自动
探测的结果覆盖节点里写的 `input_variables`，拼错也不报错。这里把它反过来：
声明就是唯一事实源，少一个槽位就直接抛异常。
"""

from __future__ import annotations

import pytest

from app.generate.prompts import (
    PromptError,
    PromptSlotError,
    get_prompt,
    known_output_schemas,
    load_prompts,
    prompt_slots,
    render_prompt,
    runtime_prompts,
)

# 路线图要求「7 个提示词：每个声明输入槽位 + 输出 JSON schema」。
EXPECTED_PROMPT_NAMES = {
    "extend_keywords_for_column_recall",
    "extend_keywords_for_value_recall",
    "extend_keywords_for_metric_recall",
    "filter_table_info",
    "filter_metric_info",
    "generate_sql",
    "correct_sql",
}

# generate_sql 的 7 个槽位（路线图说的「7 个提示词槽位」）。
GENERATE_SLOTS = (
    "query",
    "table_infos",
    "column_infos",
    "metric_infos",
    "value_infos",
    "date_info",
    "db_info",
)


def test_seven_prompts_exist() -> None:
    assert set(load_prompts()) == EXPECTED_PROMPT_NAMES


def test_generate_sql_declares_exactly_seven_slots() -> None:
    assert prompt_slots("generate_sql") == GENERATE_SLOTS


@pytest.mark.parametrize("name", sorted(EXPECTED_PROMPT_NAMES))
def test_slots_and_placeholders_are_bidirectionally_consistent(name: str) -> None:
    """声明的每个槽位都要在正文里出现；正文的每个占位符都要被声明。"""
    spec = get_prompt(name)
    declared = set(spec.slots)
    used = spec.placeholders()
    assert declared - used == set(), f"{name}: 声明了但正文没用到的槽位"
    assert used - declared == set(), f"{name}: 正文里没声明的占位符"


@pytest.mark.parametrize("name", sorted(EXPECTED_PROMPT_NAMES))
def test_output_schema_is_defined_in_contract(name: str) -> None:
    spec = get_prompt(name)
    assert spec.output_schema in known_output_schemas()


def test_only_generate_and_correct_are_runtime() -> None:
    """S3 把 5 个 recall/filter 步骤确定性化了；S6 起校正也进了运行时。

    运行时的提示词只有两个：`generate_sql`（生成）与 `correct_sql`（校正）。
    其余 5 个仍是对照资产——「不调用」不等于「不校验」。
    """
    runtime = runtime_prompts()
    assert {spec.name for spec in runtime} == {"generate_sql", "correct_sql"}
    for name in EXPECTED_PROMPT_NAMES - {"generate_sql", "correct_sql"}:
        assert get_prompt(name).runtime is False


def test_missing_slot_raises_instead_of_silently_passing() -> None:
    """旧系统这里不报错；这里必须报错。"""
    values = {slot: "x" for slot in GENERATE_SLOTS}
    del values["metric_infos"]

    with pytest.raises(PromptSlotError) as excinfo:
        render_prompt("generate_sql", values)
    assert "metric_infos" in str(excinfo.value)


def test_extra_keys_are_allowed_but_missing_ones_are_not() -> None:
    values = {slot: "x" for slot in GENERATE_SLOTS}
    values["extra_context"] = "多出来的键"
    rendered = render_prompt("generate_sql", values)
    assert rendered.strip()
    # 多出来的键不会污染提示词
    assert "多出来的键" not in rendered


def test_every_prompt_body_mentions_its_slots() -> None:
    for name in EXPECTED_PROMPT_NAMES:
        spec = get_prompt(name)
        for slot in spec.slots:
            assert "{" + slot + "}" in spec.body


def test_unknown_prompt_name_raises() -> None:
    with pytest.raises(PromptError):
        get_prompt("not_a_prompt")


def test_generate_sql_prompt_states_the_three_hard_rules() -> None:
    """三条硬规则必须同时出现在「提示词」和「代码」里，不能只在一处。"""
    spec = get_prompt("generate_sql")
    assert set(spec.hard_rules) == {
        "ranking_requires_order_by",
        "out_of_range_must_refuse",
        "no_constant_or_null_projection",
    }
    body = spec.body
    assert "ORDER BY" in body
    assert "拒答" in body
    assert "NULL" in body
