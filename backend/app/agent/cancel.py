"""S6 · 取消。

旧系统把「取消」当成「完成」：

    > `fe:src/App.tsx:149` — `status: isAbort ? "done" : "error"`

用户点了停止，界面显示「已停止本次查询。」，但状态是 `done`——在旧系统
的眼里，一次被掐断的查询和一次成功返回 20 行的查询**长得一样**。步骤条
也停在了半路，既不是成功也不是失败。

新系统里有三件事：

    1. 取消是**一个状态**（`cancelled`），不是 `done`，也不是 `error`；
    2. 取消之后**步骤条停止推进**：正在跑的那一步标成「已取消」，
       后面的步骤不再开始；
    3. 取消也是一个**有名字的通道码**（`CANCELLED`），服务端会真的停下
       这次问数，而不是继续把 SQL 跑完。
"""

from __future__ import annotations

import threading


class CancelToken:
    """一次问数的取消开关。线程安全（FastAPI 可能在不同线程里处理请求）。"""

    def __init__(self, request_id: str) -> None:
        self.request_id = request_id
        self._event = threading.Event()

    @property
    def cancelled(self) -> bool:
        return self._event.is_set()

    def cancel(self) -> None:
        self._event.set()

    def wait(self, timeout: float | None = None) -> bool:
        return self._event.wait(timeout)


class CancelRegistry:
    """`request_id` → 取消开关。

    生命周期跟着 SSE 流走：流开始时登记，结束时注销。请求结束之后再来
    取消，就返回 False（「这次问数已经结束了」），而不是报错。
    """

    def __init__(self) -> None:
        self._tokens: dict[str, CancelToken] = {}
        self._lock = threading.Lock()

    def create(self, request_id: str) -> CancelToken:
        token = CancelToken(request_id)
        with self._lock:
            self._tokens[request_id] = token
        return token

    def get(self, request_id: str) -> CancelToken | None:
        with self._lock:
            return self._tokens.get(request_id)

    def cancel(self, request_id: str) -> bool:
        """取消一次问数。返回 False 表示「找不到这次问数」（可能已经结束）。"""
        with self._lock:
            token = self._tokens.get(request_id)
        if token is None:
            return False
        token.cancel()
        return True

    def release(self, request_id: str) -> None:
        with self._lock:
            self._tokens.pop(request_id, None)
