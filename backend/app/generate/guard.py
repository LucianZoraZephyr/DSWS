"""S4 · SQL 静态校验（sqlglot AST）。

这一层是路线图里 S4 的「语义断言」，也是审计里那句老话的回音：

> 「只能查询」只写在 `generate_sql.prompt`；`validate_sql` 的 `EXPLAIN` 对
> UPDATE/DELETE 一律放行；多语句第二句照常执行（实测）。

旧系统把「只读」寄托在一句提示词上，于是 UPDATE 能过 EXPLAIN、多语句能过
校验。这里把同样的规则搬到**代码里**，用真正的 AST 判定：

    1. 单语句      —— 解析后必须恰好一条
    2. 只读        —— 根节点必须是 SELECT；任何 DML/DDL/INTO 都拒绝
    3. 列存在      —— 引用的表与字段必须都在契约上下文里
    4. 无常量投影  —— SELECT 列表里不得出现字面量常量或裸 NULL
    5. 排名有 ORDER BY —— 有排名意图却没有 ORDER BY（或缺 LIMIT）就拒绝

第 5 条依赖问题里的**排名意图**（`app.generate.intent`）。生成器和校验器
共用同一份判断，所以不会出现「生成器觉得该排、校验器觉得不用排」这种
规则跑偏。

`S5` 会在这套之上再加 AST 级安全策略（白名单、超时、注入语料）。S4 只做
语义断言，不越界。
"""

from __future__ import annotations

import re
from dataclasses import dataclass

import sqlglot
from sqlglot import exp
from sqlglot.errors import ParseError

from app.generate.intent import has_ranking_intent, ranking_limit, top_n
from app.generate.models import GenerateContext, GuardReport, GuardViolation

# 词法层的写操作标记。
#
# AST 判定是第一道防线，但 AST 有盲区：`SELECT ... INTO OUTFILE '/tmp/x'`
# 在 MySQL 方言里根本解析不了，sqlglot 会报语法错误——于是它只是「解析
# 失败」，而不是「不是只读」。这两种拒绝在结果上一样（都不会被执行），
# 但**错误码不一样**，而错误码是契约的一部分。所以再加一道词法防线：
# 剥掉注释、字符串与反引号标识符之后，逐个词匹配写操作关键字。
#
# 用词边界（\b）而不是子串匹配，避免 `created_at`、`last_update` 这类
# 合法列名被误判。
_WRITE_KEYWORDS = (
    "INSERT",
    "UPDATE",
    "DELETE",
    "DROP",
    "ALTER",
    "CREATE",
    "TRUNCATE",
    "MERGE",
    "GRANT",
    "REVOKE",
    "CALL",
    "EXECUTE",
    "HANDLER",
    "LOAD_FILE",
    "INTO",
)

_COMMENT_RE = re.compile(r"--[^\n]*|/\*.*?\*/", re.DOTALL)
_LITERAL_RE = re.compile(r"'(?:''|[^'])*'|\"(?:\"\"|[^\"])*\"|`(?:``|[^`])*`", re.DOTALL)
_WRITE_RE = re.compile(
    r"\b(" + "|".join(_WRITE_KEYWORDS) + r")\b",
    re.IGNORECASE,
)

# 任何一种出现即判定「不是只读」。
_WRITE_NODES: tuple[type[exp.Expression], ...] = (
    exp.Insert,
    exp.Update,
    exp.Delete,
    exp.Drop,
    exp.Create,
    exp.Alter,
    exp.Merge,
    exp.TruncateTable,
    exp.Command,
    exp.Into,
)

_READ_ONLY_ROOTS: tuple[type[exp.Expression], ...] = (exp.Select, exp.Union, exp.Subquery)


@dataclass(frozen=True)
class _Scope:
    """一张表在查询里的可见身份：真实表名 + 可选别名。"""

    table_name: str
    alias: str | None


def _collect_scopes(tree: exp.Expression) -> list[_Scope]:
    scopes: list[_Scope] = []
    for table in tree.find_all(exp.Table):
        name = table.name
        alias = table.alias or None
        scopes.append(_Scope(table_name=name, alias=alias))
    return scopes


def _alias_map(scopes: list[_Scope]) -> dict[str, str]:
    return {
        scope.alias: scope.table_name
        for scope in scopes
        if scope.alias and scope.alias != scope.table_name
    }


def _projection_aliases(tree: exp.Expression) -> set[str]:
    """所有投影别名（含子查询层级）。ORDER BY 引用别名时会解析成 Column，
    这些名字必须排除，否则合法的 `ORDER BY \\`销量\\`` 会被误判成未知列。"""
    names: set[str] = set()
    for alias in tree.find_all(exp.Alias):
        if alias.alias:
            names.add(alias.alias)
    return names


def _contains_literal(node: exp.Expression) -> bool:
    """表达式里是否含字面量常量 / NULL。

    `CAST(x AS DECIMAL(10, 2))` 里的 `10, 2` 属于类型声明，不是投影常量；
    `x * 1.5` 里的 `1.5` 是。所以类型部分要跳过，值部分不能跳。
    """
    if isinstance(node, exp.DataType):
        return False
    if isinstance(node, exp.Cast):
        return _contains_literal(node.this)
    if isinstance(node, (exp.Literal, exp.Null)):
        return True
    return any(_contains_literal(child) for child in node.iter_expressions())


def _is_constant_projection(projection: exp.Expression) -> bool:
    inner = projection.this if isinstance(projection, exp.Alias) else projection
    if isinstance(inner, (exp.Literal, exp.Null)):
        return True
    # 聚合表达式允许（SUM(x)、COUNT(DISTINCT x)、CAST(SUM(x) AS DECIMAL(10,2))）
    if inner.find(exp.AggFunc) is not None:
        return False
    return _contains_literal(inner)


def _select_nodes(tree: exp.Expression) -> list[exp.Select]:
    if isinstance(tree, exp.Select):
        return [tree]
    return list(tree.find_all(exp.Select))


def check_sql(sql: str, context: GenerateContext, query: str) -> GuardReport:
    """对一条 SQL 做全部语义断言。

    返回的 `GuardReport.ok` 为 True 表示这条 SQL 满足全部硬规则。
    违规**不抛异常**——调用方需要拿到违规列表（降级成拒答、或在测试里
    逐条断言）。
    """
    violations: list[GuardViolation] = []

    text = str(sql or "").strip()
    if not text:
        return GuardReport(False, (GuardViolation("SQL_EMPTY", "SQL 为空"),))

    dialect = str((context.db_info or {}).get("dialect") or "mysql").lower()

    # --- 0. 词法层的写操作标记（AST 解析不了的情况由它兜底）--------------
    markers = lexical_write_markers(text)
    if markers:
        violations.append(
            GuardViolation(
                "SQL_NOT_READ_ONLY",
                f"出现写操作关键字：{', '.join(markers)}",
            )
        )

    # --- 1. 单语句 -------------------------------------------------------
    try:
        statements = [s for s in sqlglot.parse(text, read=dialect) if s is not None]
    except ParseError as exc:
        if violations:
            return GuardReport(False, tuple(violations))
        return GuardReport(
            False,
            (
                GuardViolation(
                    "SQL_SYNTAX",
                    "SQL 无法被解析",
                    str(exc).splitlines()[0] if str(exc) else "",
                ),
            ),
        )

    if not statements:
        return GuardReport(False, (GuardViolation("SQL_EMPTY", "SQL 为空"),))
    if len(statements) > 1:
        violations.append(
            GuardViolation(
                "SQL_MULTIPLE_STATEMENTS",
                f"SQL 必须只有一条语句，实际解析出 {len(statements)} 条",
            )
        )

    tree = statements[0]

    # --- 2. 只读 ---------------------------------------------------------
    if not isinstance(tree, _READ_ONLY_ROOTS):
        if not any(item.code == "SQL_NOT_READ_ONLY" for item in violations):
            violations.append(
                GuardViolation(
                    "SQL_NOT_READ_ONLY",
                    f"根节点是 {type(tree).__name__}，只允许 SELECT",
                )
            )
    for node in tree.walk():
        if isinstance(node, _WRITE_NODES):
            if not any(item.code == "SQL_NOT_READ_ONLY" for item in violations):
                violations.append(
                    GuardViolation(
                        "SQL_NOT_READ_ONLY",
                        f"出现写操作节点 {type(node).__name__}",
                    )
                )
            break

    # --- 3. 表与列存在 ---------------------------------------------------
    scopes = _collect_scopes(tree)
    aliases = _alias_map(scopes)
    known_tables = context.table_names()

    for scope in scopes:
        if scope.table_name not in known_tables:
            violations.append(
                GuardViolation(
                    "SQL_UNKNOWN_TABLE",
                    f"表 {scope.table_name} 不在契约上下文里",
                )
            )

    projection_aliases = _projection_aliases(tree)

    for column in tree.find_all(exp.Column):
        name = column.name
        qualifier = column.table or ""
        if qualifier:
            resolved = aliases.get(qualifier, qualifier)
            if resolved not in known_tables:
                violations.append(
                    GuardViolation(
                        "SQL_UNKNOWN_TABLE",
                        f"限定符 {qualifier} 不在契约上下文里",
                    )
                )
                continue
            if name not in context.column_names(resolved):
                violations.append(
                    GuardViolation(
                        "SQL_UNKNOWN_COLUMN",
                        f"列 {resolved}.{name} 不在契约上下文里",
                    )
                )
            continue
        # 无限定符：命中投影别名则放过（ORDER BY / GROUP BY 引用别名）
        if name in projection_aliases:
            continue
        if any(name in context.column_names(table) for table in known_tables):
            continue
        violations.append(
            GuardViolation(
                "SQL_UNKNOWN_COLUMN",
                f"列 {name} 不在任何已知表里",
            )
        )

    # --- 4. 无常量 / NULL 投影 -------------------------------------------
    for select in _select_nodes(tree):
        for projection in select.selects:
            if _is_constant_projection(projection):
                violations.append(
                    GuardViolation(
                        "SQL_CONSTANT_PROJECTION",
                        "SELECT 列表里出现常量或 NULL 投影",
                        projection.sql(dialect=dialect),
                    )
                )

    # --- 5. 排名必须有 ORDER BY ------------------------------------------
    if has_ranking_intent(query):
        top_level = tree if isinstance(tree, exp.Select) else None
        order = top_level.args.get("order") if top_level is not None else None
        limit = top_level.args.get("limit") if top_level is not None else None
        if order is None or not order.expressions:
            violations.append(
                GuardViolation(
                    "SQL_MISSING_ORDER_BY",
                    "问题含排名意图，SQL 必须带 ORDER BY",
                )
            )
        if limit is None and _expects_limit(query):
            violations.append(
                GuardViolation(
                    "SQL_MISSING_LIMIT",
                    f"问题限定了名次（应为 {ranking_limit(query)}），SQL 必须带 LIMIT",
                )
            )

    if violations:
        return GuardReport(False, tuple(violations))
    return GuardReport(True, ())


def _expects_limit(query: str) -> bool:
    """是否必须带 LIMIT：显式「前 N」或者「最高/最低」这类单值问法。"""
    if top_n(query) is not None:
        return True
    normalized = str(query).replace(" ", "")
    return any(term in normalized for term in ("最高", "最低", "最大", "最小", "最好"))


def lexical_write_markers(sql: str) -> list[str]:
    """词法层扫出来的写操作关键字（已剥离注释、字符串与反引号标识符）。"""
    text = _COMMENT_RE.sub(" ", str(sql))
    text = _LITERAL_RE.sub(" ", text)
    found: list[str] = []
    for match in _WRITE_RE.finditer(text):
        keyword = match.group(1).upper()
        if keyword not in found:
            found.append(keyword)
    return found
