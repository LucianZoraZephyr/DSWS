"""S3 · 检索层错误类型。"""

from __future__ import annotations


class RecallError(RuntimeError):
    """检索失败：连接、协议或数据不完整。CLI 会把消息打到 stderr。"""
