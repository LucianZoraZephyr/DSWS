"""在 Windows 上跑仓库里 shell 脚本的公共工具。

Git Bash + mingw-make 这个组合下有三个坑，踩过一遍：

* **PATH 必须在壳内部导出。** Git Bash 启动时会把 /usr/bin 顶到 PATH 最前面，
  只靠环境变量里的顺序抢不过它——所以下面所有"把某个目录顶到 PATH 最前"
  的做法都是在壳里 ``export PATH=...:$PATH``。
* **注入的目录要写成 POSIX 形式。** 写成 ``C:/...`` 的话 MSYS 的命令查找会
  直接跳过它，注入等于没注入，必须写成 ``/c/...``。
* **假的可执行程序用带 shebang 的 sh 脚本。** MSYS 按 shebang 解释它；
  改成 .exe 反而不行。

这些工具只服务于测试，不进产品代码。
"""

from __future__ import annotations

import os
import shutil
import subprocess
from pathlib import Path


def usable_sh() -> str | None:
    """找一个真的能跑的 sh。"""
    candidates = [shutil.which("sh"), shutil.which("bash"), "/bin/sh", "/usr/bin/sh"]
    for candidate in candidates:
        if not candidate:
            continue
        try:
            result = subprocess.run(
                [candidate, "-c", "true"], capture_output=True, text=True, timeout=30
            )
        except (OSError, subprocess.SubprocessError):
            continue
        if result.returncode == 0:
            return candidate
    return None


def usable_bash() -> str | None:
    """找一个真的能跑的 bash——WSL 的启动器不算。"""
    candidates: list[str] = []
    if os.name == "nt":
        for variable in ("ProgramFiles", "ProgramW6432"):
            base = os.environ.get(variable)
            if base:
                candidates.append(str(Path(base) / "Git" / "bin" / "bash.exe"))
        candidates.append(str(Path("C:/Program Files/Git/bin/bash.exe")))
    for path in ("/bin/bash", "/usr/bin/bash"):
        if Path(path).exists():
            candidates.append(path)
    found = shutil.which("bash")
    if found:
        candidates.append(found)
    for candidate in candidates:
        try:
            result = subprocess.run(
                [candidate, "-c", "true"], capture_output=True, text=True, timeout=30
            )
        except (OSError, subprocess.SubprocessError):
            continue
        if result.returncode == 0:
            return candidate
    return None


def to_shell_path(path: Path | str) -> str:
    """把 Windows 路径转成 MSYS 认得的 POSIX 形式（``/c/...``）。"""
    text = str(path)
    if os.name != "nt":
        return text
    drive, _, rest = text.partition(":")
    return "/" + drive.lower() + rest.replace("\\", "/")


def path_without_python() -> list[str]:
    """把真实 PATH 里所有"带解释器"的目录剔掉。

    剩下的是 dirname / sed / tr / curl 之类照常可用，只少了一个 python。
    """
    kept: list[str] = []
    for entry in os.environ.get("PATH", "").split(os.pathsep):
        if not entry:
            continue
        directory = Path(entry)
        has_python = any(
            (directory / candidate).exists()
            for candidate in ("python", "python.exe", "python3", "python3.exe")
        )
        if not has_python:
            kept.append(entry)
    return kept


def fake_program(directory: Path, name: str, body: str) -> Path:
    """造一个"行为固定"的假可执行程序。"""
    path = directory / name
    path.write_text(f"#!/bin/sh\n{body}\n", encoding="utf-8", newline="\n")
    path.chmod(0o755)
    return path


def run_in_sh(
    binary: str,
    script: Path,
    bin_dir: Path | None = None,
    *,
    args: list[str] | None = None,
    stdin: str | None = None,
    base_path: list[str] | None = None,
) -> subprocess.CompletedProcess:
    """在"bin_dir 最优先"的 PATH 下跑一个脚本。

    PATH 必须在**壳内部**导出：Git Bash 启动时会把自己的 /usr/bin 顶到
    PATH 最前面，只靠环境变量里的顺序是抢不过它的。

    stdin **按字节原样送进去**：用文本模式的话，Windows 上 Python 会把
    每一个 ``\\n`` 翻译成 ``\\r\\n``，脚本收到的输入就不是测试写的那份了
    （要测 CRLF 回车时尤其致命）。stdout/stderr 则按 UTF-8 解码回字符串。
    """
    env = dict(os.environ)
    env["PATH"] = os.pathsep.join(
        base_path if base_path is not None else path_without_python()
    )
    prefix = ""
    if bin_dir is not None:
        prefix = f'export PATH="{to_shell_path(bin_dir)}:$PATH";'
    tail = " ".join(f'"{argument}"' for argument in (args or []))
    result = subprocess.run(
        [
            binary,
            "-c",
            f'{prefix} exec "{to_shell_path(script)}" {tail}'.rstrip(),
        ],
        input=(stdin or "").encode("utf-8"),
        capture_output=True,
        env=env,
        timeout=120,
    )
    result.stdout = result.stdout.decode("utf-8", errors="replace")
    result.stderr = result.stderr.decode("utf-8", errors="replace")
    return result
