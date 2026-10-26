"""S6 · 场景应用。

    cd backend && uv run python -m app.scenarios.app --port 8102

它起的是**同一个应用**（`app.main.create_app`），只是把编排器的两个外部
依赖换成了夹具里的替身：

    模型   → 夹具里的 generator / corrector
    数据库 → 夹具里的 executor（**策略仍然是真的**）

所以浏览器里看到的每一个红叉，都对应一次真实的判定：
真实的上下文构建、真实的提示词渲染、真实的 AST 静态校验、真实的安全
策略、真实的校正闭环、真实的 SSE 序列化。
"""

from __future__ import annotations

import time
from typing import Any

from fastapi import FastAPI, HTTPException

from app.agent.cancel import CancelToken
from app.agent.correct import SqlCorrector
from app.agent.orchestrator import AgentRunner
from app.main import create_app
from app.scenarios.loader import Scenario, load_scenarios, split_query
from app.security.executor import ExecutionError, ExecutionResult
from app.security.policy import enforce


class ScenarioGenerator:
    """夹具生成器：返回夹具里写好的 SQL 或拒答。"""

    name = "scenario"

    def __init__(self, scenario: Scenario) -> None:
        self._scenario = scenario

    def generate(self, context: Any, prompt: str) -> dict[str, Any]:
        if self._scenario.generator_refusal is not None:
            return {"refusal": dict(self._scenario.generator_refusal)}
        return {"sql": self._scenario.generator_sql or ""}


class ScenarioCorrector:
    """夹具校正器。"""

    name = "scenario"

    def __init__(self, scenario: Scenario) -> None:
        self._scenario = scenario

    def correct(
        self,
        context: Any,
        prompt: str,
        sql: str,
        error: str,
    ) -> dict[str, Any]:
        if self._scenario.corrector_refusal is not None:
            return {"refusal": dict(self._scenario.corrector_refusal)}
        return {"sql": self._scenario.corrector_sql or ""}


class ScenarioExecutor:
    """夹具执行器。

    **安全策略仍然是真的**：它先跑一遍 S5 的 `enforce()`，被拒就返回
    `SQL_REJECTED`。夹具只负责「数据库会返回什么」。
    """

    def __init__(self, scenario: Scenario) -> None:
        self._scenario = scenario

    def execute(self, sql: str) -> ExecutionResult:
        if self._scenario.executor_delay_s:
            # 故意慢一点：好让浏览器测试有时间点「停止」。
            time.sleep(self._scenario.executor_delay_s)

        decision = enforce(sql)
        if not decision.ok:
            first = decision.violations[0]
            message = (
                f"{first.message}：{first.detail}" if first.detail else first.message
            )
            return ExecutionResult(
                ok=False,
                sql="",
                error=ExecutionError(
                    code="SQL_REJECTED",
                    message=message,
                    reason_code=first.code,
                ),
                decision=decision,
            )

        scenario = self._scenario
        if scenario.executor_error_code:
            return ExecutionResult(
                ok=False,
                sql=decision.sql,
                error=ExecutionError(
                    code=scenario.executor_error_code,
                    message=scenario.executor_error_message,
                    reason_code=scenario.executor_error_code,
                ),
                decision=decision,
            )

        columns = scenario.executor_columns
        rows = tuple(
            dict(zip(columns, values)) for values in scenario.executor_rows
        )
        return ExecutionResult(
            ok=True,
            sql=decision.sql,
            columns=columns,
            rows=rows,
            decision=decision,
        )


def scenario_runner_factory(
    query: str,
    cancel: CancelToken,
    request_id: str,
) -> AgentRunner:
    """按问题里的场景前缀挑一个剧本。"""
    scenarios = load_scenarios()
    scenario_id, real_query = split_query(query)
    scenario = scenarios.get(scenario_id)
    if scenario is None:
        raise HTTPException(
            status_code=422,
            detail=(
                f"没有名为 {scenario_id!r} 的场景；"
                f"可用：{sorted(scenarios)}"
            ),
        )
    if not real_query:
        raise HTTPException(
            status_code=422,
            detail=f"场景 {scenario_id!r} 需要一个真实问题，例如「场景:{scenario_id} 华北销售额」",
        )

    corrector: SqlCorrector | None = None
    if scenario.corrector_sql or scenario.corrector_refusal:
        corrector = ScenarioCorrector(scenario)

    return AgentRunner(
        query=real_query,
        generator=ScenarioGenerator(scenario),
        corrector=corrector,
        executor=ScenarioExecutor(scenario),
        cancel=cancel,
        request_id=request_id,
    )


def create_scenario_app() -> FastAPI:
    """场景应用：与生产应用共用同一套 HTTP / SSE 管道。"""
    return create_app(scenario_runner_factory)


app = create_scenario_app()


if __name__ == "__main__":  # pragma: no cover - 手动起服务用
    import argparse

    import uvicorn

    parser = argparse.ArgumentParser(
        prog="python -m app.scenarios.app",
        description="S6 场景应用：只替换模型与数据库，其余全真",
    )
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8102)
    args = parser.parse_args()
    uvicorn.run(app, host=args.host, port=args.port)
