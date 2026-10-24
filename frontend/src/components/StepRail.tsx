import type { ReactNode } from "react";

import type { StepDef, StepVisualStatus, StepState } from "../types";

interface Props {
  steps: StepDef[];
  states: Record<string, StepState>;
}

const STATUS_TEXT: Record<StepVisualStatus, string> = {
  pending: "等待",
  running: "执行中",
  success: "完成",
  error: "失败",
  skipped: "未触发",
};

function formatDuration(durationMs: number | null | undefined): string {
  if (durationMs === null || durationMs === undefined) return "";
  if (durationMs < 1000) return `${Math.round(durationMs)} ms`;
  return `${(durationMs / 1000).toFixed(2)} s`;
}

/**
 * 竖向导轨。
 *
 * 旧版是 820×780 的绝对定位画布：窗口一小就裁切，步骤一多就要重算坐标。
 * 新版用普通文档流：天然响应式，新增步骤不需要改任何坐标。
 */
export default function StepRail({ steps, states }: Props) {
  return (
    <ol className="rail" aria-label="执行步骤">
      {steps.map((step, index) => {
        const state = states[step.id];
        const status: StepVisualStatus = state?.status ?? "pending";

        let marker: ReactNode;
        switch (status) {
          case "success":
            marker = "✓";
            break;
          case "error":
            marker = "×";
            break;
          case "running":
            marker = <span className="spinner" />;
            break;
          case "skipped":
            marker = "–";
            break;
          default:
            marker = index + 1;
        }

        return (
          <li key={step.id} className={`rail-item rail-${status}`}>
            <div className="rail-marker" aria-hidden="true">
              {marker}
            </div>

            <div className="rail-body">
              <span className="rail-label">{step.label}</span>
              <span className="rail-meta">
                <span className="rail-status">{STATUS_TEXT[status]}</span>
                {status === "success" && state?.duration_ms != null && (
                  <span className="rail-duration">
                    {formatDuration(state.duration_ms)}
                  </span>
                )}
              </span>
            </div>
          </li>
        );
      })}
    </ol>
  );
}
