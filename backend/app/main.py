"""
S6 · 走通骨架 → 失败通道

这一阶段把 S0 的「假 SSE 流」换成了**真的流水线**：

    上下文 8 阶段 → generate_sql → validate_sql → [correct_sql] → run_sql
                                                            → result / error

为什么要换：S0 的假流永远成功。审计里最刺眼的三类静默失败——伪 SQL 被
当成一行数据、召回为空也算 success、错误被吞掉——在假流里一个都测不出来。
S6 的目标正是「让每一种失败都有名字、有 UI、有测试」，所以流必须能失败，
而且失败必须以**契约里的错误码**收尾。

三条不变式：

    1. 流**一定**以 `result` 或 `error` 收尾（契约要求，也是 S0 就有的断言）。
    2. `error.code` 只能取 `contracts/errors.yaml` 里的码。
    3. 取消是 `cancelled`，不是 `done`（旧系统 `App.tsx:149` 的缺陷）。

应用是**工厂**：`create_app(runner_factory)`。生产用默认工厂（真模型 +
只读账号），验收用场景工厂（假模型 + 假库，但校验、策略、事件全是真的）。
"""

from __future__ import annotations

import asyncio
import json
import threading
import uuid
from pathlib import Path
from typing import Any, AsyncIterator, Callable

import yaml
from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, Field

from app.agent.cancel import CancelRegistry, CancelToken
from app.agent.correct import LlmCorrector, SqlCorrector
from app.agent.errors import build_error, load_error_table
from app.agent.orchestrator import AgentRunner
from app.generate.provider import DeterministicGenerator, HttpLlmGenerator, SqlGenerator

# ---------------------------------------------------------------------------
# 契约加载：steps.yaml 是前后端唯一事实源
# ---------------------------------------------------------------------------

REBUILD_ROOT = Path(__file__).resolve().parents[2]
CONTRACTS_DIR = REBUILD_ROOT / "contracts"

STEPS_DOC: dict = yaml.safe_load(
    (CONTRACTS_DIR / "steps.yaml").read_text(encoding="utf-8")
)
STEPS: list[dict] = STEPS_DOC["steps"]
STEPS_VERSION: int = STEPS_DOC["version"]

_STEP_IDS = [step["id"] for step in STEPS]
if len(_STEP_IDS) != len(set(_STEP_IDS)):
    raise RuntimeError("contracts/steps.yaml 存在重复的 step id")

#: 运行中的问数：`request_id` → 取消开关。
CANCEL_REGISTRY = CancelRegistry()


class QueryRequest(BaseModel):
    """`POST /api/query` 请求体：单轮自然语言问题。"""

    query: str = Field(min_length=1, max_length=500)


class CancelRequest(BaseModel):
    """`POST /api/cancel` 请求体：取消一次问数。"""

    request_id: str = Field(min_length=1, max_length=64)


class StepDef(BaseModel):
    """步骤目录中的一项。"""

    id: str
    label: str
    conditional: bool = False


class StepsResponse(BaseModel):
    """`GET /api/steps` 的响应。"""

    version: int
    steps: list[StepDef]


class EventStreamResponse(StreamingResponse):
    """让 OpenAPI 正确声明 `text/event-stream`，而不是默认的 application/json。"""

    media_type = "text/event-stream"


# ---------------------------------------------------------------------------
# 依赖装配
# ---------------------------------------------------------------------------


def _build_generator() -> SqlGenerator:
    """生成器：默认确定性；配了 LLM 环境变量就用真模型。"""
    try:
        return HttpLlmGenerator.from_env()
    except ValueError:
        return DeterministicGenerator()


def _build_corrector() -> SqlCorrector | None:
    """校正器：只有配了 LLM 环境变量才有。

    没有模型时**不假装能校正**——没有校正器时 `correct_sql` 这一步会显式
    标成「未触发」，而不是偷偷改一改 SQL。
    """
    try:
        return LlmCorrector(HttpLlmGenerator.from_env())
    except ValueError:
        return None


def default_runner_factory(
    query: str,
    cancel: CancelToken,
    request_id: str,
) -> AgentRunner:
    """生产用的编排器：真模型（配了环境变量的话）+ 只读账号。"""
    return AgentRunner(
        query=query,
        generator=_build_generator(),
        corrector=_build_corrector(),
        cancel=cancel,
        request_id=request_id,
    )


RunnerFactory = Callable[[str, CancelToken, str], AgentRunner]


# ---------------------------------------------------------------------------
# 应用工厂
# ---------------------------------------------------------------------------


def create_app(runner_factory: RunnerFactory) -> FastAPI:
    """按给定的编排器工厂造一个应用。

    生产与验收共用同一套 HTTP / SSE 管道——验收时只换掉**编排器的依赖**
    （模型、数据库），管道本身一行不改。
    """
    application = FastAPI(
        title="Shopkeeper Agent · Rebuild",
        version="0.3.0",
        description="S7：契约驱动的问数流水线 + 生成式前端契约 + 有名字的失败通道",
    )

    # 前端开发服务器（rebuild 用 5273，避开旧栈的 5173）
    application.add_middleware(
        CORSMiddleware,
        allow_origins=[
            "http://127.0.0.1:5273",
            "http://localhost:5273",
        ],
        allow_methods=["GET", "POST"],
        allow_headers=["Content-Type"],
    )

    @application.get("/api/health")
    def health() -> dict:
        return {"status": "ok", "stage": "S7"}

    @application.get("/api/steps", response_model=StepsResponse)
    def get_steps() -> StepsResponse:
        """
        下发步骤目录。

        前端不再硬编码节点：新增/删除节点只改 steps.yaml。
        """
        return StepsResponse(version=STEPS_VERSION, steps=STEPS)

    @application.get("/api/errors")
    def get_errors() -> dict:
        """
        下发错误码表。

        前端拿它把 `SQL_REJECTED` 显示成「SQL 被安全策略拒绝」，而不是把码
        直接甩给用户。契约只有一份：`contracts/errors.yaml`。
        """
        return load_error_table().to_dict()

    @application.post(
        "/api/query",
        response_class=EventStreamResponse,
        responses={
            200: {
                "content": {
                    "text/event-stream": {
                        "schema": {"type": "string"},
                        "example": 'data: {"type":"progress","step":"extract_keywords","status":"running","duration_ms":null}\n\n',
                    }
                }
            }
        },
    )
    async def query(request: QueryRequest) -> EventStreamResponse:
        if not request.query.strip():
            raise HTTPException(status_code=422, detail="query 不能为空")

        request_id = uuid.uuid4().hex
        token = CANCEL_REGISTRY.create(request_id)
        runner = runner_factory(request.query.strip(), token, request_id)

        return EventStreamResponse(
            _stream(runner, request_id),
            headers={
                "Cache-Control": "no-cache",
                "Connection": "keep-alive",
                "X-Accel-Buffering": "no",
                # 前端要靠它来取消这次问数
                "X-Request-Id": request_id,
            },
        )

    @application.post("/api/cancel")
    def cancel(request: CancelRequest) -> dict:
        """
        取消一次问数。

        旧系统把取消当成「完成」（`App.tsx:149`：`status: isAbort ? "done"`）。
        这里取消是一个**独立状态**：服务端会真的停下这次问数，并发一条
        `CANCELLED` 错误事件；找不到这次问数时返回 `cancelled: false`
        （可能已经结束了）。
        """
        stopped = CANCEL_REGISTRY.cancel(request.request_id)
        return {"request_id": request.request_id, "cancelled": stopped}

    return application


app = create_app(default_runner_factory)


# ---------------------------------------------------------------------------
# SSE
# ---------------------------------------------------------------------------


def _sse(event: dict) -> str:
    """把事件编码成 SSE 帧。前端按空行切分。

    这里**故意不传 `default=str`**。
    """
    # 旧系统传了 `default=str`，于是 `Decimal` 静悄悄变成字符串 "10"，
    # 数字与字符串在同一行里混列（审计 B4）。现在值在进 JSON 之前就被
    # `app.agent.values` 归一化过了；万一还有漏网的类型，就让它**炸**
    # ——变成一条 INTERNAL_ERROR，好过悄悄改掉用户数据的类型。
    return f"data: {json.dumps(event, ensure_ascii=False)}\n\n"


async def _stream(runner: AgentRunner, request_id: str) -> AsyncIterator[str]:
    """
    把编排器的事件流搬到 SSE 上。

    编排器是**同步**生成器（里面要跑数据库），直接放进异步响应会卡住
    事件循环。所以它跑在一个工作线程里，事件通过队列交回给协程。
    """
    loop = asyncio.get_running_loop()
    queue: asyncio.Queue[Any] = asyncio.Queue()

    def worker() -> None:
        try:
            for event in runner.events():
                loop.call_soon_threadsafe(queue.put_nowait, event)
        except Exception as exc:  # noqa: BLE001 - 兜底：流必须收尾
            error = build_error(
                "INTERNAL_ERROR",
                f"问数过程中发生未预期的错误：{exc}",
                request_id=request_id,
            )
            loop.call_soon_threadsafe(queue.put_nowait, error.to_event())
        finally:
            loop.call_soon_threadsafe(queue.put_nowait, None)

    thread = threading.Thread(target=worker, name=f"ask-{request_id}", daemon=True)
    thread.start()

    try:
        while True:
            event = await queue.get()
            if event is None:
                break
            yield _sse(event)
    finally:
        # 客户端断开（前端 abort）时，先把取消开关拉下：工作线程会在下一个
        # 步骤边界停下来，而不是把 SQL 白跑完。
        CANCEL_REGISTRY.cancel(request_id)
        CANCEL_REGISTRY.release(request_id)
