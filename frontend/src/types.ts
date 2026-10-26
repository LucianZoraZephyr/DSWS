/**
 * 前端类型。
 *
 * **事件相关的一切都从契约生成**（`src/generated/sse.ts`）——这里只做
 * 转出，不再手写第二份。
 *
 * 旧系统手写了一份 `AgentEvent = ProgressEvent | ResultEvent | ErrorEvent`，
 * 字段和后端对得上——但那是**抄**出来的对得上。后端加一个字段，前端
 * 不会报错，只会静默地不显示它。抄本迟早和原文分家。
 *
 * S7 起：
 *   - 类型来自 `contracts/sse.schema.json`（生成物由 `--check` 盯着新鲜度）
 *   - 运行时校验也来自同一份契约（`src/lib/json-schema.ts` 是通用引擎）
 *   - 派生别名（ProgressStatus / ErrorClass / CellValue）从联合**推**出来，
 *     不是再抄一遍枚举
 *
 * 不在 SSE 契约里的东西（步骤目录、错误码表）仍然手写——它们各有各的
 * 契约文件（steps.yaml / errors.yaml），由后端通过 API 下发。
 */

export type {
  AgentEvent,
  CellValue,
  ColumnType,
  ErrorClass,
  ErrorDetail,
  ErrorEvent,
  EventType,
  ProgressEvent,
  ProgressStatus,
  ResultData,
  ResultDataColumn,
  ResultDataRow,
  ResultDataRowValue,
  ResultEvent,
} from "./generated/sse";

import type { ProgressStatus } from "./generated/sse";

/** 步骤目录里的一项（来自 GET /api/steps）。 */
export interface StepDef {
  id: string;
  label: string;
  conditional?: boolean;
}

export interface StepsResponse {
  version: number;
  steps: StepDef[];
}

/**
 * 步骤在界面上的样子。
 *
 * `pending` 是**还没走到**；`skipped` 是**没走也不需要走**（条件步骤）。
 * 旧系统只有前者，于是「校正SQL」永远是一块灰的圆——用户分不清
 * 「还没到」和「不需要」（审计 D4 / F9）。
 */
export type StepVisualStatus = ProgressStatus | "pending" | "cancelled";

export interface StepState {
  status: ProgressStatus | "cancelled";
  duration_ms: number | null;
}

/**
 * 一次问数的整体状态。
 *
 * `cancelled` 与 `done` 分开：旧系统把取消算成完成
 * （`App.tsx:149`：`status: isAbort ? "done"`），用户根本看不出
 * 一次被掐断的查询和一次成功的查询的区别。
 */
export type RunStatus =
  | "idle"
  | "running"
  | "done"
  | "error"
  | "empty"
  | "cancelled";

/** 错误码表里的一项（来自 GET /api/errors）。 */
export interface ErrorCodeSpec {
  code: string;
  label: string;
  class: "failure" | "empty" | "cancelled";
  retryable: boolean;
  message: string;
}

export interface ErrorTableResponse {
  version: number;
  classes: Array<"failure" | "empty" | "cancelled">;
  codes: ErrorCodeSpec[];
}
