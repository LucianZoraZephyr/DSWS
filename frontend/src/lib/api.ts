import sseSchema from "../generated/sse.schema.json";
import { EVENT_TYPES } from "../generated/sse";
import type { AgentEvent, ErrorTableResponse } from "../types";
import { formatErrors, validate } from "./json-schema";

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

function isRecord(value: unknown): value is Record<string, unknown> {
  return typeof value === "object" && value !== null && !Array.isArray(value);
}

/**
 * 事件解析的结果。
 *
 * 三种结局，而不是两种——这是审计 F3 的落点：
 *
 *   ok            合法事件
 *   unknown_type  **后端加了新事件类型**。这不是「这条消息坏了」，
 *                 是「契约长了一条腿」。旧系统把它当成 error，于是
 *                 后端一升级，用户就看到一次莫名的失败。
 *   invalid       已知类型但字段不合法 —— 契约真的被破坏了，必须报错。
 */
export type EventParseResult =
  | { ok: true; event: AgentEvent }
  | { ok: false; kind: "unknown_type"; type: string }
  | { ok: false; kind: "invalid"; message: string };

/**
 * 运行时校验：验的是**契约本身**。
 *
 * `generated/sse.schema.json` 是 `contracts/sse.schema.json` 的生成副本，
 * 校验器（`./json-schema.ts`）是通用引擎——这里没有任何一条规则是手抄的。
 */
export function parseAgentEvent(value: unknown): EventParseResult {
  if (!isRecord(value)) {
    return { ok: false, kind: "invalid", message: "事件必须是 JSON 对象" };
  }

  const type = value.type;
  if (typeof type !== "string") {
    return { ok: false, kind: "invalid", message: "事件缺少 type" };
  }

  if (!(EVENT_TYPES as readonly string[]).includes(type)) {
    return { ok: false, kind: "unknown_type", type };
  }

  const errors = validate(sseSchema, value);
  if (errors.length > 0) {
    return { ok: false, kind: "invalid", message: formatErrors(errors) };
  }

  return { ok: true, event: value as unknown as AgentEvent };
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
  private readonly onUnknownType: ((type: string) => void) | undefined;

  constructor(
    onEvent: (event: AgentEvent) => void,
    onUnknownType?: (type: string) => void,
  ) {
    this.onEvent = onEvent;
    this.onUnknownType = onUnknownType;
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

    const result = parseAgentEvent(parsed);
    if (result.ok) {
      this.onEvent(result.event);
      return;
    }
    if (result.kind === "unknown_type") {
      // 后端加了新事件类型：**忽略**，但要留下痕迹。
      // 旧系统在这里把它当成 error（App.tsx 的兜底分支），于是后端
      // 一升级，用户就看到一次莫名的失败。
      this.onUnknownType?.(result.type);
      return;
    }
    throw new ApiError("PROTOCOL_ERROR", result.message);
  }
}

/**
 * HTTP 状态 → 人话。
 *
 * 旧系统一律显示「无法连接问数接口。」——422（问题写错了）和 500（服务
 * 挂了）是完全不同的两件事，用户却拿到同一句话（审计 F11）。
 */
function httpError(status: number, detail: string): ApiError {
  if (status === 400 || status === 422) {
    return new ApiError("VALIDATION_ERROR", `问题格式不对：${detail}`);
  }
  if (status === 404) {
    return new ApiError("NOT_FOUND", `接口不存在（HTTP ${status}）。`);
  }
  if (status >= 500) {
    return new ApiError("SERVER_ERROR", `问数服务异常（HTTP ${status}）：${detail}`);
  }
  return new ApiError("HTTP_ERROR", `接口请求失败（HTTP ${status}）：${detail}`);
}

async function readErrorDetail(response: Response): Promise<string> {
  try {
    const body: unknown = await response.json();
    if (isRecord(body) && body.detail !== undefined) {
      return typeof body.detail === "string"
        ? body.detail
        : JSON.stringify(body.detail);
    }
  } catch {
    // 错误体不是 JSON，保留 HTTP 状态
  }
  return `HTTP ${response.status}`;
}

export async function fetchSteps(): Promise<{
  version: number;
  steps: { id: string; label: string; conditional?: boolean }[];
}> {
  let response: Response;
  try {
    response = await fetch("/api/steps");
  } catch (reason) {
    throw new ApiError(
      "NETWORK_ERROR",
      `连不上问数服务：${reason instanceof Error ? reason.message : String(reason)}`,
    );
  }
  if (!response.ok) {
    throw httpError(response.status, await readErrorDetail(response));
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
    throw httpError(response.status, await readErrorDetail(response));
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
    throw httpError(response.status, await readErrorDetail(response));
  }
  const payload = (await response.json()) as { cancelled: boolean };
  return Boolean(payload.cancelled);
}

export interface StreamOptions {
  signal?: AbortSignal;
  /** 后端在响应头里给出 request_id，前端靠它取消这次问数。 */
  onRequestId?: (requestId: string) => void;
  /** 收到未知事件类型时调用（契约长了一条腿，不算失败）。 */
  onUnknownType?: (type: string) => void;
}

export async function streamQuery(
  query: string,
  onEvent: (event: AgentEvent) => void,
  options: StreamOptions = {},
): Promise<void> {
  let response: Response;
  try {
    response = await fetch("/api/query", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ query }),
      signal: options.signal,
    });
  } catch (reason) {
    if (isAbortError(reason)) throw reason;
    throw new ApiError(
      "NETWORK_ERROR",
      `连不上问数服务：${reason instanceof Error ? reason.message : String(reason)}。请确认后端已启动。`,
    );
  }

  if (!response.ok) {
    throw httpError(response.status, await readErrorDetail(response));
  }

  const requestId = response.headers.get("X-Request-Id");
  if (requestId) options.onRequestId?.(requestId);

  if (!response.body) {
    throw new ApiError("EMPTY_BODY", "响应没有 body");
  }

  const reader = response.body.getReader();
  const decoder = new TextDecoder();
  const parser = new SseParser(onEvent, options.onUnknownType);

  while (true) {
    const { done, value } = await reader.read();
    if (done) break;
    parser.feed(decoder.decode(value, { stream: true }));
  }

  parser.feed(decoder.decode()); // flush TextDecoder
  parser.flush(); // 处理缺少结尾空行的最后一帧
}

export function isAbortError(reason: unknown): boolean {
  return (
    typeof reason === "object" &&
    reason !== null &&
    (reason as { name?: string }).name === "AbortError"
  );
}
