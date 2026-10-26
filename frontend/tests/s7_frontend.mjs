/**
 * S7 浏览器验收：13 条前端缺陷（F1–F13）各一条断言。
 *
 * 由 scripts/stage7-ui.sh 启动**回放服务器**（frontend/tests/replay-server.mjs）
 * 与隔离的前端（默认 8103 / 5276）。也可以手动：
 *
 *   node tests/replay-server.mjs --port 8103
 *   VITE_DEV_PROXY_TARGET=http://127.0.0.1:8103 npm run dev -- --port 5276
 *   BASE_URL=http://127.0.0.1:5276 npm run test:ui:s7
 *
 * 为什么用回放而不是真后端：有些界面行为真系统**产生不了**（比如旧系统的
 * 伪 SQL 行），但界面必须照样处理对。回放夹具录的是后端真实吐出的字节流，
 * 只归一化了 duration_ms / request_id。
 *
 * 映射表见 docs/S7-HANDOFF.md §4.2——**照那张表写**。
 */

import assert from "node:assert/strict";

import { chromium } from "playwright-core";

import { launchArgs, resolveChromePath } from "./chrome.mjs";

const BASE_URL = process.env.BASE_URL ?? "http://127.0.0.1:5273";
const DEFAULT_VIEWPORT = { width: 1440, height: 1000 };

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

const page = await browser.newPage({ viewport: DEFAULT_VIEWPORT });

const consoleErrors = [];
const pageErrors = [];

page.on("console", (message) => {
  if (message.type() !== "error") return;
  // HTTP 500 / 422 是 F11 的**输入**：Chromium 会把它记成一条资源错误。
  // 这不是页面自身的错误，别把它算进「控制台不干净」。
  if (/Failed to load resource/.test(message.text())) return;
  consoleErrors.push(message.text());
});
page.on("pageerror", (error) => pageErrors.push(String(error)));

/** 打开应用并等步骤目录就绪。 */
async function openApp() {
  await page.setViewportSize(DEFAULT_VIEWPORT);
  await page.goto(BASE_URL, { waitUntil: "domcontentloaded" });
  await page.waitForFunction(
    () => document.querySelectorAll(".rail-item").length === 12,
    null,
    { timeout: 20_000 },
  );
  await page.waitForSelector("textarea", { timeout: 20_000 });
}

/** 用「场景:<id>」约定发起一次问数。 */
async function ask(scenarioId) {
  await page.locator("textarea").fill(`场景:${scenarioId} 华北销售额`);
  await page.getByRole("button", { name: "提问", exact: true }).click();
}

async function runStatus() {
  return (
    await page.locator('[data-testid="run-status"]').textContent()
  )?.trim();
}

async function waitRunStatus(text, timeout = 20_000) {
  await page.waitForFunction(
    (expected) =>
      document
        .querySelector('[data-testid="run-status"]')
        ?.textContent?.trim() === expected,
    text,
    { timeout },
  );
}

async function waitForTable() {
  await page.waitForSelector("tbody tr", { timeout: 20_000 });
}

const results = [];

async function check(id, title, fn) {
  try {
    await fn();
    results.push({ id, title, ok: true });
    console.log(`✓ ${id} ${title}`);
  } catch (error) {
    results.push({ id, title, ok: false, error: String(error) });
    console.error(`✗ ${id} ${title}\n  ${String(error).split("\n")[0]}`);
  }
}

try {
  // ---- F1 / F2：取消不是「完成」，running 步骤不标红 -------------------
  let slowCancelled = false;

  await check("F1", "取消被算成「完成」", async () => {
    await openApp();
    await ask("slow");
    await page.waitForSelector(".rail-running", { timeout: 20_000 });
    await page.getByRole("button", { name: "停止", exact: true }).click();

    await waitRunStatus("已取消");
    assert.equal(await page.locator("table").count(), 0, "取消后不应出现结果表");
    slowCancelled = true;
  });

  await check("F2", "取消时 running 步骤被标红叉", async () => {
    assert.ok(slowCancelled, "F1 没有完成，无法验证取消态");
    assert.ok(
      (await page.locator(".rail-cancelled").count()) > 0,
      "取消后应当有步骤标成「已取消」",
    );
    assert.equal(
      await page.locator(".rail-error").count(),
      0,
      "取消不是失败，不应出现红叉",
    );
    assert.equal(await page.locator(".error-card").count(), 0, "取消不应弹红框");
  });

  // ---- F3：未知事件类型不是错误 ---------------------------------------
  await check("F3", "未知事件类型被当成错误", async () => {
    await openApp();
    await ask("unknown_event");

    await page.waitForSelector(
      '[data-testid="unknown-type-notice"]',
      { timeout: 20_000 },
    );
    await waitForTable();

    assert.equal(
      await page.locator(".error-card").count(),
      0,
      "未知事件类型不应产生错误卡",
    );
    assert.equal(
      (await page.locator("tbody tr").count()),
      1,
      "忽略未知事件之后，正常结果仍应渲染",
    );
  });

  // ---- F4：空数组 → 表格消失 ------------------------------------------
  await check("F4", "空数组 → 表格消失", async () => {
    await openApp();
    await ask("empty_rows");

    const empty = page.locator('[data-testid="result-empty"]');
    await empty.waitFor({ state: "visible", timeout: 20_000 });

    assert.match(
      (await empty.textContent()) ?? "",
      /没有数据/,
      "空态块必须说明「没有数据」",
    );
    assert.equal(await page.locator("table").count(), 0, "空结果不应渲染表格");
  });

  // ---- F5：伪 SQL 行被当成功 ------------------------------------------
  await check("F5", "伪 SQL 行被当成功", async () => {
    await openApp();
    await ask("pseudo_row");

    const card = page.locator('[data-pseudo-error="true"]');
    await card.waitFor({ state: "visible", timeout: 20_000 });

    assert.match(
      (await card.textContent()) ?? "",
      /无法生成SQL/,
      "伪 SQL 卡必须展示那段文字",
    );
    assert.equal(await page.locator("table").count(), 0, "伪 SQL 行不能画成表");
  });

  // ---- F6：全 NULL 列像正常表 -----------------------------------------
  await check("F6", "全 NULL 列像正常表", async () => {
    await openApp();
    await ask("null_column");
    await waitForTable();

    assert.ok(
      (await page.locator('[data-testid="col-all-null"]').count()) > 0,
      "全 NULL 列头必须有「无数据」标记",
    );
    const note = page.locator('[data-testid="null-columns-note"]');
    assert.ok(await note.count(), "顶部必须有全 NULL 列的说明");
    assert.match((await note.textContent()) ?? "", /退款额/, "说明要点名是哪一列");
  });

  // ---- F7：错误不带失败步骤 -------------------------------------------
  await check("F7", "错误不带失败步骤", async () => {
    await openApp();
    await ask("sql_rejected");

    const card = page.locator(".error-card").first();
    await card.waitFor({ state: "visible", timeout: 20_000 });
    assert.equal(
      await card.getAttribute("data-error-code"),
      "SQL_REJECTED",
    );
    assert.match(
      (await card.textContent()) ?? "",
      /失败步骤：校验SQL/,
      "错误卡必须显示中文失败步骤",
    );
  });

  // ---- F8：固定 820×780 画布 ------------------------------------------
  await check("F8", "固定 820×780 画布", async () => {
    await openApp();
    await ask("happy");
    await waitForTable();

    await page.setViewportSize({ width: 420, height: 900 });
    await page.waitForTimeout(150);

    const sizes = await page.evaluate(() => ({
      innerWidth: window.innerWidth,
      scrollWidth: Math.max(
        document.documentElement.scrollWidth,
        document.body.scrollWidth,
      ),
    }));
    assert.ok(
      sizes.scrollWidth <= sizes.innerWidth + 1,
      `420px 视口不应横向滚动（scrollWidth=${sizes.scrollWidth}, innerWidth=${sizes.innerWidth}）`,
    );
  });

  // ---- F9：条件步永远灰着 ---------------------------------------------
  await check("F9", "条件步永远灰着", async () => {
    await openApp();
    await ask("happy");
    await waitForTable();

    const skipped = page.locator(".rail-skipped");
    assert.equal(await skipped.count(), 1, "应当恰好有一个「未触发」的条件步");
    assert.match(
      (await skipped.textContent()) ?? "",
      /未触发/,
      "条件步文案必须是「未触发」",
    );
  });

  // ---- F10：数字不格式化 ----------------------------------------------
  await check("F10", "数字不格式化", async () => {
    await openApp();
    await ask("happy");
    await waitForTable();

    const headers = page.locator("thead th");
    const headerCount = await headers.count();
    let columnIndex = -1;
    for (let index = 0; index < headerCount; index += 1) {
      if (((await headers.nth(index).textContent()) ?? "").includes("销售额")) {
        columnIndex = index;
        break;
      }
    }
    assert.ok(columnIndex >= 0, "缺少「销售额」列");

    const typeText = (
      await headers.nth(columnIndex).locator(".col-type").textContent()
    )?.trim();
    assert.equal(typeText, "number", "列头应标出 number 类型");

    const cellText = (
      await page
        .locator("tbody tr")
        .first()
        .locator("td")
        .nth(columnIndex)
        .textContent()
    )?.trim();
    assert.equal(cellText, "128,400", "数字必须千分位格式化");
  });

  // ---- F12：步骤条不显示耗时 ------------------------------------------
  await check("F12", "步骤条不显示耗时", async () => {
    await openApp();
    await ask("happy");
    await waitForTable();

    const durations = page.locator(".rail-duration");
    assert.ok((await durations.count()) > 0, "成功步骤应显示耗时");
    assert.ok(
      ((await durations.first().textContent()) ?? "").trim().length > 0,
      "耗时不能是空字符串",
    );
  });

  // ---- F13：复制把 "10" 原样复制 --------------------------------------
  await check("F13", "数字被当成字符串", async () => {
    await openApp();
    await ask("happy");
    await waitForTable();

    // 复制内容本身由 Vitest 单测断言（无头环境没有剪贴板权限）。
    // 浏览器里断言「没有数字样式的字符串」——列类型说 number，值就必须是数字。
    assert.equal(
      await page.locator('[data-testid="cell-numeric-string"]').count(),
      0,
      "真实数字不应被标成「数字样式的字符串」",
    );
  });

  // ---- F11：4xx/5xx 一律同一句话 --------------------------------------
  await check("F11a", "5xx 与 422 用同一句话（500）", async () => {
    await openApp();
    await ask("http_500");

    const card = page.locator(".error-card").first();
    await card.waitFor({ state: "visible", timeout: 20_000 });
    assert.match(
      (await card.textContent()) ?? "",
      /服务异常/,
      "5xx 应说明「服务异常」",
    );
  });

  await check("F11b", "5xx 与 422 用同一句话（422）", async () => {
    await openApp();
    await ask("http_422");

    const card = page.locator(".error-card").first();
    await card.waitFor({ state: "visible", timeout: 20_000 });
    assert.match(
      (await card.textContent()) ?? "",
      /问题格式不对/,
      "422 应说明「问题格式不对」",
    );
  });

  // ---- 控制台必须干净 -------------------------------------------------
  assert.deepEqual(pageErrors, [], `页面未捕获异常：${pageErrors.join(" | ")}`);
  assert.deepEqual(
    consoleErrors,
    [],
    `控制台错误：${consoleErrors.join(" | ")}`,
  );

  const failures = results.filter((result) => !result.ok);
  console.log(
    JSON.stringify(
      {
        ok: failures.length === 0,
        passed: results.length - failures.length,
        failed: failures.length,
        cases: results,
      },
      null,
      2,
    ),
  );

  if (failures.length > 0) {
    process.exitCode = 1;
  }
} catch (error) {
  console.error(error);
  process.exitCode = 1;
} finally {
  await browser.close();
}
