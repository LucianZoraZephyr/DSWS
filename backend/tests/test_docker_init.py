"""Docker init 编排的单元测试（不连接真实存储）。"""

from __future__ import annotations

from subprocess import CompletedProcess

import pytest

from app.scripts import docker_init


class FakeRunner:
    def __init__(self, returncodes: list[int]) -> None:
        self._codes = list(returncodes)
        self.commands: list[list[str]] = []

    def __call__(self, command: list[str], **_: object) -> CompletedProcess[str]:
        self.commands.append(list(command))
        return CompletedProcess(command, self._codes.pop(0))


def _flags(runner: FakeRunner) -> list[str]:
    return [command[-1] for command in runner.commands]


def test_initialize_skips_rebuild_when_meta_verify_passes() -> None:
    runner = FakeRunner([0, 0, 0])

    docker_init.initialize(runner=runner, wait=lambda: None)

    assert _flags(runner) == [
        "--provision-ro-account",
        "--verify-ro-account",
        "--verify",
    ]


def test_initialize_rebuilds_after_failed_verify() -> None:
    runner = FakeRunner([0, 0, 1, 0, 0])

    docker_init.initialize(runner=runner, wait=lambda: None)

    assert _flags(runner) == [
        "--provision-ro-account",
        "--verify-ro-account",
        "--verify",
        "--fresh",
        "--verify",
    ]


def test_initialize_stops_on_rebuild_failure() -> None:
    runner = FakeRunner([0, 0, 1, 1])

    with pytest.raises(SystemExit) as error:
        docker_init.initialize(runner=runner, wait=lambda: None)

    assert error.value.code == 1
    assert _flags(runner) == [
        "--provision-ro-account",
        "--verify-ro-account",
        "--verify",
        "--fresh",
    ]


def test_initialize_stops_when_final_verify_fails() -> None:
    runner = FakeRunner([0, 0, 1, 0, 1])

    with pytest.raises(SystemExit) as error:
        docker_init.initialize(runner=runner, wait=lambda: None)

    assert error.value.code == 1
    assert _flags(runner) == [
        "--provision-ro-account",
        "--verify-ro-account",
        "--verify",
        "--fresh",
        "--verify",
    ]


class FakeResponse:
    def __init__(self, status_code: int, payload: dict[str, object]) -> None:
        self.status_code = status_code
        self._payload = payload

    def json(self) -> dict[str, object]:
        return self._payload


def _patch_es_health(
    monkeypatch: pytest.MonkeyPatch, *, status: str
) -> None:
    response = FakeResponse(200, {"status": status, "timed_out": True})

    def fake_get(url: str, **_: object) -> FakeResponse:
        return response

    monkeypatch.setattr(docker_init.httpx, "get", fake_get)


def test_check_es_ready_rejects_red_cluster(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _patch_es_health(monkeypatch, status="red")

    with pytest.raises(RuntimeError):
        docker_init._check_es_ready("http://es:9200/_cluster/health")


def test_check_es_ready_accepts_yellow_cluster(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _patch_es_health(monkeypatch, status="yellow")

    docker_init._check_es_ready("http://es:9200/_cluster/health")
