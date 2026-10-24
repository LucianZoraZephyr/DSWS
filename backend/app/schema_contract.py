"""
S1 · Schema 契约检查器

这一层解决的不是「SQL 怎么写」，而是「表结构、口径、值域到底以谁为准」。

三方对账：
    1. fixtures/dw.sql                  物理结构与真实数据（唯一物理事实）
    2. contracts/schema.contract.yaml   语义契约（grain / measures / value_domain）
    3. fixtures/legacy/*                旧系统的负向夹具（必须被检出，不能当事实源）

为什么需要契约：
    DDL 只能表达「有哪些列」，表达不了：
      - 一行代表什么（订单？省？天？）
      - 件数 ≠ 订单数；整单金额 ≠ 单价
      - 数据只覆盖 2025 Q1，枚举只有这些值
    旧系统把这些写在 YAML 和提示词里，没有一行代码强制，于是 AOV 同时存在
    2427.47 / 448.81 / 5.41 三种口径。

命令：
    python -m app.schema_contract --check             # 契约绿 + 负向夹具红 + 文档新鲜
    python -m app.schema_contract --check-legacy      # 旧系统夹具：红（退出码 1）
    python -m app.schema_contract --expect-legacy-red # 断言负向夹具按预期变红
    python -m app.schema_contract --write-doc         # 生成 docs/SCHEMA-CONTRACT.md
    python -m app.schema_contract --check-doc         # 文档是否过期

整个检查只依赖 Python 标准库 sqlite3 + PyYAML，不需要 MySQL / Docker / LLM。
"""

from __future__ import annotations

import argparse
import re
import sqlite3
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterable, Sequence

import yaml

REBUILD_ROOT = Path(__file__).resolve().parents[2]
CONTRACT_PATH = REBUILD_ROOT / "contracts" / "schema.contract.yaml"
DDL_PATH = REBUILD_ROOT / "fixtures" / "dw.sql"
LEGACY_META_PATH = REBUILD_ROOT / "fixtures" / "legacy" / "meta_config.yaml"
FEATURE_LIST_PATH = REBUILD_ROOT / "fixtures" / "legacy" / "feature-list.md"
DOC_PATH = REBUILD_ROOT / "docs" / "SCHEMA-CONTRACT.md"

# 旧系统夹具必须命中的代码。少一个就说明检查器退化了。
EXPECTED_LEGACY_CODES = (
    "AOV_THREE_WAY",
    "AOV_COLUMN_MISMATCH",
    "AOV_ALIAS_MISMATCH",
    "CITY_GRAIN_UNDECLARED",
    "ORDER_COUNT_UNDEFINED",
    "ALIAS_COLLISION",
    "FEATURE_LIST_TABLE_DRIFT",
    "FEATURE_LIST_ROW_DRIFT",
)

# 只有真正开放取值的文本列可以写 kind=open；主键、枚举、外键都必须显式约束。
OPEN_VALUE_DOMAIN_ALLOWLIST = {
    ("dim_customer", "customer_name"),
    ("dim_product", "product_name"),
}

# 指标语义也要钉死：只校验“值是 115”不够，订单量不能声明成 additive/件。
EXPECTED_METRIC_SEMANTICS = {
    "GMV": ("additive", "订单"),
    "AOV": ("non_additive", "订单"),
    "订单量": ("non_additive", "订单"),
    "销量": ("additive", "订单"),
    "平均件单价": ("non_additive", "件"),
    "平均每单件数": ("non_additive", "订单"),
}

# 公式也钉死：否则把 GMV 改成 SUM(order_quantity)、expected_value 改成 622 也能自洽。
EXPECTED_METRIC_FORMULAS = {
    "GMV": "SUM(FACT_ORDER.ORDER_AMOUNT)",
    "AOV": "SUM(FACT_ORDER.ORDER_AMOUNT)/COUNT(DISTINCTFACT_ORDER.ORDER_ID)",
    "订单量": "COUNT(DISTINCTFACT_ORDER.ORDER_ID)",
    "销量": "SUM(FACT_ORDER.ORDER_QUANTITY)",
    "平均件单价": "SUM(FACT_ORDER.ORDER_AMOUNT)/SUM(FACT_ORDER.ORDER_QUANTITY)",
    "平均每单件数": "SUM(FACT_ORDER.ORDER_QUANTITY)/COUNT(DISTINCTFACT_ORDER.ORDER_ID)",
}

# 主键/外键的值域类型不能随意降级（例如把 order_id 改成 enum 也能“约束”住）。
EXPECTED_KEY_DOMAIN_KINDS = {
    ("dim_region", "region_id"): "enum",
    ("dim_customer", "customer_id"): "id",
    ("dim_product", "product_id"): "id",
    ("dim_date", "date_id"): "range",
    ("fact_order", "order_id"): "id",
    ("fact_order", "customer_id"): "foreign_key",
    ("fact_order", "product_id"): "foreign_key",
    ("fact_order", "date_id"): "foreign_key",
    ("fact_order", "region_id"): "foreign_key",
}

# ID pattern 直接钉死，避免用 `^ORD\d{11}$|^.*$` 这类“看起来有约束”的写法绕过。
EXPECTED_ID_PATTERNS = {
    ("dim_customer", "customer_id"): r"^C\d{3}$",
    ("dim_product", "product_id"): r"^P\d{3}$",
    ("fact_order", "order_id"): r"^ORD\d{11}$",
}


# ---------------------------------------------------------------------------
# 数据结构
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class Issue:
    """一条可定位的契约问题。"""

    code: str
    where: str
    message: str

    def __str__(self) -> str:
        return f"[{self.code}] {self.where}: {self.message}"


@dataclass(frozen=True)
class ColumnDef:
    name: str
    type: str
    nullable: bool
    primary_key: bool


@dataclass
class TableDef:
    name: str
    columns: list[ColumnDef]
    primary_key: str | None

    @property
    def column_names(self) -> set[str]:
        return {column.name for column in self.columns}

    def column(self, name: str) -> ColumnDef | None:
        return next((column for column in self.columns if column.name == name), None)


@dataclass
class Ddl:
    tables: dict[str, TableDef]
    conn: sqlite3.Connection
    source: Path

    @property
    def column_count(self) -> int:
        return sum(len(table.columns) for table in self.tables.values())

    def close(self) -> None:
        self.conn.close()


# ---------------------------------------------------------------------------
# DDL 解析：只解析 S1 需要的结构；数据交给 SQLite 执行
# ---------------------------------------------------------------------------


_CREATE_TABLE_RE = re.compile(
    r"CREATE\s+TABLE\s+(?:IF\s+NOT\s+EXISTS\s+)?`?(\w+)`?\s*\((.*?)\)"
    r"\s*(?:ENGINE\s*=\s*[^;]+)?;",
    re.IGNORECASE | re.DOTALL,
)
_COLUMN_RE = re.compile(
    r"^`?(\w+)`?\s+([A-Za-z]+(?:\s+(?:PRECISION|VARYING))?"
    r"(?:\s*\(\s*\d+\s*(?:,\s*\d+)?\s*\))?)\s*(.*)$",
    re.DOTALL,
)


def _strip_sql_comments(text: str) -> str:
    # 冻结 DDL 里没有含 "--" 的字符串字面量；按行剥离足够且可预测。
    return "\n".join(line.split("--", 1)[0] for line in text.splitlines())


def _split_top_level(text: str, separator: str = ",") -> list[str]:
    """按顶层分隔符切分，忽略括号内的逗号（如 DECIMAL(10,2)）。"""
    parts: list[str] = []
    depth = 0
    start = 0
    quote: str | None = None
    for index, char in enumerate(text):
        if quote is not None:
            if char == quote:
                quote = None
            continue
        if char in "'\"`":
            quote = char
        elif char == "(":
            depth += 1
        elif char == ")":
            depth -= 1
        elif char == separator and depth == 0:
            parts.append(text[start:index])
            start = index + 1
    parts.append(text[start:])
    return [part.strip() for part in parts if part.strip()]


def normalize_type(raw: str) -> str:
    """把 ``VARCHAR ( 20 )`` 归一成 ``VARCHAR(20)``。"""
    return re.sub(r"\s+", "", raw).upper()


def parse_ddl(text: str) -> dict[str, TableDef]:
    """从 MySQL DDL 中解析表名、列名、类型、可空性、主键。"""
    tables: dict[str, TableDef] = {}
    for match in _CREATE_TABLE_RE.finditer(_strip_sql_comments(text)):
        table_name = match.group(1)
        body = match.group(2)
        columns: list[ColumnDef] = []
        table_primary_key: str | None = None

        for part in _split_top_level(body):
            if re.match(r"^PRIMARY\s+KEY\b", part, re.IGNORECASE):
                key_match = re.search(r"\(([^)]+)\)", part)
                if key_match:
                    table_primary_key = key_match.group(1).strip().strip("`")
                continue
            if re.match(
                r"^(UNIQUE|KEY|INDEX|CONSTRAINT|FOREIGN|CHECK|FULLTEXT|SPATIAL|PARTITION)\b",
                part,
                re.IGNORECASE,
            ):
                continue

            column_match = _COLUMN_RE.match(part)
            if column_match is None:
                raise ValueError(f"无法解析 {table_name} 的列定义：{part!r}")

            name = column_match.group(1)
            column_type = normalize_type(column_match.group(2))
            rest = column_match.group(3).upper()
            primary_key = "PRIMARY KEY" in rest
            nullable = "NOT NULL" not in rest and not primary_key
            if primary_key:
                table_primary_key = name
            columns.append(
                ColumnDef(
                    name=name,
                    type=column_type,
                    nullable=nullable,
                    primary_key=primary_key,
                )
            )

        if not columns:
            raise ValueError(f"表 {table_name} 没有解析到任何列")

        # 表级 PRIMARY KEY (id) 也要回填到列上：否则会把主键误判成 nullable。
        if table_primary_key:
            if "," in table_primary_key:
                raise ValueError(
                    f"{table_name} 使用了复合主键；S1 契约只支持单列主键"
                )
            columns = [
                ColumnDef(
                    name=column.name,
                    type=column.type,
                    nullable=(
                        False if column.name == table_primary_key else column.nullable
                    ),
                    primary_key=(
                        column.primary_key or column.name == table_primary_key
                    ),
                )
                for column in columns
            ]

        tables[table_name] = TableDef(
            name=table_name,
            columns=columns,
            primary_key=table_primary_key,
        )
    if not tables:
        raise ValueError("DDL 中没有找到任何 CREATE TABLE")
    return tables


def _split_statements(text: str) -> list[str]:
    return [statement.strip() for statement in text.split(";") if statement.strip()]


def load_ddl(path: Path = DDL_PATH) -> Ddl:
    """解析 DDL 并装入内存 SQLite；不需要 MySQL/Docker。"""
    text = path.read_text(encoding="utf-8")
    tables = parse_ddl(text)

    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    for statement in _split_statements(_strip_sql_comments(text)):
        head = statement.lstrip().upper()
        if head.startswith(
            ("SET ", "CREATE DATABASE", "GRANT ", "USE ", "DROP DATABASE")
        ):
            continue
        conn.execute(statement)
    conn.commit()
    return Ddl(tables=tables, conn=conn, source=path)


def load_contract(path: Path = CONTRACT_PATH) -> dict[str, Any]:
    data = yaml.safe_load(path.read_text(encoding="utf-8"))
    if not isinstance(data, dict):
        raise ValueError(f"{path} 不是 YAML 对象")
    return data


# ---------------------------------------------------------------------------
# 通用小工具
# ---------------------------------------------------------------------------


def _contract_tables(contract: dict[str, Any]) -> dict[str, dict[str, Any]]:
    return {table["name"]: table for table in contract.get("tables", [])}


def _contract_columns(table: dict[str, Any]) -> dict[str, dict[str, Any]]:
    return {column["name"]: column for column in table.get("columns", [])}


def _contract_metrics(contract: dict[str, Any]) -> dict[str, dict[str, Any]]:
    return {metric["name"]: metric for metric in contract.get("metrics", [])}


def _scalar(conn: sqlite3.Connection, sql: str) -> Any:
    row = conn.execute(sql).fetchone()
    return row[0] if row is not None else None


def _path_label(path: Path) -> str:
    """文档里只出现仓库相对路径，避免把本机绝对路径写进生成物。"""
    try:
        return str(path.relative_to(REBUILD_ROOT))
    except ValueError:
        return str(path)


def _distinct(conn: sqlite3.Connection, table: str, column: str) -> list[Any]:
    rows = conn.execute(
        f'SELECT DISTINCT "{column}" FROM "{table}" '
        f'WHERE "{column}" IS NOT NULL ORDER BY "{column}"'
    ).fetchall()
    return [row[0] for row in rows]


def _sort_key(value: Any) -> tuple[str, str]:
    return (type(value).__name__, str(value))


def _values_equal(left: Any, right: Any) -> bool:
    if isinstance(left, (int, float)) and isinstance(right, (int, float)):
        return float(left) == float(right)
    return left == right


def _display(value: Any) -> str:
    if isinstance(value, float) and value.is_integer():
        return str(int(value))
    return str(value)


def _enum_key(value: Any) -> tuple[str, Any]:
    """枚举比较要保留类型：``2025`` 与 ``"2025"`` 不是同一个值。"""
    return (type(value).__name__, value)


_ID_SENTINELS = ("", " ", "not-an-id", "???", "中文", "A1B2C3!", "---")


def _id_pattern_is_weak(pattern: str) -> bool:
    """判断 id pattern 是否形同虚设。

    只比对 ``.*`` 这样的字符串不够：``^ORD\\d{11}$|^.*$`` 也等于没有约束。
    这里用一组“明显不是 ID”的哨兵值做反例；任何一个能匹配，就说明 pattern 太宽。
    """
    compact = pattern.replace(" ", "")
    if not compact.startswith("^") or not compact.endswith("$"):
        return True
    if compact in {"^.*$", "^.+$", "^(.+)$", "^(.+)?$"}:
        return True
    try:
        regex = re.compile(pattern)
    except re.error:
        return True
    return any(regex.fullmatch(value) is not None for value in _ID_SENTINELS)


def _declares_no_city(grain: str) -> bool:
    """地区粒度声明必须明确写出「没有城市」，只写「省」不够。"""
    text = grain.casefold().replace(" ", "")
    return any(
        phrase in text
        for phrase in ("无city", "没有city", "无城市", "没有城市")
    )


_METRIC_REF_RE = re.compile(
    r"\b([a-z_][a-z0-9_]*)\.([a-z_][a-z0-9_]*)\b", re.IGNORECASE
)


def _metric_refs(formula: str) -> set[str]:
    return {
        f"{match.group(1).lower()}.{match.group(2).lower()}"
        for match in _METRIC_REF_RE.finditer(formula)
    }


def _normalize_formula(formula: str) -> str:
    return re.sub(r"\s+", "", formula).upper()


# ---------------------------------------------------------------------------
# 别名空间：列别名/指标名/指标别名互撞；别名也不能遮蔽物理列名
# ---------------------------------------------------------------------------


def _alias_entries(
    tables: Sequence[dict[str, Any]],
    metrics: Sequence[dict[str, Any]],
    *,
    alias_key: str = "aliases",
) -> list[tuple[str, str]]:
    entries: list[tuple[str, str]] = []
    for table in tables:
        table_name = table["name"]
        for column in table.get("columns", []):
            owner = f"{table_name}.{column['name']}"
            # 列名是表内物理标识，跨表同名（customer_id / date_id）是 JOIN 的正常写法；
            # 这里只检查“别名”和“指标名”这套语义命名空间。
            for alias in column.get(alias_key, []) or []:
                entries.append((owner, str(alias)))
    for metric in metrics:
        owner = f"metric:{metric['name']}"
        entries.append((owner, metric["name"]))
        for alias in metric.get(alias_key, []) or []:
            entries.append((owner, str(alias)))
    return entries


def find_alias_collisions(
    tables: Sequence[dict[str, Any]],
    metrics: Sequence[dict[str, Any]],
    *,
    alias_key: str = "aliases",
    where_prefix: str = "alias",
) -> list[Issue]:
    """同一个名字不能指向两个不同的列/指标。"""
    owners: dict[str, set[str]] = {}
    spellings: dict[str, str] = {}
    for owner, alias in _alias_entries(tables, metrics, alias_key=alias_key):
        key = alias.strip().casefold()
        if not key:
            continue
        owners.setdefault(key, set()).add(owner)
        spellings.setdefault(key, alias.strip())

    issues: list[Issue] = []
    for key in sorted(owners):
        if len(owners[key]) > 1:
            issues.append(
                Issue(
                    code="ALIAS_COLLISION",
                    where=where_prefix,
                    message=(
                        f"别名「{spellings[key]}」同时指向 "
                        + "、".join(sorted(owners[key]))
                    ),
                )
            )
    return issues


def find_alias_shadowing_columns(
    tables: Sequence[dict[str, Any]],
    metrics: Sequence[dict[str, Any]],
    *,
    alias_key: str = "aliases",
    where_prefix: str = "alias",
) -> list[Issue]:
    """别名不能遮蔽另一个物理列名（如把 AOV 别名写成 ``order_id``）。"""
    column_owners: dict[str, set[str]] = {}
    for table in tables:
        for column in table.get("columns", []):
            column_owners.setdefault(column["name"].casefold(), set()).add(
                f"{table['name']}.{column['name']}"
            )

    issues: list[Issue] = []
    for owner, alias in _alias_entries(tables, metrics, alias_key=alias_key):
        key = alias.strip().casefold()
        if key in column_owners and owner not in column_owners[key]:
            issues.append(
                Issue(
                    code="ALIAS_SHADOWS_COLUMN",
                    where=where_prefix,
                    message=(
                        f"别名「{alias.strip()}」遮蔽了物理列 "
                        + "、".join(sorted(column_owners[key]))
                    ),
                )
            )
    return issues


def _duplicate_names(names: Iterable[str]) -> list[str]:
    seen: set[str] = set()
    duplicates: set[str] = set()
    for name in names:
        if name in seen:
            duplicates.add(name)
        seen.add(name)
    return sorted(duplicates)


def check_duplicates(contract: dict[str, Any]) -> list[Issue]:
    """YAML 里的重复定义不能静默 last-wins。"""
    issues: list[Issue] = []
    tables = contract.get("tables", []) or []
    for name in _duplicate_names(table.get("name", "") for table in tables):
        issues.append(Issue("DUPLICATE_TABLE", f"contract.{name}", "表名重复"))
    for table in tables:
        for name in _duplicate_names(
            column.get("name", "") for column in table.get("columns", []) or []
        ):
            issues.append(
                Issue(
                    "DUPLICATE_COLUMN",
                    f"{table.get('name')}.{name}",
                    "列名重复",
                )
            )
    for name in _duplicate_names(
        metric.get("name", "") for metric in contract.get("metrics", []) or []
    ):
        issues.append(Issue("DUPLICATE_METRIC", f"metric.{name}", "指标名重复"))
    return issues


# ---------------------------------------------------------------------------
# 契约检查：contract ↔ DDL ↔ 真实数据
# ---------------------------------------------------------------------------


def _check_dataset(contract: dict[str, Any], ddl: Ddl) -> list[Issue]:
    issues: list[Issue] = []
    dataset = contract.get("dataset", {})
    if contract.get("version") != 1:
        issues.append(Issue("CONTRACT_VERSION", "contract", "version 必须为 1"))

    expected_tables = dataset.get("expected_tables")
    if expected_tables != len(ddl.tables):
        issues.append(
            Issue(
                "DATASET_TABLE_COUNT",
                "dataset.expected_tables",
                f"契约声明 {expected_tables} 张表，DDL 实际 {len(ddl.tables)} 张",
            )
        )

    if "fact_order" not in ddl.tables:
        issues.append(Issue("FACT_TABLE_MISSING", "ddl", "缺少 fact_order"))
        return issues

    fact_rows = _scalar(ddl.conn, "SELECT COUNT(*) FROM fact_order")
    expected_rows = dataset.get("expected_fact_rows")
    if fact_rows != expected_rows:
        issues.append(
            Issue(
                "DATASET_ROW_COUNT",
                "dataset.expected_fact_rows",
                f"契约声明 {expected_rows} 行，fact_order 实际 {fact_rows} 行",
            )
        )

    date_range = dataset.get("date_range") or []
    actual_min = _scalar(ddl.conn, "SELECT MIN(date_id) FROM fact_order")
    actual_max = _scalar(ddl.conn, "SELECT MAX(date_id) FROM fact_order")
    if list(date_range) != [actual_min, actual_max]:
        issues.append(
            Issue(
                "DATASET_DATE_RANGE",
                "dataset.date_range",
                f"契约声明 {date_range}，实际 [{actual_min}, {actual_max}]",
            )
        )

    total_amount = _scalar(ddl.conn, "SELECT SUM(order_amount) FROM fact_order")
    if not _values_equal(total_amount, dataset.get("total_amount")):
        issues.append(
            Issue(
                "DATASET_TOTAL_AMOUNT",
                "dataset.total_amount",
                f"契约声明 {dataset.get('total_amount')}，实际 {total_amount}",
            )
        )

    total_quantity = _scalar(ddl.conn, "SELECT SUM(order_quantity) FROM fact_order")
    if not _values_equal(total_quantity, dataset.get("total_quantity")):
        issues.append(
            Issue(
                "DATASET_TOTAL_QUANTITY",
                "dataset.total_quantity",
                f"契约声明 {dataset.get('total_quantity')}，实际 {total_quantity}",
            )
        )

    return issues


def _check_table_structure(
    contract: dict[str, Any], ddl: Ddl
) -> list[Issue]:
    issues: list[Issue] = []
    contract_tables = _contract_tables(contract)
    ddl_tables = ddl.tables

    for name in sorted(set(contract_tables) - set(ddl_tables)):
        issues.append(
            Issue("TABLE_UNKNOWN", f"contract.{name}", "契约中的表在 DDL 中不存在")
        )
    for name in sorted(set(ddl_tables) - set(contract_tables)):
        issues.append(
            Issue("TABLE_UNDECLARED", f"ddl.{name}", "DDL 中的表未进入契约")
        )

    for name in sorted(set(contract_tables) & set(ddl_tables)):
        table = contract_tables[name]
        physical = ddl_tables[name]
        contract_columns = _contract_columns(table)
        physical_columns = physical.column_names

        for column in sorted(set(physical_columns) - set(contract_columns)):
            issues.append(
                Issue(
                    "COLUMN_UNDECLARED",
                    f"{name}.{column}",
                    "DDL 中存在但契约未声明",
                )
            )
        for column in sorted(set(contract_columns) - set(physical_columns)):
            issues.append(
                Issue(
                    "COLUMN_UNKNOWN",
                    f"{name}.{column}",
                    "契约声明了 DDL 中不存在的列",
                )
            )

        for column_name in sorted(set(contract_columns) & set(physical_columns)):
            column = contract_columns[column_name]
            physical_column = physical.column(column_name)
            assert physical_column is not None
            if normalize_type(str(column.get("type", ""))) != physical_column.type:
                issues.append(
                    Issue(
                        "COLUMN_TYPE_DRIFT",
                        f"{name}.{column_name}",
                        f"契约 {column.get('type')} ≠ DDL {physical_column.type}",
                    )
                )
            if bool(column.get("nullable", True)) != physical_column.nullable:
                issues.append(
                    Issue(
                        "COLUMN_NULLABLE_DRIFT",
                        f"{name}.{column_name}",
                        f"契约 nullable={column.get('nullable', True)} ≠ "
                        f"DDL nullable={physical_column.nullable}",
                    )
                )

        declared_pk = table.get("primary_key")
        if declared_pk != physical.primary_key:
            issues.append(
                Issue(
                    "PRIMARY_KEY_DRIFT",
                    f"{name}.primary_key",
                    f"契约 {declared_pk} ≠ DDL {physical.primary_key}",
                )
            )

    return issues


def _check_grain_and_measures(
    contract: dict[str, Any], ddl: Ddl
) -> list[Issue]:
    issues: list[Issue] = []
    contract_tables = _contract_tables(contract)

    for name, table in contract_tables.items():
        if name not in ddl.tables:
            continue
        grain = str(table.get("grain") or "").strip()
        if not grain:
            issues.append(
                Issue("GRAIN_UNDECLARED", f"{name}.grain", "必须声明一行代表什么")
            )

        if name == "dim_region":
            if "省" not in grain or not _declares_no_city(grain):
                issues.append(
                    Issue(
                        "REGION_GRAIN_INCOMPLETE",
                        "dim_region.grain",
                        "地区表粒度只到省，必须写明『无 city』",
                    )
                )
            if "city" in ddl.tables[name].column_names:
                issues.append(
                    Issue(
                        "CITY_COLUMN_PRESENT",
                        "dim_region.city",
                        "DDL 出现 city 列，需重新确认地区粒度",
                    )
                )

        if name == "fact_order":
            if "order_id" not in grain or not (
                "一行一单" in grain or "一行一个 order_id" in grain
            ):
                issues.append(
                    Issue(
                        "FACT_GRAIN_INCOMPLETE",
                        "fact_order.grain",
                        "事实表必须声明『一行一个 order_id（一单）』",
                    )
                )

        contract_columns = _contract_columns(table)
        measures = table.get("measures", []) or []
        declared_measure_columns = {measure.get("column") for measure in measures}
        for column_name, column in contract_columns.items():
            if column.get("role") == "measure" and column_name not in declared_measure_columns:
                issues.append(
                    Issue(
                        "MEASURE_UNDECLARED",
                        f"{name}.{column_name}",
                        "role=measure 但未在 measures 中声明口径",
                    )
                )

        for measure in measures:
            column_name = measure.get("column")
            where = f"{name}.{column_name}"
            column = contract_columns.get(column_name)
            if column is None:
                issues.append(
                    Issue("MEASURE_UNKNOWN_COLUMN", where, "度量列不存在")
                )
                continue
            # 订单量用主键 COUNT(DISTINCT order_id) 表达，主键同时充当度量基准；
            # 其他度量列必须是 role=measure。
            role = column.get("role")
            if role != "measure" and not (
                role == "primary_key"
                and measure.get("aggregation") == "count_distinct"
            ):
                issues.append(
                    Issue("MEASURE_ROLE_DRIFT", where, "度量列的 role 不是 measure")
                )
            if measure.get("aggregation") not in {"additive", "non_additive", "count_distinct"}:
                issues.append(
                    Issue(
                        "MEASURE_AGGREGATION",
                        where,
                        f"未知聚合语义 {measure.get('aggregation')!r}",
                    )
                )
            if not str(measure.get("unit") or "").strip():
                issues.append(Issue("MEASURE_UNIT", where, "度量必须声明单位"))
            if not str(measure.get("description") or "").strip():
                issues.append(Issue("MEASURE_DESCRIPTION", where, "度量必须声明口径"))

    fact_measures = {
        measure.get("column"): measure
        for measure in contract_tables.get("fact_order", {}).get("measures", []) or []
    }
    quantity = fact_measures.get("order_quantity", {})
    if quantity.get("unit") != "件" or "不是订单数" not in str(
        quantity.get("description", "")
    ):
        issues.append(
            Issue(
                "ORDER_QUANTITY_SEMANTICS",
                "fact_order.order_quantity",
                "必须写明『件数，不是订单数』",
            )
        )
    if quantity.get("aggregation") != "additive":
        issues.append(
            Issue(
                "ORDER_QUANTITY_AGGREGATION",
                "fact_order.order_quantity",
                "销量是件数求和，aggregation 必须是 additive",
            )
        )
    amount = fact_measures.get("order_amount", {})
    if amount.get("unit") != "元" or "不是单价" not in str(
        amount.get("description", "")
    ):
        issues.append(
            Issue(
                "ORDER_AMOUNT_SEMANTICS",
                "fact_order.order_amount",
                "必须写明『整单金额，不是单价』",
            )
        )
    if amount.get("aggregation") != "additive":
        issues.append(
            Issue(
                "ORDER_AMOUNT_AGGREGATION",
                "fact_order.order_amount",
                "GMV 是整单金额求和，aggregation 必须是 additive",
            )
        )
    order_id = fact_measures.get("order_id", {})
    if order_id.get("aggregation") != "count_distinct":
        issues.append(
            Issue(
                "ORDER_ID_SEMANTICS",
                "fact_order.order_id",
                "订单量必须用 COUNT(DISTINCT order_id)",
            )
        )

    return issues


def _check_foreign_keys(contract: dict[str, Any], ddl: Ddl) -> list[Issue]:
    issues: list[Issue] = []
    contract_tables = _contract_tables(contract)

    for table_name, table in contract_tables.items():
        if table_name not in ddl.tables:
            continue
        contract_columns = _contract_columns(table)
        declarations = table.get("foreign_keys", []) or []
        declared_columns = {declaration.get("column") for declaration in declarations}

        for column_name, column in contract_columns.items():
            # 本数仓的约定：非主键的 *_id 列一定是外键。DDL 没有 FK 约束，
            # 所以这条约定必须由契约强制，否则删掉 role/foreign_keys 也能过。
            if (
                column_name.endswith("_id")
                and column_name != table.get("primary_key")
                and column.get("role") != "foreign_key"
            ):
                issues.append(
                    Issue(
                        "FK_ROLE_MISSING",
                        f"{table_name}.{column_name}",
                        "非主键 *_id 列必须是 role=foreign_key",
                    )
                )
            if column.get("role") == "foreign_key" and column_name not in declared_columns:
                issues.append(
                    Issue(
                        "FK_DECLARATION_MISSING",
                        f"{table_name}.{column_name}",
                        "role=foreign_key 但未声明引用目标",
                    )
                )

        for declaration in declarations:
            column_name = declaration.get("column")
            reference = str(declaration.get("references") or "")
            where = f"{table_name}.{column_name}"
            if column_name not in contract_columns:
                issues.append(Issue("FK_UNKNOWN_COLUMN", where, "外键列不存在"))
                continue
            if "." not in reference:
                issues.append(
                    Issue("FK_REFERENCE_FORMAT", where, f"引用目标 {reference!r} 不是 table.column")
                )
                continue
            target_table, target_column = reference.split(".", 1)
            if target_table not in ddl.tables:
                issues.append(
                    Issue("FK_TARGET_MISSING", where, f"目标表 {target_table} 不存在")
                )
                continue
            target = ddl.tables[target_table].column(target_column)
            if target is None:
                issues.append(
                    Issue("FK_TARGET_MISSING", where, f"目标列 {reference} 不存在")
                )
                continue
            if not target.primary_key:
                issues.append(
                    Issue("FK_TARGET_NOT_KEY", where, f"目标列 {reference} 不是主键")
                )

            orphans = _scalar(
                ddl.conn,
                f'SELECT COUNT(*) FROM "{table_name}" AS child '
                f'LEFT JOIN "{target_table}" AS parent '
                f'ON child."{column_name}" = parent."{target_column}" '
                f'WHERE child."{column_name}" IS NOT NULL '
                f'AND parent."{target_column}" IS NULL',
            )
            if orphans:
                issues.append(
                    Issue(
                        "FK_ORPHAN_ROWS",
                        where,
                        f"{orphans} 行找不到 {reference}",
                    )
                )

    return issues


def _check_value_domains(contract: dict[str, Any], ddl: Ddl) -> list[Issue]:
    issues: list[Issue] = []
    contract_tables = _contract_tables(contract)

    for table_name, table in contract_tables.items():
        if table_name not in ddl.tables:
            continue
        fk_map = {
            declaration.get("column"): declaration.get("references")
            for declaration in table.get("foreign_keys", []) or []
        }
        for column_name, column in _contract_columns(table).items():
            if column_name not in ddl.tables[table_name].column_names:
                continue
            where = f"{table_name}.{column_name}"
            domain = column.get("value_domain")
            if not isinstance(domain, dict):
                issues.append(
                    Issue("VALUE_DOMAIN_MISSING", where, "每列都必须声明 value_domain")
                )
                continue
            kind = domain.get("kind")
            expected_kind = EXPECTED_KEY_DOMAIN_KINDS.get((table_name, column_name))
            if expected_kind is not None and kind != expected_kind:
                issues.append(
                    Issue(
                        "VALUE_DOMAIN_KIND_SEMANTICS",
                        where,
                        f"该列值域类型必须是 {expected_kind}，实际 {kind!r}",
                    )
                )
            if column.get("role") == "foreign_key" and kind != "foreign_key":
                issues.append(
                    Issue(
                        "VALUE_DOMAIN_FK_KIND",
                        where,
                        "role=foreign_key 的列必须使用 kind=foreign_key 值域",
                    )
                )
            if kind == "open":
                if (table_name, column_name) not in OPEN_VALUE_DOMAIN_ALLOWLIST:
                    issues.append(
                        Issue(
                            "VALUE_DOMAIN_OPEN_NOT_ALLOWED",
                            where,
                            "只有 customer_name / product_name 这类开放文本列可以 kind=open；"
                            "主键、枚举、外键必须显式约束",
                        )
                    )
                continue
            if kind == "enum":
                declared = list(domain.get("values") or [])
                actual = _distinct(ddl.conn, table_name, column_name)
                declared_keys = {_enum_key(value) for value in declared}
                actual_keys = {_enum_key(value) for value in actual}
                if declared_keys != actual_keys:
                    missing = sorted(
                        _display(value)
                        for value in actual
                        if _enum_key(value) not in declared_keys
                    )
                    extra = sorted(
                        _display(value)
                        for value in declared
                        if _enum_key(value) not in actual_keys
                    )
                    issues.append(
                        Issue(
                            "VALUE_DOMAIN_ENUM",
                            where,
                            f"枚举漂移：数据多出 {missing}，契约多出 {extra}",
                        )
                    )
            elif kind == "range":
                actual_min = _scalar(
                    ddl.conn,
                    f'SELECT MIN("{column_name}") FROM "{table_name}"',
                )
                actual_max = _scalar(
                    ddl.conn,
                    f'SELECT MAX("{column_name}") FROM "{table_name}"',
                )
                if not (
                    _values_equal(actual_min, domain.get("min"))
                    and _values_equal(actual_max, domain.get("max"))
                ):
                    issues.append(
                        Issue(
                            "VALUE_DOMAIN_RANGE",
                            where,
                            f"契约 [{domain.get('min')}, {domain.get('max')}] "
                            f"≠ 实际 [{actual_min}, {actual_max}]",
                        )
                    )
            elif kind == "id":
                pattern = str(domain.get("pattern") or "")
                if not pattern:
                    issues.append(
                        Issue("VALUE_DOMAIN_ID", where, "id 值域缺少 pattern")
                    )
                    continue
                expected_pattern = EXPECTED_ID_PATTERNS.get(
                    (table_name, column_name)
                )
                if expected_pattern is not None and pattern != expected_pattern:
                    issues.append(
                        Issue(
                            "VALUE_DOMAIN_ID_PATTERN_SEMANTICS",
                            where,
                            f"该列 pattern 必须是 {expected_pattern!r}，实际 {pattern!r}",
                        )
                    )
                if _id_pattern_is_weak(pattern):
                    issues.append(
                        Issue(
                            "VALUE_DOMAIN_ID_PATTERN_WEAK",
                            where,
                            f"pattern {pattern!r} 没有实际约束（必须首尾锚定且非 .*）",
                        )
                    )
                try:
                    regex = re.compile(pattern)
                except re.error as error:
                    issues.append(
                        Issue(
                            "VALUE_DOMAIN_ID_PATTERN_INVALID",
                            where,
                            f"pattern 无法编译：{error}",
                        )
                    )
                    continue
                bad = [
                    value
                    for value in _distinct(ddl.conn, table_name, column_name)
                    if regex.fullmatch(str(value)) is None
                ]
                if bad:
                    issues.append(
                        Issue(
                            "VALUE_DOMAIN_ID",
                            where,
                            f"{len(bad)} 个值不匹配 {pattern}：{bad[:5]}",
                        )
                    )
            elif kind == "foreign_key":
                reference = fk_map.get(column_name)
                if not reference:
                    issues.append(
                        Issue("VALUE_DOMAIN_FK", where, "foreign_key 值域缺少引用目标")
                    )
                if "range" in domain:
                    actual_min = _scalar(
                        ddl.conn,
                        f'SELECT MIN("{column_name}") FROM "{table_name}"',
                    )
                    actual_max = _scalar(
                        ddl.conn,
                        f'SELECT MAX("{column_name}") FROM "{table_name}"',
                    )
                    range_values = list(domain.get("range") or [])
                    if (
                        len(range_values) != 2
                        or not _values_equal(actual_min, range_values[0])
                        or not _values_equal(actual_max, range_values[1])
                    ):
                        issues.append(
                            Issue(
                                "VALUE_DOMAIN_RANGE",
                                where,
                                f"契约 {range_values} ≠ 实际 [{actual_min}, {actual_max}]",
                            )
                        )
                if domain.get("format") == "yyyyMMdd":
                    bad = [
                        value
                        for value in _distinct(ddl.conn, table_name, column_name)
                        if not (isinstance(value, int) and len(str(value)) == 8)
                    ]
                    if bad:
                        issues.append(
                            Issue(
                                "VALUE_DOMAIN_DATE_FORMAT",
                                where,
                                f"{len(bad)} 个值不是 yyyyMMdd 整数：{bad[:5]}",
                            )
                        )
            else:
                issues.append(
                    Issue("VALUE_DOMAIN_KIND", where, f"未知值域类型 {kind!r}")
                )

    return issues


def _check_metrics(contract: dict[str, Any], ddl: Ddl) -> list[Issue]:
    issues: list[Issue] = []
    metrics = _contract_metrics(contract)
    if not metrics:
        return [Issue("METRIC_EMPTY", "metrics", "至少需要一个指标")]

    for name, metric in metrics.items():
        where = f"metric.{name}"
        formula = str(metric.get("formula") or "").strip()
        relevant = {str(column).lower() for column in metric.get("relevant_columns", [])}
        if not formula:
            issues.append(Issue("METRIC_FORMULA_MISSING", where, "缺少公式"))
            continue
        refs = _metric_refs(formula)
        if not refs:
            issues.append(
                Issue("METRIC_FORMULA_INVALID", where, "公式中没有 table.column 引用")
            )
            continue
        if refs != relevant:
            issues.append(
                Issue(
                    "METRIC_RELEVANT_COLUMNS",
                    where,
                    f"公式引用 {sorted(refs)} ≠ relevant_columns {sorted(relevant)}",
                )
            )
        for reference in sorted(refs):
            table_name, column_name = reference.split(".", 1)
            if table_name not in ddl.tables:
                issues.append(
                    Issue("METRIC_UNKNOWN_TABLE", where, f"引用了不存在的表 {table_name}")
                )
                continue
            if column_name not in ddl.tables[table_name].column_names:
                issues.append(
                    Issue(
                        "METRIC_UNKNOWN_COLUMN",
                        where,
                        f"引用了不存在的列 {reference}",
                    )
                )
        tables = {reference.split(".", 1)[0] for reference in refs}
        if len(tables) != 1:
            issues.append(
                Issue(
                    "METRIC_MULTI_TABLE",
                    where,
                    "S1 只校验单表指标；跨表指标留给后续阶段",
                )
            )
            continue
        if not refs <= {
            f"{table}.{column}"
            for table in ddl.tables
            for column in ddl.tables[table].column_names
        }:
            continue

        try:
            # SQLite 的整数除法会截断（622/115=5），MySQL 返回小数。
            # 这里只影响“求值校验”，不改契约公式：把每个 `/` 的左侧乘 1.0。
            sqlite_formula = formula.replace("/", "* 1.0 /")
            actual = _scalar(
                ddl.conn,
                f"SELECT {sqlite_formula} FROM {next(iter(tables))}",
            )
        except sqlite3.Error as error:
            issues.append(
                Issue("METRIC_FORMULA_INVALID", where, f"SQLite 无法求值：{error}")
            )
            continue
        expected = metric.get("expected_value")
        if not isinstance(expected, (int, float)):
            issues.append(
                Issue("METRIC_EXPECTED_VALUE", where, "expected_value 必须是数字")
            )
        elif actual is None or abs(float(actual) - float(expected)) > 0.005:
            issues.append(
                Issue(
                    "METRIC_VALUE_MISMATCH",
                    where,
                    f"公式实际值 {actual} ≠ 契约期望 {expected}",
                )
            )

        if metric.get("aggregation") not in {"additive", "non_additive"}:
            issues.append(
                Issue(
                    "METRIC_AGGREGATION",
                    where,
                    f"未知聚合语义 {metric.get('aggregation')!r}",
                )
            )
        if not str(metric.get("grain") or "").strip():
            issues.append(Issue("METRIC_GRAIN", where, "指标必须声明 grain"))

        expected_formula = EXPECTED_METRIC_FORMULAS.get(name)
        if (
            expected_formula is not None
            and _normalize_formula(formula) != expected_formula
        ):
            issues.append(
                Issue(
                    "METRIC_FORMULA_SEMANTICS",
                    where,
                    f"{name} 的公式必须是 {expected_formula}",
                )
            )

        semantics = EXPECTED_METRIC_SEMANTICS.get(name)
        if semantics is not None:
            expected_aggregation, expected_grain = semantics
            if metric.get("aggregation") != expected_aggregation:
                issues.append(
                    Issue(
                        "METRIC_AGGREGATION_SEMANTICS",
                        where,
                        f"{name} 的 aggregation 必须是 {expected_aggregation}，"
                        f"实际 {metric.get('aggregation')!r}",
                    )
                )
            if str(metric.get("grain") or "").strip() != expected_grain:
                issues.append(
                    Issue(
                        "METRIC_GRAIN_SEMANTICS",
                        where,
                        f"{name} 的 grain 必须是 {expected_grain}，"
                        f"实际 {metric.get('grain')!r}",
                    )
                )

    for name in EXPECTED_METRIC_SEMANTICS:
        if name not in metrics:
            issues.append(
                Issue("METRIC_MISSING", f"metric.{name}", "缺少必需指标")
            )

    # AOV 是旧系统最危险的歧义点，单独做语义检查。
    aov = metrics.get("AOV")
    if aov is None:
        issues.append(Issue("AOV_MISSING", "metric.AOV", "缺少 AOV 指标"))
    else:
        formula = str(aov.get("formula") or "").upper()
        relevant = {str(column).lower() for column in aov.get("relevant_columns", [])}
        aliases = {str(alias) for alias in aov.get("aliases", []) or []}
        description = str(aov.get("description") or "")
        if not (
            "ORDER_AMOUNT" in formula
            and "ORDER_ID" in formula
            and "COUNT" in formula
            and "DISTINCT" in formula
            and "/" in formula
        ):
            issues.append(
                Issue(
                    "AOV_FORMULA",
                    "metric.AOV",
                    "AOV 必须是 SUM(order_amount) / COUNT(DISTINCT order_id)",
                )
            )
        if "fact_order.order_quantity" in relevant:
            issues.append(
                Issue(
                    "AOV_COLUMN_MISMATCH",
                    "metric.AOV",
                    "AOV 的 relevant_columns 不能是件数列 order_quantity",
                )
            )
        if aliases & {"平均单价", "件单价"}:
            issues.append(
                Issue(
                    "AOV_ALIAS_MISMATCH",
                    "metric.AOV",
                    "『平均单价』属于平均件单价，不能作为 AOV 别名",
                )
            )
        if "每单" not in description:
            issues.append(
                Issue(
                    "AOV_DESCRIPTION_MISMATCH",
                    "metric.AOV",
                    "AOV 描述必须写明『平均每单』",
                )
            )

        for rejected in aov.get("rejected_values", []) or []:
            value = rejected.get("value")
            meaning = str(rejected.get("meaning") or "")
            matches = [
                metric
                for metric in metrics.values()
                if metric is not aov
                and isinstance(metric.get("expected_value"), (int, float))
                and isinstance(value, (int, float))
                and abs(float(metric["expected_value"]) - float(value)) <= 0.005
            ]
            if not matches:
                issues.append(
                    Issue(
                        "AOV_REJECTED_UNKNOWN",
                        "metric.AOV.rejected_values",
                        f"{value} 不对应任何已知指标",
                    )
                )
            elif not any(
                metric["name"] in meaning or metric.get("label", "") in meaning
                for metric in matches
            ):
                issues.append(
                    Issue(
                        "AOV_REJECTED_MEANING",
                        "metric.AOV.rejected_values",
                        f"{value} 的含义没有指向 {[m['name'] for m in matches]}",
                    )
                )

    # 必须是 COUNT(DISTINCT order_id)：order_id 是主键，去掉 DISTINCT 数值仍然对，
    # 但语义已经退化成 COUNT(*)，将来一旦允许一行多明细就会算错。
    order_count = next(
        (
            metric
            for metric in metrics.values()
            if re.sub(r"\s+", "", str(metric.get("formula") or "")).upper()
            == "COUNT(DISTINCTFACT_ORDER.ORDER_ID)"
        ),
        None,
    )
    if order_count is None:
        issues.append(
            Issue(
                "ORDER_COUNT_UNDEFINED",
                "metrics",
                "必须定义订单量 = COUNT(DISTINCT fact_order.order_id)",
            )
        )

    per_item = metrics.get("平均件单价")
    if per_item is None:
        issues.append(
            Issue("PER_ITEM_METRIC_MISSING", "metrics", "缺少平均件单价指标")
        )
    elif "平均单价" not in {str(alias) for alias in per_item.get("aliases", []) or []}:
        issues.append(
            Issue(
                "PER_ITEM_ALIAS_MISSING",
                "metric.平均件单价",
                "『平均单价』必须归平均件单价",
            )
        )

    return issues


def check_contract(contract: dict[str, Any], ddl: Ddl) -> list[Issue]:
    """契约 ↔ DDL ↔ 数据；返回空列表表示绿。"""
    issues: list[Issue] = []
    issues.extend(check_duplicates(contract))
    issues.extend(_check_dataset(contract, ddl))
    issues.extend(_check_table_structure(contract, ddl))
    issues.extend(_check_grain_and_measures(contract, ddl))
    issues.extend(_check_foreign_keys(contract, ddl))
    issues.extend(_check_value_domains(contract, ddl))
    issues.extend(_check_metrics(contract, ddl))
    issues.extend(
        find_alias_collisions(
            contract.get("tables", []),
            contract.get("metrics", []),
            where_prefix="contract.alias",
        )
    )
    issues.extend(
        find_alias_shadowing_columns(
            contract.get("tables", []),
            contract.get("metrics", []),
            where_prefix="contract.alias",
        )
    )
    return issues


# ---------------------------------------------------------------------------
# 负向夹具：旧系统必须红
# ---------------------------------------------------------------------------


def _check_legacy_meta(meta: dict[str, Any], ddl: Ddl) -> list[Issue]:
    issues: list[Issue] = []
    legacy_tables = {table["name"]: table for table in meta.get("tables", []) or []}
    legacy_metrics = {metric["name"]: metric for metric in meta.get("metrics", []) or []}

    for name in sorted(set(legacy_tables) - set(ddl.tables)):
        issues.append(
            Issue("LEGACY_TABLE_DRIFT", f"legacy.{name}", "旧元数据多出表")
        )
    for name in sorted(set(ddl.tables) - set(legacy_tables)):
        issues.append(
            Issue("LEGACY_TABLE_DRIFT", f"legacy.{name}", "旧元数据缺少表")
        )
    for name in sorted(set(legacy_tables) & set(ddl.tables)):
        legacy_columns = {
            column["name"] for column in legacy_tables[name].get("columns", []) or []
        }
        if legacy_columns != ddl.tables[name].column_names:
            issues.append(
                Issue(
                    "LEGACY_COLUMN_DRIFT",
                    f"legacy.{name}",
                    f"旧元数据列 {sorted(legacy_columns)} ≠ "
                    f"DDL {sorted(ddl.tables[name].column_names)}",
                )
            )

    # AOV：描述说金额平均、依赖列是件数、别名又占着「平均单价」。
    aov = legacy_metrics.get("AOV")
    if aov is None:
        issues.append(Issue("AOV_MISSING", "legacy.metric.AOV", "旧元数据缺少 AOV"))
    else:
        description = str(aov.get("description") or "")
        relevant = [str(column) for column in aov.get("relevant_columns", []) or []]
        aliases = [str(alias) for alias in aov.get("alias", []) or []]
        description_is_amount_average = (
            "金额" in description or "成交额" in description
        ) and ("平均" in description or "均值" in description)
        columns_are_quantity = any(
            column.endswith("order_quantity") for column in relevant
        )
        alias_is_per_item = "平均单价" in aliases

        if description_is_amount_average and columns_are_quantity:
            issues.append(
                Issue(
                    "AOV_COLUMN_MISMATCH",
                    "legacy.metric.AOV",
                    f"描述是金额平均，relevant_columns 却是 {relevant}",
                )
            )
        if alias_is_per_item:
            issues.append(
                Issue(
                    "AOV_ALIAS_MISMATCH",
                    "legacy.metric.AOV",
                    "别名『平均单价』指向 AOV，但 AOV 不是件单价",
                )
            )
        if description_is_amount_average and columns_are_quantity and alias_is_per_item:
            issues.append(
                Issue(
                    "AOV_THREE_WAY",
                    "legacy.metric.AOV",
                    "描述(金额平均) / 依赖列(件数) / 别名(平均单价) 三方互相矛盾；"
                    "同一词可算出 2427.47、448.81、5.41 三种结果",
                )
            )

    # 城市粒度：DDL 没有 city 列，旧元数据也不声明「只到省」。
    region = legacy_tables.get("dim_region")
    if region is not None:
        region_columns = {
            column["name"] for column in region.get("columns", []) or []
        }
        grain = str(region.get("grain") or "")
        if "city" not in region_columns and not _declares_no_city(grain):
            issues.append(
                Issue(
                    "CITY_GRAIN_UNDECLARED",
                    "legacy.dim_region",
                    "物理结构没有 city 列，元数据却未声明『只到省，无城市粒度』；"
                    "LLM 只能把 province 起别名叫『城市』",
                )
            )

    # 订单量：没有任何指标/列把 COUNT(DISTINCT order_id) 定义成订单量。
    order_count_defined = any(
        "fact_order.order_id" in (metric.get("relevant_columns") or [])
        and "COUNT" in str(metric.get("formula") or "").upper()
        for metric in legacy_metrics.values()
    )
    if not order_count_defined:
        issues.append(
            Issue(
                "ORDER_COUNT_UNDEFINED",
                "legacy.metrics",
                "旧元数据没有订单量指标；order_quantity 的别名『销量』容易被写成 "
                "SUM(order_quantity)，比真实订单量偏大 5.41 倍",
            )
        )

    issues.extend(
        find_alias_collisions(
            list(legacy_tables.values()),
            list(legacy_metrics.values()),
            alias_key="alias",
            where_prefix="legacy.alias",
        )
    )
    return issues


def check_feature_list(
    contract: dict[str, Any],
    ddl: Ddl,
    path: Path = FEATURE_LIST_PATH,
) -> list[Issue]:
    """功能清单声称的「6 表 / 138 行」必须和 DDL 实际对账。"""
    issues: list[Issue] = []
    label = _path_label(path)
    if not path.exists():
        return [Issue("FEATURE_LIST_MISSING", label, "功能清单夹具不存在")]
    text = path.read_text(encoding="utf-8")

    # 一份文档里可能同时存在「已修正的数字」和「残留的旧数字」；
    # 必须把所有声明都扫一遍，不能只信第一处。
    table_claims = {
        int(match.group(1))
        for match in re.finditer(r"(\d+)\s*张[^。；;\n]*?表", text)
    }
    row_claims = {
        int(match.group(1)) for match in re.finditer(r"共\s*(\d+)\s*行", text)
    }
    actual_tables = len(ddl.tables)
    actual_rows = _scalar(ddl.conn, "SELECT COUNT(*) FROM fact_order")

    if not table_claims:
        issues.append(
            Issue("FEATURE_LIST_UNPARSEABLE", label, "找不到『N 张…表』")
        )
    for claimed_tables in sorted(table_claims - {actual_tables}):
        issues.append(
            Issue(
                "FEATURE_LIST_TABLE_DRIFT",
                label,
                f"功能清单声称 {claimed_tables} 张表，DDL 实际 {actual_tables} 张",
            )
        )

    if not row_claims:
        issues.append(
            Issue("FEATURE_LIST_UNPARSEABLE", label, "找不到『共 N 行』")
        )
    for claimed_rows in sorted(row_claims - {actual_rows}):
        issues.append(
            Issue(
                "FEATURE_LIST_ROW_DRIFT",
                label,
                f"功能清单声称 {claimed_rows} 行订单，fact_order 实际 {actual_rows} 行",
            )
        )
    return issues


def check_legacy(
    contract: dict[str, Any],
    ddl: Ddl,
    meta_path: Path = LEGACY_META_PATH,
    feature_path: Path = FEATURE_LIST_PATH,
) -> list[Issue]:
    """旧系统负向夹具：AOV 三义 / 无 city / 订单量未定义 / 别名互撞 / 功能清单漂移。"""
    meta = yaml.safe_load(meta_path.read_text(encoding="utf-8"))
    if not isinstance(meta, dict):
        return [Issue("LEGACY_META_INVALID", str(meta_path), "不是 YAML 对象")]
    return _check_legacy_meta(meta, ddl) + check_feature_list(
        contract, ddl, path=feature_path
    )


# ---------------------------------------------------------------------------
# 人读版契约
# ---------------------------------------------------------------------------


def _render_domain(domain: dict[str, Any] | None) -> str:
    if not isinstance(domain, dict):
        return "—"
    kind = domain.get("kind")
    if kind == "enum":
        return "枚举：" + " / ".join(_display(value) for value in domain.get("values", []))
    if kind == "range":
        return f"范围：[{_display(domain.get('min'))}, {_display(domain.get('max'))}]"
    if kind == "id":
        return f"格式：`{domain.get('pattern')}`"
    if kind == "foreign_key":
        suffix = []
        if domain.get("format"):
            suffix.append(str(domain["format"]))
        if domain.get("range"):
            suffix.append(
                f"[{_display(domain['range'][0])}, {_display(domain['range'][1])}]"
            )
        return "外键" + (f"（{'，'.join(suffix)}）" if suffix else "")
    return "开放"


def _md_escape(value: Any) -> str:
    return str(value).replace("|", "\\|").replace("\n", " ")


def render_doc(contract: dict[str, Any], ddl: Ddl) -> str:
    """生成 docs/SCHEMA-CONTRACT.md；输出必须确定，便于做新鲜度检查。"""
    dataset = contract.get("dataset", {})
    tables = contract.get("tables", [])
    metrics = contract.get("metrics", [])
    legacy_issues = check_legacy(contract, ddl)
    fact_rows = _scalar(ddl.conn, "SELECT COUNT(*) FROM fact_order")
    total_amount = _scalar(ddl.conn, "SELECT SUM(order_amount) FROM fact_order")
    total_quantity = _scalar(ddl.conn, "SELECT SUM(order_quantity) FROM fact_order")
    min_date = _scalar(ddl.conn, "SELECT MIN(date_id) FROM fact_order")
    max_date = _scalar(ddl.conn, "SELECT MAX(date_id) FROM fact_order")

    lines: list[str] = []
    lines.append("# Schema 契约（自动生成）")
    lines.append("")
    lines.append("> 由 `python -m app.schema_contract --write-doc` 生成，请勿手改。")
    lines.append("> 语义事实源：`contracts/schema.contract.yaml`；")
    lines.append("> 物理结构与数据：`fixtures/dw.sql`（内存 SQLite 加载，不需要 MySQL）。")
    lines.append("")
    lines.append("## 数据集")
    lines.append("")
    lines.append("| 项 | 值 |")
    lines.append("|---|---|")
    lines.append(f"| 表数 | {len(ddl.tables)} |")
    lines.append(f"| 列数 | {ddl.column_count} |")
    lines.append(f"| 事实表行数 | {fact_rows} |")
    lines.append(f"| 日期范围 | {min_date} – {max_date} |")
    lines.append(f"| GMV | {total_amount:,.1f} |")
    lines.append(f"| 销量 | {total_quantity} |")
    aov = next((metric for metric in metrics if metric["name"] == "AOV"), None)
    if aov is not None:
        lines.append(f"| AOV | {float(aov['expected_value']):,.2f} |")
    lines.append("")
    lines.append(f"> {dataset.get('note', '')}")
    lines.append("")

    lines.append("## 表契约")
    lines.append("")
    for table in tables:
        name = table["name"]
        role = table.get("role", "?")
        lines.append(f"### `{name}`（{role}）")
        lines.append("")
        lines.append(f"- **粒度**：{table.get('grain', '—')}")
        lines.append(f"- **主键**：`{table.get('primary_key') or '—'}`")
        lines.append(f"- **说明**：{table.get('description', '—')}")
        lines.append("")
        lines.append("| 列 | 类型 | 可空 | 角色 | 值域 | 说明 | 别名 |")
        lines.append("|---|---|---|---|---|---|---|")
        for column in table.get("columns", []):
            aliases = " / ".join(str(alias) for alias in column.get("aliases", []) or [])
            lines.append(
                "| `{name}` | `{type}` | {nullable} | {role} | {domain} | {description} | {aliases} |".format(
                    name=column["name"],
                    type=column.get("type", "—"),
                    nullable="是" if column.get("nullable", True) else "否",
                    role=column.get("role", "—"),
                    domain=_md_escape(_render_domain(column.get("value_domain"))),
                    description=_md_escape(column.get("description", "—")),
                    aliases=_md_escape(aliases) or "—",
                )
            )
        lines.append("")
        foreign_keys = table.get("foreign_keys", []) or []
        if foreign_keys:
            lines.append("- **外键**：")
            for foreign_key in foreign_keys:
                lines.append(
                    f"  - `{name}.{foreign_key.get('column')}` → "
                    f"`{foreign_key.get('references')}`"
                )
        else:
            lines.append("- **外键**：无")
        measures = table.get("measures", []) or []
        if measures:
            lines.append("- **度量**：")
            for measure in measures:
                lines.append(
                    f"  - `{measure.get('column')}`：{measure.get('aggregation')}，"
                    f"单位 {measure.get('unit')}；{measure.get('description')}"
                )
        else:
            lines.append("- **度量**：无")
        lines.append("")

    lines.append("## 指标契约")
    lines.append("")
    lines.append("| 指标 | 口径（公式） | 粒度 | 期望值 | 依赖列 | 别名 |")
    lines.append("|---|---|---|---|---|---|")
    for metric in metrics:
        lines.append(
            "| {name} | `{formula}` | {grain} | {value} | {columns} | {aliases} |".format(
                name=_md_escape(metric["name"]),
                formula=_md_escape(metric.get("formula", "—")),
                grain=_md_escape(metric.get("grain", "—")),
                value=_display(metric.get("expected_value")),
                columns=_md_escape(
                    " / ".join(metric.get("relevant_columns", []) or [])
                ),
                aliases=_md_escape(
                    " / ".join(str(alias) for alias in metric.get("aliases", []) or [])
                )
                or "—",
            )
        )
    lines.append("")

    if aov is not None:
        lines.append("### AOV 的唯一口径")
        lines.append("")
        lines.append(
            f"AOV = `{aov.get('formula')}` = **{float(aov['expected_value']):,.2f}**。"
        )
        lines.append("")
        lines.append("以下值**不是** AOV：")
        lines.append("")
        lines.append("| 值 | 真实含义 |")
        lines.append("|---|---|")
        for rejected in aov.get("rejected_values", []) or []:
            lines.append(
                f"| {_display(rejected.get('value'))} | {_md_escape(rejected.get('meaning', '—'))} |"
            )
        lines.append("")

    lines.append("## 负向夹具（必须红）")
    lines.append("")
    lines.append(
        "这些是旧系统 `fixtures/legacy/*` 的真实缺陷。它们**不是**事实源；"
        "检查器必须持续把它们检出来，否则说明防线退化。"
    )
    lines.append("")
    lines.append("| 代码 | 位置 | 说明 |")
    lines.append("|---|---|---|")
    for issue in legacy_issues:
        lines.append(
            f"| `{issue.code}` | `{_md_escape(issue.where)}` | {_md_escape(issue.message)} |"
        )
    lines.append("")
    lines.append("## 复现")
    lines.append("")
    lines.append("```bash")
    lines.append("cd backend")
    lines.append("uv run python -m app.schema_contract --check")
    lines.append("uv run python -m app.schema_contract --check-legacy   # 预期退出码 1")
    lines.append("uv run python -m app.schema_contract --write-doc")
    lines.append("```")
    lines.append("")
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def _print_issues(issues: Iterable[Issue]) -> None:
    for issue in issues:
        print(f"  {issue}")


def _doc_is_fresh(contract: dict[str, Any], ddl: Ddl) -> bool:
    if not DOC_PATH.exists():
        return False
    return DOC_PATH.read_text(encoding="utf-8") == render_doc(contract, ddl)


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="python -m app.schema_contract",
        description="S1 Schema 契约检查（DDL ↔ contract ↔ 旧系统负向夹具）",
    )
    group = parser.add_mutually_exclusive_group(required=True)
    group.add_argument(
        "--check",
        action="store_true",
        help="契约绿 + 负向夹具红 + 文档新鲜（发布门禁）",
    )
    group.add_argument(
        "--check-legacy",
        action="store_true",
        help="检查旧系统夹具；有缺陷时退出码 1（红）",
    )
    group.add_argument(
        "--expect-legacy-red",
        action="store_true",
        help="断言旧系统夹具按预期变红（8 个代码齐全）",
    )
    group.add_argument("--write-doc", action="store_true", help="生成人读版契约")
    group.add_argument("--check-doc", action="store_true", help="检查人读版是否过期")
    args = parser.parse_args(argv)

    contract = load_contract()
    ddl = load_ddl()
    try:
        if args.check:
            print("== 1/3 契约 ↔ DDL/数据 ==")
            issues = check_contract(contract, ddl)
            if issues:
                _print_issues(issues)
                print(f"FAIL: {len(issues)} 个契约问题")
                return 1
            print(
                f"PASS: {len(ddl.tables)} 张表 / {ddl.column_count} 列 / "
                f"{_scalar(ddl.conn, 'SELECT COUNT(*) FROM fact_order')} 行 / "
                f"GMV={_scalar(ddl.conn, 'SELECT SUM(order_amount) FROM fact_order')} / "
                f"销量={_scalar(ddl.conn, 'SELECT SUM(order_quantity) FROM fact_order')}"
            )

            print("== 2/3 负向夹具（必须红） ==")
            legacy = check_legacy(contract, ddl)
            codes = {issue.code for issue in legacy}
            missing = [code for code in EXPECTED_LEGACY_CODES if code not in codes]
            unexpected = sorted(codes - set(EXPECTED_LEGACY_CODES))
            if missing or unexpected:
                print(
                    "FAIL: 负向夹具与预期不符，"
                    f"缺少 {missing}，多出 {unexpected}"
                )
                _print_issues(legacy)
                return 1
            print(f"PASS: 旧系统 {len(legacy)} 条问题 / {len(codes)} 个代码（预期红）")
            for code in EXPECTED_LEGACY_CODES:
                print(f"  - {code}")

            print("== 3/3 文档新鲜度 ==")
            if not _doc_is_fresh(contract, ddl):
                print(f"FAIL: {DOC_PATH.relative_to(REBUILD_ROOT)} 已过期")
                print("     运行 --write-doc 重新生成")
                return 1
            print(f"PASS: {DOC_PATH.relative_to(REBUILD_ROOT)} 与契约一致")
            print("S1 PASS")
            return 0

        if args.check_legacy:
            issues = check_legacy(contract, ddl)
            if not issues:
                print("GREEN: 旧系统夹具没有检出问题（这不正常）")
                return 0
            print(f"RED: 旧系统夹具检出 {len(issues)} 条问题")
            _print_issues(issues)
            return 1

        if args.expect_legacy_red:
            issues = check_legacy(contract, ddl)
            codes = {issue.code for issue in issues}
            missing = [code for code in EXPECTED_LEGACY_CODES if code not in codes]
            unexpected = sorted(codes - set(EXPECTED_LEGACY_CODES))
            if not issues:
                print("FAIL: 预期负向夹具变红，实际为绿")
                return 1
            if missing or unexpected:
                print(f"FAIL: 负向夹具缺少 {missing}，多出 {unexpected}")
                _print_issues(issues)
                return 1
            print(
                f"PASS: 负向夹具按预期变红，{len(codes)} 个代码 / {len(issues)} 条问题"
            )
            return 0

        if args.write_doc:
            DOC_PATH.parent.mkdir(parents=True, exist_ok=True)
            DOC_PATH.write_text(render_doc(contract, ddl), encoding="utf-8")
            print(f"WROTE: {DOC_PATH.relative_to(REBUILD_ROOT)}")
            return 0

        if args.check_doc:
            if _doc_is_fresh(contract, ddl):
                print(f"PASS: {DOC_PATH.relative_to(REBUILD_ROOT)} 与契约一致")
                return 0
            print(f"FAIL: {DOC_PATH.relative_to(REBUILD_ROOT)} 已过期")
            return 1
    finally:
        ddl.close()

    return 1


if __name__ == "__main__":
    sys.exit(main())
