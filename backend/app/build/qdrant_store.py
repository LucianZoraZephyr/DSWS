"""S2 · Qdrant 向量集合。

每个字段/指标会生成多个 point（名称、描述、每个别名各一个），
point id 由 `uuid5(kind, object_id, text)` 决定，因此重复构建不会产生重复点。
"""

from __future__ import annotations

from collections.abc import Sequence
from typing import Any

import httpx

from app.build.config import BuildConfig
from app.build.errors import BuildError
from app.build.plan import VectorPoint


class QdrantVectorStore:
    def __init__(self, config: BuildConfig, collection: str):
        self.config = config
        self.collection = collection
        self.client = httpx.Client(
            base_url=config.qdrant_url.rstrip("/"),
            timeout=config.request_timeout,
        )

    def close(self) -> None:
        self.client.close()

    def _request(self, method: str, path: str, **kwargs: Any) -> httpx.Response:
        try:
            response = self.client.request(method, path, **kwargs)
        except httpx.HTTPError as exc:
            raise BuildError(
                f"连接 Qdrant 失败（{self.config.qdrant_url}）：{exc}"
            ) from exc
        return response

    def ping(self) -> bool:
        try:
            response = self._request("GET", "/collections")
            return response.status_code == 200
        except BuildError:
            return False

    def exists(self) -> bool:
        response = self._request("GET", f"/collections/{self.collection}")
        return response.status_code == 200

    def delete(self) -> None:
        response = self._request("DELETE", f"/collections/{self.collection}")
        if response.status_code not in (200, 404):
            raise BuildError(
                f"删除 Qdrant collection 失败：{response.status_code} {response.text}"
            )

    def create(self) -> int:
        if self.exists():
            size = self.vector_size()
            if size is not None and size != self.config.vector_size:
                raise BuildError(
                    f"Qdrant collection {self.collection} 现有维度 {size}，"
                    f"与配置 {self.config.vector_size} 不一致；请用 --fresh 重建"
                )
            return size or self.config.vector_size
        response = self._request(
            "PUT",
            f"/collections/{self.collection}",
            json={
                "vectors": {
                    "size": self.config.vector_size,
                    "distance": "Cosine",
                }
            },
        )
        if response.status_code not in (200, 201):
            raise BuildError(
                f"创建 Qdrant collection 失败：{response.status_code} {response.text}"
            )
        return self.config.vector_size

    def vector_size(self) -> int | None:
        response = self._request("GET", f"/collections/{self.collection}")
        if response.status_code == 404:
            return None
        if response.status_code != 200:
            raise BuildError(
                f"读取 Qdrant collection 失败：{response.status_code} {response.text}"
            )
        try:
            vectors = response.json()["result"]["config"]["params"]["vectors"]
            if isinstance(vectors, dict):
                return int(vectors["size"])
            return None
        except (KeyError, TypeError, ValueError):
            return None

    def count(self) -> int | None:
        response = self._request(
            "POST",
            f"/collections/{self.collection}/points/count",
            json={"exact": True},
        )
        if response.status_code == 404:
            return None
        if response.status_code != 200:
            raise BuildError(
                f"读取 Qdrant count 失败：{response.status_code} {response.text}"
            )
        return int(response.json()["result"]["count"])

    def upsert(
        self, points: Sequence[VectorPoint], vectors: Sequence[Sequence[float]]
    ) -> int:
        if len(points) != len(vectors):
            raise BuildError("Qdrant points 与 vectors 数量不一致")
        if not points:
            return 0
        batch_size = 64
        written = 0
        for start in range(0, len(points), batch_size):
            batch_points = points[start : start + batch_size]
            batch_vectors = vectors[start : start + batch_size]
            body = {
                "points": [
                    {
                        "id": point.id,
                        "vector": list(vector),
                        "payload": point.payload,
                    }
                    for point, vector in zip(batch_points, batch_vectors)
                ]
            }
            response = self._request(
                "PUT",
                f"/collections/{self.collection}/points?wait=true",
                json=body,
            )
            if response.status_code not in (200, 201):
                raise BuildError(
                    f"Qdrant upsert 失败：{response.status_code} {response.text[:500]}"
                )
            written += len(batch_points)
        return written

    def get(self, point_id: str) -> dict[str, Any] | None:
        response = self._request(
            "GET", f"/collections/{self.collection}/points/{point_id}"
        )
        if response.status_code == 404:
            return None
        if response.status_code != 200:
            raise BuildError(
                f"读取 Qdrant point 失败：{response.status_code} {response.text}"
            )
        return response.json().get("result")
