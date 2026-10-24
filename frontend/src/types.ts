/**
 * 契约类型（手写版）
 *
 * 与 contracts/sse.schema.json 一一对应。
 * S7 会用 schema 自动生成这些类型；S0 先手写，并用后端契约测试保证不漂移。
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
