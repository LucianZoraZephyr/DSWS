"""S7 · 单元格值的类型保真。

    MySQL DECIMAL  ->  Python Decimal  ->  JSON 数字
    MySQL DATE     ->  Python date     ->  ISO 8601 字符串

为什么单独一个模块：旧系统在 `json.dumps(..., default=str)` 里把 `Decimal`
降级成字符串，于是同一行里出现 `{"销量": "10", "销售额": 62190.0}`——
**数字与字符串混列**。屏幕上看不出差别（前端一律 `String(value)`），
复制出去才是 `"10"`，下游做数值计算会踩坑。

审计把这条记成 B4（后端改写类型），但真正能根治的地方只有**两个**：

    1. 后端：值在进 JSON 之前就归一化，数字永远是数字。
    2. 前端：列类型由契约声明，界面照着类型渲染，不靠猜。

这里做第 1 件事。归一化必须是**显式**的：这里没有 `default=str` 那种
「顺手降级」的兜底，每一种类型都有它自己的规则。

一个刻意的取舍：`Decimal("10")` 走 `int`（不是 `10.0`），`Decimal("10.5")`
走 `float`。理由是 `SUM(INT)` 在 MySQL 里就是 DECIMAL，用户心里的
「10 件」应该显示成 `10` 而不是 `10.0`。真正有小数的金额才落到 float，
那一步会引入 IEEE-754 的浮点误差——这是 JSON 数字类型的固有限制，
契约里 `column.type` 会如实标成 `number`，前端按两位小数显示。
"""

from __future__ import annotations

import datetime as dt
from decimal import Decimal
from typing import Any

__all__ = ["normalize_cell", "normalize_value"]


def normalize_cell(value: Any) -> Any:
    """把一个数据库返回的单元格值变成「JSON 能编码且不丢类型」的值。

    返回值只可能是：`None` / `bool` / `int` / `float` / `str` / `list` /
    `dict`。**没有 Decimal、没有 date**——它们在进 JSON 之前就被换掉了。
    """
    # 布尔必须排在整数前面：`isinstance(True, int)` 是 True。
    if value is None or isinstance(value, (bool, int, float, str)):
        return value

    if isinstance(value, Decimal):
        # 整数值的 Decimal 走 int：`SUM(INT)` 得到的 DECIMAL 在用户眼里
        # 就是「10 件」，不是「10.0 件」。
        if value == value.to_integral_value():
            return int(value)
        return float(value)

    if isinstance(value, dt.datetime):
        return value.isoformat()
    if isinstance(value, dt.date):
        return value.isoformat()
    if isinstance(value, dt.time):
        return value.isoformat()
    if isinstance(value, dt.timedelta):
        return value.total_seconds()

    if isinstance(value, (bytes, bytearray, memoryview)):
        raw = bytes(value)
        try:
            return raw.decode("utf-8")
        except UnicodeDecodeError:
            # 不是文本的二进制：给十六进制，至少不丢内容。
            return raw.hex()

    if isinstance(value, (list, tuple, set, frozenset)):
        return [normalize_cell(item) for item in value]
    if isinstance(value, dict):
        return {str(key): normalize_cell(item) for key, item in value.items()}

    # 最后一种情况：驱动返回了没见过的类型。
    #
    # 这里**不**静默降级成字符串——降级就是 B4 那个缺陷本身。改成
    # 明确报错：调用方会把它变成 INTERNAL_ERROR，用户看到「系统内部错误」
    # 而不是一份悄悄被改过类型的数据。
    raise TypeError(
        f"无法归一化的单元格类型：{type(value).__name__}。"
        f"请在 app/agent/values.py 里为它补一条规则。"
    )


def normalize_value(value: Any) -> Any:
    """归一化任意结构（错误 detail 之类），语义同 `normalize_cell`。"""
    return normalize_cell(value)
