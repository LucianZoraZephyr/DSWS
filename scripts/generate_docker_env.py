#!/usr/bin/env python3
"""从 .env.example 生成带随机密码的 .env。"""

from __future__ import annotations

import argparse
import os
import re
import secrets
import stat
import sys
import tempfile
from collections.abc import Sequence
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
EXAMPLE = ROOT / ".env.example"
DEFAULT_TARGET = ROOT / ".env"

# 本脚本负责生成的密码。
SECRET_KEYS = (
    "MYSQL_ROOT_PASSWORD",
    "MYSQL_PASSWORD",
    "REBUILD_DW_RO_PASSWORD",
)

# 需要在 --dry-run 里打码的键：生成的密码 + 用户自己填进去的外部 API Key。
# 两者都是密钥，只打码前者的话，--dry-run 会把 LLM 的 Key 原样打到终端上。
REDACT_KEYS = SECRET_KEYS + ("REBUILD_LLM_API_KEY",)

REDACTED = "***"

# 名单是固定的写法必然漏：旧 .env 里常留着模板没有的第三方密钥
# （DEEPSEEK_API_KEY 之类），而这些键会被原样搬进新文件。所以除了名单，
# 还要按**名字**保守地打码——凡是键名像是密钥的，一律打码。
#
# 宁可多打码（预览里少看一个端口），不可少打码（密钥进了终端记录）。
_SECRET_NAME = re.compile(r"(?i)(pass|secret|token|key|credential|dsn|auth)")

# 值里带账号密码的连接串：键名未必可疑，值本身却是密钥。
# 只打掉密码那一段，scheme 和用户名留着——预览还得看得出连的是谁。
_CREDENTIAL_URL = re.compile(
    r"(?P<scheme>[A-Za-z][A-Za-z0-9+.\-]*://)(?P<user>[^:/@\s]*):(?P<password>[^@/\s]*)@"
)

_KEY_LINE = re.compile(r"^([A-Za-z_][A-Za-z0-9_]*)=", re.MULTILINE)

_ROTATE_WARNING = """\
警告：已重新生成密码。
MySQL 只在**首次**初始化 mysql_data 数据卷时用 MYSQL_ROOT_PASSWORD 建账号，
数据卷还在的话，容器里的密码仍然是旧的那个——新密码不会生效，
mysql 的健康检查会一直失败，init 和 backend 也就都起不来。
请先执行 `make docker-reset`（删除数据卷），再重新 `docker compose up`。\
"""


def _replace(text: str, key: str, value: str) -> str:
    pattern = re.compile(rf"^{re.escape(key)}=.*$", re.MULTILINE)
    if pattern.search(text) is None:
        raise SystemExit(f".env.example 缺少 {key}= 行")
    # 用 lambda 替换：value 里带的反斜杠会被 re.sub 当成转义序列解释掉
    # （"a\1b" 会被替换成 "a" + 第一个分组）。
    return pattern.sub(lambda _match: f"{key}={value}", text, count=1)


def parse_env(text: str) -> dict[str, str]:
    """把一份 .env 解析成 key -> value。

    只认 ``KEY=VALUE`` 这种最朴素的写法，值原样保留（引号也留着）：
    **最终解释这个文件的是 Compose**，它的 dotenv 语义（行内注释、
    引号里的转义）不该在这里被"猜"一遍——猜错了，宿主机 CLI 和容器
    就会拿着两个不同的密码。
    """
    values: dict[str, str] = {}
    for line in text.splitlines():
        stripped = line.strip()
        if not stripped or stripped.startswith("#"):
            continue
        key, separator, value = line.partition("=")
        if not separator:
            continue
        key = key.strip()
        if key.startswith("export "):
            key = key[len("export ") :].strip()
        values[key] = value.strip()
    return values


def render(
    example: str,
    existing: dict[str, str] | None = None,
    *,
    rotate: bool = False,
) -> str:
    """把模板渲染成最终的 .env 内容。

    * **已有的值一律保留**，密码也不例外。MySQL 只在首次初始化数据卷时
      用 ``MYSQL_ROOT_PASSWORD`` 建账号；数据卷一旦存在，容器里的密码就
      固定成当初那一个。``.env`` 里的密码跟着换、容器里的不换，
      mysql 的健康检查就永远失败，整栈起不来——症状还指向健康检查，
      真正的原因一个字都不提。
    * ``rotate=True``（``--rotate-secrets``）才重新生成密码。
    * 模板里有、旧文件里没有的键：非密码用模板值，密码用随机值。
    * **旧文件里有、模板里没有的键：原样保留**（例如
      ``COMPOSE_PROJECT_NAME``）。丢掉它，Compose 的项目名就变回默认值，
      数据卷的名字跟着变——用户的卷"消失"了，栈却从一个空卷重新起。
    """
    existing = existing or {}
    text = example

    for key in _KEY_LINE.findall(example):
        if key in SECRET_KEYS:
            continue
        kept = existing.get(key, "")
        if kept:
            text = _replace(text, key, kept)

    for key in SECRET_KEYS:
        kept = "" if rotate else existing.get(key, "")
        value = kept or secrets.token_urlsafe(24)
        text = _replace(text, key, value)

    return _append_extra_keys(text, example, existing)


def _append_extra_keys(text: str, example: str, existing: dict[str, str]) -> str:
    """把"旧 .env 里有、模板里没有"的键补到末尾。

    顺序沿用旧文件里的先后（``parse_env`` 保序），值原样保留。
    """
    template_keys = set(_KEY_LINE.findall(example))
    extra = [
        (key, value) for key, value in existing.items() if key not in template_keys
    ]
    if not extra:
        return text
    lines = text.rstrip("\n").splitlines()
    lines.append("")
    lines.append("# 以下键只存在于旧的 .env，模板里没有；保留下来，别被悄悄丢掉。")
    lines.extend(f"{key}={value}" for key, value in extra)
    return "\n".join(lines) + "\n"


def _mask_url_credentials(match: re.Match[str]) -> str:
    return f"{match.group('scheme')}{match.group('user')}:{REDACTED}@"


def redact(text: str) -> str:
    """把密钥的值换成 `***`——只给 `--dry-run` 用。

    密钥不该出现在终端记录、CI 日志或粘贴板里；`--dry-run` 的用途是
    "看一眼要写什么"，不是"把密钥读出来"。

    打码范围是**保守**的：

    * 名单里的键（生成的密码 + 外部 API Key）；
    * 名字像是密钥的键（pass/secret/token/key/...）——旧 .env 里
      模板没有的第三方密钥就是这么漏出去的；
    * 值里带账号密码的连接串——只打掉密码那一段。

    打码了不该打码的键，最坏结果是预览里少看一个端口；漏打码一个密钥，
    后果是它进了终端历史、CI 日志和粘贴板。
    """
    trailing = "\n" if text.endswith("\n") else ""
    lines = []
    for line in text.splitlines():
        stripped = line.strip()
        if not stripped or stripped.startswith("#"):
            # 空行和注释：注释里的 "=" 不是键值分隔符。
            lines.append(line)
            continue
        key, separator, value = line.partition("=")
        if not separator:
            lines.append(line)
            continue
        name = key.strip()
        # Compose 允许 "export KEY=value" 这种写法——前缀不能把打码绕过去。
        if name.startswith("export "):
            name = name[len("export ") :].strip()
        if name in REDACT_KEYS or _SECRET_NAME.search(name):
            lines.append(f"{key}={REDACTED}")
        else:
            lines.append(f"{key}={_CREDENTIAL_URL.sub(_mask_url_credentials, value)}")
    return "\n".join(lines) + trailing


def _write_private(path: Path, text: str) -> None:
    """**原子地**写出 .env：先写同目录的临时文件，再整体挪过去。

    两个性质各管一件事：

    * **创建即私密**——临时文件从建出来那一刻就是 0600。先 `write_text`
      再 `chmod` 的写法，中间那几毫秒里密码是全局可读的；容器化之后这个
      文件会被 Compose 读进所有服务，更不该留这个窗口。
    * **落盘不留半个文件**——`os.replace` 在 POSIX 和 Windows 上都是原子
      的：读者要么看到旧的完整文件，要么看到新的完整文件。

    原来的顺序是"先删旧的、再建新的"。这两步之间出了任何岔子（磁盘满、
    权限被改、Ctrl-C），磁盘上就**一个 .env 都不剩**——而那份文件里有
    MySQL 的密码、用户的 LLM API Key、COMPOSE_PROJECT_NAME。密码能重新
    生成，API Key 生成不了：用户得翻回聊天记录里把它找回来。
    """
    handle, temp_name = tempfile.mkstemp(
        dir=str(path.parent), prefix=f".{path.name}.", suffix=".tmp"
    )
    temp_path = Path(temp_name)
    try:
        with os.fdopen(handle, "w", encoding="utf-8", newline="\n") as stream:
            stream.write(text)
        try:
            # mkstemp 建出来就是 0600；这里再 chmod 一次，是把"创建即私密"
            # 这件事写死在代码里——将来有人动了 mkstemp 的 mode 也拦得住。
            temp_path.chmod(stat.S_IRUSR | stat.S_IWUSR)
        except OSError:
            # Windows 上 chmod 只影响只读位；文件内容仍是正确的。
            pass
        _replace_atomic(temp_path, path)
    except BaseException:
        # 失败时把临时文件收走，别在仓库根目录留一堆 .env.*.tmp。
        temp_path.unlink(missing_ok=True)
        raise


def _replace_atomic(source: Path, target: Path) -> None:
    """把 source 原子地挪到 target 的位置。

    Windows 上目标文件是只读时，replace 会被系统拒绝（ERROR_ACCESS_DENIED）：
    先把它改回可写再重试一次。
    """
    try:
        os.replace(source, target)
    except PermissionError:
        target.chmod(stat.S_IRUSR | stat.S_IWUSR)
        os.replace(source, target)


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="生成 Docker Compose 的 .env（随机密码）"
    )
    parser.add_argument(
        "--force",
        action="store_true",
        help="覆盖已存在的 .env（保留已有的密码与配置）",
    )
    parser.add_argument(
        "--rotate-secrets",
        action="store_true",
        help="重新生成密码（旧密码会失效，需要先重置数据卷）",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="只打印生成结果（密钥显示为 ***），不写文件",
    )
    parser.add_argument(
        "--output",
        type=Path,
        default=DEFAULT_TARGET,
        help="输出路径（默认仓库根 .env）",
    )
    args = parser.parse_args(argv)

    if not EXAMPLE.is_file():
        print(f"缺少模板：{EXAMPLE}", file=sys.stderr)
        return 1

    existing_text = ""
    if args.output.exists():
        try:
            existing_text = args.output.read_text(encoding="utf-8")
        except OSError as error:
            print(f"读不了 {args.output}：{error}", file=sys.stderr)
            return 1
        if not (args.force or args.rotate_secrets or args.dry_run):
            print(
                f"{args.output} 已存在；如需覆盖请加 --force",
                file=sys.stderr,
            )
            return 1

    existing = parse_env(existing_text)
    text = render(
        EXAMPLE.read_text(encoding="utf-8"),
        existing,
        rotate=args.rotate_secrets,
    )

    if args.dry_run:
        if args.output.exists() and not (args.force or args.rotate_secrets):
            print(
                f"（预览：{args.output} 已存在，实际写入需要 --force）",
                file=sys.stderr,
            )
        print(redact(text), end="")
        return 0

    # 覆盖写是原子的：旧文件一直都在，直到新内容整个就位。
    _write_private(args.output, text)
    if args.rotate_secrets:
        # 无条件提醒：数据卷可不管 .env 在不在。卷还在（没跑过
        # make docker-reset）的时候换密码，容器里的密码仍然是旧的，
        # 健康检查会一直失败，init 和 backend 都起不来。
        print(_ROTATE_WARNING, file=sys.stderr)
    print(f"已生成 {args.output}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
