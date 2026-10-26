#!/usr/bin/env bash
#
# S7 浏览器验收：13 条前端缺陷（F1–F13）各一条断言。
#
# 自起隔离的**回放服务器**与前端，跑完自动清理。
#
# 为什么用回放而不是真后端：
#   有些界面行为真系统产生不了（旧系统的伪 SQL 行、契约之外的事件），
#   但界面必须照样处理对。夹具录的是后端真吐出的字节流，只归一化了
#   duration_ms / request_id（见 backend/app/scripts/record_sse.py）。
#
# 默认端口 8103 / 5276，避开正在运行的 dev 服务（8100 / 5273）与
# stage0-ui（8101 / 5274）、stage6-ui（8102 / 5275）。
# 用法：
#   ./scripts/stage7-ui.sh
#   API_PORT=8203 WEB_PORT=5376 ./scripts/stage7-ui.sh

set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
API_PORT="${API_PORT:-8103}"
WEB_PORT="${WEB_PORT:-5276}"
LOG_ROOT="${TMPDIR:-/tmp}"
mkdir -p "$LOG_ROOT"
LOG_DIR="$(mktemp -d "$LOG_ROOT/rebuild-s7-ui.XXXXXX")"

cleanup() {
  # 只杀本次自己起的进程（Linux 上按 setsid 进程组，Windows/git-bash 上
  # 按 pid 转 winpid 后 taskkill 整个进程树）——不用 pkill 之类的模式
  # 匹配，免得误伤别的会话。
  for pidfile in "$LOG_DIR/api.pid" "$LOG_DIR/web.pid"; do
    [[ -f "$pidfile" ]] || continue
    pid="$(cat "$pidfile")"
    kill -TERM -- "-$pid" 2>/dev/null || true
    # git-bash 下 `kill` 不一定能把 npm.cmd / node.exe 的子进程带走，
    # 所以再用 Windows 原生的进程树终止。转换参数，避免 MSYS 把 /F
    # 当成路径改写。
    if [[ -r "/proc/$pid/winpid" ]] && command -v taskkill >/dev/null 2>&1; then
      winpid="$(cat "/proc/$pid/winpid" 2>/dev/null || true)"
      if [[ -n "$winpid" ]]; then
        MSYS2_ARG_CONV_EXCL='*' taskkill /F /T /PID "$winpid" >/dev/null 2>&1 || true
      fi
    fi
    kill -TERM "$pid" 2>/dev/null || true
  done
  wait 2>/dev/null || true
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

echo "== 启动回放服务器 :$API_PORT =="
start_bg "$LOG_DIR/api.pid" "$LOG_DIR/api.log" "$ROOT/frontend" \
  "node tests/replay-server.mjs --port $API_PORT"

if ! wait_for "http://127.0.0.1:$API_PORT/api/health" 60; then
  echo "回放服务器启动失败，日志：$LOG_DIR/api.log"
  tail -20 "$LOG_DIR/api.log" || true
  exit 1
fi

echo "== 启动临时前端 :$WEB_PORT =="
# 直接起 vite，而不是 `npm run dev`：npm 在 Windows 上会多套一层 cmd.exe，
# 清理时按 pid 杀容易留下孤儿 vite 进程。
start_bg "$LOG_DIR/web.pid" "$LOG_DIR/web.log" "$ROOT/frontend" \
  "env VITE_DEV_PROXY_TARGET=http://127.0.0.1:$API_PORT node node_modules/vite/bin/vite.js --port $WEB_PORT --strictPort"

if ! wait_for "http://127.0.0.1:$WEB_PORT/" 80; then
  echo "前端启动失败，日志：$LOG_DIR/web.log"
  tail -20 "$LOG_DIR/web.log" || true
  exit 1
fi

echo "== 浏览器验收 :$WEB_PORT =="
cd "$ROOT/frontend"
BASE_URL="http://127.0.0.1:$WEB_PORT" npm run test:ui:s7
