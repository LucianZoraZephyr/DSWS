/**
 * S7 回放服务器：把录制好的 SSE 夹具按真实协议吐出来。
 *
 * 由 scripts/stage7-ui.sh 启动（默认 8103），也可以手动：
 *   node tests/replay-server.mjs --port 8103
 *
 * 协议与生产后端一致：
 *   GET  /api/health    -> {"status":"ok","stage":"S7"}
 *   GET  /api/steps     -> generated/contract.json 的 steps（steps.yaml 的投影）
 *   GET  /api/errors    -> generated/contract.json 的 errors（errors.yaml 的投影）
 *   POST /api/query     -> 按 query 里的「场景:<id>」找夹具，逐帧写 SSE
 *   POST /api/cancel    -> {"cancelled": true}
 *
 * 约定：
 *   - query 写成「场景:<夹具 id> <真实问题>」，和场景应用同一套约定。
 *   - 找不到夹具 → 422。
 *   - 「场景:http_500」/「场景:http_422」直接返回对应 HTTP 状态，
 *     用来验收 F11（4xx / 5xx 不能是同一句话）。
 *
 * 为什么不直接起真后端：有些流真系统**产生不了**（旧系统的伪 SQL 行、
 * 契约之外的事件），但界面仍必须处理对。夹具录的是真后端吐出的字节流，
 * 只归一化了 duration_ms / request_id。
 *
 * 清理：这个脚本不 `pkill` 任何东西；谁起的进程谁按 pid 清理
 * （见 scripts/stage7-ui.sh）。
 */

import { randomUUID } from "node:crypto";
import { readFileSync, existsSync } from "node:fs";
import { createServer } from "node:http";
import { dirname, join } from "node:path";
import { fileURLToPath } from "node:url";

const HERE = dirname(fileURLToPath(import.meta.url));
const FRONTEND_ROOT = join(HERE, "..");
const REBUILD_ROOT = join(FRONTEND_ROOT, "..");
const CONTRACT_PATH = join(
  FRONTEND_ROOT,
  "src",
  "generated",
  "contract.json",
);
const FIXTURE_DIR = join(REBUILD_ROOT, "fixtures", "sse");

/** 帧与帧之间的间隔：让步骤条真的「走」起来，浏览器才有东西可点。 */
const FRAME_DELAY_MS = 80;

const contract = JSON.parse(readFileSync(CONTRACT_PATH, "utf8"));

/** 正在跑的 request_id → 是否已被取消。 */
const active = new Map();

function parseArgs(argv) {
  let port = 8103;
  let host = "127.0.0.1";
  for (let index = 0; index < argv.length; index += 1) {
    if (argv[index] === "--port") port = Number(argv[++index]);
    else if (argv[index] === "--host") host = argv[++index];
  }
  return { port, host };
}

function sendJson(response, status, payload) {
  const body = JSON.stringify(payload);
  response.writeHead(status, {
    "Content-Type": "application/json; charset=utf-8",
    "Content-Length": Buffer.byteLength(body),
  });
  response.end(body);
}

function readJsonBody(request) {
  return new Promise((resolve, reject) => {
    let raw = "";
    request.on("data", (chunk) => {
      raw += chunk;
      if (raw.length > 1_000_000) {
        reject(new Error("请求体过大"));
        request.destroy();
      }
    });
    request.on("end", () => {
      if (!raw.trim()) return resolve({});
      try {
        resolve(JSON.parse(raw));
      } catch {
        reject(new Error("请求体不是合法 JSON"));
      }
    });
    request.on("error", reject);
  });
}

function sleep(ms) {
  return new Promise((resolve) => setTimeout(resolve, ms));
}

function loadFixture(id) {
  const path = join(FIXTURE_DIR, `${id}.json`);
  if (!existsSync(path)) return null;
  return JSON.parse(readFileSync(path, "utf8"));
}

/**
 * 把夹具逐帧写出去。
 *
 * `delay_before_last_ms` 只作用在**最后一帧之前**：`slow` 夹具靠它给
 * 浏览器留出点「停止」的时间。客户端一断开就立刻停手，不再往一个死掉的
 * socket 里写。
 */
async function streamFixture(request, response, fixture, requestId) {
  response.writeHead(200, {
    "Content-Type": "text/event-stream; charset=utf-8",
    "Cache-Control": "no-cache",
    Connection: "keep-alive",
    "X-Accel-Buffering": "no",
    "X-Request-Id": requestId,
  });
  response.flushHeaders?.();

  let closed = false;
  const stop = () => {
    closed = true;
  };
  request.on("close", stop);
  response.on("close", stop);
  response.on("error", stop);

  const frames = fixture.frames;
  for (let index = 0; index < frames.length; index += 1) {
    if (closed || response.destroyed || response.writableEnded) break;
    if (active.get(requestId) === "cancelled") break;

    const isLast = index === frames.length - 1;
    const wait = isLast
      ? Math.max(fixture.delay_before_last_ms ?? 0, FRAME_DELAY_MS)
      : FRAME_DELAY_MS;
    if (wait > 0) await sleep(wait);
    if (closed || response.destroyed || response.writableEnded) break;
    if (active.get(requestId) === "cancelled") break;

    try {
      response.write(`${frames[index]}\n\n`);
    } catch {
      break;
    }
  }

  active.delete(requestId);
  if (!response.writableEnded && !response.destroyed) response.end();
}

const server = createServer(async (request, response) => {
  const url = new URL(request.url ?? "/", "http://127.0.0.1");

  try {
    if (request.method === "GET" && url.pathname === "/api/health") {
      return sendJson(response, 200, { status: "ok", stage: "S7" });
    }

    if (request.method === "GET" && url.pathname === "/api/steps") {
      // contract.steps 的形状与生产后端 /api/steps 一致：{version, steps}。
      return sendJson(response, 200, contract.steps);
    }

    if (request.method === "GET" && url.pathname === "/api/errors") {
      // contract.json 里 codes 是「码 → 规格」的映射；生产后端下发的是
      // 带 `code` 字段的数组。这里做**形状转换**，不改变内容。
      const { version, classes, codes } = contract.errors;
      return sendJson(response, 200, {
        version,
        classes,
        codes: Object.entries(codes).map(([code, spec]) => ({
          code,
          ...spec,
        })),
      });
    }

    if (request.method === "POST" && url.pathname === "/api/query") {
      const body = await readJsonBody(request);
      const query = typeof body.query === "string" ? body.query.trim() : "";
      if (!query) {
        return sendJson(response, 422, { detail: "query 不能为空" });
      }

      const match = /场景:([0-9A-Za-z_]+)/.exec(query);
      if (!match) {
        return sendJson(response, 422, {
          detail: "回放约定：query 必须写成「场景:<夹具 id> ...」",
        });
      }
      const fixtureId = match[1];

      // F11：模拟 HTTP 失败（不是 SSE 里的 error 事件）。
      if (fixtureId === "http_500") {
        return sendJson(response, 500, { detail: "回放服务器模拟的内部错误" });
      }
      if (fixtureId === "http_422") {
        return sendJson(response, 422, { detail: "回放服务器模拟的格式错误" });
      }

      const fixture = loadFixture(fixtureId);
      if (!fixture) {
        return sendJson(response, 422, {
          detail: `没有回放夹具：${fixtureId}`,
        });
      }

      const requestId = randomUUID();
      active.set(requestId, "running");
      await streamFixture(request, response, fixture, requestId);
      return;
    }

    if (request.method === "POST" && url.pathname === "/api/cancel") {
      const body = await readJsonBody(request);
      const requestId = typeof body.request_id === "string" ? body.request_id : "";
      const running = active.has(requestId);
      if (running) active.set(requestId, "cancelled");
      return sendJson(response, 200, {
        request_id: requestId,
        cancelled: running,
      });
    }

    return sendJson(response, 404, { detail: `未知接口：${url.pathname}` });
  } catch (error) {
    if (!response.headersSent) {
      return sendJson(response, 500, {
        detail: String(error?.message ?? error),
      });
    }
    response.destroy();
  }
});

const { port, host } = parseArgs(process.argv.slice(2));

server.listen(port, host, () => {
  console.log(`S7 回放服务器：http://${host}:${port}（夹具 ${FIXTURE_DIR}）`);
});

for (const signal of ["SIGINT", "SIGTERM"]) {
  process.on(signal, () => {
    server.close(() => process.exit(0));
  });
}
