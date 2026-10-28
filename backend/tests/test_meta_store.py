"""S2 · MetaStore 建库时的 root 降级路径。

`ensure_database()` 需要 root 来 CREATE/GRANT。但**库已经存在**的时候，
root 其实用不上——这是容器化的常态：init 容器跑一次把库建好，
之后每次 `docker compose up` 都不该再要 root 密码。

所以 root 连不上（或者压根没配 root 密码）时的降级路径必须是：
"库在 → 什么都不做就返回"。

这条路径以前是坏的：`mysql_dsn(root=True)` 缺密码时抛的是 `BuildError`，
而 `ensure_database()` 只接 `pymysql.MySQLError`——于是 `BuildError` 直接
穿透出去，"库已存在就不需要 root" 这条分支永远走不到。
"""

from __future__ import annotations

from typing import Any

import pytest

from app.build.config import BuildConfig
from app.build.errors import BuildError
from app.build.meta_store import MetaStore


class _FakeCursor:
    def __init__(self, exists: bool) -> None:
        self._exists = exists

    def __enter__(self) -> "_FakeCursor":
        return self

    def __exit__(self, *exc: object) -> bool:
        return False

    def execute(self, sql: str, params: object = None) -> None:
        return None

    def fetchone(self) -> object:
        return ("meta_rebuild",) if self._exists else None


class _FakeConnection:
    def __init__(self, exists: bool) -> None:
        self._exists = exists
        self.closed = False

    def cursor(self) -> _FakeCursor:
        return _FakeCursor(self._exists)

    def close(self) -> None:
        self.closed = True


def _fake_connect(
    recorded: list[dict[str, Any]], exists: bool
) -> Any:
    def _connect(**kwargs: Any) -> _FakeConnection:
        recorded.append(kwargs)
        return _FakeConnection(exists)

    return _connect


def test_existing_database_needs_no_root_password(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """库已存在 + 没有 root 密码 → 安静返回，不报错。"""
    monkeypatch.delenv("REBUILD_MYSQL_ROOT_PASSWORD", raising=False)
    monkeypatch.setenv("REBUILD_MYSQL_PASSWORD", "app-secret")
    recorded: list[dict[str, Any]] = []
    monkeypatch.setattr("pymysql.connect", _fake_connect(recorded, True))

    MetaStore(BuildConfig.from_env()).ensure_database()

    # 只用应用账号连了一次；root 那次压根没发起。
    assert len(recorded) == 1, recorded
    assert recorded[0]["user"] == "didilili"


def test_missing_database_still_reports_the_root_password(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """库不存在 + 没有 root 密码 → 报错，并且点名该配哪个变量。"""
    monkeypatch.delenv("REBUILD_MYSQL_ROOT_PASSWORD", raising=False)
    monkeypatch.setenv("REBUILD_MYSQL_PASSWORD", "app-secret")
    monkeypatch.setattr("pymysql.connect", _fake_connect([], False))

    with pytest.raises(BuildError) as excinfo:
        MetaStore(BuildConfig.from_env()).ensure_database()

    assert "REBUILD_MYSQL_ROOT_PASSWORD" in str(excinfo.value)


def test_both_passwords_missing_names_both_variables(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """root 和应用密码都没配 → 报错要点名**两个**变量。

    `database_exists()` 里的 `_connect()` 走 `mysql_dsn()`，应用密码缺失
    时抛的是 `BuildError`——它从 except 块里穿出去，把本该出现的
    合并消息顶掉了。用户只看到"缺 REBUILD_MYSQL_PASSWORD"，
    看不到 root 那条，于是以为只差一个变量。
    """
    monkeypatch.delenv("REBUILD_MYSQL_ROOT_PASSWORD", raising=False)
    monkeypatch.delenv("REBUILD_MYSQL_PASSWORD", raising=False)

    with pytest.raises(BuildError) as excinfo:
        MetaStore(BuildConfig.from_env()).ensure_database()

    message = str(excinfo.value)
    assert "REBUILD_MYSQL_ROOT_PASSWORD" in message, message
    assert "REBUILD_MYSQL_PASSWORD" in message, message


def test_root_connection_is_not_attempted_without_a_root_password(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """没有 root 密码时，不能拿着空密码去撞一次 root。"""
    monkeypatch.delenv("REBUILD_MYSQL_ROOT_PASSWORD", raising=False)
    monkeypatch.setenv("REBUILD_MYSQL_PASSWORD", "app-secret")

    recorded: list[dict[str, Any]] = []

    def _connect(**kwargs: Any) -> _FakeConnection:
        recorded.append(kwargs)
        if kwargs.get("user") == "root":
            raise AssertionError("没有 root 密码时不该发起 root 连接")
        return _FakeConnection(False)

    monkeypatch.setattr("pymysql.connect", _connect)

    with pytest.raises(BuildError):
        MetaStore(BuildConfig.from_env()).ensure_database()

    assert all(call.get("user") != "root" for call in recorded), recorded
