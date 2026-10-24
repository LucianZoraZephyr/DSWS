/**
 * S0 浏览器冒烟测试
 *
 * 由 scripts/stage0-ui.sh 启动隔离的临时后端/前端（默认 8101 / 5274）。
 * 也可以手动：
 *   BASE_URL=http://127.0.0.1:5273 npm run test:ui
 *
 * 断言的是「契约在真实浏览器里也成立」：
 *   - 步骤条的数量/顺序/文案来自 /api/steps，不是前端硬编码
 *   - 11 个非条件步 success，correct_sql 显式 skipped 且文案是「未触发」
 *   - 结果 4 行、列名/首行值正确
 *   - 正常路径没有错误卡、控制台错误、失败请求
 */

import assert from "node:assert/strict";
import { existsSync } from "node:fs";

import { chromium } from "playwright-core";

const BASE_URL = process.env.BASE_URL ?? "http://127.0.0.1:5273";
const CANDIDATES = [
  process.env.CHROME_PATH,
  "/usr/bin/google-chrome",
  "/usr/bin/google-chrome-stable",
  "/usr/bin/chromium",
  "/usr/bin/chromium-browser",
].filter(Boolean);

const executablePath = CANDIDATES.find((candidate) => existsSync(candidate));

if (!executablePath) {
  console.error(
    "找不到 Chrome。请安装 Chrome/Chromium，或设置 CHROME_PATH=/path/to/chrome。",
  );
  process.exit(2);
}

const browser = await chromium.launch({
  executablePath,
  headless: true,
  args: ["--no-sandbox"],
});

const page = await browser.newPage({ viewport: { width: 1440, height: 1000 } });
const consoleErrors = [];
const failedRequests = [];

page.on("console", (message) => {
  if (message.type() === "error") consoleErrors.push(message.text());
});
page.on("pageerror", (error) => consoleErrors.push(String(error)));
page.on("requestfailed", (request) => {
  failedRequests.push(
    `${request.url()} ${request.failure()?.errorText ?? "unknown"}`,
  );
});

try {
  // 先挂响应监听，再导航，避免 /api/steps 太快而错过
  const stepsResponsePromise = page.waitForResponse(
    (response) => response.url().includes("/api/steps") && response.ok(),
  );

  await page.goto(BASE_URL, { waitUntil: "load" });
  const catalog = await (await stepsResponsePromise).json();

  await page.waitForSelector("text=电商问数智能体", { timeout: 15_000 });

  const railCount = await page.locator(".rail-item").count();
  assert.equal(railCount, 12, "步骤条应有 12 步");
  assert.equal(
    railCount,
    catalog.steps.length,
    "步骤条数量应等于 /api/steps 下发的数量",
  );

  const labels = (await page.locator(".rail-label").allTextContents()).map(
    (label) => label.trim(),
  );
  assert.deepEqual(
    labels,
    catalog.steps.map((step) => step.label),
    "步骤顺序与文案必须来自 /api/steps",
  );

  const conditionalLabels = catalog.steps
    .filter((step) => step.conditional)
    .map((step) => step.label);
  assert.deepEqual(conditionalLabels, ["校正SQL"], "应只有一个条件步");

  await page.locator("textarea").fill("华北地区销售额");
  await page.getByRole("button", { name: "提问" }).click();

  // 等结果表真的出现（不是命中用户提问气泡里的「华北」）
  await page.waitForFunction(
    () => document.querySelectorAll("tbody tr").length === 4,
    null,
    { timeout: 20_000 },
  );
  await page.waitForFunction(
    () => !document.querySelector(".spinner.dark"),
    null,
    { timeout: 20_000 },
  );

  const success = await page.locator(".rail-success").count();
  const skipped = await page.locator(".rail-skipped").count();
  assert.equal(success, 11, "11 个非条件步应全部 success");
  assert.equal(skipped, 1, "correct_sql 应显式 skipped");

  const skippedLabel = (
    await page.locator(".rail-skipped .rail-label").textContent()
  )?.trim();
  const skippedStatus = (
    await page.locator(".rail-skipped .rail-status").textContent()
  )?.trim();
  assert.equal(skippedLabel, "校正SQL", "被跳过的必须是 correct_sql");
  assert.equal(skippedStatus, "未触发", "skipped 的文案必须是「未触发」");

  const headers = await page.locator("thead th").allTextContents();
  assert.ok(headers.some((header) => header.includes("地区")), "缺少「地区」列");
  assert.ok(
    headers.some((header) => header.includes("销售额")),
    "缺少「销售额」列",
  );
  assert.ok(
    headers.some((header) => header.includes("订单数")),
    "缺少「订单数」列",
  );

  const firstRow = (
    await page.locator("tbody tr").first().locator("td").allTextContents()
  ).map((cell) => cell.trim());
  assert.deepEqual(firstRow, ["华北", "128,400", "32"], "首行结果不符合预期");

  assert.equal(
    await page.locator(".error-card").count(),
    0,
    "正常路径不应出现错误卡",
  );
  assert.deepEqual(consoleErrors, [], `控制台错误：${consoleErrors.join(" | ")}`);
  assert.deepEqual(
    failedRequests,
    [],
    `失败请求：${failedRequests.join(" | ")}`,
  );

  console.log(
    JSON.stringify(
      {
        ok: true,
        railCount,
        success,
        skipped,
        skippedLabel,
        skippedStatus,
        rows: 4,
        firstRow,
      },
      null,
      2,
    ),
  );
} finally {
  await browser.close();
}
