"""S7 · 回放夹具必须逐帧符合契约。

夹具是「真实流」的录像：除了 `duration_ms` / `request_id` 被归一化，
一个字节都不该改。所以它们也必须和真流一样，过同一份 SSE 契约。

唯一的例外是 `unknown_event`：它**故意**携带一条契约之外的事件，用来
验证前端「忽略未知类型」的行为（F3）。例外必须显式写在这里，而不是
让坏帧混过去。
"""

from __future__ import annotations

import json
from pathlib import Path

import jsonschema
import pytest
import yaml

ROOT = Path(__file__).resolve().parents[3]
FIXTURE_DIR = ROOT / "fixtures" / "sse"
SSE_SCHEMA = json.loads(
    (ROOT / "contracts" / "sse.schema.json").read_text(encoding="utf-8")
)
STEPS = yaml.safe_load(
    (ROOT / "contracts" / "steps.yaml").read_text(encoding="utf-8")
)
ERRORS = yaml.safe_load(
    (ROOT / "contracts" / "errors.yaml").read_text(encoding="utf-8")
)

STEP_IDS = {step["id"] for step in STEPS["steps"]}
ERROR_CODES = set(ERRORS["codes"])
EVENT_TYPES = {"progress", "result", "error"}


def _fixture_paths() -> list[Path]:
    return sorted(FIXTURE_DIR.glob("*.json"))


def _load(path: Path) -> dict:
    return json.loads(path.read_text(encoding="utf-8"))


def _frame_payload(frame: str) -> dict:
    assert frame.startswith("data: "), f"不是 data 帧：{frame[:60]!r}"
    return json.loads(frame[len("data: ") :])


@pytest.mark.parametrize("path", _fixture_paths(), ids=lambda p: p.stem)
def test_fixture_shape(path: Path) -> None:
    fixture = _load(path)
    assert fixture["id"] == path.stem
    assert fixture["source"] in {"recorded", "handwritten"}
    assert isinstance(fixture["delay_before_last_ms"], int)
    assert fixture["delay_before_last_ms"] >= 0
    assert fixture["frames"], "夹具不能没有帧"


@pytest.mark.parametrize("path", _fixture_paths(), ids=lambda p: p.stem)
def test_frames_follow_contract(path: Path) -> None:
    fixture = _load(path)
    for index, frame in enumerate(fixture["frames"]):
        payload = _frame_payload(frame)
        event_type = payload.get("type")

        if event_type not in EVENT_TYPES:
            # 只有 unknown_event 允许出现契约之外的事件：它是 F3 的输入。
            assert fixture["id"] == "unknown_event", (
                f"{path.name} 第 {index} 帧的事件类型 {event_type!r} "
                "不在契约里；只有 unknown_event 可以这样"
            )
            continue

        jsonschema.validate(instance=payload, schema=SSE_SCHEMA)

        if event_type == "progress":
            assert payload["step"] in STEP_IDS, (
                f"{path.name} 第 {index} 帧引用了未知步骤 {payload['step']!r}"
            )
        elif event_type == "error":
            assert payload["code"] in ERROR_CODES, (
                f"{path.name} 第 {index} 帧引用了未登记的码 {payload['code']!r}"
            )
            assert payload["class"] in ERRORS["classes"]


def test_unknown_event_is_the_only_exception() -> None:
    """`unknown_event` 必须恰好有一条契约之外的事件。"""
    fixture = _load(FIXTURE_DIR / "unknown_event.json")
    unknown = [
        payload
        for payload in (_frame_payload(frame) for frame in fixture["frames"])
        if payload.get("type") not in EVENT_TYPES
    ]
    assert len(unknown) == 1
    assert unknown[0]["type"] == "meta"


def test_expected_fixtures_exist() -> None:
    """交接文档点名的夹具一条都不能少（含 F6 的 null_column）。"""
    expected = {
        "happy",
        "corrected",
        "empty_result",
        "sql_rejected",
        "sql_syntax",
        "sql_unknown_column",
        "llm_refusal",
        "timeout",
        "slow",
        "cancelled",
        "pseudo_row",
        "unknown_event",
        "empty_rows",
        "null_column",
    }
    assert {path.stem for path in _fixture_paths()} == expected
