"""
S0 契约测试

验收标准（对应 REBUILD-ROADMAP.md 的 `make stage0`）：
  1. /api/steps 下发 12 个步骤，其中恰好 1 个 conditional；version 来自 YAML
  2. /api/query 的每一条事件都通过 contracts/sse.schema.json
  3. 非条件步骤：running → success；条件步骤：skipped
  4. 最后一条事件是 result
  5. 未知事件类型 / 非法 duration 必须被 schema 拒绝
  6. 边界：500 字通过、501 字 422、空白 422；CORS 只放行开发源
"""

from __future__ import annotations

import json
from pathlib import Path

import jsonschema
import pytest
import yaml
from fastapi.testclient import TestClient

from app.main import app

ROOT = Path(__file__).resolve().parents[2]
SCHEMA = json.loads((ROOT / "contracts" / "sse.schema.json").read_text(encoding="utf-8"))
STEPS_DOC = yaml.safe_load(
    (ROOT / "contracts" / "steps.yaml").read_text(encoding="utf-8")
)
STEPS = STEPS_DOC["steps"]

client = TestClient(app)


def parse_sse(body: str) -> list[dict]:
    """把 SSE 响应体解析成事件列表。"""
    events: list[dict] = []
    for block in body.split("\n\n"):
        for line in block.splitlines():
            if line.startswith("data:"):
                events.append(json.loads(line[len("data:") :].strip()))
    return events


def test_steps_catalog() -> None:
    response = client.get("/api/steps")
    assert response.status_code == 200

    payload = response.json()
    assert payload["version"] == STEPS_DOC["version"]
    assert len(payload["steps"]) == 12
    assert sum(1 for step in payload["steps"] if step.get("conditional")) == 1
    assert [step["id"] for step in payload["steps"]] == [step["id"] for step in STEPS]
    assert [step["label"] for step in payload["steps"]] == [
        step["label"] for step in STEPS
    ]
    assert [step.get("conditional", False) for step in payload["steps"]] == [
        step.get("conditional", False) for step in STEPS
    ]


def test_every_event_matches_contract() -> None:
    response = client.post("/api/query", json={"query": "华北地区销售额"})
    assert response.status_code == 200

    events = parse_sse(response.text)
    assert events, "没有收到任何 SSE 事件"

    for event in events:
        jsonschema.validate(instance=event, schema=SCHEMA)

    known_ids = {step["id"] for step in STEPS}
    for event in events:
        if event["type"] == "progress":
            assert event["step"] in known_ids


def test_step_lifecycle() -> None:
    """每个步骤都要有结论，条件步骤要么跑要么显式 skipped。

    S6 起 `/api/query` 跑的是**真的**流水线（S0 是假流，永远成功），
    所以非条件步骤的最后一条事件可能是 `success`，也可能是 `error`
    （例如没配只读账号，执行步骤就会失败）——但**不能没有结论**。
    """
    events = parse_sse(client.post("/api/query", json={"query": "test"}).text)
    progress = [event for event in events if event["type"] == "progress"]

    by_step: dict[str, list[dict]] = {}
    for event in progress:
        by_step.setdefault(event["step"], []).append(event)

    for step in STEPS:
        step_events = by_step.get(step["id"], [])
        statuses = [event["status"] for event in step_events]
        if step.get("conditional"):
            # 条件步骤：跑了就 running → 终态，没跑就显式 skipped。
            # 绝不能「什么都没有」——那就是永远灰着的 pending（审计 D4）。
            assert statuses in (
                ["skipped"],
                ["running", "success"],
                ["running", "error"],
            ), f"{step['id']} 的状态序列不对：{statuses}"
        elif statuses:
            # 跑过的步骤必须有结论，不能半途而废。
            assert statuses[0] == "running", f"{step['id']} 应当先报 running"
            assert statuses[-1] in ("success", "error"), (
                f"{step['id']} 必须有结论，不能半途而废：{statuses}"
            )
            assert step_events[-1]["duration_ms"] is not None
            assert step_events[-1]["duration_ms"] >= 0
        # 没有事件的步骤 = 流水线在它之前就停了，它压根没开始。

    # 流必须以 result 或 error 收尾——没有第三种结局。
    assert events[-1]["type"] in ("result", "error")


def test_skipped_only_for_conditional_steps() -> None:
    """`skipped` 是条件步骤的专属状态。

    反过来也成立：条件步骤没跑的时候必须**显式** skipped，不能留一个
    永远灰着的 pending（审计 D4）。
    """
    events = parse_sse(client.post("/api/query", json={"query": "test"}).text)
    skipped = {
        event["step"]
        for event in events
        if event["type"] == "progress" and event["status"] == "skipped"
    }
    conditional = {step["id"] for step in STEPS if step.get("conditional")}
    assert skipped <= conditional, f"非条件步骤不该 skipped：{sorted(skipped)}"

    for step in STEPS:
        if not step.get("conditional"):
            continue
        states = [
            event["status"]
            for event in events
            if event["type"] == "progress" and event["step"] == step["id"]
        ]
        assert states, f"条件步骤 {step['id']} 必须有状态，不能空着"
        assert states[-1] in ("skipped", "success", "error")


def test_unknown_event_is_rejected() -> None:
    """未知事件必须被契约拒绝——这是旧系统「静默忽略」缺陷的反例。"""
    with pytest.raises(jsonschema.ValidationError):
        jsonschema.validate(instance={"type": "unknown"}, schema=SCHEMA)


@pytest.mark.parametrize(
    "event",
    [
        # success / error 必须带非负数字
        {"type": "progress", "step": "run_sql", "status": "success"},
        {"type": "progress", "step": "run_sql", "status": "success", "duration_ms": None},
        {"type": "progress", "step": "run_sql", "status": "error", "duration_ms": -1},
        # running / skipped 必须是 null
        {"type": "progress", "step": "run_sql", "status": "running", "duration_ms": 12},
        {"type": "progress", "step": "correct_sql", "status": "skipped", "duration_ms": 0},
    ],
)
def test_bad_duration_is_rejected(event: dict) -> None:
    with pytest.raises(jsonschema.ValidationError):
        jsonschema.validate(instance=event, schema=SCHEMA)


@pytest.mark.parametrize(
    "event",
    [
        {"type": "progress", "step": "run_sql", "status": "success", "duration_ms": 12},
        {"type": "progress", "step": "run_sql", "status": "error", "duration_ms": 0},
        {"type": "progress", "step": "run_sql", "status": "running", "duration_ms": None},
        {
            "type": "progress",
            "step": "correct_sql",
            "status": "skipped",
            "duration_ms": None,
        },
    ],
)
def test_good_duration_is_accepted(event: dict) -> None:
    jsonschema.validate(instance=event, schema=SCHEMA)


def test_error_event_requires_step_and_class() -> None:
    """error 事件必须带 `step` 与 `class`。

    `class` 是 S6 加的：前端靠它区分「红框」「空态」「取消」。少了它，
    失败就只剩一种长相——那正是旧系统的毛病。
    """
    with pytest.raises(jsonschema.ValidationError):
        jsonschema.validate(
            instance={
                "type": "error",
                "code": "SQL_REJECTED",
                "class": "failure",
                "message": "x",
            },
            schema=SCHEMA,
        )
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
    jsonschema.validate(
        instance={
            "type": "error",
            "code": "SQL_REJECTED",
            "class": "failure",
            "message": "x",
            "step": None,
        },
        schema=SCHEMA,
    )


def test_empty_query_is_rejected() -> None:
    assert client.post("/api/query", json={"query": ""}).status_code == 422
    assert client.post("/api/query", json={"query": "   "}).status_code == 422


def test_query_length_boundary() -> None:
    assert client.post("/api/query", json={"query": "字" * 500}).status_code == 200
    assert client.post("/api/query", json={"query": "字" * 501}).status_code == 422


def test_cors_only_allows_dev_origin() -> None:
    allowed = client.options(
        "/api/query",
        headers={
            "Origin": "http://127.0.0.1:5273",
            "Access-Control-Request-Method": "POST",
        },
    )
    assert allowed.headers.get("access-control-allow-origin") == "http://127.0.0.1:5273"

    denied = client.options(
        "/api/query",
        headers={
            "Origin": "http://evil.example",
            "Access-Control-Request-Method": "POST",
        },
    )
    assert "access-control-allow-origin" not in denied.headers


def test_openapi_query_declares_event_stream() -> None:
    spec = client.get("/openapi.json").json()
    content = spec["paths"]["/api/query"]["post"]["responses"]["200"]["content"]
    assert "text/event-stream" in content
    assert "application/json" not in content
