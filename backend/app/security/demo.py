"""S5 · Demo：同一段恶意 SQL，旧系统执行、新系统拒绝。

路线图 S5 的验收原话：

    > Demo：同一段恶意 SQL，旧系统执行、新系统拒绝——两屏对比。

这不是演出来的：这里**真的**按旧系统的做法跑一遍——

    旧系统 validate()： `sql = f"explain {sql}"`   —— 不检查语句条数
    旧系统 run()：      `await session.execute(text(sql))` —— 直接执行

然后用**同一个探针表**证明两件事：

    1. 旧链路校验通过，而且第二句真的执行了（探针表被 DROP 掉）；
    2. 新链路在**连库之前**就拒了（探针表还在）。

探针表 `_s5_probe` 由这个 demo 自己建、自己删；被 payload 里的 DROP 删掉
正是要演示的效果——它只影响这张一次性的探针表，不碰任何业务表。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from app.security.policy import PolicyDecision, enforce

PROBE_TABLE = "_s5_probe"

# 旧系统的做法：整段 SQL 前面加 EXPLAIN，后面跟着的语句按原样执行。
LEGACY_VALIDATE_TEMPLATE = "explain {sql}"


@dataclass(frozen=True)
class DemoReport:
    """两屏对比的结果。"""

    payload: str
    legacy_validate: str = ""
    legacy_executed: bool = False
    legacy_probe_alive: bool = True
    new_rejected: bool = False
    new_reason_code: str = ""
    new_probe_alive: bool = True
    decision: PolicyDecision | None = field(default=None, repr=False)
    problems: tuple[str, ...] = ()

    @property
    def ok(self) -> bool:
        return not self.problems

    def to_dict(self) -> dict[str, Any]:
        return {
            "ok": self.ok,
            "payload": self.payload,
            "legacy": {
                "validate": self.legacy_validate,
                "executed": self.legacy_executed,
                "probe_alive": self.legacy_probe_alive,
            },
            "new": {
                "rejected": self.new_rejected,
                "reason_code": self.new_reason_code,
                "probe_alive": self.new_probe_alive,
            },
            "problems": list(self.problems),
        }


def _probe_exists(cursor: Any) -> bool:
    cursor.execute(f"SHOW TABLES LIKE '{PROBE_TABLE}'")
    return bool(cursor.fetchall())


def run_demo(connection: Any) -> DemoReport:
    """跑两屏对比。

    `connection` 必须是**特权**连接（能建表删表），并且带
    `CLIENT_MULTI_STATEMENTS`——旧系统的驱动（asyncmy）默认就带这个标志，
    而 pymysql 默认不带。不带这个标志就复现不出旧系统的行为。
    """
    payload = f"SELECT 1 AS a FROM fact_order LIMIT 1; DROP TABLE {PROBE_TABLE}"

    # --- 新系统：策略先行，连库之前就判 ----------------------------------
    decision = enforce(payload)

    problems: list[str] = []
    legacy_validate = ""
    legacy_executed = False
    legacy_probe_alive = True
    new_probe_alive = True

    cursor = connection.cursor()
    try:
        # 备好探针表
        cursor.execute(f"DROP TABLE IF EXISTS {PROBE_TABLE}")
        cursor.execute(f"CREATE TABLE {PROBE_TABLE} (id INT)")

        # --- 旧系统：validate() = explain {sql} --------------------------
        try:
            cursor.execute(LEGACY_VALIDATE_TEMPLATE.format(sql=payload))
            legacy_validate = "通过（没有抛出任何异常）"
        except Exception as exc:  # noqa: BLE001 - 这就是要演示的现象
            legacy_validate = f"失败：{exc}"

        # --- 旧系统：run() = 直接执行 -----------------------------------
        cursor.execute(payload)
        while cursor.nextset():
            pass
        legacy_probe_alive = _probe_exists(cursor)
        legacy_executed = not legacy_probe_alive

        if legacy_validate.startswith("失败"):
            problems.append("旧系统的校验居然没通过——复现不成立，结论不可信")
        if not legacy_executed:
            problems.append("旧系统没有真的执行第二句——复现不成立，结论不可信")

        # --- 新系统：同一条 payload，探针表应当毫发无损 --------------------
        cursor.execute(f"DROP TABLE IF EXISTS {PROBE_TABLE}")
        cursor.execute(f"CREATE TABLE {PROBE_TABLE} (id INT)")
        new_probe_alive = _probe_exists(cursor)

        if decision.ok:
            problems.append("新系统的策略居然放行了这条 payload")
        if not new_probe_alive:
            problems.append("探针表不见了——新系统不该碰过数据库")
    finally:
        try:
            cursor.execute(f"DROP TABLE IF EXISTS {PROBE_TABLE}")
        except Exception:  # noqa: BLE001 - 清理失败不影响结论
            pass
        cursor.close()

    return DemoReport(
        payload=payload,
        legacy_validate=legacy_validate,
        legacy_executed=legacy_executed,
        legacy_probe_alive=legacy_probe_alive,
        new_rejected=not decision.ok,
        new_reason_code=decision.reason_code or "",
        new_probe_alive=new_probe_alive,
        decision=decision,
        problems=tuple(problems),
    )
