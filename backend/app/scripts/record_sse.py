"""S7 · 录制 SSE 字节流。

    cd backend && uv run python -m app.scripts.record_sse           # 录制
    cd backend && uv run python -m app.scripts.record_sse --check   # 对账

录的是**真的**字节流：走的是场景应用（`app.main.create_app`）的同一条
HTTP / SSE 管道，模型与数据库是夹具里的替身，其余全是真代码。

为什么要录制：

    路线图 S7 的验收之一是「用录制好的 SSE fixture 回放，UI 与真实流
    一致」。有些界面行为**没法**在真系统里复现——比如旧系统那种「把失败
    编成一行数据」的伪 SQL，新后端会直接拒掉它。于是界面这一侧就没人
    测了。录制夹具把那种流**原样**存下来，回放时界面必须照样处理对。

归一化（只有两处，都是为了能对账）：

    duration_ms   每次跑都不一样 → 固定成 12.5
    request_id    每次都不一样   → 删掉

除此以外**一个字节都不改**：录下来的就是后端吐出来的东西。
"""

from __future__ import annotations

import argparse
import json
import sys
from collections.abc import Sequence
from pathlib import Path
from typing import Any

from app.cli import enable_utf8_stdio

REBUILD_ROOT = Path(__file__).resolve().parents[3]
FIXTURE_DIR = REBUILD_ROOT / "fixtures" / "sse"

#: 归一化之后固定的值。
FIXED_DURATION_MS = 12.5

#: 录制的场景（对应 fixtures/failures/scenarios.yaml 里的 id）。
RECORDED_SCENARIOS = (
    "happy",
    "corrected",
    "empty_result",
    "sql_rejected",
    "sql_syntax",
    "sql_unknown_column",
    "llm_refusal",
    "timeout",
    "slow",
)

#: 手工写的夹具：这些流**真系统不会产生**（新后端会拒掉它们），
#: 所以只能手写。它们的形状仍然必须过契约（tests/frontend 里逐条验）。
HANDWRITTEN = {
    "pseudo_row": {
        "id": "pseudo_row",
        "title": "伪 SQL 当数据行（旧系统的静默失败）",
        "source": "handwritten",
        "note": (
            "旧系统的 LLM 编出 SELECT '无法生成SQL…' AS error_message，"
            "这条 SQL 能过 EXPLAIN，于是被当成一行正常数据发回来。"
            "新后端会在校验阶段拒掉它（S4 拒绝常量投影），"
            "但前端必须有兜底：界面不能把失败画成一张成功的表。"
        ),
        "delay_before_last_ms": 0,
        "frames": [
            "data: "
            + json.dumps(
                {
                    "type": "result",
                    "data": {
                        "columns": [{"name": "error_message", "type": "string"}],
                        "rows": [["无法生成SQL：请补充时间范围后再试。"]],
                    },
                },
                ensure_ascii=False,
            )
            + "\n\n"
        ],
    },
    "unknown_event": {
        "id": "unknown_event",
        "title": "契约之外的事件类型",
        "source": "handwritten",
        "note": (
            "后端将来加一个新事件类型。前端必须**忽略**它并继续跑，"
            "而不是把它当成失败（旧系统的兜底分支就是这么错的）。"
        ),
        "delay_before_last_ms": 0,
        "frames": [
            "data: "
            + json.dumps(
                {"type": "meta", "note": "后端新增的事件类型"}, ensure_ascii=False
            )
            + "\n\n",
            "data: "
            + json.dumps(
                {
                    "type": "result",
                    "data": {
                        "columns": [
                            {"name": "地区", "type": "string"},
                            {"name": "销售额", "type": "number"},
                        ],
                        "rows": [["华北", 128400]],
                    },
                },
                ensure_ascii=False,
            )
            + "\n\n",
        ],
    },
    "empty_rows": {
        "id": "empty_rows",
        "title": "空数组（旧系统的 data:[]）",
        "source": "handwritten",
        "note": (
            "旧系统发 data:[] 时前端整张表消失，只剩一句话。"
            "新后端改发 EMPTY_RESULT（类别 empty），但前端仍要能处理"
            "0 行的结果表：显式空态块，不是空白。"
        ),
        "delay_before_last_ms": 0,
        "frames": [
            "data: "
            + json.dumps(
                {
                    "type": "result",
                    "data": {"columns": [], "rows": []},
                },
                ensure_ascii=False,
            )
            + "\n\n"
        ],
    },
    "null_column": {
        "id": "null_column",
        "title": "整列都是 NULL",
        "source": "handwritten",
        "note": (
            "整列都没有非空值时，列类型会被推成 null。旧前端把这种列"
            "画得和普通列一样，用户分不清「这一列是 0」还是「这一列"
            "根本没取到数据」（审计 F6）。这条流只能手写：场景夹具里的"
            "结果都是真有值的。"
        ),
        "delay_before_last_ms": 0,
        "frames": [
            "data: "
            + json.dumps(
                {
                    "type": "result",
                    "data": {
                        "columns": [
                            {"name": "地区", "type": "string"},
                            {"name": "退款额", "type": "null"},
                        ],
                        "rows": [["华北", None], ["华南", None]],
                    },
                },
                ensure_ascii=False,
            )
            + "\n\n"
        ],
    },
    "cancelled": {
        "id": "cancelled",
        "title": "用户取消",
        "source": "recorded",
        "note": "后端真的停下来时发出的那一条 CANCELLED 事件。",
        "delay_before_last_ms": 0,
        "frames": [],  # 录制时填
    },
}


# ---------------------------------------------------------------------------
# 录制
# ---------------------------------------------------------------------------


def _split_frames(body: str) -> list[str]:
    """SSE 响应体 → 帧列表（每帧是 `data: {...}` 那一行，不含结尾空行）。"""
    frames: list[str] = []
    for block in body.split("\n\n"):
        text = block.strip()
        if not text:
            continue
        if not text.startswith("data:"):
            raise RuntimeError(f"录制到非 data 帧：{text[:80]!r}")
        frames.append(text)
    return frames


def _normalize(frame: str) -> str:
    """归一化一帧（只动耗时与 request_id）。"""
    payload = json.loads(frame[len("data:") :].strip())

    if payload.get("type") == "progress":
        if payload.get("duration_ms") is not None:
            payload["duration_ms"] = FIXED_DURATION_MS
    elif payload.get("type") == "error":
        detail = payload.get("detail")
        if isinstance(detail, dict):
            detail.pop("request_id", None)

    return "data: " + json.dumps(payload, ensure_ascii=False)


def _record_scenario(scenario_id: str) -> list[str]:
    """从场景应用录一个场景。"""
    from fastapi.testclient import TestClient

    from app.scenarios.app import create_scenario_app

    app = create_scenario_app()
    with TestClient(app) as client:
        response = client.post(
            "/api/query", json={"query": f"场景:{scenario_id} 华北销售额"}
        )
        body = response.text
    return [_normalize(frame) for frame in _split_frames(body)]


def _record_cancelled() -> list[str]:
    """录一条真实的 CANCELLED 事件。

    做法：给编排器一个**已经拉下**的取消开关，于是它在第一个步骤边界
    就停下来，发一条 CANCELLED 错误事件就收尾——和用户点「停止」之后
    服务端看到的是同一条路径。
    """
    from fastapi.testclient import TestClient

    from app.agent.orchestrator import AgentRunner
    from app.main import create_app
    from app.scenarios.app import ScenarioExecutor, ScenarioGenerator
    from app.scenarios.loader import load_scenarios

    scenario = load_scenarios()["happy"]

    def factory(query: str, cancel: Any, request_id: str) -> AgentRunner:
        cancel.cancel()
        return AgentRunner(
            query=query,
            generator=ScenarioGenerator(scenario),
            executor=ScenarioExecutor(scenario),
            cancel=cancel,
            request_id=request_id,
        )

    app = create_app(factory)
    with TestClient(app) as client:
        response = client.post(
            "/api/query", json={"query": "场景:cancelled 华北销售额"}
        )
        body = response.text
    return [_normalize(frame) for frame in _split_frames(body)]


# ---------------------------------------------------------------------------
# 夹具
# ---------------------------------------------------------------------------


def _fixture(scenario_id: str, frames: list[str], note: str) -> dict[str, Any]:
    from app.scenarios.loader import load_scenarios

    title = load_scenarios()[scenario_id].title
    return {
        "id": scenario_id,
        "title": title,
        "source": "recorded",
        "note": note,
        "delay_before_last_ms": 3000 if scenario_id == "slow" else 0,
        "frames": frames,
    }


def build_fixtures() -> dict[str, dict[str, Any]]:
    """录制全部夹具：路径名 → 夹具内容。"""
    fixtures: dict[str, dict[str, Any]] = {}

    for scenario_id in RECORDED_SCENARIOS:
        frames = _record_scenario(scenario_id)
        fixtures[scenario_id] = _fixture(
            scenario_id,
            frames,
            "从场景应用录下的真实字节流（模型与数据库是替身，其余全真）。",
        )

    fixtures["cancelled"] = {
        "id": "cancelled",
        "title": "用户取消",
        "source": "recorded",
        "note": "取消开关在第一个步骤边界就被拉下时，后端发出的那一条事件。",
        "delay_before_last_ms": 0,
        "frames": _record_cancelled(),
    }

    for key, value in HANDWRITTEN.items():
        if key in fixtures:
            # 已经有录好的版本了（cancelled 是录出来的，不是手写的）。
            continue
        fixtures[key] = value

    return fixtures


def _render(fixture: dict[str, Any]) -> str:
    return json.dumps(fixture, ensure_ascii=False, indent=2) + "\n"


def _write(fixtures: dict[str, dict[str, Any]]) -> None:
    FIXTURE_DIR.mkdir(parents=True, exist_ok=True)
    for key, fixture in fixtures.items():
        (FIXTURE_DIR / f"{key}.json").write_text(
            _render(fixture), encoding="utf-8", newline="\n"
        )


def _check(fixtures: dict[str, dict[str, Any]]) -> list[str]:
    stale: list[str] = []
    for key, fixture in fixtures.items():
        path = FIXTURE_DIR / f"{key}.json"
        if not path.exists():
            stale.append(f"{path.name} 不存在")
            continue
        if path.read_text(encoding="utf-8") != _render(fixture):
            stale.append(f"{path.name} 与今天的真实输出不同步")
    return stale


def main(argv: Sequence[str] | None = None) -> int:
    enable_utf8_stdio()
    parser = argparse.ArgumentParser(
        prog="python -m app.scripts.record_sse",
        description="S7：录制 / 对账 SSE 字节流夹具",
    )
    parser.add_argument(
        "--check",
        action="store_true",
        help="只对账，不写文件（夹具过期就退出码 1）",
    )
    args = parser.parse_args(argv)

    try:
        fixtures = build_fixtures()
    except Exception as exc:  # noqa: BLE001 - 录制失败要说得清楚
        print(f"FAIL: 录制失败：{exc}", file=sys.stderr)
        return 1

    if args.check:
        stale = _check(fixtures)
        if stale:
            print("FAIL: SSE 夹具与真实输出不同步：", file=sys.stderr)
            for item in stale:
                print(f"  - {item}", file=sys.stderr)
            print(
                "\n请运行：cd backend && uv run python -m app.scripts.record_sse",
                file=sys.stderr,
            )
            return 1
        print(f"S7 SSE RECORD PASS（{len(fixtures)} 个夹具与真实输出一致）")
        return 0

    _write(fixtures)
    for key, fixture in fixtures.items():
        print(
            f"已录制 {key}.json（{len(fixture['frames'])} 帧，"
            f"{fixture['source']}）"
        )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
