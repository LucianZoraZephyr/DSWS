import type { AgentEvent, StepsResponse } from "../types";

/**
 * 开发期走 Vite 代理（/api → http://127.0.0.1:8100）。
 * 生产期同源部署，因此这里不需要写死域名。
 */

export async function fetchSteps(): Promise<StepsResponse> {
  const response = await fetch("/api/steps");
  if (!response.ok) {
    throw new Error(`加载步骤目录失败：HTTP ${response.status}`);
  }
  return (await response.json()) as StepsResponse;
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

  if (!response.ok || !response.body) {
    throw new Error(`请求失败：HTTP ${response.status}`);
  }

  const reader = response.body.getReader();
  const decoder = new TextDecoder();
  let buffer = "";

  while (true) {
    const { done, value } = await reader.read();
    if (done) break;

    buffer += decoder.decode(value, { stream: true });

    // SSE 以空行分帧；最后一段可能不完整，留在 buffer 里
    const blocks = buffer.split("\n\n");
    buffer = blocks.pop() ?? "";

    for (const block of blocks) {
      const dataLine = block
        .split("\n")
        .find((line) => line.startsWith("data:"));

      if (!dataLine) continue;

      const raw = dataLine.slice("data:".length).trim();
      if (!raw) continue;

      onEvent(JSON.parse(raw) as AgentEvent);
    }
  }
}
