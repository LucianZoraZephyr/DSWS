"""S6 · 失败场景夹具与场景应用。

`app.scenarios.loader` 读 `fixtures/failures/scenarios.yaml`；
`app.scenarios.app` 起一个**只替换外部依赖**的应用：模型、数据库换成
夹具里的替身，校验、策略、事件、协议全部是真的。

它存在的唯一理由：路线图 S6 的验收要求「6 类失败各一个 Playwright 用例」，
而浏览器测试需要**可复现**的失败——不能指望真模型恰好吐出一条坏 SQL。
"""

from app.scenarios.loader import (
    SCENARIO_PREFIX,
    Scenario,
    ScenarioError,
    load_scenarios,
    split_query,
)

__all__ = [
    "SCENARIO_PREFIX",
    "Scenario",
    "ScenarioError",
    "load_scenarios",
    "split_query",
]
