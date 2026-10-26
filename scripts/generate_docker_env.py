#!/usr/bin/env python3
"""从 .env.example 生成带随机密码的 .env。"""

from __future__ import annotations

import argparse
import re
import secrets
import sys
from collections.abc import Sequence
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
EXAMPLE = ROOT / ".env.example"
DEFAULT_TARGET = ROOT / ".env"

SECRET_KEYS = (
    "MYSQL_ROOT_PASSWORD",
    "MYSQL_PASSWORD",
    "REBUILD_DW_RO_PASSWORD",
)


def _replace(text: str, key: str, value: str) -> str:
    pattern = re.compile(rf"^{re.escape(key)}=.*$", re.MULTILINE)
    if pattern.search(text) is None:
        raise SystemExit(f".env.example 缺少 {key}= 行")
    return pattern.sub(f"{key}={value}", text, count=1)


def render(example: str) -> str:
    """把三个占位密码替换成随机值；其他行原样保留。"""
    text = example
    for key in SECRET_KEYS:
        text = _replace(text, key, secrets.token_urlsafe(24))
    return text


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="生成 Docker Compose 的 .env（随机密码）"
    )
    parser.add_argument(
        "--force",
        action="store_true",
        help="覆盖已存在的 .env",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="只打印生成结果，不写文件",
    )
    parser.add_argument(
        "--output",
        type=Path,
        default=DEFAULT_TARGET,
        help="输出路径（默认仓库根 .env）",
    )
    args = parser.parse_args(argv)

    if not EXAMPLE.is_file():
        print(f"缺少模板：{EXAMPLE}", file=sys.stderr)
        return 1

    text = render(EXAMPLE.read_text(encoding="utf-8"))
    if args.dry_run:
        print(text, end="")
        return 0

    if args.output.exists() and not args.force:
        print(
            f"{args.output} 已存在；如需覆盖请加 --force",
            file=sys.stderr,
        )
        return 1

    args.output.write_text(text, encoding="utf-8")
    try:
        args.output.chmod(0o600)
    except OSError:
        # Windows 上 chmod 不保证生效；文件内容仍是正确的。
        pass
    print(f"已生成 {args.output}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
