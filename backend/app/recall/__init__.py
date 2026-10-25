"""S3 · 检索层（无 LLM）。

这一层把用户问题变成「LLM 将看到的上下文」：

    query
      -> 确定性关键词
      -> Qdrant 字段召回 / ES 取值召回 / Qdrant 指标召回（全部批量）
      -> Meta MySQL 补齐
      -> 确定性过滤
      -> 日期 / 数据库环境上下文
      -> 稳定 YAML

对外只暴露 `recall()`；CLI 在 `app.scripts.recall`。
"""

from app.recall.models import RecallState
from app.recall.pipeline import recall

__all__ = ["RecallState", "recall"]
