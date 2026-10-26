"""S6 · 失败场景夹具的加载。

`fixtures/failures/scenarios.yaml` 是「6 类失败」在浏览器里的剧本。
加载是**严格**的：缺字段、类型不对、id 重复、期望的码不在错误码表里，
全部直接抛异常——夹具本身漂移了，测试就会给出假绿。

夹具的形状：

    generator   模型返回什么（一条 SQL，或一个拒答）
    corrector   校正器返回什么（一条 SQL，或一个拒答；不写就是没有校正器）
    executor    数据库返回什么（若干行，或一个错误；不写就是执行失败）
    expect      期望的结局（outcome / code / class / step / rows）

`expect` 是**断言的依据**：浏览器测试与 CLI 都拿它对照实际事件。
"""

from __future__ import annotations

from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path
from typing import Any

import yaml

from app.agent.errors import load_error_table

REBUILD_ROOT = Path(__file__).resolve().parents[3]
SCENARIOS_PATH = REBUILD_ROOT / "fixtures" / "failures" / "scenarios.yaml"

#: 场景前缀：「场景:<id> <真实问题>」。
SCENARIO_PREFIX = "场景:"


class ScenarioError(Exception):
    """场景夹具本身有问题。"""


@dataclass(frozen=True)
class Scenario:
    """一个场景。"""

    id: str
    title: str
    desc: str
    generator_sql: str | None
    generator_refusal: dict[str, Any] | None
    corrector_sql: str | None
    corrector_refusal: dict[str, Any] | None
    executor_error_code: str | None
    executor_error_message: str
    executor_columns: tuple[str, ...]
    executor_rows: tuple[tuple[Any, ...], ...]
    executor_delay_s: float
    expect: dict[str, Any]

    @property
    def expected_code(self) -> str | None:
        return self.expect.get("code")

    @property
    def expected_class(self) -> str | None:
        return self.expect.get("class")

    @property
    def expected_step(self) -> str | None:
        return self.expect.get("step")

    @property
    def expected_outcome(self) -> str:
        return str(self.expect.get("outcome") or "error")


def _require(mapping: dict[str, Any], key: str, name: str) -> Any:
    if key not in mapping:
        raise ScenarioError(f"场景 {name} 缺少 {key}")
    return mapping[key]


def _parse_generator(raw: Any, name: str) -> tuple[str | None, dict | None]:
    if raw is None:
        raise ScenarioError(f"场景 {name} 缺少 generator")
    if not isinstance(raw, dict):
        raise ScenarioError(f"场景 {name} 的 generator 必须是映射")
    unknown = sorted(set(raw) - {"sql", "refusal"})
    if unknown:
        raise ScenarioError(f"场景 {name} 的 generator 出现未定义的键：{unknown}")
    sql = raw.get("sql")
    refusal = raw.get("refusal")
    if bool(sql) == bool(refusal):
        raise ScenarioError(
            f"场景 {name} 的 generator 必须**恰好**给 sql 或 refusal 之一"
        )
    if refusal is not None:
        if not isinstance(refusal, dict):
            raise ScenarioError(f"场景 {name} 的 refusal 必须是映射")
        for key in ("code", "reason"):
            if key not in refusal:
                raise ScenarioError(f"场景 {name} 的 refusal 缺少 {key}")
    return (str(sql) if sql else None, refusal)


def _parse_corrector(raw: Any, name: str) -> tuple[str | None, dict | None]:
    if raw is None:
        return (None, None)
    if not isinstance(raw, dict):
        raise ScenarioError(f"场景 {name} 的 corrector 必须是映射")
    unknown = sorted(set(raw) - {"sql", "refusal"})
    if unknown:
        raise ScenarioError(f"场景 {name} 的 corrector 出现未定义的键：{unknown}")
    sql = raw.get("sql")
    refusal = raw.get("refusal")
    if bool(sql) == bool(refusal):
        raise ScenarioError(
            f"场景 {name} 的 corrector 必须**恰好**给 sql 或 refusal 之一"
        )
    if refusal is not None:
        if not isinstance(refusal, dict):
            raise ScenarioError(f"场景 {name} 的 refusal 必须是映射")
        for key in ("code", "reason"):
            if key not in refusal:
                raise ScenarioError(f"场景 {name} 的 refusal 缺少 {key}")
    return (str(sql) if sql else None, refusal)


def _parse_executor(
    raw: Any, name: str
) -> tuple[str | None, str, tuple[str, ...], tuple, float]:
    if raw is None:
        raise ScenarioError(f"场景 {name} 缺少 executor")
    if not isinstance(raw, dict):
        raise ScenarioError(f"场景 {name} 的 executor 必须是映射")
    unknown = sorted(set(raw) - {"error", "columns", "rows", "unused", "delay_s"})
    if unknown:
        raise ScenarioError(f"场景 {name} 的 executor 出现未定义的键：{unknown}")

    delay = raw.get("delay_s", 0.0)
    if not isinstance(delay, (int, float)) or isinstance(delay, bool) or delay < 0:
        raise ScenarioError(f"场景 {name} 的 executor.delay_s 必须是非负数")

    error = raw.get("error")
    if error is not None:
        if not isinstance(error, dict):
            raise ScenarioError(f"场景 {name} 的 executor.error 必须是映射")
        for key in ("code", "message"):
            if key not in error:
                raise ScenarioError(f"场景 {name} 的 executor.error 缺少 {key}")
        code = str(error["code"])
        if code not in load_error_table():
            raise ScenarioError(
                f"场景 {name} 的 executor.error.code={code!r} 不在错误码表里"
            )
        return (code, str(error["message"]), (), (), float(delay))

    columns = raw.get("columns")
    rows = raw.get("rows")
    if columns is None:
        columns = []
    if rows is None:
        rows = []
    if not isinstance(columns, list) or not isinstance(rows, list):
        raise ScenarioError(f"场景 {name} 的 executor.columns / rows 必须是列表")
    names = tuple(str(item) for item in columns)
    parsed_rows: list[tuple[Any, ...]] = []
    for row in rows:
        if not isinstance(row, list):
            raise ScenarioError(f"场景 {name} 的 executor.rows 每行必须是列表")
        if names and len(row) != len(names):
            raise ScenarioError(
                f"场景 {name} 的 executor 有 {len(row)} 个值，"
                f"但有 {len(names)} 列"
            )
        parsed_rows.append(tuple(row))
    return (None, "", names, tuple(parsed_rows), float(delay))


def _parse_expect(raw: Any, name: str) -> dict[str, Any]:
    if not isinstance(raw, dict):
        raise ScenarioError(f"场景 {name} 缺少 expect")
    unknown = sorted(set(raw) - {"outcome", "code", "class", "step", "rows"})
    if unknown:
        raise ScenarioError(f"场景 {name} 的 expect 出现未定义的键：{unknown}")
    outcome = str(raw.get("outcome") or "")
    if outcome not in ("result", "error"):
        raise ScenarioError(
            f"场景 {name} 的 expect.outcome 必须是 result 或 error，"
            f"实际是 {outcome!r}"
        )
    expect = dict(raw)
    code = expect.get("code")
    if outcome == "error":
        if not code:
            raise ScenarioError(f"场景 {name} 期望失败，必须写 expect.code")
        if str(code) not in load_error_table():
            raise ScenarioError(
                f"场景 {name} 的 expect.code={code!r} 不在错误码表里"
            )
        klass = expect.get("class")
        if klass is not None and str(klass) not in load_error_table().classes:
            raise ScenarioError(f"场景 {name} 的 expect.class={klass!r} 非法")
    return expect


def parse_scenarios(data: Any) -> dict[str, Scenario]:
    """校验并转成场景表。"""
    if not isinstance(data, dict):
        raise ScenarioError("场景夹具不是 YAML 映射")
    unknown = sorted(set(data) - {"version", "default", "prefix", "scenarios"})
    if unknown:
        raise ScenarioError(f"场景夹具出现未定义的键：{unknown}")
    if data.get("version") != 1:
        raise ScenarioError("场景夹具 version 必须为 1")

    raw_scenarios = data.get("scenarios")
    if not isinstance(raw_scenarios, list) or not raw_scenarios:
        raise ScenarioError("场景夹具 scenarios 必须是非空列表")

    scenarios: dict[str, Scenario] = {}
    for raw in raw_scenarios:
        if not isinstance(raw, dict):
            raise ScenarioError("场景必须是映射")
        unknown = sorted(
            set(raw) - {"id", "title", "desc", "generator", "corrector", "executor", "expect"}
        )
        if unknown:
            raise ScenarioError(f"场景出现未定义的键：{unknown}")
        name = str(_require(raw, "id", "场景"))
        if name in scenarios:
            raise ScenarioError(f"场景 id 重复：{name}")
        title = str(_require(raw, "title", name))
        desc = str(raw.get("desc") or "")
        generator_sql, generator_refusal = _parse_generator(raw.get("generator"), name)
        corrector_sql, corrector_refusal = _parse_corrector(raw.get("corrector"), name)
        error_code, error_message, columns, rows, delay = _parse_executor(
            raw.get("executor"), name
        )
        expect = _parse_expect(raw.get("expect"), name)
        scenarios[name] = Scenario(
            id=name,
            title=title,
            desc=desc,
            generator_sql=generator_sql,
            generator_refusal=generator_refusal,
            corrector_sql=corrector_sql,
            corrector_refusal=corrector_refusal,
            executor_error_code=error_code,
            executor_error_message=error_message,
            executor_columns=columns,
            executor_rows=rows,
            executor_delay_s=delay,
            expect=expect,
        )

    default = str(data.get("default") or "")
    if default not in scenarios:
        raise ScenarioError(f"场景夹具的 default={default!r} 不存在")
    return scenarios


@lru_cache(maxsize=1)
def load_scenarios(path: Path = SCENARIOS_PATH) -> dict[str, Scenario]:
    """读 `fixtures/failures/scenarios.yaml` 并校验。"""
    data = yaml.safe_load(path.read_text(encoding="utf-8"))
    return parse_scenarios(data)


@lru_cache(maxsize=1)
def load_default_id(path: Path = SCENARIOS_PATH) -> str:
    """没有场景前缀时走哪个场景。"""
    data = yaml.safe_load(path.read_text(encoding="utf-8"))
    default = str(data.get("default") or "")
    if default not in load_scenarios(path):
        raise ScenarioError(f"场景夹具的 default={default!r} 不存在")
    return default


def split_query(query: str) -> tuple[str, str]:
    """「场景:<id> <真实问题>」→ `(<id>, <真实问题>)`。

    没有前缀时返回 `(default, 整句)`。**真实问题**才是交给流水线的问题，
    所以上下文、提示词、校验全都是真的。
    """
    text = str(query or "")
    if not text.startswith(SCENARIO_PREFIX):
        return (load_default_id(), text)
    body = text[len(SCENARIO_PREFIX) :].strip()
    if not body:
        return ("", text)
    scenario_id, _, real_query = body.partition(" ")
    return (scenario_id.strip(), real_query.strip() or body)
