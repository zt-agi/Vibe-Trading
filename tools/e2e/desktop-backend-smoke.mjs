// ZT add-on: desktop parity smoke test for the private backend.
//
//   node tools/e2e/desktop-backend-smoke.mjs
//
// Starts the desktop shell's own BackendManager (desktop/electron/dist, built with
// `npm run build` in desktop/electron) exactly as Electron's main process does:
// VIBE_TRADING_EXECUTABLE is spawned by the watchdog with
// `serve --host 127.0.0.1 --port <free>` and a fresh per-launch API key. With
// VIBE_TRADING_EXECUTABLE = vt-zt (tools\vt_zt_launcher) the private backend must
// serve the SPA and the /zt routes, accept the shell's key as the Bearer header the
// shell injects (the report frame included), mint the single-use ticket the
// "Open in browser" button hands the system browser, and shut down cleanly.
// Needs VIBE_TRADING_EXECUTABLE plus the VT runtime environment (VIBE_TRADING_HOME,
// INVESTMENT_AI_PROJECT_ROOT, ...). Prints a JSON summary; exits 1 on any failure.
import assert from "node:assert/strict";
import { randomBytes } from "node:crypto";
import { mkdtemp } from "node:fs/promises";
import os from "node:os";
import path from "node:path";
import { fileURLToPath } from "node:url";

const here = path.dirname(fileURLToPath(import.meta.url));
const electronRoot = path.resolve(here, "..", "..", "desktop", "electron");
const { BackendManager } = await import(path.join(electronRoot, "dist", "backend-manager.js"));
const { getDesktopMessages } = await import(path.join(electronRoot, "dist", "locales.js"));

assert.ok(process.env.VIBE_TRADING_EXECUTABLE, "set VIBE_TRADING_EXECUTABLE (the vt-zt executable)");
const logDirectory = await mkdtemp(path.join(os.tmpdir(), "vt-desktop-smoke-"));
const apiAuthKey = randomBytes(32).toString("base64url");
const bearer = { Authorization: `Bearer ${apiAuthKey}` };
let unexpectedExit;
const backend = new BackendManager({
  appPath: electronRoot,
  resourcesPath: electronRoot,
  allowSourceDiscovery: false,
  logDirectory,
  apiAuthKey,
  messages: getDesktopMessages("en"),
  onStatus: () => undefined,
  onUnexpectedExit: (message) => {
    unexpectedExit = message;
  },
});

const summary = { executable: process.env.VIBE_TRADING_EXECUTABLE, logDirectory };
let evidence;
try {
  const url = await backend.start();
  summary.backendOrigin = new URL(url).origin;
  const get = (route, headers = bearer) => fetch(new URL(route, url), { headers });

  assert.equal((await get("zt/preflight", {})).status, 401, "/zt/preflight must require the key");
  const preflight = await get("zt/preflight");
  assert.equal(preflight.status, 200, "/zt/preflight with the shell's key");
  const report = await preflight.json();
  const routes = report.checks.find((check) => check.id === "routes.extensions");
  assert.equal(routes?.status, "OK", `extension routes: ${routes?.summary}`);
  summary.preflight = { overall: report.overall, counts: report.counts, extensionRoutes: routes.summary };

  const spa = await get("zt", { ...bearer, Accept: "text/html" });
  assert.equal(spa.status, 200);
  assert.match(await spa.text(), /<div id="root">/, "the SPA is served at /zt");

  const list = await (await get("zt/reports")).json();
  const viewable = list.data.reports.find((item) => item.viewable);
  assert.ok(viewable, "at least one viewable report");
  const reportPath = `zt/reports/${viewable.id.split("/").map(encodeURIComponent).join("/")}`;
  const framed = await get(reportPath, { ...bearer, Accept: "text/html" });
  assert.equal(framed.status, 200, "the report frame loads with the injected header");
  assert.match(framed.headers.get("content-security-policy") ?? "", /sandbox allow-scripts/);

  const minted = await fetch(new URL("auth/sse-ticket", url), { method: "POST", headers: bearer });
  assert.equal(minted.status, 200);
  const { ticket } = await minted.json();
  const withTicket = `${reportPath}?ticket=${encodeURIComponent(ticket)}`;
  assert.equal((await get(withTicket, {})).status, 200, "the system browser opens the report with the ticket");
  assert.equal((await get(withTicket, {})).status, 401, "the ticket is single-use");
  summary.report = viewable.id;
  assert.equal(unexpectedExit, undefined, unexpectedExit);
} finally {
  evidence = await backend.stop();
}
assert.equal(evidence.backendExited && evidence.watchdogExited && evidence.listenerClosed, true,
  `shutdown evidence ${JSON.stringify(evidence)}`);
summary.shutdown = evidence;
console.log(JSON.stringify(summary, null, 2));
