// ZT add-on: acceptance on a real Electron window, never an emulated desktop header.
// Dot-source the PC1 VT environment and run with a finite execution_guard timeout.
// Required: VT_DESKTOP_APP_DIR, VT_DESKTOP_PLAYWRIGHT, VT_DESKTOP_OUTPUT (all runtime paths on E:).
// Optional: VT_DESKTOP_PROMPT_SMOKE=1 permits one tool-free Ollama exchange in the isolated QA home.
import assert from "node:assert/strict";
import { createRequire } from "node:module";
import { mkdir, readFile, copyFile, writeFile } from "node:fs/promises";
import path from "node:path";
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
const profile = path.join(output, "userData");
const qaHome = path.join(output, "qa-home");
const screenshots = path.join(output, "screenshots");
await mkdir(screenshots, { recursive: true });
await mkdir(profile, { recursive: true });
await mkdir(path.join(qaHome, "live"), { recursive: true });
// Preserve the configured provider/MCP settings; import no existing sessions or user chat.
for (const file of [".env", "agent.json", "pit_audit_receipt.json", "pit_index_receipt.json"]) {
  try { await copyFile(path.join(originalHome, file), path.join(qaHome, file)); }
  catch (error) { if (error.code !== "ENOENT") throw error; }
}
await writeFile(path.join(qaHome, "live", "HALT"), "Desktop QA: live trading remains halted.\n");
const envText = await readFile(path.join(qaHome, ".env"), "utf8");
const setting = name => envText.match(new RegExp(`^${name}=(.*)$`, "m"))?.[1]?.trim().replace(/^['"]|['"]$/g, "");
const secrets = [...envText.matchAll(/^([A-Z_]*(?:KEY|TOKEN|SECRET|PASSWORD)[A-Z_]*)=(.+)$/gm)]
  .map(match => match[2].trim()).filter(Boolean);
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
    const env = { ...process.env, VIBE_TRADING_HOME: qaHome,
      VIBE_TRADING_DESKTOP_TEST_USER_DATA: profile, VIBE_TRADING_DESKTOP_LOCALE: "en" };
    delete env.ELECTRON_RUN_AS_NODE;
    app = await electron.launch({ executablePath: path.join(appDir, "node_modules", "electron", "dist", "electron.exe"),
      args: ["--lang=en-US", appDir], cwd: appDir, env, timeout: 60000 });
    page = await app.firstWindow({ timeout: 60000 });
    page.setDefaultTimeout(15000);
    page.on("pageerror", error => receipt.page_errors.push(redact(error.message)));
    page.on("console", message => {
      if (message.type() === "error") receipt.console_errors.push(redact(message.text()));
    });
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
    assert.match(counts, /\d+ OK.*\d+ WARN.*0 FAIL/);
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
    await screenshot("report-frame");
    await page.keyboard.press("Control+r");
    await page.getByRole("region", { name: "Report viewer", exact: true }).locator("iframe").waitFor({ timeout: 120000 });
    await page.getByRole("region", { name: "Report viewer", exact: true }).getByRole("button", { name: "Close", exact: true }).click();
    assert.equal(await page.getByRole("region", { name: "Report viewer", exact: true }).count(), 0);
    return { report: name, control_count: controlCount, detail_control_exercised: exercisedDetail, reload_deep_link: true, closed: true };
  });
  await check("ordinary-back-forward-navigation", async () => {
    await page.locator('a[href="/"]').first().click();
    await page.keyboard.press("Alt+ArrowLeft");
    await page.waitForURL(/\/zt(?:\?|$)/, { timeout: 5000 });
    await page.keyboard.press("Alt+ArrowRight");
    await page.waitForURL(url => url.pathname === "/", { timeout: 5000 });
    return { back: "/zt", forward: "/" };
  });
  if (process.env.VT_DESKTOP_PROMPT_SMOKE === "1") {
    await check("real-ollama-prompt-through-desktop", async () => {
      assert.equal(receipt.provider, "ollama", "safe prompt smoke requires existing Ollama configuration");
      await page.locator('a[href="/"]').first().click();
      const input = page.locator("textarea").first();
      await input.fill("Desktop QA only. Do not call any tools, read or write any files, make predictions, or place orders. Reply with exactly: VT_DESKTOP_QA_OK");
      await input.press("Enter");
      await poll(async () => {
        const text = await page.locator("main").innerText();
        return text.split("VT_DESKTOP_QA_OK").length >= 3;
      }, 120000);
      await screenshot("ollama-response");
      return { marker: "VT_DESKTOP_QA_OK", provider: receipt.provider, model: receipt.model,
        session_location: qaHome, tools_requested: false };
    });
  } else receipt.rows.push({ id: "real-ollama-prompt-through-desktop", status: "UNRUN", reason: "Set VT_DESKTOP_PROMPT_SMOKE=1 to authorize isolated QA output." });

  await check("desktop-renderer-bridge-backend-recovery", async () => {
    const oldOrigin = await rememberOrigin();
    await page.evaluate(() => window.vibeDesktop.restartBackend());
    await page.waitForURL(/^http:\/\/127\.0\.0\.1:\d+\//, { timeout: 90000 });
    await page.locator("textarea").first().waitFor({ timeout: 30000 });
    const newOrigin = await rememberOrigin();
    assert.notEqual(oldOrigin, newOrigin, "restart must own a new private listener");
    await poll(async () => !(await portOpen(oldOrigin)));
    await screenshot("recovered-first-content");
    return { oldOrigin, newOrigin, old_listener_closed: true, bridge_restart: true };
  });
} catch (error) {
  receipt.fatal = redact(error.message);
} finally {
  if (app) await check("close-window-and-owned-backend-cleanup", async () => {
    await app.evaluate(({ BrowserWindow }) => BrowserWindow.getAllWindows()[0]?.close());
    await poll(async () => !alive(mainPid), 30000);
    for (const origin of receipt.backend_origins) await poll(async () => !(await portOpen(origin)), 10000);
    let backendPids = [];
    if (logsPath) {
      const day = new Date().toISOString().slice(0, 10).replaceAll("-", "");
      const log = await readFile(path.join(logsPath, `desktop-${day}.log`), "utf8");
      backendPids = [...log.matchAll(/backend started pid=(\d+)/g)].map(match => Number(match[1]));
      for (const pid of backendPids) assert.equal(alive(pid), false, `owned backend PID ${pid} remains alive`);
      assert.ok(!log.includes("incomplete evidence"), "desktop reported incomplete shutdown");
      assert.ok(!secrets.some(secret => log.includes(secret)), "secret found in desktop log");
    }
    return { mainPid, main_exited: true, backendPids, listeners_closed: receipt.backend_origins.length };
  });
  if (app && mainPid && alive(mainPid)) await app.close().catch(() => undefined);
  receipt.finished_at = new Date().toISOString();
  receipt.rows.push({ id: "clean-renderer-console", status: receipt.page_errors.length || receipt.console_errors.length ? "FAIL" : "PASS",
    evidence: { page_errors: receipt.page_errors.length, console_errors: receipt.console_errors.length } });
  receipt.status = receipt.fatal || receipt.rows.some(row => row.status === "FAIL")
    ? "NOT_ACCEPTANCE_TESTED_FAILURES" : "CORE_DESKTOP_PATH_PASSED_WITH_DECLARED_UNRUN_ROWS";
  receipt.counts = Object.fromEntries(["PASS", "FAIL", "UNRUN"].map(status => [status, receipt.rows.filter(row => row.status === status).length]));
  await save();
  console.log(JSON.stringify({ status: receipt.status, counts: receipt.counts, results: path.join(output, "results.json") }));
}
process.exitCode = receipt.rows.some(row => row.status === "FAIL") || receipt.fatal ? 1 : 0;
