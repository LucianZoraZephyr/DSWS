"""S2 · 元知识构建的配置。

所有目标名都带 `_rebuild` 后缀，默认值与旧系统（meta / value_index /
column_info_collection / metric_info_collection）隔离。这样 S2 可以反复
`--fresh`，而不会动到旧系统正在使用的数据。

本地教学环境的默认值来自 `../shopkeeper-agent/docker/docker-compose.yaml`；
部署到别处时用环境变量覆盖即可。

**凭据约定**：密码**没有**默认值，也不写死在任何地方——和
`app/scripts/security.py` 一样，密码只由环境变量提供。容器化之后
`.env` 生成的是随机密码，任何兜底值都只会换来一条误导性的
`Access denied`。缺密码时 `mysql_dsn()` 直接抛错，并点名是哪个环境变量。
"""

from __future__ import annotations

import os
from dataclasses import dataclass

from app.build.errors import BuildError


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
            mysql_password=_env("REBUILD_MYSQL_PASSWORD", ""),
            meta_db=_env("REBUILD_META_DB", "meta_rebuild"),
            mysql_root_user=_env("REBUILD_MYSQL_ROOT_USER", "root"),
            mysql_root_password=_env("REBUILD_MYSQL_ROOT_PASSWORD", ""),
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
        """pymysql.connect 的参数；不打印密码。

        密码缺失时抛 `BuildError` 并点名环境变量——比"连上去再说"早一步
        失败，运维不用去翻 MySQL 的 access log 猜是哪个变量没传。
        """
        if root:
            user, password = self.mysql_root_user, self.mysql_root_password
            missing_env = "REBUILD_MYSQL_ROOT_PASSWORD"
            missing_hint = "创建元数据库、开通只读账号需要 root 权限"
        else:
            user, password = self.mysql_user, self.mysql_password
            missing_env = "REBUILD_MYSQL_PASSWORD"
            missing_hint = "Meta MySQL 的密码不在仓库里，必须由部署环境提供"
        if not password:
            raise BuildError(
                f"缺少环境变量 {missing_env}：{missing_hint}。"
                "容器内请用 `docker compose run --rm --no-deps init ...`，"
                "宿主机请先 `source scripts/export-docker-env.sh`。"
            )
        return {
            "host": self.mysql_host,
            "port": self.mysql_port,
            "user": user,
            "password": password,
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
