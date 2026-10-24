"""S2 · 元知识构建的配置。

所有目标名都带 `_rebuild` 后缀，默认值与旧系统（meta / value_index /
column_info_collection / metric_info_collection）隔离。这样 S2 可以反复
`--fresh`，而不会动到旧系统正在使用的数据。

本地教学环境的默认值来自 `../shopkeeper-agent/docker/docker-compose.yaml`；
部署到别处时用环境变量覆盖即可。
"""

from __future__ import annotations

import os
from dataclasses import dataclass


def _env(name: str, default: str) -> str:
    return os.getenv(name, default)


def _env_int(name: str, default: int) -> int:
    raw = os.getenv(name)
    if raw is None or raw == "":
        return default
    try:
        return int(raw)
    except ValueError as exc:
        raise ValueError(f"环境变量 {name} 必须是整数，当前为 {raw!r}") from exc


@dataclass(frozen=True)
class BuildConfig:
    """一次元知识构建需要知道的所有外部目标。"""

    # Meta MySQL
    mysql_host: str
    mysql_port: int
    mysql_user: str
    mysql_password: str
    meta_db: str
    mysql_root_user: str
    mysql_root_password: str

    # Elasticsearch
    es_url: str
    es_index: str

    # Qdrant
    qdrant_url: str
    column_collection: str
    metric_collection: str
    vector_size: int

    # 构建策略
    embedding_provider: str
    max_value_docs: int
    request_timeout: float

    @classmethod
    def from_env(cls) -> BuildConfig:
        return cls(
            mysql_host=_env("REBUILD_MYSQL_HOST", "127.0.0.1"),
            mysql_port=_env_int("REBUILD_MYSQL_PORT", 3306),
            mysql_user=_env("REBUILD_MYSQL_USER", "didilili"),
            mysql_password=_env("REBUILD_MYSQL_PASSWORD", "dili123"),
            meta_db=_env("REBUILD_META_DB", "meta_rebuild"),
            mysql_root_user=_env("REBUILD_MYSQL_ROOT_USER", "root"),
            mysql_root_password=_env("REBUILD_MYSQL_ROOT_PASSWORD", "dili123"),
            es_url=_env("REBUILD_ES_URL", "http://127.0.0.1:9200"),
            es_index=_env("REBUILD_ES_INDEX", "value_index_rebuild"),
            qdrant_url=_env("REBUILD_QDRANT_URL", "http://127.0.0.1:6333"),
            column_collection=_env("REBUILD_COLUMN_COLLECTION", "column_info_rebuild"),
            metric_collection=_env("REBUILD_METRIC_COLLECTION", "metric_info_rebuild"),
            vector_size=_env_int("REBUILD_VECTOR_SIZE", 256),
            embedding_provider=_env("REBUILD_EMBEDDING_PROVIDER", "hash-ngram-v1"),
            max_value_docs=_env_int("REBUILD_MAX_VALUE_DOCS", 1000),
            request_timeout=float(_env("REBUILD_REQUEST_TIMEOUT", "15")),
        )

    def targets(self) -> dict[str, object]:
        """写进 manifest，`--verify` 用它证明检查的是同一组目标。"""
        return {
            "meta_db": self.meta_db,
            "es_index": self.es_index,
            "column_collection": self.column_collection,
            "metric_collection": self.metric_collection,
            "vector_size": self.vector_size,
        }

    def mysql_dsn(self, *, database: str | None = None, root: bool = False) -> dict:
        """pymysql.connect 的参数；不打印密码。"""
        return {
            "host": self.mysql_host,
            "port": self.mysql_port,
            "user": self.mysql_root_user if root else self.mysql_user,
            "password": (self.mysql_root_password if root else self.mysql_password),
            "database": database,
            "charset": "utf8mb4",
            "connect_timeout": int(self.request_timeout),
            "autocommit": True,
        }

    def safe_repr(self) -> str:
        """CLI 打印用：只暴露目标名，不暴露密码。"""
        return (
            f"meta={self.meta_db} es={self.es_index} "
            f"qdrant={self.column_collection}/{self.metric_collection} "
            f"embedding={self.embedding_provider}:{self.vector_size}"
        )
