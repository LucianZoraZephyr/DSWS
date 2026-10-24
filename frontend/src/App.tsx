import { useEffect, useMemo, useState } from "react";

import Composer from "./components/Composer";
import ResultTable from "./components/ResultTable";
import StepRail from "./components/StepRail";
import { fetchSteps, streamQuery, ApiError } from "./lib/api";
import type {
  AgentEvent,
  ErrorEvent,
  ResultData,
  StepDef,
  StepState,
} from "./types";

function markRunningAsError(
  states: Record<string, StepState>,
  step?: string | null,
): Record<string, StepState> {
  const next = { ...states };
  for (const [id, state] of Object.entries(next)) {
    if (state.status === "running") {
      next[id] = { status: "error", duration_ms: null };
    }
  }
  if (step) {
    next[step] = { status: "error", duration_ms: null };
  }
  return next;
}

export default function App() {
  const [steps, setSteps] = useState<StepDef[]>([]);
  const [states, setStates] = useState<Record<string, StepState>>({});
  const [result, setResult] = useState<ResultData | null>(null);
  const [error, setError] = useState<ErrorEvent | null>(null);
  const [running, setRunning] = useState(false);
  const [lastQuery, setLastQuery] = useState("");
  const [loadError, setLoadError] = useState<string | null>(null);

  useEffect(() => {
    fetchSteps()
      .then((payload) => setSteps(payload.steps))
      .catch((reason: unknown) =>
        setLoadError(reason instanceof Error ? reason.message : String(reason)),
      );
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

  async function ask(query: string) {
    setRunning(true);
    setStates({});
    setResult(null);
    setError(null);
    setLastQuery(query);

    let terminal = false;

    try {
      await streamQuery(query, (event: AgentEvent) => {
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
          return;
        }

        terminal = true;
        setError(event);
        setStates((previous) => markRunningAsError(previous, event.step));
      });

      // 契约要求：流必须以 result 或 error 收尾。
      // 静默结束 = 失败，不能当成成功（旧系统的缺陷之一）。
      if (!terminal) {
        setError({
          type: "error",
          code: "STREAM_INCOMPLETE",
          message: "连接已结束，但没有收到结果或错误事件。",
          step: null,
        });
        setStates((previous) => markRunningAsError(previous));
      }
    } catch (reason: unknown) {
      setError({
        type: "error",
        code: reason instanceof ApiError ? reason.code : "NETWORK_ERROR",
        message: reason instanceof Error ? reason.message : String(reason),
        step: null,
      });
      setStates((previous) => markRunningAsError(previous));
    } finally {
      setRunning(false);
    }
  }

  return (
    <div className="app">
      <header className="masthead">
        <div>
          <p className="eyebrow">从零重建 · 教学版</p>
          <h1>电商问数智能体</h1>
          <p className="subtitle">
            契约先行：步骤目录唯一源 · SSE 事件过 Schema · 条件步骤显式跳过
          </p>
        </div>
        <div className="badges">
          <span className="badge badge-stage">S0 骨架</span>
          <span className="badge">{steps.length} 步</span>
          <span className="badge">{conditionalCount} 条件步</span>
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
              {running && runningStep
                ? `正在执行：${runningStep.label}`
                : "单轮问答"}
            </span>
          </div>

          <div className="chat-body">
            {!lastQuery && (
              <div className="empty-state">
                <p className="empty-title">这是一个骨架，不是成品。</p>
                <p>
                  S0 刻意不接 LLM / 数据库 / 检索，只验证一件事：
                  <strong>前后端对「一次问数」的契约理解完全一致。</strong>
                </p>
                <ul>
                  <li>步骤列表由后端下发，前端零硬编码</li>
                  <li>每条事件都通过 contracts/sse.schema.json</li>
                  <li>correct_sql 是条件步，显式显示「未触发」</li>
                </ul>
              </div>
            )}

            {lastQuery && (
              <div className="query-bubble">{lastQuery}</div>
            )}

            {running && (
              <div className="running-hint">
                <span className="spinner dark" />
                正在跑假流……
              </div>
            )}

            {result && <ResultTable data={result} />}

            {error && (
              <div className="error-card">
                <div className="error-head">
                  <strong>{error.message}</strong>
                  <code>{error.code}</code>
                </div>
                {error.step && (
                  <p className="hint">失败步骤：{error.step}</p>
                )}
              </div>
            )}
          </div>

          <Composer disabled={running || steps.length === 0} onSubmit={ask} />
        </section>
      </main>

      <footer className="footer">
        <span>rebuild/ · S0</span>
        <span>
          下一步 S1：把「表结构」变成可校验的契约（粒度 / 口径 / 值域）
        </span>
      </footer>
    </div>
  );
}
