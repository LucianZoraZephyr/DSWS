"""
S0 · 走通骨架

这一阶段刻意不接 LLM、不接数据库、不接检索：
只用一份「步骤目录」+ 一条「假 SSE 流」，把 前端 → 契约 → 后端 的链路跑通。

为什么先做这个？
    审计发现的 30+ 条缺陷收敛到三个根因，第一个就是「契约只写在文档里」。
    所以第一个可运行的系统，必须先把契约变成代码，而不是先装数据库。
"""

from __future__ import annotations

import asyncio
import json
import time
from pathlib import Path
from typing import AsyncIterator

import yaml
from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, Field

# ---------------------------------------------------------------------------
# 契约加载：steps.yaml 是前后端唯一事实源
# ---------------------------------------------------------------------------

REBUILD_ROOT = Path(__file__).resolve().parents[2]
CONTRACTS_DIR = REBUILD_ROOT / "contracts"

STEPS: list[dict] = yaml.safe_load(
    (CONTRACTS_DIR / "steps.yaml").read_text(encoding="utf-8")
)["steps"]

_STEP_IDS = [step["id"] for step in STEPS]
if len(_STEP_IDS) != len(set(_STEP_IDS)):
    raise RuntimeError("contracts/steps.yaml 存在重复的 step id")

# ---------------------------------------------------------------------------
# 应用
# ---------------------------------------------------------------------------

app = FastAPI(
    title="Shopkeeper Agent · Rebuild",
    version="0.1.0",
    description="S0 骨架：契约驱动 + 假 SSE 流",
)

# 前端开发服务器（rebuild 用 5273，避开旧栈的 5173）
app.add_middleware(
    CORSMiddleware,
    allow_origins=[
        "http://127.0.0.1:5273",
        "http://localhost:5273",
    ],
    allow_methods=["*"],
    allow_headers=["*"],
)


class QueryRequest(BaseModel):
    """`POST /api/query` 请求体：单轮自然语言问题。"""

    query: str = Field(min_length=1, max_length=500)


# ---------------------------------------------------------------------------
# 路由
# ---------------------------------------------------------------------------


@app.get("/api/health")
def health() -> dict:
    return {"status": "ok", "stage": "S0"}


@app.get("/api/steps")
def get_steps() -> dict:
    """
    下发步骤目录。

    前端不再硬编码节点：新增/删除节点只改 steps.yaml。
    """
    return {"version": 1, "steps": STEPS}


@app.post("/api/query")
async def query(request: QueryRequest) -> StreamingResponse:
    if not request.query.strip():
        raise HTTPException(status_code=422, detail="query 不能为空")

    return StreamingResponse(
        _fake_stream(request.query),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "Connection": "keep-alive",
            "X-Accel-Buffering": "no",
        },
    )


# ---------------------------------------------------------------------------
# 假流：严格按契约发事件
# ---------------------------------------------------------------------------


def _sse(event: dict) -> str:
    """把事件编码成 SSE 帧。前端按空行切分。"""
    return f"data: {json.dumps(event, ensure_ascii=False)}\n\n"


async def _fake_stream(query: str) -> AsyncIterator[str]:
    """
    S0 的假流：

      - 非条件步骤：running → success（带真实耗时）
      - 条件步骤：  skipped（显式「未触发」，不是永远 pending）
      - 最后：      result

    这条流的意义不是「像真的」，而是「把契约跑通」。
    """
    for step in STEPS:
        step_id = step["id"]

        if step.get("conditional"):
            yield _sse(
                {
                    "type": "progress",
                    "step": step_id,
                    "status": "skipped",
                    "duration_ms": None,
                }
            )
            continue

        yield _sse(
            {
                "type": "progress",
                "step": step_id,
                "status": "running",
                "duration_ms": None,
            }
        )

        started = time.perf_counter()
        await asyncio.sleep(0.12)  # 让前端看得见「正在执行」
        elapsed_ms = round((time.perf_counter() - started) * 1000, 1)

        yield _sse(
            {
                "type": "progress",
                "step": step_id,
                "status": "success",
                "duration_ms": elapsed_ms,
            }
        )

    yield _sse(
        {
            "type": "result",
            "data": {
                "columns": [
                    {"name": "地区", "type": "string"},
                    {"name": "销售额", "type": "number"},
                    {"name": "订单数", "type": "number"},
                ],
                "rows": [
                    ["华北", 128400.0, 32],
                    ["华南", 96500.0, 24],
                    ["华东", 87200.0, 21],
                    ["西南", 41300.0, 11],
                ],
            },
        }
    )
