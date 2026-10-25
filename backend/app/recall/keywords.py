"""S3 · 确定性关键词抽取（无 LLM、无 jieba）。

策略只有三层，任何一层都不依赖外部服务：

1. **契约词典**：表名、列名、列别名、指标 name/label/alias、契约声明的真实
   枚举值。SQL 里叫 `order_amount`，用户说「销售额 / 成交总额 / 订单金额」，
   这些映射必须由契约给出，而不是让模型猜。
2. **日期模式**：`2025 年 1 月`、`2025Q1`、`2025-01` 这类结构用正则识别，
   同时产生 `dim_date.year / month / quarter` 的确定性提示。
3. **完整 query 兜底**：无论词典是否命中，原问题永远保留在关键词列表里。
   这样用户说了一个契约里没有的客户名/商品名时，ES 仍有机会用全文命中。

关键词只用于「检索」，不参与任何生成；输出顺序固定，重复执行结果完全一致。
"""

from __future__ import annotations

import re
import unicodedata
from collections.abc import Iterable
from dataclasses import dataclass, field

from app.recall.catalog import Catalog
from app.recall.models import KeywordHint

# 单字别名只在语义非常明确时参与匹配，避免「日」「天」把任意问题都拉进日期表。
_SAFE_SINGLE_CHAR_ALIASES = {"省", "年", "月", "日", "天"}

_YEAR_RE = re.compile(r"(?<!\d)(20\d{2})(?!\d)")
_MONTH_RE = re.compile(r"(?<!\d)(1[0-2]|0?[1-9])\s*月")
_YEAR_MONTH_RE = re.compile(r"(?<!\d)(20\d{2})[-/.](\d{1,2})(?!\d)")
_QUARTER_RE = re.compile(r"(?<![a-z])q([1-4])(?![0-9])", re.IGNORECASE)
_QUARTER_CN_RE = re.compile(r"第?\s*([1-4一二三四])\s*季度")
_DATE_ID_RE = re.compile(r"(?<!\d)(20\d{6})(?!\d)")

_CN_QUARTER = {"一": 1, "二": 2, "三": 3, "四": 4}

# 关键词列表的稳定分段：值 -> 指标 -> 字段 -> 表 -> 日期。
_KIND_ORDER = {"column": 0, "value": 1, "metric": 2, "table": 3}

# 引用顺序只用于把多个来源的 matched_keywords 合并成稳定列表。
_SOURCE_ORDER = {"name": 0, "label": 1, "alias": 2, "value": 3, "date": 4}


@dataclass
class KeywordResult:
    keywords: list[str]
    hints: list[KeywordHint]
    value_terms: list[str] = field(default_factory=list)
    date_terms: list[str] = field(default_factory=list)


@dataclass(frozen=True)
class _Surface:
    term: str
    kind: str
    target_ids: tuple[str, ...]
    source: str
    base_weight: float
    order: tuple[int, int, int]


def normalize_text(text: str) -> str:
    """NFKC + 小写 + 折叠空白；只用于匹配，不改写用户原问题。"""
    normalized = unicodedata.normalize("NFKC", str(text)).strip().casefold()
    return re.sub(r"\s+", " ", normalized)


def compact_text(text: str) -> str:
    return re.sub(r"\s+", "", normalize_text(text))


def _ascii_token_match(term: str, query: str) -> bool:
    pattern = re.compile(
        r"(?<![a-z0-9_])" + re.escape(term) + r"(?![a-z0-9_])"
    )
    return bool(pattern.search(query))


def _matches(term: str, query_norm: str, query_compact: str) -> bool:
    normalized = normalize_text(term)
    if not normalized:
        return False
    compact = compact_text(term)
    if not compact:
        return False
    if normalized.isascii() and re.search(r"[a-z0-9]", normalized):
        if " " in normalized:
            return compact in query_compact
        return _ascii_token_match(normalized, query_norm)
    return compact in query_compact


def term_matches(term: str, query: str) -> bool:
    """公开的确定性匹配函数；召回节点做词法重排时复用同一套规则。"""
    return _matches(term, normalize_text(query), compact_text(query))


def _surface_weight(base: float, term: str, query_compact: str) -> float:
    weight = base
    length = len(compact_text(term))
    if length >= 3:
        weight += min(0.6, 0.15 * (length - 2))
    if compact_text(term) == query_compact:
        weight += 1.0
    return round(weight, 3)


def _surfaces(catalog: Catalog) -> list[_Surface]:
    surfaces: list[_Surface] = []
    for table in catalog.tables.values():
        surfaces.append(
            _Surface(
                term=table.name,
                kind="table",
                target_ids=(table.name,),
                source="name",
                base_weight=2.2,
                order=(_KIND_ORDER["table"], table.order, 0),
            )
        )

    for column in catalog.columns.values():
        # 列名（英文）优先级略高于中文别名，但两者都只是候选信号。
        surfaces.append(
            _Surface(
                term=column.name,
                kind="column",
                target_ids=(column.id,),
                source="name",
                base_weight=4.3,
                order=(_KIND_ORDER["column"], column.order, 0),
            )
        )
        for alias_order, alias in enumerate(column.aliases):
            surfaces.append(
                _Surface(
                    term=alias,
                    kind="column",
                    target_ids=(column.id,),
                    source="alias",
                    base_weight=3.8,
                    order=(_KIND_ORDER["column"], column.order, alias_order + 1),
                )
            )

    for metric in catalog.metrics.values():
        surfaces.append(
            _Surface(
                term=metric.name,
                kind="metric",
                target_ids=(metric.name,),
                source="name",
                base_weight=5.0,
                order=(_KIND_ORDER["metric"], metric.order, 0),
            )
        )
        surfaces.append(
            _Surface(
                term=metric.label,
                kind="metric",
                target_ids=(metric.name,),
                source="label",
                base_weight=4.6,
                order=(_KIND_ORDER["metric"], metric.order, 1),
            )
        )
        for alias_order, alias in enumerate(metric.aliases):
            surfaces.append(
                _Surface(
                    term=alias,
                    kind="metric",
                    target_ids=(metric.name,),
                    source="alias",
                    base_weight=4.2,
                    order=(_KIND_ORDER["metric"], metric.order, alias_order + 2),
                )
            )

    for value_order, (value, column_id) in enumerate(catalog.value_surfaces()):
        column = catalog.column(column_id)
        surfaces.append(
            _Surface(
                term=value,
                kind="value",
                target_ids=(column_id,),
                source="value",
                base_weight=4.5,
                order=(
                    _KIND_ORDER["value"],
                    column.order if column else 0,
                    value_order,
                ),
            )
        )
    return surfaces


def _dedupe_hints(hints: list[KeywordHint]) -> list[KeywordHint]:
    best: dict[tuple[str, tuple[str, ...], str], KeywordHint] = {}
    for hint in hints:
        key = (hint.kind, tuple(hint.target_ids), hint.term)
        current = best.get(key)
        if current is None or hint.weight > current.weight:
            best[key] = hint
    return sorted(
        best.values(),
        key=lambda item: (
            _KIND_ORDER.get(item.kind, 9),
            tuple(item.target_ids),
            -item.weight,
            item.term,
            _SOURCE_ORDER.get(item.source, 9),
        ),
    )


def _date_terms(catalog: Catalog, query: str) -> tuple[list[str], list[KeywordHint]]:
    """识别年份/月份/季度，并给出 dim_date 列的确定性提示。"""
    query_norm = normalize_text(query)
    terms: list[str] = []
    hints: list[KeywordHint] = []

    def add_date(term: str, column_name: str) -> None:
        column_id = f"dim_date.{column_name}"
        if column_id not in catalog.columns:
            return
        if term not in terms:
            terms.append(term)
        hints.append(
            KeywordHint(
                term=term,
                kind="column",
                target_ids=[column_id],
                weight=5.0,
                source="date",
            )
        )

    years: list[str] = []
    for match in _YEAR_RE.finditer(query_norm):
        years.append(match.group(1))
    for match in _YEAR_MONTH_RE.finditer(query_norm):
        year, month = match.group(1), int(match.group(2))
        if year not in years:
            years.append(year)
        add_date(f"{month}月", "month")
    for year in dict.fromkeys(years):
        add_date(year, "year")

    for match in _MONTH_RE.finditer(query_norm):
        add_date(f"{int(match.group(1))}月", "month")

    quarter_found = False
    for match in _QUARTER_RE.finditer(query_norm):
        add_date(f"Q{int(match.group(1))}", "quarter")
        quarter_found = True
    if not quarter_found:
        for match in _QUARTER_CN_RE.finditer(query_norm):
            raw = match.group(1)
            number = _CN_QUARTER.get(raw, int(raw) if raw.isdigit() else 0)
            if number:
                add_date(f"Q{number}", "quarter")

    if date_id_match := _DATE_ID_RE.search(query_norm):
        add_date(date_id_match.group(1), "date_id")

    return terms, hints


def extract_keywords(query: str, catalog: Catalog) -> KeywordResult:
    """从问题生成稳定关键词 + 词法提示；不调用任何模型或分词器。"""
    original = unicodedata.normalize("NFKC", str(query)).strip()
    if not original:
        raise ValueError("query 不能为空")

    query_norm = normalize_text(original)
    query_compact = compact_text(original)

    hints: list[KeywordHint] = []
    value_terms: list[str] = []
    metric_terms: list[str] = []
    column_terms: list[str] = []
    table_terms: list[str] = []

    for surface in _surfaces(catalog):
        term = surface.term.strip()
        if not term or not _matches(term, query_norm, query_compact):
            continue
        compact = compact_text(term)
        single_char_alias = (
            len(compact) == 1
            and surface.kind == "column"
            and surface.source == "alias"
            and term not in _SAFE_SINGLE_CHAR_ALIASES
        )
        if single_char_alias:
            continue

        weight = _surface_weight(
            surface.base_weight,
            term,
            query_compact,
        )
        hints.append(
            KeywordHint(
                term=term,
                kind=surface.kind,
                target_ids=list(surface.target_ids),
                weight=weight,
                source=surface.source,
            )
        )

        if surface.kind == "value":
            value_terms.append(term)
        elif surface.kind == "metric":
            metric_terms.append(term)
        elif surface.kind == "column" and len(compact) >= 2:
            column_terms.append(term)
        elif surface.kind == "table":
            table_terms.append(term)

    date_terms, date_hints = _date_terms(catalog, original)
    hints.extend(date_hints)

    # 稳定去重：query 第一，之后按 值 -> 指标 -> 字段 -> 表 -> 日期。
    keywords: list[str] = [original]

    def add_terms(terms: Iterable[str]) -> None:
        for term in terms:
            if term and term not in keywords:
                keywords.append(term)

    add_terms(dict.fromkeys(value_terms))
    add_terms(dict.fromkeys(metric_terms))
    add_terms(dict.fromkeys(column_terms))
    add_terms(dict.fromkeys(table_terms))
    add_terms(date_terms)

    return KeywordResult(
        keywords=keywords,
        hints=_dedupe_hints(hints),
        value_terms=list(dict.fromkeys(value_terms)),
        date_terms=date_terms,
    )
