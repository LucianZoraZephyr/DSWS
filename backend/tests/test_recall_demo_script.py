"""S3 阶段演示：中文检索问题必须**原样**到达 recall CLI。

这一环最容易在 Windows 上被悄悄弄坏：mingw-make 构造 Windows 命令行时，
会把配方里的字节按**系统 ANSI 代码页**（cp936/GBK）解码一次，UTF-8 的中文
因此变成乱码：

    "·"（U+00B7 = C2 B7）  →  "路"（U+8DEF，GBK 码位 C2B7）

问题写进 Makefile 配方就躲不掉这一层。所以演示脚本把问题放在文件里，
由 shell 读出来再交给 CLI——这条链路上字节不再经过 make。

这里用假的 ``uv`` 把 CLI 收到的 argv 记下来（真跑一次需要 MySQL/ES/Qdrant），
断言：
* 传进去的最后一个参数就是黄金集的第一题，**逐字节相等**；
* 黄金集改了第一题，这个脚本也得跟着改（不许悄悄演示另一个问题）。
"""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

import pytest
import yaml

from shellkit import fake_program, run_in_sh, to_shell_path, usable_sh

ROOT = Path(__file__).resolve().parents[2]
DEMO = ROOT / "scripts" / "recall-demo.sh"
GOLDEN = ROOT / "backend" / "tests" / "recall" / "golden.yaml"

SH = usable_sh()

_RECORD = '''\
import json
import sys

with open({path!r}, "w", encoding="utf-8") as handle:
    json.dump(sys.argv, handle, ensure_ascii=False)
'''


def _first_golden_question() -> str:
    data = yaml.safe_load(GOLDEN.read_text(encoding="utf-8"))
    return str(data["questions"][0]["question"])


def _run_demo(
    tmp_path: Path, output: Path | None = None
) -> tuple[subprocess.CompletedProcess, list[str]]:
    """跑一次演示脚本，返回 (结果, 假 uv 记下的 argv)。

    output 为 None 表示**不传**输出路径（脚本应当拒绝）。
    """
    assert DEMO.exists(), "缺少 scripts/recall-demo.sh"
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    record = tmp_path / "argv.json"
    recorder = tmp_path / "record.py"
    recorder.write_text(
        _RECORD.format(path=str(record)), encoding="utf-8", newline="\n"
    )

    fake_program(
        bin_dir,
        "uv",
        f'exec "{to_shell_path(sys.executable)}" "{to_shell_path(recorder)}" "$@"',
    )

    args = [to_shell_path(output)] if output is not None else None
    result = run_in_sh(SH or "sh", DEMO, bin_dir, args=args, stdin="")
    argv = json.loads(record.read_text(encoding="utf-8")) if record.exists() else []
    return result, argv


@pytest.mark.skipif(SH is None, reason="没有可用的 sh 来跑这个脚本")
def test_the_query_reaches_the_cli_byte_exactly(tmp_path: Path) -> None:
    """CLI 收到的中文必须和黄金集第一题逐字节相等。"""
    result, argv = _run_demo(tmp_path, tmp_path / "recall.yaml")

    assert result.returncode == 0, result.stderr
    assert argv, "假 uv 没有被调用"
    assert "app.scripts.recall" in argv, argv
    assert argv[-1] == _first_golden_question(), (
        f"传过去的检索问题被改动了：{argv[-1]!r} != {_first_golden_question()!r}"
    )


@pytest.mark.skipif(SH is None, reason="没有可用的 sh 来跑这个脚本")
def test_the_demo_writes_the_cli_output_to_the_given_file(tmp_path: Path) -> None:
    """CLI 的 YAML 输出要落到指定文件，Makefile 随后要 head 它。"""
    out = tmp_path / "recall.yaml"

    result, _ = _run_demo(tmp_path, out)

    assert result.returncode == 0, result.stderr
    assert out.exists(), "没有生成输出文件"


@pytest.mark.skipif(SH is None, reason="没有可用的 sh 来跑这个脚本")
def test_the_demo_needs_an_output_path(tmp_path: Path) -> None:
    """不给输出路径就直接报错——Makefile 少写参数时要立刻看得见。"""
    result, _ = _run_demo(tmp_path)

    assert result.returncode != 0
    assert result.stdout.strip() == ""
