"""S4 · 静态校验（sqlglot AST）的测试。

守的是审计里那句：

> 「只能查询」只写在 `generate_sql.prompt`；`validate_sql` 的 `EXPLAIN` 对
> UPDATE/DELETE 一律放行；多语句第二句照常执行（实测）。

这里用真正的 AST 把「只读」「单语句」「列存在」变成代码级守卫，并且给出
**每一个违规码都有一条能复现它的负例**。
"""

from __future__ import annotations

import pytest

from app.generate.context import build_offline_context
from app.generate.guard import check_sql
from app.generate.pipeline import generate


def _codes(sql: str, query: str) -> set[str]:
    context = build_offline_context(query)
    report = check_sql(sql, context, query)
    return {violation.code for violation in report.violations}


# ---------------------------------------------------------------------------
# 正例
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "query",
    [
        "华北销售额",
        "平均客单价",
        "各品类销量",
        "品牌销售额 Top 3",
        "黄金会员订单量",
        "哪个省销量最高",
        "各省平均订单金额",
        "2025 年 1 月销量",
    ],
)
def test_generated_sql_passes_every_rule(query: str) -> None:
    result = generate(query)
    assert result.ok, result.refusal
    assert result.guard.ok
    assert result.guard.violations == ()


def test_order_by_on_alias_is_not_treated_as_unknown_column() -> None:
    """`ORDER BY \\`销量\\`` 会被 sqlglot 解析成 Column，不能误判成未知列。"""
    result = generate("各品类销量")
    assert result.ok
    assert "ORDER BY" in (result.sql or "")


# ---------------------------------------------------------------------------
# 负例：每个违规码都要能复现
# ---------------------------------------------------------------------------


def test_multiple_statements_are_rejected() -> None:
    sql = "SELECT fact_order.order_amount AS a FROM fact_order; DROP TABLE fact_order"
    assert "SQL_MULTIPLE_STATEMENTS" in _codes(sql, "销售额")


def test_update_is_rejected() -> None:
    sql = "UPDATE fact_order SET order_amount = 0"
    assert "SQL_NOT_READ_ONLY" in _codes(sql, "销售额")


def test_delete_is_rejected() -> None:
    sql = "DELETE FROM fact_order WHERE fact_order.order_id = 'ORD00000000001'"
    assert "SQL_NOT_READ_ONLY" in _codes(sql, "销售额")


def test_drop_is_rejected() -> None:
    sql = "DROP TABLE fact_order"
    assert "SQL_NOT_READ_ONLY" in _codes(sql, "销售额")


def test_insert_into_outfile_is_rejected() -> None:
    sql = "SELECT fact_order.order_amount INTO OUTFILE '/tmp/x' FROM fact_order"
    assert "SQL_NOT_READ_ONLY" in _codes(sql, "销售额")


def test_unknown_table_is_rejected() -> None:
    sql = "SELECT made_up_table.col FROM made_up_table"
    assert "SQL_UNKNOWN_TABLE" in _codes(sql, "销售额")


def test_unknown_column_is_rejected() -> None:
    sql = "SELECT fact_order.not_a_real_column AS a FROM fact_order"
    assert "SQL_UNKNOWN_COLUMN" in _codes(sql, "销售额")


def test_column_of_a_table_outside_the_context_is_rejected() -> None:
    """上下文里没有 dim_product，就不许查 dim_product.brand。"""
    sql = (
        "SELECT dim_product.brand AS a, SUM(fact_order.order_amount) AS b "
        "FROM fact_order JOIN dim_product ON fact_order.product_id = dim_product.product_id "
        "GROUP BY dim_product.brand"
    )
    assert "SQL_UNKNOWN_TABLE" in _codes(sql, "销售额")


def test_constant_projection_is_rejected() -> None:
    sql = "SELECT 1 AS a FROM fact_order"
    assert "SQL_CONSTANT_PROJECTION" in _codes(sql, "销售额")


def test_null_projection_is_rejected() -> None:
    sql = "SELECT NULL AS a FROM fact_order"
    assert "SQL_CONSTANT_PROJECTION" in _codes(sql, "销售额")


def test_computed_constant_projection_is_rejected() -> None:
    sql = "SELECT fact_order.order_amount * 1.5 AS a FROM fact_order"
    assert "SQL_CONSTANT_PROJECTION" in _codes(sql, "销售额")


def test_cast_with_decimal_precision_is_allowed() -> None:
    """`CAST(x AS DECIMAL(10,2))` 里的 10,2 是类型声明，不是投影常量。"""
    sql = (
        "SELECT CAST(SUM(fact_order.order_amount) AS DECIMAL(10, 2)) AS a "
        "FROM fact_order"
    )
    assert "SQL_CONSTANT_PROJECTION" not in _codes(sql, "销售额")


def test_missing_order_by_is_rejected_for_ranking_questions() -> None:
    sql = (
        "SELECT dim_region.province AS a, SUM(fact_order.order_amount) AS b "
        "FROM fact_order JOIN dim_region ON fact_order.region_id = dim_region.region_id "
        "GROUP BY dim_region.province"
    )
    assert "SQL_MISSING_ORDER_BY" in _codes(sql, "各省 GMV 排名")


def test_missing_limit_is_rejected_for_top_n_questions() -> None:
    sql = (
        "SELECT dim_product.brand AS a, SUM(fact_order.order_amount) AS b "
        "FROM fact_order JOIN dim_product ON fact_order.product_id = dim_product.product_id "
        "GROUP BY dim_product.brand ORDER BY b DESC"
    )
    assert "SQL_MISSING_LIMIT" in _codes(sql, "品牌销售额 Top 3")


def test_syntax_error_is_reported() -> None:
    assert "SQL_SYNTAX" in _codes("SELEC * FROM fact_order", "销售额")


def test_empty_sql_is_reported() -> None:
    assert "SQL_EMPTY" in _codes("   ", "销售额")
