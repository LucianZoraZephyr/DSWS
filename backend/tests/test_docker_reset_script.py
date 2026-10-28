"""``make docker-reset`` 会删掉三个数据卷——确认提示必须真的拦得住。

这一条没有测试就等于把 ``docker compose down -v`` 挂在一次回车键上：
删掉的是 MySQL / ES / Qdrant 的全部数据，不可恢复。

要守住的性质：

* 输入不是 ``yes``（包括直接回车、EOF）时**不删任何东西**，并且以非 0 退出；
* 输入 ``yes`` 时才删；
* 提示里必须把要删的三个卷名**逐个写出来**——用户得知道自己要删什么。

这里的 docker 是假的：真跑一次 ``down -v`` 会把用户的数据卷真的删掉。
"""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from shellkit import fake_program, run_in_sh, to_shell_path, usable_sh

ROOT = Path(__file__).resolve().parents[2]
RESET = ROOT / "scripts" / "docker-reset.sh"

SH = usable_sh()

VOLUMES = (
    "shopkeeper-rebuild_mysql_data",
    "shopkeeper-rebuild_es_data",
    "shopkeeper-rebuild_qdrant_data",
)


def _run(tmp_path: Path, answer: str) -> tuple[subprocess.CompletedProcess, str]:
    """跑一次 docker-reset.sh，返回 (结果, 假 docker 收到的命令行)。"""
    return _run_raw(tmp_path, answer + "\n")


def _run_raw(tmp_path: Path, stdin: str) -> tuple[subprocess.CompletedProcess, str]:
    """同上，但 stdin 原样送进去（用来模拟 Windows 的 CRLF 回车）。"""
    assert RESET.exists(), "缺少 scripts/docker-reset.sh"
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    log = tmp_path / "docker.log"
    fake_program(bin_dir, "docker", f'echo "$@" >> "{to_shell_path(log)}"')

    result = run_in_sh(SH or "sh", RESET, bin_dir, stdin=stdin)
    calls = log.read_text(encoding="utf-8") if log.exists() else ""
    return result, calls


@pytest.mark.skipif(SH is None, reason="没有可用的 sh 来跑这个脚本")
@pytest.mark.parametrize("answer", ["no", "y", "YES", "", " yes"])
def test_refuses_to_delete_the_volumes_unless_the_answer_is_exactly_yes(
    tmp_path: Path, answer: str
) -> None:
    """不是 ``yes`` 就一个字节都不许删。"""
    result, calls = _run(tmp_path, answer)

    assert result.returncode != 0, f"输入 {answer!r} 竟然被放行了"
    assert calls == "", f"输入 {answer!r} 时仍然调了 docker：{calls!r}"


@pytest.mark.skipif(SH is None, reason="没有可用的 sh 来跑这个脚本")
def test_deletes_the_volumes_after_an_explicit_yes(tmp_path: Path) -> None:
    """输入 yes 才删——而且删的是 compose 的数据卷。"""
    result, calls = _run(tmp_path, "yes")

    assert result.returncode == 0, result.stderr
    assert "down -v" in calls, calls


@pytest.mark.skipif(SH is None, reason="没有可用的 sh 来跑这个脚本")
def test_the_deletion_targets_exactly_the_project_the_prompt_listed(
    tmp_path: Path,
) -> None:
    """提示里列的卷名，必须就是真会被删的那些。

    脚本自己算出项目名、把它逐条写进提示，然后调用
    ``docker compose down -v``——这一句**没有指定项目名**，于是删掉的是
    Compose 自己解析出来的那个项目。两边只要差一点点（Compose 多认了
    一个控制变量、.env 的 dotenv 语义和脚本的解析不完全一样），就会同时
    成立两件事："你将被删除 X"和"实际被删除 Y"。

    把项目名显式交给 Compose，提示和动作就锁死在同一个名字上。
    """
    result, calls = _run(tmp_path, "yes")

    assert result.returncode == 0, result.stderr
    assert "--project-name shopkeeper-rebuild" in calls, calls


@pytest.mark.skipif(SH is None, reason="没有可用的 sh 来跑这个脚本")
def test_the_prompt_says_the_containers_and_the_network_go_too(
    tmp_path: Path,
) -> None:
    """``down -v`` 不止删卷：容器和网络也一起没了。

    提示只说"删除数据卷"，用户按完 yes 发现整个栈都不见了，以为
    ``down -v`` 顺手带走了别的东西——其实是提示少说了一半。
    """
    result, _ = _run(tmp_path, "no")

    assert "容器" in result.stdout, result.stdout
    assert "网络" in result.stdout, result.stdout


@pytest.mark.skipif(SH is None, reason="没有可用的 sh 来跑这个脚本")
def test_accepts_yes_typed_in_a_windows_console(tmp_path: Path) -> None:
    """Windows 终端回车是 CRLF——用户敲了 yes 就不该被当成没敲。"""
    result, calls = _run_raw(tmp_path, "yes\r\n")

    assert result.returncode == 0, result.stderr
    assert "down -v" in calls, calls


@pytest.mark.skipif(SH is None, reason="没有可用的 sh 来跑这个脚本")
def test_prompt_names_every_volume_it_is_about_to_delete(tmp_path: Path) -> None:
    """提示里要逐个写出要删的卷名，用户才可能知道自己删的是什么。"""
    result, _ = _run(tmp_path, "no")

    for volume in VOLUMES:
        assert volume in result.stdout, f"提示里没有 {volume}：\n{result.stdout}"


@pytest.mark.skipif(SH is None, reason="没有可用的 sh 来跑这个脚本")
def test_the_prompt_follows_the_compose_project_name(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """改了 ``COMPOSE_PROJECT_NAME``，提示里的卷名也得跟着改。

    卷的实际名字是 ``<项目名>_<卷名>``。提示里写着别的名字，用户就不知道
    自己到底要删什么——而 ``docker compose down -v`` 删的恰恰是新项目名
    底下那三个卷。
    """
    monkeypatch.setenv("COMPOSE_PROJECT_NAME", "my-shop")

    result, _ = _run(tmp_path, "no")

    assert "my-shop_mysql_data" in result.stdout, result.stdout
    assert "shopkeeper-rebuild_mysql_data" not in result.stdout, result.stdout


@pytest.mark.skipif(SH is None, reason="没有可用的 sh 来跑这个脚本")
def test_the_confirmation_prompt_is_not_mojibake(tmp_path: Path) -> None:
    """提示语是中文，必须原样输出。

    Windows 上 mingw-make 会把配方里的字节按 ANSI 代码页（cp936/GBK）解码一次，
    放在 Makefile 里的中文因此会变成乱码——所以提示语在脚本文件里，
    而且这个测试守着它别被挪回 Makefile。
    """
    result, _ = _run(tmp_path, "no")

    assert "输入 yes 继续" in result.stdout, result.stdout


# ---------------------------------------------------------------------------
# 项目名的优先级
#
# 卷的实际名字是 "<项目名>_<卷名>"。Compose 解析项目名的顺序是
#   环境变量 COMPOSE_PROJECT_NAME > .env 里的同名键 > compose.yaml 的 name:
# 脚本原来只看前两个里的第一个——在 .env 里设了项目名的用户，提示里的卷名
# 和真正被删掉的卷名对不上。
# ---------------------------------------------------------------------------


def _repo(
    tmp_path: Path, *, env_file: str | None = None, compose: str | None = None
) -> Path:
    """把脚本复制到一个临时仓库里——``.env`` / ``compose.yaml`` 由测试控制。

    脚本自己会 ``cd`` 到它所在目录的上一级，所以光复制脚本不够：仓库根下
    还得有测试自己的 ``.env`` 和 ``compose.yaml``。
    """
    root = tmp_path / "repo"
    (root / "scripts").mkdir(parents=True)
    (root / "scripts" / "docker-reset.sh").write_bytes(RESET.read_bytes())
    if env_file is not None:
        (root / ".env").write_text(env_file, encoding="utf-8")
    if compose is not None:
        (root / "compose.yaml").write_text(compose, encoding="utf-8")
    return root


def _run_in(root: Path, tmp_path: Path, answer: str = "no") -> tuple:
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir(exist_ok=True)
    log = tmp_path / "docker.log"
    fake_program(bin_dir, "docker", f'echo "$@" >> "{to_shell_path(log)}"')

    result = run_in_sh(
        SH or "sh",
        root / "scripts" / "docker-reset.sh",
        bin_dir,
        stdin=answer + "\n",
    )
    calls = log.read_text(encoding="utf-8") if log.exists() else ""
    return result, calls


@pytest.mark.skipif(SH is None, reason="没有可用的 sh 来跑这个脚本")
def test_the_prompt_follows_the_project_name_in_the_env_file(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """``.env`` 里的 ``COMPOSE_PROJECT_NAME`` 也要算数。

    在 ``.env`` 里设了项目名的用户，看到的提示卷名必须和真正被删掉的
    卷名一致——不然用户按 yes 的时候并不知道自己在删什么。
    """
    monkeypatch.delenv("COMPOSE_PROJECT_NAME", raising=False)
    root = _repo(
        tmp_path,
        env_file="COMPOSE_PROJECT_NAME=from-env-file\n",
        compose="name: from-compose-file\n",
    )

    result, _ = _run_in(root, tmp_path)

    assert "from-env-file_mysql_data" in result.stdout, result.stdout
    assert "from-compose-file_mysql_data" not in result.stdout, result.stdout


@pytest.mark.skipif(SH is None, reason="没有可用的 sh 来跑这个脚本")
def test_the_project_name_in_the_env_file_may_be_quoted(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """``.env`` 里的值可以带引号——引号不该进到卷名里。"""
    monkeypatch.delenv("COMPOSE_PROJECT_NAME", raising=False)
    root = _repo(
        tmp_path,
        env_file='COMPOSE_PROJECT_NAME="from-env-file"\n',
        compose="name: from-compose-file\n",
    )

    result, _ = _run_in(root, tmp_path)

    assert "from-env-file_mysql_data" in result.stdout, result.stdout
    assert '"' not in result.stdout.split("mysql_data")[0].split("\n")[-1], result.stdout


@pytest.mark.skipif(SH is None, reason="没有可用的 sh 来跑这个脚本")
def test_the_prompt_falls_back_to_the_compose_file_name(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """``.env`` 里没有项目名时，用 ``compose.yaml`` 的 ``name:``。"""
    monkeypatch.delenv("COMPOSE_PROJECT_NAME", raising=False)
    root = _repo(
        tmp_path,
        env_file="MYSQL_PASSWORD=x\n",
        compose="name: from-compose-file\n",
    )

    result, _ = _run_in(root, tmp_path)

    assert "from-compose-file_mysql_data" in result.stdout, result.stdout


@pytest.mark.skipif(SH is None, reason="没有可用的 sh 来跑这个脚本")
def test_the_environment_wins_over_the_env_file(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """优先级和 Compose 一致：环境变量 > ``.env`` > ``compose.yaml``。"""
    monkeypatch.setenv("COMPOSE_PROJECT_NAME", "from-shell")
    root = _repo(
        tmp_path,
        env_file="COMPOSE_PROJECT_NAME=from-env-file\n",
        compose="name: from-compose-file\n",
    )

    result, _ = _run_in(root, tmp_path)

    assert "from-shell_mysql_data" in result.stdout, result.stdout


@pytest.mark.skipif(SH is None, reason="没有可用的 sh 来跑这个脚本")
def test_it_refuses_when_the_project_name_cannot_be_determined(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """项目名定不下来就不能动手——卷名猜错，删的就是别的东西。

    这种时候 Compose 会退回"用目录名当项目名"，而脚本猜不到那个名字：
    于是提示里只能写没有前缀的 ``mysql_data``，真正被删的却是
    ``<某个项目名>_mysql_data``。这比直接拒绝危险得多。
    """
    monkeypatch.delenv("COMPOSE_PROJECT_NAME", raising=False)
    root = _repo(tmp_path, compose="services: {}\n")

    result, calls = _run_in(root, tmp_path)

    assert result.returncode != 0, result.stdout
    assert calls == "", f"项目名都没定下来就动 docker 了：{calls!r}"
    assert "COMPOSE_PROJECT_NAME" in result.stderr, result.stderr

