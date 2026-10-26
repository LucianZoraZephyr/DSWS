"""S6 · SSE 失败通道（HTTP 端到端）。

这一层查的是「浏览器看到的东西」：

    - 每条事件都过 `contracts/sse.schema.json`；
    - `error` 事件**真的可达**（S0 的假流永远成功，所以这一条是新东西）；
    - 取消接口能让一次问数停下来；
    - 错误码表能从前端拿到（前端不硬编码码的含义）。
"""

from __future__ import annotations

import json
from pathlib import Path

import jsonschema
import pytest
import yaml
from fastapi.testclient import TestClient

from app.agent.errors import load_error_table
from app.main import app
from app.scenarios.app import app as scenario_app

ROOT = Path(__file__).resolve().parents[3]
SCHEMA = json.loads(
    (ROOT / "contracts" / "sse.schema.json").read_text(encoding="utf-8")
)
STEPS = yaml.safe_load(
    (ROOT / "contracts" / "steps.yaml").read_text(encoding="utf-8")
)["steps"]

client = TestClient(app)
scenario_client = TestClient(scenario_app)


def parse_sse(body: str) -> list[dict]:
    events: list[dict] = []
    for block in body.split("\n\n"):
        for line in block.splitlines():
            if line.startswith("data:"):
                events.append(json.loads(line[len("data:") :].strip()))
    return events


# ---------------------------------------------------------------------------
# 契约
# ---------------------------------------------------------------------------


def test_steps_endpoint_still_serves_the_catalog() -> None:
    response = client.get("/api/steps")
    assert response.status_code == 200
    payload = response.json()
    assert payload["version"] == 1
    assert [step["id"] for step in payload["steps"]] == [
        step["id"] for step in STEPS
    ]


def test_error_table_endpoint_serves_the_contract() -> None:
    """前端拿它把码翻译成人话，所以它必须与契约逐字一致。"""
    response = client.get("/api/errors")
    assert response.status_code == 200
    payload = response.json()
    table = load_error_table()
    assert payload["codes"] == [spec.to_dict() for spec in table.specs]
    assert payload["classes"] == list(table.classes)


def test_error_event_requires_the_class_field() -> None:
    with pytest.raises(jsonschema.ValidationError):
        jsonschema.validate(
            instance={
                "type": "error",
                "code": "SQL_REJECTED",
                "message": "x",
                "step": None,
            },
            schema=SCHEMA,
        )


# ---------------------------------------------------------------------------
# 失败通道真的可达
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "scenario_id",
    ["sql_rejected", "sql_syntax", "sql_unknown_column", "empty_result",
     "llm_refusal", "timeout"],
)
def test_every_failure_class_reaches_the_browser(scenario_id: str) -> None:
    """6 类失败：每一类都必须以一条符合契约的 error 事件收尾。"""
    response = scenario_client.post(
        "/api/query", json={"query": f"场景:{scenario_id} 华北销售额"}
    )
    assert response.status_code == 200

    events = parse_sse(response.text)
    assert events
    for event in events:
        jsonschema.validate(instance=event, schema=SCHEMA)

    terminal = events[-1]
    assert terminal["type"] == "error"
    assert terminal["code"] in load_error_table()


def test_stream_never_ends_without_a_terminal_event() -> None:
    """静默结束 = 失败，但没人知道——这是旧系统最大的缺陷。"""
    response = scenario_client.post("/api/query", json={"query": "场景:happy 华北销售额"})
    events = parse_sse(response.text)
    assert events[-1]["type"] in ("result", "error")


def test_request_id_is_exposed_for_cancellation() -> None:
    response = scenario_client.post(
        "/api/query", json={"query": "场景:happy 华北销售额"}
    )
    assert response.headers.get("x-request-id")


def test_cancel_endpoint_reports_whether_it_found_the_request() -> None:
    response = scenario_client.post("/api/cancel", json={"request_id": "nope"})
    assert response.status_code == 200
    assert response.json() == {"request_id": "nope", "cancelled": False}


def test_cancel_endpoint_accepts_a_request_id() -> None:
    """取消一个不存在的问数不是错误——它可能已经结束了。"""
    response = client.post("/api/cancel", json={"request_id": "does-not-exist"})
    assert response.status_code == 200
    assert response.json()["cancelled"] is False


def test_empty_query_is_still_rejected() -> None:
    assert scenario_client.post("/api/query", json={"query": "  "}).status_code == 422


def test_unknown_scenario_is_rejected() -> None:
    response = scenario_client.post(
        "/api/query", json={"query": "场景:没有这个场景 华北销售额"}
    )
    assert response.status_code == 422


# ---------------------------------------------------------------------------
# 与步骤目录的关系
# ---------------------------------------------------------------------------


def test_progress_events_only_use_known_steps() -> None:
    response = scenario_client.post(
        "/api/query", json={"query": "场景:corrected 华北销售额"}
    )
    known = {step["id"] for step in STEPS}
    for event in parse_sse(response.text):
        if event["type"] == "progress":
            assert event["step"] in known
