/**
 * 由 `python -m app.scripts.contracts_gen` 从 `contracts/sse.schema.json` 生成。
 *
 * **不要手改这个文件。**
 *
 * 契约改了，就重跑生成器；`make stage7` 会用 `--check` 盯着新鲜度。
 * 手写一份 TS 类型等于把契约抄第二遍——抄本迟早和原文对不上，
 * 而前端类型对不上 = 界面按错误的契约渲染。
 *
 * 生成的类型：
 *   - 事件联合（判别联合，`type` 是判别字段）
 *   - 从联合**派生**出来的别名（ProgressStatus / ErrorClass / ...）
 *
 * 运行时校验不在这里：它由 `sse.schema.json` 的副本驱动
 * （`src/lib/json-schema.ts` 是一台通用 JSON Schema 校验器，
 * 契约本身仍然是唯一事实源）。
 */

export interface ProgressEvent {
  type: "progress";
  /** 必须是 contracts/steps.yaml 里声明过的 step id */
  step: string;
  status: "running" | "success" | "error" | "skipped";
  /** success / error 时必填数字；running / skipped 时必须为 null */
  duration_ms: number | null;
}
export interface ResultEvent {
  type: "result";
  data: ResultData;
}
export interface ErrorEvent {
  type: "error";
  /** 机器可判定的错误码，取值见 contracts/errors.yaml */
  code: string;
  /** 失败类别：failure 真失败 / empty 合法但无数据 / cancelled 用户取消 */
  class: "failure" | "empty" | "cancelled";
  message: string;
  step: string | null;
  /** 可复制的细节：原因码、原始 SQL、校正后的 SQL、重试次数。 */
  detail?: ErrorDetail;
}

export interface ResultDataColumn {
  name: string;
  type: "string" | "number" | "boolean" | "null" | "date";
}
export interface ResultData {
  columns: ResultDataColumn[];
  rows: ResultDataRow[];
}
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

export type ResultDataRowValue = string | number | boolean | null;
export type ResultDataRow = ResultDataRowValue[];

export type AgentEvent = ProgressEvent | ResultEvent | ErrorEvent;

/** 契约里声明的全部事件类型。收到不在这里面的 type 是「后端加了新事件」，
 *  不是「这条消息坏了」——前者忽略，后者报错。 */
export const EVENT_TYPES = ["progress", "result", "error"] as const;

export type EventType = (typeof EVENT_TYPES)[number];

/* ---- 从联合派生的别名：一个事实源，不抄第二遍 ---- */
export type ProgressStatus = ProgressEvent["status"];
export type ErrorClass = ErrorEvent["class"];
export type ColumnType = ResultDataColumn["type"];
export type CellValue = ResultDataRowValue;

/* 契约里出现的枚举取值（只作阅读参考，类型请用上面的别名）： */
// progress.status: "running", "success", "error", "skipped"
// column.type:    "string", "number", "boolean", "null", "date"
// error.class:    "failure", "empty", "cancelled"
