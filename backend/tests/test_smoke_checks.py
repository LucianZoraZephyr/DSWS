"""`scripts/smoke_checks.py` 的行为测试。

这个文件是 docker 验收脚本的"检查"部分。两个坑：

1. **不能用 assert。** 宿主机上 `PYTHONOPTIMIZE=1` 会让 `python -O` 把
   assert 直接剥掉——验收脚本于是永远"通过"，它验的东西根本没验。
   所以检查一律用显式的返回码，而且要真的在 `-O` 下跑一遍证明这一点。
2. **SSE 的流式判据不能靠"分片数 >= 2"。** 载荷一大，被代理攒起来一次
   发出的响应也会被拆成好几个分片，"分片 >= 2" 于是照样过，缓冲开着
   也测不出来。真正的证据是**到达时刻**：首尾分片几乎同时到达 = 攒完
   了才一次性发。
"""

from __future__ import annotations

import ast
import importlib.util
import json
import os
import socket
import subprocess
import sys
import threading
import time
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
SCRIPT = ROOT / "scripts" / "smoke_checks.py"


def _load_module():
    spec = importlib.util.spec_from_file_location("smoke_checks", SCRIPT)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


smoke = _load_module()


# ---------------------------------------------------------------------------
# health
# ---------------------------------------------------------------------------


def test_health_accepts_a_healthy_stack() -> None:
    assert smoke.check_health('{"status":"ok","stage":"S7"}') == 0


def test_health_rejects_a_degraded_stack() -> None:
    assert smoke.check_health('{"status":"degraded","stage":"S3"}') == 1


def test_health_rejects_a_wrong_stage() -> None:
    assert smoke.check_health('{"status":"ok","stage":"S6"}') == 1


def test_health_rejects_garbage() -> None:
    assert smoke.check_health("not json at all") == 1


def test_health_check_still_fails_under_optimize() -> None:
    """`python -O` 不能把检查削成空壳。

    这是验收脚本最要命的一种失效：脚本一路绿灯，最后打印
    "DOCKER SMOKE PASS"，而它验的东西根本没验。
    """
    result = subprocess.run(
        [sys.executable, "-O", str(SCRIPT), "health"],
        input='{"status":"degraded","stage":"S3"}',
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=60,
    )
    assert result.returncode != 0, result.stdout


def test_health_check_still_passes_under_optimize() -> None:
    result = subprocess.run(
        [sys.executable, "-O", str(SCRIPT), "health"],
        input='{"status":"ok","stage":"S7"}',
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=60,
    )
    assert result.returncode == 0, result.stderr


# ---------------------------------------------------------------------------
# services
# ---------------------------------------------------------------------------


GOOD_TABLE = (
    "mysql\trunning\thealthy\t0\n"
    "elasticsearch\trunning\thealthy\t0\n"
    "qdrant\trunning\thealthy\t0\n"
    "backend\trunning\thealthy\t0\n"
    "frontend\trunning\thealthy\t0\n"
    "init\texited\t\t0\n"
)


def test_services_accepts_a_healthy_stack() -> None:
    assert smoke.check_services(GOOD_TABLE) == 0


def test_services_requires_every_long_running_service() -> None:
    table = GOOD_TABLE.replace("qdrant\trunning\thealthy\t0\n", "")
    assert smoke.check_services(table) == 1


def test_services_requires_init_to_have_exited_zero() -> None:
    table = GOOD_TABLE.replace("init\texited\t\t0\n", "init\trunning\t\t0\n")
    assert smoke.check_services(table) == 1


def test_services_rejects_an_unhealthy_service() -> None:
    table = GOOD_TABLE.replace(
        "backend\trunning\thealthy\t0\n", "backend\trunning\tunhealthy\t0\n"
    )
    assert smoke.check_services(table) == 1


# ---------------------------------------------------------------------------
# steps
# ---------------------------------------------------------------------------


def _steps_payload(count: int) -> bytes:
    return json.dumps({"steps": [{"id": i} for i in range(count)]}).encode("utf-8")


def test_steps_accepts_twelve_steps() -> None:
    with _FakeHttp(lambda conn: _send_json(conn, _steps_payload(12))) as server:
        assert smoke.check_steps("http://127.0.0.1:" + str(server.port)) == 0


def test_steps_rejects_the_wrong_count() -> None:
    with _FakeHttp(lambda conn: _send_json(conn, _steps_payload(11))) as server:
        assert smoke.check_steps("http://127.0.0.1:" + str(server.port)) == 1


def test_steps_reports_a_broken_response_instead_of_a_traceback(
    capsys: pytest.CaptureFixture[str],
) -> None:
    """后端回的不是 JSON 时，要给一句话，不能甩一段栈回溯。

    代理配错时 /api/steps 回的往往是一张 HTML 错误页。原来这里直接
    json.load，抛的是 JSONDecodeError——验收脚本的读者看到的是一段
    Python 栈回溯，而不是"这个地址返回的不是 JSON"。
    """
    def _html_error(conn: socket.socket) -> None:
        conn.sendall(
            b"HTTP/1.1 502 Bad Gateway\r\n"
            b"Content-Type: text/html\r\n"
            b"Content-Length: 13\r\n"
            b"Connection: close\r\n"
            b"\r\n<html>502</html>"
        )

    with _FakeHttp(_html_error) as server:
        assert smoke.check_steps("http://127.0.0.1:" + str(server.port)) == 1
    captured = capsys.readouterr()
    assert "Traceback" not in captured.err, captured.err
    assert captured.err.strip() != ""


# ---------------------------------------------------------------------------
# SSE：流式 vs 缓冲
# ---------------------------------------------------------------------------


def _result_event(rows: int = 2) -> dict:
    return {
        "type": "result",
        "data": {
            "rows": [{"c": i} for i in range(rows)],
            "columns": ["c"],
        },
    }


def _sse_body(*events: dict) -> bytes:
    return b"".join(
        ("data: " + json.dumps(event, ensure_ascii=False) + "\n\n").encode("utf-8")
        for event in events
    )


def _send_chunked(conn: socket.socket, body: bytes) -> None:
    conn.sendall(b"%x\r\n" % len(body) + body + b"\r\n")


def _sse_headers() -> bytes:
    return (
        b"HTTP/1.1 200 OK\r\n"
        b"Content-Type: text/event-stream; charset=utf-8\r\n"
        b"Transfer-Encoding: chunked\r\n"
        b"Connection: close\r\n"
        b"\r\n"
    )


def _streamed_responder(conn: socket.socket) -> None:
    """边生成边发：每个事件之间隔 150ms。"""
    conn.sendall(_sse_headers())
    for event in ({"type": "progress"}, _result_event()):
        _send_chunked(conn, _sse_body(event))
        time.sleep(0.15)
    conn.sendall(b"0\r\n\r\n")


def _split(body: bytes, pieces: int) -> list[bytes]:
    step = max(1, len(body) // pieces)
    return [body[index : index + step] for index in range(0, len(body), step)]


def _buffered_responder(recorded: list[int] | None = None):
    """代理开了缓冲：先等 300ms，再一口气把整个响应发出去。

    故意拆成好几个分片：分片多并不能证明是流式到达——攒起来一次发
    的响应照样被拆成好几个分片。所以判据只能是**到达时刻**。
    """

    def _responder(conn: socket.socket) -> None:
        conn.sendall(_sse_headers())
        time.sleep(0.3)
        body = _sse_body({"type": "progress"}, _result_event())
        pieces = _split(body, 4)
        if recorded is not None:
            recorded.append(len(pieces))
        for piece in pieces:
            _send_chunked(conn, piece)
        conn.sendall(b"0\r\n\r\n")

    return _responder


def _no_events_responder(conn: socket.socket) -> None:
    conn.sendall(_sse_headers())
    _send_chunked(conn, "（这里什么都没有）\n".encode("utf-8"))
    conn.sendall(b"0\r\n\r\n")


def _empty_rows_responder(conn: socket.socket) -> None:
    conn.sendall(_sse_headers())
    _send_chunked(conn, _sse_body(_result_event(rows=0)))
    conn.sendall(b"0\r\n\r\n")


def _sse_base(server: "_FakeHttp") -> str:
    return "http://127.0.0.1:" + str(server.port)


def test_sse_accepts_a_streamed_response() -> None:
    with _FakeHttp(_streamed_responder) as server:
        assert smoke.check_sse(_sse_base(server), attempts=1) == 0


def test_sse_does_not_double_the_slash_when_the_base_url_ends_with_one() -> None:
    """DOCKER_BASE_URL 末尾带斜杠时，请求行里不能出现 //api/query。

    nginx 的代理配置是 ``proxy_pass $backend_upstream$request_uri;``——
    $request_uri 是原样的请求行，双斜杠会被原样转给后端，后端回 404。
    """
    with _FakeHttp(_streamed_responder) as server:
        assert smoke.check_sse(_sse_base(server) + "/", attempts=1) == 0
    assert server.requests, "假服务一个请求都没收到"
    assert all("//api" not in line for line in server.requests), server.requests


def test_sse_rejects_a_buffered_response() -> None:
    """缓冲开着时必须失败——哪怕分片数 >= 2。"""
    with _FakeHttp(_buffered_responder()) as server:
        assert smoke.check_sse(_sse_base(server), attempts=1) == 1


def test_the_chunk_count_alone_would_not_catch_buffering() -> None:
    """分片数 >= 2 不能证明是流式——判据只能是到达时刻。

    这个假响应确实被拆成了 4 个分片（攒起来一次发的响应也会被拆），
    但它们是同时到的：所以必须判失败。老的"分片 >= 2"判据在这里
    会放行——这正是要修的那个洞。
    """
    recorded: list[int] = []
    with _FakeHttp(_buffered_responder(recorded)) as server:
        assert smoke.check_sse(_sse_base(server), attempts=1) == 1
    assert recorded == [4], recorded


def test_sse_buffered_message_points_at_the_nginx_setting(capsys) -> None:
    with _FakeHttp(_buffered_responder()) as server:
        smoke.check_sse(_sse_base(server), attempts=1)
    captured = capsys.readouterr()
    assert "proxy_buffering" in captured.err, captured.err


def test_sse_rejects_a_response_without_events() -> None:
    with _FakeHttp(_no_events_responder) as server:
        assert smoke.check_sse(_sse_base(server), attempts=1) == 1


def test_sse_rejects_an_empty_result() -> None:
    with _FakeHttp(_empty_rows_responder) as server:
        assert smoke.check_sse(_sse_base(server), attempts=1) == 1


def _instant_responder(conn: socket.socket) -> None:
    """一口气就答完：快到分辨不出是否流式。"""
    conn.sendall(_sse_headers())
    _send_chunked(conn, _sse_body({"type": "progress"}, _result_event()))
    conn.sendall(b"0\r\n\r\n")


# nginx 的生效配置（"代理有没有开着缓冲"）——"太快"这一档唯一的证据。
NGINX_CONF_BUFFERING_OFF = """
server {
    location /api/ {
        proxy_pass http://backend:8100;
        proxy_buffering off;
    }
}
"""

NGINX_CONF_BUFFERING_ON = """
server {
    location /api/ {
        proxy_pass http://backend:8100;
        proxy_buffering on;
    }
}
"""


def test_sse_retries_an_inconclusive_response(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """快到来不及分辨流式与否时，重试几次再下结论。

    这种情况下没有证据说它被缓冲，所以不判失败——但也不能一次就放过。
    """
    monkeypatch.setenv("SMOKE_SSE_ATTEMPTS", "3")
    with _FakeHttp(_instant_responder) as server:
        assert (
            smoke.check_sse(
                _sse_base(server), nginx_conf=NGINX_CONF_BUFFERING_OFF
            )
            == 0
        )
    assert server.connections == 3, server.connections


def _silent_responder(conn: socket.socket) -> None:
    """连接建立后一个字都不发就关掉。"""
    return


def _plain_responder(conn: socket.socket) -> None:
    """一次发完，没有分片传输。"""
    conn.sendall(
        b"HTTP/1.1 200 OK\r\n"
        b"Content-Type: text/event-stream; charset=utf-8\r\n"
        b"Content-Length: 32\r\n"
        b"Connection: close\r\n"
        b"\r\n" + b"data: {\"type\": \"result\"}\n\n" + b" " * 8
    )


def test_sse_rejects_a_response_with_no_headers_at_all() -> None:
    """服务连一个字节都没回——要报清楚，不能抛 IndexError。

    原来的写法是 ``header_text.splitlines()[0]``：响应头为空时
    splitlines() 返回空列表，取 [0] 直接 IndexError，验收脚本于是打印
    一段 Python 栈回溯——真正的原因（服务没回话）一个字都不提。
    """
    with _FakeHttp(_silent_responder) as server:
        assert smoke.check_sse(_sse_base(server), attempts=1) == 1


def test_sse_reports_a_refused_connection_instead_of_a_traceback(
    capsys: pytest.CaptureFixture[str],
) -> None:
    """连不上服务时要给一句话，不能甩一段栈回溯出来。

    栈回溯对验收脚本的读者毫无意义：真正的原因是"端口上没人听"。
    """
    # 端口 1 上不会有东西在听——连过去必然被拒。
    assert smoke.check_sse("http://127.0.0.1:1", attempts=1) == 1
    captured = capsys.readouterr()
    assert "Traceback" not in captured.err, captured.err
    assert "sse_verdict=" in captured.out, captured.out


def test_sse_rejects_a_response_that_is_not_chunked() -> None:
    """不是分片传输就不可能流式——判失败。"""
    with _FakeHttp(_plain_responder) as server:
        assert smoke.check_sse(_sse_base(server), attempts=1) == 1


def test_sse_rejects_a_streamed_response_when_the_nginx_config_still_buffers() -> None:
    """时间上像流式，nginx 却说开着缓冲——按 nginx 的说法判失败。

    nginx 开着缓冲时，响应大到撑满代理缓冲，它会先发一整块再把剩下的
    一次发完——从客户端看也是"分片隔开到达"，时间上的判据看不出区别。
    nginx 的生效配置才是确定性的那一份证据。
    """
    with _FakeHttp(_streamed_responder) as server:
        assert (
            smoke.check_sse(
                _sse_base(server),
                attempts=1,
                nginx_conf=NGINX_CONF_BUFFERING_ON,
            )
            == 1
        )


def test_the_sse_check_reports_its_verdict(capsys) -> None:
    """判据要能被脚本读到。

    "太快，分辨不出来"这一档要靠 nginx 的生效配置兜底——脚本得知道
    这一次的结论是什么，才好决定要不要查配置。
    """
    with _FakeHttp(_streamed_responder) as server:
        assert smoke.check_sse(_sse_base(server), attempts=1) == 0
    assert "sse_verdict=streamed" in capsys.readouterr().out


def test_the_sse_check_reports_too_fast(capsys) -> None:
    with _FakeHttp(_instant_responder) as server:
        assert (
            smoke.check_sse(
                _sse_base(server), attempts=1, nginx_conf=NGINX_CONF_BUFFERING_OFF
            )
            == 0
        )
    assert "sse_verdict=too-fast" in capsys.readouterr().out


# ---------------------------------------------------------------------------
# "太快，分辨不出来"这一档不能就这么报通过
#
# 没有证据说它被缓冲，**也不等于**有证据说它是流式。响应快到 200 毫秒内
# 就结束的时候，时间上分辨不出来——这一档唯一的证据是 nginx 的生效配置：
# 代理开着缓冲，SSE 就是被攒成整包再发的。
#
# 原来那一档直接放过（返回 0），于是"没有证据"变成了"验收通过"——
# 验收脚本最要命的一种失效：报 PASS，而它验的东西根本没验。
# ---------------------------------------------------------------------------


def test_sse_refuses_to_call_an_inconclusive_response_a_pass() -> None:
    """拿不到 nginx 的生效配置，就不能给这一档定性——更不能报"通过"。"""
    with _FakeHttp(_instant_responder) as server:
        assert smoke.check_sse(_sse_base(server), attempts=1) == 1


def test_sse_accepts_an_inconclusive_response_when_the_nginx_config_is_clean() -> None:
    """nginx 的生效配置说 /api/ 关着缓冲——这一档就有据可依了。"""
    with _FakeHttp(_instant_responder) as server:
        assert (
            smoke.check_sse(
                _sse_base(server), attempts=1, nginx_conf=NGINX_CONF_BUFFERING_OFF
            )
            == 0
        )


def test_sse_rejects_an_inconclusive_response_when_the_nginx_config_still_buffers() -> (
    None
):
    """nginx 的生效配置说 /api/ 还开着缓冲——那就是被攒成整包了。"""
    with _FakeHttp(_instant_responder) as server:
        assert (
            smoke.check_sse(
                _sse_base(server), attempts=1, nginx_conf=NGINX_CONF_BUFFERING_ON
            )
            == 1
        )


def test_the_sse_command_rejects_a_nginx_conf_without_a_path() -> None:
    """用法错误要报 2（环境/用法不对），不是 1（检查不通过）。"""
    result = subprocess.run(
        [sys.executable, str(SCRIPT), "sse", "http://127.0.0.1:1/", "--nginx-conf"],
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=60,
    )
    assert result.returncode == 2, result.stdout + result.stderr


def test_the_sse_command_rejects_a_missing_nginx_conf_file(tmp_path: Path) -> None:
    result = subprocess.run(
        [
            sys.executable,
            str(SCRIPT),
            "sse",
            "http://127.0.0.1:1/",
            "--nginx-conf",
            str(tmp_path / "no-such-file.conf"),
        ],
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=60,
    )
    assert result.returncode == 2, result.stdout + result.stderr


def test_sse_probe_works_under_optimize() -> None:
    """`-O` 下的 SSE 检查也得是真检查。"""
    with _FakeHttp(_buffered_responder()) as server:
        env = dict(os.environ)
        env["SMOKE_SSE_ATTEMPTS"] = "1"
        result = subprocess.run(
            [
                sys.executable,
                "-O",
                str(SCRIPT),
                "sse",
                _sse_base(server),
            ],
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            env=env,
            timeout=120,
        )
    assert result.returncode != 0, result.stdout


# ---------------------------------------------------------------------------
# 假 HTTP 服务
# ---------------------------------------------------------------------------


def _send_json(conn: socket.socket, payload: bytes) -> None:
    conn.sendall(
        b"HTTP/1.1 200 OK\r\n"
        b"Content-Type: application/json; charset=utf-8\r\n"
        b"Content-Length: " + str(len(payload)).encode() + b"\r\n"
        b"Connection: close\r\n"
        b"\r\n" + payload
    )


class _FakeHttp:
    """一次性的假 HTTP 服务：按 responder 的行为回话。"""

    def __init__(self, responder) -> None:
        self._responder = responder
        self.connections = 0
        self.requests: list[str] = []
        self._sock = socket.socket()
        self._sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        self._sock.bind(("127.0.0.1", 0))
        self._sock.listen(8)
        self.port = int(self._sock.getsockname()[1])
        self._thread = threading.Thread(target=self._serve, daemon=True)
        self._thread.start()

    def _serve(self) -> None:
        while True:
            try:
                conn, _ = self._sock.accept()
            except OSError:
                return
            self.connections += 1
            try:
                request = conn.recv(65536)
                self.requests.append(
                    request.split(b"\r\n", 1)[0].decode("latin-1", errors="replace")
                )
                self._responder(conn)
            except OSError:
                pass
            finally:
                try:
                    conn.close()
                except OSError:
                    pass

    def close(self) -> None:
        try:
            self._sock.close()
        except OSError:
            pass

    def __enter__(self) -> "_FakeHttp":
        return self

    def __exit__(self, *exc: object) -> bool:
        self.close()
        return False


# ---------------------------------------------------------------------------
# nginx 生效配置
#
# 判据必须是"处理 /api/ 请求的那个 location 的**有效** proxy_buffering"。
#
# 原来那条 `grep -q "proxy_buffering off;"` 扫的是 nginx -T 的**全部**输出：
# 只要配置里**任何地方**出现过这句话就算过。于是把 /api/ 的缓冲改成 on、
# 而另一个 location（比如 /static/）留着 off，这一条照样"通过"——SSE 实际
# 已经被攒成整包，验收却报 PASS。
# ---------------------------------------------------------------------------


API_SSE_PATH = "/api/query"


def _nginx_dump(*sections: tuple[str, str]) -> str:
    """拼一份 `nginx -T` 的输出。

    真实的 `nginx -T` 会先打一行 `# configuration file <路径>:`，再打那个
    文件的内容；多个文件就这么一段段拼起来。
    """
    parts: list[str] = []
    for path, body in sections:
        parts.append("# configuration file " + path + ":")
        parts.append(body)
    return "\n".join(parts) + "\n"


def test_nginx_accepts_the_repository_configuration() -> None:
    """仓库自己的 nginx.conf 必须能通过——它就是部署用的那份。"""
    body = (ROOT / "docker" / "nginx.conf").read_text(encoding="utf-8")
    dump = _nginx_dump(("/etc/nginx/conf.d/default.conf", body))
    assert smoke.check_nginx(dump) == 0


def test_nginx_rejects_a_config_where_the_api_location_keeps_buffering_on() -> None:
    dump = _nginx_dump(
        (
            "/etc/nginx/conf.d/default.conf",
            "server {\n"
            "    location /api/ {\n"
            "        proxy_buffering on;\n"
            "    }\n"
            "}\n",
        )
    )
    assert smoke.check_nginx(dump) == 1


def test_nginx_rejects_a_config_where_buffering_is_off_only_in_another_location() -> None:
    """缓冲只在**别的** location 关着不算数。

    这是原来那条 grep 判据漏掉的洞：`grep "proxy_buffering off;"` 扫的是
    整个配置，别处留一句 off 就能让 /api/ 开着缓冲通过验收。
    """
    dump = _nginx_dump(
        (
            "/etc/nginx/conf.d/default.conf",
            "server {\n"
            "    location /static/ {\n"
            "        proxy_buffering off;\n"
            "    }\n"
            "    location /api/ {\n"
            "        proxy_buffering on;\n"
            "    }\n"
            "}\n",
        )
    )
    assert smoke.check_nginx(dump) == 1


def test_nginx_inherits_the_setting_from_the_enclosing_server_block() -> None:
    """proxy_buffering 可以写在 server 里，由 location 继承。"""
    dump = _nginx_dump(
        (
            "/etc/nginx/conf.d/default.conf",
            "server {\n"
            "    proxy_buffering off;\n"
            "    location /api/ {\n"
            "        proxy_pass http://backend;\n"
            "    }\n"
            "}\n",
        )
    )
    assert smoke.check_nginx(dump) == 0


def test_nginx_rejects_a_config_with_no_api_location() -> None:
    """连处理 /api/ 的 location 都没有——SSE 根本没被代理，判失败。"""
    dump = _nginx_dump(
        (
            "/etc/nginx/conf.d/default.conf",
            "server {\n"
            "    location / {\n"
            "        try_files $uri $uri/ /index.html;\n"
            "    }\n"
            "}\n",
        )
    )
    assert smoke.check_nginx(dump) == 1


def test_nginx_ignores_a_commented_out_directive() -> None:
    """注释掉的 off 不算数——它不生效。"""
    dump = _nginx_dump(
        (
            "/etc/nginx/conf.d/default.conf",
            "server {\n"
            "    location /api/ {\n"
            "        # proxy_buffering off;\n"
            "    }\n"
            "}\n",
        )
    )
    assert smoke.check_nginx(dump) == 1


def test_nginx_resolves_an_included_file_in_the_main_configuration() -> None:
    """include 进来的文件要和主配置拼在一起看。

    真实的 nginx.conf 在 http 里 `include /etc/nginx/conf.d/*.conf;`，
    而 conf.d/default.conf 里才有那个 location。继承关系要连着算。
    """
    main = (
        "http {\n"
        "    proxy_buffering off;\n"
        "    include /etc/nginx/conf.d/*.conf;\n"
        "}\n"
    )
    default = (
        "server {\n"
        "    location /api/ {\n"
        "        proxy_pass http://backend;\n"
        "    }\n"
        "}\n"
    )
    dump = _nginx_dump(
        ("/etc/nginx/nginx.conf", main),
        ("/etc/nginx/conf.d/default.conf", default),
    )
    assert smoke.check_nginx(dump) == 0


def test_nginx_rejects_an_empty_configuration() -> None:
    """空配置——拿不到生效配置时不能当成通过。"""
    assert smoke.check_nginx("") == 1


# ---------------------------------------------------------------------------
# 正则 location
#
# nginx 的 location 匹配顺序是：精确（=）→ 最长前缀（带 ^~ 就到此为止）
# → 按配置顺序第一个匹配的正则（~ / ~*）→ 最长前缀。
#
# 跳过正则 location 的话，判断会落到一个**根本不会生效**的 location 上：
# 配置里真正处理 /api/ 的那个正则 location 开着缓冲，另一个谁都不管
# 的 location 写着 off——验收就会报 PASS，而 SSE 实际被攒成整包。
# ---------------------------------------------------------------------------


def test_nginx_judges_the_regex_location_that_actually_serves_the_api() -> None:
    """真正处理 /api/query 的是正则 location——判断必须落在它身上。"""
    dump = _nginx_dump(
        (
            "/etc/nginx/conf.d/default.conf",
            "server {\n"
            "    location ~ ^/api/ {\n"
            "        proxy_buffering on;\n"
            "    }\n"
            "    location / {\n"
            "        proxy_buffering off;\n"
            "    }\n"
            "}\n",
        )
    )
    assert smoke.check_nginx(dump) == 1


def test_nginx_accepts_a_regex_location_with_buffering_off() -> None:
    """正则 location 关着缓冲就算过。"""
    dump = _nginx_dump(
        (
            "/etc/nginx/conf.d/default.conf",
            "server {\n"
            "    location ~* ^/api/ {\n"
            "        proxy_buffering off;\n"
            "    }\n"
            "}\n",
        )
    )
    assert smoke.check_nginx(dump) == 0


def test_nginx_ignores_a_regex_location_that_does_not_match_the_path() -> None:
    """不匹配 /api/query 的正则 location 不算数。"""
    dump = _nginx_dump(
        (
            "/etc/nginx/conf.d/default.conf",
            "server {\n"
            "    location ~ \\.php$ {\n"
            "        proxy_buffering on;\n"
            "    }\n"
            "    location /api/ {\n"
            "        proxy_buffering off;\n"
            "    }\n"
            "}\n",
        )
    )
    assert smoke.check_nginx(dump) == 0


def test_nginx_refuses_to_pass_when_the_matching_regex_cannot_be_compiled() -> None:
    """判不了的时候不能报通过。

    nginx 用的是 PCRE，Python 的 re 不是它的超集（原子组 ``(?>...)`` 就是
    一例）。编译不了的正则没法判断匹不匹配——也就是没法知道真正处理
    /api/query 的是哪个 location。这种时候只能判失败。
    """
    dump = _nginx_dump(
        (
            "/etc/nginx/conf.d/default.conf",
            "server {\n"
            "    location ~ (?>api) {\n"
            "        proxy_buffering on;\n"
            "    }\n"
            "    location / {\n"
            "        proxy_buffering off;\n"
            "    }\n"
            "}\n",
        )
    )
    assert smoke.check_nginx(dump) == 1


def test_nginx_honours_a_directive_only_included_file() -> None:
    """被 include 进来的文件里只有指令时，那些指令也要算数。

    ``proxy_params`` 这类文件就是纯指令。丢掉它们的话，http 块上写着的
    ``proxy_buffering off`` 会被漏掉，location 于是被判成默认的 on——
    一份明明关着缓冲的配置被报成失败。
    """
    dump = _nginx_dump(
        (
            "/etc/nginx/nginx.conf",
            "http {\n"
            "    include /etc/nginx/proxy_off.conf;\n"
            "    include /etc/nginx/conf.d/*.conf;\n"
            "}\n",
        ),
        ("/etc/nginx/proxy_off.conf", "proxy_buffering off;\n"),
        (
            "/etc/nginx/conf.d/default.conf",
            "server {\n"
            "    location /api/ {\n"
            "        proxy_pass http://backend;\n"
            "    }\n"
            "}\n",
        ),
    )
    assert smoke.check_nginx(dump) == 0


def test_nginx_resolves_an_include_written_as_a_relative_path() -> None:
    """include 写相对路径时也要能对上 nginx -T 打印的绝对路径。

    nginx 把相对路径按 prefix 解析，而 ``nginx -T`` 打印的是绝对路径。
    """
    dump = _nginx_dump(
        (
            "/etc/nginx/nginx.conf",
            "http {\n"
            "    include conf.d/*.conf;\n"
            "}\n",
        ),
        (
            "/etc/nginx/conf.d/default.conf",
            "server {\n"
            "    location /api/ {\n"
            "        proxy_buffering off;\n"
            "    }\n"
            "}\n",
        ),
    )
    assert smoke.check_nginx(dump) == 0


def test_nginx_survives_an_unbalanced_single_quote() -> None:
    """nginx 只把 " 当引号；落单的 ' 是普通字符，不能把后面的配置吃掉。

    当成引号处理的话，落单的 ' 会把文件剩下的部分全吞掉——真正处理
    /api/ 的 location 就这么没了，报错还会说"没有处理 /api/query 的
    location"，指着一个根本没问题的配置。
    """
    dump = _nginx_dump(
        (
            "/etc/nginx/conf.d/default.conf",
            "server {\n"
            "    add_header X-Odd 'oops;\n"
            "    location /api/ {\n"
            "        proxy_buffering off;\n"
            "    }\n"
            "}\n",
        )
    )
    assert smoke.check_nginx(dump) == 0


def test_nginx_reads_the_shape_a_real_nginx_dump_has() -> None:
    """真实的 ``nginx -T`` 长成什么样，判据就得读得懂什么样。

    真实输出是好几段 ``# configuration file <路径>:`` 拼起来的：主配置里
    只有 http 块和 include，真正的 location 在被 include 的文件里，
    中间还夹着 mime.types 这种纯指令的片段和带单引号的 log_format。
    """
    dump = _nginx_dump(
        (
            "/etc/nginx/nginx.conf",
            "user nginx;\n"
            "worker_processes auto;\n"
            "http {\n"
            "    include /etc/nginx/mime.types;\n"
            "    log_format main '$remote_addr - $remote_user [$time_local]';\n"
            "    access_log /var/log/nginx/access.log main;\n"
            "    include /etc/nginx/conf.d/*.conf;\n"
            "}\n",
        ),
        ("/etc/nginx/mime.types", "types {\n    text/html html;\n}\n"),
        (
            "/etc/nginx/conf.d/default.conf",
            "server {\n"
            "    listen 80;\n"
            "    location /api/ {\n"
            "        proxy_pass http://backend:8100;\n"
            "        proxy_buffering off;\n"
            "    }\n"
            "    location / {\n"
            "        root /usr/share/nginx/html;\n"
            "    }\n"
            "}\n",
        ),
    )
    assert smoke.check_nginx(dump) == 0


def test_nginx_ignores_the_lines_printed_before_the_first_configuration_file() -> None:
    """``nginx -T`` 的输出前面可能夹着几行提示（空行、"syntax is ok" 之类）。

    那些提示不属于任何一份配置文件。判据原来把第一段提示当成了主配置，
    主配置里的 location 就全丢了——一份好好的配置被判成"没有处理
    /api/ 的 location"，验收于是误报失败，把人引到错的方向上。
    """
    dump = (
        "\n"
        "nginx: the configuration file /etc/nginx/nginx.conf syntax is ok\n"
        + _nginx_dump(
            (
                "/etc/nginx/nginx.conf",
                "http {\n"
                "    server {\n"
                "        location /api/ {\n"
                "            proxy_pass http://backend:8100;\n"
                "            proxy_buffering off;\n"
                "        }\n"
                "    }\n"
                "}\n",
            )
        )
    )
    assert smoke.check_nginx(dump) == 0


def test_the_nginx_check_still_fails_under_optimize() -> None:
    result = subprocess.run(
        [sys.executable, "-O", str(SCRIPT), "nginx"],
        input=_nginx_dump(
            (
                "/etc/nginx/conf.d/default.conf",
                "server {\n"
                "    location /api/ {\n"
                "        proxy_buffering on;\n"
                "    }\n"
                "}\n",
            )
        ),
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=60,
    )
    assert result.returncode != 0, result.stdout


# ---------------------------------------------------------------------------
# 脚本本身
# ---------------------------------------------------------------------------


def test_the_acceptance_checker_has_no_assert_statements() -> None:
    """验收判据里不许有 Python 的 assert 语句（`-O` 会把它剥掉）。

    这条规则说的是 **Python 的 assert 语句**，所以要看的是
    ``scripts/smoke_checks.py``。原来这条测试去看的是 shell 脚本——
    shell 里永远不会有 Python 的 assert，于是这条测试什么都拦不住：
    有人往 ``check_services`` 里塞一个 assert，它照样绿。
    """
    source = SCRIPT.read_text(encoding="utf-8")
    tree = ast.parse(source)
    offenders = [
        "line " + str(node.lineno)
        for node in ast.walk(tree)
        if isinstance(node, ast.Assert)
    ]
    assert not offenders, "smoke_checks.py 里出现了 assert：" + ", ".join(offenders)


def test_the_smoke_script_has_no_bare_asserts() -> None:
    """shell 脚本里也不许藏着 `python -c "assert ..."` 这种写法。"""
    script = (ROOT / "scripts" / "docker-smoke.sh").read_text(encoding="utf-8")
    offenders = []
    for number, line in enumerate(script.splitlines(), start=1):
        code = line.split("#", 1)[0]
        if "assert" in code:
            offenders.append(f"{number}: {line.strip()}")
    assert not offenders, "\n".join(offenders)
