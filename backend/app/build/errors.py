"""S2 · 可读的构建错误。"""

from __future__ import annotations


class BuildError(RuntimeError):
    """构建前置条件不满足，或存储写入失败。"""


class VerifyError(BuildError):
    """`--verify` / `--assert-idempotent` 断言失败。"""
