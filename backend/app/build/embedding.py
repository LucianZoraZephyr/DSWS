"""S2 · 可替换的 embedding 提供方。

为什么 S2 不直接用旧系统的 HuggingFace/TEI：
    旧栈的 `embedding` 容器需要 1.3GB 的 bge 权重；当前机器上权重缺失，
    容器一直 restarting。S2 的目标是验证「构建 + 三存储 + manifest + 幂等」，
    不应该被一个外部模型下载卡住。

这里用确定性的 n-gram 哈希向量作为教学默认实现：
    - 同一段文本在任何进程、任何机器上都得到同一个向量；
    - 不依赖模型、网络、GPU；
    - 接口保留，S3/S4 可以换成真实 bge，只要 dim 与 Qdrant collection 对齐。

它不是语义模型，召回质量有限；这是有意的取舍，README 会写清楚。
"""

from __future__ import annotations

import hashlib
import math
import re
from collections.abc import Sequence
from typing import Protocol

_TOKEN_RE = re.compile(r"[A-Za-z0-9_]+|[\u4e00-\u9fff]")
_CHINESE_RUN_RE = re.compile(r"[\u4e00-\u9fff]+")


class EmbeddingProvider(Protocol):
    """最小接口：批量把文本变成等长向量。"""

    name: str
    dim: int

    def embed_many(self, texts: Sequence[str]) -> list[list[float]]: ...


class HashEmbeddingProvider:
    """确定性的字符/词 n-gram 哈希向量。

    中文按「单字 + 相邻双字」切，英文/数字按 token 切。每个 token 用
    BLAKE2b 稳定哈希到固定维度，符号位决定加减，最后 L2 归一化。
    """

    name = "hash-ngram-v1"

    def __init__(self, dim: int = 256):
        if dim <= 0:
            raise ValueError("embedding dim 必须为正数")
        self.dim = dim

    def _tokens(self, text: str) -> list[str]:
        lowered = text.lower()
        tokens = _TOKEN_RE.findall(lowered)
        for run in _CHINESE_RUN_RE.findall(lowered):
            for index in range(len(run) - 1):
                tokens.append(run[index : index + 2])
        return tokens

    def embed(self, text: str) -> list[float]:
        vector = [0.0] * self.dim
        for token in self._tokens(text):
            digest = hashlib.blake2b(token.encode("utf-8"), digest_size=8).digest()
            number = int.from_bytes(digest, "big")
            index = number % self.dim
            sign = 1.0 if (number >> 8) & 1 else -1.0
            vector[index] += sign

        norm = math.sqrt(sum(value * value for value in vector))
        if norm == 0.0:
            # 空文本/纯标点：给一个固定的单位向量，避免 Qdrant 拒绝零向量。
            vector[0] = 1.0
            return vector
        return [value / norm for value in vector]

    def embed_many(self, texts: Sequence[str]) -> list[list[float]]:
        return [self.embed(text) for text in texts]


def make_embedding_provider(name: str, dim: int) -> EmbeddingProvider:
    """当前只有一个实现；名字不匹配时显式失败，避免静默换模型。"""
    if name != HashEmbeddingProvider.name:
        raise ValueError(
            f"未知 embedding provider: {name!r}；"
            f"当前 S2 只支持 {HashEmbeddingProvider.name!r}"
        )
    return HashEmbeddingProvider(dim=dim)
