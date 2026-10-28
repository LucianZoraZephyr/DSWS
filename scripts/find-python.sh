#!/bin/sh
# 打印一个**真的能执行**、而且版本够新的 Python 解释器路径；
# 一个都没有就以 1 退出。
#
# 为什么不能只看"名字在不在 PATH 上"：
# Windows 上 C:\Users\<用户>\AppData\Local\Microsoft\WindowsApps\python3
# 是 Microsoft Store 的转发器（AppInstallerPythonRedirector.exe）——文件确实存在，
# command -v 也查得到，但一执行就以 49 退出、什么也不输出。挑中它之后：
#
#   * make docker-env 以 "Error 49" 收场，看不出是自己的 python3 是假的；
#   * docker-smoke.sh 会在 180 次健康检查重试里空转 6 分钟，最后报
#     "360 秒内 /api/health 未就绪"——报错指向健康检查，真正的原因
#     （解释器根本跑不起来）被埋在日志里。
#
# 所以判定标准是"实际执行一次"：能 import sys 的第一个候选就是答案。
#
# 还要看版本："跑得起来"不等于"跑得了这个仓库的脚本"。跑在宿主机上的
# 两个脚本（generate_docker_env.py、smoke_checks.py）用了 f-string 和
# Path.unlink(missing_ok=True)——后者是 3.8 才有的。装过老 Python 的机器上，
# `python` 常常指向 2.7 或 3.6：它 import sys 是成功的（探活会通过），
# 却跑不了这些脚本，用户看到的是一句 SyntaxError 或 AttributeError，
# 指向的是脚本，而不是"你的 Python 太老了"。
#
# 用法：PY="$(sh scripts/find-python.sh)" || { echo "..." >&2; exit 1; }

set -u

# 宿主机脚本需要的最低版本（不是后端要求的 3.12——后端跑在容器里）。
MIN_VERSION="3.8"

too_old=""

for candidate in python3 python; do
  path="$(command -v "$candidate" 2>/dev/null)" || continue
  [ -n "$path" ] || continue
  # 一次问清楚两件事：能不能跑、版本够不够新。
  # 输出全部丢掉——stdout 上只留解释器的路径。
  if "$path" -c 'import sys; raise SystemExit(0 if sys.version_info >= (3, 8) else 1)' \
    >/dev/null 2>&1; then
    printf '%s\n' "$path"
    exit 0
  fi
  # 没通过。再问一次版本：问得到版本 = 它跑得起来，只是太老；
  # 问不到 = 压根跑不起来（Windows 上 Store 转发器就是这种）。
  version="$("$path" -c 'import sys; print("%d.%d" % sys.version_info[:2])' 2>/dev/null || true)"
  if [ -n "$version" ]; then
    too_old="$too_old $candidate($version)"
  fi
done

# 诊断信息放在这里而不是 Makefile 配方里：Windows 上 make 的配方会经过
# ANSI 代码页转换，中文会变成乱码，个别情况下还会把引号吃掉，直接让 shell
# 报语法错。脚本文件是被 shell 直接读的，字节原样输出。
if [ -n "$too_old" ]; then
  echo "找到的 Python 版本太老（需要 ${MIN_VERSION} 或更高）：$too_old" >&2
fi
echo "找不到可用的 Python 解释器（python3 / python 都跑不起来，或版本低于 ${MIN_VERSION}）：生成 .env 需要 Python ${MIN_VERSION} 以上。" >&2
echo "Windows 上 AppData/Local/Microsoft/WindowsApps 里的 python3 是 Microsoft Store 的转发器，跑不起来；请装好 Python ${MIN_VERSION}+ 并确认 python3 能真正执行。" >&2
exit 1
