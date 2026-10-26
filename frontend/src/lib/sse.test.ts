import { describe, expect, it } from "vitest";

import type { AgentEvent } from "../types";
import { ApiError, parseAgentEvent, SseParser } from "./api";

const progress = {
  type: "progress",
  step: "run_sql",
  status: "success",
  duration_ms: 12,
} as const;

const result = {
  type: "result",
  data: {
    columns: [{ name: "地区", type: "string" }],
    rows: [["华北"]],
  },
} as const;

const error = {
  type: "error",
  code: "SQL_REJECTED",
  class: "failure",
  message: "拒绝执行",
  step: "validate_sql",
} as const;

function collect(
  chunks: string[],
  onUnknownType?: (type: string) => void,
): AgentEvent[] {
  const events: AgentEvent[] = [];
  const parser = new SseParser((event) => events.push(event), onUnknownType);
  for (const chunk of chunks) parser.feed(chunk);
  parser.flush();
  return events;
}

function frame(value: unknown): string {
  return `data: ${JSON.stringify(value)}\n\n`;
}

describe("parseAgentEvent", () => {
  it("接受合法的 progress / result / error", () => {
    for (const event of [progress, result, error]) {
      const parsed = parseAgentEvent(event);
      expect(parsed.ok).toBe(true);
      if (parsed.ok) expect(parsed.event).toEqual(event);
    }
  });

  it("校验的是契约本身：字段不对就拒绝", () => {
    // success 必须带 duration_ms
    const parsed = parseAgentEvent({
      type: "progress",
      step: "run_sql",
      status: "success",
    });
    expect(parsed.ok).toBe(false);
    if (!parsed.ok && parsed.kind === "invalid") {
      expect(parsed.message).toContain("duration_ms");
    }
  });

  it.each([
    [
      "success 的 duration_ms 为 null",
      { type: "progress", step: "run_sql", status: "success", duration_ms: null },
    ],
    [
      "running 带数字 duration_ms",
      { type: "progress", step: "run_sql", status: "running", duration_ms: 3 },
    ],
    [
      "skipped 带数字 duration_ms",
      { type: "progress", step: "correct_sql", status: "skipped", duration_ms: 0 },
    ],
    [
      "duration_ms 为负数",
      { type: "progress", step: "run_sql", status: "error", duration_ms: -1 },
    ],
  ])("拒绝非法 duration：%s", (_name, event) => {
    const parsed = parseAgentEvent(event);
    expect(parsed.ok).toBe(false);
  });

  it("未知事件类型**不是**失败，而是「契约长了一条腿」", () => {
    // 这是审计 F3：旧系统把任何未知 type 当成 error，于是后端一升级，
    // 用户就看到一次莫名的失败。
    const parsed = parseAgentEvent({ type: "bogus" });
    expect(parsed.ok).toBe(false);
    if (!parsed.ok) {
      expect(parsed.kind).toBe("unknown_type");
      if (parsed.kind === "unknown_type") expect(parsed.type).toBe("bogus");
    }
  });

  it("拒绝未知 status", () => {
    const parsed = parseAgentEvent({
      type: "progress",
      step: "run_sql",
      status: "finished",
      duration_ms: null,
    });
    expect(parsed.ok).toBe(false);
  });

  it("拒绝缺少 step 的 error", () => {
    const parsed = parseAgentEvent({
      type: "error",
      code: "X",
      class: "failure",
      message: "y",
    });
    expect(parsed.ok).toBe(false);
  });

  it("拒绝缺少 class 的 error", () => {
    // class 决定界面怎么渲染：红框 / 空态 / 取消。少了它，失败就只剩
    // 一种长相——那正是旧系统的毛病。
    const parsed = parseAgentEvent({
      type: "error",
      code: "X",
      message: "y",
      step: null,
    });
    expect(parsed.ok).toBe(false);
  });

  it("拒绝未知的 class", () => {
    const parsed = parseAgentEvent({
      type: "error",
      code: "X",
      class: "boom",
      message: "y",
      step: null,
    });
    expect(parsed.ok).toBe(false);
  });

  it("接受三种 class", () => {
    for (const klass of ["failure", "empty", "cancelled"]) {
      const parsed = parseAgentEvent({
        type: "error",
        code: "X",
        class: klass,
        message: "y",
        step: null,
      });
      expect(parsed.ok).toBe(true);
    }
  });

  it("解析错误细节", () => {
    const parsed = parseAgentEvent({
      type: "error",
      code: "SQL_REJECTED",
      class: "failure",
      message: "y",
      step: "validate_sql",
      detail: {
        reason_code: "SQL_FORBIDDEN_CONSTRUCT",
        original_sql: "SELECT 1",
        attempts: 1,
      },
    });
    expect(parsed.ok).toBe(true);
    if (parsed.ok) {
      expect(parsed.event).toMatchObject({
        detail: {
          reason_code: "SQL_FORBIDDEN_CONSTRUCT",
          original_sql: "SELECT 1",
          attempts: 1,
        },
      });
    }
  });

  it("拒绝未定义的细节字段", () => {
    const parsed = parseAgentEvent({
      type: "error",
      code: "X",
      class: "failure",
      message: "y",
      step: null,
      detail: { nope: 1 },
    });
    expect(parsed.ok).toBe(false);
  });

  it("拒绝多出来的顶层字段", () => {
    const parsed = parseAgentEvent({ ...progress, extra: 1 });
    expect(parsed.ok).toBe(false);
  });

  it("拒绝非法列类型与非法单元格", () => {
    expect(
      parseAgentEvent({
        type: "result",
        data: { columns: [{ name: "x", type: "object" }], rows: [] },
      }).ok,
    ).toBe(false);

    expect(
      parseAgentEvent({
        type: "result",
        data: { columns: [{ name: "x", type: "string" }], rows: [[{}]] },
      }).ok,
    ).toBe(false);
  });

  it("接受数字与字符串混列的行（类型保真靠值，不靠猜）", () => {
    const parsed = parseAgentEvent({
      type: "result",
      data: {
        columns: [
          { name: "销量", type: "number" },
          { name: "地区", type: "string" },
        ],
        rows: [[10, "华北"]],
      },
    });
    expect(parsed.ok).toBe(true);
  });
});

describe("SseParser", () => {
  it("解析单帧", () => {
    expect(collect([frame(progress)])).toEqual([progress]);
  });

  it("支持 CRLF 分帧", () => {
    const raw = frame(progress).replace(/\n/g, "\r\n");
    expect(collect([raw])).toEqual([progress]);
  });

  it("多条 data: 行按规范拼接", () => {
    const raw =
      'data: {"type":"progress",\ndata: "step":"run_sql","status":"success","duration_ms":1}\n\n';
    expect(collect([raw])).toEqual([
      { type: "progress", step: "run_sql", status: "success", duration_ms: 1 },
    ]);
  });

  it("跨 chunk 的半帧留在 buffer", () => {
    const raw = frame(progress);
    expect(collect([raw.slice(0, 18), raw.slice(18)])).toEqual([progress]);
  });

  it("flush 处理缺少结尾空行的尾帧", () => {
    expect(collect([`data: ${JSON.stringify(progress)}`])).toEqual([progress]);
  });

  it("非 JSON 帧抛 PROTOCOL_ERROR", () => {
    try {
      collect(["data: {oops}\n\n"]);
      expect.unreachable("应当抛错");
    } catch (reason) {
      expect(reason).toBeInstanceOf(ApiError);
      expect((reason as ApiError).code).toBe("PROTOCOL_ERROR");
    }
  });

  it("忽略心跳/注释帧", () => {
    expect(collect([": keepalive\n\n", "\n\n"])).toEqual([]);
  });

  it("一次 chunk 里多帧全部解析", () => {
    expect(collect([frame(progress) + frame(result)])).toEqual([
      progress,
      result,
    ]);
  });

  it("未知事件类型被忽略，只留一条记录", () => {
    const seen: string[] = [];
    const events = collect(
      [frame({ type: "meta", note: "后端新增的事件" }) + frame(result)],
      (type) => seen.push(type),
    );
    expect(events).toEqual([result]);
    expect(seen).toEqual(["meta"]);
  });
});
