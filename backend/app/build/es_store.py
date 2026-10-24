"""S2 · Elasticsearch 字段取值索引。

只存「字段值 -> column_id」这一层，供 S3 的关键词召回使用。
索引名默认 `value_index_rebuild`，与旧系统 `value_index` 隔离。
"""

from __future__ import annotations

import json
from collections.abc import Sequence
from typing import Any
from urllib.parse import quote

import httpx

from app.build.config import BuildConfig
from app.build.errors import BuildError
from app.build.plan import ValueDoc


def _mapping(analyzer: str) -> dict[str, Any]:
    return {
        "dynamic": False,
        "properties": {
            "id": {"type": "keyword"},
            "value": {
                "type": "text",
                "analyzer": analyzer,
                "search_analyzer": analyzer,
            },
            "column_id": {"type": "keyword"},
        },
    }


class ESValueStore:
    def __init__(self, config: BuildConfig):
        self.config = config
        self.client = httpx.Client(
            base_url=config.es_url.rstrip("/"),
            timeout=config.request_timeout,
        )

    def close(self) -> None:
        self.client.close()

    def _request(self, method: str, path: str, **kwargs: Any) -> httpx.Response:
        try:
            response = self.client.request(method, path, **kwargs)
        except httpx.HTTPError as exc:
            raise BuildError(
                f"连接 Elasticsearch 失败（{self.config.es_url}）：{exc}"
            ) from exc
        return response

    def ping(self) -> bool:
        try:
            response = self._request("GET", "/")
            return response.status_code == 200
        except BuildError:
            return False

    def exists(self) -> bool:
        response = self._request("HEAD", f"/{self.config.es_index}")
        return response.status_code == 200

    def delete(self) -> None:
        response = self._request("DELETE", f"/{self.config.es_index}")
        if response.status_code not in (200, 404):
            raise BuildError(
                f"删除 ES 索引失败：{response.status_code} {response.text}"
            )

    def create(self) -> str:
        """创建索引；优先 IK 分词，缺插件时回退 standard，并返回实际 analyzer。"""
        if self.exists():
            return self.analyzer() or "standard"
        last_error = ""
        for analyzer in ("ik_max_word", "standard"):
            response = self._request(
                "PUT",
                f"/{self.config.es_index}",
                json={"mappings": _mapping(analyzer)},
            )
            if response.status_code in (200, 201):
                return analyzer
            last_error = f"{response.status_code} {response.text}"
            # 只有「analyzer 不存在」才回退；其他错误直接失败。
            if "analyzer" not in response.text and "unknown" not in response.text:
                break
        raise BuildError(f"创建 ES 索引失败：{last_error}")

    def analyzer(self) -> str | None:
        response = self._request("GET", f"/{self.config.es_index}/_mapping")
        if response.status_code == 404:
            return None
        if response.status_code != 200:
            raise BuildError(
                f"读取 ES mapping 失败：{response.status_code} {response.text}"
            )
        try:
            mapping = response.json()[self.config.es_index]["mappings"]
            return mapping["properties"]["value"].get("analyzer")
        except (KeyError, TypeError):
            return None

    def bulk_index(self, docs: Sequence[ValueDoc]) -> int:
        if not docs:
            return 0
        lines: list[str] = []
        for doc in docs:
            lines.append(
                json.dumps(
                    {"index": {"_index": self.config.es_index, "_id": doc.id}},
                    ensure_ascii=False,
                )
            )
            lines.append(json.dumps(doc.as_source(), ensure_ascii=False))
        body = "\n".join(lines) + "\n"
        response = self._request(
            "POST",
            "/_bulk?refresh=true",
            content=body.encode("utf-8"),
            headers={"Content-Type": "application/x-ndjson"},
        )
        if response.status_code != 200:
            raise BuildError(
                f"ES bulk 写入失败：{response.status_code} {response.text[:500]}"
            )
        payload = response.json()
        if payload.get("errors"):
            first = next(
                (
                    item
                    for item in payload.get("items", [])
                    if item.get("index", {}).get("error")
                ),
                None,
            )
            raise BuildError(f"ES bulk 存在失败项：{first}")
        return len(docs)

    def count(self) -> int | None:
        response = self._request("GET", f"/{self.config.es_index}/_count")
        if response.status_code == 404:
            return None
        if response.status_code != 200:
            raise BuildError(
                f"读取 ES count 失败：{response.status_code} {response.text}"
            )
        return int(response.json()["count"])

    def get(self, doc_id: str) -> dict[str, Any] | None:
        path = f"/{self.config.es_index}/_doc/{quote(doc_id, safe='')}"
        response = self._request("GET", path)
        if response.status_code == 404:
            return None
        if response.status_code != 200:
            raise BuildError(
                f"读取 ES 文档失败：{response.status_code} {response.text}"
            )
        return response.json().get("_source")
