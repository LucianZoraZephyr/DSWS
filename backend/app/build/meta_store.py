"""S2 · Meta MySQL 仓储。

只负责「建库/建表/写实体/读 manifest」，不负责决定构建流程。
目标库默认 `meta_rebuild`，与旧系统的 `meta` 完全隔离。
"""

from __future__ import annotations

import json
import re
from typing import Any

import pymysql
from pymysql.cursors import DictCursor

from app.build.config import BuildConfig
from app.build.errors import BuildError
from app.build.plan import BuildPlan

_IDENT_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")

# manifest 自身的格式版本；与 contract_version 分开，方便以后迁移表结构。
MANIFEST_VERSION = 1

SCHEMA_STATEMENTS = (
    """
    CREATE TABLE IF NOT EXISTS table_info (
        id VARCHAR(128) PRIMARY KEY,
        name VARCHAR(128) NOT NULL,
        role VARCHAR(32) NOT NULL,
        description TEXT,
        grain TEXT
    ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4
    """,
    """
    CREATE TABLE IF NOT EXISTS column_info (
        id VARCHAR(256) PRIMARY KEY,
        name VARCHAR(128) NOT NULL,
        type VARCHAR(64) NOT NULL,
        role VARCHAR(32) NOT NULL,
        examples JSON,
        description TEXT,
        alias JSON,
        table_id VARCHAR(128) NOT NULL,
        value_domain JSON,
        sync_values TINYINT(1) NOT NULL DEFAULT 0,
        KEY idx_column_table (table_id)
    ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4
    """,
    """
    CREATE TABLE IF NOT EXISTS metric_info (
        id VARCHAR(128) PRIMARY KEY,
        name VARCHAR(128) NOT NULL,
        label VARCHAR(128) NOT NULL,
        description TEXT,
        formula TEXT,
        relevant_columns JSON,
        alias JSON,
        aggregation VARCHAR(32),
        expected_value DOUBLE,
        grain VARCHAR(32),
        rejected_values JSON
    ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4
    """,
    """
    CREATE TABLE IF NOT EXISTS column_metric (
        column_id VARCHAR(256) NOT NULL,
        metric_id VARCHAR(128) NOT NULL,
        PRIMARY KEY (column_id, metric_id)
    ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4
    """,
    """
    CREATE TABLE IF NOT EXISTS build_manifest (
        seq BIGINT AUTO_INCREMENT PRIMARY KEY,
        build_id VARCHAR(64) NOT NULL UNIQUE,
        manifest_version INT NOT NULL,
        built_at VARCHAR(32) NOT NULL,
        config_hash CHAR(64) NOT NULL,
        contract_version INT NOT NULL,
        embedding_provider VARCHAR(64) NOT NULL,
        embedding_dim INT NOT NULL,
        meta_tables INT NOT NULL,
        meta_columns INT NOT NULL,
        meta_metrics INT NOT NULL,
        meta_column_metrics INT NOT NULL,
        es_values INT NOT NULL,
        qdrant_columns INT NOT NULL,
        qdrant_metrics INT NOT NULL,
        es_analyzer VARCHAR(32),
        targets_json TEXT NOT NULL,
        status VARCHAR(16) NOT NULL,
        error TEXT
    ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4
    """,
)

COUNT_KEYS = (
    "meta_tables",
    "meta_columns",
    "meta_metrics",
    "meta_column_metrics",
    "es_values",
    "qdrant_columns",
    "qdrant_metrics",
)


def _safe_ident(name: str) -> str:
    if not _IDENT_RE.fullmatch(name):
        raise BuildError(f"非法数据库标识符：{name!r}")
    return name


def _json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"), default=str)


def _is_missing_schema_error(exc: pymysql.MySQLError) -> bool:
    """1049 = 数据库不存在；1146 = 表不存在。"""
    code = exc.args[0] if exc.args else None
    return code in (1049, 1146)


class MetaStore:
    def __init__(self, config: BuildConfig):
        self.config = config

    def _connect(
        self, *, database: str | None = None, root: bool = False
    ) -> pymysql.connections.Connection:
        params = self.config.mysql_dsn(database=database, root=root)
        params["cursorclass"] = DictCursor
        return pymysql.connect(**params)

    # ------------------------------------------------------------------
    # 库/表生命周期
    # ------------------------------------------------------------------

    def database_exists(self) -> bool:
        try:
            conn = self._connect()
        except pymysql.MySQLError as exc:
            raise BuildError(f"无法连接 Meta MySQL：{exc}") from exc
        try:
            with conn.cursor() as cur:
                cur.execute("SHOW DATABASES LIKE %s", (self.config.meta_db,))
                return cur.fetchone() is not None
        finally:
            conn.close()

    def ensure_database(self) -> None:
        """幂等建库；需要 root（或等价权限）来 CREATE/GRANT。

        库已经存在的时候 root 是多余的——容器化之后这是常态：init 容器
        建一次库，之后每次 `docker compose up` 都不该再要 root 密码。
        所以拿不到 root（密码没配、或者连不上）时先看库在不在：
        在就返回，不在才报错。

        注意 `mysql_dsn(root=True)` 缺密码时抛的是 `BuildError` 而不是
        `pymysql.MySQLError`——不接住它，这条降级路径就永远走不到。

        同理，`database_exists()` 里也会抛 `BuildError`（应用密码缺失）。
        它同样不能从 except 块里穿出去：那会把下面那条合并消息顶掉，
        用户只看到"缺 REBUILD_MYSQL_PASSWORD"，看不到 root 那条，
        于是以为只差一个变量——其实两个都没配。
        """
        db = _safe_ident(self.config.meta_db)
        user = self.config.mysql_user.replace("'", "''")
        try:
            conn = self._connect(root=True)
        except (pymysql.MySQLError, BuildError) as root_exc:
            try:
                exists = self.database_exists()
            except (pymysql.MySQLError, BuildError) as probe_exc:
                raise BuildError(
                    "无法以 root 创建 rebuild 元数据库，且无法确认库是否存在。\n"
                    "请设置 REBUILD_MYSQL_ROOT_USER / REBUILD_MYSQL_ROOT_PASSWORD"
                    "（建库、开通只读账号需要 root 权限），"
                    "以及 REBUILD_MYSQL_PASSWORD（Meta MySQL 的应用账号）。\n"
                    f"root 连接错误：{root_exc}\n"
                    f"探测库是否存在时出错：{probe_exc}"
                ) from root_exc
            if exists:
                return
            raise BuildError(
                "无法以 root 创建 rebuild 元数据库，且库不存在。\n"
                "请设置 REBUILD_MYSQL_ROOT_USER / REBUILD_MYSQL_ROOT_PASSWORD，"
                "或手工创建库并授权。\n"
                f"root 连接错误：{root_exc}"
            ) from root_exc
        try:
            with conn.cursor() as cur:
                cur.execute(
                    f"CREATE DATABASE IF NOT EXISTS `{db}` "
                    "DEFAULT CHARACTER SET utf8mb4 COLLATE utf8mb4_general_ci"
                )
                cur.execute(f"GRANT ALL PRIVILEGES ON `{db}`.* TO '{user}'@'%'")
                cur.execute("FLUSH PRIVILEGES")
        finally:
            conn.close()

    def drop_database(self) -> None:
        """只删 rebuild 自己的库；旧系统 `meta` 不在此列。"""
        db = _safe_ident(self.config.meta_db)
        conn = self._connect(root=True)
        try:
            with conn.cursor() as cur:
                cur.execute(f"DROP DATABASE IF EXISTS `{db}`")
        finally:
            conn.close()

    def create_schema(self) -> None:
        conn = self._connect(database=self.config.meta_db)
        try:
            with conn.cursor() as cur:
                for statement in SCHEMA_STATEMENTS:
                    cur.execute(statement)
        finally:
            conn.close()

    # ------------------------------------------------------------------
    # 写实体
    # ------------------------------------------------------------------

    def reset_data(self) -> None:
        conn = self._connect(database=self.config.meta_db)
        try:
            with conn.cursor() as cur:
                cur.execute("DELETE FROM column_metric")
                cur.execute("DELETE FROM metric_info")
                cur.execute("DELETE FROM column_info")
                cur.execute("DELETE FROM table_info")
        finally:
            conn.close()

    def write_plan(self, plan: BuildPlan) -> None:
        conn = self._connect(database=self.config.meta_db)
        try:
            conn.begin()
            with conn.cursor() as cur:
                for table in plan.tables:
                    cur.execute(
                        "INSERT INTO table_info "
                        "(id, name, role, description, grain) "
                        "VALUES (%s, %s, %s, %s, %s)",
                        (
                            table.id,
                            table.name,
                            table.role,
                            table.description,
                            table.grain,
                        ),
                    )
                for column in plan.columns:
                    cur.execute(
                        "INSERT INTO column_info "
                        "(id, name, type, role, examples, description, alias, "
                        " table_id, value_domain, sync_values) "
                        "VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s)",
                        (
                            column.id,
                            column.name,
                            column.type,
                            column.role,
                            _json(column.examples),
                            column.description,
                            _json(column.aliases),
                            column.table_id,
                            _json(column.value_domain),
                            1 if column.sync_values else 0,
                        ),
                    )
                for metric in plan.metrics:
                    cur.execute(
                        "INSERT INTO metric_info "
                        "(id, name, label, description, formula, relevant_columns, "
                        " alias, aggregation, expected_value, grain, rejected_values) "
                        "VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)",
                        (
                            metric.id,
                            metric.name,
                            metric.label,
                            metric.description,
                            metric.formula,
                            _json(metric.relevant_columns),
                            _json(metric.aliases),
                            metric.aggregation,
                            metric.expected_value,
                            metric.grain,
                            _json(metric.rejected_values),
                        ),
                    )
                for column_id, metric_id in plan.column_metric_pairs():
                    cur.execute(
                        "INSERT INTO column_metric (column_id, metric_id) "
                        "VALUES (%s, %s)",
                        (column_id, metric_id),
                    )
            conn.commit()
        except Exception:
            conn.rollback()
            raise
        finally:
            conn.close()

    def write_manifest(self, manifest: dict[str, Any]) -> None:
        conn = self._connect(database=self.config.meta_db)
        try:
            with conn.cursor() as cur:
                cur.execute(
                    "INSERT INTO build_manifest "
                    "(build_id, manifest_version, built_at, config_hash, "
                    " contract_version, embedding_provider, embedding_dim, "
                    " meta_tables, meta_columns, meta_metrics, meta_column_metrics, "
                    " es_values, qdrant_columns, qdrant_metrics, es_analyzer, "
                    " targets_json, status, error) "
                    "VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, "
                    "        %s, %s, %s, %s, %s, %s)",
                    (
                        manifest["build_id"],
                        manifest["manifest_version"],
                        manifest["built_at"],
                        manifest["config_hash"],
                        manifest["contract_version"],
                        manifest["embedding_provider"],
                        manifest["embedding_dim"],
                        manifest["meta_tables"],
                        manifest["meta_columns"],
                        manifest["meta_metrics"],
                        manifest["meta_column_metrics"],
                        manifest["es_values"],
                        manifest["qdrant_columns"],
                        manifest["qdrant_metrics"],
                        manifest.get("es_analyzer"),
                        _json(manifest["targets"]),
                        manifest["status"],
                        manifest.get("error"),
                    ),
                )
        finally:
            conn.close()

    # ------------------------------------------------------------------
    # 读 manifest / 计数
    # ------------------------------------------------------------------

    def read_manifest(self) -> dict[str, Any] | None:
        try:
            conn = self._connect(database=self.config.meta_db)
        except pymysql.MySQLError as exc:
            if _is_missing_schema_error(exc):
                return None
            raise BuildError(f"读取 manifest 失败：{exc}") from exc
        try:
            with conn.cursor() as cur:
                cur.execute("SELECT * FROM build_manifest ORDER BY seq DESC LIMIT 1")
                row = cur.fetchone()
        except pymysql.MySQLError as exc:
            if _is_missing_schema_error(exc):
                return None
            raise BuildError(f"读取 manifest 失败：{exc}") from exc
        finally:
            conn.close()
        if row is None:
            return None
        row = dict(row)
        row["targets"] = json.loads(row.pop("targets_json"))
        return row

    def counts(self) -> dict[str, int] | None:
        """返回 4 个 Meta 计数 + 3 个外部计数占位；表不存在返回 None。"""
        try:
            conn = self._connect(database=self.config.meta_db)
        except pymysql.MySQLError as exc:
            if _is_missing_schema_error(exc):
                return None
            raise BuildError(f"读取 Meta 计数失败：{exc}") from exc
        try:
            with conn.cursor() as cur:
                result: dict[str, int] = {}
                for key, table in (
                    ("meta_tables", "table_info"),
                    ("meta_columns", "column_info"),
                    ("meta_metrics", "metric_info"),
                    ("meta_column_metrics", "column_metric"),
                ):
                    cur.execute(f"SELECT COUNT(*) AS n FROM {table}")
                    result[key] = int(cur.fetchone()["n"])
                return result
        except pymysql.MySQLError as exc:
            if _is_missing_schema_error(exc):
                return None
            raise BuildError(f"读取 Meta 计数失败：{exc}") from exc
        finally:
            conn.close()

    def get_row(self, table: str, row_id: str) -> dict[str, Any] | None:
        table = _safe_ident(table)
        conn = self._connect(database=self.config.meta_db)
        try:
            with conn.cursor() as cur:
                cur.execute(f"SELECT * FROM {table} WHERE id = %s", (row_id,))
                row = cur.fetchone()
                return dict(row) if row else None
        finally:
            conn.close()
