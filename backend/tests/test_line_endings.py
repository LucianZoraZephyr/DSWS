"""换行符：bash 脚本必须是 LF。

Windows 上 Git 的 `core.autocrlf=true`（默认值）会把 LF 检出成 CRLF。
bash 读到 CR 的后果不是「慢一点」，而是直接炸：

    scripts/stage0-ui.sh: line 3: $'\\r': command not found

`#!/usr/bin/env bash` 后面那个 CR 也会让 Linux 内核报
`bad interpreter: No such file or directory`。

仓库根的 .gitattributes 把这些文件钉死成 LF——这是唯一能跨平台
统一的做法：靠 `core.autocrlf` 的话，每个人的本地配置都不一样。
"""

from __future__ import annotations

from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
SCRIPTS_DIR = ROOT / "scripts"
GITATTRIBUTES = ROOT / ".gitattributes"


def _rules() -> list[str]:
    if not GITATTRIBUTES.is_file():
        return []
    return [
        line.strip()
        for line in GITATTRIBUTES.read_text(encoding="utf-8").splitlines()
        if line.strip() and not line.strip().startswith("#")
    ]


def test_gitattributes_pins_shell_scripts_to_lf() -> None:
    rules = _rules()
    assert rules, "缺少 .gitattributes：Windows 检出后 bash 脚本会带 CRLF"
    matching = [rule for rule in rules if rule.startswith("*.sh")]
    assert matching, ".gitattributes 没有管住 *.sh"
    assert all("eol=lf" in rule for rule in matching), matching


def test_gitattributes_pins_makefile_to_lf() -> None:
    rules = _rules()
    matching = [rule for rule in rules if rule.split() and rule.split()[0] == "Makefile"]
    assert matching, ".gitattributes 没有管住 Makefile"
    assert all("eol=lf" in rule for rule in matching), matching


def test_shell_scripts_have_no_carriage_returns() -> None:
    offenders = []
    for script in sorted(SCRIPTS_DIR.glob("*.sh")):
        raw = script.read_bytes()
        if b"\r\n" in raw:
            offenders.append(script.name)
    assert not offenders, (
        f"这些脚本带 CRLF，bash 会报 command not found：{offenders}；"
        "先 git checkout -- scripts/ 让 .gitattributes 生效"
    )
