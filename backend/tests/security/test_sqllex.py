"""S5 · 词法层的测试。

词法层是 AST 的补漏层，所以这里测的全是「AST 看不见」的场合：

    - 字符串字面量里的关键字不是关键字
    - 注释里的关键字不是关键字
    - 但 MySQL 可执行注释 `/*! ... */` 必须被认出来（服务端会执行它）
    - 词边界：`created_at` 里没有 CREATE
"""

from __future__ import annotations

from app.sqllex import (
    has_executable_comment,
    mask_literals,
    scan_keywords,
    strip_comments,
)

WRITE_KEYWORDS = ("UPDATE", "DROP", "DELETE", "INSERT", "INTO")


def test_mask_literals_keeps_length() -> None:
    text = "SELECT 'abc' FROM t"
    masked = mask_literals(text)
    assert len(masked) == len(text)
    assert "abc" not in masked
    assert "SELECT" in masked


def test_mask_literals_handles_escaped_quotes() -> None:
    masked = mask_literals("SELECT 'it''s' FROM t")
    assert "it" not in masked
    assert len(masked) == len("SELECT 'it''s' FROM t")


def test_backtick_identifier_is_masked() -> None:
    """反引号里的内容是标识符，不是字面量——但同样不该被当成关键字扫到。"""
    masked = mask_literals("SELECT `drop` FROM t")
    assert "drop" not in masked.lower()


def test_keyword_inside_string_is_not_a_keyword() -> None:
    assert scan_keywords("SELECT 'DROP TABLE' AS x FROM t", WRITE_KEYWORDS) == []


def test_keyword_inside_comment_is_not_a_keyword() -> None:
    assert scan_keywords("SELECT 1 /* DROP TABLE t */ FROM t", WRITE_KEYWORDS) == []
    assert scan_keywords("SELECT 1 -- DROP TABLE t\nFROM t", WRITE_KEYWORDS) == []


def test_real_keyword_is_found() -> None:
    assert scan_keywords("DELETE FROM t", WRITE_KEYWORDS) == ["DELETE"]


def test_word_boundary_is_respected() -> None:
    """`created_at` 里没有 CREATE，`last_update` 里没有 UPDATE。"""
    found = scan_keywords(
        "SELECT created_at, last_update FROM t",
        ("CREATE", "UPDATE", "INSERT"),
    )
    assert found == []


def test_keywords_are_upper_cased_and_deduplicated() -> None:
    found = scan_keywords("drop table t; drop table u", WRITE_KEYWORDS)
    assert found == ["DROP"]


def test_executable_comment_is_detected() -> None:
    assert has_executable_comment("SELECT 1 /*! UNION SELECT 2 */")


def test_executable_comment_inside_string_is_not_detected() -> None:
    assert not has_executable_comment("SELECT '/*! not a comment */' AS x FROM t")


def test_plain_comment_is_not_executable() -> None:
    assert not has_executable_comment("SELECT 1 /* a normal comment */ FROM t")


def test_strip_comments_removes_both_kinds() -> None:
    stripped = strip_comments("SELECT 1 /* x */ FROM t -- y\n")
    assert "/*" not in stripped
    assert "--" not in stripped
    assert "SELECT 1" in stripped
