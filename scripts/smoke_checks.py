#!/usr/bin/env python3
"""docker-smoke.sh 的各项检查。

跑在**宿主机**上，只用标准库——宿主机不一定装了后端的依赖，
但一定有个能跑的 Python（见 scripts/find-python.sh）。

为什么从 shell 的 heredoc 里搬出来：

1. **不能用 assert。** 宿主机的 ``PYTHONOPTIMIZE=1`` 会让 ``python -O``
   把 assert 直接剥掉：验收脚本于是永远"通过"，而它验的东西根本没验。
   这里一律用显式的返回码（见 tests/test_smoke_checks.py）。
2. **要能单测。** SSE 的流式判据藏在 heredoc 里，就只能靠真跑一遍整个栈
   才能验——而它恰恰是最容易"看起来在验、其实没验"的一处。

子命令：

  health            从 stdin 读 /api/health 的 JSON，检查 status 与 stage
  steps <base>      拉 /api/steps，检查步骤数
  services          从 stdin 读 docker compose ps 的表格，检查服务状态
  sse <base> [query] [--nginx-conf <文件>]
                   发一次真实查询，检查 SSE 是流式到达且结果非空。
                   响应太快、从时间上分辨不出是否流式时，用 --nginx-conf
                   给出 `nginx -T` 的输出——那一档只有靠它定性
  nginx             从 stdin 读 `nginx -T` 的输出，检查处理 SSE 的那个
                    location 是不是真的关着代理缓冲

退出码：0 = 通过，1 = 不通过，2 = 用法/环境不对。
"""

from __future__ import annotations

import fnmatch
import json
import os
import re
import socket
import sys
import time
import urllib.error
import urllib.request
from urllib.parse import urlsplit

HEALTH_STATUS = "ok"
HEALTH_STAGE = "S7"
EXPECTED_STEPS = 12
LONG_RUNNING_SERVICES = ("mysql", "elasticsearch", "qdrant", "backend", "frontend")
DEFAULT_QUERY = "华北销售额"

# --- SSE 流式判据 -----------------------------------------------------------
#
# 分片个数**不能**当判据：载荷一大，被代理攒起来一次性发出的响应也会被
# 拆成好几个分片，"分片 >= 2" 于是照样过，缓冲开着也测不出来。
# 真正的证据是**到达时刻**：首尾分片几乎同时到达 = 攒完了才一次性发。
#
# 首尾分片的到达时间差超过 STREAM_GAP_SECONDS，就说明是边生成边发。
STREAM_GAP_SECONDS = 0.05
# 整个响应快到 TOO_FAST_SECONDS 内就结束了 —— 那就没有证据说它被缓冲，
# 不据此判失败（nginx 的生效配置另有一步在查）。
TOO_FAST_SECONDS = 0.2
DEFAULT_SSE_ATTEMPTS = 3


def _enable_utf8_stdio() -> None:
    for stream in (sys.stdout, sys.stderr):
        reconfigure = getattr(stream, "reconfigure", None)
        if reconfigure is None:
            continue
        try:
            reconfigure(encoding="utf-8", errors="replace")
        except (ValueError, OSError, AttributeError):  # pragma: no cover
            pass


# ---------------------------------------------------------------------------
# health
# ---------------------------------------------------------------------------


def check_health(raw: str) -> int:
    try:
        data = json.loads(raw)
    except ValueError as exc:
        print("health 响应不是合法 JSON：" + str(exc), file=sys.stderr)
        return 1
    if not isinstance(data, dict):
        print("health 响应不是一个对象", file=sys.stderr)
        return 1
    status = data.get("status")
    stage = data.get("stage")
    if status != HEALTH_STATUS or stage != HEALTH_STAGE:
        print(
            "health 不满足要求：status="
            + str(status)
            + " stage="
            + str(stage)
            + "（期望 status="
            + HEALTH_STATUS
            + " stage="
            + HEALTH_STAGE
            + "）",
            file=sys.stderr,
        )
        return 1
    return 0


# ---------------------------------------------------------------------------
# services
# ---------------------------------------------------------------------------


def check_services(table: str) -> int:
    rows = [line.split("\t") for line in table.splitlines() if line.strip()]
    services = {row[0]: (row + ["", "", ""])[1:4] for row in rows}

    problems: list[str] = []
    for name in LONG_RUNNING_SERVICES:
        state, health, _ = services.get(name, ("", "", ""))
        if state != "running":
            problems.append(name + " 未运行（state=" + (state or "缺失") + "）")
        elif health != "healthy":
            problems.append(
                name + " 未健康（health=" + (health or "无 healthcheck") + "）"
            )

    init_state, _, init_exit = services.get("init", ("", "", ""))
    if init_state != "exited" or init_exit != "0":
        problems.append(
            "init 没有成功退出（state="
            + (init_state or "缺失")
            + ", exit="
            + (init_exit or "?")
            + "）"
        )

    if problems:
        print("；".join(problems), file=sys.stderr)
        return 1
    print(
        "services: "
        + ", ".join(name + "=" + value[0] for name, value in sorted(services.items()))
    )
    return 0


# ---------------------------------------------------------------------------
# steps
# ---------------------------------------------------------------------------


def check_steps(base: str) -> int:
    url = base.rstrip("/") + "/api/steps"
    try:
        with urllib.request.urlopen(url, timeout=30) as response:
            data = json.load(response)
    except (urllib.error.URLError, OSError, ValueError) as exc:
        # 代理配错时这里回的多半是一张 HTML 错误页。原来直接 json.load，
        # 抛出去的是 JSONDecodeError——验收脚本的读者看到的是一段 Python
        # 栈回溯，而不是"这个地址返回的不是 JSON"。
        print("/api/steps 拿不到可解析的 JSON：" + str(exc), file=sys.stderr)
        return 1
    steps = data.get("steps") if isinstance(data, dict) else None
    if not isinstance(steps, list):
        print("/api/steps 的返回里没有 steps 数组", file=sys.stderr)
        return 1
    if len(steps) != EXPECTED_STEPS:
        print(
            "步骤数不是 " + str(EXPECTED_STEPS) + "：" + str(len(steps)),
            file=sys.stderr,
        )
        return 1
    print("steps=" + str(EXPECTED_STEPS))
    return 0


# ---------------------------------------------------------------------------
# SSE
# ---------------------------------------------------------------------------


def parse_chunked(body: bytes) -> list[bytes]:
    """把 chunked 的响应体拆成一个个分片。"""
    chunks: list[bytes] = []
    rest = body
    while True:
        line, sep, rest = rest.partition(b"\r\n")
        if not sep:
            break
        try:
            size = int(line.split(b";")[0], 16)
        except ValueError:
            break
        if size == 0:
            break
        chunks.append(rest[:size])
        rest = rest[size + 2 :]
    return chunks


def _body_timing(
    reads: list[tuple[float, int]], header_len: int
) -> tuple[float | None, float | None]:
    """响应体第一个字节、最后一个字节的到达时刻。

    `reads` 是 [(读到的时刻, 这次读了多少字节)]。响应头可能和第一批正文
    在同一次读里到达，所以要按字节数把头部那段跳过去。
    """
    consumed = 0
    first_at: float | None = None
    last_at: float | None = None
    for at, length in reads:
        if consumed + length <= header_len:
            consumed += length
            continue
        if first_at is None:
            first_at = at
        last_at = at
        consumed += length
    return first_at, last_at


def judge_stream(
    first_at: float | None, last_at: float | None, sent_at: float
) -> tuple[str, str]:
    """判断这次响应是不是"边生成边发"。

    返回 (结论, 说明)。结论取 "streamed" / "too-fast" / "buffered" / "empty"。
    """
    if first_at is None or last_at is None:
        return "empty", "响应体是空的：没有收到任何正文"
    gap = last_at - first_at
    total = last_at - sent_at
    if gap >= STREAM_GAP_SECONDS:
        return (
            "streamed",
            "SSE 是流式到达的（首尾分片相差 " + str(round(gap, 3)) + " 秒）",
        )
    if total < TOO_FAST_SECONDS:
        return (
            "too-fast",
            "整个响应 " + str(round(total, 3)) + " 秒就结束了，"
            "快到分辨不出是否流式（没有证据说它被缓冲）",
        )
    return (
        "buffered",
        "SSE 是攒完了一次性到达的：服务器用了 "
        + str(round(total, 3))
        + " 秒，但首尾分片只差 "
        + str(round(gap, 3))
        + " 秒。代理很可能开了缓冲，"
        "请检查 docker/nginx.conf 的 proxy_buffering off",
    )


def _send_query(
    host: str, port: int, path: str, payload: bytes, timeout: float
) -> tuple[bytes, list[tuple[float, int]], float]:
    """发一次 SSE 查询。

    用裸 socket 而不是 urllib：只有裸 socket 能看到 HTTP 分片的边界和
    到达时刻。response.read() 会一次读到 EOF，代理开了缓冲也照样"成功"。

    返回 (原始响应, [(读到的时刻, 这次读的字节数)], 请求发出的时刻)。
    """
    head = (
        "POST "
        + path
        + "/api/query HTTP/1.1\r\n"
        + "Host: "
        + host
        + ":"
        + str(port)
        + "\r\n"
        + "Content-Type: application/json; charset=utf-8\r\n"
        + "Content-Length: "
        + str(len(payload))
        + "\r\n"
        + "Connection: close\r\n"
        + "\r\n"
    ).encode("ascii")

    sock = socket.create_connection((host, port), timeout=timeout)
    sock.settimeout(timeout)
    raw = b""
    reads: list[tuple[float, int]] = []
    sent_at = time.monotonic()
    try:
        sock.sendall(head + payload)
        while True:
            try:
                part = sock.recv(65536)
            except socket.timeout:
                break
            if not part:
                break
            reads.append((time.monotonic(), len(part)))
            raw += part
    finally:
        sock.close()
    return raw, reads, sent_at


def extract_events(chunks: list[bytes]) -> tuple[list[dict], str]:
    """从 SSE 分片里取出事件列表；顺带报出"一个事件都没有"这类毛病。"""
    text = b"".join(chunks).decode("utf-8", errors="replace")
    events: list[dict] = []
    for block in text.split("\n\n"):
        for line in block.splitlines():
            if not line.startswith("data:"):
                continue
            raw = line[len("data:") :].strip()
            try:
                parsed = json.loads(raw)
            except ValueError:
                continue
            if isinstance(parsed, dict):
                events.append(parsed)
    if not events:
        return [], "没有收到任何 SSE 事件"
    return events, ""


def _check_result(events: list[dict], chunk_count: int) -> int:
    final = events[-1]
    if final.get("type") != "result":
        print("最后一条不是 result：" + str(final), file=sys.stderr)
        return 1
    data = final.get("data")
    data = data if isinstance(data, dict) else {}
    rows = data.get("rows")
    rows = rows if isinstance(rows, list) else []
    if not rows:
        print("result.rows 为空", file=sys.stderr)
        return 1
    columns = data.get("columns")
    columns = columns if isinstance(columns, list) else []
    print(
        "result rows="
        + str(len(rows))
        + " columns="
        + str(len(columns))
        + " sse_chunks="
        + str(chunk_count)
    )
    return 0


def _sse_attempts() -> int:
    raw = os.getenv("SMOKE_SSE_ATTEMPTS", "")
    if not raw:
        return DEFAULT_SSE_ATTEMPTS
    try:
        value = int(raw)
    except ValueError:
        return DEFAULT_SSE_ATTEMPTS
    return max(1, value)


def check_sse(
    base: str,
    *,
    attempts: int | None = None,
    query: str = DEFAULT_QUERY,
    nginx_conf: str | None = None,
) -> int:
    parts = urlsplit(base)
    if parts.scheme != "http":
        print("DOCKER BASE URL 只支持 http://，当前为 " + parts.scheme, file=sys.stderr)
        return 2
    host = parts.hostname or "127.0.0.1"
    port = parts.port or 80
    # 末尾的斜杠要去掉：DOCKER_BASE_URL 写成 http://host:8080/ 时，
    # path 会是 "/"，拼出来的请求行就成了 "POST //api/query"。
    # nginx 的代理配置用的是 $request_uri（原样转交），双斜杠被转给
    # 后端就是 404——报错指向"请求失败"，地址写歪了这件事没人提。
    path = (parts.path or "").rstrip("/")
    payload = json.dumps({"query": query}, ensure_ascii=False).encode("utf-8")

    if attempts is None:
        attempts = _sse_attempts()

    verdict = ""
    detail = ""
    events: list[dict] = []
    event_error = ""
    chunk_count = 0
    last_error = ""

    for attempt in range(1, attempts + 1):
        try:
            raw, reads, sent_at = _send_query(host, port, path, payload, timeout=180.0)
        except OSError as exc:
            # 连不上：端口上没人听、容器还没起来、连接被掐断……
            # 原来这里直接往上抛，验收脚本于是打印一段栈回溯——真正的原因
            # （没人听）一个字都不提。
            last_error = str(exc)
            if attempt < attempts:
                time.sleep(0.5)
                continue
            break
        header_text, _, body = raw.partition(b"\r\n\r\n")
        headers = header_text.decode("latin-1").lower()
        if not header_text.strip():
            # 服务连一个字节都没回：响应头是空的。
            # 原来这里是 header_text.splitlines()[0] —— 空列表取 [0] 直接
            # IndexError，验收脚本于是打印一段 Python 栈回溯，真正的原因
            # （服务没回话）一个字都不提。
            print("sse_verdict=malformed")
            print(
                "响应里没有 HTTP 头：服务一个字节都没回（连接被掐断或根本没起来），"
                "SSE 根本没法开始",
                file=sys.stderr,
            )
            return 1
        if "transfer-encoding: chunked" not in headers:
            print("sse_verdict=not-chunked")
            first_line = header_text.splitlines()[0] if header_text.splitlines() else ""
            print(
                "响应不是分片传输，SSE 不可能流式："
                + first_line.decode("latin-1", errors="replace"),
                file=sys.stderr,
            )
            return 1

        chunks = parse_chunked(body)
        chunk_count = len(chunks)
        first_at, last_at = _body_timing(reads, len(header_text) + 4)
        verdict, detail = judge_stream(first_at, last_at, sent_at)
        events, event_error = extract_events(chunks)

        if verdict in ("streamed", "buffered", "empty"):
            # streamed：成了。buffered / empty：确定性失败，再试也是同样结果。
            break
        # too-fast：这次太快，分辨不出来——再试一次。
        time.sleep(0.5)

    # 结论要能被脚本读到："太快，分辨不出来"这一档要靠 nginx 的生效配置
    # 兜底，脚本得知道这一次的结论是什么。
    if not verdict:
        print("sse_verdict=unreachable")
        print(
            "连不上 " + host + ":" + str(port) + "：" + last_error,
            file=sys.stderr,
        )
        return 1
    print("sse_verdict=" + verdict)

    if event_error:
        print(event_error, file=sys.stderr)
        result_ok = False
    else:
        # 结果本身对不对，和流式与否是两回事：空结果不管怎么流都是失败。
        result_ok = _check_result(events, chunk_count) == 0

    if verdict == "streamed":
        # 时间上像流式，但 nginx 开着缓冲时，响应大到撑满代理缓冲，它会
        # 先发一整块再把剩下的发完——从客户端看也是"分片陆续到达"，时间
        # 上的判据分不出来。nginx 的生效配置才是确定性的那一份证据：
        # 它说还开着缓冲，就按它说的判失败。
        if nginx_conf is not None and check_nginx(nginx_conf) != 0:
            print(
                detail
                + "；但 nginx 的生效配置显示处理 /api/ 的 location 还开着缓冲"
                "——响应大到撑满代理缓冲时它会先发一整块再把剩下的发完，"
                "看上去也是分片陆续到达",
                file=sys.stderr,
            )
            return 1
        return 0 if result_ok else 1
    if verdict == "too-fast":
        # 这一档不能就这么放过：没有证据说它被缓冲，**也不等于**有证据说
        # 它是流式。响应快到 200 毫秒内就结束的时候，时间上分辨不出来——
        # 唯一的证据是 nginx 的生效配置：代理开着缓冲，SSE 就是被攒成整包
        # 再发的。拿不到配置就不能下结论，更不能报"通过"（验收脚本最要命的
        # 一种失效：报 PASS，而它验的东西根本没验）。
        if nginx_conf is None:
            print(
                detail
                + "；没有 nginx 的生效配置，这一档无法定性"
                + "（用 --nginx-conf <文件> 传 `nginx -T` 的输出）",
                file=sys.stderr,
            )
            return 1
        if check_nginx(nginx_conf) != 0:
            print(detail, file=sys.stderr)
            return 1
        print(
            detail + "；nginx 的生效配置显示处理 /api/ 的 location 关着缓冲",
            file=sys.stderr,
        )
        return 0 if result_ok else 1
    print(detail, file=sys.stderr)
    return 1


# ---------------------------------------------------------------------------
# nginx 生效配置
# ---------------------------------------------------------------------------

# SSE 走的是这个路径——判据必须落在**处理它的那个 location** 上。
NGINX_SSE_PATH = "/api/query"


class _NginxBlock:
    """nginx 配置里的一个块（http / server / location ...）。"""

    __slots__ = ("header", "directives", "children", "parent")

    def __init__(self, header: str, parent: "_NginxBlock | None") -> None:
        self.header = header
        self.directives: list[str] = []
        self.children: list["_NginxBlock"] = []
        self.parent = parent


def _nginx_sections(text: str) -> list[tuple[str, str]]:
    """把 `nginx -T` 的输出按"配置文件"切开。

    `nginx -T` 先打一行 ``# configuration file <路径>:``，再打那个文件的内容；
    多个文件就这么一段段拼起来。被 include 进来的文件也在里面——
    这正是"生效配置"的意思。

    第一个标记**之前**的内容不属于任何配置文件（空行、nginx 自己打的
    "syntax is ok" 提示），要丢掉。把它当成第一份配置的话，主配置里的
    location 就全丢了——一份好好的配置会被判成"没有处理 /api/ 的
    location"，验收误报失败，把人引到错的方向上。
    """
    sections: list[tuple[str, str]] = []
    path = ""
    lines: list[str] = []
    seen_marker = False
    for line in text.splitlines():
        if line.startswith("# configuration file "):
            if seen_marker:
                sections.append((path, "\n".join(lines)))
            path = line[len("# configuration file ") :].rstrip(":").strip()
            lines = []
            seen_marker = True
            continue
        if seen_marker:
            lines.append(line)
    if seen_marker:
        sections.append((path, "\n".join(lines)))
    elif text.strip():
        # 一个标记都没有：整份当成主配置（手写配置、只测一个片段）。
        sections.append(("", text))
    return sections


def _parse_nginx(text: str) -> _NginxBlock:
    """把一段 nginx 配置解析成块树。

    只按 ``{ }`` 和 ``;`` 切分：nginx 的注释是 # 到行尾，写在引号里的
    ``{ }`` 不是块的开合。

    只有 ``"`` 是引号——nginx 不认单引号。把落单的 ``'`` 当引号处理的话，
    它会把文件剩下的部分全吞掉（``add_header X 'oops;`` 这种写法很常见
    在日志模板里），真正处理请求的 location 就这么没了。

    返回的是根块：根块上的**指令**也要留着——被 include 进来的文件
    常常只有指令（``proxy_params`` 那种），丢掉它们继承就断了。
    """
    root = _NginxBlock("", None)
    stack = [root]
    buffer: list[str] = []
    index = 0
    length = len(text)
    while index < length:
        char = text[index]
        if char == "#":
            newline = text.find("\n", index)
            index = length if newline == -1 else newline + 1
            continue
        if char == '"':
            end = text.find(char, index + 1)
            if end == -1:
                # 引号没配上：不能把后面的配置全吃掉，当普通字符处理。
                buffer.append(char)
                index += 1
                continue
            buffer.append(text[index : end + 1])
            index = end + 1
            continue
        if char == ";":
            statement = "".join(buffer).strip()
            buffer = []
            index += 1
            if statement:
                stack[-1].directives.append(statement)
            continue
        if char == "{":
            statement = "".join(buffer).strip()
            buffer = []
            index += 1
            block = _NginxBlock(statement, stack[-1])
            stack[-1].children.append(block)
            stack.append(block)
            continue
        if char == "}":
            buffer = []
            index += 1
            if len(stack) > 1:
                stack.pop()
            continue
        buffer.append(char)
        index += 1
    return root


def _include_matches(path: str, pattern: str) -> bool:
    """include 的写法与被包含文件的路径对不对得上。

    nginx 允许相对路径（相对 prefix 解析），而 ``nginx -T`` 打印的是
    绝对路径：所以相对写法要能对上绝对路径的尾部。
    """
    pattern = pattern.strip()
    paths = [path, "/" + path.lstrip("/")]
    patterns = [pattern, "/" + pattern.lstrip("/")]
    if not pattern.startswith("/"):
        patterns.append("*/" + pattern.lstrip("./"))
    return any(
        fnmatch.fnmatchcase(candidate, candidate_pattern)
        for candidate in paths
        for candidate_pattern in patterns
    )


def _splice_includes(
    block: _NginxBlock,
    sections: list[tuple[str, str]],
    parsed: list[_NginxBlock],
    used: set[int],
) -> None:
    """把被 include 的文件挂到 include 指令所在的那个块下面。

    挂错地方继承就断了：include 写在 http 块里，被包含文件里的 location
    就得算作 http 的子块——否则 http 上的 proxy_buffering off 管不到它。

    块和指令都要挂：被包含的文件里只有指令（``proxy_params`` 那种）时，
    那些指令就是这一层的配置，丢了它们等于没 include。
    """
    for statement in list(block.directives):
        if not statement.startswith("include "):
            continue
        pattern = statement[len("include ") :].strip()
        for index, (path, _) in enumerate(sections):
            if index in used or not _include_matches(path, pattern):
                continue
            used.add(index)
            for child in parsed[index].children:
                child.parent = block
                block.children.append(child)
            block.directives.extend(parsed[index].directives)
    for child in list(block.children):
        _splice_includes(child, sections, parsed, used)


def _nginx_tree(text: str) -> _NginxBlock:
    """把 `nginx -T` 的输出拼成一棵块树。"""
    sections = _nginx_sections(text)
    root = _NginxBlock("", None)
    if not sections:
        return root
    parsed = [_parse_nginx(body) for _, body in sections]
    for block in parsed[0].children:
        block.parent = root
        root.children.append(block)
    _splice_includes(root, sections, parsed, {0})
    return root


def _location_spec(header: str) -> tuple[str, str] | None:
    """location 块的匹配方式与路径；不是 location 就返回 None。

    匹配方式：``exact``（``=``）、``prefix``、``prefix-noregex``（``^~``）、
    ``regex``（``~``）、``regex-ci``（``~*``）。
    """
    tokens = header.split()
    if not tokens or tokens[0] != "location":
        return None
    rest = tokens[1:]
    if not rest:
        return None
    if rest[0] == "=":
        return ("exact", rest[1] if len(rest) > 1 else "")
    if rest[0] == "^~":
        return ("prefix-noregex", rest[1] if len(rest) > 1 else "")
    if rest[0] == "~":
        return ("regex", rest[1] if len(rest) > 1 else "")
    if rest[0] == "~*":
        return ("regex-ci", rest[1] if len(rest) > 1 else "")
    return ("prefix", rest[0])


def _is_server(block: _NginxBlock) -> bool:
    tokens = block.header.split()
    return bool(tokens) and tokens[0] == "server"


def _collect_locations(
    block: _NginxBlock,
    server: _NginxBlock | None,
    out: list[tuple[_NginxBlock | None, _NginxBlock, tuple[str, str]]],
) -> None:
    for child in block.children:
        spec = _location_spec(child.header)
        if spec is not None:
            out.append((server, child, spec))
            continue
        _collect_locations(child, child if _is_server(child) else server, out)


class _NginxUndecidable(Exception):
    """配置里有判不了的写法——只能判失败，不能放行。"""


def _regex_matches(pattern: str, target: str, *, case_insensitive: bool) -> bool:
    """正则 location 匹不匹配这个路径。

    nginx 用的是 PCRE，Python 的 re 不是它的超集（原子组 ``(?>...)`` 就是
    一例）。编译不了的写法没法判断匹不匹配——也就没法知道真正处理请求的
    是哪个 location。这种时候只能判失败。
    """
    flags = re.IGNORECASE if case_insensitive else 0
    try:
        compiled = re.compile(pattern, flags)
    except re.error as exc:
        raise _NginxUndecidable(
            "location 的正则 " + pattern + " 判不了（" + str(exc) + "）"
        ) from exc
    return compiled.search(target) is not None


def _winning_location(
    entries: list[tuple[_NginxBlock, tuple[str, str]]], target: str
) -> _NginxBlock | None:
    """nginx 真正会拿来处理 target 的那个 location。

    nginx 的匹配顺序：精确（``=``）→ 最长前缀（带 ``^~`` 就到此为止，
    不再看正则）→ 按配置顺序第一个匹配的正则（``~`` / ``~*``）→ 最长前缀。

    正则 location 不能跳过：跳过的话，判断会落到一个**根本不会生效**的
    location 上——真正处理 /api/ 的那个正则 location 开着缓冲，另一个
    谁都不管的 location 写着 off，验收就会报 PASS。
    """
    for block, spec in entries:
        if spec[0] == "exact" and spec[1] == target:
            return block

    prefixes = [
        (len(spec[1]), block, spec)
        for block, spec in entries
        if spec[0] in ("prefix", "prefix-noregex") and target.startswith(spec[1])
    ]
    prefixes.sort(key=lambda item: item[0], reverse=True)
    longest = prefixes[0] if prefixes else None
    if longest is not None and longest[2][0] == "prefix-noregex":
        return longest[1]

    for block, spec in entries:
        if spec[0] in ("regex", "regex-ci"):
            if _regex_matches(
                spec[1], target, case_insensitive=spec[0] == "regex-ci"
            ):
                return block

    return longest[1] if longest is not None else None


def _select_locations(root: _NginxBlock, target: str) -> list[_NginxBlock]:
    """选出真正会处理 target 的 location。

    按 server 分组——请求只会落到某一个 server 上，每个 server 各选一次。
    """
    found: list[tuple[_NginxBlock | None, _NginxBlock, tuple[str, str]]] = []
    _collect_locations(root, None, found)
    by_server: dict[int, list[tuple[_NginxBlock, tuple[str, str]]]] = {}
    for server, block, spec in found:
        by_server.setdefault(id(server), []).append((block, spec))

    selected: list[_NginxBlock] = []
    for entries in by_server.values():
        winner = _winning_location(entries, target)
        if winner is not None:
            selected.append(winner)
    return selected


def _effective_proxy_buffering(block: _NginxBlock) -> str:
    """这个 location 真正生效的 proxy_buffering。

    nginx 的指令是**继承**的：location 里没写就往外层找（server → http），
    都没有就用默认值 on。
    """
    current: _NginxBlock | None = block
    while current is not None:
        for statement in current.directives:
            parts = statement.split()
            if parts and parts[0] == "proxy_buffering":
                return parts[1] if len(parts) > 1 else "on"
        current = current.parent
    return "on"


def check_nginx(config_text: str) -> int:
    """检查处理 SSE 的那个 location 是不是真的关着缓冲。

    判据必须是**生效值**：别的 location 上写着 off 不算数，注释掉的
    也不算数——nginx 真正生效的是处理 ``/api/`` 请求的那一块。原来那条
    ``grep -q "proxy_buffering off;"`` 扫的是整份输出，配置里任何地方
    出现过这句话就算过，于是把 /api/ 的缓冲改成 on、而另一个 location
    留着 off，验收照样报 PASS——SSE 实际已经被攒成整包。
    """
    root = _nginx_tree(config_text)
    try:
        selected = _select_locations(root, NGINX_SSE_PATH)
    except _NginxUndecidable as exc:
        print(
            "nginx 生效配置判不了："
            + str(exc)
            + "，没法确认处理 "
            + NGINX_SSE_PATH
            + " 的 location 有没有关着缓冲",
            file=sys.stderr,
        )
        return 1
    if not selected:
        print(
            "nginx 生效配置里没有处理 " + NGINX_SSE_PATH + " 的 location",
            file=sys.stderr,
        )
        return 1

    problems: list[str] = []
    for block in selected:
        value = _effective_proxy_buffering(block)
        if value != "off":
            problems.append(
                block.header + " 的 proxy_buffering 是 " + value + "（期望 off）"
            )
    if problems:
        print(
            "SSE 会被攒成一个整包再发："
            + "；".join(problems)
            + "。请检查 docker/nginx.conf 里 location /api/ 的 proxy_buffering off",
            file=sys.stderr,
        )
        return 1
    print("nginx: " + NGINX_SSE_PATH + " 的代理缓冲已关闭")
    return 0


# ---------------------------------------------------------------------------
# 入口
# ---------------------------------------------------------------------------


def main(argv: list[str]) -> int:
    _enable_utf8_stdio()
    if len(argv) < 2:
        print(__doc__ or "", file=sys.stderr)
        return 2
    command = argv[1]
    if command == "health":
        return check_health(sys.stdin.read())
    if command == "services":
        return check_services(sys.stdin.read())
    if command == "steps":
        if len(argv) < 3:
            print("用法：smoke_checks.py steps <base-url>", file=sys.stderr)
            return 2
        return check_steps(argv[2])
    if command == "sse":
        rest = argv[2:]
        nginx_conf_path = ""
        positional: list[str] = []
        index = 0
        while index < len(rest):
            argument = rest[index]
            if argument == "--nginx-conf":
                index += 1
                if index >= len(rest):
                    print(
                        "用法：smoke_checks.py sse <base-url> [query] [--nginx-conf <文件>]",
                        file=sys.stderr,
                    )
                    return 2
                nginx_conf_path = rest[index]
            elif argument.startswith("--nginx-conf="):
                nginx_conf_path = argument.split("=", 1)[1]
            else:
                positional.append(argument)
            index += 1
        if not positional:
            print(
                "用法：smoke_checks.py sse <base-url> [query] [--nginx-conf <文件>]",
                file=sys.stderr,
            )
            return 2
        nginx_conf = None
        if nginx_conf_path:
            try:
                with open(
                    nginx_conf_path, "r", encoding="utf-8", errors="replace"
                ) as handle:
                    nginx_conf = handle.read()
            except OSError as exc:
                print("读不了 nginx 的生效配置：" + str(exc), file=sys.stderr)
                return 2
        query = positional[1] if len(positional) > 1 else DEFAULT_QUERY
        return check_sse(positional[0], query=query, nginx_conf=nginx_conf)
    if command == "nginx":
        return check_nginx(sys.stdin.read())
    print("未知子命令：" + command, file=sys.stderr)
    return 2


if __name__ == "__main__":
    sys.exit(main(sys.argv))
