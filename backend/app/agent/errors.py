"""S6 · 错误码表与失败通道。

这一层是路线图 S6 的地基：「让每一种失败都有名字」。

审计里最刺眼的一条不是「报错了」，而是**报错了但没人知道**：

    > 后端一次都没发过 `type=error`；伪 SQL 被当成一行数据返回；
    > 流程图 11 个节点全绿；文案是「查询完成，共 1 行结果。」

所以这里做三件事：

    1. **一张表**（`contracts/errors.yaml`）——每个失败码带名字、类别、
       文案、能不能重试。代码层严格加载：少一个键、多一个键都抛异常。
    2. **一个出口**（`ChannelError`）——所有失败都变成同一个形状：
       `code` / `class` / `message` / `step` / `detail`。
    3. **一条映射规则**——从各层的内部码映射到通道码：
       在表里 → 就是它自己；不在表里 → `LLM_REFUSAL`。

第 3 条是刻意的：**表里没有的码就是「模型自己拒答」**。S4 的生成器会
主动拒答（`OUT_OF_RANGE` / `UNSUPPORTED_INTENT` / …），那些码不是失败类别，
是模型的判断。把它们统统归到 `LLM_REFUSAL` 一个通道码上，既不丢名字
（`detail.reason_code` 里还留着原码），又不需要在两张表之间手工同步。

`class` 决定 UI 怎么渲染，三种取值互不相同：

    failure    真失败：红框 + 失败步骤红叉
    empty      合法但没有数据：空态卡，**不标红**（审计 FP-8）
    cancelled  用户取消：取消态，**既不是 done 也不是失败**
"""

from __future__ import annotations

from dataclasses import dataclass, field
from functools import lru_cache
from pathlib import Path
from typing import Any, Mapping

import yaml

REBUILD_ROOT = Path(__file__).resolve().parents[3]
ERRORS_PATH = REBUILD_ROOT / "contracts" / "errors.yaml"

# -- 失败类别 ---------------------------------------------------------------
FAILURE = "failure"
EMPTY = "empty"
CANCELLED = "cancelled"

# 通道码：路线图 S6 点名的 7 个。表里的其他码是「让每一种失败都有名字」
# 补齐的，但通道码本身只有这几个是硬性要求。
SQL_REJECTED = "SQL_REJECTED"
SQL_SYNTAX = "SQL_SYNTAX"
SQL_UNKNOWN_COLUMN = "SQL_UNKNOWN_COLUMN"
EMPTY_RESULT = "EMPTY_RESULT"
LLM_REFUSAL = "LLM_REFUSAL"
TIMEOUT = "TIMEOUT"
CANCELLED_CODE = "CANCELLED"
INTERNAL_ERROR = "INTERNAL_ERROR"

#: 路线图点名的 7 个通道码。`tests/agent/test_contract_alignment.py` 会
#: 逐个核对表里确实有它们。
ROADMAP_CODES: tuple[str, ...] = (
    SQL_REJECTED,
    SQL_SYNTAX,
    SQL_UNKNOWN_COLUMN,
    EMPTY_RESULT,
    LLM_REFUSAL,
    TIMEOUT,
    CANCELLED_CODE,
)


class ErrorTableError(Exception):
    """错误码表本身有问题（缺键、多键、类别非法、码重复）。"""


@dataclass(frozen=True)
class ErrorCodeSpec:
    """一个错误码的全部契约信息。"""

    code: str
    label: str
    klass: str
    retryable: bool
    message: str

    def to_dict(self) -> dict[str, Any]:
        return {
            "code": self.code,
            "label": self.label,
            "class": self.klass,
            "retryable": self.retryable,
            "message": self.message,
        }


@dataclass(frozen=True)
class CorrectionPolicy:
    """校正闭环的策略（契约里的 `correction` 段）。"""

    max_attempts: int
    prompt: str
    revalidate: bool
    revalidate_with: tuple[str, ...]

    def to_dict(self) -> dict[str, Any]:
        return {
            "max_attempts": self.max_attempts,
            "prompt": self.prompt,
            "revalidate": self.revalidate,
            "revalidate_with": list(self.revalidate_with),
        }


@dataclass(frozen=True)
class ErrorTable:
    """`contracts/errors.yaml` 的运行时视图。"""

    version: int
    classes: tuple[str, ...]
    specs: tuple[ErrorCodeSpec, ...]
    correction: CorrectionPolicy

    @property
    def codes(self) -> tuple[str, ...]:
        return tuple(spec.code for spec in self.specs)

    def get(self, code: str) -> ErrorCodeSpec:
        for spec in self.specs:
            if spec.code == code:
                return spec
        raise ErrorTableError(f"错误码 {code!r} 不在 contracts/errors.yaml 里")

    def __contains__(self, code: object) -> bool:
        return any(spec.code == code for spec in self.specs)

    def to_dict(self) -> dict[str, Any]:
        return {
            "version": self.version,
            "classes": list(self.classes),
            "codes": [spec.to_dict() for spec in self.specs],
            "correction": self.correction.to_dict(),
        }


def _exact_keys(section: Mapping[str, Any], name: str, expected: set[str]) -> None:
    actual = set(section)
    missing = sorted(expected - actual)
    unknown = sorted(actual - expected)
    if missing:
        raise ErrorTableError(f"错误码表 {name} 段缺少键：{missing}")
    if unknown:
        raise ErrorTableError(f"错误码表 {name} 段出现未定义的键：{unknown}")


def parse_error_table(data: Any) -> ErrorTable:
    """校验并转成 `ErrorTable`。任何不合契约的输入都抛异常。"""
    if not isinstance(data, dict):
        raise ErrorTableError("错误码表不是 YAML 映射")
    _exact_keys(data, "根", {"version", "classes", "codes", "correction"})
    if data.get("version") != 1:
        raise ErrorTableError("错误码表 version 必须为 1")

    raw_classes = data.get("classes")
    if not isinstance(raw_classes, list) or not raw_classes:
        raise ErrorTableError("错误码表 classes 必须是非空列表")
    classes = tuple(str(item) for item in raw_classes)
    if len(set(classes)) != len(classes):
        raise ErrorTableError("错误码表 classes 有重复项")
    for required in (FAILURE, EMPTY, CANCELLED):
        if required not in classes:
            raise ErrorTableError(f"错误码表 classes 必须包含 {required}")

    raw_codes = data.get("codes")
    if not isinstance(raw_codes, dict) or not raw_codes:
        raise ErrorTableError("错误码表 codes 必须是非空映射")

    specs: list[ErrorCodeSpec] = []
    for code, spec in raw_codes.items():
        name = str(code)
        if not isinstance(spec, dict):
            raise ErrorTableError(f"错误码 {name} 的定义不是映射")
        _exact_keys(spec, name, {"label", "class", "retryable", "message"})
        klass = str(spec.get("class") or "")
        if klass not in classes:
            raise ErrorTableError(
                f"错误码 {name} 的 class={klass!r} 不在 classes {list(classes)} 里"
            )
        retryable = spec.get("retryable")
        if not isinstance(retryable, bool):
            raise ErrorTableError(f"错误码 {name} 的 retryable 必须是布尔值")
        label = str(spec.get("label") or "")
        message = str(spec.get("message") or "")
        if not label or not message:
            raise ErrorTableError(f"错误码 {name} 的 label / message 不能为空")
        specs.append(
            ErrorCodeSpec(
                code=name,
                label=label,
                klass=klass,
                retryable=retryable,
                message=message,
            )
        )

    found = {spec.code for spec in specs}
    duplicated = len(found) != len(specs)
    if duplicated:
        raise ErrorTableError("错误码表 codes 里有重复的码")
    missing_roadmap = [code for code in ROADMAP_CODES if code not in found]
    if missing_roadmap:
        raise ErrorTableError(f"错误码表缺少路线图要求的通道码：{missing_roadmap}")

    correction = _parse_correction(data.get("correction"))

    return ErrorTable(
        version=1,
        classes=classes,
        specs=tuple(specs),
        correction=correction,
    )


def _parse_correction(raw: Any) -> CorrectionPolicy:
    """校正闭环策略。`max_attempts` 至少是 1（0 次就等于没有闭环）。"""
    if not isinstance(raw, dict):
        raise ErrorTableError("错误码表 correction 段必须是映射")
    _exact_keys(
        raw,
        "correction",
        {"max_attempts", "prompt", "revalidate", "revalidate_with"},
    )
    max_attempts = raw.get("max_attempts")
    if not isinstance(max_attempts, int) or isinstance(max_attempts, bool):
        raise ErrorTableError("错误码表 correction.max_attempts 必须是整数")
    if max_attempts < 1:
        raise ErrorTableError("错误码表 correction.max_attempts 至少为 1")
    prompt = str(raw.get("prompt") or "")
    if not prompt:
        raise ErrorTableError("错误码表 correction.prompt 不能为空")
    revalidate = raw.get("revalidate")
    if not isinstance(revalidate, bool):
        raise ErrorTableError("错误码表 correction.revalidate 必须是布尔值")
    raw_with = raw.get("revalidate_with")
    if not isinstance(raw_with, list) or not raw_with:
        raise ErrorTableError("错误码表 correction.revalidate_with 必须是非空列表")
    revalidate_with = tuple(str(item) for item in raw_with)
    unknown = sorted(set(revalidate_with) - {"guard", "policy"})
    if unknown:
        raise ErrorTableError(
            f"错误码表 correction.revalidate_with 出现未知的校验关卡：{unknown}"
        )
    if revalidate and not revalidate_with:
        raise ErrorTableError("错误码表 correction.revalidate 为真时必须有关卡")
    return CorrectionPolicy(
        max_attempts=max_attempts,
        prompt=prompt,
        revalidate=revalidate,
        revalidate_with=revalidate_with,
    )


@lru_cache(maxsize=1)
def load_error_table(path: Path = ERRORS_PATH) -> ErrorTable:
    """读 `contracts/errors.yaml` 并校验。"""
    data = yaml.safe_load(path.read_text(encoding="utf-8"))
    return parse_error_table(data)


# ---------------------------------------------------------------------------
# 通道错误
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class ChannelError:
    """一次失败在通道上的表示。

    这是**唯一**的失败形状：S4 的拒答、S5 的策略拒绝与执行失败、
    S6 的校正失败、取消，全部变成它。
    """

    code: str
    message: str
    step: str | None = None
    detail: Mapping[str, Any] = field(default_factory=dict)

    @property
    def klass(self) -> str:
        return load_error_table().get(self.code).klass

    @property
    def retryable(self) -> bool:
        return load_error_table().get(self.code).retryable

    def to_event(self) -> dict[str, Any]:
        """符合 `contracts/sse.schema.json#/$defs/error` 的事件。"""
        detail: dict[str, Any] = {}
        for key in (
            "reason_code",
            "reason",
            "sql",
            "original_sql",
            "corrected_sql",
            "attempts",
            "rows",
            "request_id",
        ):
            if key in self.detail and self.detail[key] is not None:
                detail[key] = self.detail[key]
        event: dict[str, Any] = {
            "type": "error",
            "code": self.code,
            "class": self.klass,
            "message": self.message,
            "step": self.step,
        }
        if detail:
            event["detail"] = detail
        return event


def build_error(
    code: str,
    message: str = "",
    *,
    step: str | None = None,
    **detail: Any,
) -> ChannelError:
    """构造一个通道错误。码必须在表里，否则直接抛异常。"""
    table = load_error_table()
    spec = table.get(code)  # 不在表里就抛 ErrorTableError
    return ChannelError(
        code=code,
        message=message or spec.message,
        step=step,
        detail={key: value for key, value in detail.items() if value is not None},
    )


def channel_code_for_refusal(refusal_code: str) -> str:
    """拒答码 → 通道码。

    规则只有一条：**在错误码表里就是它自己，不在就是 `LLM_REFUSAL`**。

    于是 S4 静态校验的 9 个 `SQL_*` 违规码各自保留自己的名字，而生成器
    主动拒答的 `OUT_OF_RANGE` / `UNSUPPORTED_INTENT` / … 统一归到
    `LLM_REFUSAL`——它们是模型的判断，不是系统的失败类别。
    """
    if refusal_code in load_error_table():
        return refusal_code
    return LLM_REFUSAL
