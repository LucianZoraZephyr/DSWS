/**
 * S0 浏览器冒烟测试
 *
 * 前提：dev-backend（8100）与 dev-frontend（5273）已启动。
 * 用法：npm run test:ui
 *
 * 断言的是「契约在真实浏览器里也成立」：
 *   12 步 / 11 成功 / 1 未触发 / 4 行结果 / 控制台零错误
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
const errors = [];

page.on("console", (message) => {
  if (message.type() === "error") errors.push(message.text());
});
page.on("pageerror", (error) => errors.push(String(error)));

try {
  await page.goto(BASE_URL, { waitUntil: "load" });
  await page.waitForSelector("text=电商问数智能体", { timeout: 15_000 });

  const railCount = await page.locator(".rail-item").count();
  assert.equal(railCount, 12, "步骤条应有 12 步");
  assert.equal(
    await page.locator(".rail-skipped").count(),
    0,
    "初始状态不应有 skipped",
  );

  await page.locator("textarea").fill("华北地区销售额");
  await page.getByRole("button", { name: "提问" }).click();

  await page.waitForSelector("text=华北", { timeout: 20_000 });
  await page.waitForFunction(
    () => !document.querySelector(".spinner.dark"),
    null,
    { timeout: 20_000 },
  );

  const success = await page.locator(".rail-success").count();
  const skipped = await page.locator(".rail-skipped").count();
  const rows = await page.locator("tbody tr").count();

  assert.equal(success, 11, "11 个非条件步应全部 success");
  assert.equal(skipped, 1, "correct_sql 应显式 skipped");
  assert.equal(rows, 4, "假结果应有 4 行");
  assert.deepEqual(errors, [], `控制台出现错误：${errors.join(" | ")}`);

  console.log(
    JSON.stringify({ ok: true, railCount, success, skipped, rows }, null, 2),
  );
} finally {
  await browser.close();
}
