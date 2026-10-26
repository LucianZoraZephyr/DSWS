"""S6 · 校验关卡。

一条 SQL 要被执行，必须先过**两道关**：

    guard  —— S4 的静态校验（语义）：表/列在不在上下文里、有没有常量投影、
               排名意图有没有 ORDER BY / LIMIT、是不是单条只读语句
    policy —— S5 的安全策略（安全）：白名单、禁 DDL/DML/INTO、集合运算、
               危险函数、可执行注释，并自动补 LIMIT

两道关合起来就是「校验SQL」这一个步骤。旧系统在这里只做
`f"explain {sql}"`——字符串拼接，不查语句条数，UPDATE/DELETE 全部放行
（审计 D6）。现在它是**代码里的两道函数**，返回的是**有名字的错误**。

为什么把 S5 的策略也放进校验步骤，而不是只留在执行器里：
    - 执行器自己也会再查一遍策略（纵深防御，别的调用方也受保护）；
    - 但从编排的角度，「这条 SQL 不该被执行」这件事应该在**执行之前**
      就变成一个错误事件，而不是让用户等到执行步骤才看到红叉。
"""

from __future__ import annotations

from app.agent.errors import ChannelError, build_error
from app.generate.guard import check_sql
from app.generate.models import GenerateContext
from app.security.policy import enforce

#: 校验关卡的默认值：语义 + 安全都查。
DEFAULT_GATES: tuple[str, ...] = ("guard", "policy")


def validate_sql(
    sql: str,
    context: GenerateContext,
    query: str,
    *,
    gates: tuple[str, ...] = DEFAULT_GATES,
) -> ChannelError | None:
    """校验一条 SQL。返回 `None` 表示通过。

    失败时返回**通道错误**：码来自 `contracts/errors.yaml`，原因码
    （`reason_code`）保留更细的来源（静态校验的违规码 / 策略的原因码）。
    """
    text = str(sql or "")

    if "guard" in gates:
        report = check_sql(text, context, query)
        if not report.ok:
            first = report.violations[0]
            message = first.message
            if first.detail:
                message = f"{message}：{first.detail}"
            return build_error(
                first.code,
                message,
                reason_code=first.code,
                sql=text,
            )

    if "policy" in gates:
        decision = enforce(text)
        if not decision.ok:
            first = decision.violations[0]
            message = f"{first.message}：{first.detail}" if first.detail else first.message
            return build_error(
                "SQL_REJECTED",
                message,
                reason_code=first.code,
                sql=text,
            )

    return None
