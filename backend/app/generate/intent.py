"""S4 · 确定性意图识别。

这一层回答三个问题，全部只靠正则与契约词面，不调用任何模型：

    1. 这道题有没有排名 / 排序意图？（决定要不要 ORDER BY / LIMIT）
    2. 这道题有没有越界？（年份、月份超出数据集覆盖范围）
    3. 这道题有没有触到契约里不存在的概念？（城市、退货、利润……）

**为什么要有这一层**：审计发现旧系统的 7 个提示词里，`ORDER BY` / `排序` /
`LIMIT` / `Top` 总命中 0，`拒绝` / `无法生成` 总命中 0。也就是说「排名必带
ORDER BY」和「越界必拒答」这两条规则**从来就没存在过**。这里把它们变成
可单测的确定性函数，生成器和静态校验器共用同一份判断——规则只有一处定义。

`GUARD` 与 `GENERATOR` 共用这些函数，是刻意的：生成器按它决定要不要写
ORDER BY，校验器按它决定缺 ORDER BY 算不算违规。规则不会两边跑偏。
"""

from __future__ import annotations

import re
from dataclasses import dataclass

# 排名 / 排序意图。
#
# 刻意保守：只收录**明确表达排序意图**的词。像「最大」「最小」这类词在
# 业务问数里也可能是「求 MAX()」而不是「排名」，但它们同样需要 ORDER BY
# 才能得到有序结果，所以一并收录；真正的歧义由生成器按维度是否存在来兜底。
_RANKING_TERMS = (
    "排名",
    "排行",
    "排序",
    "top",
    "highest",
    "lowest",
    "最高",
    "最低",
    "最大",
    "最小",
    "最多",
    "最少",
    "最贵",
    "最便宜",
    "最热",
    "最好",
    "哪几个",
    "哪些最",
    "哪个最",
    "哪个省",
    "哪个城市",
    "哪个品牌",
    "哪个品类",
    "哪个商品",
    "第一",
)

# 「前 N 名 / Top N」里的 N。
_TOP_N_RE = re.compile(r"(?:前|top)\s*(\d+)", re.IGNORECASE)

# 「每月 / 按月」这类时间序列标记。只有出现这些词，dim_date 才会成为
# 分组维度；否则「2025 年 1 月销量」里的「月」只是过滤条件，不是分组。
_TIME_SERIES_TERMS = {
    "dim_date.month": ("每月", "按月", "月度", "各月", "逐月", "月走势"),
    "dim_date.year": ("每年", "按年", "年度", "各年", "逐年", "年走势"),
    "dim_date.quarter": ("每季", "按季", "季度", "各季", "逐季"),
    "dim_date.day": ("每天", "按天", "每日", "各天", "逐日"),
}

# 契约里根本不存在的概念。用户问到这些时必须拒答，不能编造字段。
#
# 每一条都对应契约里的具体事实，而不是「不支持 XX」这种空泛的拒绝理由。
_UNSUPPORTED_CONCEPTS = {
    "城市": "dim_region 的粒度只到省级行政区，契约里没有 city 列。",
    "city": "dim_region 的粒度只到省级行政区，契约里没有 city 列。",
    "退货": "fact_order 没有退货列，契约的订单语义不含退货流程。",
    "退款": "fact_order 没有退款列；契约注明 order_amount 不含退款/运费。",
    "运费": "fact_order 没有运费列；契约注明 order_amount 不含退款/运费。",
    "利润": "事实表没有成本或利润列，无法计算利润口径。",
    "成本": "事实表没有成本列，无法计算利润口径。",
    "折扣": "契约没有折扣/优惠相关列。",
    "库存": "契约没有库存相关表或列。",
}

_YEAR_RE = re.compile(r"(?<!\d)(20\d{2})(?!\d)")
_MONTH_RE = re.compile(r"(?<!\d)(1[0-2]|0?[1-9])\s*月")
_QUARTER_RE = re.compile(r"(?<![a-z])q([1-4])(?![0-9])", re.IGNORECASE)
_QUARTER_CN_RE = re.compile(r"第?\s*([1-4一二三四])\s*季度")
_DATE_ID_RE = re.compile(r"(?<!\d)(20\d{6})(?!\d)")

_CN_QUARTER = {"一": 1, "二": 2, "三": 3, "四": 4}


@dataclass(frozen=True)
class TimeScope:
    """从问题里解析出的时间范围。"""

    year: int | None = None
    month: int | None = None
    quarter: int | None = None
    date_id: int | None = None
    date_ids: tuple[int, ...] = ()

    @property
    def has_scope(self) -> bool:
        return any(
            item is not None
            for item in (self.year, self.month, self.quarter, self.date_id)
        )


def _normalize(text: str) -> str:
    return re.sub(r"\s+", "", str(text).strip().casefold())


def has_ranking_intent(query: str) -> bool:
    """问题里是否有排名 / 排序意图。"""
    normalized = _normalize(query)
    return any(term in normalized for term in _RANKING_TERMS)


def top_n(query: str) -> int | None:
    """「前 N 名 / Top N」里的 N；没有则返回 None。"""
    match = _TOP_N_RE.search(str(query))
    if not match:
        return None
    value = int(match.group(1))
    return value if value > 0 else None


def ranking_direction(query: str) -> str:
    """排序方向：默认 DESC（大的在前）；出现「最低/最少/最小/最便宜」时 ASC。"""
    normalized = _normalize(query)
    for term in ("最低", "最少", "最小", "最便宜", "asc", "升序"):
        if term in normalized:
            return "ASC"
    return "DESC"


def ranking_limit(query: str) -> int:
    """排名场景的 LIMIT：显式「前 N」用 N，「最高/最低」用 1，其余用 10。"""
    explicit = top_n(query)
    if explicit is not None:
        return explicit
    normalized = _normalize(query)
    if any(term in normalized for term in ("最高", "最低", "最大", "最小", "最好")):
        return 1
    return 10


def parse_time_scope(query: str) -> TimeScope:
    """从问题里解析年份 / 月份 / 季度 / 具体日期。"""
    text = str(query)
    normalized = text.casefold()

    years: list[int] = []
    for match in _YEAR_RE.finditer(normalized):
        years.append(int(match.group(1)))

    months: list[int] = []
    for match in _MONTH_RE.finditer(normalized):
        months.append(int(match.group(1)))

    quarters: list[int] = []
    for match in _QUARTER_RE.finditer(normalized):
        quarters.append(int(match.group(1)))
    if not quarters:
        for match in _QUARTER_CN_RE.finditer(normalized):
            raw = match.group(1)
            number = _CN_QUARTER.get(raw, int(raw) if raw.isdigit() else 0)
            if number:
                quarters.append(number)

    date_ids: list[int] = []
    for match in _DATE_ID_RE.finditer(normalized):
        date_ids.append(int(match.group(1)))

    return TimeScope(
        year=years[0] if years else None,
        month=months[0] if months else None,
        quarter=quarters[0] if quarters else None,
        date_id=date_ids[0] if date_ids else None,
        date_ids=tuple(dict.fromkeys(date_ids)),
    )


def is_time_series_dimension(column_id: str, query: str) -> bool:
    """该时间维度在问题里是否被当作「分组序列」而不是「过滤条件」。"""
    terms = _TIME_SERIES_TERMS.get(column_id)
    if not terms:
        return False
    normalized = _normalize(query)
    return any(term in normalized for term in terms)


def unsupported_concept(query: str) -> tuple[str, str] | None:
    """问题是否触到契约里不存在的概念；返回 (概念, 原因) 或 None。"""
    normalized = _normalize(query)
    for term, reason in _UNSUPPORTED_CONCEPTS.items():
        if term in normalized:
            return term, reason
    return None


def year_in_range(year: int, start: int, end: int) -> bool:
    """年份是否落在 [start, end] 覆盖的年份集合内。"""
    return start // 10000 <= year <= end // 10000


def month_in_range(month: int, start: int, end: int) -> bool:
    """月份是否落在数据集覆盖的月份内（按年内第几月比较）。"""
    start_month = (start % 10000) // 100
    end_month = (end % 10000) // 100
    return start_month <= month <= end_month
