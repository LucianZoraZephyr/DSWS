"""`scripts/generate_docker_env.py` 的行为测试。

这个脚本直接写出**明文密码**，所以三件事必须成立：

1. 写出来的文件是 LF 换行——Windows 上默认的换行转换会把 ``\\r`` 塞进
   密码值，于是宿主机 CLI（读 .env 时去掉 \\r）和容器里的密码对不上。
2. 文件从**创建那一刻**就是 0600，不是"先写再 chmod"——中间那几毫秒里
   密码是全局可读的。
3. ``--dry-run`` 不把密码打到终端上。
"""

from __future__ import annotations

import importlib.util
import os
import stat
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
SCRIPT = ROOT / "scripts" / "generate_docker_env.py"


@pytest.fixture(scope="module")
def env_script():
    spec = importlib.util.spec_from_file_location("generate_docker_env", SCRIPT)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_written_file_uses_lf_only(env_script, tmp_path: Path) -> None:
    target = tmp_path / ".env"
    assert env_script.main(["--output", str(target)]) == 0
    raw = target.read_bytes()
    assert b"\r\n" not in raw


@pytest.mark.skipif(os.name == "nt", reason="POSIX 权限位在 Windows 上没有意义")
def test_written_file_is_owner_only(env_script, tmp_path: Path) -> None:
    target = tmp_path / ".env"
    assert env_script.main(["--output", str(target)]) == 0
    mode = stat.S_IMODE(target.stat().st_mode)
    assert mode == 0o600


def test_dry_run_redacts_secrets(env_script, tmp_path: Path, capsys) -> None:
    target = tmp_path / ".env"
    assert env_script.main(["--dry-run", "--output", str(target)]) == 0
    printed = capsys.readouterr().out
    for key in env_script.SECRET_KEYS:
        assert f"{key}=***" in printed
    assert not target.exists()


def test_dry_run_never_prints_the_real_secret(
    env_script, tmp_path: Path, capsys, monkeypatch
) -> None:
    """上面那条只验了"有 ***"——顺手多打一行真密码它照样过。

    这里把随机源钉成一个已知的口令，再看终端输出里有没有它。
    （不钉死随机源的话，两次生成的密码根本不是同一个，比对没有意义。）
    """
    marker = "SECRET-MUST-NOT-APPEAR-1234567890"
    monkeypatch.setattr(
        env_script.secrets, "token_urlsafe", lambda *_args, **_kwargs: marker
    )
    target = tmp_path / ".env"
    assert env_script.main(["--dry-run", "--output", str(target)]) == 0
    printed = capsys.readouterr().out
    assert marker not in printed
    assert f"{env_script.SECRET_KEYS[0]}={env_script.REDACTED}" in printed


def test_existing_file_is_not_overwritten_without_force(
    env_script, tmp_path: Path
) -> None:
    target = tmp_path / ".env"
    target.write_text("keep me", encoding="utf-8")
    assert env_script.main(["--output", str(target)]) == 1
    assert target.read_text(encoding="utf-8") == "keep me"


def test_force_overwrites(env_script, tmp_path: Path) -> None:
    target = tmp_path / ".env"
    target.write_text("stale", encoding="utf-8")
    assert env_script.main(["--force", "--output", str(target)]) == 0
    text = target.read_text(encoding="utf-8")
    assert "stale" not in text
    assert "MYSQL_ROOT_PASSWORD=" in text


# ---------------------------------------------------------------------------
# --force 不能偷偷换掉密码
# ---------------------------------------------------------------------------


def test_force_keeps_the_passwords_the_mysql_volume_already_knows(
    env_script, tmp_path: Path
) -> None:
    """``--force`` 必须**保留**已有的密码值。

    MySQL 官方镜像只在**首次**初始化 ``/var/lib/mysql`` 时拿
    ``MYSQL_ROOT_PASSWORD`` 建账号；数据卷一旦存在，容器里的密码就固定成
    当初那一个。``.env`` 里的密码跟着换了、容器里的没换，于是：

    * ``mysql`` 的健康检查 ``mysqladmin ping -p$MYSQL_ROOT_PASSWORD`` 永远失败；
    * ``init`` 依赖 ``mysql`` healthy，永远不启动；
    * ``backend`` 依赖 ``init`` 成功退出，也永远不启动。

    症状指向健康检查，真正的原因（密码被换了）一个字都没提。
    """
    target = tmp_path / ".env"
    target.write_text(
        "MYSQL_ROOT_PASSWORD=root-keep\n"
        "MYSQL_PASSWORD=app-keep\n"
        "REBUILD_DW_RO_PASSWORD=ro-keep\n",
        encoding="utf-8",
    )

    assert env_script.main(["--force", "--output", str(target)]) == 0

    text = target.read_text(encoding="utf-8")
    assert "MYSQL_ROOT_PASSWORD=root-keep" in text
    assert "MYSQL_PASSWORD=app-keep" in text
    assert "REBUILD_DW_RO_PASSWORD=ro-keep" in text


def test_force_keeps_the_other_settings_you_configured(
    env_script, tmp_path: Path
) -> None:
    """端口、镜像源、外部 API Key 这些改过的值也不该被冲掉。"""
    target = tmp_path / ".env"
    target.write_text(
        "MYSQL_ROOT_PASSWORD=root-keep\n"
        "MYSQL_HOST_PORT=3307\n"
        "REBUILD_LLM_API_KEY=sk-mine\n",
        encoding="utf-8",
    )

    assert env_script.main(["--force", "--output", str(target)]) == 0

    text = target.read_text(encoding="utf-8")
    assert "MYSQL_HOST_PORT=3307" in text
    assert "REBUILD_LLM_API_KEY=sk-mine" in text


def test_force_invents_a_password_when_the_old_value_was_empty(
    env_script, tmp_path: Path
) -> None:
    """旧的密码是空的就得重新生成一个——空密码 mysql 根本起不来。"""
    target = tmp_path / ".env"
    target.write_text("MYSQL_ROOT_PASSWORD=\n", encoding="utf-8")

    assert env_script.main(["--force", "--output", str(target)]) == 0

    text = target.read_text(encoding="utf-8")
    assert "MYSQL_ROOT_PASSWORD=\n" not in text
    assert "MYSQL_ROOT_PASSWORD=change-me-root" not in text


def test_force_fills_in_the_keys_the_old_file_was_missing(
    env_script, tmp_path: Path
) -> None:
    """旧文件里没有的键按模板补齐（密码用随机值，不是模板里的占位符）。"""
    target = tmp_path / ".env"
    target.write_text("MYSQL_ROOT_PASSWORD=root-keep\n", encoding="utf-8")

    assert env_script.main(["--force", "--output", str(target)]) == 0

    text = target.read_text(encoding="utf-8")
    assert "MYSQL_ROOT_PASSWORD=root-keep" in text
    assert "MYSQL_PASSWORD=change-me-app" not in text
    assert "REBUILD_LLM_BASE_URL=" in text


def test_force_keeps_a_key_the_template_does_not_have(
    env_script, tmp_path: Path
) -> None:
    """模板里没有的键也得保留——最典型的是 ``COMPOSE_PROJECT_NAME``。

    丢掉它，Compose 的项目名就变回默认值，数据卷的名字跟着变：
    用户的卷"消失"了，栈却从一个**空卷**重新起（MySQL 于是把 dw.sql
    又导一遍，旧数据还躺在老名字的卷里没人管）。
    """
    target = tmp_path / ".env"
    target.write_text("COMPOSE_PROJECT_NAME=my-shop\n", encoding="utf-8")

    assert env_script.main(["--force", "--output", str(target)]) == 0

    text = target.read_text(encoding="utf-8")
    assert "COMPOSE_PROJECT_NAME=my-shop" in text


def test_rotate_secrets_replaces_the_passwords_and_warns_about_the_volume(
    env_script, tmp_path: Path, capsys
) -> None:
    """想换密码得显式说，并且必须提醒数据卷的事。"""
    target = tmp_path / ".env"
    target.write_text(
        "MYSQL_ROOT_PASSWORD=root-old\nMYSQL_PASSWORD=app-old\n", encoding="utf-8"
    )

    assert env_script.main(["--rotate-secrets", "--output", str(target)]) == 0

    text = target.read_text(encoding="utf-8")
    assert "root-old" not in text
    assert "app-old" not in text
    stderr = capsys.readouterr().err
    assert "mysql_data" in stderr, stderr
    assert "docker-reset" in stderr, stderr


def test_rotate_secrets_warns_about_the_volume_even_without_an_existing_file(
    env_script, tmp_path: Path, capsys
) -> None:
    """没有 .env 也要提醒——数据卷可不管 .env 在不在。

    卷还在（``make docker-reset`` 没跑过）的时候换密码，容器里的
    密码仍然是旧的，整栈起不来。这条提醒跟 .env 存不存在无关。
    """
    target = tmp_path / ".env"

    assert env_script.main(["--rotate-secrets", "--output", str(target)]) == 0

    stderr = capsys.readouterr().err
    assert "mysql_data" in stderr, stderr
    assert "docker-reset" in stderr, stderr


def test_dry_run_does_not_print_the_api_key_from_the_existing_file(
    env_script, tmp_path: Path, capsys
) -> None:
    """``--dry-run`` 是"看一眼要写什么"，不是"把密钥读出来"。

    ``REBUILD_LLM_API_KEY`` 也算密钥：它进不了 ``SECRET_KEYS``（那儿只管
    本脚本生成的密码），但打码名单里必须有它。
    """
    target = tmp_path / ".env"
    target.write_text("REBUILD_LLM_API_KEY=sk-super-secret\n", encoding="utf-8")

    assert env_script.main(["--dry-run", "--output", str(target)]) == 0

    printed = capsys.readouterr().out
    assert "sk-super-secret" not in printed
    assert "REBUILD_LLM_API_KEY=***" in printed


# ---------------------------------------------------------------------------
# 写 .env 必须是原子的
#
# 写文件的老顺序是"先删旧的、再建新的"。这两步之间出了任何岔子（磁盘满、
# 权限被改、Ctrl-C、笔记本合盖），磁盘上就**一个 .env 都不剩**——
# 而那份文件里有 MySQL 的密码、用户的 LLM API Key、COMPOSE_PROJECT_NAME。
# 密码能重生成，API Key 重生成不了：用户得翻回聊天记录里把它找回来。
# ---------------------------------------------------------------------------


def test_a_failed_write_leaves_the_old_env_in_place(
    env_script, tmp_path: Path, monkeypatch
) -> None:
    """写到一半失败时，旧 .env 必须原封不动地留在原地。

    这里模拟的是"最后落盘那一下失败"（磁盘满）——如果实现是先删再建，
    这一刻磁盘上就空了，而调用方只看到一句 OSError。
    """
    target = tmp_path / ".env"
    original = "MYSQL_ROOT_PASSWORD=root-keep\nREBUILD_LLM_API_KEY=sk-mine\n"
    target.write_text(original, encoding="utf-8")

    real_replace = os.replace

    def _fail_on_this_file(src: object, dst: object, **kwargs: object) -> None:
        if str(dst) == str(target):
            raise OSError("no space left on device")
        real_replace(src, dst)  # type: ignore[arg-type]

    monkeypatch.setattr(env_script.os, "replace", _fail_on_this_file)

    try:
        env_script.main(["--force", "--output", str(target)])
    except OSError:
        pass

    assert target.exists(), "旧 .env 被删掉了：写失败时磁盘上不该一个 .env 都不剩"
    assert target.read_text(encoding="utf-8") == original, (
        "旧 .env 的内容被改动了：失败的那次写入不该碰到已有的文件"
    )


def test_overwriting_never_deletes_the_target_before_writing_the_new_one(
    env_script, tmp_path: Path, monkeypatch
) -> None:
    """覆盖写的过程里，目标文件一次都不该被 ``unlink``。

    只看结果看不出顺序：删掉再建、最后内容也对。但顺序本身是安全性
    的一部分——"先删"的那个窗口就是数据可能消失的那个窗口。
    """
    target = tmp_path / ".env"
    target.write_text("MYSQL_ROOT_PASSWORD=root-keep\n", encoding="utf-8")

    deleted: list[str] = []
    real_unlink = Path.unlink

    def _record(self: Path, *args: object, **kwargs: object) -> None:
        deleted.append(str(self))
        return real_unlink(self, *args, **kwargs)  # type: ignore[arg-type]

    monkeypatch.setattr(Path, "unlink", _record)

    assert env_script.main(["--force", "--output", str(target)]) == 0

    assert str(target) not in deleted, (
        "目标文件被先删后建了：应该写成临时文件再原子替换过去"
    )


def test_no_temporary_file_is_left_behind_after_a_successful_write(
    env_script, tmp_path: Path
) -> None:
    """原子写要留下临时文件，但成功之后必须把它收走。"""
    target = tmp_path / ".env"

    assert env_script.main(["--output", str(target)]) == 0

    assert sorted(item.name for item in tmp_path.iterdir()) == [".env"]


# ---------------------------------------------------------------------------
# --dry-run 的打码范围
#
# 固定名单（REDACT_KEYS）是"认识几个算几个"：旧 .env 里常留着模板没有的
# 第三方密钥（DEEPSEEK_API_KEY 之类），而这些键会被原样搬进新文件。
# --dry-run 的输出是用户会往工单、聊天记录里贴的东西——它必须只含"要写
# 什么"，不含密钥本身。
# ---------------------------------------------------------------------------


def test_dry_run_never_prints_a_secret_from_a_key_the_template_does_not_have(
    env_script, tmp_path: Path, capsys
) -> None:
    """模板里没有的键也可能是密钥——打码不能只认固定名单。"""
    target = tmp_path / ".env"
    target.write_text("DEEPSEEK_API_KEY=sk-must-not-appear\n", encoding="utf-8")

    assert env_script.main(["--dry-run", "--output", str(target)]) == 0

    printed = capsys.readouterr().out
    assert "sk-must-not-appear" not in printed
    assert "DEEPSEEK_API_KEY=***" in printed


def test_dry_run_masks_the_password_inside_a_connection_url(
    env_script, tmp_path: Path, capsys
) -> None:
    """键名看不出是密钥，但值里带着账号密码——那一段必须打掉。"""
    target = tmp_path / ".env"
    target.write_text(
        "LEGACY_DATABASE_URL=mysql://someone:dsn-secret@127.0.0.1:3306/dw\n",
        encoding="utf-8",
    )

    assert env_script.main(["--dry-run", "--output", str(target)]) == 0

    printed = capsys.readouterr().out
    assert "dsn-secret" not in printed
    assert "LEGACY_DATABASE_URL=mysql://someone:***@127.0.0.1:3306/dw" in printed


def test_redact_masks_a_secret_written_with_the_export_prefix(env_script) -> None:
    """``export KEY=value`` 是合法的写法——打码不能被前缀绕过去。"""
    assert "export MYSQL_PASSWORD=***" in env_script.redact(
        "export MYSQL_PASSWORD=abc\n"
    )


def test_redact_leaves_an_ordinary_value_alone(env_script) -> None:
    """打码是保守的，但不该把普通配置也一起糊掉——预览还要看得懂。"""
    text = "MYSQL_HOST_PORT=3306\nNPM_REGISTRY=https://registry.npmmirror.com\n"
    assert env_script.redact(text) == text


def test_redact_leaves_a_comment_alone(env_script) -> None:
    """注释里的 ``=`` 不是键值分隔符，不能被当成一行配置改掉。"""
    text = "# 密码见 1Password: MYSQL_PASSWORD=change-me\n"
    assert env_script.redact(text) == text
