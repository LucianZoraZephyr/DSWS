"""S3 · 只读客户端在**凭据缺失**时的行为。

背景：容器化之前 `BuildConfig` 有硬编码的密码兜底值，缺配置时会连上
MySQL 再报 `Access denied`。现在缺配置会立刻抛错——但检索栈的
`ping()` 是给健康检查用的，**不能**因为没配密码就整个炸掉：
它必须报告"这个依赖不可用"，让 ES / Qdrant 的状态照常返回。
"""

from __future__ import annotations

import pytest

from app.build.config import BuildConfig
from app.recall.clients import MetaReader
from app.recall.errors import RecallError


@pytest.fixture(autouse=True)
def _clean_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("REBUILD_MYSQL_PASSWORD", raising=False)
    monkeypatch.delenv("REBUILD_MYSQL_ROOT_PASSWORD", raising=False)


def test_meta_ping_is_false_when_credentials_missing() -> None:
    reader = MetaReader(BuildConfig.from_env())
    assert reader.ping() is False


def test_meta_ping_does_not_even_try_to_connect(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """凭据缺失时必须**一次连接都不发起**。

    光断言 ``ping() is False`` 是拦不住兜底密码的：密码一回来，
    ``ping()`` 会真的去连 MySQL，连不上，还是 False——测试照样绿。
    区别不在结果上，在"有没有发起连接"：缺凭据时应该就地停下，
    而不是拿着一个八成是错的密码去撞一次库。
    """
    attempts: list[dict[str, object]] = []

    def _refuse(**kwargs: object) -> None:
        attempts.append(kwargs)
        raise AssertionError("凭据缺失时不该尝试连接 MySQL")

    monkeypatch.setattr("pymysql.connect", _refuse)

    reader = MetaReader(BuildConfig.from_env())

    assert reader.ping() is False
    assert attempts == [], attempts


def test_meta_load_snapshot_explains_missing_credentials() -> None:
    reader = MetaReader(BuildConfig.from_env())
    with pytest.raises(RecallError) as excinfo:
        reader.load_snapshot()
    assert "REBUILD_MYSQL_PASSWORD" in str(excinfo.value)
