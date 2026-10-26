import type {
  AgentEvent,
  CellValue,
  ColumnType,
  ErrorClass,
  ErrorDetail,
  ErrorTableResponse,
  ProgressStatus,
  ResultColumn,
} from "../types";

/**
 * 开发期走 Vite 代理（/api → 后端），生产期同源部署。
 * 因此这里不需要写死域名。
 */

export class ApiError extends Error {
  readonly code: string;

  constructor(code: string, message: string) {
    super(message);
    this.name = "ApiError";
    this.code = code;
  }
}

const PROGRESS_STATUS: ReadonlySet<string> = new Set([
  "running",
  "success",
  "error",
  "skipped",
]);

const ERROR_CLASSES: ReadonlySet<string> = new Set([
  "failure",
  "empty",
  "cancelled",
]);

const COLUMN_TYPES: ReadonlySet<string> = new Set([
  "string",
  "number",
  "boolean",
  "null",
  "date",
]);

function isRecord(value: unknown): value is Record<string, unknown> {
  return typeof value === "object" && value !== null && !Array.isArray(value);
}

function fail(message: string): never {
  throw new ApiError("PROTOCOL_ERROR", message);
}

const DETAIL_KEYS = [
  "reason_code",
  "reason",
  "sql",
  "original_sql",
  "corrected_sql",
  "attempts",
  "rows",
  "request_id",
] as const;

/**
 * 错误细节。字段名由契约定死，多余的键直接报错——不能让后端悄悄塞
 * 一个前端看不懂的字段进来。
 */
function parseDetail(value: unknown): ErrorDetail | undefined {
  if (value === undefined || value === null) return undefined;
  if (!isRecord(value)) fail("error.detail 必须是对象");
  const detail: Record<string, unknown> = {};
  for (const key of Object.keys(value)) {
    if (!DETAIL_KEYS.includes(key as (typeof DETAIL_KEYS)[number])) {
      fail(`error.detail 出现未定义的键：${key}`);
    }
    detail[key] = value[key];
  }
  return detail as ErrorDetail;
}

/**
 * 运行时类型守卫。
 *
 * `as AgentEvent` 只是编译期断言；线上收到什么都有可能。
 * 这里逐字段校验，未知 type / status / step 一律抛错，绝不静默吞掉。
 */
export function parseAgentEvent(value: unknown): AgentEvent {
  if (!isRecord(value)) fail("事件必须是 JSON 对象");

  const type = value.type;
  if (typeof type !== "string") fail("事件缺少 type");

  switch (type) {
    case "progress": {
      const step = value.step;
      if (typeof step !== "string" || step.length === 0) {
        fail("progress.step 必须是非空字符串");
      }

      const status = value.status;
      if (typeof status !== "string" || !PROGRESS_STATUS.has(status)) {
        fail(`未知的 progress.status：${String(status)}`);
      }

      const duration = value.duration_ms;
      if (status === "success" || status === "error") {
        if (
          typeof duration !== "number" ||
          !Number.isFinite(duration) ||
          duration < 0
        ) {
          fail(`${status} 必须带非负的 duration_ms`);
        }
      } else if (duration !== null) {
        fail(`${status} 的 duration_ms 必须是 null`);
      }

      return {
        type: "progress",
        step,
        status: status as ProgressStatus,
        duration_ms: duration as number | null,
      };
    }

    case "result": {
      const data = value.data;
      if (!isRecord(data)) fail("result.data 必须是对象");

      const { columns, rows } = data;
      if (!Array.isArray(columns) || !Array.isArray(rows)) {
        fail("result.data 必须包含 columns 和 rows 数组");
      }

      const parsedColumns: ResultColumn[] = columns.map((column, index) => {
        if (!isRecord(column)) fail(`columns[${index}] 必须是对象`);
        const name = column.name;
        const columnType = column.type;
        if (
          typeof name !== "string" ||
          typeof columnType !== "string" ||
          !COLUMN_TYPES.has(columnType)
        ) {
          fail(`columns[${index}] 的 name/type 不合法`);
        }
        return { name, type: columnType as ColumnType };
      });

      const parsedRows: CellValue[][] = rows.map((row, rowIndex) => {
        if (!Array.isArray(row)) fail(`rows[${rowIndex}] 必须是数组`);
        return row.map((cell) => {
          if (
            cell === null ||
            typeof cell === "string" ||
            typeof cell === "number" ||
            typeof cell === "boolean"
          ) {
            return cell;
          }
          return fail(`rows[${rowIndex}] 含非法单元格类型`);
        });
      });

      return { type: "result", data: { columns: parsedColumns, rows: parsedRows } };
    }

    case "error": {
      const code = value.code;
      const message = value.message;
      if (typeof code !== "string" || !/^[A-Z][A-Z0-9_]*$/.test(code)) {
        fail("error.code 必须是 UPPER_SNAKE_CASE");
      }
      if (typeof message !== "string") fail("error.message 必须是字符串");
      if (!("step" in value) || (value.step !== null && typeof value.step !== "string")) {
        fail("error.step 必须是字符串或 null");
      }
      // class 是必填：前端靠它区分「红框」「空态」「取消」。
      // 没有它，失败就只剩一种长相——那正是旧系统的毛病。
      if (!("class" in value) || typeof value.class !== "string") {
        fail("error.class 必须是字符串");
      }
      if (!ERROR_CLASSES.has(value.class)) {
        fail(`未知的 error.class：${String(value.class)}`);
      }
      const detail = parseDetail(value.detail);
      return {
        type: "error",
        code,
        class: value.class as ErrorClass,
        message,
        step: value.step as string | null,
        ...(detail ? { detail } : {}),
      };
    }

    default:
      throw new ApiError("UNKNOWN_EVENT", `未知事件类型：${type}`);
  }
}

/**
 * SSE 解析器。
 *
 * 相比旧实现修了四点：
 *   1. 同时接受 \n\n 与 \r\n\r\n
 *   2. 多条 data: 行按规范用 \n 拼接
 *   3. 跨 chunk 的半帧留在 buffer
 *   4. 流结束时 flush 尾帧（不缺最后的空行也不丢）
 */
export class SseParser {
  private buffer = "";
  private readonly onEvent: (event: AgentEvent) => void;

  constructor(onEvent: (event: AgentEvent) => void) {
    this.onEvent = onEvent;
  }

  feed(chunk: string): void {
    this.buffer += chunk;
    const frames = this.buffer.split(/\r?\n\r?\n/);
    this.buffer = frames.pop() ?? "";
    for (const frame of frames) this.dispatch(frame);
  }

  flush(): void {
    const rest = this.buffer.trim();
    this.buffer = "";
    if (rest) this.dispatch(rest);
  }

  private dispatch(frame: string): void {
    const data = frame
      .split(/\r?\n/)
      .filter((line) => line.startsWith("data:"))
      .map((line) => line.slice("data:".length).replace(/^ /, ""))
      .join("\n");

    if (!data.trim()) return; // 心跳 / 注释帧

    let parsed: unknown;
    try {
      parsed = JSON.parse(data);
    } catch {
      throw new ApiError("PROTOCOL_ERROR", "事件不是合法 JSON");
    }

    this.onEvent(parseAgentEvent(parsed));
  }
}

export async function fetchSteps(): Promise<{
  version: number;
  steps: { id: string; label: string; conditional?: boolean }[];
}> {
  const response = await fetch("/api/steps");
  if (!response.ok) {
    throw new ApiError("HTTP_ERROR", `加载步骤目录失败：HTTP ${response.status}`);
  }
  return (await response.json()) as {
    version: number;
    steps: { id: string; label: string; conditional?: boolean }[];
  };
}

/**
 * 拉取错误码表。
 *
 * 前端**不硬编码**码的含义：`SQL_REJECTED` 显示成「SQL 被安全策略拒绝」
 * 这件事由后端从 `contracts/errors.yaml` 下发。契约只有一份。
 */
export async function fetchErrorCodes(): Promise<ErrorTableResponse> {
  const response = await fetch("/api/errors");
  if (!response.ok) {
    throw new ApiError(
      "HTTP_ERROR",
      `加载错误码表失败：HTTP ${response.status}`,
    );
  }
  return (await response.json()) as ErrorTableResponse;
}

/**
 * 取消一次问数。
 *
 * 取消是一个**独立状态**，不是「完成」：服务端会真的停下这次问数，
 * 前端把界面标成「已取消」，步骤条停止推进。
 */
export async function cancelQuery(requestId: string): Promise<boolean> {
  const response = await fetch("/api/cancel", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ request_id: requestId }),
  });
  if (!response.ok) {
    throw new ApiError("HTTP_ERROR", `取消失败：HTTP ${response.status}`);
  }
  const payload = (await response.json()) as { cancelled: boolean };
  return Boolean(payload.cancelled);
}

export interface StreamOptions {
  signal?: AbortSignal;
  /** 后端在响应头里给出 request_id，前端靠它取消这次问数。 */
  onRequestId?: (requestId: string) => void;
}

export async function streamQuery(
  query: string,
  onEvent: (event: AgentEvent) => void,
  options: StreamOptions = {},
): Promise<void> {
  const response = await fetch("/api/query", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ query }),
    signal: options.signal,
  });

  if (!response.ok) {
    let detail = `HTTP ${response.status}`;
    try {
      const body: unknown = await response.json();
      if (isRecord(body) && body.detail !== undefined) {
        detail =
          typeof body.detail === "string"
            ? body.detail
            : JSON.stringify(body.detail);
      }
    } catch {
      // 错误体不是 JSON，保留 HTTP 状态
    }
    throw new ApiError(
      response.status === 422 ? "VALIDATION_ERROR" : "HTTP_ERROR",
      detail,
    );
  }

  const requestId = response.headers.get("X-Request-Id");
  if (requestId) options.onRequestId?.(requestId);

  if (!response.body) {
    throw new ApiError("EMPTY_BODY", "响应没有 body");
  }

  const reader = response.body.getReader();
  const decoder = new TextDecoder();
  const parser = new SseParser(onEvent);

  while (true) {
    const { done, value } = await reader.read();
    if (done) break;
    parser.feed(decoder.decode(value, { stream: true }));
  }

  parser.feed(decoder.decode()); // flush TextDecoder
  parser.flush(); // 处理缺少结尾空行的最后一帧
}
