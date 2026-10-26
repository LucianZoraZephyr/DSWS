"""S5 · 安全策略的测试。

每条规则一个用例。规则本身写在 `contracts/sql.policy.yaml` 里，
这里测的是**代码真的执行了契约**，而不是代码里另写一套。
"""

from __future__ import annotations

import pytest

from app.security.config import load_policy_config
from app.security.policy import default_whitelist, enforce


def _code(sql: str) -> str | None:
    decision = enforce(sql)
    return decision.reason_code


# ---------------------------------------------------------------------------
# 逐条规则
# ---------------------------------------------------------------------------


def test_empty_sql_is_rejected() -> None:
    assert _code("") == "SQL_EMPTY"
    assert _code("   ") == "SQL_EMPTY"


def test_unparsable_sql_is_rejected() -> None:
    assert _code("SELEC 1 FROM fact_order") == "SQL_SYNTAX"


def test_multiple_statements_are_rejected() -> None:
    """D6 现象 A：旧系统只给第一句加 EXPLAIN，第二句照常执行。"""
    assert _code("SELECT 1 FROM fact_order; DROP TABLE fact_order") == (
        "SQL_MULTIPLE_STATEMENTS"
    )


def test_statement_hidden_after_a_line_comment_is_rejected() -> None:
    """`-- ` 之后换行，分号照样分句。"""
    assert _code("SELECT 1 FROM fact_order -- \n; DROP TABLE fact_order") == (
        "SQL_MULTIPLE_STATEMENTS"
    )


@pytest.mark.parametrize(
    "sql",
    [
        "UPDATE fact_order SET order_amount = 0",
        "DELETE FROM fact_order",
        "INSERT INTO fact_order (order_id) VALUES ('X')",
        "DROP TABLE fact_order",
        "TRUNCATE TABLE fact_order",
        "GRANT ALL PRIVILEGES ON dw.* TO 'x'@'%'",
        "SET GLOBAL max_execution_time = 0",
        "USE mysql",
        "SHOW TABLES",
    ],
)
def test_write_statements_are_rejected(sql: str) -> None:
    assert _code(sql) == "SQL_NOT_READ_ONLY"


def test_into_outfile_is_rejected_as_not_read_only() -> None:
    """这条在 MySQL 方言里根本解析不了——但它显然不是只读查询。"""
    assert _code("SELECT order_amount FROM fact_order INTO OUTFILE '/tmp/x'") == (
        "SQL_NOT_READ_ONLY"
    )


def test_executable_comment_is_rejected() -> None:
    """服务端会执行 `/*! ... */` 里的内容，解析器不会。"""
    assert _code("SELECT 1 /*!32302 UNION SELECT 2 */") == (
        "SQL_COMMENT_OBFUSCATION"
    )


def test_set_operations_are_rejected() -> None:
    assert _code("SELECT order_amount FROM fact_order UNION SELECT 1") == (
        "SQL_FORBIDDEN_CONSTRUCT"
    )


@pytest.mark.parametrize(
    "sql",
    [
        "SELECT SLEEP(10) FROM fact_order",
        "SELECT BENCHMARK(10000000, MD5('a')) FROM fact_order",
        "SELECT LOAD_FILE('/etc/passwd')",
        "SELECT GET_LOCK('dw', 10)",
        "SELECT EXTRACTVALUE(1, CONCAT(0x7e, 1))",
    ],
)
def test_dangerous_functions_are_rejected(sql: str) -> None:
    assert _code(sql) == "SQL_FORBIDDEN_CONSTRUCT"


def test_table_outside_the_contract_is_rejected() -> None:
    assert _code("SELECT a FROM secret_table") == "SQL_TABLE_NOT_ALLOWED"


def test_other_database_is_rejected() -> None:
    assert _code("SELECT table_name FROM information_schema.tables") == (
        "SQL_TABLE_NOT_ALLOWED"
    )


def test_column_outside_the_contract_is_rejected() -> None:
    assert _code("SELECT secret_column FROM fact_order") == (
        "SQL_COLUMN_NOT_ALLOWED"
    )


def test_column_qualified_with_an_unknown_table_is_rejected() -> None:
    assert _code("SELECT nope.a FROM nope") == "SQL_TABLE_NOT_ALLOWED"


# ---------------------------------------------------------------------------
# 白名单来自契约
# ---------------------------------------------------------------------------


def test_whitelist_is_derived_from_the_schema_contract() -> None:
    whitelist = default_whitelist()
    assert "fact_order" in whitelist.tables
    assert "order_amount" in whitelist.column_names("fact_order")
    assert "dim_date" in whitelist.tables
    assert whitelist.database == load_policy_config().account_database


def test_every_contract_table_is_reachable() -> None:
    """契约里的表都该被放行——白名单不能比契约还窄。"""
    whitelist = default_whitelist()
    for table in sorted(whitelist.tables):
        decision = enforce(f"SELECT * FROM {table}")
        assert decision.ok, f"{table} 被误拒：{decision.violations}"


def test_alias_qualified_column_is_resolved() -> None:
    decision = enforce(
        "SELECT f.order_amount FROM fact_order AS f"
    )
    assert decision.ok, decision.violations


def test_projection_alias_in_order_by_is_allowed() -> None:
    """`ORDER BY \\`销售额\\`` 里的「销售额」是别名，不是物理列。"""
    decision = enforce(
        "SELECT SUM(fact_order.order_amount) AS `销售额` FROM fact_order "
        "ORDER BY `销售额` DESC"
    )
    assert decision.ok, decision.violations


def test_subquery_tables_are_checked_too() -> None:
    """子查询里的表也要在白名单里。"""
    assert _code(
        "SELECT 1 FROM fact_order WHERE customer_id IN (SELECT customer_id FROM evil)"
    ) == "SQL_TABLE_NOT_ALLOWED"


# ---------------------------------------------------------------------------
# 改写：自动 LIMIT + 去注释
# ---------------------------------------------------------------------------


def test_missing_limit_is_added() -> None:
    config = load_policy_config()
    decision = enforce("SELECT SUM(fact_order.order_amount) FROM fact_order")
    assert decision.ok
    assert decision.applied_limit == config.default_limit
    assert f"LIMIT {config.default_limit}" in decision.sql


def test_existing_limit_is_kept() -> None:
    decision = enforce("SELECT * FROM fact_order LIMIT 3")
    assert decision.ok
    assert decision.applied_limit == 3
    assert "LIMIT 3" in decision.sql


def test_oversized_limit_is_clamped() -> None:
    config = load_policy_config()
    decision = enforce("SELECT * FROM fact_order LIMIT 999999")
    assert decision.ok
    assert decision.applied_limit == config.max_limit
    assert f"LIMIT {config.max_limit}" in decision.sql


def test_comments_are_stripped_from_the_executed_sql() -> None:
    """执行的 SQL 里不该留着注释——服务端会剥，我们先剥。"""
    decision = enforce("SELECT 1 AS a /* 一段注释 */ FROM fact_order")
    assert decision.ok
    assert "/*" not in decision.sql
    assert "注释" not in decision.sql


def test_the_executed_sql_is_what_was_validated() -> None:
    """策略判定的是哪条，执行的就是哪条。"""
    decision = enforce(
        "SELECT SUM(fact_order.order_amount) FROM fact_order -- 注释\n"
    )
    assert decision.ok
    assert decision.sql == decision.sql.strip()
    assert "--" not in decision.sql


def test_policy_is_deterministic() -> None:
    sql = "SELECT dim_region.region_name, SUM(fact_order.order_amount) FROM fact_order JOIN dim_region ON fact_order.region_id = dim_region.region_id GROUP BY dim_region.region_name"
    first = enforce(sql)
    second = enforce(sql)
    assert first.sql == second.sql
    assert first.applied_limit == second.applied_limit
