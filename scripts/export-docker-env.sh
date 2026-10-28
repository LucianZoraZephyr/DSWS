#!/usr/bin/env bash
# 把仓库根 .env 里的 Compose 凭据导出成宿主 CLI 使用的 REBUILD_* 变量。
#
# 用法（在仓库根目录执行；必须 source，不能直接执行）：
#   source scripts/export-docker-env.sh
#
# 成功时完全静默（只有取值对不上时会在 stderr 上告警）；任何信息都不包含密码值。
#
# ---------------------------------------------------------------------------
# 取值规则（必须和 Compose 对齐，否则容器和宿主 CLI 会拿到不同的密码）
#
# 1. 解析规则和 Compose（compose-go 的 dotenv）一致：同一份 .env，
#    容器里的 MySQL 密码和宿主 CLI 用的密码得是同一个字符串。两边都"没写错"
#    但拿到的值不同，宿主机 CLI 就只能收到一句莫名其妙的 Access denied。
#
# 2. .env 是这份部署的事实源。shell 里已经有同名的值、但和 .env 不一致时，
#    以 .env 为准，并告警（只报变量名，不报值）。
#    理由：Compose 解析 ${VAR} 是"已导出的 shell 值优先、再 .env"，脚本改写
#    shell 之后容器读到的就是脚本导出的那个值——两边必须一样。
#
#    "已导出"这三个字是实打实的限制：Compose 是**另一个进程**，它只看得到
#    **export 出去**的变量。shell 里 `VAR=value`（没 export）定义的值，
#    Compose 根本看不见——那种情况下"以 .env 为准"才是和 Compose 一致的做法，
#    脚本会另外告警，说明你设的那个值 Compose 没看到。
#
# 3. .env 里是空值时，不覆盖 shell 里已有的非空值。
#    .env 的 LLM 三个变量默认就是空的（不接外部模型），用户在自己 shell 里
#    export 的 API key 不能被空值抹掉。
#
# 4. 脚本不导出的源变量（端口），按 Compose 的 ${VAR:-default} 取值：
#    已导出的 shell 值优先、再 .env、最后默认值。
#
# 5. 改了 .env 之后要**重新 source 一次**：脚本导出的是 .env 的快照，
#    shell 里那份旧值会挡住 Compose 读新的 .env。

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

# source 之前 shell 里已有的值。用来判断"用户是不是自己设过"，
# 以及"这次是不是把 .env 的新值盖到了一份旧的 shell 快照上"。
# 用 ${VAR-} 而不是 ${VAR}：变量没定义时也是空串，但不会在 set -u 下报错。
_export_docker_env_prev_mysql_password="${MYSQL_PASSWORD-}"
_export_docker_env_prev_mysql_root_password="${MYSQL_ROOT_PASSWORD-}"
_export_docker_env_prev_ro_password="${REBUILD_DW_RO_PASSWORD-}"
_export_docker_env_prev_llm_base_url="${REBUILD_LLM_BASE_URL-}"
_export_docker_env_prev_llm_api_key="${REBUILD_LLM_API_KEY-}"
_export_docker_env_prev_llm_model="${REBUILD_LLM_MODEL-}"
_export_docker_env_prev_llm_timeout="${REBUILD_LLM_TIMEOUT-}"
_export_docker_env_prev_mysql_port="${MYSQL_HOST_PORT-}"
_export_docker_env_prev_es_port="${ES_HOST_PORT-}"
_export_docker_env_prev_qdrant_port="${QDRANT_HOST_PORT-}"
# 端口和 MySQL 密码这几个还要分清"导出没导出"——见下面 _export_docker_env_state_of。
# Compose 的 ${VAR:-default} / ${VAR:?} 只在前者（没定义）才去看 .env，
# 而"shell 优先"这条只对**已导出**的值成立。

_export_docker_env_mysql_password=
_export_docker_env_mysql_root_password=
_export_docker_env_ro_password=
_export_docker_env_mysql_port=
_export_docker_env_es_port=
_export_docker_env_qdrant_port=
_export_docker_env_llm_base_url=
_export_docker_env_llm_api_key=
_export_docker_env_llm_model=
_export_docker_env_llm_timeout=
_export_docker_env_parsed=

# 一个键名能不能当变量名用（Compose 的规则：字母或下划线开头，
# 后面跟字母、数字、下划线）。
_export_docker_env_is_name() {
  case "$1" in
    ''|[0-9]*) return 1 ;;
    *[!A-Za-z0-9_]*) return 1 ;;
    *) return 0 ;;
  esac
}

# 查一个变量的值，结果放在 _export_docker_env_found（0/1）和
# _export_docker_env_found_value。
#
# 顺序必须和 Compose 一致：Compose 是另一个进程，它看得到的是宿主 shell
# **导出的**变量，没 export 的它看不见。实测（docker compose v5）：
#   * shell 里有（已导出）-> 用 shell 的，.env 里同名的键被挡住；
#   * shell 里没有 -> 用 .env 里**前面**定义过的键（后面才定义的算没有）；
#   * 两边都没有 -> 空串。
_export_docker_env_lookup() {
  local name="$1"
  local declaration slot

  _export_docker_env_found=0
  _export_docker_env_found_value=

  # 1. shell 里已导出的变量
  if declaration="$(declare -p "$name" 2>/dev/null)"; then
    case "$declaration" in
      "declare -x "*)
        _export_docker_env_found=1
        _export_docker_env_found_value="${!name}"
        return 0
        ;;
    esac
  fi

  # 2. .env 里前面定义过的键
  slot="_export_docker_env_file_value_$name"
  if eval "[ -n \"\${$slot+set}\" ]"; then
    _export_docker_env_found=1
    eval "_export_docker_env_found_value=\${$slot}"
  fi
  return 0
}

# shell 里某个变量的状态：
#   exported   —— 定义了，而且导出到环境里了（compose 看得见）
#   unexported —— 定义了，但没导出（compose 看不见）
#   unset      —— 根本没定义
#
# 为什么要分"导出没导出"：docker compose 是**另一个进程**，它只看得到
# 宿主 shell 导出到环境里的变量。shell 里 `MYSQL_PASSWORD=xxx`（没有
# export）的话，compose 根本不知道有这个名字——它用的是 .env 里的值。
#
# 所以"shell 优先、再 .env"这条规则只对 exported 成立。用 `${VAR+set}`
# 判的是"定义过"，那是另一个东西：脚本会拿一个 compose 看不见的值去
# 覆盖 .env，容器用 .env 的密码、宿主 CLI 用 shell 的密码——两个不同的
# 字符串，症状只有一句 Access denied，而两边都"没写错"。
_export_docker_env_state=unset
_export_docker_env_state_of() {
  local name="$1" declaration
  if ! declaration="$(declare -p "$name" 2>/dev/null)"; then
    _export_docker_env_state=unset
    return 0
  fi
  case "$declaration" in
    "declare -x "*) _export_docker_env_state=exported ;;
    *) _export_docker_env_state=unexported ;;
  esac
  return 0
}

# 变量名列表（内部用空格存）拼成"甲、乙、丙"。
# 告警里只出现名字，不出现值。
_export_docker_env_names() {
  local list="$1"
  list="${list# }"
  printf '%s' "${list// /、}"
}

_export_docker_env_state_of MYSQL_PASSWORD
_export_docker_env_mysql_password_state="$_export_docker_env_state"
_export_docker_env_state_of MYSQL_ROOT_PASSWORD
_export_docker_env_mysql_root_password_state="$_export_docker_env_state"
_export_docker_env_state_of MYSQL_HOST_PORT
_export_docker_env_mysql_port_state="$_export_docker_env_state"
_export_docker_env_state_of ES_HOST_PORT
_export_docker_env_es_port_state="$_export_docker_env_state"
_export_docker_env_state_of QDRANT_HOST_PORT
_export_docker_env_qdrant_port_state="$_export_docker_env_state"

# 展开值里的 $name / ${name} / ${name:-default}，结果放在
# _export_docker_env_expanded。
#
# 规则（对着 docker compose v5 实测过，不是照文档猜的）：
#   * $name / ${name}：查得到就用它的值，查不到就是空串。
#   * ${name:-default}：name 没定义**或是空串**时用 default；
#     ${name-default}：只有 name 根本没定义时才用 default（空串算数）。
#   * $$ 是字面量 $ 的写法；值里一个字面量 $ 最终表示成 $$。
#   * 单独一个 $（后面不是合法的变量名开头）表示成 $$。
#   * ${ 没有配对的 } 是无效的 .env：Compose 自己会拒绝启动，脚本也得停下来。
#
# 结果不用 $(...) 收：命令替换会把结尾的换行吃掉。
_export_docker_env_expand() {
  local raw="$1"
  local length=${#raw}
  local index=0
  local char next name default inner
  local depth close j c
  local has_default is_colon
  local found value saved

  _export_docker_env_expanded=""
  while [ "$index" -lt "$length" ]; do
    char="${raw:$index:1}"
    if [ "$char" != '$' ]; then
      _export_docker_env_expanded="${_export_docker_env_expanded}${char}"
      index=$((index + 1))
      continue
    fi

    next="${raw:$((index + 1)):1}"
    if [ "$next" = '$' ]; then
      # $$ -> $$（字面量 $）
      _export_docker_env_expanded="${_export_docker_env_expanded}\$\$"
      index=$((index + 2))
      continue
    fi

    if [ "$next" = '{' ]; then
      # 找配对的 }（默认值里可能还有 ${...}）
      depth=0
      close=-1
      j=$((index + 2))
      while [ "$j" -lt "$length" ]; do
        c="${raw:$j:1}"
        if [ "$c" = '{' ]; then
          depth=$((depth + 1))
        elif [ "$c" = '}' ]; then
          if [ "$depth" -eq 0 ]; then
            close="$j"
            break
          fi
          depth=$((depth - 1))
        fi
        j=$((j + 1))
      done
      if [ "$close" -lt 0 ]; then
        return 1
      fi
      inner="${raw:$((index + 2)):$((close - index - 2))}"
      index=$((close + 1))

      name=""
      default=""
      has_default=0
      is_colon=0
      case "$inner" in
        *:-*)
          name="${inner%%:-*}"
          default="${inner#*:-}"
          has_default=1
          is_colon=1
          ;;
        *-*)
          name="${inner%%-*}"
          default="${inner#*-}"
          has_default=1
          ;;
        *)
          name="$inner"
          ;;
      esac
      if ! _export_docker_env_is_name "$name"; then
        return 1
      fi

      _export_docker_env_lookup "$name"
      found="$_export_docker_env_found"
      value="$_export_docker_env_found_value"
      if [ "$has_default" -eq 1 ]; then
        # 默认值本身也要展开（${NOPE:-${OTHER}} 能取到 OTHER）。
        # 递归时把当前结果存起来——递归调用会覆盖同一个变量。
        if [ "$is_colon" -eq 1 ] && [ -z "$value" ]; then
          saved="$_export_docker_env_expanded"
          _export_docker_env_expand "$default" || return 1
          value="$_export_docker_env_expanded"
          _export_docker_env_expanded="$saved"
        elif [ "$is_colon" -eq 0 ] && [ "$found" -eq 0 ]; then
          saved="$_export_docker_env_expanded"
          _export_docker_env_expand "$default" || return 1
          value="$_export_docker_env_expanded"
          _export_docker_env_expanded="$saved"
        fi
      fi
      _export_docker_env_expanded="${_export_docker_env_expanded}${value}"
      continue
    fi

    # $name：名字以字母或下划线开头，后面跟字母、数字、下划线
    case "$next" in
      [A-Za-z_]*) ;;
      *)
        # 单独一个 $：表示成 $$
        _export_docker_env_expanded="${_export_docker_env_expanded}\$\$"
        index=$((index + 1))
        continue
        ;;
    esac
    j=$((index + 1))
    name=""
    while [ "$j" -lt "$length" ]; do
      c="${raw:$j:1}"
      case "$c" in
        [A-Za-z0-9_]*) name="${name}${c}" ;;
        *) break ;;
      esac
      j=$((j + 1))
    done
    index="$j"
    _export_docker_env_lookup "$name"
    _export_docker_env_expanded="${_export_docker_env_expanded}${_export_docker_env_found_value}"
  done
  return 0
}

# 按 Compose 的 dotenv 语义解析一个值，结果放在 _export_docker_env_parsed。
#
# 规则（对着 docker compose v5 实测过，不是照文档猜的）：
#   * 未加引号：去掉首尾空白；" #"（空格 + #）之后是行内注释，截断。
#     注意是"空格 + #"，制表符不算——"a<TAB>#b" 的值是 "a<TAB>#b"。
#     注释是在展开**之前**截掉的。
#   * 单引号：字面量，不处理任何转义（"\n" 就是反斜杠加 n），也不展开 $；
#     但每个 $ 都表示成 $$。
#   * 双引号：处理 \" \\ \n \t \r \$，其余的反斜杠原样保留；然后展开 $。
#   * 闭合引号之后的内容一律丢弃（'"a" 尾巴' 的值就是 "a"）。
#
# 结果不用 $(...) 收：命令替换会把结尾的换行吃掉。
_export_docker_env_parse_value() {
  local raw="$1"
  local body char next index length

  # 去掉首尾空白
  raw="${raw#"${raw%%[![:space:]]*}"}"
  raw="${raw%"${raw##*[![:space:]]}"}"

  case "$raw" in
    '"'*)
      body=""
      index=1
      length=${#raw}
      while [ "$index" -lt "$length" ]; do
        char="${raw:$index:1}"
        if [ "$char" = "\\" ]; then
          index=$((index + 1))
          next="${raw:$index:1}"
          case "$next" in
            '"')  body="${body}\"" ;;
            "\\") body="${body}\\" ;;
            "n")  body="${body}"$'\n' ;;
            "t")  body="${body}"$'\t' ;;
            "r")  body="${body}"$'\r' ;;
            '$')  body="${body}\$\$" ;;
            *)    body="${body}\\${next}" ;;
          esac
        elif [ "$char" = '"' ]; then
          break
        else
          body="${body}${char}"
        fi
        index=$((index + 1))
      done
      _export_docker_env_expand "$body" || return 1
      _export_docker_env_parsed="$_export_docker_env_expanded"
      ;;
    "'"*)
      body=""
      index=1
      length=${#raw}
      while [ "$index" -lt "$length" ]; do
        char="${raw:$index:1}"
        if [ "$char" = "'" ]; then
          break
        fi
        if [ "$char" = '$' ]; then
          # 单引号里没有展开，但字面量 $ 表示成 $$
          body="${body}\$\$"
        else
          body="${body}${char}"
        fi
        index=$((index + 1))
      done
      _export_docker_env_parsed="$body"
      ;;
    *)
      case "$raw" in
        *" #"*) raw="${raw%%" #"*}" ;;
      esac
      raw="${raw%"${raw##*[![:space:]]}"}"
      _export_docker_env_expand "$raw" || return 1
      _export_docker_env_parsed="$_export_docker_env_expanded"
      ;;
  esac
  return 0
}

_export_docker_env_line_number=0
_export_docker_env_file_keys=
while IFS= read -r _export_docker_env_line || [ -n "$_export_docker_env_line" ]; do
  _export_docker_env_line_number=$((_export_docker_env_line_number + 1))
  _export_docker_env_line="${_export_docker_env_line%$'\r'}"
  # 行首空白：Compose 会去掉，去了之后行首的 # 才是注释。
  _export_docker_env_line="${_export_docker_env_line#"${_export_docker_env_line%%[![:space:]]*}"}"
  _export_docker_env_line="${_export_docker_env_line%"${_export_docker_env_line##*[![:space:]]}"}"
  case "$_export_docker_env_line" in
    ''|'#'*) continue ;;
  esac
  _export_docker_env_key="${_export_docker_env_line%%=*}"
  # Compose 允许 "export KEY=value" 这种写法。
  case "$_export_docker_env_key" in
    export[[:space:]]*)
      _export_docker_env_key="${_export_docker_env_key#export}"
      _export_docker_env_key="${_export_docker_env_key#"${_export_docker_env_key%%[![:space:]]*}"}"
      ;;
  esac
  if ! _export_docker_env_parse_value "${_export_docker_env_line#*=}"; then
    # 提示里只有行号：值本身可能是密码，不能写进 stderr。
    printf '无效的 .env：第 %s 行的值里有没配对的 ${ 或者不合法的变量名（docker compose 也会拒绝这份文件）\n' \
      "$_export_docker_env_line_number" >&2
    unset _export_docker_env_root _export_docker_env_file \
      _export_docker_env_line _export_docker_env_line_number \
      _export_docker_env_key _export_docker_env_value \
      _export_docker_env_parsed _export_docker_env_expanded \
      _export_docker_env_found _export_docker_env_found_value
    unset -f _export_docker_env_parse_value _export_docker_env_expand \
      _export_docker_env_lookup _export_docker_env_is_name
    for _export_docker_env_cleanup_key in $_export_docker_env_file_keys; do
      unset "_export_docker_env_file_value_${_export_docker_env_cleanup_key}"
    done
    unset _export_docker_env_cleanup_key _export_docker_env_file_keys
    return 1
  fi
  _export_docker_env_value="$_export_docker_env_parsed"
  # 记下这个键的值：后面 ${...} 展开时要能查到它（只认前面定义过的键）。
  if _export_docker_env_is_name "$_export_docker_env_key"; then
    eval "_export_docker_env_file_value_${_export_docker_env_key}=\$_export_docker_env_value"
    _export_docker_env_file_keys="$_export_docker_env_file_keys $_export_docker_env_key"
  fi
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
    REBUILD_LLM_BASE_URL)
      _export_docker_env_llm_base_url="$_export_docker_env_value"
      ;;
    REBUILD_LLM_API_KEY)
      _export_docker_env_llm_api_key="$_export_docker_env_value"
      ;;
    REBUILD_LLM_MODEL)
      _export_docker_env_llm_model="$_export_docker_env_value"
      ;;
    REBUILD_LLM_TIMEOUT)
      _export_docker_env_llm_timeout="$_export_docker_env_value"
      ;;
  esac
done < "$_export_docker_env_file"

# ---------------------------------------------------------------------------
# 对账：.env 的值 vs source 之前 shell 里已有的值
#
# 脚本和 Compose 用同一个变量名的键（凭据、LLM 设置）：脚本会覆盖 shell，
# 所以这里必须定出唯一的值——两边读到的必须是同一个字符串。
#
#   * .env 有值            -> 用 .env 的（.env 是这份部署的事实源）
#   * .env 没值            -> 沿用 shell 里的（别用空值把用户自己设的抹掉）
#   * 两边都有值还不一样   -> 用 .env 的，并告警
#
# 结果放在 _export_docker_env_resolved，冲突的变量名记在 _export_docker_env_conflicts。
_export_docker_env_resolved=
_export_docker_env_conflicts=
_export_docker_env_conflicts_shell=
_export_docker_env_unexported=

_export_docker_env_resolve_shared() {
  local file_value="$1" prev_value="$2" name="$3"
  if [ -n "$file_value" ]; then
    if [ -n "$prev_value" ] && [ "$file_value" != "$prev_value" ]; then
      _export_docker_env_conflicts="${_export_docker_env_conflicts} ${name}"
    fi
    _export_docker_env_resolved="$file_value"
  else
    _export_docker_env_resolved="$prev_value"
  fi
}

# 照抄 Compose 的 ${VAR:?}：shell 优先、再 .env。
#
# 用在这一族变量（MYSQL_PASSWORD / MYSQL_ROOT_PASSWORD）上，是因为脚本
# **不导出**这两个名字——脚本用同名变量 export 出去的话，shell 里那份就
# 挡住了 .env 里的新值：用户改了 .env（轮换密码）之后，容器用的还是旧
# 密码，宿主 CLI 用的是新密码，两边对不上，症状只有一句 Access denied。
#
# "shell 优先"只认**已导出**的值：compose 是另一个进程，只看得到环境里
# 的变量。shell 里定义了但没 export 的值，compose 看不见它，用的是 .env
# 里的——脚本也得用 .env 的那个，否则两边不是同一个字符串。
#
# 值为空（已导出的空串也算）和没定义一样：Compose 的 :? 会报错，
# 脚本也得跟着报错，不能偷偷换成另一个值——那会让两边拿到不同的字符串。
_export_docker_env_resolve_required() {
  local file_value="$1" prev_value="$2" state="$3" name="$4"
  if [ "$state" = "exported" ]; then
    _export_docker_env_resolved="$prev_value"
    if [ -n "$prev_value" ] && [ -n "$file_value" ] && [ "$prev_value" != "$file_value" ]; then
      _export_docker_env_conflicts_shell="${_export_docker_env_conflicts_shell} ${name}"
    fi
  else
    _export_docker_env_resolved="$file_value"
    if [ "$state" = "unexported" ] && [ -n "$prev_value" ] && [ -n "$file_value" ] \
      && [ "$prev_value" != "$file_value" ]; then
      _export_docker_env_unexported="${_export_docker_env_unexported} ${name}"
    fi
  fi
}

# 脚本读 .env 里的源变量、导出成另一个名字的键（端口）。
# Compose 用的是 ${VAR:-default}：shell 里**已导出**了就用 shell 的（含空串），
# 没导出才看 .env，最后空值退化成默认值。这里必须照抄那个顺序。
_export_docker_env_resolve_port() {
  local file_value="$1" prev_value="$2" state="$3" default_value="$4" name="$5"
  if [ "$state" = "exported" ]; then
    _export_docker_env_resolved="${prev_value:-$default_value}"
    if [ -n "$prev_value" ] && [ -n "$file_value" ] && [ "$prev_value" != "$file_value" ]; then
      _export_docker_env_conflicts_shell="${_export_docker_env_conflicts_shell} ${name}"
    fi
  else
    _export_docker_env_resolved="${file_value:-$default_value}"
    if [ "$state" = "unexported" ] && [ -n "$prev_value" ] && [ -n "$file_value" ] \
      && [ "$prev_value" != "$file_value" ]; then
      _export_docker_env_unexported="${_export_docker_env_unexported} ${name}"
    fi
  fi
}

_export_docker_env_resolve_required \
  "$_export_docker_env_mysql_password" \
  "$_export_docker_env_prev_mysql_password" \
  "$_export_docker_env_mysql_password_state" MYSQL_PASSWORD
_export_docker_env_mysql_password="$_export_docker_env_resolved"

_export_docker_env_resolve_required \
  "$_export_docker_env_mysql_root_password" \
  "$_export_docker_env_prev_mysql_root_password" \
  "$_export_docker_env_mysql_root_password_state" MYSQL_ROOT_PASSWORD
_export_docker_env_mysql_root_password="$_export_docker_env_resolved"

_export_docker_env_resolve_shared \
  "$_export_docker_env_ro_password" \
  "$_export_docker_env_prev_ro_password" REBUILD_DW_RO_PASSWORD
_export_docker_env_ro_password="$_export_docker_env_resolved"

_export_docker_env_resolve_shared \
  "$_export_docker_env_llm_base_url" \
  "$_export_docker_env_prev_llm_base_url" REBUILD_LLM_BASE_URL
_export_docker_env_llm_base_url="$_export_docker_env_resolved"

_export_docker_env_resolve_shared \
  "$_export_docker_env_llm_api_key" \
  "$_export_docker_env_prev_llm_api_key" REBUILD_LLM_API_KEY
_export_docker_env_llm_api_key="$_export_docker_env_resolved"

_export_docker_env_resolve_shared \
  "$_export_docker_env_llm_model" \
  "$_export_docker_env_prev_llm_model" REBUILD_LLM_MODEL
_export_docker_env_llm_model="$_export_docker_env_resolved"

_export_docker_env_resolve_shared \
  "$_export_docker_env_llm_timeout" \
  "$_export_docker_env_prev_llm_timeout" REBUILD_LLM_TIMEOUT
_export_docker_env_llm_timeout="$_export_docker_env_resolved"

_export_docker_env_resolve_port \
  "$_export_docker_env_mysql_port" \
  "$_export_docker_env_prev_mysql_port" \
  "$_export_docker_env_mysql_port_state" 3306 MYSQL_HOST_PORT
_export_docker_env_mysql_port="$_export_docker_env_resolved"

_export_docker_env_resolve_port \
  "$_export_docker_env_es_port" \
  "$_export_docker_env_prev_es_port" \
  "$_export_docker_env_es_port_state" 9200 ES_HOST_PORT
_export_docker_env_es_port="$_export_docker_env_resolved"

_export_docker_env_resolve_port \
  "$_export_docker_env_qdrant_port" \
  "$_export_docker_env_prev_qdrant_port" \
  "$_export_docker_env_qdrant_port_state" 6333 QDRANT_HOST_PORT
_export_docker_env_qdrant_port="$_export_docker_env_resolved"

_export_docker_env_missing=
_export_docker_env_missing_is_shell=
_export_docker_env_missing_is_unexported=
if [ -z "$_export_docker_env_mysql_password" ]; then
  _export_docker_env_missing=MYSQL_PASSWORD
  [ "$_export_docker_env_mysql_password_state" != "exported" ] \
    || _export_docker_env_missing_is_shell=1
  [ "$_export_docker_env_mysql_password_state" != "unexported" ] \
    || _export_docker_env_missing_is_unexported=1
elif [ -z "$_export_docker_env_mysql_root_password" ]; then
  _export_docker_env_missing=MYSQL_ROOT_PASSWORD
  [ "$_export_docker_env_mysql_root_password_state" != "exported" ] \
    || _export_docker_env_missing_is_shell=1
  [ "$_export_docker_env_mysql_root_password_state" != "unexported" ] \
    || _export_docker_env_missing_is_unexported=1
elif [ -z "$_export_docker_env_ro_password" ]; then
  _export_docker_env_missing=REBUILD_DW_RO_PASSWORD
fi

if [ -n "$_export_docker_env_missing" ]; then
  if [ -n "$_export_docker_env_missing_is_shell" ]; then
    # shell 里定义成了空串：Compose 的 ${VAR:?} 把空串当没有，它自己就起不来。
    # 脚本不能偷偷换成 .env 里的值——那会让两边拿到不同的字符串。
    printf 'shell 里的 %s 是空值；docker compose 也会因此起不来（它优先读 shell）。\n' \
      "$_export_docker_env_missing" >&2
    printf '请 unset %s 或者给它一个值，再重新 source 一次。\n' \
      "$_export_docker_env_missing" >&2
  elif [ -n "$_export_docker_env_missing_is_unexported" ]; then
    # shell 里定义了但没 export：compose 看不见它，所以 .env 里没有值时
    # compose 自己就起不来。脚本不能拿一个 compose 看不见的值去顶上。
    printf 'shell 里的 %s 没有 export，docker compose 看不到它；.env 里也没有这个值。\n' \
      "$_export_docker_env_missing" >&2
    printf '请 export %s 或者把它写进 .env，再重新 source 一次。\n' \
      "$_export_docker_env_missing" >&2
  else
    printf '缺少必填项 %s（.env 和 shell 环境里都没有）\n' "$_export_docker_env_missing" >&2
  fi
  unset _export_docker_env_root _export_docker_env_file \
    _export_docker_env_line _export_docker_env_line_number \
    _export_docker_env_key _export_docker_env_value \
    _export_docker_env_mysql_password _export_docker_env_mysql_root_password \
    _export_docker_env_ro_password _export_docker_env_mysql_port \
    _export_docker_env_es_port _export_docker_env_qdrant_port \
    _export_docker_env_llm_base_url _export_docker_env_llm_api_key \
    _export_docker_env_llm_model _export_docker_env_llm_timeout \
    _export_docker_env_parsed _export_docker_env_expanded _export_docker_env_missing \
    _export_docker_env_found _export_docker_env_found_value \
    _export_docker_env_missing_is_shell \
    _export_docker_env_missing_is_unexported \
    _export_docker_env_prev_mysql_password _export_docker_env_prev_mysql_root_password \
    _export_docker_env_prev_ro_password _export_docker_env_prev_llm_base_url \
    _export_docker_env_prev_llm_api_key _export_docker_env_prev_llm_model \
    _export_docker_env_prev_llm_timeout _export_docker_env_prev_mysql_port \
    _export_docker_env_prev_es_port _export_docker_env_prev_qdrant_port \
    _export_docker_env_mysql_port_state _export_docker_env_es_port_state \
    _export_docker_env_qdrant_port_state _export_docker_env_resolved \
    _export_docker_env_mysql_password_state \
    _export_docker_env_mysql_root_password_state \
    _export_docker_env_state \
    _export_docker_env_conflicts _export_docker_env_conflicts_shell \
    _export_docker_env_unexported
  unset -f _export_docker_env_parse_value _export_docker_env_expand \
    _export_docker_env_lookup _export_docker_env_is_name \
    _export_docker_env_state_of _export_docker_env_names \
    _export_docker_env_resolve_shared _export_docker_env_resolve_port \
    _export_docker_env_resolve_required
  for _export_docker_env_cleanup_key in $_export_docker_env_file_keys; do
    unset "_export_docker_env_file_value_${_export_docker_env_cleanup_key}"
  done
  unset _export_docker_env_cleanup_key _export_docker_env_file_keys
  return 1
fi

if [ -n "$_export_docker_env_conflicts" ]; then
  printf '警告：shell 里的%s与 .env 不一致，已改用 .env 的值。\n' \
    "$(_export_docker_env_names "$_export_docker_env_conflicts")" >&2
fi
if [ -n "$_export_docker_env_conflicts_shell" ]; then
  # 这句"与 docker compose 一致"只对**已导出**的变量成立：导出了，
  # compose 才看得见这个值、才会用同一个字符串。
  printf '警告：shell 里的%s与 .env 不一致，已按 shell 的取值（与 docker compose 一致）。\n' \
    "$(_export_docker_env_names "$_export_docker_env_conflicts_shell")" >&2
fi
if [ -n "$_export_docker_env_unexported" ]; then
  # 没 export 的值 compose 看不见：容器用的是 .env 里的，脚本也用 .env 里的。
  # 这里不能说"与 docker compose 一致"——脚本取的是 .env 的值，和 compose
  # 一致的那句话对**这个 shell 变量**是不成立的。
  printf '警告：shell 里的%s没有 export，docker compose 看不到它；已改用 .env 的值。\n' \
    "$(_export_docker_env_names "$_export_docker_env_unexported")" >&2
fi

export REBUILD_MYSQL_HOST=127.0.0.1
export REBUILD_MYSQL_PORT="$_export_docker_env_mysql_port"
export REBUILD_MYSQL_USER=didilili
export REBUILD_MYSQL_PASSWORD="$_export_docker_env_mysql_password"
export REBUILD_MYSQL_ROOT_USER=root
export REBUILD_MYSQL_ROOT_PASSWORD="$_export_docker_env_mysql_root_password"
export REBUILD_DW_RO_PASSWORD="$_export_docker_env_ro_password"
export REBUILD_ES_URL="http://127.0.0.1:${_export_docker_env_es_port}"
export REBUILD_QDRANT_URL="http://127.0.0.1:${_export_docker_env_qdrant_port}"
# 外部 LLM：不导出的话，宿主 CLI 就用不了外部模型，和容器里的行为对不上。
# 取值是空的时候不导出：留着变量名会让 Compose 优先读 shell 里的空值，
# 把 .env 里后来填上的值挡在门外。
if [ -n "$_export_docker_env_llm_base_url" ]; then
  export REBUILD_LLM_BASE_URL="$_export_docker_env_llm_base_url"
fi
if [ -n "$_export_docker_env_llm_api_key" ]; then
  export REBUILD_LLM_API_KEY="$_export_docker_env_llm_api_key"
fi
if [ -n "$_export_docker_env_llm_model" ]; then
  export REBUILD_LLM_MODEL="$_export_docker_env_llm_model"
fi
export REBUILD_LLM_TIMEOUT="${_export_docker_env_llm_timeout:-30}"

# 刻意**不**导出 MYSQL_PASSWORD / MYSQL_ROOT_PASSWORD。
#
# Compose 解析 ${MYSQL_PASSWORD} 是"shell 优先、再 .env"。脚本用同名变量
# export 出去，shell 里那份就挡住了 .env 里的新值：用户改了 .env（轮换
# 密码）之后，容器读到的还是旧密码，宿主 CLI 读到的是新密码——两边不是
# 同一个字符串，症状只有一句莫名其妙的 Access denied。
#
# 不导出，Compose 下次读 .env 时读到的就是新值；脚本这边也已经按
# Compose 的顺序（shell 优先、再 .env）取好了同一个字符串。

unset _export_docker_env_root _export_docker_env_file \
  _export_docker_env_line _export_docker_env_line_number \
  _export_docker_env_key _export_docker_env_value \
  _export_docker_env_mysql_password _export_docker_env_mysql_root_password \
  _export_docker_env_ro_password _export_docker_env_mysql_port \
  _export_docker_env_es_port _export_docker_env_qdrant_port \
  _export_docker_env_llm_base_url _export_docker_env_llm_api_key \
  _export_docker_env_llm_model _export_docker_env_llm_timeout \
  _export_docker_env_parsed _export_docker_env_expanded _export_docker_env_missing \
  _export_docker_env_found _export_docker_env_found_value \
  _export_docker_env_missing_is_shell \
  _export_docker_env_missing_is_unexported \
  _export_docker_env_prev_mysql_password _export_docker_env_prev_mysql_root_password \
  _export_docker_env_prev_ro_password _export_docker_env_prev_llm_base_url \
  _export_docker_env_prev_llm_api_key _export_docker_env_prev_llm_model \
  _export_docker_env_prev_llm_timeout _export_docker_env_prev_mysql_port \
  _export_docker_env_prev_es_port _export_docker_env_prev_qdrant_port \
  _export_docker_env_mysql_port_state _export_docker_env_es_port_state \
  _export_docker_env_qdrant_port_state _export_docker_env_resolved \
  _export_docker_env_mysql_password_state \
  _export_docker_env_mysql_root_password_state \
  _export_docker_env_state \
  _export_docker_env_conflicts _export_docker_env_conflicts_shell \
  _export_docker_env_unexported
unset -f _export_docker_env_parse_value _export_docker_env_expand \
  _export_docker_env_lookup _export_docker_env_is_name \
  _export_docker_env_state_of _export_docker_env_names \
  _export_docker_env_resolve_shared _export_docker_env_resolve_port \
  _export_docker_env_resolve_required

# .env 里每个键都留了一份展开后的值在 shell 里（展开 ${...} 时要用）。
# 用完清掉：source 一次不该在用户的 shell 里留一堆变量。
for _export_docker_env_cleanup_key in $_export_docker_env_file_keys; do
  unset "_export_docker_env_file_value_${_export_docker_env_cleanup_key}"
done
unset _export_docker_env_cleanup_key _export_docker_env_file_keys

return 0
