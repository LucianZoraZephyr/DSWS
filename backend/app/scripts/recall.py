"""S3 · 检索 CLI。

    cd backend && uv run python -m app.scripts.recall "华北销售额"

stdout：稳定 YAML（LLM 将看到的上下文）
stderr：检索统计（ES / Qdrant / Meta 往返次数、候选数、最终保留数）

CLI 不生成 SQL、不调用 LLM，只验证检索层本身。
"""

from __future__ import annotations

import argparse
import sys
from collections.abc import Sequence

from app.recall.errors import RecallError
from app.recall.pipeline import recall
from app.recall.render import render
from app.cli import enable_utf8_stdio


def _stats_line(state) -> str:
    trace = state.trace
    candidates = (
        f"columns={trace.get('column_candidates')} "
        f"values={trace.get('value_candidates')} "
        f"metrics={trace.get('metric_candidates')}"
    )
    final = (
        f"tables={len(state.tables)} columns={len(state.columns)} "
        f"metrics={len(state.metrics)} values={len(state.values)}"
    )
    return (
        f"recall stats: query={state.query!r} "
        f"keywords={trace.get('keyword_count')} "
        f"es_roundtrips={trace.get('es_roundtrips')} "
        f"qdrant_roundtrips={trace.get('qdrant_roundtrips')} "
        f"(column={trace.get('qdrant_column_roundtrips')} "
        f"metric={trace.get('qdrant_metric_roundtrips')}) "
        f"meta_roundtrips={trace.get('meta_roundtrips')} "
        f"candidates[{candidates}] final[{final}]"
    )


def main(argv: Sequence[str] | None = None) -> int:
    enable_utf8_stdio()
    parser = argparse.ArgumentParser(
        prog="python -m app.scripts.recall",
        description="S3 检索层：输入问题，输出 LLM 上下文 YAML（无 LLM）",
    )
    parser.add_argument("query", help="自然语言问题，例如：华北销售额")
    args = parser.parse_args(argv)

    try:
        state = recall(args.query)
    except RecallError as exc:
        print(f"recall error: {exc}", file=sys.stderr)
        return 1
    except Exception as exc:  # pragma: no cover - CLI 兜底，避免只打 traceback
        print(f"recall failed: {type(exc).__name__}: {exc}", file=sys.stderr)
        return 1

    sys.stdout.write(render(state))
    sys.stdout.flush()
    print(_stats_line(state), file=sys.stderr)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
