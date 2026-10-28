#!/usr/bin/env bash
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT"

fail() {
  echo "DOCKER SMOKE FAIL: $*" >&2
  # 一次性 init 已经退出，不加 -a 就看不到它——最可能的故障现场会消失。
  docker compose ps -a >&2 || true
  docker compose logs --tail=60 init backend frontend >&2 || true
  docker compose logs --tail=20 mysql elasticsearch qdrant >&2 || true
  exit 1
}

# ---------------------------------------------------------------------------
# 预检：先把"环境没配好"和"部署有问题"分开
# ---------------------------------------------------------------------------

# 解释器名字在平台之间不一样：Linux/macOS 常见 python3，
# 某些 Windows 环境只有 python。判定标准是"真的能跑"，不是"名字在 PATH 上"——
# Windows 上 Microsoft Store 的 python3 转发器存在但一执行就退 49，
# 挑中它会让这里变成"360 秒后报 /api/health 未就绪"，把方向指错。
# 详见 scripts/find-python.sh。
#
# 这两项检查排在 .env 之前：解释器根本跑不起来的时候，报"缺少 .env"
# 会把人引到错误的方向——make docker-env 同样要解释器，照着提示做
# 只会再撞一次墙。
# find-python.sh 的诊断**不要**丢：它说得出"版本太老（3.6）"这种具体原因，
# 而这里的消息只能说出大方向——用户照着大方向去装一个新的 Python，
# 装完还是同样的错，方向就指错了。
PYTHON="$(sh "$ROOT/scripts/find-python.sh" || true)"
if [ -z "$PYTHON" ]; then
  fail "宿主机找不到可用的 Python 解释器（python3 / python 跑不起来，或版本低于 3.8）：验收脚本要用它解析 JSON、检查 SSE 分片。Windows 上 AppData/Local/Microsoft/WindowsApps 里的 python3 是 Microsoft Store 的转发器，跑不起来；请用 Git Bash 并装好能真正执行的 Python 3.8 以上"
fi
if ! command -v curl >/dev/null 2>&1; then
  fail "宿主机找不到 curl：验收脚本用它探测 /api/health 与发起 SSE 查询"
fi

if [ ! -f .env ]; then
  fail "缺少 .env；先运行 make docker-env"
fi

# .env 里还是模板的占位密码（change-me-*）？——那等于没有密码：谁都能连上库。
# compose 的 ${MYSQL_PASSWORD:?} 只查"非空"，占位值照样让整个栈起来、
# 验收照常通过——而数据库的密码是写在 .env.example 里的公开值。
# 验收脚本不该给这样的部署盖章。
if grep -Eiq \
  '^[[:space:]]*(export[[:space:]]+)?(MYSQL_ROOT_PASSWORD|MYSQL_PASSWORD|REBUILD_DW_RO_PASSWORD)[[:space:]]*=[[:space:]]*"?change-me' \
  .env; then
  fail ".env 里还是模板的占位密码（change-me-*）：这份部署的密码是公开的，谁都能连上库；先跑 python scripts/generate_docker_env.py 换掉它"
fi

echo "== 1/8 启动 stack（含一次性 init） =="
docker compose up -d --build

# 端口问 Compose，不要自己解析 .env。
#
# Compose 解析 ${FRONTEND_HOST_PORT:-8080} 是"已导出的 shell 值优先、再 .env"；
# 自己 sed 一遍 .env 拿到的可能是另一个端口——用户在 shell 里 export 过这个
# 变量时尤其如此。于是脚本去戳一个没人监听的端口，报"360 秒内 /api/health 未就绪"，
# 真正的原因（端口不对）一个字都不提。
#
# `docker compose port frontend 80` 报的就是实际发布在宿主机的端口，
# 那是唯一的权威。BASE 从第 2 步才开始用，所以放在栈起来之后问正好。
BASE="${DOCKER_BASE_URL:-}"
if [ -z "$BASE" ]; then
  mapping="$(docker compose port frontend 80 2>/dev/null || true)"
  # 可能同时报出 IPv4 和 IPv6 两行，只看第一行。
  mapping="${mapping%%$'\n'*}"
  port="${mapping##*:}"
  case "$port" in
    ''|*[!0-9]*)
      fail "问不到 frontend 实际发布在宿主机的端口（docker compose port frontend 80）：栈是不是没起来？"
      ;;
  esac
  BASE="http://127.0.0.1:${port}"
fi
# 末尾的斜杠要去掉：DOCKER_BASE_URL 写成 http://host:8080/ 时，
# "$BASE/api/health" 会变成 //api/health。nginx 的代理配置用的是
# $request_uri（原样转交），后端收到 //api/health 就回 404——
# 这一步于是报"请求失败"，地址写歪了这件事一个字都不提。
while [ "${#BASE}" -gt 1 ] && [ "${BASE%/}" != "$BASE" ]; do
  BASE="${BASE%/}"
done

echo "== 2/8 等待 ${BASE}/api/health =="
healthy=0
attempt=1
# 不用 $(seq 1 180)：seq 在 macOS/BSD 上不存在，循环体一次都不执行，
# 脚本会立刻带着"health 未就绪"失败——比真正的原因更难查。
while [ "$attempt" -le 180 ]; do
  if curl -fsS "$BASE/api/health" 2>/dev/null \
    | "$PYTHON" "$ROOT/scripts/smoke_checks.py" health \
    >/dev/null 2>&1; then
    healthy=1
    break
  fi
  sleep 2
  attempt=$((attempt + 1))
done
if [ "$healthy" -ne 1 ]; then
  # 最后一次的失败原因要说出来：轮询时把 stderr 吞了，免得刷 180 遍。
  curl -fsS "$BASE/api/health" 2>/dev/null \
    | "$PYTHON" "$ROOT/scripts/smoke_checks.py" health \
    || true
  fail "360 秒内 /api/health 未就绪"
fi
echo "health: $(curl -fsS "$BASE/api/health")"

echo "== 3/8 五个长驻服务 healthy + init 正常退出 =="
STATUS_TABLE="$(docker compose ps -a --format "{{.Service}}\t{{.State}}\t{{.Health}}\t{{.ExitCode}}")" \
  || fail "docker compose ps 失败：栈是不是根本没起来？"
printf '%s\n' "$STATUS_TABLE" \
  | "$PYTHON" "$ROOT/scripts/smoke_checks.py" services \
  || fail "服务状态不满足要求"

echo "== 4/8 /api/steps = 12 =="
curl -fsS "$BASE/api/steps" > /dev/null || fail "/api/steps 请求失败"
"$PYTHON" "$ROOT/scripts/smoke_checks.py" steps "$BASE" \
  || fail "/api/steps 的步骤数不对"

echo "== 5/8 经 ${BASE} 发起真实查询（结果非空；是否流式由 nginx 生效配置把关） =="
# nginx 的生效配置要**先**取回来：SSE 这一步在"响应太快"的时候从时间上
# 分辨不出是否流式，只能靠这份配置定性（代理有没有开着缓冲）。第 6 步
# 再单独把这份配置的结论报一遍。
#
# 放在文件里而不是变量里：检查脚本要能从命令行拿到它，而把 7 KB 的配置
# 塞进命令行在 Windows 上离 32 KB 的上限太近。
NGINX_CONF_FILE="$(mktemp "${TMPDIR:-/tmp}/dsws-nginx-conf.XXXXXX")"
NGINX_CONF_ERR="$(mktemp "${TMPDIR:-/tmp}/dsws-nginx-err.XXXXXX")"
trap 'rm -f "$NGINX_CONF_FILE" "$NGINX_CONF_ERR"' EXIT
# 退出码要看：nginx -T 自己失败（配置有问题、容器里的 nginx 压根没起来）
# 时，它留下的半份输出不能当成"生效配置"——那份配置是 SSE 那一档**唯一**
# 的证据，拿一份残缺的当证据，验收就会报 PASS。
if ! docker compose exec -T frontend nginx -T >"$NGINX_CONF_FILE" 2>"$NGINX_CONF_ERR"; then
  fail "取 frontend 容器的 nginx 生效配置失败（nginx -T 退出非 0）：$(tail -n 3 "$NGINX_CONF_ERR" 2>/dev/null || true)"
fi
# 真的 nginx -T 每读一份配置文件，前面都有一行 "# configuration file <路径>:"。
# 没有它，说明这份输出不是一份完整的生效配置（多半是取到一半就断了）。
if ! grep -q '^# configuration file ' "$NGINX_CONF_FILE"; then
  fail "拿到的不是 nginx -T 的完整输出（没有 '# configuration file' 头）：多半是取到一半就断了，不能拿它当证据"
fi

# 检查逻辑在 scripts/smoke_checks.py 里：SSE 必须用裸 socket 测，
# 只有裸 socket 看得到 HTTP 分片的边界和到达时刻；而且它不能用 assert
# 写——`python -O` 会把 assert 剥掉，验收脚本就永远「通过」了。
#
# 这一步验的是"查得到结果"，不是"一定是流式"：响应快到 200 毫秒内就
# 结束的时候，从时间上分辨不出流式与否——那一档交给 nginx 的生效配置
# 定性，没拿到配置就判不通过（没有证据不等于通过）。
SSE_OUTPUT="$("$PYTHON" "$ROOT/scripts/smoke_checks.py" sse "$BASE" \
  --nginx-conf "$NGINX_CONF_FILE")" \
  || fail "SSE 查询没有通过流式与结果检查"
printf '%s\n' "$SSE_OUTPUT"

echo "== 6/8 nginx 的 /api/ 代理确实关着缓冲 =="
# 判断在 scripts/smoke_checks.py 里：grep 扫的是**整份** nginx -T 输出，
# 配置里任何地方出现过 "proxy_buffering off;" 就算过——把 /api/ 的缓冲
# 改成 on、而另一个 location（比如 /static/）留着 off，那一条照样"通过"，
# SSE 实际已经被攒成整包。要判的是处理 /api/ 请求的那个 location 的**生效值**
# （含 server / http 块上的继承，以及被 include 进来的文件）。
"$PYTHON" "$ROOT/scripts/smoke_checks.py" nginx <"$NGINX_CONF_FILE" \
  || fail "nginx 的生效配置里，处理 /api/ 的 location 没有 proxy_buffering off：SSE 会被攒成一个整包再发"

echo "== 7/8 只读账号验证 =="
docker compose exec -T backend \
  python -m app.scripts.security --verify-ro-account \
  || fail "只读账号验证未通过"

echo "== 8/8 S2 元知识对账（init 配置一次性执行） =="
docker compose run --rm --no-deps init \
  python -m app.build_meta_knowledge --verify \
  || fail "S2 元知识对账未通过"

echo "== init 幂等（必须跳过重建） =="
init_output="$(docker compose run --rm --no-deps init)"
printf '%s\n' "$init_output"
grep -q "meta: verify passed; skip rebuild" <<<"$init_output" \
  || fail "init 没有走跳过重建路径"

echo "DOCKER SMOKE PASS"
