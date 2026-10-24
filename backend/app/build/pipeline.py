"""S2 · 构建 / 校验 / 幂等断言的主流程。

关键顺序：
    1. 先加载并校验契约，生成计划（不写存储）
    2. 非 --fresh 时先读 manifest，再读三存储计数
       - 哈希一致 + 计数一致 -> 直接 noop，零写入
    3. 只有确实需要重建时，才建库/建索引/写数据
    4. 最后写 manifest；manifest 是「这次构建成功」的唯一凭据
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any

from app.build.config import BuildConfig
from app.build.embedding import EmbeddingProvider, make_embedding_provider
from app.build.errors import BuildError, VerifyError
from app.build.es_store import ESValueStore
from app.build.meta_store import COUNT_KEYS, MANIFEST_VERSION, MetaStore
from app.build.plan import BuildPlan, build_plan
from app.build.qdrant_store import QdrantVectorStore
from app.schema_contract import load_contract, load_ddl


@dataclass
class BuildResult:
    status: str
    plan: BuildPlan
    manifest: dict[str, Any] | None
    writes: int
    reason: str = ""


@dataclass
class VerifyResult:
    ok: bool
    plan: BuildPlan
    manifest: dict[str, Any] | None
    actual: dict[str, int] | None
    failures: list[str] = field(default_factory=list)


def load_plan(config: BuildConfig) -> tuple[BuildPlan, EmbeddingProvider]:
    """契约 + 冻结数仓 -> 计划；任何存储都还没碰。"""
    contract = load_contract()
    ddl = load_ddl()
    try:
        embedding = make_embedding_provider(
            config.embedding_provider, config.vector_size
        )
        plan = build_plan(contract, ddl, config, embedding)
        return plan, embedding
    finally:
        ddl.close()


def manifest_counts(manifest: dict[str, Any]) -> dict[str, int]:
    return {key: int(manifest[key]) for key in COUNT_KEYS}


def _actual_counts(
    meta: MetaStore,
    es: ESValueStore,
    columns: QdrantVectorStore,
    metrics: QdrantVectorStore,
) -> dict[str, int] | None:
    meta_counts = meta.counts()
    if meta_counts is None:
        return None
    es_count = es.count()
    column_count = columns.count()
    metric_count = metrics.count()
    if es_count is None or column_count is None or metric_count is None:
        return None
    return {
        **meta_counts,
        "es_values": es_count,
        "qdrant_columns": column_count,
        "qdrant_metrics": metric_count,
    }


def _targets_healthy(
    config: BuildConfig,
    manifest: dict[str, Any],
    es: ESValueStore,
    columns: QdrantVectorStore,
    metrics: QdrantVectorStore,
) -> bool:
    """计数一致还不够：维度/analyzer 被外部改坏时也要重建。"""
    analyzer = es.analyzer()
    if analyzer is None:
        return False
    if manifest.get("es_analyzer") and analyzer != manifest["es_analyzer"]:
        return False
    return (
        columns.vector_size() == config.vector_size
        and metrics.vector_size() == config.vector_size
    )


def _samples_healthy(
    meta: MetaStore,
    es: ESValueStore,
    columns: QdrantVectorStore,
    metrics: QdrantVectorStore,
    plan: BuildPlan,
) -> bool:
    """抽查内容，避免「计数对但内容错」被 noop 放过。"""
    if plan.values:
        sample = plan.values[0]
        source = es.get(sample.id)
        if (
            source is None
            or source.get("value") != sample.value
            or source.get("column_id") != sample.column_id
        ):
            return False
    if plan.column_points:
        sample = plan.column_points[0]
        point = columns.get(sample.id)
        if point is None or point.get("payload", {}).get("id") != sample.payload.get(
            "id"
        ):
            return False
    if plan.metric_points:
        sample = plan.metric_points[0]
        point = metrics.get(sample.id)
        if point is None or point.get("payload", {}).get("id") != sample.payload.get(
            "id"
        ):
            return False
    for metric in plan.metrics:
        if metric.id != "AOV":
            continue
        row = meta.get_row("metric_info", metric.id)
        if row is None:
            return False
        if row.get("formula") != metric.formula:
            return False
        if abs(float(row.get("expected_value", 0.0)) - metric.expected_value) > 0.005:
            return False
    return True


def _new_manifest(
    plan: BuildPlan,
    embedding: EmbeddingProvider,
    config: BuildConfig,
    *,
    es_analyzer: str | None,
    status: str,
    error: str | None = None,
) -> dict[str, Any]:
    return {
        "build_id": str(uuid.uuid4()),
        "manifest_version": MANIFEST_VERSION,
        "built_at": datetime.now(UTC).isoformat(timespec="seconds"),
        "config_hash": plan.config_hash,
        "contract_version": plan.contract_version,
        "embedding_provider": embedding.name,
        "embedding_dim": embedding.dim,
        "es_analyzer": es_analyzer,
        "targets": config.targets(),
        "status": status,
        "error": error,
        **plan.counts(),
    }


def _write_failed_manifest(
    meta: MetaStore,
    plan: BuildPlan,
    embedding: EmbeddingProvider,
    config: BuildConfig,
    error: Exception,
) -> None:
    try:
        manifest = _new_manifest(
            plan,
            embedding,
            config,
            es_analyzer=None,
            status="failed",
            error=str(error)[:4000],
        )
        meta.write_manifest(manifest)
    except Exception:  # noqa: BLE001, S110
        # 失败路径不能掩盖原始异常。
        pass


def build(
    config: BuildConfig, *, fresh: bool = False, dry_run: bool = False
) -> BuildResult:
    plan, embedding = load_plan(config)

    if dry_run:
        return BuildResult(
            status="dry-run",
            plan=plan,
            manifest=None,
            writes=0,
            reason="dry-run 只生成计划，不连接/写入任何存储",
        )

    meta = MetaStore(config)
    es = ESValueStore(config)
    columns = QdrantVectorStore(config, config.column_collection)
    metrics = QdrantVectorStore(config, config.metric_collection)

    try:
        if not fresh:
            existing = meta.read_manifest()
            if (
                existing is not None
                and existing.get("manifest_version") == MANIFEST_VERSION
                and existing.get("status") == "success"
                and existing.get("config_hash") == plan.config_hash
                and existing.get("contract_version") == plan.contract_version
            ):
                actual = _actual_counts(meta, es, columns, metrics)
                expected = manifest_counts(existing)
                if (
                    actual == expected
                    and _targets_healthy(config, existing, es, columns, metrics)
                    and _samples_healthy(meta, es, columns, metrics, plan)
                ):
                    return BuildResult(
                        status="noop",
                        plan=plan,
                        manifest=existing,
                        writes=0,
                        reason="manifest 哈希、三存储计数、维度与样例均一致，跳过写入",
                    )
                # 计数不一致：不在这里返回，继续走下面的修复路径。
                # 这样 --fresh 之外的普通构建也能修复被外部删掉/改坏的索引。

        if fresh:
            # 只删除 rebuild 目标；旧系统 meta/value_index/*_collection 不受影响。
            meta.drop_database()
            meta.ensure_database()
            meta.create_schema()
        else:
            meta.ensure_database()
            meta.create_schema()

        # 只要走到真正的写入路径，就重建两个索引。
        # 这样配置变更后不会残留旧值文档/旧向量；noop 路径在上面已经返回。
        es.delete()
        columns.delete()
        metrics.delete()

        es_analyzer = es.create()
        columns.create()
        metrics.create()

        # Meta 是事实表，先写；ES/Qdrant 是可重建索引，后写。
        meta.reset_data()
        meta.write_plan(plan)

        es_written = es.bulk_index(plan.values)

        column_vectors = embedding.embed_many(
            [point.text for point in plan.column_points]
        )
        column_written = columns.upsert(plan.column_points, column_vectors)

        metric_vectors = embedding.embed_many(
            [point.text for point in plan.metric_points]
        )
        metric_written = metrics.upsert(plan.metric_points, metric_vectors)

        manifest = _new_manifest(
            plan,
            embedding,
            config,
            es_analyzer=es_analyzer,
            status="success",
        )
        meta.write_manifest(manifest)

        meta_rows = sum(
            count for key, count in plan.counts().items() if key.startswith("meta_")
        )
        writes = meta_rows + es_written + column_written + metric_written + 1
        return BuildResult(
            status="built",
            plan=plan,
            manifest=manifest,
            writes=writes,
            reason="已写入 Meta MySQL、ES、Qdrant，并登记 manifest",
        )
    except Exception as exc:
        _write_failed_manifest(meta, plan, embedding, config, exc)
        if isinstance(exc, BuildError):
            raise
        raise BuildError(f"构建失败：{exc}") from exc
    finally:
        es.close()
        columns.close()
        metrics.close()


def verify(config: BuildConfig) -> VerifyResult:
    """只读校验：manifest、配置哈希、三存储计数、向量维度、样例文档。"""
    plan, _ = load_plan(config)
    meta = MetaStore(config)
    es = ESValueStore(config)
    columns = QdrantVectorStore(config, config.column_collection)
    metrics = QdrantVectorStore(config, config.metric_collection)
    failures: list[str] = []

    try:
        manifest = meta.read_manifest()
        if manifest is None:
            return VerifyResult(
                ok=False,
                plan=plan,
                manifest=None,
                actual=None,
                failures=["没有找到 build_manifest；先运行 --fresh"],
            )

        if manifest.get("manifest_version") != MANIFEST_VERSION:
            failures.append(
                f"manifest_version={manifest.get('manifest_version')!r}，"
                f"期望 {MANIFEST_VERSION}"
            )
        if manifest.get("status") != "success":
            failures.append(
                f"最新 manifest 状态为 {manifest.get('status')!r}，不是 success"
            )
        if manifest.get("config_hash") != plan.config_hash:
            failures.append(
                "config_hash 不一致：当前契约/配置/数据与 manifest 不是同一次构建"
            )
        if manifest.get("contract_version") != plan.contract_version:
            failures.append("contract_version 不一致")
        if manifest.get("targets") != config.targets():
            failures.append(
                f"manifest 目标 {manifest.get('targets')} 与当前配置 {config.targets()} 不一致"
            )

        actual = _actual_counts(meta, es, columns, metrics)
        if actual is None:
            failures.append("有存储目标不存在（Meta 表 / ES 索引 / Qdrant collection）")
        else:
            expected = manifest_counts(manifest)
            for key in COUNT_KEYS:
                if actual[key] != expected[key]:
                    failures.append(
                        f"{key}: manifest={expected[key]} 实际={actual[key]}"
                    )

        analyzer = es.analyzer()
        if analyzer is None:
            failures.append("ES 索引 mapping 中没有 value.analyzer")
        elif manifest.get("es_analyzer") and analyzer != manifest["es_analyzer"]:
            failures.append(
                f"ES analyzer: manifest={manifest['es_analyzer']} 实际={analyzer}"
            )

        for label, store in (("column", columns), ("metric", metrics)):
            size = store.vector_size()
            if size is None:
                failures.append(f"Qdrant {label} collection 不存在")
            elif size != config.vector_size:
                failures.append(
                    f"Qdrant {label} 维度={size}，配置={config.vector_size}"
                )

        # 样例检查：计数对但内容为空/错列，也要红。
        if plan.values:
            sample = plan.values[0]
            source = es.get(sample.id)
            if source is None:
                failures.append(f"ES 样例文档缺失：{sample.id}")
            elif (
                source.get("value") != sample.value
                or source.get("column_id") != sample.column_id
            ):
                failures.append(f"ES 样例内容不一致：{sample.id} -> {source}")

        if plan.column_points:
            sample = plan.column_points[0]
            point = columns.get(sample.id)
            if point is None:
                failures.append(f"Qdrant column 样例缺失：{sample.id}")
            elif point.get("payload", {}).get("id") != sample.payload.get("id"):
                failures.append(f"Qdrant column 样例 payload 不一致：{sample.id}")

        if plan.metric_points:
            sample = plan.metric_points[0]
            point = metrics.get(sample.id)
            if point is None:
                failures.append(f"Qdrant metric 样例缺失：{sample.id}")
            elif point.get("payload", {}).get("id") != sample.payload.get("id"):
                failures.append(f"Qdrant metric 样例 payload 不一致：{sample.id}")

        # Meta 也抽查内容：计数相同但 AOV 公式被改坏，必须红。
        if actual is not None:
            aov = next((metric for metric in plan.metrics if metric.id == "AOV"), None)
            if aov is not None:
                row = meta.get_row("metric_info", aov.id)
                if row is None:
                    failures.append("Meta 样例指标 AOV 缺失")
                else:
                    if row.get("formula") != aov.formula:
                        failures.append("Meta AOV formula 与契约不一致")
                    if (
                        abs(float(row.get("expected_value", 0.0)) - aov.expected_value)
                        > 0.005
                    ):
                        failures.append("Meta AOV expected_value 与契约不一致")
            column_row = meta.get_row("column_info", "fact_order.order_amount")
            if column_row is None:
                failures.append("Meta 样例列 fact_order.order_amount 缺失")
            elif column_row.get("table_id") != "fact_order":
                failures.append("Meta 样例列 table_id 不一致")
            table_row = meta.get_row("table_info", "fact_order")
            if table_row is None:
                failures.append("Meta 样例表 fact_order 缺失")
            elif "order_id" not in str(table_row.get("grain", "")):
                failures.append("Meta 样例表 grain 与契约不一致")

        return VerifyResult(
            ok=not failures,
            plan=plan,
            manifest=manifest,
            actual=actual,
            failures=failures,
        )
    finally:
        es.close()
        columns.close()
        metrics.close()


def assert_idempotent(config: BuildConfig) -> BuildResult:
    """跑第二次构建，断言它是 noop 且 manifest 完全没变。"""
    meta = MetaStore(config)
    before = meta.read_manifest()
    if before is None:
        raise VerifyError("没有 manifest；先运行 --fresh 再断言幂等")

    result = build(config, fresh=False)
    if result.status != "noop":
        raise VerifyError(
            f"第二次构建没有跳过：status={result.status}, writes={result.writes}; "
            f"原因：{result.reason}"
        )

    after = meta.read_manifest()
    if after is None:
        raise VerifyError("第二次构建后 manifest 消失")
    if (before["build_id"], before["built_at"]) != (
        after["build_id"],
        after["built_at"],
    ):
        raise VerifyError(
            "第二次构建改写了 manifest："
            f"{before['build_id']}@{before['built_at']} -> "
            f"{after['build_id']}@{after['built_at']}"
        )
    return result
