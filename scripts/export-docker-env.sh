#!/usr/bin/env bash
# 把仓库根 .env 里的 Compose 凭据导出成宿主 CLI 使用的 REBUILD_* 变量。
#
# 用法（在仓库根目录执行；必须 source，不能直接执行）：
#   source scripts/export-docker-env.sh
#
# 成功时完全静默；任何错误信息都不包含密码值。

if [ "${BASH_SOURCE[0]}" = "$0" ]; then
  printf '用法：source scripts/export-docker-env.sh（必须用 source 运行，不能直接执行）\n' >&2
  exit 1
fi

_export_docker_env_root="$(
  cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd
)" || return 1
_export_docker_env_file="$_export_docker_env_root/.env"

if [ ! -f "$_export_docker_env_file" ]; then
  printf '缺少 %s；先运行 make docker-env\n' "$_export_docker_env_file" >&2
  unset _export_docker_env_root _export_docker_env_file
  return 1
fi

_export_docker_env_mysql_password=
_export_docker_env_mysql_root_password=
_export_docker_env_ro_password=
_export_docker_env_mysql_port=
_export_docker_env_es_port=
_export_docker_env_qdrant_port=

while IFS= read -r _export_docker_env_line || [ -n "$_export_docker_env_line" ]; do
  _export_docker_env_line="${_export_docker_env_line%$'\r'}"
  case "$_export_docker_env_line" in
    ''|'#'*) continue ;;
  esac
  _export_docker_env_key="${_export_docker_env_line%%=*}"
  _export_docker_env_value="${_export_docker_env_line#*=}"
  case "$_export_docker_env_key" in
    MYSQL_PASSWORD)
      _export_docker_env_mysql_password="$_export_docker_env_value"
      ;;
    MYSQL_ROOT_PASSWORD)
      _export_docker_env_mysql_root_password="$_export_docker_env_value"
      ;;
    REBUILD_DW_RO_PASSWORD)
      _export_docker_env_ro_password="$_export_docker_env_value"
      ;;
    MYSQL_HOST_PORT)
      _export_docker_env_mysql_port="$_export_docker_env_value"
      ;;
    ES_HOST_PORT)
      _export_docker_env_es_port="$_export_docker_env_value"
      ;;
    QDRANT_HOST_PORT)
      _export_docker_env_qdrant_port="$_export_docker_env_value"
      ;;
  esac
done < "$_export_docker_env_file"

_export_docker_env_missing=
if [ -z "$_export_docker_env_mysql_password" ]; then
  _export_docker_env_missing=MYSQL_PASSWORD
elif [ -z "$_export_docker_env_mysql_root_password" ]; then
  _export_docker_env_missing=MYSQL_ROOT_PASSWORD
elif [ -z "$_export_docker_env_ro_password" ]; then
  _export_docker_env_missing=REBUILD_DW_RO_PASSWORD
fi

if [ -n "$_export_docker_env_missing" ]; then
  printf '.env 缺少必填项 %s\n' "$_export_docker_env_missing" >&2
  unset _export_docker_env_root _export_docker_env_file \
    _export_docker_env_line _export_docker_env_key _export_docker_env_value \
    _export_docker_env_mysql_password _export_docker_env_mysql_root_password \
    _export_docker_env_ro_password _export_docker_env_mysql_port \
    _export_docker_env_es_port _export_docker_env_qdrant_port \
    _export_docker_env_missing
  return 1
fi

export REBUILD_MYSQL_HOST=127.0.0.1
export REBUILD_MYSQL_PORT="${_export_docker_env_mysql_port:-3306}"
export REBUILD_MYSQL_USER=didilili
export REBUILD_MYSQL_PASSWORD="$_export_docker_env_mysql_password"
export REBUILD_MYSQL_ROOT_USER=root
export REBUILD_MYSQL_ROOT_PASSWORD="$_export_docker_env_mysql_root_password"
export REBUILD_DW_RO_PASSWORD="$_export_docker_env_ro_password"
export REBUILD_ES_URL="http://127.0.0.1:${_export_docker_env_es_port:-9200}"
export REBUILD_QDRANT_URL="http://127.0.0.1:${_export_docker_env_qdrant_port:-6333}"

unset _export_docker_env_root _export_docker_env_file \
  _export_docker_env_line _export_docker_env_key _export_docker_env_value \
  _export_docker_env_mysql_password _export_docker_env_mysql_root_password \
  _export_docker_env_ro_password _export_docker_env_mysql_port \
  _export_docker_env_es_port _export_docker_env_qdrant_port \
  _export_docker_env_missing

return 0
