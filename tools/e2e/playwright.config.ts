// ZT add-on: Playwright UI QA gate for the Vibe-Trading web UI.
//
// Two modes, one suite:
//   * Container / CI (VT_E2E_BASE_URL unset): starts tools/e2e/fixture_server.py,
//     a throwaway server with fixtures and a random key, on VT_E2E_PORT (8911).
//     VT_E2E_VIA=vt-zt starts it the way the desktop shell does (needs
//     VT_ZT_LAUNCHER_DIR). Build the SPA first: cd frontend && npm run build.
//   * A real server (PC1): VT_E2E_BASE_URL=http://127.0.0.1:8899 and VT_E2E_KEY
//     = the API key (run-pc1.ps1 reads it from home\.env in memory). Read-only:
//     the suite only navigates and opens a report; it never sends a prompt,
//     saves settings or touches orders.
//
// Other knobs: VT_E2E_CHANNEL (e.g. msedge: use the installed Edge, no browser
// download), VT_E2E_OUTPUT (results.json and screenshots; default ./test-results),
// VT_E2E_WORKERS, VT_E2E_PYTHON, VT_E2E_IGNORE_CONSOLE (extra regex to ignore).
// Traces, videos and the HTML report stay off so the key is never written to disk.
import { randomBytes } from "node:crypto";
import path from "node:path";
import { defineConfig } from "@playwright/test";

const here = import.meta.dirname;
const external = (process.env.VT_E2E_BASE_URL ?? "").trim().replace(/\/+$/, "");
const port = Number(process.env.VT_E2E_PORT ?? 8911);
if (!process.env.VT_E2E_KEY && !external) {
  // Set once in the runner; workers inherit it, so every process uses one key.
  process.env.VT_E2E_KEY = `e2e-${randomBytes(24).toString("hex")}`;
}
const output = path.resolve(process.env.VT_E2E_OUTPUT ?? path.join(here, "test-results"));
process.env.VT_E2E_OUTPUT_DIR = output;
const baseURL = external || `http://127.0.0.1:${port}`;
// Loopback never goes through a proxy (the container has HTTP(S)_PROXY set).
process.env.NO_PROXY = [process.env.NO_PROXY, "127.0.0.1", "localhost"].filter(Boolean).join(",");
const channel = (process.env.VT_E2E_CHANNEL ?? "").trim() || undefined;

export default defineConfig({
  testDir: path.join(here, "tests"),
  outputDir: path.join(output, "artifacts"),
  timeout: 90_000,
  expect: { timeout: 15_000 },
  fullyParallel: false,
  workers: Number(process.env.VT_E2E_WORKERS ?? (external ? 1 : 2)),
  retries: 0,
  reporter: [["list"], ["json", { outputFile: path.join(output, "results.json") }]],
  use: {
    baseURL,
    channel,
    trace: "off",
    video: "off",
    screenshot: "off",
    locale: "en-US",
    timezoneId: "America/New_York",
  },
  projects: [
    { name: "w1366", use: { viewport: { width: 1366, height: 900 } } },
    { name: "w390", use: { viewport: { width: 390, height: 844 } } },
  ],
  webServer: external
    ? undefined
    : {
        command: [
          process.env.VT_E2E_PYTHON ?? "python3",
          JSON.stringify(path.join(here, "fixture_server.py")),
          "--port",
          String(port),
          "--via",
          process.env.VT_E2E_VIA ?? "launch_api",
        ].join(" "),
        url: `${baseURL}/health`,
        timeout: 240_000,
        reuseExistingServer: false,
        stdout: "ignore",
        stderr: "pipe",
        gracefulShutdown: { signal: "SIGTERM", timeout: 15_000 },
        // No explicit env: the server inherits process.env, and the key stays out of results.json.
      },
});
