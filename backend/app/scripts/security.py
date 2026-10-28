"""S5 · 安全 CLI。

    cd backend && uv run python -m app.scripts.security --check

离线门禁（**不需要** MySQL）：

    --check            注入语料 100% 拒绝 + 零误杀 + S4 基线零误杀
    --policy "<sql>"   看一条 SQL 的策略判定（改写后的可执行 SQL）
    --corpus           打印注入语料的判定表

需要 MySQL 的部分：

    --execute "<sql>"            用只读账号安全地执行一条 SQL
    --provision-ro-account       开通只读账号（需要 root）
    --verify-ro-account          验证只读账号真的写不了
    --demo                       两屏对比：旧系统执行 / 新系统拒绝

CLI 的退出码就是结论：0 = 通过，非 0 = 有问题。
"""

from __future__ import annotations

import argparse
import os
import sys
from collections.abc import Sequence
from pathlib import Path
from typing import Any

import yaml

from app.cli import enable_utf8_stdio
from app.security.account import AccountSpec, verify_account
from app.security.config import load_policy_config
from app.security.corpus import load_corpus
from app.security.demo import run_demo
from app.security.executor import PASSWORD_ENV, ReadOnlyExecutor
from app.security.policy import enforce

BASELINE_PATH = (
    Path(__file__).resolve().parents[2] / "tests" / "generate" / "baseline.yaml"
)


# ---------------------------------------------------------------------------
# 连接
# ---------------------------------------------------------------------------


def _connect(
    *,
    user: str,
    password: str,
    database: str | None = None,
    multi_statements: bool = False,
):
    import pymysql
    from pymysql.constants import CLIENT

    flags = CLIENT.CAPABILITIES
    if multi_statements:
        flags |= CLIENT.MULTI_STATEMENTS
    else:
        flags &= ~CLIENT.MULTI_STATEMENTS
    return pymysql.connect(
        host=os.getenv("REBUILD_MYSQL_HOST", "127.0.0.1"),
        port=int(os.getenv("REBUILD_MYSQL_PORT", "3306")),
        user=user,
        password=password,
        database=database,
        charset="utf8mb4",
        connect_timeout=5,
        read_timeout=30,
        client_flag=flags,
        autocommit=True,
    )


def _ro_password() -> str:
    password = os.getenv(PASSWORD_ENV, "")
    if not password:
        raise SystemExit(
            f"缺少环境变量 {PASSWORD_ENV}：只读账号的密码不在仓库里，"
            f"必须由部署环境提供"
        )
    return password


def _privileged_password() -> str:
    password = os.getenv("REBUILD_MYSQL_ROOT_PASSWORD", "")
    if not password:
        raise SystemExit(
            "缺少环境变量 REBUILD_MYSQL_ROOT_PASSWORD（开通只读账号需要 root）"
        )
    return password


def _app_password() -> str:
    """应用账号的密码。

    和 `_ro_password()` / `_privileged_password()` 同一个约定：密码只由环境
    变量提供，缺了就地停下并点名变量——不要拿着空密码去撞一次库，那只会
    换来一句看不出所以然的 ``Access denied``。
    """
    password = os.getenv("REBUILD_MYSQL_PASSWORD", "")
    if not password:
        raise SystemExit(
            "缺少环境变量 REBUILD_MYSQL_PASSWORD：应用账号的密码不在仓库里，"
            "必须由部署环境提供"
        )
    return password


# ---------------------------------------------------------------------------
# 离线门禁
# ---------------------------------------------------------------------------


def _check_corpus() -> list[str]:
    """注入语料：必须 100% 被拒绝，且原因码精确匹配。"""
    corpus = load_corpus()
    problems: list[str] = []
    for case in corpus.reject:
        decision = enforce(case.sql)
        if decision.ok:
            problems.append(f"{case.id}: 竟然被放行了")
            continue
        if decision.reason_code != case.reason_code:
            problems.append(
                f"{case.id}: 原因码不符（期望 {case.reason_code}，"
                f"实际 {decision.reason_code}）"
            )
    print(
        f"  注入语料：{len(corpus.reject) - len(problems)}/{len(corpus.reject)} "
        f"条按预期拒绝"
    )
    return problems


def _check_allow() -> list[str]:
    """反向语料：长得像正常查询，必须全部放行（零误杀）。"""
    corpus = load_corpus()
    problems: list[str] = []
    for case in corpus.allow:
        decision = enforce(case.sql)
        if not decision.ok:
            problems.append(
                f"{case.id}: 被误杀（{decision.reason_code}："
                f"{decision.violations[0].message}）"
            )
    print(f"  零误杀语料：{len(corpus.allow) - len(problems)}/{len(corpus.allow)} 条放行")
    return problems


def _check_baseline() -> list[str]:
    """S4 的 10 题基线：生成的 SQL 必须全部通过 S5 策略。"""
    from app.generate.pipeline import generate

    data = yaml.safe_load(BASELINE_PATH.read_text(encoding="utf-8"))
    problems: list[str] = []
    checked = 0
    for case in data.get("questions", []) or []:
        result = generate(str(case["question"]))
        if not result.ok:
            # 拒答题根本不会产生 SQL，也就谈不上被策略误杀。
            continue
        checked += 1
        decision = enforce(result.sql or "")
        if not decision.ok:
            problems.append(
                f"{case.get('id', case['question'])}: 基线 SQL 被策略拒绝"
                f"（{decision.reason_code}）"
            )
    print(f"  S4 基线：{checked - len(problems)}/{checked} 条 SQL 通过策略")
    return problems


def _run_check() -> int:
    print("== 1/3 注入语料（必须 100% 拒绝） ==")
    problems = _check_corpus()
    print("== 2/3 零误杀语料（必须全部放行） ==")
    problems += _check_allow()
    print("== 3/3 S4 基线零误杀 ==")
    problems += _check_baseline()
    if problems:
        print(f"FAIL: {len(problems)} 项不符合预期", file=sys.stderr)
        for item in problems:
            print(f"  - {item}", file=sys.stderr)
        return 1
    print("S5 PASS")
    return 0


# ---------------------------------------------------------------------------
# 打印
# ---------------------------------------------------------------------------


def _print_decision(decision: Any) -> None:
    if decision.ok:
        print(f"判定：放行（自动 LIMIT = {decision.applied_limit}）")
        print(f"可执行的 SQL：\n{decision.sql}")
        return
    print(f"判定：拒绝（原因码 {decision.reason_code}）")
    for item in decision.violations:
        print(f"  - {item.code}：{item.message}")
        if item.detail:
            print(f"    现场：{item.detail}")


def _print_corpus() -> int:
    corpus = load_corpus()
    print("| 语料 | 期望 | 实际 | 判定 |")
    print("|---|---|---|---|")
    failures = 0
    for case in (*corpus.reject, *corpus.allow):
        decision = enforce(case.sql)
        if case.expect_reject:
            good = (not decision.ok) and decision.reason_code == case.reason_code
            expect = case.reason_code or ""
            actual = decision.reason_code or "放行"
        else:
            good = decision.ok
            expect = "放行"
            actual = "放行" if decision.ok else (decision.reason_code or "拒绝")
        if not good:
            failures += 1
        print(
            f"| {case.id} | {expect} | {actual} | {'PASS' if good else 'FAIL'} |"
        )
    print(f"\n语料：{len(corpus)} 条，失败 {failures} 条", file=sys.stderr)
    return 1 if failures else 0


# ---------------------------------------------------------------------------
# 需要 MySQL 的部分
# ---------------------------------------------------------------------------


def _provision_ro_account() -> int:
    config = load_policy_config()
    spec = AccountSpec.from_config(config, password=_ro_password())
    connection = _connect(
        user=os.getenv("REBUILD_MYSQL_ROOT_USER", "root"),
        password=_privileged_password(),
    )
    try:
        with connection.cursor() as cursor:
            for statement in spec.statements():
                cursor.execute(statement)
                print(f"  已执行：{_redact(statement, spec.password)}")
    finally:
        connection.close()
    print(f"只读账号就绪：{spec.safe_repr()}")
    return 0


def _redact(statement: str, password: str) -> str:
    return statement.replace(password, "***") if password else statement


def _verify_ro_account() -> int:
    config = load_policy_config()
    spec = AccountSpec.from_config(config, password=_ro_password())
    connection = _connect(
        user=spec.user,
        password=spec.password,
        database=spec.database,
    )
    try:
        report = verify_account(connection, spec)
    finally:
        connection.close()
    print(f"账号：{spec.safe_repr()}")
    for grant in report.grants:
        print(f"  GRANT {grant}")
    print(f"写操作尝试：{report.write_attempt}")
    if not report.ok:
        print(f"FAIL: {len(report.problems)} 项问题", file=sys.stderr)
        for item in report.problems:
            print(f"  - {item}", file=sys.stderr)
        return 1
    print("PASS: 该账号在 dw 上只有 SELECT")
    return 0


def _execute(sql: str) -> int:
    executor = ReadOnlyExecutor(password=_ro_password())
    result = executor.execute(sql)
    if not result.ok:
        error = result.error
        if error is None:
            # 结果说"不 ok"却没带错误信息：这是结果本身出了毛病，如实说。
            # 这里原来是一句 assert——它只给类型检查看，运行时什么都不保证，
            # 而且 python -O 会把它剥掉，于是同一件事变成一句
            # AttributeError 的 traceback：调用方看到的是崩溃，不是"被拒绝"。
            print("拒绝：结果里没有错误信息", file=sys.stderr)
            return 1
        print(
            f"拒绝：{error.code}"
            f"（原因码 {error.reason_code}）",
            file=sys.stderr,
        )
        print(f"  {error.message}", file=sys.stderr)
        return 1
    print(f"SQL：{result.sql}")
    print(f"行数：{len(result.rows)}  耗时：{result.elapsed_ms} ms")
    for row in result.rows[:20]:
        print(yaml.safe_dump(row, allow_unicode=True, sort_keys=False).rstrip())
    return 0


def _demo() -> int:
    """两屏对比：旧系统执行、新系统拒绝。"""
    connection = _connect(
        user=os.getenv("REBUILD_MYSQL_USER", "didilili"),
        password=_app_password(),
        database=load_policy_config().account_database,
        multi_statements=True,
    )
    try:
        report = run_demo(connection)
    finally:
        connection.close()

    print("载荷：" + report.payload)
    print()
    print("【旧系统】validate() = explain {sql}，然后直接执行：")
    print(f"  校验结果：{report.legacy_validate}")
    print(
        f"  第二句真的执行了：{'是' if report.legacy_executed else '否'}"
        f"（探针表 {'还在' if report.legacy_probe_alive else '已被 DROP'}）"
    )
    print()
    print("【新系统】执行前先过安全策略：")
    print(
        f"  判定：{'拒绝' if report.new_rejected else '放行'}"
        f"（原因码 {report.new_reason_code}）"
    )
    print(f"  探针表：{'还在（数据库没被碰过）' if report.new_probe_alive else '不见了'}")
    print()
    if not report.ok:
        print(f"FAIL: {len(report.problems)} 项问题", file=sys.stderr)
        for item in report.problems:
            print(f"  - {item}", file=sys.stderr)
        return 1
    print("PASS: 同一段 payload，旧系统执行、新系统拒绝")
    return 0


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def main(argv: Sequence[str] | None = None) -> int:
    enable_utf8_stdio()
    parser = argparse.ArgumentParser(
        prog="python -m app.scripts.security",
        description="S5 安全层：AST 策略 + 只读执行 + 注入语料",
    )
    group = parser.add_mutually_exclusive_group(required=True)
    group.add_argument(
        "--check",
        action="store_true",
        help="离线门禁：注入语料 100% 拒绝 + 零误杀（不需要 MySQL）",
    )
    group.add_argument("--policy", metavar="SQL", help="看一条 SQL 的策略判定")
    group.add_argument("--corpus", action="store_true", help="打印注入语料判定表")
    group.add_argument(
        "--execute",
        metavar="SQL",
        help="用只读账号安全地执行一条 SQL（需要 MySQL）",
    )
    group.add_argument(
        "--provision-ro-account",
        action="store_true",
        help="开通只读账号（需要 MySQL root）",
    )
    group.add_argument(
        "--verify-ro-account",
        action="store_true",
        help="验证只读账号真的写不了（需要 MySQL）",
    )
    group.add_argument(
        "--demo",
        action="store_true",
        help="两屏对比：旧系统执行、新系统拒绝（需要 MySQL）",
    )
    args = parser.parse_args(argv)

    if args.check:
        return _run_check()
    if args.corpus:
        return _print_corpus()
    if args.policy is not None:
        _print_decision(enforce(args.policy))
        return 0
    if args.execute is not None:
        return _execute(args.execute)
    if args.provision_ro_account:
        return _provision_ro_account()
    if args.verify_ro_account:
        return _verify_ro_account()
    if args.demo:
        return _demo()
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
