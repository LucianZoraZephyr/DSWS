"""S3 · 只读检索客户端。

这里不碰 S2 的写入语义，只做三件事：

    - Elasticsearch `_msearch`：一次 HTTP 请求查完所有关键词
    - Qdrant `points/search/batch`：一次请求查完所有关键词向量
    - Meta MySQL：一次连接、批量读取 4 张元数据表 + 数据库版本

所有客户端都记录「往返次数」到 `roundtrips`，CLI 和测试据此断言没有
把批量请求偷偷拆成 N 次单条请求。
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from typing import Any

import httpx
import pymysql
from pymysql.cursors import DictCursor

from app.build.config import BuildConfig
from app.build.embedding import EmbeddingProvider, make_embedding_provider
from app.recall.catalog import Catalog, default_catalog
from app.recall.errors import RecallError


def _decode_json(value: Any, *, field_name: str, row_id: str) -> Any:
    if value is None:
        return None
    if isinstance(value, (list, dict)):
        return value
    try:
        return json.loads(value)
    except (TypeError, ValueError) as exc:
        raise RecallError(
            f"Meta 字段 {field_name} 不是合法 JSON（id={row_id}）：{value!r}"
        ) from exc


@dataclass
class MetaSnapshot:
    """一次批量读取的 Meta 快照；S3 不再按候选逐个查库。"""

    tables: dict[str, dict[str, Any]]
    columns: dict[str, dict[str, Any]]
    metrics: dict[str, dict[str, Any]]
    column_metrics: dict[str, list[str]]
    version: str | None
    roundtrips: int = 0


class MetaReader:
    """Meta MySQL 的只读批量访问器。"""

    def __init__(self, config: BuildConfig):
        self.config = config
        self.roundtrips = 0

    def _connect(self) -> pymysql.connections.Connection:
        params = self.config.mysql_dsn(database=self.config.meta_db)
        params["cursorclass"] = DictCursor
        try:
            return pymysql.connect(**params)
        except pymysql.MySQLError as exc:
            raise RecallError(
                f"连接 Meta MySQL 失败（{self.config.meta_db}）：{exc}"
            ) from exc

    def ping(self) -> bool:
        try:
            conn = self._connect()
        except RecallError:
            return False
        try:
            with conn.cursor() as cur:
                cur.execute("SELECT 1 AS ok")
                return cur.fetchone()["ok"] == 1
        finally:
            conn.close()

    def load_snapshot(self) -> MetaSnapshot:
        conn = self._connect()
        try:
            with conn.cursor() as cur:
                tables: dict[str, dict[str, Any]] = {}
                columns: dict[str, dict[str, Any]] = {}
                metrics: dict[str, dict[str, Any]] = {}
                column_metrics: dict[str, list[str]] = {}

                cur.execute("SELECT * FROM table_info")
                self.roundtrips += 1
                for row in cur.fetchall():
                    item = dict(row)
                    tables[str(item["id"])] = item

                cur.execute("SELECT * FROM column_info")
                self.roundtrips += 1
                for row in cur.fetchall():
                    item = dict(row)
                    row_id = str(item["id"])
                    item["aliases"] = _decode_json(
                        item.pop("alias", None), field_name="alias", row_id=row_id
                    ) or []
                    item["examples"] = _decode_json(
                        item.get("examples"), field_name="examples", row_id=row_id
                    ) or []
                    item["value_domain"] = _decode_json(
                        item.get("value_domain"),
                        field_name="value_domain",
                        row_id=row_id,
                    ) or {}
                    columns[row_id] = item

                cur.execute("SELECT * FROM metric_info")
                self.roundtrips += 1
                for row in cur.fetchall():
                    item = dict(row)
                    row_id = str(item["id"])
                    item["aliases"] = _decode_json(
                        item.pop("alias", None), field_name="alias", row_id=row_id
                    ) or []
                    item["relevant_columns"] = _decode_json(
                        item.get("relevant_columns"),
                        field_name="relevant_columns",
                        row_id=row_id,
                    ) or []
                    item["rejected_values"] = _decode_json(
                        item.get("rejected_values"),
                        field_name="rejected_values",
                        row_id=row_id,
                    ) or []
                    item["expected_value"] = float(item.get("expected_value") or 0.0)
                    metrics[row_id] = item

                cur.execute("SELECT column_id, metric_id FROM column_metric")
                self.roundtrips += 1
                for row in cur.fetchall():
                    metric_id = str(row["metric_id"])
                    column_metrics.setdefault(metric_id, []).append(
                        str(row["column_id"])
                    )

                cur.execute("SELECT VERSION() AS version")
                self.roundtrips += 1
                version_row = cur.fetchone()
                version = str(version_row["version"]) if version_row else None
        except pymysql.MySQLError as exc:
            raise RecallError(f"读取 Meta 元数据失败：{exc}") from exc
        finally:
            conn.close()

        if not columns or not metrics or not tables:
            raise RecallError(
                f"Meta 库 {self.config.meta_db} 缺少 S2 元数据；"
                "请先运行 python -m app.build_meta_knowledge --verify"
            )
        return MetaSnapshot(
            tables=tables,
            columns=columns,
            metrics=metrics,
            column_metrics=column_metrics,
            version=version,
            roundtrips=self.roundtrips,
        )


@dataclass
class ESValueHit:
    id: str
    value: str
    column_id: str
    score: float
    matched_keywords: list[str] = field(default_factory=list)


class ESRecallClient:
    """ES 取值召回：`_msearch` 一次请求查完所有关键词。"""

    def __init__(self, config: BuildConfig):
        self.config = config
        self.index = config.es_index
        self.roundtrips = 0
        self.client = httpx.Client(
            base_url=config.es_url.rstrip("/"),
            timeout=config.request_timeout,
        )

    def close(self) -> None:
        self.client.close()

    def ping(self) -> bool:
        try:
            response = self.client.get("/")
            return response.status_code == 200
        except httpx.HTTPError:
            return False

    def _query_body(
        self, keyword: str, size: int, *, is_fallback: bool = False
    ) -> dict[str, Any]:
        should: list[dict[str, Any]] = [
            {
                "match_phrase": {
                    "value": {"query": keyword, "boost": 3.0}
                }
            },
            {
                "match": {
                    "value": {"query": keyword, "operator": "and"}
                }
            },
        ]
        if is_fallback:
            # 完整 query 的兜底不能只靠分词：IK 会把「哪个省」切出单字「省」，
            # 把「1 月」切出数字「1」，从而误命中省份/季度值。这里改成
            # 「完整短语 + 汉字的 2-gram」；既保留未知客户名（张敏）的召回，
            # 又不会用单字把整个省份枚举拉进来。
            fallback_terms: list[str] = []
            for run in re.findall(r"[\u4e00-\u9fff]+", keyword):
                for index in range(len(run) - 1):
                    gram = run[index : index + 2]
                    if gram not in fallback_terms:
                        fallback_terms.append(gram)
            for token in re.findall(r"[a-z0-9_]+", keyword.casefold()):
                # 纯数字（尤其是「1 月」里的 1）在 value 索引里会把 Q1 这类
                # 枚举值误召回；日期已由独立关键词 2025 / Q1 覆盖。
                if token.isdigit():
                    continue
                if token not in fallback_terms:
                    fallback_terms.append(token)
            for term in fallback_terms:
                should.append(
                    {
                        "match": {
                            "value": {
                                "query": term,
                                "boost": 0.8,
                                # 关键：fallback 的 2-gram 不再交给 IK 切成单字。
                                # 否则「哪个省」的「个省」会被切成「省」，把整个
                                # 省份枚举误召回。keyword analyzer 要求 value 索引里
                                # 真的存在这个 2-gram token。
                                "analyzer": "keyword",
                            }
                        }
                    }
                )
        return {
            "size": size,
            "track_total_hits": False,
            "_source": ["id", "value", "column_id"],
            "query": {
                "bool": {
                    "should": should,
                    "minimum_should_match": 1,
                }
            },
        }

    def msearch(
        self,
        keywords: list[str],
        *,
        size: int = 5,
        fallback_query: str | None = None,
    ) -> list[ESValueHit]:
        """一次 `_msearch`；返回按 value id 去重、按 (-score, id) 排序的命中。"""
        terms = [term for term in dict.fromkeys(keywords) if term]
        if not terms:
            return []

        lines: list[str] = []
        for keyword in terms:
            lines.append(
                json.dumps({"index": self.index}, ensure_ascii=False, separators=(",", ":"))
            )
            lines.append(
                json.dumps(
                    self._query_body(
                        keyword,
                        size,
                        is_fallback=fallback_query is not None
                        and keyword == fallback_query,
                    ),
                    ensure_ascii=False,
                    separators=(",", ":"),
                )
            )
        body = "\n".join(lines) + "\n"

        try:
            response = self.client.post(
                "/_msearch",
                content=body.encode("utf-8"),
                headers={"Content-Type": "application/x-ndjson"},
            )
        except httpx.HTTPError as exc:
            raise RecallError(
                f"连接 Elasticsearch 失败（{self.config.es_url}）：{exc}"
            ) from exc
        self.roundtrips += 1
        if response.status_code != 200:
            raise RecallError(
                f"ES _msearch 失败：{response.status_code} {response.text[:500]}"
            )
        try:
            payload = response.json()
        except ValueError as exc:
            raise RecallError("ES _msearch 返回的不是 JSON") from exc

        responses = payload.get("responses")
        if not isinstance(responses, list) or len(responses) != len(terms):
            raise RecallError(
                "ES _msearch 返回的 responses 数量与关键词数量不一致："
                f"{len(responses) if isinstance(responses, list) else 'invalid'} "
                f"!= {len(terms)}"
            )

        by_id: dict[str, ESValueHit] = {}
        for keyword, result in zip(terms, responses):
            if result.get("error"):
                raise RecallError(f"ES _msearch 子查询失败：{result['error']}")
            for hit in result.get("hits", {}).get("hits", []):
                source = hit.get("_source") or {}
                doc_id = str(source.get("id") or hit.get("_id") or "")
                value = str(source.get("value") or "")
                column_id = str(source.get("column_id") or "")
                if not doc_id or not value or not column_id:
                    continue
                score = float(hit.get("_score") or 0.0)
                current = by_id.get(doc_id)
                if current is None:
                    by_id[doc_id] = ESValueHit(
                        id=doc_id,
                        value=value,
                        column_id=column_id,
                        score=score,
                        matched_keywords=[keyword],
                    )
                else:
                    current.score = max(current.score, score)
                    if keyword not in current.matched_keywords:
                        current.matched_keywords.append(keyword)

        return sorted(by_id.values(), key=lambda item: (-item.score, item.id))


@dataclass
class QdrantHit:
    point_id: str
    score: float
    payload: dict[str, Any]
    keyword: str


class QdrantRecallClient:
    """Qdrant 批量向量召回：一个关键词一个 search，但只发一次 HTTP。"""

    def __init__(self, config: BuildConfig, collection: str):
        self.config = config
        self.collection = collection
        self.roundtrips = 0
        self.client = httpx.Client(
            base_url=config.qdrant_url.rstrip("/"),
            timeout=config.request_timeout,
        )

    def close(self) -> None:
        self.client.close()

    def ping(self) -> bool:
        try:
            response = self.client.get("/collections")
            return response.status_code == 200
        except httpx.HTTPError:
            return False

    def batch_search(
        self,
        vectors: list[list[float]],
        keywords: list[str],
        *,
        limit: int = 12,
    ) -> list[QdrantHit]:
        if len(vectors) != len(keywords):
            raise RecallError("Qdrant 查询向量与关键词数量不一致")
        if not vectors:
            return []
        body = {
            "searches": [
                {
                    "vector": vector,
                    "limit": limit,
                    "with_payload": True,
                    "with_vector": False,
                }
                for vector in vectors
            ]
        }
        try:
            response = self.client.post(
                f"/collections/{self.collection}/points/search/batch",
                json=body,
            )
        except httpx.HTTPError as exc:
            raise RecallError(
                f"连接 Qdrant 失败（{self.config.qdrant_url}）：{exc}"
            ) from exc
        self.roundtrips += 1
        if response.status_code != 200:
            raise RecallError(
                f"Qdrant batch search 失败（{self.collection}）："
                f"{response.status_code} {response.text[:500]}"
            )
        try:
            results = response.json()["result"]
        except (KeyError, ValueError) as exc:
            raise RecallError("Qdrant batch search 返回结构不合法") from exc
        if not isinstance(results, list) or len(results) != len(vectors):
            raise RecallError(
                "Qdrant batch search 返回的 searches 数量与关键词数量不一致"
            )

        hits: list[QdrantHit] = []
        for keyword, result in zip(keywords, results):
            for item in result:
                payload = item.get("payload") or {}
                if not payload.get("id"):
                    continue
                hits.append(
                    QdrantHit(
                        point_id=str(item.get("id", "")),
                        score=float(item.get("score") or 0.0),
                        payload=payload,
                        keyword=keyword,
                    )
                )
        return hits


@dataclass
class RecallStack:
    """一次检索需要的全部只读依赖。"""

    config: BuildConfig
    catalog: Catalog
    embedding: EmbeddingProvider
    es: ESRecallClient
    column_qdrant: QdrantRecallClient
    metric_qdrant: QdrantRecallClient
    meta: MetaReader

    @classmethod
    def open(
        cls,
        config: BuildConfig | None = None,
        catalog: Catalog | None = None,
    ) -> "RecallStack":
        resolved = config or BuildConfig.from_env()
        try:
            embedding = make_embedding_provider(
                resolved.embedding_provider, resolved.vector_size
            )
        except ValueError as exc:
            raise RecallError(str(exc)) from exc
        return cls(
            config=resolved,
            catalog=catalog or default_catalog(),
            embedding=embedding,
            es=ESRecallClient(resolved),
            column_qdrant=QdrantRecallClient(
                resolved, resolved.column_collection
            ),
            metric_qdrant=QdrantRecallClient(
                resolved, resolved.metric_collection
            ),
            meta=MetaReader(resolved),
        )

    def close(self) -> None:
        self.es.close()
        self.column_qdrant.close()
        self.metric_qdrant.close()

    def targets(self) -> dict[str, Any]:
        return {
            "meta_db": self.config.meta_db,
            "es_index": self.config.es_index,
            "column_collection": self.config.column_collection,
            "metric_collection": self.config.metric_collection,
            "embedding": self.config.embedding_provider,
            "vector_size": self.config.vector_size,
        }

    def ping(self) -> dict[str, bool]:
        return {
            "mysql": self.meta.ping(),
            "es": self.es.ping(),
            "qdrant": self.column_qdrant.ping() and self.metric_qdrant.ping(),
        }
