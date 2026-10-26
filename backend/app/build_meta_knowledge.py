"""S2 CLI：构建 / 校验元知识。

用法：
    python -m app.build_meta_knowledge --dry-run
    python -m app.build_meta_knowledge --fresh
    python -m app.build_meta_knowledge --verify
    python -m app.build_meta_knowledge --assert-idempotent
    python -m app.build_meta_knowledge              # 增量构建（已有且一致则 noop）
"""

from __future__ import annotations

import argparse
import sys
from collections.abc import Sequence

from app.build.config import BuildConfig
from app.build.errors import BuildError, VerifyError
from app.build.pipeline import (
    BuildResult,
    VerifyResult,
    assert_idempotent,
    build,
    verify,
)
from app.build.plan import BuildPlan
from app.cli import enable_utf8_stdio


def _print_plan(plan: BuildPlan, config: BuildConfig) -> None:
    counts = plan.counts()
    print("source: contracts/schema.contract.yaml + fixtures/dw.sql (in-memory SQLite)")
    print(f"targets: {config.safe_repr()}")
    print(f"contract: version={plan.contract_version} hash={plan.config_hash[:16]}…")
    print("counts:")
    print(
        "  Meta    "
        f"tables={counts['meta_tables']} columns={counts['meta_columns']} "
        f"metrics={counts['meta_metrics']} column_metrics={counts['meta_column_metrics']}"
    )
    print(f"  ES      values={counts['es_values']}")
    print(
        "  Qdrant  "
        f"column_points={counts['qdrant_columns']} "
        f"metric_points={counts['qdrant_metrics']}"
    )


def _print_manifest(manifest: dict | None) -> None:
    if not manifest:
        return
    print(
        f"manifest: build_id={manifest.get('build_id')} "
        f"built_at={manifest.get('built_at')} status={manifest.get('status')}"
    )


def _print_build_result(result: BuildResult, config: BuildConfig) -> None:
    _print_plan(result.plan, config)
    if result.status == "dry-run":
        print("DRY RUN: 0 writes（未连接 Meta/ES/Qdrant）")
        return
    if result.status == "noop":
        print(f"NOOP: 0 writes · {result.reason}")
        _print_manifest(result.manifest)
        return
    print(f"BUILT: writes={result.writes} · {result.reason}")
    _print_manifest(result.manifest)
    print("隔离：旧系统 meta / value_index / *_collection 未改动")


def _print_verify(result: VerifyResult, config: BuildConfig) -> None:
    _print_plan(result.plan, config)
    if result.actual:
        print("actual:")
        for key, value in result.actual.items():
            print(f"  {key}={value}")
    _print_manifest(result.manifest)
    if result.failures:
        print("S2 VERIFY FAIL")
        for failure in result.failures:
            print(f"  - {failure}")
    else:
        print("S2 VERIFY PASS")


def main(argv: Sequence[str] | None = None) -> int:
    enable_utf8_stdio()
    parser = argparse.ArgumentParser(
        description="S2 · 从契约构建元知识（Meta MySQL + ES + Qdrant）"
    )
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument(
        "--fresh",
        action="store_true",
        help="删除并重建 rebuild 自己的库/索引/collection，然后全量构建",
    )
    mode.add_argument(
        "--verify",
        action="store_true",
        help="只读校验 manifest、三存储计数、维度与样例",
    )
    mode.add_argument(
        "--dry-run",
        action="store_true",
        help="只生成计划并打印计数，不连接任何存储",
    )
    mode.add_argument(
        "--assert-idempotent",
        action="store_true",
        help="再跑一次构建，断言 noop 且 manifest 未变",
    )
    args = parser.parse_args(argv)

    config = BuildConfig.from_env()
    try:
        if args.verify:
            result = verify(config)
            _print_verify(result, config)
            return 0 if result.ok else 1
        if args.assert_idempotent:
            result = assert_idempotent(config)
            _print_build_result(result, config)
            print("S2 IDEMPOTENT PASS")
            return 0
        result = build(config, fresh=args.fresh, dry_run=args.dry_run)
        _print_build_result(result, config)
        return 0
    except VerifyError as exc:
        print(f"S2 FAIL: {exc}")
        return 1
    except BuildError as exc:
        print(f"S2 FAIL: {exc}")
        return 1
    except Exception as exc:  # noqa: BLE001  # 连接失败等也要给出可读错误
        print(f"S2 FAIL: {type(exc).__name__}: {exc}")
        return 1


if __name__ == "__main__":
    sys.exit(main())
