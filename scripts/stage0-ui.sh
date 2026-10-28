#!/usr/bin/env bash
#
# S0 UI 冒烟：自起隔离的临时后端/前端，跑完自动清理。
#
# 默认端口 8101 / 5274，避开正在运行的 dev 服务（8100 / 5273）。
# 用法：
#   ./scripts/stage0-ui.sh
#   API_PORT=8201 WEB_PORT=5374 ./scripts/stage0-ui.sh

set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
API_PORT="${API_PORT:-8101}"
WEB_PORT="${WEB_PORT:-5274}"
LOG_ROOT="${TMPDIR:-/tmp}"
mkdir -p "$LOG_ROOT"
LOG_DIR="$(mktemp -d "$LOG_ROOT/rebuild-s0-ui.XXXXXX")"

cleanup() {
  for pidfile in "$LOG_DIR/api.pid" "$LOG_DIR/web.pid"; do
    [[ -f "$pidfile" ]] || continue
    pid="$(cat "$pidfile")"
    kill -TERM -- "-$pid" 2>/dev/null || kill -TERM "$pid" 2>/dev/null || true
  done

  # 兜底：按本次使用的唯一端口清理（防止 setsid/uv/npm 留下孤儿）
  pkill -f "uvicorn app.main:app --host 127.0.0.1 --port $API_PORT" 2>/dev/null || true
  pkill -f "vite.*--port $WEB_PORT" 2>/dev/null || true
}
trap cleanup EXIT

# 在后台起一个进程，并把它的 pid 记下来。
# setsid 在 Linux 上有、Windows 的 git-bash 里没有；没有时退化成普通后台
# 进程，清理时按 pid 杀。
start_bg() {
  local pidfile="$1" logfile="$2" workdir="$3" command="$4"
  if command -v setsid >/dev/null 2>&1; then
    setsid bash -c "echo \$\$ > '$pidfile'; cd '$workdir' && exec $command" \
      >"$logfile" 2>&1 &
  else
    bash -c "echo \$\$ > '$pidfile'; cd '$workdir' && exec $command" \
      >"$logfile" 2>&1 &
  fi
}

echo "== 启动临时后端 :$API_PORT =="
start_bg "$LOG_DIR/api.pid" "$LOG_DIR/api.log" "$ROOT/backend" \
  "uv run uvicorn app.main:app --host 127.0.0.1 --port $API_PORT"

# 不用 $(seq 1 60)：seq 在 macOS/BSD 上不存在（那边是 jot），命令替换展开
# 成空串，循环体一次都不执行，脚本会立刻带着"后端启动失败"失败——比真正
# 的原因更难查。
attempt=1
while [ "$attempt" -le 60 ]; do
  if curl -sf "http://127.0.0.1:$API_PORT/api/health" >/dev/null; then
    break
  fi
  sleep 0.5
  attempt=$((attempt + 1))
done

if ! curl -sf "http://127.0.0.1:$API_PORT/api/health" >/dev/null; then
  echo "后端启动失败，日志：$LOG_DIR/api.log"
  tail -20 "$LOG_DIR/api.log" || true
  exit 1
fi

echo "== 启动临时前端 :$WEB_PORT =="
start_bg "$LOG_DIR/web.pid" "$LOG_DIR/web.log" "$ROOT/frontend" \
  "VITE_DEV_PROXY_TARGET=http://127.0.0.1:$API_PORT exec npm run dev -- --port $WEB_PORT --strictPort"

attempt=1
while [ "$attempt" -le 80 ]; do
  if curl -sf "http://127.0.0.1:$WEB_PORT/" >/dev/null; then
    break
  fi
  sleep 0.5
  attempt=$((attempt + 1))
done

if ! curl -sf "http://127.0.0.1:$WEB_PORT/" >/dev/null; then
  echo "前端启动失败，日志：$LOG_DIR/web.log"
  tail -20 "$LOG_DIR/web.log" || true
  exit 1
fi

echo "== 冒烟测试 :$WEB_PORT =="
cd "$ROOT/frontend"
BASE_URL="http://127.0.0.1:$WEB_PORT" npm run test:ui
