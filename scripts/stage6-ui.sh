#!/usr/bin/env bash
#
# S6 浏览器验收：6 类失败 + 取消。
#
# 自起隔离的临时后端（**场景应用**）与前端，跑完自动清理。
#
# 为什么用「场景应用」而不是生产应用：
#   场景应用与生产应用共用同一套 HTTP / SSE 管道（app.main.create_app），
#   只把编排器的两个外部依赖换成了夹具里的替身——模型与数据库。
#   校验、策略、校正、事件、协议全都是真代码，所以屏幕上的每一个红叉
#   都对应一次真实的判定；而 6 类失败因此**可复现**。
#
# 默认端口 8102 / 5275，避开正在运行的 dev 服务（8100 / 5273）。
# 用法：
#   ./scripts/stage6-ui.sh
#   API_PORT=8202 WEB_PORT=5375 ./scripts/stage6-ui.sh

set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
API_PORT="${API_PORT:-8102}"
WEB_PORT="${WEB_PORT:-5275}"
LOG_ROOT="${TMPDIR:-/tmp}"
mkdir -p "$LOG_ROOT"
LOG_DIR="$(mktemp -d "$LOG_ROOT/rebuild-s6-ui.XXXXXX")"

cleanup() {
  # 只杀本次自己起的进程组（setsid 让它们各自成一个组）——
  # 不用 pkill 之类的模式匹配，免得误伤别的会话。
  for pidfile in "$LOG_DIR/api.pid" "$LOG_DIR/web.pid"; do
    [[ -f "$pidfile" ]] || continue
    pid="$(cat "$pidfile")"
    kill -TERM -- "-$pid" 2>/dev/null || kill -TERM "$pid" 2>/dev/null || true
  done
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

wait_for() {
  local url="$1" tries="$2"
  for _ in $(seq 1 "$tries"); do
    if curl -sf "$url" >/dev/null; then
      return 0
    fi
    sleep 0.5
  done
  return 1
}

echo "== 启动临时场景后端 :$API_PORT =="
start_bg "$LOG_DIR/api.pid" "$LOG_DIR/api.log" "$ROOT/backend" \
  "uv run python -m app.scenarios.app --host 127.0.0.1 --port $API_PORT"

if ! wait_for "http://127.0.0.1:$API_PORT/api/health" 60; then
  echo "后端启动失败，日志：$LOG_DIR/api.log"
  tail -20 "$LOG_DIR/api.log" || true
  exit 1
fi

echo "== 启动临时前端 :$WEB_PORT =="
start_bg "$LOG_DIR/web.pid" "$LOG_DIR/web.log" "$ROOT/frontend" \
  "env VITE_DEV_PROXY_TARGET=http://127.0.0.1:$API_PORT npm run dev -- --port $WEB_PORT --strictPort"

if ! wait_for "http://127.0.0.1:$WEB_PORT/" 80; then
  echo "前端启动失败，日志：$LOG_DIR/web.log"
  tail -20 "$LOG_DIR/web.log" || true
  exit 1
fi

echo "== 浏览器验收 :$WEB_PORT =="
cd "$ROOT/frontend"
BASE_URL="http://127.0.0.1:$WEB_PORT" npm run test:ui
