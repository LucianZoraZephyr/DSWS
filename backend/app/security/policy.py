"""S5 · SQL 安全策略（AST 级）。

这一层是路线图 S5 的核心：**SQL 在执行前必须过这道策略**。

它对应审计里最刺眼的一条（`audit-sql-validation.md` D6，高·安全）：

    > 旧系统 `validate()` 做的是 `f"explain {sql}"` —— 字符串拼接，
    > **不检查语句条数**。只给第一句加 EXPLAIN 前缀，后面的语句按原样执行。
    > 实测 `SELECT 1 AS a FROM fact_order LIMIT 1; DROP TABLE _probe_esc;`
    > 校验无异常抛出，DROP 真执行了（MySQL 对 DDL 自动提交）。
    > 而 `DELETE` / `UPDATE` / `INSERT` 全部**通过校验**
    > （MySQL 8.0.18+ 支持 EXPLAIN DELETE/UPDATE/INSERT）。

新系统的判定顺序是刻意的——**先问 AST，再问词法**：

    1. 可执行注释                     —— 服务端会执行、解析器会忽略的那一类
    2. 解析 + 单语句                   —— D6 现象 A
    3. 只读 / 集合运算 / 危险函数      —— D6 现象 B、外带数据、DoS
    4. 表白名单 / 列白名单              —— 白名单来自契约，不是提示词
    5. 词法层兜底                      —— AST 看不见的那些（`INTO OUTFILE`）
    6. 自动 LIMIT + 去注释              —— **执行的就是校验过的那条 SQL**

词法层放在 AST 之后是刻意的：AST 能给出更准确的原因码。
`SELECT 1; DROP TABLE t` 应该说「不是单条语句」，而不是「出现了 DROP」。

最后一条是这一层最重要的性质：策略最后把 AST 重新序列化（注释剥掉、
LIMIT 补上或截断），执行器跑的是这个**产物**，不是原始文本。
否则「校验的那条」和「跑的那条」就会是两条语句——D6 的漏洞换个地方复发。
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from functools import lru_cache
from typing import Any, Mapping

import sqlglot
from sqlglot import exp
from sqlglot.errors import ParseError

from app.recall.catalog import Catalog, default_catalog
from app.security.config import PolicyConfig, load_policy_config
from app.sqllex import has_executable_comment, scan_keywords

DIALECT = "mysql"

# sqlglot 遇到「不认识的语句」（GRANT、FLUSH 之类）会打一条 WARNING：
# 「contains unsupported syntax. Falling back to parsing as a 'Command'.」
# 策略层本来就要拒绝这类语句，这条警告对使用者没有价值，却会污染 CLI 输出。
logging.getLogger("sqlglot").setLevel(logging.ERROR)


# ---------------------------------------------------------------------------
# 数据结构
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class PolicyViolation:
    """策略抓到的一条违规。`code` 是原因码（reason_code）。"""

    code: str
    message: str
    detail: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {"code": self.code, "message": self.message, "detail": self.detail}


@dataclass(frozen=True)
class PolicyDecision:
    """策略结论。

    `ok` 为 True 时，`sql` 是**可以执行**的那条 SQL（已去注释、已加 LIMIT）；
    `original` 是原封不动的输入。两者不一致是正常的——策略会改写它。
    """

    ok: bool
    original: str
    sql: str
    violations: tuple[PolicyViolation, ...] = ()
    applied_limit: int | None = None

    @property
    def reason_code(self) -> str | None:
        """第一条违规的原因码；通过时为 None。"""
        return self.violations[0].code if self.violations else None

    def to_dict(self) -> dict[str, Any]:
        return {
            "ok": self.ok,
            "original": self.original,
            "sql": self.sql,
            "applied_limit": self.applied_limit,
            "violations": [item.to_dict() for item in self.violations],
        }


@dataclass(frozen=True)
class SqlWhitelist:
    """执行期白名单：哪些库、哪些表、哪些列可以出现在 SQL 里。

    它由 `contracts/schema.contract.yaml` 派生（约束 3：schema 单一事实源，
    禁止手写第二份）。S4 的静态校验用的是「召回上下文」这一子集，
    S5 用的是**整个契约**——生成器就算引用了契约里存在但没被召回的表，
    在 S5 这一层也是被允许的（它是合法表），而契约里根本不存在的表会被拦下。
    """

    database: str
    tables: frozenset[str]
    columns: Mapping[str, frozenset[str]]

    def column_names(self, table: str) -> frozenset[str]:
        return self.columns.get(table, frozenset())

    @classmethod
    def from_catalog(
        cls,
        catalog: Catalog,
        *,
        database: str,
    ) -> SqlWhitelist:
        return cls(
            database=database,
            tables=frozenset(catalog.tables),
            columns={
                name: frozenset(
                    catalog.columns[column_id].name
                    for column_id in table.column_ids
                )
                for name, table in catalog.tables.items()
            },
        )


@lru_cache(maxsize=1)
def default_whitelist() -> SqlWhitelist:
    """按契约推导的白名单。

    库名也来自策略契约——`dw` 这个名字不能在代码里再写一遍。
    """
    return SqlWhitelist.from_catalog(
        default_catalog(),
        database=load_policy_config().account_database,
    )


# ---------------------------------------------------------------------------
# AST 节点分类
# ---------------------------------------------------------------------------


def _nodes(*names: str) -> tuple[type[exp.Expression], ...]:
    """按类名取节点类型；类不存在就跳过（不同 sqlglot 版本可能有出入）。"""
    found: list[type[exp.Expression]] = []
    for name in names:
        node = getattr(exp, name, None)
        if isinstance(node, type) and issubclass(node, exp.Expression):
            found.append(node)
    return tuple(found)


# 会改数据或改结构的节点。
_WRITE_NODES = _nodes(
    "Insert",
    "Update",
    "Delete",
    "Drop",
    "Create",
    "Alter",
    "Merge",
    "TruncateTable",
    "Command",
    "Into",
    "Set",
    "Use",
    "Grant",
)

# 集合运算：外带数据的载体。
_SET_OPERATIONS = _nodes("Union", "Except", "Intersect")


@dataclass(frozen=True)
class _Scope:
    """一张表在查询里的可见身份：真实表名 + 库名 + 可选别名。"""

    table: str
    db: str
    alias: str | None


def _collect_scopes(tree: exp.Expression) -> list[_Scope]:
    scopes: list[_Scope] = []
    for table in tree.find_all(exp.Table):
        scopes.append(
            _Scope(
                table=table.name,
                db=table.db or "",
                alias=table.alias or None,
            )
        )
    return scopes


def _alias_map(scopes: list[_Scope]) -> dict[str, str]:
    return {
        scope.alias: scope.table
        for scope in scopes
        if scope.alias and scope.alias != scope.table
    }


def _projection_aliases(tree: exp.Expression) -> set[str]:
    """所有投影别名。

    `ORDER BY \\`销售额\\`` 会被解析成一个列名叫「销售额」的 Column——
    它不是物理列，必须放行，否则合法的聚合查询会被误判成「引用了白名单外的列」。
    """
    names: set[str] = set()
    for alias in tree.find_all(exp.Alias):
        if alias.alias:
            names.add(alias.alias)
    return names


def _function_name(node: exp.Expression) -> str:
    """函数名。sqlglot 的内置函数类是独立类（`exp.MD5`），未知函数是 Anonymous。"""
    if isinstance(node, exp.Anonymous):
        return str(node.name or "").upper()
    return type(node).__name__.upper()


def _strip_comments(tree: exp.Expression) -> exp.Expression:
    """剥掉 AST 里的所有注释。

    普通注释服务端也会剥掉，所以剥掉再序列化是忠实的；可执行注释
    `/*! ... */` 已经在更早一步被整体拒绝（它不是普通注释）。
    """
    for node in tree.walk():
        node.pop_comments()
    return tree


def _existing_limit(tree: exp.Expression) -> int | None:
    """根查询已有的 LIMIT；不是整数字面量时返回 None（视作不可信）。"""
    limit = tree.args.get("limit")
    if limit is None:
        return None
    expression = limit.expression if hasattr(limit, "expression") else None
    if isinstance(expression, exp.Literal) and expression.is_number:
        try:
            return int(expression.this)
        except (TypeError, ValueError):
            return None
    return None


# ---------------------------------------------------------------------------
# 策略
# ---------------------------------------------------------------------------


def _reject(original: str, violations: list[PolicyViolation]) -> PolicyDecision:
    return PolicyDecision(
        ok=False,
        original=original,
        sql="",
        violations=tuple(violations),
    )


def _lexical_violations(text: str, cfg: PolicyConfig) -> list[PolicyViolation]:
    """词法层扫出来的违规（AST 看不见的那些构造）。"""
    found: list[PolicyViolation] = []
    write_hits = scan_keywords(text, cfg.forbid_write_keywords)
    if write_hits:
        found.append(
            PolicyViolation(
                "SQL_NOT_READ_ONLY",
                f"出现写操作关键字：{', '.join(write_hits)}",
                _excerpt(text),
            )
        )
    danger_hits = scan_keywords(text, cfg.forbid_dangerous_keywords)
    if danger_hits:
        found.append(
            PolicyViolation(
                "SQL_FORBIDDEN_CONSTRUCT",
                f"出现禁用函数/构造：{', '.join(danger_hits)}",
                _excerpt(text),
            )
        )
    return found


def enforce(
    sql: str,
    *,
    whitelist: SqlWhitelist | None = None,
    config: PolicyConfig | None = None,
    dialect: str = DIALECT,
) -> PolicyDecision:
    """判定一条 SQL 能不能执行。

    返回的 `PolicyDecision.ok` 为 True 时，`decision.sql` 是**实际要执行**的
    那条 SQL（已去注释、已加/截断 LIMIT）。违规**不抛异常**——调用方要拿
    到违规列表来降级成拒答，或者在测试里逐条断言。
    """
    cfg = config or load_policy_config()
    wl = whitelist if whitelist is not None else default_whitelist()
    original = str(sql or "")
    text = original.strip()

    if not text:
        return _reject(
            original,
            [PolicyViolation("SQL_EMPTY", "SQL 为空")],
        )

    violations: list[PolicyViolation] = []

    # --- 1. 可执行注释：解析器看不见，服务端会执行 -------------------------
    if cfg.forbid_executable_comments and has_executable_comment(text):
        violations.append(
            PolicyViolation(
                "SQL_COMMENT_OBFUSCATION",
                "含 MySQL 可执行注释 /*! ... */：服务端会执行注释里的内容",
                _excerpt(text),
            )
        )

    # --- 2. 解析 ------------------------------------------------------------
    try:
        statements = sqlglot.parse(text, read=dialect)
    except ParseError as exc:
        # 解析失败时 AST 层什么都给不了，词法层是唯一的线索：
        # `SELECT a INTO OUTFILE '/tmp/x'` 解析不了，但它显然不是只读查询，
        # 所以先看词法禁词，再退到「语法错」。
        if not violations:
            violations.extend(_lexical_violations(text, cfg))
        if not violations:
            violations.append(
                PolicyViolation(
                    "SQL_SYNTAX",
                    "SQL 无法被解析",
                    str(exc).splitlines()[0] if str(exc) else "",
                )
            )
        return _reject(original, violations)

    # --- 3. 单语句 ----------------------------------------------------------
    meaningful = [
        statement
        for statement in statements
        if statement is not None and not isinstance(statement, exp.Semicolon)
    ]
    if not meaningful:
        violations.append(PolicyViolation("SQL_EMPTY", "SQL 为空"))
        return _reject(original, violations)

    if cfg.single_statement and len(meaningful) > 1:
        violations.append(
            PolicyViolation(
                "SQL_MULTIPLE_STATEMENTS",
                f"SQL 必须只有一条语句，实际解析出 {len(meaningful)} 条",
                _excerpt(text),
            )
        )
        return _reject(original, violations)

    tree = meaningful[0]

    # --- 4. 集合运算：它比「根节点不是 SELECT」更具体，先判 ----------------
    # UNION 本身是只读的（根节点是 exp.Union），但它是外带数据的载体，
    # 所以原因码必须是「构造不允许」而不是「不是只读查询」。
    if cfg.forbid_set_operations:
        for node in tree.walk():
            if isinstance(node, _SET_OPERATIONS):
                violations.append(
                    PolicyViolation(
                        "SQL_FORBIDDEN_CONSTRUCT",
                        f"出现集合运算 {type(node).__name__}",
                        _excerpt(text),
                    )
                )
                return _reject(original, violations)

    # --- 5. 根节点必须是只读查询 -------------------------------------------
    if type(tree).__name__ not in cfg.allowed_roots:
        violations.append(
            PolicyViolation(
                "SQL_NOT_READ_ONLY",
                f"根节点是 {type(tree).__name__}，"
                f"只允许 {' / '.join(cfg.allowed_roots)}",
                _excerpt(text),
            )
        )

    # --- 6. 任何位置都不得有写节点 -----------------------------------------
    for node in tree.walk():
        if isinstance(node, _WRITE_NODES):
            violations.append(
                PolicyViolation(
                    "SQL_NOT_READ_ONLY",
                    f"出现写操作节点 {type(node).__name__}",
                    _excerpt(text),
                )
            )
            break

    # --- 7. 危险函数 --------------------------------------------------------
    dangerous = set(cfg.forbid_dangerous_keywords)
    for node in tree.find_all(exp.Func):
        name = _function_name(node)
        if name in dangerous:
            violations.append(
                PolicyViolation(
                    "SQL_FORBIDDEN_CONSTRUCT",
                    f"出现禁用函数 {name}",
                    _excerpt(text),
                )
            )

    # --- 8. 词法层兜底：只在 AST 层没给出结论时才说话 ----------------------
    # AST 能认出来的写操作（DML / DDL）上面已经判过了；词法层负责的是
    # 「AST 看不见、但服务端会执行」的那一类。放在最后是为了让原因码更准：
    # `SELECT 1; DROP TABLE t` 应该说「不是单条语句」，而不是「出现 DROP」。
    if not any(
        item.code in ("SQL_NOT_READ_ONLY", "SQL_FORBIDDEN_CONSTRUCT")
        for item in violations
    ):
        violations.extend(_lexical_violations(text, cfg))

    # --- 9. 表白名单 --------------------------------------------------------
    scopes = _collect_scopes(tree)
    aliases = _alias_map(scopes)
    for scope in scopes:
        if scope.db and scope.db.lower() != wl.database.lower():
            violations.append(
                PolicyViolation(
                    "SQL_TABLE_NOT_ALLOWED",
                    f"库 {scope.db} 不在白名单里（只允许 {wl.database}）",
                    _excerpt(text),
                )
            )
            continue
        if scope.table not in wl.tables:
            violations.append(
                PolicyViolation(
                    "SQL_TABLE_NOT_ALLOWED",
                    f"表 {scope.table} 不在契约白名单里",
                    _excerpt(text),
                )
            )

    # --- 10. 列白名单 --------------------------------------------------------
    projection_aliases = _projection_aliases(tree)
    for column in tree.find_all(exp.Column):
        name = column.name
        qualifier = column.table or ""
        if column.db and column.db.lower() != wl.database.lower():
            violations.append(
                PolicyViolation(
                    "SQL_TABLE_NOT_ALLOWED",
                    f"库 {column.db} 不在白名单里（只允许 {wl.database}）",
                    _excerpt(text),
                )
            )
            continue
        if qualifier:
            resolved = aliases.get(qualifier, qualifier)
            if resolved not in wl.tables:
                violations.append(
                    PolicyViolation(
                        "SQL_TABLE_NOT_ALLOWED",
                        f"限定符 {qualifier} 不在契约白名单里",
                        _excerpt(text),
                    )
                )
                continue
            if name not in wl.column_names(resolved):
                violations.append(
                    PolicyViolation(
                        "SQL_COLUMN_NOT_ALLOWED",
                        f"列 {resolved}.{name} 不在契约白名单里",
                        _excerpt(text),
                    )
                )
            continue
        # 无限定符：命中投影别名则放过（ORDER BY / GROUP BY 引用别名）
        if name in projection_aliases:
            continue
        if any(name in wl.column_names(table) for table in wl.tables):
            continue
        violations.append(
            PolicyViolation(
                "SQL_COLUMN_NOT_ALLOWED",
                f"列 {name} 不在任何白名单表里",
                _excerpt(text),
            )
        )

    if violations:
        return _reject(original, violations)

    # --- 11. LIMIT：没有就补，超了就截 --------------------------------------
    existing = _existing_limit(tree)
    if existing is None:
        tree = tree.limit(cfg.default_limit)
        applied_limit = cfg.default_limit
    elif existing > cfg.max_limit:
        tree = tree.limit(cfg.max_limit)
        applied_limit = cfg.max_limit
    else:
        applied_limit = existing

    # --- 12. 重新序列化：执行的就是这条 -------------------------------------
    final_sql = _strip_comments(tree).sql(dialect=dialect)

    return PolicyDecision(
        ok=True,
        original=original,
        sql=final_sql,
        violations=(),
        applied_limit=applied_limit,
    )


def _excerpt(text: str, limit: int = 160) -> str:
    """违规现场：截断后的原文。"""
    flat = " ".join(str(text).split())
    if len(flat) <= limit:
        return flat
    return flat[:limit] + "…"
