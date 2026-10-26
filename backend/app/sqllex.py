"""S4 / S5 共用的 SQL 词法层。

AST 解析不是万能的，两个洞必须由词法层补上：

    1. MySQL 的**可执行注释** `/*! ... */`：服务端会执行注释里的内容，
       而 sqlglot 把它当普通注释忽略掉。实测
       `SELECT 1; /*! DROP TABLE t */` 解析出来只有一条 Select——
       AST 完全看不见那条 DROP。这正是「注释绕过」的入口。
    2. `SELECT a INTO OUTFILE '/tmp/x' FROM t` 在 MySQL 方言里**根本
       解析不了**，sqlglot 直接抛 ParseError，于是它只是「语法错」，
       而不是「不是只读」。这两种拒绝在结果上一样（都不会被执行），
       但**错误码不一样**，而错误码是契约的一部分。

所以词法判断的顺序是固定的：

    先遮蔽字符串字面量与反引号标识符 → 再剥注释 → 最后按**词边界**扫关键字。

先遮蔽字面量是必须的：`SELECT 'DROP TABLE'` 里的 DROP 是数据，不是语句。
用词边界（`\b`）而不是子串匹配，是为了不误伤 `created_at`、`last_update`
这类合法列名。

这个模块被两处共用：

    - app.generate.guard   （S4：生成层的静态校验）
    - app.security.policy  （S5：执行前的安全策略）

S4 与 S5 在依赖图里是**并行**的两条线（路线图 §3 原则 3），所以词法层必须
放在中立位置——不能让任何一边 import 另一边。
"""

from __future__ import annotations

import re

# 字符串字面量（单引号 / 双引号）与反引号标识符。
# 单引号内部用两个单引号转义，反引号同理。
_LITERAL_RE = re.compile(
    r"'(?:''|[^'])*'|\"(?:\"\"|[^\"])*\"|`(?:``|[^`])*`",
    re.DOTALL,
)

# MySQL 可执行注释：`/*!` 之后的内容服务端会执行。
_EXECUTABLE_COMMENT_RE = re.compile(r"/\*!")

# 普通注释：块注释 + 行注释。
# 行注释用 `--` 起头（MySQL 要求其后紧跟空白，这里放宽到行尾，
# 与 SQL 标准实现一致；真正决定成败的是 AST 解析，词法层只是补漏）。
_BLOCK_COMMENT_RE = re.compile(r"/\*.*?\*/", re.DOTALL)
_LINE_COMMENT_RE = re.compile(r"--[^\n]*")


def mask_literals(text: str) -> str:
    """把字面量与反引号标识符替换成等长空格。

    保持长度不变是有意的：调用方有时还要靠偏移量定位原文。
    """
    return _LITERAL_RE.sub(lambda match: " " * len(match.group(0)), text)


def has_executable_comment(text: str) -> bool:
    """是否含 MySQL 可执行注释 `/*! ... */`。

    判断前先遮蔽字面量：`SELECT '/*!'` 里的 `/*!` 是字符串内容，
    服务端不会把它当注释。
    """
    return bool(_EXECUTABLE_COMMENT_RE.search(mask_literals(text)))


def strip_comments(text: str) -> str:
    """剥掉注释；字面量里的 `--` / `/*` 不受影响（先遮蔽再剥）。"""
    masked = mask_literals(text)
    masked = _BLOCK_COMMENT_RE.sub(" ", masked)
    return _LINE_COMMENT_RE.sub(" ", masked)


def scan_keywords(text: str, keywords: tuple[str, ...] | list[str]) -> list[str]:
    """扫出**会被执行**的关键字（大写，按首次出现顺序）。

    字面量与注释都已被排除，所以命中的都是真正出现在语句里的关键字。
    """
    if not keywords:
        return []
    pattern = re.compile(
        r"\b(" + "|".join(re.escape(word) for word in keywords) + r")\b",
        re.IGNORECASE,
    )
    found: list[str] = []
    for match in pattern.finditer(strip_comments(text)):
        keyword = match.group(1).upper()
        if keyword not in found:
            found.append(keyword)
    return found
