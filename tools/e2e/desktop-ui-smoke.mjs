// ZT add-on: acceptance on a real Electron window, never an emulated desktop header.
// Dot-source the PC1 VT environment and run with a finite execution_guard timeout.
// Required: VT_DESKTOP_APP_DIR, VT_DESKTOP_PLAYWRIGHT, VT_DESKTOP_OUTPUT (all runtime paths on E:).
// Optional: VT_DESKTOP_PROMPT_SMOKE=1 permits one tool-free Ollama exchange in the isolated QA home.
import assert from "node:assert/strict";
import { createRequire } from "node:module";
import { mkdir, mkdtemp, readFile, copyFile, writeFile } from "node:fs/promises";
import path from "node:path";
import { randomBytes } from "node:crypto";
import { fileURLToPath } from "node:url";
import { connect } from "node:net";

function onE(value, label) {
  assert.ok(value && /^E:[\\/]/i.test(path.resolve(value)), `${label} must be on E:`);
  return path.resolve(value);
}
const appDir = onE(process.env.VT_DESKTOP_APP_DIR, "Electron app");
const output = onE(process.env.VT_DESKTOP_OUTPUT, "acceptance output");
const dependency = onE(process.env.VT_DESKTOP_PLAYWRIGHT, "Playwright dependency");
const originalHome = onE(process.env.VIBE_TRADING_HOME, "VT home");
const executable = onE(process.env.VIBE_TRADING_EXECUTABLE, "backend executable");
const require = createRequire(dependency);
const { _electron: electron } = require(dependency);
await mkdir(output, { recursive: true });
const runRoot = await mkdtemp(path.join(output, "run-"));
let profile = path.join(runRoot, "userData");
let qaHome = path.join(runRoot, "qa-home");
const screenshots = path.join(runRoot, "screenshots");
const envText = await readFile(path.join(originalHome, ".env"), "utf8");
const unquote = value => value.trim().replace(/^(['"])(.*)\1$/, "$2");
const settings = Object.fromEntries([...envText.matchAll(/^([A-Z][A-Z0-9_]*)=(.*)$/gm)].map(m => [m[1], unquote(m[2])]));
const setting = name => settings[name];
const secrets = [...envText.matchAll(/^([A-Z_]*(?:KEY|TOKEN|SECRET|PASSWORD)[A-Z_]*)=(.+)$/gm)]
  .flatMap(match => [match[2].trim(), unquote(match[2])]).filter(Boolean);
const safeSettings = ["LANGCHAIN_PROVIDER", "LANGCHAIN_MODEL_NAME", "OLLAMA_BASE_URL", "TOKEN_THRESHOLD", "TIMEOUT_SECONDS"];
assert.equal(setting("LANGCHAIN_PROVIDER"), "ollama", "QA imports only local Ollama configuration");
assert.match(setting("OLLAMA_BASE_URL") || "http://127.0.0.1:11434", /^http:\/\/(127\.0\.0\.1|localhost)(:\d+)?\/?$/, "Ollama must be local");
async function prepareHome(home, data, mcp = true) {
  await mkdir(data, { recursive: true });
  await mkdir(path.join(home, "live"), { recursive: true });
  await writeFile(path.join(home, "live", "HALT"), "Desktop QA: live trading remains halted.\n");
  await writeFile(path.join(home, ".env"), safeSettings.filter(k => settings[k]).map(k => `${k}=${settings[k]}`).concat([
    `API_AUTH_KEY=${randomBytes(32).toString("hex")}`, "VIBE_ORDER_APPROVAL=required", "VIBE_TRADING_ENABLE_SHELL_TOOLS=false", "VIBE_TRADING_ENABLE_SCHEDULER=false", "VIBE_TRADING_CHANNELS_AUTO_START=false", ""]).join("\n"));
  // Only this project's read-only dashboard MCP registration is retained for preflight.
  const source = JSON.parse(await readFile(path.join(originalHome, "agent.json"), "utf8"));
  const dashboard = source.mcpServers?.["zt-dashboards"];
  const config = { mcpServers: mcp && dashboard ? { "zt-dashboards": { command: dashboard.command, args: dashboard.args,
    env: { INVESTMENT_AI_PROJECT_ROOT: process.env.INVESTMENT_AI_PROJECT_ROOT },
    enabledTools: ["daily_snapshot", "project_reports", "pit_inventory"] } } : {} };
  await writeFile(path.join(home, "agent.json"), JSON.stringify(config));
  if (mcp) for (const file of ["pit_audit_receipt.json", "pit_index_receipt.json"]) {
    try { await copyFile(path.join(originalHome, file), path.join(home, file)); }
    catch (error) { if (error.code !== "ENOENT") throw error; }
  }
}
await mkdir(screenshots, { recursive: true });
await prepareHome(qaHome, profile);
function launchEnv(home, data, extra = {}) {
  const allowed = ["SystemRoot", "WINDIR", "COMSPEC", "PATH", "PATHEXT", "PROCESSOR_ARCHITECTURE", "NUMBER_OF_PROCESSORS", "PYTHONPATH", "PYTHONUTF8", "PYTHONUNBUFFERED", "PYTHONPYCACHEPREFIX", "INVESTMENT_AI_PROJECT_ROOT", "PITDB_INDEX", "VIBE_TRADING_EXECUTABLE", "VIBE_TRADING_PLAYBOOK_DIR", "WEASYPRINT_DLL_DIRECTORIES", "OPENBLAS_NUM_THREADS", "OMP_NUM_THREADS", "MKL_NUM_THREADS"];
  return { ...Object.fromEntries(allowed.filter(k => process.env[k]).map(k => [k, process.env[k]])),
    HOME: home, USERPROFILE: home, APPDATA: data, LOCALAPPDATA: data,
    TEMP: runRoot, TMP: runRoot, TMPDIR: runRoot, NO_PROXY: "127.0.0.1,localhost",
    VIBE_TRADING_HOME: home, VIBE_TRADING_DESKTOP_TEST_USER_DATA: data, VIBE_TRADING_DESKTOP_LOCALE: "en",
    VIBE_TRADING_ENABLE_SHELL_TOOLS: "false", VIBE_TRADING_ENABLE_SCHEDULER: "false", VIBE_TRADING_CHANNELS_AUTO_START: "false", ...extra };
}
function redact(value) {
  let text = String(value);
  for (const secret of secrets) text = text.split(secret).join("<redacted>");
  return text.replace(/([?&#](?:ticket|vt_key)=)[^&#\s"']+/gi, "$1<redacted>")
    .replace(/Bearer\s+[A-Za-z0-9._~-]+/gi, "Bearer <redacted>");
}
const receipt = {
  schema: "vt-desktop-ui-acceptance/1", started_at: new Date().toISOString(),
  source_commit: process.env.VT_DESKTOP_SOURCE_COMMIT ?? "unverified",
  surface: "actual Electron BrowserWindow and preload bridge", appDir, output,
  isolated_home: qaHome, isolated_profile: profile,
  provider: setting("LANGCHAIN_PROVIDER"), model: setting("LANGCHAIN_MODEL_NAME"),
  rows: [], page_errors: [], console_errors: [], backend_origins: [], screenshots: [],
  limitations: ["OS default-browser opening is not exercised by this harness.",
    "Native minimum width is 960 px; widths below that are unsupported by the desktop window."],
};
async function save() {
  await writeFile(path.join(output, "results.json"), JSON.stringify(receipt, null, 2) + "\n");
}
async function check(id, action) {
  const started = Date.now();
  try {
    const evidence = await action();
    receipt.rows.push({ id, status: "PASS", milliseconds: Date.now() - started, evidence });
  } catch (error) {
    receipt.rows.push({ id, status: "FAIL", milliseconds: Date.now() - started, error: redact(error.message) });
  }
  await save();
  console.log(JSON.stringify({ id, status: receipt.rows.at(-1).status, milliseconds: Date.now() - started }));
  return receipt.rows.at(-1).status === "PASS";
}
const sleep = ms => new Promise(resolve => setTimeout(resolve, ms));
async function poll(action, timeout = 20000) {
  const end = Date.now() + timeout;
  while (Date.now() < end) { if (await action()) return; await sleep(150); }
  throw new Error(`Condition did not settle within ${timeout} ms`);
}
async function portOpen(origin) {
  return new Promise(resolve => {
    const socket = connect({ host: "127.0.0.1", port: Number(new URL(origin).port) });
    const done = value => { socket.destroy(); resolve(value); };
    socket.setTimeout(500, () => done(false));
    socket.once("connect", () => done(true));
    socket.once("error", () => done(false));
  });
}
function alive(pid) { try { process.kill(pid, 0); return true; } catch { return false; } }
let app, page, mainPid, logsPath;
let stopEvidencePath;
let keyEvidencePath;
let lifecyclePhase = "active";
receipt.expected_disconnects = [];
const resourceFailures = [];
function expectedDisconnect(url, error, kind) {
  if (!["cleanup", "restart"].includes(lifecyclePhase) || !String(error).includes("ERR_CONNECTION_RESET")) return false;
  let parsed; try { parsed = new URL(url); } catch { return false; }
  if (parsed.hostname !== "127.0.0.1" || !receipt.backend_origins.includes(parsed.origin)
      || !/^\/sessions\/[^/]+\/events$/.test(parsed.pathname) || parsed.searchParams.get("replay") !== "active") return false;
  receipt.expected_disconnects.push({ at: new Date().toISOString(), phase: lifecyclePhase, kind, url: redact(url), error: redact(error) });
  return true;
}
function watchPage(target) {
  target.on("pageerror", error => receipt.page_errors.push(redact(error.message)));
  target.on("console", message => {
    if (message.type() === "error") {
      const location = message.location();
      if (expectedDisconnect(location.url, message.text(), "console")) return;
      receipt.console_errors.push({ text: redact(message.text()), location: { ...location, url: redact(location.url) }, page_url: redact(target.url()) });
    }
  });
  target.on("response", response => { if (response.status() >= 400) resourceFailures.push({ url: redact(response.url()), status: response.status(), type: response.request().resourceType() }); });
  target.on("requestfailed", request => {
    const error = request.failure()?.errorText || "request failed";
    if (!expectedDisconnect(request.url(), error, "request")) resourceFailures.push({ url: redact(request.url()), error: redact(error), type: request.resourceType(), phase: lifecyclePhase });
  });
}
async function captureLifecycle() {
  lifecyclePhase = "active";
  mainPid = await app.evaluate(() => process.pid);
  receipt.main_pids ??= []; receipt.main_pids.push(mainPid);
  stopEvidencePath = path.join(runRoot, `shutdown-${mainPid}.jsonl`);
  keyEvidencePath = path.join(runRoot, `input-${mainPid}.jsonl`);
  ({ logsPath } = await app.evaluate(async ({ app }, args) => {
    const manager = process.mainModule.require(args.module);
    const fs = process.mainModule.require("node:fs");
    const { BrowserWindow } = process.mainModule.require("electron");
    const watchInput = window => window.webContents.on("before-input-event", (_event, input) => {
      if (input.alt || input.key?.startsWith("Arrow")) fs.appendFileSync(args.inputEvidence, JSON.stringify({ at: new Date().toISOString(), type: input.type, key: input.key, code: input.code, alt: input.alt, control: input.control, meta: input.meta, shift: input.shift }) + "\n");
    });
    for (const window of BrowserWindow.getAllWindows()) watchInput(window);
    app.on("browser-window-created", (_event, window) => watchInput(window));
    const original = manager.BackendManager.prototype.stop;
    manager.BackendManager.prototype.stop = async function (...params) {
      const result = await original.apply(this, params);
      fs.appendFileSync(args.evidence, JSON.stringify(result) + "\n");
      return result;
    };
    return { logsPath: app.getPath("logs") };
  }, { module: path.join(appDir, "dist", "backend-manager.js"), evidence: stopEvidencePath, inputEvidence: keyEvidencePath }));
}
async function cleanup() {
  lifecyclePhase = "cleanup";
  assert.ok(app && Number.isInteger(mainPid), "launched main PID must be captured");
  try { await app.evaluate(({ BrowserWindow }) => BrowserWindow.getAllWindows()[0]?.close()); } catch {}
  await poll(async () => !alive(mainPid), 30000);
  const records = (await readFile(stopEvidencePath, "utf8")).trim().split("\n").filter(Boolean).map(JSON.parse);
  assert.ok(records.length > 0, "native BackendManager.stop evidence is required");
  for (const evidence of records) {
    assert.ok(evidence.backendPid && evidence.watchdogPid, "positive backend and watchdog identity required");
    assert.equal(evidence.backendExited, true); assert.equal(evidence.watchdogExited, true); assert.equal(evidence.listenerClosed, true);
    assert.equal(alive(evidence.backendPid), false); assert.equal(alive(evidence.watchdogPid), false);
  }
  for (const origin of receipt.backend_origins) await poll(async () => !(await portOpen(origin)), 10000);
  const day = new Date().toISOString().slice(0, 10).replaceAll("-", "");
  const log = await readFile(path.join(logsPath, `desktop-${day}.log`), "utf8");
  assert.ok(!log.includes("incomplete evidence"));
  assert.ok(!secrets.some(secret => log.includes(secret)), "secret found in desktop log");
  return { mainPid, main_exited: true, native_shutdown: records, listeners_closed: receipt.backend_origins.length };
}
async function screenshot(name) {
  const filename = path.join(screenshots, `${name}.png`);
  await page.screenshot({ path: filename });
  receipt.screenshots.push(filename);
}
async function rememberOrigin() {
  const origin = new URL(page.url()).origin;
  if (!receipt.backend_origins.includes(origin)) receipt.backend_origins.push(origin);
  return origin;
}
async function scrollState() {
  return page.locator("main").evaluate(el => ({ top: el.scrollTop, height: el.scrollHeight, viewport: el.clientHeight }));
}
async function traverse(method, direction) {
  const before = await scrollState();
  if (before.height <= before.viewport + 1) return { not_applicable: "pane fits viewport", before };
  await page.locator("main").click({ position: { x: 12, y: 20 } });
  const bounds = await page.locator("main").boundingBox();
  await page.mouse.move(bounds.x + bounds.width - 15, bounds.y + bounds.height / 2);
  for (let i = 0; i < 40; i++) {
    if (method === "wheel") await page.mouse.wheel(0, direction * 650);
    else await page.keyboard.press(direction > 0 ? "PageDown" : "PageUp");
    await sleep(50);
    const state = await scrollState();
    if (direction > 0 ? state.top + state.viewport >= state.height - 3 : state.top <= 3) break;
  }
  const after = await scrollState();
  assert.ok(direction > 0 ? after.top + after.viewport >= after.height - 3 : after.top <= 3,
    `${method} did not reach ${direction > 0 ? "final content" : "first content"}`);
  assert.ok(direction > 0 ? after.top > before.top : after.top < before.top,
    `${method} caused no directional scroll-position change`);
  return { before, after };
}

await save();
try {
  const launched = await check("native-launch-and-first-content", async () => {
    const env = launchEnv(qaHome, profile);
    delete env.ELECTRON_RUN_AS_NODE;
    app = await electron.launch({ executablePath: path.join(appDir, "node_modules", "electron", "dist", "electron.exe"),
      args: ["--lang=en-US", appDir], cwd: appDir, env, timeout: 60000 });
    await captureLifecycle();
    page = await app.firstWindow({ timeout: 60000 });
    page.setDefaultTimeout(15000);
    watchPage(page);
    await page.waitForURL(/^http:\/\/127\.0\.0\.1:\d+\//, { timeout: 90000 });
    await page.locator("#root > *").first().waitFor();
    await page.locator("textarea").first().waitFor({ state: "visible", timeout: 30000 });
    ({ mainPid, logsPath } = await app.evaluate(({ app }) => ({ mainPid: process.pid, logsPath: app.getPath("logs") })));
    assert.ok(/^E:[\\/]/i.test(logsPath), "desktop log directory must remain on E:");
    const state = await page.evaluate(() => ({ language: navigator.language,
      desktop: window.vibeDesktop?.isDesktop, mainText: document.querySelector("main")?.innerText.slice(0, 250) }));
    assert.equal(state.desktop, true, "actual preload bridge must be present");
    assert.ok(state.language.startsWith("en"), "renderer language must be English");
    assert.ok(!/[\u3400-\u9fff]/u.test(state.mainText ?? ""), "first content must be English");
    await rememberOrigin(); await screenshot("first-content");
    return { mainPid, logsPath, language: state.language, bridge: state.desktop, first_content: state.mainText };
  });
  if (!launched) throw new Error("Actual Electron launch failed; dependent checks were not run");

  await check("navigation-to-zt-and-preflight", async () => {
    await page.locator('a[href="/zt"]').first().click();
    await page.getByRole("heading", { name: "ZT research dashboards", exact: true }).waitFor();
    await page.getByTestId("zt-preflight-counts").waitFor({ timeout: 120000 });
    const counts = await page.getByTestId("zt-preflight-counts").innerText();
    const failureCount = Number(counts.match(/(?:^|\s)(\d+) FAIL(?:\s|$)/)?.[1]);
    assert.equal(failureCount, 0, `preflight failures: ${counts}`);
    await screenshot("zt-preflight");
    return { counts, path: new URL(page.url()).pathname };
  });
  await check("preflight-expand-collapse-and-detail", async () => {
    const toggle = page.getByRole("button", { name: "Show all checks", exact: true });
    await toggle.click();
    assert.equal(await page.getByRole("button", { name: "Hide OK checks", exact: true }).getAttribute("aria-expanded"), "true");
    const rowCount = await page.getByTestId("zt-preflight-check").count();
    assert.ok(rowCount > 5);
    const detail = page.getByTestId("zt-preflight-check").locator("summary").first();
    await detail.click();
    assert.equal(await detail.locator("..").getAttribute("open"), "");
    await detail.press("Enter");
    assert.equal(await detail.locator("..").getAttribute("open"), null);
    await page.getByRole("button", { name: "Hide OK checks", exact: true }).focus();
    await page.keyboard.press("Space");
    assert.equal(await page.getByRole("button", { name: "Show all checks", exact: true }).getAttribute("aria-expanded"), "false");
    return { row_count: rowCount, detail_mouse_and_keyboard: true, toggle_keyboard: true };
  });
  await check("snapshot-select-and-refresh", async () => {
    const select = page.locator("main select").first();
    await select.selectOption("latest");
    assert.equal(await select.inputValue(), "latest");
    await select.selectOption("today");
    await page.getByRole("button", { name: "Refresh", exact: true }).click();
    await poll(async () => !(await page.getByRole("button", { name: "Refresh", exact: true }).isDisabled()), 120000);
    return { selected: await select.inputValue(), refreshed: true };
  });

  for (const width of [1280, 1024, 960]) {
    await check(`native-width-${width}-scroll-and-overflow`, async () => {
      await app.evaluate(({ BrowserWindow }, width) => BrowserWindow.getAllWindows()[0].setSize(width, 820), width);
      await sleep(250);
      const dimensions = await page.evaluate(() => ({ width: innerWidth,
        document: { client: document.documentElement.clientWidth, scroll: document.documentElement.scrollWidth },
        main: { client: document.querySelector("main").clientWidth, scroll: document.querySelector("main").scrollWidth } }));
      assert.ok(dimensions.document.scroll <= dimensions.document.client + 1, "document horizontal overflow");
      assert.ok(dimensions.main.scroll <= dimensions.main.client + 1, "main pane horizontal overflow");
      const wheelDown = await traverse("wheel", 1); const wheelUp = await traverse("wheel", -1);
      const pageDown = await traverse("keyboard", 1); const pageUp = await traverse("keyboard", -1);
      await screenshot(`zt-width-${width}`);
      return { dimensions, wheelDown, wheelUp, pageDown, pageUp };
    });
  }

  await check("real-report-frame-and-desktop-controls", async () => {
    const buttons = page.getByRole("button", { name: /^Open / });
    const preferred = page.getByRole("button", { name: /Open .*Alpha Monitor/i });
    const button = await preferred.count() ? preferred.first() : buttons.first();
    const name = await button.getAttribute("aria-label");
    await button.click();
    const viewer = page.getByRole("region", { name: "Report viewer", exact: true });
    await viewer.waitFor();
    const iframe = viewer.locator("iframe"); await iframe.waitFor();
    receipt.report_reveal_evidence = {};
    await poll(async () => {
      const box = await viewer.boundingBox();
      const viewport = await page.evaluate(() => ({ height: innerHeight, scroll: document.querySelector("main")?.scrollTop, windowScroll: scrollY }));
      receipt.report_reveal_evidence.initial = { box, viewport };
      return box && box.y >= -1 && box.y + 44 < viewport.height;
    });
    const initialReveal = { viewer: await viewer.boundingBox(), main: await page.evaluate(() => ({ url: location.href, scroll: document.querySelector("main")?.scrollTop, windowScroll: scrollY })) };
    const frame = await iframe.elementHandle().then(handle => handle.contentFrame());
    assert.ok(frame, "real report frame must exist");
    await frame.locator("body").waitFor();
    await poll(async () => (await frame.locator("body").innerText()).trim().length > 100, 30000);
    assert.ok(!/Unauthorized|API key required/.test((await frame.locator("body").innerText()).slice(0, 150)), "report authentication failed");
    await viewer.getByRole("button", { name: "Open in browser", exact: true }).waitFor();
    const controlCount = await frame.locator("button,input,select,summary").count();
    const summary = frame.locator("summary").first();
    const exercisedDetail = Boolean(await summary.count());
    if (exercisedDetail) { await summary.click(); await summary.press("Enter"); }
    const afterFrameActions = { viewer: await viewer.boundingBox(), main: await page.evaluate(() => ({ scroll: document.querySelector("main")?.scrollTop, windowScroll: scrollY })) };
    await viewer.scrollIntoViewIfNeeded();
    await iframe.screenshot({ path: path.join(screenshots, "report-frame-content.png") });
    receipt.screenshots.push(path.join(screenshots, "report-frame-content.png"));
    await screenshot("report-frame");
    await page.keyboard.press("Control+r");
    await page.getByRole("region", { name: "Report viewer", exact: true }).locator("iframe").waitFor({ timeout: 120000 });
    await page.getByRole("region", { name: "Report viewer", exact: true }).getByRole("button", { name: "Close", exact: true }).click();
    assert.equal(await page.getByRole("region", { name: "Report viewer", exact: true }).count(), 0);
    return { report: name, control_count: controlCount, detail_control_exercised: exercisedDetail, initialReveal, afterFrameActions, reload_deep_link: true, closed: true };
  });
  await check("ordinary-back-forward-navigation", async () => {
    await page.locator('a[href="/"]').first().click();
    const snapshot = async () => JSON.parse(redact(JSON.stringify({
      renderer: await page.evaluate(() => ({ url: location.href, active: document.activeElement?.tagName })),
      main: await app.evaluate(({ BrowserWindow }) => {
        const contents = BrowserWindow.getAllWindows()[0].webContents;
        return { url: contents.getURL(), entries: contents.navigationHistory.getAllEntries().map(({ url, title }) => ({ url, title })), index: contents.navigationHistory.getActiveIndex() };
      }),
    })));
    const nativeArrow = async keyCode => app.evaluate(({ BrowserWindow }, keyCode) => {
      const window = BrowserWindow.getAllWindows()[0];
      window.focus(); window.webContents.focus();
      window.webContents.sendInputEvent({ type: "keyDown", keyCode, modifiers: ["alt"] });
      window.webContents.sendInputEvent({ type: "keyUp", keyCode, modifiers: ["alt"] });
    }, keyCode);
    const before = await snapshot();
    const historyEvidence = { before, input_method: "Electron webContents.sendInputEvent with Alt and arrow keys" };
    receipt.navigation_evidence = historyEvidence;
    try {
    await nativeArrow("Left");
    historyEvidence.afterBackInput = await snapshot();
    await poll(async () => new URL(page.url()).pathname === "/zt", 15000);
    await page.getByRole("heading", { name: "ZT research dashboards", exact: true }).waitFor({ timeout: 15000 });
    const afterBack = await snapshot();
    await nativeArrow("Right");
    historyEvidence.afterForwardInput = await snapshot();
    await poll(async () => new URL(page.url()).pathname === "/", 15000);
    await page.locator("textarea").first().waitFor({ timeout: 15000 });
    const afterForward = await snapshot();
    const input = await readFile(keyEvidencePath, "utf8").catch(() => "");
    const arrows = input.trim().split("\n").filter(Boolean).map(JSON.parse);
    assert.ok(arrows.some(row => row.alt && row.type === "keyDown" && row.key === "ArrowLeft"));
    assert.ok(arrows.some(row => row.alt && row.type === "keyDown" && row.key === "ArrowRight"));
    return { back: "/zt", forward: "/", before, afterBack, afterForward, arrow_inputs: arrows };
    } finally {
      historyEvidence.final = await snapshot();
      const input = await readFile(keyEvidencePath, "utf8").catch(() => "");
      historyEvidence.arrow_inputs = input.trim().split("\n").filter(Boolean).map(JSON.parse);
    }
  });
  await check("desktop-renderer-bridge-backend-recovery", async () => {
    const oldOrigin = await rememberOrigin();
    lifecyclePhase = "restart";
    await page.evaluate(() => { void window.vibeDesktop.restartBackend(); });
    await page.waitForURL(url => url.hostname === "127.0.0.1" && url.origin !== oldOrigin, { timeout: 120000, waitUntil: "domcontentloaded" });
    await page.locator("textarea").first().waitFor({ timeout: 60000 });
    lifecyclePhase = "active";
    const newOrigin = await rememberOrigin();
    assert.notEqual(oldOrigin, newOrigin, "restart must own a new private listener");
    await poll(async () => !(await portOpen(oldOrigin)));
    await screenshot("recovered-first-content");
    return { oldOrigin, newOrigin, old_listener_closed: true, bridge_restart: true };
  });
  if (process.env.VT_DESKTOP_PROMPT_SMOKE === "1") {
    await check("real-ollama-prompt-through-desktop", async () => {
      assert.ok(await check("preflight-phase-cleanup", cleanup), "prior owned desktop must close before model phase");
      await app.close().catch(() => undefined); app = undefined;
      qaHome = path.join(runRoot, "no-tools-home"); profile = path.join(runRoot, "no-tools-profile");
      await prepareHome(qaHome, profile, false);
      const seamDir = path.join(runRoot, "no-tools-seam"); await mkdir(seamDir);
      await copyFile(path.join(path.dirname(fileURLToPath(import.meta.url)), "no_tools_sitecustomize.py"), path.join(seamDir, "sitecustomize.py"));
      const toolEvidence = path.join(runRoot, "no-tools-evidence.jsonl");
      app = await electron.launch({ executablePath: path.join(appDir, "node_modules", "electron", "dist", "electron.exe"),
        args: ["--lang=en-US", appDir], cwd: appDir, env: launchEnv(qaHome, profile, {
          PYTHONPATH: [seamDir, "E:\\codex-runtime\\investment-ai\\vibe-trading\\agent", "E:\\codex-runtime\\investment-ai\\vibe-trading\\launcher", process.env.PYTHONPATH].filter(Boolean).join(path.delimiter), VT_QA_NO_TOOLS_EVIDENCE: toolEvidence }), timeout: 60000 });
      await captureLifecycle(); page = await app.firstWindow({ timeout: 60000 });
      watchPage(page);
      await page.waitForURL(/^http:\/\/127\.0\.0\.1:\d+\//, { timeout: 90000 });
      await page.locator("textarea").first().waitFor({ state: "visible", timeout: 30000 });
      await rememberOrigin();
      const boot = (await readFile(toolEvidence, "utf8")).trim().split("\n").map(JSON.parse);
      assert.ok(boot.some(r => r.phase === "installed" && r.mcp_servers === 0 && r.shell_enabled === false), "empty registry fixture must install before prompt");
      await page.locator('a[href="/"]').first().click();
      const input = page.locator("textarea").first();
      await input.fill("Desktop QA only. Do not call any tools, read or write any files, make predictions, or place orders. Reply with exactly: VT_DESKTOP_QA_OK");
      const titleResponse = page.waitForResponse(response => /^\/sessions\/[^/]+\/title\/auto$/.test(new URL(response.url()).pathname), { timeout: 90000 });
      titleResponse.catch(() => undefined);
      await input.press("Enter");
      await poll(async () => {
        const text = await page.locator("main").innerText();
        return text.split("VT_DESKTOP_QA_OK").length >= 3;
      }, 120000);
      const proofs = (await readFile(toolEvidence, "utf8")).trim().split("\n").map(JSON.parse);
      assert.ok(proofs.some(r => r.phase === "registry" && r.tools === 0), "registry was never built empty");
      assert.ok(proofs.some(r => r.phase === "definitions" && r.tools === 0), "bound tool definitions were never verified");
      assert.ok(proofs.every(r => r.tools === undefined || r.tools === 0));
      const sessions = await page.evaluate(async () => { const response = await fetch("/sessions"); if (!response.ok) throw new Error(`sessions HTTP ${response.status}`); return response.json(); });
      assert.ok(Array.isArray(sessions) && sessions.length === 1 && sessions[0].session_id, "fresh fixture must contain exactly one real session");
      const messages = await page.evaluate(async sid => { const response = await fetch(`/sessions/${sid}/messages`); if (!response.ok) throw new Error(`messages HTTP ${response.status}`); return response.json(); }, sessions[0].session_id);
      assert.ok(Array.isArray(messages), "real message list required");
      assert.ok(messages.some(message => message.role === "assistant" && message.content.trim() === "VT_DESKTOP_QA_OK"), "completed assistant response required");
      const completedTitle = await titleResponse;
      assert.equal(completedTitle.status(), 200, "automatic title request must finish before desktop cleanup");
      const trails = messages.flatMap(message => message.tool_trail ?? []);
      assert.equal(trails.length, 0, "prompt produced tool events");
      await screenshot("ollama-response");
      return { marker: "VT_DESKTOP_QA_OK", provider: receipt.provider, model: receipt.model,
        session_location: qaHome, tools_available: 0, tool_events: 0, isolation_proofs: proofs };
    });
  } else receipt.rows.push({ id: "real-ollama-prompt-through-desktop", status: "UNRUN", reason: "Set VT_DESKTOP_PROMPT_SMOKE=1 to authorize isolated QA output." });

} catch (error) {
  receipt.fatal = redact(error.message);
} finally {
  if (app) {
    try { await check("close-window-and-owned-backend-cleanup", cleanup); }
    finally { await app.close().catch(() => undefined); }
  }
  receipt.resource_failures = resourceFailures;
  receipt.finished_at = new Date().toISOString();
  receipt.rows.push({ id: "clean-renderer-console", status: receipt.page_errors.length || receipt.console_errors.length || resourceFailures.length ? "FAIL" : "PASS",
    evidence: { page_errors: receipt.page_errors.length, console_errors: receipt.console_errors.length, unexpected_resource_failures: resourceFailures.length } });
  receipt.status = receipt.fatal || receipt.rows.some(row => row.status === "FAIL")
    ? "NOT_ACCEPTANCE_TESTED_FAILURES" : "CORE_DESKTOP_PATH_PASSED_WITH_DECLARED_UNRUN_ROWS";
  receipt.counts = Object.fromEntries(["PASS", "FAIL", "UNRUN"].map(status => [status, receipt.rows.filter(row => row.status === status).length]));
  await save();
  console.log(JSON.stringify({ status: receipt.status, counts: receipt.counts, results: path.join(output, "results.json") }));
}
process.exitCode = receipt.rows.some(row => row.status === "FAIL") || receipt.fatal ? 1 : 0;
