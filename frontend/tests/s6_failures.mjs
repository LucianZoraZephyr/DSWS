/**
 * S6 浏览器验收：6 类失败 + 取消
 *
 * 由 scripts/stage6-ui.sh 启动隔离的临时后端（场景应用）/前端
 * （默认 8102 / 5275）。也可以手动：
 *   BASE_URL=http://127.0.0.1:5273 npm run test:ui:s6
 *
 * 断言的是「失败在真实浏览器里也成立」：
 *   1. 6 类失败各自有**自己的**界面状态（码 / 类别 / 失败步骤互不相同）
 *   2. 真失败：失败步骤红叉 + 红框 + 错误码可见 + 可复制
 *   3. 空结果：**不**标红（合法但没有数据，不是失败）
 *   4. 取消：状态 = 已取消（不是「完成」），步骤条停止推进
 */

import assert from "node:assert/strict";
import { chromium } from "playwright-core";

import { launchArgs, resolveChromePath } from "./chrome.mjs";

const BASE_URL = process.env.BASE_URL ?? "http://127.0.0.1:5273";

/** 路线图 S6 点名的 6 类失败。 */
const FAILURES = [
  { id: "sql_rejected", code: "SQL_REJECTED", klass: "failure", step: "validate_sql" },
  { id: "sql_syntax", code: "SQL_SYNTAX", klass: "failure", step: "validate_sql" },
  {
    id: "sql_unknown_column",
    code: "SQL_UNKNOWN_COLUMN",
    klass: "failure",
    step: "validate_sql",
  },
  { id: "empty_result", code: "EMPTY_RESULT", klass: "empty", step: "run_sql" },
  { id: "llm_refusal", code: "LLM_REFUSAL", klass: "failure", step: "generate_sql" },
  { id: "timeout", code: "TIMEOUT", klass: "failure", step: "run_sql" },
];

const executablePath = resolveChromePath();
if (!executablePath) {
  console.error(
    "找不到 Chrome/Edge。请安装，或设置 CHROME_PATH=/path/to/chrome。",
  );
  process.exit(2);
}

const browser = await chromium.launch({
  executablePath,
  headless: true,
  args: launchArgs(),
});

const page = await browser.newPage({ viewport: { width: 1440, height: 1000 } });
const consoleErrors = [];

page.on("console", (message) => {
  if (message.type() === "error") consoleErrors.push(message.text());
});
page.on("pageerror", (error) => consoleErrors.push(String(error)));

/** 读步骤条的最终状态：[{label, status}] */
async function readRail() {
  return page.$$eval(".rail-item", (items) =>
    items.map((element) => ({
      label: element.querySelector(".rail-label")?.textContent?.trim() ?? "",
      // class 形如 "rail-item rail-error"：只取状态那一个，别把
      // "rail-item" 里的 "item" 当成状态。
      status:
        element.className.match(
          /rail-(success|error|running|skipped|cancelled|pending)/,
        )?.[1] ?? "",
    })),
  );
}

async function ask(scenarioId) {
  await page.locator("textarea").fill(`场景:${scenarioId} 华北销售额`);
  await page.getByRole("button", { name: "提问" }).click();
}

try {
  const stepsResponsePromise = page.waitForResponse(
    (response) => response.url().includes("/api/steps") && response.ok(),
  );
  await page.goto(BASE_URL, { waitUntil: "load" });
  const catalog = await (await stepsResponsePromise).json();

  const labelOf = new Map(catalog.steps.map((step) => [step.id, step.label]));
  assert.ok(labelOf.size > 0, "步骤目录应当非空");

  // ---- 6 类失败 ---------------------------------------------------------
  const signatures = new Set();
  const observed = [];

  for (const failure of FAILURES) {
    await ask(failure.id);
    await page.waitForSelector(".error-card", { timeout: 20_000 });

    const card = page.locator(".error-card").first();
    const code = await card.getAttribute("data-error-code");
    const klass = await card.getAttribute("data-error-class");

    assert.equal(code, failure.code, `${failure.id} 的错误码应当是 ${failure.code}`);
    assert.equal(klass, failure.klass, `${failure.id} 的类别应当是 ${failure.klass}`);

    // 错误码要**看得见**（不是只在 data 属性里）
    const codeText = (await card.locator("code").first().textContent())?.trim();
    assert.equal(codeText, failure.code, "错误码必须显示在卡片上");

    // 失败步骤
    const rail = await readRail();
    const failed = rail.find((item) => item.label === labelOf.get(failure.step));
    assert.ok(failed, `步骤条里应当有「${labelOf.get(failure.step)}」`);

    if (failure.klass === "failure") {
      assert.equal(failed.status, "error", `${failure.id} 的失败步骤应当是红叉`);
    } else {
      // 空结果不是失败：执行步骤仍然是绿的
      assert.equal(
        failed.status,
        "success",
        `${failure.id} 是合法但无数据，执行步骤不应当标红`,
      );
    }

    // 可复制
    const copyButton = card.getByRole("button", { name: /复制错误|已复制/ });
    assert.equal(await copyButton.count(), 1, "错误必须可以复制");

    const signature = `${code}|${klass}|${failure.step}|${failed.status}`;
    assert.ok(
      !signatures.has(signature),
      `${failure.id} 的界面状态与别的失败重复了：${signature}`,
    );
    signatures.add(signature);
    observed.push({ id: failure.id, code, klass, step: failure.step, status: failed.status });
  }

  assert.equal(
    signatures.size,
    FAILURES.length,
    `6 类失败只给出了 ${signatures.size} 种界面状态`,
  );

  // ---- 取消 -------------------------------------------------------------
  await page.locator("textarea").fill("场景:slow 华北销售额");
  await page.getByRole("button", { name: "提问" }).click();
  await page.waitForSelector(".rail-running", { timeout: 20_000 });
  await page.getByRole("button", { name: "停止" }).click();

  await page.waitForFunction(
    () =>
      document
        .querySelector('[data-testid="run-status"]')
        ?.textContent?.trim() === "已取消",
    null,
    { timeout: 20_000 },
  );

  const cancelledRail = await readRail();
  assert.ok(
    cancelledRail.some((item) => item.status === "cancelled"),
    "取消之后，正在跑的步骤应当标成「已取消」",
  );
  assert.equal(
    await page.locator(".error-card").count(),
    0,
    "取消不是错误，不该弹红框",
  );

  // 步骤条停止推进：等一会儿，状态不能再变
  await page.waitForTimeout(2000);
  const afterWait = await readRail();
  assert.deepEqual(
    afterWait.map((item) => item.status),
    cancelledRail.map((item) => item.status),
    "取消之后步骤条不应继续推进",
  );

  assert.deepEqual(consoleErrors, [], `控制台错误：${consoleErrors.join(" | ")}`);

  console.log(
    JSON.stringify(
      {
        ok: true,
        failures: observed,
        distinctSignatures: signatures.size,
        cancelled: true,
      },
      null,
      2,
    ),
  );
} finally {
  await browser.close();
}
