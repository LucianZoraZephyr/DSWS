#!/bin/sh
# 打印 scripts/banners.txt 里的一条横幅。
#
# 为什么横幅不直接写在 Makefile 里：Windows 上 mingw-make 构造 Windows 命令行时，
# 会把配方里的字节按**系统 ANSI 代码页**（中文 Windows 是 cp936 / GBK）解码一次。
# UTF-8 的中文因此被当成 GBK 读，印出来是乱码；个别情况下那一段字节会被整段
# 替换掉，连带的引号一起被吃掉，shell 于是看到一个不配对的引号：
#
#     >&2;: -c: line 4: unexpected EOF while looking for matching `"'
#
# 放在文件里、由 shell 直接读，字节原样输出，不经过 Windows 命令行这一层。
#
# 用法：sh scripts/say.sh <键>

set -u

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
BANNERS="$ROOT/scripts/banners.txt"

key="${1:-}"
if [ -z "$key" ]; then
  echo "用法: sh scripts/say.sh <键>" >&2
  exit 2
fi

# 键会被拼进 sed 的正则，这里先挡掉正则元字符，免得一个手滑的键匹配到别人。
case "$key" in
  *[!A-Za-z0-9._-]*)
    echo "横幅键 '$key' 含有非法字符（只允许字母、数字、. _ -）" >&2
    exit 2
    ;;
esac

if [ ! -f "$BANNERS" ]; then
  echo "找不到横幅文件: $BANNERS" >&2
  exit 1
fi

# 键要拼进 sed 的正则，所以先把正则元字符转义掉。上面的字符白名单里
# 只有 `.` 是元字符（正则里它匹配"任意一个字符"）：不转义的话，键
# "a.b" 会把 "aXb" 一起匹上，印出来的是**别人的**横幅。
escaped_key="$(printf '%s' "$key" | sed 's/\./\\./g')"

line="$(sed -n "s/^${escaped_key}|//p" "$BANNERS" | head -n 1)"
if [ -z "$line" ]; then
  echo "banners.txt 里没有键 '$key'" >&2
  exit 1
fi

printf '%s\n' "$line"
