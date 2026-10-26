"""S6 · 失败场景夹具。

夹具是「6 类失败」在浏览器里的剧本，所以它必须**真的**演出那 6 类失败：
每个场景实际跑一遍流水线，结局必须与夹具里 `expect` 写的一致。

夹具漂了（改了 SQL 却没改 expect），这里就会红。
"""

from __future__ import annotations

import json

import pytest
from fastapi.testclient import TestClient

from app.agent.errors import load_error_table
from app.scenarios.app import app as scenario_app
from app.scenarios.loader import load_scenarios, split_query

client = TestClient(scenario_app)
SCENARIOS = load_scenarios()

# 路线图 S6 点名的 6 类失败（不含 CANCELLED——取消由取消按钮触发）。
FAILURE_SCENARIOS = (
    "sql_rejected",
    "sql_syntax",
    "sql_unknown_column",
    "empty_result",
    "llm_refusal",
    "timeout",
)


def _events(scenario_id: str) -> list[dict]:
    response = client.post(
        "/api/query", json={"query": f"场景:{scenario_id} 华北销售额"}
    )
    assert response.status_code == 200, response.text
    events: list[dict] = []
    for block in response.text.split("\n\n"):
        for line in block.splitlines():
            if line.startswith("data:"):
                events.append(json.loads(line[len("data:") :].strip()))
    return events


# ---------------------------------------------------------------------------
# 夹具本身
# ---------------------------------------------------------------------------


def test_fixture_has_the_six_failure_classes() -> None:
    for scenario_id in FAILURE_SCENARIOS:
        assert scenario_id in SCENARIOS, f"夹具缺少场景 {scenario_id}"


def test_expected_codes_are_in_the_error_table() -> None:
    table = load_error_table()
    for scenario in SCENARIOS.values():
        code = scenario.expected_code
        if code:
            assert code in table, f"场景 {scenario.id} 的期望码 {code} 不在错误码表里"


def test_scenario_prefix_is_split_correctly() -> None:
    scenario_id, query = split_query("场景:timeout 华北销售额")
    assert scenario_id == "timeout"
    assert query == "华北销售额"


def test_query_without_prefix_uses_the_default() -> None:
    scenario_id, query = split_query("华北销售额")
    assert scenario_id == "happy"
    assert query == "华北销售额"


# ---------------------------------------------------------------------------
# 每个场景都要演出它声称的结局
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("scenario_id", sorted(SCENARIOS))
def test_scenario_plays_its_expected_outcome(scenario_id: str) -> None:
    scenario = SCENARIOS[scenario_id]
    events = _events(scenario_id)
    terminal = events[-1]

    if scenario.expected_outcome == "result":
        assert terminal["type"] == "result", f"{scenario_id} 应当成功"
        expected_rows = scenario.expect.get("rows")
        if expected_rows is not None:
            assert len(terminal["data"]["rows"]) == expected_rows
        return

    assert terminal["type"] == "error", f"{scenario_id} 应当失败"
    assert terminal["code"] == scenario.expected_code
    assert terminal["class"] == scenario.expected_class
    assert terminal["step"] == scenario.expected_step


def test_the_six_failure_classes_have_different_ui_signatures() -> None:
    """6 类失败的界面状态必须互不相同。

    旧系统里它们全是「查询完成，共 1 行结果。」——一个绿勾了事。
    """
    signatures = set()
    for scenario_id in FAILURE_SCENARIOS:
        terminal = _events(scenario_id)[-1]
        signatures.add(
            (
                terminal["code"],
                terminal["class"],
                terminal["step"],
            )
        )
    assert len(signatures) == len(FAILURE_SCENARIOS), (
        f"6 类失败只给出了 {len(signatures)} 种界面状态：{sorted(signatures)}"
    )


def test_empty_result_is_not_marked_as_a_failure_step() -> None:
    """空结果不是失败：执行步骤仍然是绿的。"""
    events = _events("empty_result")
    run_states = [
        event["status"]
        for event in events
        if event["type"] == "progress" and event["step"] == "run_sql"
    ]
    assert run_states[-1] == "success"


def test_correction_scenario_ends_with_a_result() -> None:
    """校正闭环成功的那一幕：先红后绿，最后拿到结果。"""
    events = _events("corrected")
    assert events[-1]["type"] == "result"
    validate_states = [
        event["status"]
        for event in events
        if event["type"] == "progress" and event["step"] == "validate_sql"
    ]
    assert validate_states == ["running", "error", "running", "success"]
    correct_states = [
        event["status"]
        for event in events
        if event["type"] == "progress" and event["step"] == "correct_sql"
    ]
    assert correct_states == ["running", "success"]
