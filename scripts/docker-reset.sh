#!/bin/sh
# 删除本项目的 Docker 数据卷（MySQL / ES / Qdrant）——不可恢复。
#
# 为什么单独一个脚本、而不是写在 Makefile 配方里：
# Windows 上 mingw-make 构造 Windows 命令行时，会把配方里的字节按**系统
# ANSI 代码页**（cp936/GBK）解码一次，UTF-8 的中文因此变成乱码；个别情况下
# 那段字节会被整段替换掉，连带的引号一起被吃掉，shell 于是报
#     unexpected EOF while looking for matching `"'
# 脚本文件是被 shell 直接读的，字节原样输出，不经过 Windows 命令行这一层。
#
# 用法：sh scripts/docker-reset.sh   （或 make docker-reset）

set -u

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
cd "$ROOT"

# 从 .env 里读一个键的值。只处理这一个键，规则照 Compose 的 dotenv 来：
#   * "export KEY=value" 这种写法要认；
#   * 值可以带引号，引号里的内容原样保留；
#   * 未加引号时，" #"（空格 + #）之后是行内注释；
#   * 同一个键写多次时，后写的算数。
env_value() {
  _ev_key="$1"
  _ev_file="$2"
  [ -f "$_ev_file" ] || return 0
  _ev_raw="$(grep -E "^[[:space:]]*(export[[:space:]]+)?${_ev_key}[[:space:]]*=" \
    "$_ev_file" 2>/dev/null | tail -n 1 | tr -d '\r')"
  [ -n "$_ev_raw" ] || return 0
  _ev_raw="${_ev_raw#*=}"
  case "$_ev_raw" in
    '"'*) _ev_raw="${_ev_raw#\"}"; _ev_raw="${_ev_raw%%\"*}" ;;
    "'"*) _ev_raw="${_ev_raw#\'}"; _ev_raw="${_ev_raw%%\'*}" ;;
    *) case "$_ev_raw" in *" #"*) _ev_raw="${_ev_raw%%" #"*}" ;; esac ;;
  esac
  # 去掉首尾空白
  _ev_raw="${_ev_raw#"${_ev_raw%%[![:space:]]*}"}"
  _ev_raw="${_ev_raw%"${_ev_raw##*[![:space:]]}"}"
  printf '%s' "$_ev_raw"
}

# 卷的实际名字是 <项目名>_<卷名>。项目名的优先级和 Compose 一致：
#   环境变量 COMPOSE_PROJECT_NAME > .env 里的同名键 > compose.yaml 的 name:
# 提示里写错卷名，用户就不知道自己到底要删什么——而真正被删掉的
# 恰恰是当前项目名底下那三个卷。
#
# 注意：这里读到的 shell 变量一定是**已 export** 的（脚本是另一个进程，
# 没 export 的 shell 变量它根本看不见），而 Compose 看到的也是同一份
# 环境变量——两边不会因为"一个看得到、一个看不到"而错开。
PROJECT="${COMPOSE_PROJECT_NAME:-}"
if [ -z "$PROJECT" ]; then
  PROJECT="$(env_value COMPOSE_PROJECT_NAME "$ROOT/.env")"
fi
if [ -z "$PROJECT" ]; then
  PROJECT="$(sed -n 's/^name:[[:space:]]*//p' compose.yaml 2>/dev/null | head -n 1 | tr -d ' \t\r')"
  case "$PROJECT" in
    '"'*) PROJECT="${PROJECT#\"}"; PROJECT="${PROJECT%%\"*}" ;;
    "'"*) PROJECT="${PROJECT#\'}"; PROJECT="${PROJECT%%\'*}" ;;
    *) case "$PROJECT" in *" #"*) PROJECT="${PROJECT%%" #"*}" ;; esac ;;
  esac
fi

# 定不下项目名就不能往下走：Compose 这时候会退回"拿目录名当项目名"，
# 而脚本猜不到那个名字——提示里只能写没有前缀的 mysql_data，真正被删的
# 却是 <某个项目名>_mysql_data。宁可拒绝，也不能让人删了还以为是别的卷。
if [ -z "$PROJECT" ]; then
  printf '定不下 Compose 的项目名，列不出要删的卷名——先不删。\n' >&2
  printf '请设置 COMPOSE_PROJECT_NAME（环境变量或 .env 里写都行），再重跑一次。\n' >&2
  exit 1
fi

VOLUMES="mysql_data es_data qdrant_data"

printf '这会停掉并删除本项目的容器和网络，并删除以下数据卷（不可恢复）：\n'
for volume in $VOLUMES; do
  printf '  - %s_%s\n' "$PROJECT" "$volume"
done
printf '输入 yes 继续：'

# IFS= 让 read 保留前后的空格（" yes" 不算确认）；代价是它连行尾的换行符
# 也一起保留，Windows 终端回车还是 CRLF。先用命令替换去掉行尾的 LF（命令替换
# 会吃掉尾部的换行符），再去掉行尾的 CR。
answer=""
IFS= read -r answer || true
answer="$(printf '%s' "$answer")"
cr="$(printf '\r')"
answer="${answer%"$cr"}"

if [ "$answer" != "yes" ]; then
  printf '\n已取消：输入的不是 yes，数据卷没有被动过。\n' >&2
  exit 1
fi

printf '\n正在删除数据卷...\n'
# 项目名**显式**交给 Compose：提示里列的是这个项目名下的卷，
# 真正删的也必须是它们。不指定的话，删的是 Compose 自己解析出来的那个
# 项目——两边只要差一点（Compose 多认了一个控制变量、.env 的 dotenv
# 语义和上面那段解析不完全一样），用户看到的卷名和真正消失的卷就不是
# 同一批，而这是一句没有第二次确认的 `down -v`。
docker compose --project-name "$PROJECT" down -v
