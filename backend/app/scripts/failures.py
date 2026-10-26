"""S6 · 失败通道 CLI。

    cd backend && uv run python -m app.scripts.failures --check

离线门禁（**不需要** MySQL / LLM）：

    --check       6 类失败各跑一遍，结局必须与夹具一致
    --table       打印错误码表
    --scenarios   打印失败场景表

需要浏览器的部分见 `make stage6-ui`。
"""

from __future__ import annotations

import argparse
import sys
from collections.abc import Sequence

from app.agent.cancel import CancelToken
from app.agent.errors import ROADMAP_CODES, load_error_table
from app.cli import enable_utf8_stdio
from app.scenarios.app import scenario_runner_factory
from app.scenarios.loader import load_scenarios


def _run_scenario(scenario_id: str, query: str) -> list[dict]:
    """跑一个场景，返回它的事件流。"""
    token = CancelToken(f"cli-{scenario_id}")
    runner = scenario_runner_factory(
        f"场景:{scenario_id} {query}", token, f"cli-{scenario_id}"
    )
    return list(runner.events())


def _check() -> int:
    scenarios = load_scenarios()
    problems: list[str] = []
    print("== 1/2 失败场景（每类都必须演出它声称的结局） ==")
    for scenario in scenarios.values():
        events = _run_scenario(scenario.id, "华北销售额")
        terminal = events[-1]
        if scenario.expected_outcome == "result":
            if terminal["type"] != "result":
                problems.append(
                    f"{scenario.id}: 应当成功，实际是 {terminal.get('code')}"
                )
                actual = terminal.get("code", "")
            else:
                actual = "result"
            expect = "result"
        else:
            if terminal["type"] != "error":
                problems.append(f"{scenario.id}: 应当失败，实际成功了")
                actual = "result"
            else:
                actual = terminal["code"]
                if actual != scenario.expected_code:
                    problems.append(
                        f"{scenario.id}: 错误码不符（期望 {scenario.expected_code}，"
                        f"实际 {actual}）"
                    )
                if terminal["class"] != scenario.expected_class:
                    problems.append(
                        f"{scenario.id}: 类别不符（期望 {scenario.expected_class}，"
                        f"实际 {terminal['class']}）"
                    )
                if terminal["step"] != scenario.expected_step:
                    problems.append(
                        f"{scenario.id}: 失败步骤不符（期望 {scenario.expected_step}，"
                        f"实际 {terminal['step']}）"
                    )
            expect = scenario.expected_code or ""
        print(f"  {scenario.id:20s} 期望 {expect:20s} 实际 {actual:20s}")

    print("== 2/2 错误码表（路线图点名的 7 个码必须都在） ==")
    table = load_error_table()

    missing = [code for code in ROADMAP_CODES if code not in table]
    if missing:
        problems.append(f"错误码表缺少路线图要求的码：{missing}")
    print(f"  错误码表：{len(table.codes)} 个码，路线图要求的 {len(ROADMAP_CODES)} 个全在")

    if problems:
        print(f"FAIL: {len(problems)} 项不符合预期", file=sys.stderr)
        for item in problems:
            print(f"  - {item}", file=sys.stderr)
        return 1
    print("S6 PASS")
    return 0


def _table() -> int:
    table = load_error_table()
    print("| 错误码 | 名称 | 类别 | 可重试 | 说明 |")
    print("|---|---|---|---|---|")
    for spec in table.specs:
        print(
            f"| `{spec.code}` | {spec.label} | {spec.klass} | "
            f"{'是' if spec.retryable else '否'} | {spec.message} |"
        )
    return 0


def _scenarios() -> int:
    scenarios = load_scenarios()
    print("| 场景 | 名称 | 期望 | 类别 | 失败步骤 |")
    print("|---|---|---|---|---|")
    for scenario in scenarios.values():
        expect = scenario.expected_code or "result"
        klass = scenario.expected_class or "-"
        step = scenario.expected_step or "-"
        print(f"| `{scenario.id}` | {scenario.title} | {expect} | {klass} | {step} |")
    return 0


def main(argv: Sequence[str] | None = None) -> int:
    enable_utf8_stdio()
    parser = argparse.ArgumentParser(
        prog="python -m app.scripts.failures",
        description="S6 失败通道：错误码表 + 6 类失败场景",
    )
    group = parser.add_mutually_exclusive_group(required=True)
    group.add_argument(
        "--check",
        action="store_true",
        help="离线门禁：6 类失败各跑一遍（不需要 MySQL / LLM）",
    )
    group.add_argument("--table", action="store_true", help="打印错误码表")
    group.add_argument("--scenarios", action="store_true", help="打印失败场景表")
    args = parser.parse_args(argv)

    if args.check:
        return _check()
    if args.table:
        return _table()
    return _scenarios()


if __name__ == "__main__":
    raise SystemExit(main())
