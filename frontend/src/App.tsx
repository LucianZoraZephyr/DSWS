import { useEffect, useMemo, useRef, useState } from "react";

import Composer from "./components/Composer";
import ErrorCard from "./components/ErrorCard";
import ResultTable from "./components/ResultTable";
import StepRail from "./components/StepRail";
import {
  fetchErrorCodes,
  fetchSteps,
  streamQuery,
  cancelQuery,
  ApiError,
} from "./lib/api";
import type {
  AgentEvent,
  ErrorCodeSpec,
  ErrorEvent,
  ResultData,
  RunStatus,
  StepDef,
  StepState,
} from "./types";

/**
 * 把「正在跑」的步骤标成失败或取消。
 *
 * 旧系统只有一种：`markRunningAsError` —— 不管出了什么事，统统标成红色
 * 的「失败」。取消不是失败，空结果也不是失败，所以这里分开处理。
 */
function markRunning(
  states: Record<string, StepState>,
  status: "error" | "cancelled",
  step?: string | null,
): Record<string, StepState> {
  const next = { ...states };
  for (const [id, state] of Object.entries(next)) {
    if (state.status === "running") {
      next[id] = { status, duration_ms: null };
    }
  }
  if (step) {
    next[step] = { status, duration_ms: null };
  }
  return next;
}

const RUN_STATUS_TEXT: Record<RunStatus, string> = {
  idle: "待命",
  running: "执行中",
  done: "已完成",
  error: "失败",
  empty: "无数据",
  cancelled: "已取消",
};

export default function App() {
  const [steps, setSteps] = useState<StepDef[]>([]);
  const [states, setStates] = useState<Record<string, StepState>>({});
  const [result, setResult] = useState<ResultData | null>(null);
  const [error, setError] = useState<ErrorEvent | null>(null);
  const [status, setStatus] = useState<RunStatus>("idle");
  const [lastQuery, setLastQuery] = useState("");
  const [loadError, setLoadError] = useState<string | null>(null);
  const [errorSpecs, setErrorSpecs] = useState<
    Record<string, ErrorCodeSpec>
  >({});

  const controllerRef = useRef<AbortController | null>(null);
  const requestIdRef = useRef<string | null>(null);

  useEffect(() => {
    fetchSteps()
      .then((payload) => setSteps(payload.steps))
      .catch((reason: unknown) =>
        setLoadError(reason instanceof Error ? reason.message : String(reason)),
      );
  }, []);

  useEffect(() => {
    // 错误码表是契约的一部分：前端不硬编码码的含义。
    fetchErrorCodes()
      .then((payload) => {
        const table: Record<string, ErrorCodeSpec> = {};
        for (const spec of payload.codes) table[spec.code] = spec;
        setErrorSpecs(table);
      })
      .catch(() => {
        // 拿不到就退回用码本身当标题——不能因为它把整个界面卡住。
      });
  }, []);

  const conditionalCount = useMemo(
    () => steps.filter((step) => step.conditional).length,
    [steps],
  );

  const runningStep = useMemo(() => {
    const entry = Object.entries(states).find(
      ([, state]) => state.status === "running",
    );
    if (!entry) return null;
    return steps.find((step) => step.id === entry[0]) ?? null;
  }, [states, steps]);

  const stepLabel = useMemo(() => {
    const table: Record<string, string> = {};
    for (const step of steps) table[step.id] = step.label;
    return table;
  }, [steps]);

  async function ask(query: string) {
    const controller = new AbortController();
    controllerRef.current = controller;
    requestIdRef.current = null;

    setStatus("running");
    setStates({});
    setResult(null);
    setError(null);
    setLastQuery(query);

    let terminal = false;

    try {
      await streamQuery(
        query,
        (event: AgentEvent) => {
          if (terminal) return;

          if (event.type === "progress") {
            if (!steps.some((step) => step.id === event.step)) {
              throw new ApiError("UNKNOWN_STEP", `未知步骤：${event.step}`);
            }
            setStates((previous) => ({
              ...previous,
              [event.step]: {
                status: event.status,
                duration_ms: event.duration_ms,
              },
            }));
            return;
          }

          if (event.type === "result") {
            terminal = true;
            setResult(event.data);
            setStatus("done");
            return;
          }

          // error 事件：三种类别三种处理。
          terminal = true;
          setError(event);
          if (event.class === "empty") {
            // 合法但没有数据：不算失败，步骤也不标红。
            setStatus("empty");
            return;
          }
          if (event.class === "cancelled") {
            setStatus("cancelled");
            setStates((previous) => markRunning(previous, "cancelled"));
            return;
          }
          setStatus("error");
          setStates((previous) => markRunning(previous, "error", event.step));
        },
        {
          signal: controller.signal,
          onRequestId: (requestId) => {
            requestIdRef.current = requestId;
          },
        },
      );

      // 契约要求：流必须以 result 或 error 收尾。
      // 静默结束 = 失败，不能当成成功（旧系统的缺陷之一）。
      if (!terminal) {
        setError({
          type: "error",
          code: "STREAM_INCOMPLETE",
          class: "failure",
          message: "连接已结束，但没有收到结果或错误事件。",
          step: null,
        });
        setStatus("error");
        setStates((previous) => markRunning(previous, "error"));
      }
    } catch (reason: unknown) {
      if (isAbort(reason)) {
        // 用户点了停止：状态是「已取消」，**不是** done。
        setStatus("cancelled");
        setStates((previous) => markRunning(previous, "cancelled"));
        return;
      }
      setError({
        type: "error",
        code: reason instanceof ApiError ? reason.code : "NETWORK_ERROR",
        class: "failure",
        message: reason instanceof Error ? reason.message : String(reason),
        step: null,
      });
      setStatus("error");
      setStates((previous) => markRunning(previous, "error"));
    } finally {
      controllerRef.current = null;
    }
  }

  /**
   * 停止一次问数。
   *
   * 两件事同时做：告诉服务端别跑了（`/api/cancel`），并把本地这条流
   * 掐断（AbortController）。界面状态是「已取消」——旧系统这里写的是
   * `status: isAbort ? "done" : "error"`，取消被算成了完成。
   */
  function stop() {
    const requestId = requestIdRef.current;
    if (requestId) {
      void cancelQuery(requestId).catch(() => {
        // 服务端已经结束就算了：本地状态照样是「已取消」。
      });
    }
    controllerRef.current?.abort();
    setStatus("cancelled");
    setStates((previous) => markRunning(previous, "cancelled"));
  }

  return (
    <div className="app">
      <header className="masthead">
        <div>
          <p className="eyebrow">从零重建 · 教学版</p>
          <h1>电商问数智能体</h1>
          <p className="subtitle">
            契约先行：步骤目录唯一源 · SSE 事件过 Schema · 每一种失败都有名字
          </p>
        </div>
        <div className="badges">
          <span className="badge badge-stage">S6 失败通道</span>
          <span className="badge">{steps.length} 步</span>
          <span className="badge">{conditionalCount} 条件步</span>
          <span className="badge" data-testid="run-status">
            {RUN_STATUS_TEXT[status]}
          </span>
        </div>
      </header>

      {loadError && (
        <div className="error-card">
          <strong>步骤目录加载失败</strong>
          <p>{loadError}</p>
          <p className="hint">请确认后端已在 8100 端口启动。</p>
        </div>
      )}

      <main className="layout">
        <aside className="panel rail-panel">
          <div className="panel-head">
            <h2>执行链路</h2>
            <span className="panel-note">来自 /api/steps</span>
          </div>
          <StepRail steps={steps} states={states} />
        </aside>

        <section className="panel chat-panel">
          <div className="panel-head">
            <h2>问数</h2>
            <span className="panel-note">
              {status === "running" && runningStep
                ? `正在执行：${runningStep.label}`
                : RUN_STATUS_TEXT[status]}
            </span>
          </div>

          <div className="chat-body">
            {!lastQuery && (
              <div className="empty-state">
                <p className="empty-title">失败也要有名字。</p>
                <p>
                  这一版把「只读」与「只查得到」都搬进了代码：
                  <strong>每一次失败都有错误码、有界面、有测试。</strong>
                </p>
                <ul>
                  <li>步骤列表由后端下发，前端零硬编码</li>
                  <li>每条事件都通过 contracts/sse.schema.json</li>
                  <li>6 类失败各有各的样子，取消不是「完成」</li>
                </ul>
              </div>
            )}

            {lastQuery && (
              <div className="query-bubble">{lastQuery}</div>
            )}

            {status === "running" && (
              <div className="running-hint">
                <span className="spinner dark" />
                正在执行…
              </div>
            )}

            {result && <ResultTable data={result} />}

            {error && (
              <ErrorCard
                error={error}
                spec={errorSpecs[error.code]}
                stepLabel={error.step ? (stepLabel[error.step] ?? null) : null}
              />
            )}
          </div>

          <Composer
            disabled={status === "running" || steps.length === 0}
            running={status === "running"}
            onStop={stop}
            onSubmit={ask}
          />
        </section>
      </main>

      <footer className="footer">
        <span>rebuild/ · S6</span>
        <span>
          下一步 S7：前端从契约生成，不再从记忆硬编码
        </span>
      </footer>
    </div>
  );
}

function isAbort(reason: unknown): boolean {
  return (
    typeof reason === "object" &&
    reason !== null &&
    (reason as { name?: string }).name === "AbortError"
  );
}
