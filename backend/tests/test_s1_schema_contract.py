"""
S1 契约测试

验收标准（对应 REBUILD-ROADMAP.md 的 `make stage1`）：
  1. DDL 可被解析并装入内存 SQLite：5 张表 / 24 列 / 115 单
  2. contracts/schema.contract.yaml 对 DDL + 数据全绿
  3. 6 个指标的值唯一：GMV=279159.5、AOV=2427.47、订单量=115、
     销量=622、平均件单价=448.81、平均每单件数=5.41
  4. 旧系统夹具必须红，且 8 个已知代码一个不少
  5. 功能清单「6 表 / 138 行」漂移必须被检出
  6. 契约被改坏时必须红：AOV 公式/依赖列/别名、city 列、别名互撞、值域、订单量
  7. 生成的 docs/SCHEMA-CONTRACT.md 与契约一致
"""

from __future__ import annotations

import copy
from pathlib import Path

import pytest
import yaml

from app import schema_contract as sc

ROOT = Path(__file__).resolve().parents[2]


@pytest.fixture(scope="module")
def contract() -> dict:
    return sc.load_contract()


@pytest.fixture(scope="module")
def ddl():
    loaded = sc.load_ddl()
    yield loaded
    loaded.close()


def codes_of(issues) -> set[str]:
    return {issue.code for issue in issues}


# ---------------------------------------------------------------------------
# 物理层：DDL + 数据
# ---------------------------------------------------------------------------


def test_ddl_shape(ddl) -> None:
    assert set(ddl.tables) == {
        "dim_region",
        "dim_customer",
        "dim_product",
        "dim_date",
        "fact_order",
    }
    assert len(ddl.tables) == 5
    assert ddl.column_count == 24
    assert ddl.tables["fact_order"].primary_key == "order_id"
    assert ddl.tables["dim_region"].column("city") is None
    assert ddl.tables["dim_product"].column("price") is None


def test_dataset_facts(ddl) -> None:
    assert sc._scalar(ddl.conn, "SELECT COUNT(*) FROM fact_order") == 115
    assert sc._scalar(ddl.conn, "SELECT SUM(order_amount) FROM fact_order") == 279159.5
    assert sc._scalar(ddl.conn, "SELECT SUM(order_quantity) FROM fact_order") == 622
    assert sc._scalar(ddl.conn, "SELECT MIN(date_id) FROM fact_order") == 20250101
    assert sc._scalar(ddl.conn, "SELECT MAX(date_id) FROM fact_order") == 20250331
    assert (
        sc._scalar(ddl.conn, "SELECT COUNT(DISTINCT date_id) FROM dim_date") == 90
    )


def test_ddl_parser_supports_table_level_pk_and_engine_options() -> None:
    tables = sc.parse_ddl(
        """
        CREATE TABLE sample (
            id VARCHAR(20),
            name VARCHAR(50),
            PRIMARY KEY (id)
        ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;
        """
    )
    sample = tables["sample"]
    assert sample.primary_key == "id"
    assert sample.column("id").primary_key is True
    assert sample.column("id").nullable is False
    assert sample.column("name").nullable is True


# ---------------------------------------------------------------------------
# 契约层：唯一口径
# ---------------------------------------------------------------------------


def test_canonical_contract_is_green(contract, ddl) -> None:
    assert sc.check_contract(contract, ddl) == []


def test_metric_values_are_unique(contract, ddl) -> None:
    metrics = {metric["name"]: metric for metric in contract["metrics"]}
    assert {name: metric["expected_value"] for name, metric in metrics.items()} == {
        "GMV": 279159.5,
        "AOV": 2427.47,
        "订单量": 115,
        "销量": 622,
        "平均件单价": 448.81,
        "平均每单件数": 5.41,
    }

    # 三个容易混淆的算术关系，必须在同一份数据上同时成立。
    assert round(279159.5 / 115, 2) == 2427.47  # AOV
    assert round(279159.5 / 622, 2) == 448.81  # 平均件单价
    assert round(622 / 115, 2) == 5.41  # 平均每单件数

    rejected = {
        rejected["value"]: rejected["meaning"]
        for rejected in metrics["AOV"]["rejected_values"]
    }
    assert rejected[448.81].startswith("平均件单价")
    assert rejected[5.41].startswith("平均每单件数")


def test_metric_formulas_evaluate_to_contract_values(contract, ddl) -> None:
    for metric in contract["metrics"]:
        formula = metric["formula"].replace("/", "* 1.0 /")
        value = sc._scalar(ddl.conn, f"SELECT {formula} FROM fact_order")
        assert abs(float(value) - float(metric["expected_value"])) <= 0.005, metric[
            "name"
        ]


# ---------------------------------------------------------------------------
# 负向夹具：旧系统必须红
# ---------------------------------------------------------------------------


def test_legacy_fixture_is_red_with_expected_codes(contract, ddl) -> None:
    issues = sc.check_legacy(contract, ddl)
    codes = codes_of(issues)
    assert codes == set(sc.EXPECTED_LEGACY_CODES)
    assert "LEGACY_TABLE_DRIFT" not in codes
    assert "LEGACY_COLUMN_DRIFT" not in codes

    messages = {issue.code: issue.message for issue in issues}
    assert "order_quantity" in messages["AOV_COLUMN_MISMATCH"]
    assert "平均单价" in messages["AOV_ALIAS_MISMATCH"]
    assert "2427.47" in messages["AOV_THREE_WAY"]
    assert "city" in messages["CITY_GRAIN_UNDECLARED"]
    assert "5.41" in messages["ORDER_COUNT_UNDEFINED"]


def test_feature_list_drift(contract, ddl) -> None:
    issues = sc.check_feature_list(contract, ddl)
    assert codes_of(issues) == {
        "FEATURE_LIST_TABLE_DRIFT",
        "FEATURE_LIST_ROW_DRIFT",
    }
    messages = " ".join(issue.message for issue in issues)
    assert "6" in messages and "5" in messages
    assert "138" in messages and "115" in messages


def test_fixed_legacy_fixture_can_go_green(contract, ddl, tmp_path) -> None:
    """负向夹具不是「永远红」：按契约修复后，同一个检查器必须变绿。"""
    meta = yaml.safe_load(sc.LEGACY_META_PATH.read_text(encoding="utf-8"))
    tables = {table["name"]: table for table in meta["tables"]}
    metrics = {metric["name"]: metric for metric in meta["metrics"]}

    aov = metrics["AOV"]
    aov["description"] = "平均每单的成交金额；= 成交总额 / 订单量。"
    aov["formula"] = (
        "SUM(fact_order.order_amount) / COUNT(DISTINCT fact_order.order_id)"
    )
    aov["relevant_columns"] = [
        "fact_order.order_amount",
        "fact_order.order_id",
    ]
    aov["alias"] = ["平均订单金额", "客单价"]

    tables["dim_region"]["grain"] = "一行一个 region_id（省级行政区；无 city 列）"

    meta["metrics"].append(
        {
            "name": "订单量",
            "description": "订单数；一行一单，COUNT(DISTINCT order_id)。",
            "formula": "COUNT(DISTINCT fact_order.order_id)",
            "relevant_columns": ["fact_order.order_id"],
            "alias": ["订单量", "订单数"],
        }
    )

    fixed_aliases = {
        "customer_id": ["客户外键", "关联客户ID"],
        "product_id": ["商品外键", "关联商品ID"],
        "date_id": ["日期外键", "关联日期ID"],
        "region_id": ["地区外键", "关联地区ID"],
    }
    for column in tables["fact_order"]["columns"]:
        if column["name"] in fixed_aliases:
            column["alias"] = fixed_aliases[column["name"]]

    meta_path = tmp_path / "meta_config.yaml"
    meta_path.write_text(
        yaml.safe_dump(meta, allow_unicode=True, sort_keys=False),
        encoding="utf-8",
    )
    feature_path = tmp_path / "feature-list.md"
    feature_path.write_text(
        "提供 5 张 MySQL 数仓表共 115 行订单数据。\n",
        encoding="utf-8",
    )

    assert (
        sc.check_legacy(
            contract,
            ddl,
            meta_path=meta_path,
            feature_path=feature_path,
        )
        == []
    )


# ---------------------------------------------------------------------------
# 改坏契约：必须被拒绝
# ---------------------------------------------------------------------------


def test_wrong_aov_formula_is_rejected(contract, ddl) -> None:
    bad = copy.deepcopy(contract)
    aov = next(metric for metric in bad["metrics"] if metric["name"] == "AOV")
    aov["formula"] = "SUM(fact_order.order_amount) / SUM(fact_order.order_quantity)"
    aov["relevant_columns"] = [
        "fact_order.order_amount",
        "fact_order.order_quantity",
    ]

    codes = codes_of(sc.check_contract(bad, ddl))
    assert "AOV_FORMULA" in codes
    assert "AOV_COLUMN_MISMATCH" in codes
    assert "METRIC_VALUE_MISMATCH" in codes


def test_aov_relevant_column_mismatch_is_rejected(contract, ddl) -> None:
    bad = copy.deepcopy(contract)
    aov = next(metric for metric in bad["metrics"] if metric["name"] == "AOV")
    aov["relevant_columns"] = ["fact_order.order_quantity"]

    codes = codes_of(sc.check_contract(bad, ddl))
    assert "AOV_COLUMN_MISMATCH" in codes
    assert "METRIC_RELEVANT_COLUMNS" in codes


def test_aov_alias_collision_is_rejected(contract, ddl) -> None:
    bad = copy.deepcopy(contract)
    aov = next(metric for metric in bad["metrics"] if metric["name"] == "AOV")
    aov["aliases"].append("平均单价")

    codes = codes_of(sc.check_contract(bad, ddl))
    assert "AOV_ALIAS_MISMATCH" in codes
    assert "ALIAS_COLLISION" in codes


def test_unknown_city_column_is_rejected(contract, ddl) -> None:
    bad = copy.deepcopy(contract)
    region = next(table for table in bad["tables"] if table["name"] == "dim_region")
    region["columns"].append(
        {
            "name": "city",
            "type": "VARCHAR(50)",
            "role": "dimension",
            "nullable": True,
            "description": "城市。",
            "aliases": ["城市"],
            "value_domain": {"kind": "open"},
        }
    )

    codes = codes_of(sc.check_contract(bad, ddl))
    assert "COLUMN_UNKNOWN" in codes


def test_value_domain_drift_is_rejected(contract, ddl) -> None:
    bad = copy.deepcopy(contract)
    brand = next(
        column
        for table in bad["tables"]
        if table["name"] == "dim_product"
        for column in table["columns"]
        if column["name"] == "brand"
    )
    brand["value_domain"]["values"].remove("Instant Pot")

    assert "VALUE_DOMAIN_ENUM" in codes_of(sc.check_contract(bad, ddl))


def test_alias_collision_is_rejected(contract, ddl) -> None:
    bad = copy.deepcopy(contract)
    fact = next(table for table in bad["tables"] if table["name"] == "fact_order")
    customer_id = next(
        column for column in fact["columns"] if column["name"] == "customer_id"
    )
    customer_id["aliases"].append("客户ID")

    assert "ALIAS_COLLISION" in codes_of(sc.check_contract(bad, ddl))


def test_order_count_metric_is_required(contract, ddl) -> None:
    bad = copy.deepcopy(contract)
    bad["metrics"] = [
        metric for metric in bad["metrics"] if metric["name"] != "订单量"
    ]

    assert "ORDER_COUNT_UNDEFINED" in codes_of(sc.check_contract(bad, ddl))


def test_order_count_requires_distinct(contract, ddl) -> None:
    bad = copy.deepcopy(contract)
    order_count = next(metric for metric in bad["metrics"] if metric["name"] == "订单量")
    order_count["formula"] = "COUNT(fact_order.order_id)"

    assert "ORDER_COUNT_UNDEFINED" in codes_of(sc.check_contract(bad, ddl))


def test_metric_formula_semantics_are_enforced(contract, ddl) -> None:
    bad = copy.deepcopy(contract)
    gmv = next(metric for metric in bad["metrics"] if metric["name"] == "GMV")
    gmv["formula"] = "SUM(fact_order.order_quantity)"
    gmv["relevant_columns"] = ["fact_order.order_quantity"]
    gmv["expected_value"] = 622

    assert "METRIC_FORMULA_SEMANTICS" in codes_of(sc.check_contract(bad, ddl))


def test_metric_semantics_are_enforced(contract, ddl) -> None:
    bad = copy.deepcopy(contract)
    order_count = next(metric for metric in bad["metrics"] if metric["name"] == "订单量")
    order_count["aggregation"] = "additive"
    assert "METRIC_AGGREGATION_SEMANTICS" in codes_of(sc.check_contract(bad, ddl))

    bad = copy.deepcopy(contract)
    order_count = next(metric for metric in bad["metrics"] if metric["name"] == "订单量")
    order_count["grain"] = "件"
    assert "METRIC_GRAIN_SEMANTICS" in codes_of(sc.check_contract(bad, ddl))

    bad = copy.deepcopy(contract)
    aov = next(metric for metric in bad["metrics"] if metric["name"] == "AOV")
    aov["aggregation"] = "additive"
    assert "METRIC_AGGREGATION_SEMANTICS" in codes_of(sc.check_contract(bad, ddl))


def test_required_metrics_are_present(contract, ddl) -> None:
    bad = copy.deepcopy(contract)
    bad["metrics"] = [
        metric for metric in bad["metrics"] if metric["name"] != "GMV"
    ]
    assert "METRIC_MISSING" in codes_of(sc.check_contract(bad, ddl))


def test_measure_aggregation_is_enforced(contract, ddl) -> None:
    bad = copy.deepcopy(contract)
    fact = next(table for table in bad["tables"] if table["name"] == "fact_order")
    quantity = next(
        measure for measure in fact["measures"] if measure["column"] == "order_quantity"
    )
    quantity["aggregation"] = "count_distinct"
    assert "ORDER_QUANTITY_AGGREGATION" in codes_of(sc.check_contract(bad, ddl))

    bad = copy.deepcopy(contract)
    fact = next(table for table in bad["tables"] if table["name"] == "fact_order")
    amount = next(
        measure for measure in fact["measures"] if measure["column"] == "order_amount"
    )
    amount["aggregation"] = "non_additive"
    assert "ORDER_AMOUNT_AGGREGATION" in codes_of(sc.check_contract(bad, ddl))


def test_fk_roles_and_declarations_are_required(contract, ddl) -> None:
    bad = copy.deepcopy(contract)
    fact = next(table for table in bad["tables"] if table["name"] == "fact_order")
    for column in fact["columns"]:
        if column["name"].endswith("_id") and column["name"] != "order_id":
            column["role"] = "dimension"
            column["value_domain"] = {"kind": "open"}
    fact["foreign_keys"] = []

    codes = codes_of(sc.check_contract(bad, ddl))
    assert "FK_ROLE_MISSING" in codes
    assert "VALUE_DOMAIN_OPEN_NOT_ALLOWED" in codes

    bad = copy.deepcopy(contract)
    fact = next(table for table in bad["tables"] if table["name"] == "fact_order")
    fact["foreign_keys"] = []
    assert "FK_DECLARATION_MISSING" in codes_of(sc.check_contract(bad, ddl))


def test_open_value_domain_is_rejected(contract, ddl) -> None:
    bad = copy.deepcopy(contract)
    fact = next(table for table in bad["tables"] if table["name"] == "fact_order")
    order_id = next(column for column in fact["columns"] if column["name"] == "order_id")
    order_id["value_domain"] = {"kind": "open"}

    assert "VALUE_DOMAIN_OPEN_NOT_ALLOWED" in codes_of(sc.check_contract(bad, ddl))


def test_key_value_domain_kind_is_enforced(contract, ddl) -> None:
    bad = copy.deepcopy(contract)
    fact = next(table for table in bad["tables"] if table["name"] == "fact_order")
    order_id = next(column for column in fact["columns"] if column["name"] == "order_id")
    order_id["value_domain"] = {
        "kind": "enum",
        "values": sc._distinct(ddl.conn, "fact_order", "order_id"),
    }

    assert "VALUE_DOMAIN_KIND_SEMANTICS" in codes_of(sc.check_contract(bad, ddl))


@pytest.mark.parametrize(
    "pattern",
    [
        ".*",
        "^.*$",
        r"^ORD\d{11}$|^.*$",
        r"^(?:.*)$",
        r"^[\s\S]*$",
    ],
)
def test_weak_id_pattern_is_rejected(contract, ddl, pattern: str) -> None:
    bad = copy.deepcopy(contract)
    fact = next(table for table in bad["tables"] if table["name"] == "fact_order")
    order_id = next(column for column in fact["columns"] if column["name"] == "order_id")
    order_id["value_domain"]["pattern"] = pattern

    codes = codes_of(sc.check_contract(bad, ddl))
    assert "VALUE_DOMAIN_ID_PATTERN_WEAK" in codes
    assert "VALUE_DOMAIN_ID_PATTERN_SEMANTICS" in codes


def test_id_pattern_semantics_are_enforced(contract, ddl) -> None:
    bad = copy.deepcopy(contract)
    fact = next(table for table in bad["tables"] if table["name"] == "fact_order")
    order_id = next(column for column in fact["columns"] if column["name"] == "order_id")
    order_id["value_domain"]["pattern"] = r"^ORD\d{12}$"

    assert "VALUE_DOMAIN_ID_PATTERN_SEMANTICS" in codes_of(
        sc.check_contract(bad, ddl)
    )


def test_fk_value_domain_kind_is_enforced(contract, ddl) -> None:
    bad = copy.deepcopy(contract)
    fact = next(table for table in bad["tables"] if table["name"] == "fact_order")
    customer_id = next(
        column for column in fact["columns"] if column["name"] == "customer_id"
    )
    customer_id["value_domain"] = {"kind": "id", "pattern": r"^C\d{3}$"}

    assert "VALUE_DOMAIN_FK_KIND" in codes_of(sc.check_contract(bad, ddl))


def test_duplicate_metric_is_rejected(contract, ddl) -> None:
    bad = copy.deepcopy(contract)
    bad["metrics"].append(copy.deepcopy(bad["metrics"][0]))

    assert "DUPLICATE_METRIC" in codes_of(sc.check_contract(bad, ddl))


def test_alias_shadowing_column_is_rejected(contract, ddl) -> None:
    bad = copy.deepcopy(contract)
    aov = next(metric for metric in bad["metrics"] if metric["name"] == "AOV")
    aov["aliases"].append("order_id")

    assert "ALIAS_SHADOWS_COLUMN" in codes_of(sc.check_contract(bad, ddl))


def test_value_domain_range_and_id_are_enforced(contract, ddl) -> None:
    bad = copy.deepcopy(contract)
    fact = next(table for table in bad["tables"] if table["name"] == "fact_order")
    quantity = next(
        column for column in fact["columns"] if column["name"] == "order_quantity"
    )
    quantity["value_domain"]["max"] = 29
    assert "VALUE_DOMAIN_RANGE" in codes_of(sc.check_contract(bad, ddl))

    bad = copy.deepcopy(contract)
    fact = next(table for table in bad["tables"] if table["name"] == "fact_order")
    order_id = next(column for column in fact["columns"] if column["name"] == "order_id")
    order_id["value_domain"]["pattern"] = r"^ORD\d{10}$"
    assert "VALUE_DOMAIN_ID" in codes_of(sc.check_contract(bad, ddl))


def test_partial_feature_list_drift_is_detected(contract, ddl, tmp_path) -> None:
    path = tmp_path / "feature-list.md"
    path.write_text(
        "提供 5 张 MySQL 数仓表共 115 行订单数据。\n"
        "旧版残留：6 张表共 138 行订单数据。\n",
        encoding="utf-8",
    )
    codes = codes_of(sc.check_feature_list(contract, ddl, path=path))
    assert codes == {"FEATURE_LIST_TABLE_DRIFT", "FEATURE_LIST_ROW_DRIFT"}


def test_foreign_key_orphan_is_rejected(contract) -> None:
    local = sc.load_ddl()
    try:
        local.conn.execute(
            "INSERT INTO fact_order "
            "(order_id, customer_id, product_id, date_id, region_id, "
            "order_quantity, order_amount) "
            "VALUES ('ORD20250101999', 'C999', 'P001', 20250101, 'R001', 1, 100.0)"
        )
        local.conn.commit()
        assert "FK_ORPHAN_ROWS" in codes_of(sc.check_contract(contract, local))
    finally:
        local.close()


# ---------------------------------------------------------------------------
# 生成物 + CLI
# ---------------------------------------------------------------------------


def test_generated_doc_is_fresh(contract, ddl) -> None:
    assert sc.DOC_PATH.exists()
    expected = sc.render_doc(contract, ddl)
    assert sc.DOC_PATH.read_text(encoding="utf-8") == expected
    assert str(ROOT) not in expected
    assert "/home/" not in expected
    assert "A020-" not in expected
    assert "2,427.47" in expected
    assert "448.81" in expected
    assert "5.41" in expected


def test_cli_check_is_green() -> None:
    assert sc.main(["--check"]) == 0


def test_cli_legacy_is_red() -> None:
    assert sc.main(["--check-legacy"]) == 1
    assert sc.main(["--expect-legacy-red"]) == 0


def test_cli_doc_check_is_green() -> None:
    assert sc.main(["--check-doc"]) == 0
