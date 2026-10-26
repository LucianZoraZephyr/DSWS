"""S5 · 注入语料夹具的读取。

`fixtures/injection/sql-injection.yaml` 是 S5 的负向夹具：每一条都是
「旧系统实测能跑通、新系统必须拒绝」的载荷，外加一段「长得像正常查询、
必须放行」的反向语料。

这里的读取器做两件事：

    1. **严格校验**：缺字段、重复 id、原因码不在契约里 → 直接抛异常。
       夹具本身坏了却静默少测几条，是最危险的失败方式。
    2. **只读**：夹具是资产，不是运行时配置。
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any

import yaml

from app.security.config import load_policy_config

REBUILD_ROOT = Path(__file__).resolve().parents[3]
CORPUS_PATH = REBUILD_ROOT / "fixtures" / "injection" / "sql-injection.yaml"


class CorpusError(Exception):
    """注入语料夹具本身有问题。"""


@dataclass(frozen=True)
class CorpusCase:
    """一条语料。"""

    id: str
    sql: str
    reason_code: str | None = None
    legacy: str = ""
    evidence: str = ""
    note: str = ""

    @property
    def expect_reject(self) -> bool:
        return self.reason_code is not None


@dataclass(frozen=True)
class Corpus:
    """语料全集。"""

    reject: tuple[CorpusCase, ...]
    allow: tuple[CorpusCase, ...]

    def __len__(self) -> int:
        return len(self.reject) + len(self.allow)


def _case(raw: dict[str, Any], *, index: int) -> CorpusCase:
    if not isinstance(raw, dict):
        raise CorpusError(f"语料第 {index} 条不是映射")
    case_id = str(raw.get("id") or "")
    if not case_id:
        raise CorpusError(f"语料第 {index} 条缺少 id")
    sql = str(raw.get("sql") or "")
    if not sql.strip():
        raise CorpusError(f"语料 {case_id} 缺少 sql")
    return CorpusCase(
        id=case_id,
        sql=sql,
        reason_code=(str(raw["reason_code"]) if raw.get("reason_code") else None),
        legacy=str(raw.get("legacy") or ""),
        evidence=str(raw.get("evidence") or ""),
        note=str(raw.get("note") or ""),
    )


def load_corpus(path: Path = CORPUS_PATH) -> Corpus:
    """读注入语料并校验。"""
    if not path.exists():
        raise CorpusError(f"注入语料夹具不存在：{path}")
    data = yaml.safe_load(path.read_text(encoding="utf-8"))
    if not isinstance(data, dict):
        raise CorpusError(f"{path} 不是 YAML 映射")
    if data.get("version") != 1:
        raise CorpusError(f"{path} 的 version 必须为 1")

    reject_raw = data.get("reject")
    allow_raw = data.get("allow")
    if not isinstance(reject_raw, list) or not reject_raw:
        raise CorpusError(f"{path} 缺少 reject 段")
    if not isinstance(allow_raw, list) or not allow_raw:
        raise CorpusError(f"{path} 缺少 allow 段")

    reject = tuple(_case(item, index=index) for index, item in enumerate(reject_raw))
    allow = tuple(_case(item, index=index) for index, item in enumerate(allow_raw))

    ids = [case.id for case in (*reject, *allow)]
    duplicates = sorted({name for name in ids if ids.count(name) > 1})
    if duplicates:
        raise CorpusError(f"语料 id 重复：{duplicates}")

    # 原因码必须在策略契约里，否则夹具在测一个不存在的码。
    known = set(load_policy_config().reason_codes)
    unknown = sorted(
        {case.reason_code for case in reject if case.reason_code} - known
    )
    if unknown:
        raise CorpusError(
            f"语料里的原因码不在策略契约里：{unknown}；"
            f"契约声明的是 {sorted(known)}"
        )

    return Corpus(reject=reject, allow=allow)
