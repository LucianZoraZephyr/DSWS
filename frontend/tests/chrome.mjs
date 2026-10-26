/**
 * 找一个能用的 Chromium 内核浏览器。
 *
 * playwright-core 不自带浏览器，必须指向本机已装的 Chrome / Edge。
 * 优先用 CHROME_PATH，其次按平台猜常见安装位置。
 */
import { existsSync } from "node:fs";

const CANDIDATES = [
  process.env.CHROME_PATH,
  // Windows
  "C:\\Program Files\\Google\\Chrome\\Application\\chrome.exe",
  "C:\\Program Files (x86)\\Google\\Chrome\\Application\\chrome.exe",
  "C:\\Program Files (x86)\\Microsoft\\Edge\\Application\\msedge.exe",
  "C:\\Program Files\\Microsoft\\Edge\\Application\\msedge.exe",
  // Linux / macOS
  "/usr/bin/google-chrome",
  "/usr/bin/google-chrome-stable",
  "/usr/bin/chromium",
  "/usr/bin/chromium-browser",
  "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome",
].filter(Boolean);

export function resolveChromePath() {
  return CANDIDATES.find((candidate) => existsSync(candidate)) ?? null;
}

/**
 * 启动参数。
 *
 * `--no-sandbox` 只在 Linux 上加：Windows 与 macOS 上 Chromium 本来就
 * 不走 Linux 的沙箱机制，硬加这个参数没有意义（而且它会关掉一层保护）。
 */
export function launchArgs() {
  return process.platform === "linux" ? ["--no-sandbox"] : [];
}
