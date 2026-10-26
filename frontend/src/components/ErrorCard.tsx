import { useState } from "react";

import type { ErrorClass, ErrorCodeSpec, ErrorEvent } from "../types";

interface Props {
  error: ErrorEvent;
  /** 错误码表里的一项（来自 GET /api/errors）。没有就用码本身当标题。 */
  spec?: ErrorCodeSpec;
  /** 失败步骤的中文名（来自 GET /api/steps）。 */
  stepLabel?: string | null;
}

const CLASS_TEXT: Record<ErrorClass, string> = {
  failure: "失败",
  empty: "无数据",
  cancelled: "已取消",
};

/**
 * 失败卡片。
 *
 * 三种长相（由 `class` 决定，**不是**由码决定）：
 *
 *   failure    红框 + 红叉 —— 真的失败了
 *   empty      虚线空态卡 —— 查询成功但没有数据，**不标红**
 *   cancelled  中性卡     —— 用户自己停的，不算失败
 *
 * 错误**可复制**：整段文本（码 / 步骤 / 原因码 / 消息 / 原始 SQL）在页面
 * 上看得见，也能一键复制走。旧系统只有一句「查询完成，共 1 行结果。」，
 * 用户拿不走任何东西。
 */
export default function ErrorCard({ error, spec, stepLabel }: Props) {
  const [copied, setCopied] = useState(false);

  const title = spec?.label ?? error.code;
  const detail = error.detail ?? {};
  const text = buildText(error, spec, stepLabel);

  async function copy() {
    try {
      await navigator.clipboard.writeText(text);
    } catch {
      // 剪贴板不可用（没有 HTTPS / 没有权限）时退回老办法
      legacyCopy(text);
    }
    setCopied(true);
    window.setTimeout(() => setCopied(false), 2000);
  }

  return (
    <div
      className={`error-card error-card--${error.class}`}
      data-error-code={error.code}
      data-error-class={error.class}
      role="alert"
    >
      <div className="error-head">
        <strong>{title}</strong>
        <code>{error.code}</code>
      </div>

      <p className="error-message">{error.message}</p>

      <p className="error-meta">
        <span className="error-chip">{CLASS_TEXT[error.class]}</span>
        {error.step && (
          <span>
            失败步骤：{stepLabel ?? error.step}
          </span>
        )}
        {detail.reason_code && (
          <span>
            原因码：<code className="error-reason">{detail.reason_code}</code>
          </span>
        )}
        {typeof detail.attempts === "number" && detail.attempts > 0 && (
          <span>已校正 {detail.attempts} 次</span>
        )}
      </p>

      {(detail.original_sql || detail.corrected_sql) && (
        <details className="error-detail">
          <summary>SQL 细节</summary>
          {detail.original_sql && (
            <pre>
              {"原始 SQL\n"}
              {detail.original_sql}
            </pre>
          )}
          {detail.corrected_sql && (
            <pre>
              {"校正后 SQL\n"}
              {detail.corrected_sql}
            </pre>
          )}
        </details>
      )}

      <div className="error-actions">
        <button type="button" className="ghost-button" onClick={copy}>
          {copied ? "已复制 ✓" : "复制错误"}
        </button>
      </div>
    </div>
  );
}

function buildText(
  error: ErrorEvent,
  spec?: ErrorCodeSpec,
  stepLabel?: string | null,
): string {
  const detail = error.detail ?? {};
  const lines = [
    `[${error.code}] ${spec?.label ?? ""}`.trim(),
    `类别：${error.class}`,
  ];
  if (error.step) lines.push(`失败步骤：${stepLabel ?? error.step}`);
  if (detail.reason_code) lines.push(`原因码：${detail.reason_code}`);
  lines.push(`消息：${error.message}`);
  if (detail.original_sql) lines.push(`原始 SQL：${detail.original_sql}`);
  if (detail.corrected_sql) lines.push(`校正后 SQL：${detail.corrected_sql}`);
  if (typeof detail.attempts === "number" && detail.attempts > 0) {
    lines.push(`校正次数：${detail.attempts}`);
  }
  if (detail.request_id) lines.push(`request_id：${detail.request_id}`);
  return lines.join("\n");
}

function legacyCopy(text: string): void {
  const area = document.createElement("textarea");
  area.value = text;
  area.setAttribute("readonly", "");
  area.style.position = "fixed";
  area.style.opacity = "0";
  document.body.appendChild(area);
  area.select();
  try {
    document.execCommand("copy");
  } catch {
    // 复制不了就算了：文本在页面上看得见
  }
  document.body.removeChild(area);
}
