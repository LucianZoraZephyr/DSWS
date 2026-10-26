/**
 * 契约类型（手写版）
 *
 * 与 contracts/sse.schema.json 对应。
 * S0 尚未自动对账：schema 由后端契约测试校验，TS 类型由 tsc 校验，
 * 两者的一致性靠下面的 Vitest 用例 + 后端测试共同守住。
 * S7 会改为从 schema 自动生成类型，并加 diff 检查。
 *
 * S6 起多了一组东西：**失败类别**。
 *
 *   failure    真失败：红框 + 失败步骤红叉
 *   empty      合法但没有数据：空态卡，**不标红**
 *   cancelled  用户取消：取消态，**既不是 done 也不是失败**
 *
 * 这三种在旧系统里长得一模一样（都是「查询完成」）。
 */

export type ProgressStatus = "running" | "success" | "error" | "skipped";
export type StepVisualStatus = ProgressStatus | "pending" | "cancelled";

export interface StepDef {
  id: string;
  label: string;
  conditional?: boolean;
}

export interface StepsResponse {
  version: number;
  steps: StepDef[];
}

export interface ProgressEvent {
  type: "progress";
  step: string;
  status: ProgressStatus;
  duration_ms: number | null;
}

export type ColumnType = "string" | "number" | "boolean" | "null" | "date";

export interface ResultColumn {
  name: string;
  type: ColumnType;
}

export type CellValue = string | number | boolean | null;

export interface ResultData {
  columns: ResultColumn[];
  rows: CellValue[][];
}

export interface ResultEvent {
  type: "result";
  data: ResultData;
}

/** 失败类别。取值来自 contracts/errors.yaml 的 `classes`。 */
export type ErrorClass = "failure" | "empty" | "cancelled";

/** 错误细节。字段名与 contracts/sse.schema.json 的 detail 一一对应。 */
export interface ErrorDetail {
  reason_code?: string | null;
  reason?: string | null;
  sql?: string | null;
  original_sql?: string | null;
  corrected_sql?: string | null;
  attempts?: number | null;
  rows?: number | null;
  request_id?: string | null;
}

export interface ErrorEvent {
  type: "error";
  code: string;
  class: ErrorClass;
  message: string;
  step: string | null;
  detail?: ErrorDetail;
}

export type AgentEvent = ProgressEvent | ResultEvent | ErrorEvent;

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
  class: ErrorClass;
  retryable: boolean;
  message: string;
}

export interface ErrorTableResponse {
  version: number;
  classes: ErrorClass[];
  codes: ErrorCodeSpec[];
}
