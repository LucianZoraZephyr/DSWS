"""S4 · 生成层的输入 / 输出契约。

这一层刻意不引入任何图框架，也**不引入任何模型**。生成层的输入是
`GenerateContext`（一份已经被裁剪过的「LLM 将看到的上下文」），输出是
`GenerationResult`（要么一条 SQL，要么一个拒答）。

为什么要有 `GenerateContext`：
    S3 的 `RecallState` 是检索层的内部状态，里面塞满了召回分数、候选、
    trace 等只服务于检索过程的信息。生成层只需要「表 / 字段 / 指标 / 取值 /
    数据边界 / 数据库环境」这六类事实。把它们抽成一个独立的小契约，
    生成层就能被离线测试，而不需要 MySQL / ES / Qdrant 全都起着。

两个入口产出同一种 `GenerateContext`：
    1. `app.generate.context.build_offline_context` —— 只读契约，离线可跑
    2. `app.generate.context.context_from_recall_state` —— 接 S3 的真实召回

输出只有两种形状，与 `contracts/sql.output.schema.json` 的 `sql_or_refusal`
一一对应。**没有第三种形状**：所以「模型把错误信息写成一行数据」这件事在
类型层面就不可能发生（审计根因 B）。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any


@dataclass(frozen=True)
class ColumnLite:
    """生成层看到的字段。id 固定为 `表.列`。"""

    id: str
    name: str
    table_id: str
    type: str
    role: str
    description: str = ""
    aliases: tuple[str, ...] = ()
    value_domain: dict[str, Any] = field(default_factory=dict)

    @property
    def is_key(self) -> bool:
        return self.role in ("primary_key", "foreign_key")

    @property
    def is_dimension(self) -> bool:
        return self.role == "dimension"

    @property
    def is_measure(self) -> bool:
        return self.role == "measure"

    @property
    def is_time_dimension(self) -> bool:
        return self.table_id == "dim_date"


@dataclass(frozen=True)
class TableLite:
    """生成层看到的表。grain 与 measures 是契约里 DDL 表达不了的东西。"""

    name: str
    role: str = ""
    grain: str = ""
    description: str = ""
    primary_key: str | None = None
    foreign_keys: tuple[dict[str, str], ...] = ()

    def references(self, column_name: str) -> str | None:
        """返回 `column_name` 外键指向的 `表.列`；不是外键则返回 None。"""
        for item in self.foreign_keys:
            if item.get("column") == column_name:
                return str(item.get("references") or "") or None
        return None


@dataclass(frozen=True)
class MetricLite:
    """生成层看到的指标。formula 本身就是可执行的 SQL 表达式。"""

    name: str
    label: str = ""
    description: str = ""
    formula: str = ""
    relevant_columns: tuple[str, ...] = ()
    aliases: tuple[str, ...] = ()
    aggregation: str = ""
    expected_value: float = 0.0
    grain: str = ""
    rejected_values: tuple[dict[str, Any], ...] = ()

    @property
    def surfaces(self) -> tuple[str, ...]:
        """指标可用于匹配的词面：name / label / aliases。"""
        return tuple(
            item for item in (self.name, self.label, *self.aliases) if item
        )


@dataclass(frozen=True)
class ValueLite:
    """命中的一条真实取值（来自契约声明的枚举值域）。"""

    value: str
    column_id: str

    @property
    def table_id(self) -> str:
        return self.column_id.split(".", 1)[0]


@dataclass(frozen=True)
class GenerateContext:
    """生成层的全部输入。字段顺序即提示词的槽位顺序。"""

    query: str
    tables: tuple[TableLite, ...] = ()
    columns: tuple[ColumnLite, ...] = ()
    metrics: tuple[MetricLite, ...] = ()
    values: tuple[ValueLite, ...] = ()
    dataset: dict[str, Any] = field(default_factory=dict)
    date_info: dict[str, Any] = field(default_factory=dict)
    db_info: dict[str, Any] = field(default_factory=dict)

    # -- 查询辅助 -------------------------------------------------------
    def table(self, name: str) -> TableLite | None:
        for table in self.tables:
            if table.name == name:
                return table
        return None

    def column(self, column_id: str) -> ColumnLite | None:
        for column in self.columns:
            if column.id == column_id:
                return column
        return None

    def column_names(self, table_name: str) -> set[str]:
        return {c.name for c in self.columns if c.table_id == table_name}

    def table_names(self) -> set[str]:
        return {t.name for t in self.tables}


@dataclass(frozen=True)
class Refusal:
    """一次显式拒答。code 必须是契约里允许的码。"""

    code: str
    reason: str

    def to_dict(self) -> dict[str, Any]:
        return {"refusal": {"code": self.code, "reason": self.reason}}


@dataclass(frozen=True)
class GuardViolation:
    """静态校验抓到的一条违规。"""

    code: str
    message: str
    detail: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {"code": self.code, "message": self.message, "detail": self.detail}


@dataclass(frozen=True)
class GuardReport:
    """静态校验结论。ok 为 False 时 violations 一定非空。"""

    ok: bool
    violations: tuple[GuardViolation, ...] = ()

    def to_dict(self) -> dict[str, Any]:
        return {
            "ok": self.ok,
            "violations": [item.to_dict() for item in self.violations],
        }


@dataclass(frozen=True)
class GenerationResult:
    """生成层的输出。

    `outcome` 只有两种取值：
        - "sql"     —— 生成成功，sql 非空
        - "refusal" —— 显式拒答，refusal 非空

    静态校验不通过的 SQL **不会**以 "sql" 的形态返回：它会被降级成拒答
    （code 取具体的违规码）。这样下游拿到的永远是「一条过了校验的 SQL」
    或「一个有名字的失败原因」，不会有第三种状态。
    """

    query: str
    outcome: str
    sql: str | None = None
    refusal: Refusal | None = None
    guard: GuardReport = field(default_factory=lambda: GuardReport(ok=True))
    prompt: str = ""
    slots: dict[str, str] = field(default_factory=dict)

    @property
    def ok(self) -> bool:
        return self.outcome == "sql"

    def to_payload(self) -> dict[str, Any]:
        """符合 `contracts/sql.output.schema.json#/$defs/sql_or_refusal`。"""
        if self.outcome == "sql":
            return {"sql": self.sql or ""}
        assert self.refusal is not None
        return self.refusal.to_dict()
