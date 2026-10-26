"""S4 · 生成 CLI。

    cd backend && uv run python -m app.scripts.generate "华北销售额"

stdout：YAML（outcome / sql / refusal / guard）
stderr：统计（生成器名字、契约版本、槽位个数）

基线模式（路线图里的 `make baseline`）：

    cd backend && uv run python -m app.scripts.generate --baseline

stdout：问题 / SQL / 判定 的表格
stderr：汇总（通过数、拒答数、失败数）

CLI **不执行 SQL**：S4 只到「生成 + 静态校验」。
"""

from __future__ import annotations

import argparse
import sys
from collections.abc import Sequence
from datetime import datetime
from pathlib import Path
from typing import Any

import yaml

from app.generate.pipeline import generate, generate_with_recall
from app.cli import enable_utf8_stdio

REBUILD_ROOT = Path(__file__).resolve().parents[3]
BASELINE_PATH = REBUILD_ROOT / "backend" / "tests" / "generate" / "baseline.yaml"


def _load_baseline(path: Path) -> list[dict[str, Any]]:
    data = yaml.safe_load(path.read_text(encoding="utf-8"))
    if not isinstance(data, dict):
        raise ValueError(f"{path} 不是 YAML 映射")
    questions = data.get("questions")
    if not isinstance(questions, list) or not questions:
        raise ValueError(f"{path} 里没有 questions")
    return questions


def _result_payload(result) -> dict[str, Any]:
    return {
        "query": result.query,
        "outcome": result.outcome,
        "sql": result.sql,
        "refusal": (
            {"code": result.refusal.code, "reason": result.refusal.reason}
            if result.refusal
            else None
        ),
        "guard": result.guard.to_dict(),
    }


def _print_baseline(cases: list[dict[str, Any]], *, live: bool) -> int:
    rows: list[tuple[str, str, str]] = []
    failures: list[str] = []

    for case in cases:
        question = str(case["question"])
        expect = str(case.get("expect") or "sql")
        if live:
            result = generate_with_recall(question)
        else:
            result = generate(question)

        problems: list[str] = []

        if expect == "refusal":
            if result.ok:
                problems.append("期望拒答，实际生成了 SQL")
            else:
                expected_code = case.get("refusal_code")
                if expected_code and result.refusal.code != str(expected_code):
                    problems.append(
                        f"拒答码不符：期望 {expected_code}，实际 {result.refusal.code}"
                    )
            verdict = "REFUSAL" if not result.ok else "FAIL"
        else:
            if not result.ok:
                problems.append(
                    f"期望 SQL，实际拒答 {result.refusal.code if result.refusal else '?'}"
                )
            else:
                sql = result.sql or ""
                for needle in case.get("sql_contains") or []:
                    if str(needle) not in sql:
                        problems.append(f"SQL 缺少 {needle!r}")
                for needle in case.get("sql_not_contains") or []:
                    if str(needle) in sql:
                        problems.append(f"SQL 不该包含 {needle!r}")
                if not result.guard.ok:
                    problems.append("静态校验未通过")
            verdict = "PASS" if result.ok else "FAIL"

        if problems:
            failures.append(f"{case.get('id', question)}: " + "；".join(problems))
            verdict = "FAIL"

        sql_text = (result.sql or "").replace("\n", " ")
        if not result.ok and result.refusal:
            sql_text = f"[{result.refusal.code}] {result.refusal.reason}"
        rows.append((question, sql_text, verdict))

    width_q = max((len(row[0]) for row in rows), default=4)
    width_v = max((len(row[2]) for row in rows), default=4)

    print("| 问题 | SQL / 拒答 | 判定 |")
    print(f"| {'-' * width_q} | {'-' * 60} | {'-' * width_v} |")
    for question, sql_text, verdict in rows:
        print(f"| {question} | {sql_text} | {verdict} |")

    passed = sum(1 for row in rows if row[2] != "FAIL")
    print(
        f"\n基线：{passed}/{len(rows)} 通过"
        f"（拒答 {sum(1 for row in rows if row[2] == 'REFUSAL')}）",
        file=sys.stderr,
    )
    if failures:
        print("失败明细：", file=sys.stderr)
        for item in failures:
            print(f"  - {item}", file=sys.stderr)
        return 1
    return 0


def main(argv: Sequence[str] | None = None) -> int:
    enable_utf8_stdio()
    parser = argparse.ArgumentParser(
        prog="python -m app.scripts.generate",
        description="S4 生成层：输入问题，输出 SQL 或显式拒答（不执行 SQL）",
    )
    parser.add_argument("query", nargs="?", help="自然语言问题，例如：华北销售额")
    parser.add_argument(
        "--baseline",
        action="store_true",
        help="跑 10 题基线并打印表格",
    )
    parser.add_argument(
        "--baseline-file",
        type=Path,
        default=BASELINE_PATH,
        help="基线题库路径",
    )
    parser.add_argument(
        "--live",
        action="store_true",
        help="用 S3 的真实召回作为上下文（需要 MySQL/ES/Qdrant）",
    )
    parser.add_argument(
        "--now",
        default=None,
        help="注入当前时间（ISO 格式），用于可复现运行",
    )
    args = parser.parse_args(argv)

    now = datetime.fromisoformat(args.now) if args.now else None

    if args.baseline:
        if not args.baseline_file.exists():
            print(f"基线文件不存在：{args.baseline_file}", file=sys.stderr)
            return 1
        cases = _load_baseline(args.baseline_file)
        return _print_baseline(cases, live=args.live)

    if not args.query:
        parser.error("需要一个问题，或使用 --baseline")

    try:
        if args.live:
            result = generate_with_recall(args.query, now=now)
        else:
            result = generate(args.query, now=now)
    except Exception as exc:
        print(f"generate failed: {type(exc).__name__}: {exc}", file=sys.stderr)
        return 1

    payload = _result_payload(result)
    sys.stdout.write(
        yaml.safe_dump(
            payload,
            allow_unicode=True,
            sort_keys=False,
            default_flow_style=False,
            width=1000,
        )
    )
    sys.stdout.flush()
    print(
        f"generate stats: outcome={result.outcome} "
        f"slots={len(result.slots)} guard_ok={result.guard.ok}",
        file=sys.stderr,
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
