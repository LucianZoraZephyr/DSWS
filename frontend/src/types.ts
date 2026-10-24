/**
 * 契约类型（手写版）
 *
 * 与 contracts/sse.schema.json 对应。
 * S0 尚未自动对账：schema 由后端契约测试校验，TS 类型由 tsc 校验，
 * 两者的一致性靠下面的 Vitest 用例 + 后端测试共同守住。
 * S7 会改为从 schema 自动生成类型，并加 diff 检查。
 */

export type ProgressStatus = "running" | "success" | "error" | "skipped";
export type StepVisualStatus = ProgressStatus | "pending";

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

export interface ErrorEvent {
  type: "error";
  code: string;
  message: string;
  step: string | null;
}

export type AgentEvent = ProgressEvent | ResultEvent | ErrorEvent;

export interface StepState {
  status: ProgressStatus;
  duration_ms: number | null;
}
