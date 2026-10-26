#!/usr/bin/env bash
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT"

fail() {
  echo "DOCKER SMOKE FAIL: $*" >&2
  docker compose ps >&2 || true
  docker compose logs --tail=80 init backend frontend >&2 || true
  exit 1
}

if [ ! -f .env ]; then
  fail "缺少 .env；先运行 make docker-env"
fi

# .env 可能由 Windows Python 生成为 CRLF，先去掉 \r 再拼 URL。
FRONTEND_HOST_PORT="$(
  sed -n 's/^FRONTEND_HOST_PORT=//p' .env | tr -d '\r' | tail -n 1
)"
BASE="${DOCKER_BASE_URL:-http://127.0.0.1:${FRONTEND_HOST_PORT:-8080}}"

echo "== 1/7 启动 stack（含一次性 init） =="
docker compose up -d --build

echo "== 2/7 等待 ${BASE}/api/health =="
healthy=0
for attempt in $(seq 1 180); do
  if curl -fsS "$BASE/api/health" 2>/dev/null \
    | python -c 'import json,sys; d=json.load(sys.stdin); assert d.get("status")=="ok" and d.get("stage")=="S7"' \
    >/dev/null 2>&1; then
    healthy=1
    break
  fi
  sleep 2
done
if [ "$healthy" -ne 1 ]; then
  fail "360 秒内 /api/health 未就绪"
fi
echo "health: $(curl -fsS "$BASE/api/health")"

echo "== 3/7 五长驻服务运行 + /api/steps = 12 =="
running="$(docker compose ps --status running --services)"
for service in mysql elasticsearch qdrant backend frontend; do
  if ! grep -qx "$service" <<<"$running"; then
    fail "服务未运行：$service"
  fi
done
curl -fsS "$BASE/api/steps" \
  | python -c 'import json,sys; d=json.load(sys.stdin); assert len(d["steps"])==12; print("steps=%d" % len(d["steps"]))'

echo "== 4/7 经 8080 发起真实查询（SSE 必须 result 且行数 > 0） =="
python - "$BASE" <<'PY'
import json
import sys
import urllib.request

base = sys.argv[1]
payload = json.dumps({"query": "华北销售额"}, ensure_ascii=False).encode("utf-8")
request = urllib.request.Request(
    base + "/api/query",
    data=payload,
    headers={"Content-Type": "application/json; charset=utf-8"},
)
with urllib.request.urlopen(request, timeout=180) as response:
    body = response.read().decode("utf-8")

events = []
for block in body.split("\n\n"):
    for line in block.splitlines():
        if line.startswith("data:"):
            events.append(json.loads(line[len("data:"):].strip()))

assert events, "没有收到任何 SSE 事件"
last = events[-1]
assert last["type"] == "result", f"最后一条不是 result：{last}"
rows = last["data"]["rows"]
assert rows, "result.rows 为空"
print(f"result rows={len(rows)} columns={len(last['data']['columns'])}")
PY

echo "== 5/7 只读账号验证 =="
docker compose exec -T backend \
  python -m app.scripts.security --verify-ro-account

echo "== 6/7 S2 元知识对账（init 配置一次性执行） =="
docker compose run --rm --no-deps init \
  python -m app.build_meta_knowledge --verify

echo "== 7/7 init 幂等（必须跳过重建） =="
init_output="$(docker compose run --rm --no-deps init)"
printf '%s\n' "$init_output"
grep -q "meta: verify passed; skip rebuild" <<<"$init_output" \
  || fail "init 没有走跳过重建路径"

echo "DOCKER SMOKE PASS"
