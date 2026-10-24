import type {
  AgentEvent,
  CellValue,
  ColumnType,
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
      return {
        type: "error",
        code,
        message,
        step: value.step as string | null,
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

export async function streamQuery(
  query: string,
  onEvent: (event: AgentEvent) => void,
  signal?: AbortSignal,
): Promise<void> {
  const response = await fetch("/api/query", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ query }),
    signal,
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
