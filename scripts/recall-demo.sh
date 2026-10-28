#!/bin/sh
# S3 阶段：跑一次真实检索，把 recall CLI 的 YAML 输出存到文件里。
#
# 检索问题写在脚本里、而不是 Makefile 配方里：Windows 上 mingw-make 构造
# Windows 命令行时，会把配方里的字节按**系统 ANSI 代码页**（cp936/GBK）
# 解码一次，UTF-8 的中文因此变成乱码。脚本文件由 shell 直接读，字节原样
# 输出，不经过 Windows 命令行这一层。
#
# 这个问题是 tests/recall/golden.yaml 里的第一题——
# backend/tests/test_recall_demo_script.py 守着两者一致。
#
# 用法：sh scripts/recall-demo.sh <输出文件>

set -u

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
OUT="${1:-}"

if [ -z "$OUT" ]; then
  echo "用法: sh scripts/recall-demo.sh <输出文件>" >&2
  exit 2
fi

QUERY='华北销售额'
if [ -z "$QUERY" ]; then
  echo "检索问题是空的" >&2
  exit 1
fi

cd "$ROOT/backend"
uv run python -m app.scripts.recall "$QUERY" > "$OUT"
