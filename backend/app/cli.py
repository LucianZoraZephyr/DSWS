"""CLI 公共入口：把标准输出固定成 UTF-8。

Windows 的默认控制台代码页是 GBK（cp936），Python 在管道 / 控制台下按
locale 编码写 stdout。项目里的提示信息含中文和 `↔` 这类符号，直接 print
会抛：

    UnicodeEncodeError: 'gbk' codec can't encode character '\\u2194'

于是 `make stage1` 在任何 GBK 控制台上都跑不完——这不是环境问题，是代码
缺陷：**契约的验收命令不应该是平台相关的**。

在每个 CLI 的 `main()` 开头调用 `enable_utf8_stdio()` 即可。`errors="replace"`
兜底，保证即使遇到无法编码的字符也只是替换而不是崩溃——**验收命令要么
成功，要么给出真实的失败原因，不许因为编码问题崩掉**。
"""

from __future__ import annotations

import sys


def enable_utf8_stdio() -> None:
    """把 stdout / stderr 切到 UTF-8；对不支持 reconfigure 的流静默跳过。"""
    for stream in (sys.stdout, sys.stderr):
        reconfigure = getattr(stream, "reconfigure", None)
        if reconfigure is None:
            continue
        try:
            reconfigure(encoding="utf-8", errors="replace")
        except (ValueError, OSError, AttributeError):  # pragma: no cover
            pass
