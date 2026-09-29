// ZT add-on: UI QA gate. Every main route opens through the one-click sign-in
// (#vt_key), renders without uncaught page errors or console errors, and has
// no horizontal overflow at every supported width (390, 500, 800, 1024, 1366, 1680). /zt is checked
// in depth: pre-flight panel, report list, one report in the viewer, snapshot
// cards. Screenshots land in $VT_E2E_OUTPUT/screenshots/<project>/.
import { mkdirSync } from "node:fs";
import path from "node:path";
import { expect, test, type Page, type TestInfo } from "@playwright/test";

const KEY = process.env.VT_E2E_KEY ?? "";
const OUTPUT = process.env.VT_E2E_OUTPUT_DIR ?? path.resolve("test-results");
const STORAGE_KEY = "vibe_trading_api_auth_key";

// Main routes (router.tsx). VT has no /runs list page: GET /runs is the JSON API,
// so the runs check opens /runs/<id> of the newest run (skipped when there is none).
const ROUTES: { path: string; name: string }[] = [
  { path: "/", name: "home-agent" },
  { path: "/agent", name: "agent" },
  { path: "/reports", name: "reports" },
  { path: "/scheduled", name: "scheduled" },
  { path: "/portfolio", name: "portfolio" },
  { path: "/settings", name: "settings" },
  { path: "/zt", name: "zt" },
  { path: "/alpha-zoo", name: "alpha-zoo" },
  { path: "/options", name: "options" },
  { path: "/correlation", name: "correlation" },
  { path: "/compare", name: "compare" },
  { path: "/runtime", name: "runtime" },
];

// VT's run page (RunDetail.tsx) asks for the run's optional strategy files with
// api.getRunCode(...).catch(() => ({})): a run without a code folder answers 404,
// which the page handles, but the browser still logs the failed resource load.
// That 404 is VT's contract for such runs (manual analyses, swarm-only runs), not a UI error.
const OPTIONAL_RUN_FILE_404 = /status of 404 \(Not Found\) \([^)]*\/runs\/[^/)]+\/(?:code|pine)\)$/;
function redact(text: string): string {
  if (!KEY) return text;
  return text.split(encodeURIComponent(KEY)).join("<api-key>").split(KEY).join("<api-key>");
}

type Watch = { pageErrors: string[]; consoleErrors: string[]; allConsole: string[] };

function watch(page: Page): Watch {
  const seen: Watch = { pageErrors: [], consoleErrors: [], allConsole: [] };
  page.on("pageerror", (error) => seen.pageErrors.push(redact(`${error.name}: ${error.message}`)));
  page.on("console", (message) => {
    const text = message.text();
    seen.allConsole.push(text);
    if (message.type() !== "error") return;
    const where = message.location().url ? ` (${message.location().url})` : "";
    const line = redact(`${text}${where}`);
    if (OPTIONAL_RUN_FILE_404.test(line)) return;
    seen.consoleErrors.push(line);
  });
  return seen;
}

/** Open a route the way the launchers do: /route#vt_key=<key>. Errors never carry the key. */
async function openSignedIn(page: Page, route: string): Promise<void> {
  expect(KEY.length, "VT_E2E_KEY must be set").toBeGreaterThan(8);
  try {
    await page.goto(`${route}#vt_key=${encodeURIComponent(KEY)}`, { waitUntil: "domcontentloaded" });
  } catch (error) {
    throw new Error(redact(error instanceof Error ? error.message : String(error)));
  }
}

async function settle(page: Page): Promise<void> {
  await page.waitForLoadState("load");
  await page.locator("#root > *").first().waitFor({ state: "attached" });
  // The route-level Suspense fallback reads "Loading…".
  await expect(page.getByText("Loading…", { exact: true })).toHaveCount(0, { timeout: 20_000 }).catch(() => undefined);
  await page.waitForLoadState("networkidle", { timeout: 6_000 }).catch(() => undefined);
  await page.waitForTimeout(800);
}

/**
 * Horizontal overflow of the page as the user scrolls it: the document and VT's
 * <main> scroll pane (the layout scrolls inside <main>, not the document).
 * Inner containers that scroll sideways by design (tables, code) are not counted.
 */
async function horizontalOverflow(page: Page) {
  return page.evaluate(() => {
    const describe = (el: Element) => {
      const id = el.id ? `#${el.id}` : "";
      const cls = typeof el.className === "string" ? el.className.trim().split(/\s+/).slice(0, 4).join(".") : "";
      const text = (el.textContent ?? "").trim().slice(0, 40);
      return `${el.tagName.toLowerCase()}${id}${cls ? `.${cls}` : ""}${text ? ` "${text}"` : ""}`;
    };
    const doc = document.documentElement;
    const panes: { el: Element; label: string }[] = [{ el: doc, label: "document" }];
    for (const main of Array.from(document.querySelectorAll("main"))) panes.push({ el: main, label: "main" });
    let worst = { width: doc.clientWidth, scroll: doc.clientWidth, pane: "document" };
    const offenders: string[] = [];
    for (const { el, label } of panes) {
      const width = el.clientWidth;
      const scroll = el === doc ? Math.max(doc.scrollWidth, document.body ? document.body.scrollWidth : 0) : el.scrollWidth;
      if (scroll - width > worst.scroll - worst.width) worst = { width, scroll, pane: label };
      if (scroll <= width + 1) continue;
      const edge = (el === doc ? 0 : el.getBoundingClientRect().left) + width + 1;
      for (const child of Array.from(el.querySelectorAll("*"))) {
        const rect = child.getBoundingClientRect();
        if (rect.width === 0 || rect.right <= edge) continue;
        const parent = child.parentElement;
        if (parent && parent !== el && parent.getBoundingClientRect().right > edge) continue; // outermost only
        offenders.push(`${label}: ${describe(child)} right=${Math.round(rect.right)} width=${Math.round(rect.width)}`);
        if (offenders.length >= 6) break;
      }
    }
    return { width: worst.width, scroll: worst.scroll, pane: worst.pane, offenders };
  });
}

async function screenshot(page: Page, info: TestInfo, name: string): Promise<void> {
  const folder = path.join(OUTPUT, "screenshots", info.project.name);
  mkdirSync(folder, { recursive: true });
  await page.screenshot({ path: path.join(folder, `${name}.png`), fullPage: true });
}

async function expectClean(page: Page, seen: Watch, label: string, info?: TestInfo): Promise<void> {
  expect.soft(seen.pageErrors, `${label}: uncaught page errors`).toEqual([]);
  expect.soft(seen.consoleErrors, `${label}: console errors`).toEqual([]);
  const overflow = await horizontalOverflow(page);
  const message = `${label}: horizontal overflow of the ${overflow.pane} (${overflow.width}px): ${overflow.offenders.join("; ")}`;
  expect.soft(overflow.scroll, message).toBeLessThanOrEqual(overflow.width + 1);
  expect.soft(page.url(), `${label}: the key left the URL`).not.toContain("vt_key");
  expect(seen.allConsole.some((text) => text.includes(KEY)), `${label}: the key was logged`).toBe(false);
}

async function authed(page: Page, pathname: string): Promise<{ status: number; body: unknown }> {
  return page.evaluate(
    async ({ pathname, storageKey }) => {
      const key = localStorage.getItem(storageKey) ?? "";
      const res = await fetch(pathname, { headers: { Authorization: `Bearer ${key}` } });
      let body: unknown = null;
      try {
        body = await res.json();
      } catch {
        body = null;
      }
      return { status: res.status, body };
    },
    { pathname, storageKey: STORAGE_KEY },
  );
}

test.describe("one-click sign-in", () => {
  test("#vt_key is stored, removed from the URL and history, and never logged", async ({ page }) => {
    const seen = watch(page);
    await openSignedIn(page, "/settings");
    await settle(page);
    const state = await page.evaluate(
      ({ key, storageKey }) => ({
        hash: location.hash,
        href: location.href.includes(key) || location.href.includes(encodeURIComponent(key)),
        stored: localStorage.getItem(storageKey) === key,
      }),
      { key: KEY, storageKey: STORAGE_KEY },
    );
    expect(state).toEqual({ hash: "", href: false, stored: true });
    expect(new URL(page.url()).pathname).toBe("/settings");
    expect((await authed(page, "/zt/preflight")).status).toBe(200);
    await expectClean(page, seen, "/settings");
    // replaceState, not pushState: the entry before is the blank start page, and
    // no history entry still carries the key.
    await page.goBack();
    expect(page.url()).toBe("about:blank");
    await page.goForward();
    expect(page.url()).not.toContain("vt_key");
    expect(new URL(page.url()).pathname).toBe("/settings");
  });
});

test.describe("main routes", () => {
  for (const route of ROUTES) {
    test(`${route.path} renders cleanly`, async ({ page }, info) => {
      const seen = watch(page);
      await openSignedIn(page, route.path);
      await settle(page);
      await screenshot(page, info, route.name);
      await expectClean(page, seen, route.path, info);
    });
  }

  test("/runs/<newest run> renders cleanly", async ({ page }, info) => {
    const seen = watch(page);
    await openSignedIn(page, "/settings");
    await settle(page);
    const runs = await authed(page, "/runs?limit=1");
    expect(runs.status).toBe(200);
    const list = runs.body as { run_id: string }[];
    test.skip(!Array.isArray(list) || list.length === 0, "no run in this runtime");
    await openSignedIn(page, `/runs/${encodeURIComponent(list[0].run_id)}`);
    await settle(page);
    await screenshot(page, info, "run-detail");
    await expectClean(page, seen, "/runs/<id>");
  });

  test("/zt/approvals renders cleanly when the approvals add-on is installed", async ({ page }, info) => {
    await openSignedIn(page, "/zt");
    await settle(page);
    const linked = await page.locator('a[href="/zt/approvals"]').count();
    test.skip(linked === 0, "no /zt/approvals link in the navigation (approvals add-on not installed)");
    const seen = watch(page);
    await openSignedIn(page, "/zt/approvals");
    await settle(page);
    await screenshot(page, info, "zt-approvals");
    await expectClean(page, seen, "/zt/approvals");
  });
});

test.describe("/zt in depth", () => {
  test("native wheel and keyboard reach the bottom and return", async ({ page }, info) => {
    await openSignedIn(page, "/runtime");
    await settle(page);
    const main = page.locator("main").first();
    const geometry = await main.evaluate(el => ({ height: el.clientHeight, total: el.scrollHeight }));
    test.skip(geometry.total <= geometry.height + 1, "page fits this viewport");
    const box = await main.boundingBox();
    expect(box).not.toBeNull();
    await page.mouse.move(box!.x + box!.width / 2, box!.y + box!.height / 2);
    const top = await main.evaluate(el => el.scrollTop);
    await page.mouse.wheel(0, geometry.total);
    await expect.poll(() => main.evaluate(el => el.scrollTop)).toBeGreaterThan(top);
    await expect.poll(() => main.evaluate(el => el.scrollTop + el.clientHeight)).toBeGreaterThanOrEqual(geometry.total - 2);
    await page.mouse.wheel(0, -geometry.total);
    await expect.poll(() => main.evaluate(el => el.scrollTop)).toBeLessThanOrEqual(2);
    // Use VT's existing skip link, the same ordinary keyboard path as the user.
    await page.reload();
    await settle(page);
    await page.keyboard.press("Tab");
    await page.keyboard.press("Enter");
    expect(await main.evaluate(el => el === document.activeElement)).toBe(true);
    await page.keyboard.press("PageDown");
    await expect.poll(() => main.evaluate(el => el.scrollTop)).toBeGreaterThan(0);
    for (let step = 0; step < Math.ceil(geometry.total / geometry.height) + 1; step++) await page.keyboard.press("PageDown");
    await expect.poll(() => main.evaluate(el => el.scrollTop + el.clientHeight)).toBeGreaterThanOrEqual(geometry.total - 2);
    await screenshot(page, info, "runtime-bottom");
    for (let step = 0; step < Math.ceil(geometry.total / geometry.height) + 1; step++) await page.keyboard.press("PageUp");
    await expect.poll(() => main.evaluate(el => el.scrollTop)).toBeLessThanOrEqual(2);
  });

  test("important add-on and runtime controls have usable targets", async ({ page }) => {
    for (const route of ["/runtime", "/zt"]) {
      await openSignedIn(page, route);
      await settle(page);
      const controls = page.locator("main button, main select");
      for (const control of await controls.all()) {
        if (!await control.isVisible()) continue;
        const box = await control.boundingBox();
        expect(box?.width, `${route}: ${await control.innerText()} width`).toBeGreaterThanOrEqual(44);
        expect(box?.height, `${route}: ${await control.innerText()} height`).toBeGreaterThanOrEqual(44);
      }
    }
  });

  test("pre-flight panel, report list, report viewer and snapshot cards", async ({ page }, info) => {
    const seen = watch(page);
    await openSignedIn(page, "/zt");
    await settle(page);

    const preflight = page.getByTestId("zt-preflight");
    await expect(preflight.getByRole("heading", { name: /Pre-flight/ })).toBeVisible();
    await expect(preflight.getByTestId("zt-preflight-counts")).toHaveText(/\d+ OK · \d+ WARN · \d+ FAIL/);
    // Problems are rows, OK checks are chips until expanded.
    const items = preflight.locator('[data-testid="zt-preflight-check"], [data-testid="zt-preflight-ok"]');
    await expect(items.first()).toBeVisible();
    const total = await items.count();
    expect(total).toBeGreaterThanOrEqual(12);
    const ids = await items.evaluateAll((nodes) => nodes.map((node) => node.getAttribute("data-check-id")));
    for (const id of ["llm.provider", "llm.ollama", "pit.audit_receipt", "pit.price_lake", "orders.approval"]) {
      expect(ids).toContain(id);
    }
    const expand = preflight.getByRole("button", { name: "Show all checks" });
    if (await expand.count()) await expand.click();
    const rows = preflight.getByTestId("zt-preflight-check");
    await expect(rows).toHaveCount(total);
    const statuses = await rows.evaluateAll((nodes) => nodes.map((node) => node.getAttribute("data-status")));
    expect(statuses.every((status) => ["OK", "WARN", "FAIL"].includes(status ?? ""))).toBe(true);
    // Every detail open: long paths and JSON must wrap, not widen the page.
    await preflight.locator("details").evaluateAll((nodes) => nodes.forEach((node) => node.setAttribute("open", "")));
    const opened = await horizontalOverflow(page);
    expect
      .soft(opened.scroll, `pre-flight details overflow at ${opened.width}px: ${opened.offenders.join("; ")}`)
      .toBeLessThanOrEqual(opened.width + 1);
    await screenshot(page, info, "zt-preflight-expanded");
    await preflight.locator("details").evaluateAll((nodes) => nodes.forEach((node) => node.removeAttribute("open")));
    const collapse = preflight.getByRole("button", { name: "Hide OK checks" });
    if (await collapse.count()) await collapse.click();

    // Report list, then one report in the sandboxed viewer.
    const open = page.getByRole("button", { name: /^Open / }).and(page.locator(":enabled"));
    await expect(open.first()).toBeVisible();
    const label = (await open.first().getAttribute("aria-label")) ?? "";
    const title = label.replace(/^Open /, "");
    await open.first().click();
    const frame = page.getByTitle(`Report: ${title}`);
    await expect(frame).toBeVisible();
    await expect(frame).toHaveAttribute("sandbox", /allow-scripts/);
    const src = (await frame.getAttribute("src")) ?? "";
    expect(src).toMatch(/^\/zt\/reports\/.+\?ticket=/);
    expect(src).not.toContain(KEY);
    const body = page.frameLocator(`iframe[title="Report: ${title}"]`).locator("body");
    await expect(body).not.toBeEmpty();
    expect((await body.innerText()).trim().length).toBeGreaterThan(20);
    await screenshot(page, info, "zt-report-viewer");
    await page.getByRole("button", { name: "Close" }).click();
    await expect(frame).toHaveCount(0);

    // Snapshot cards: today's export may not exist yet; the page offers the latest.
    const latest = page.getByRole("button", { name: /^Show latest/ });
    if (await latest.count()) await latest.click();
    await expect(page.getByRole("heading", { name: "Source health" })).toBeVisible();
    await expect(page.getByRole("heading", { name: "Freshness" })).toBeVisible();
    await screenshot(page, info, "zt-snapshot");
    await expectClean(page, seen, "/zt in depth");
  });
});

// The desktop shell (desktop/electron) loads the SPA from its private backend and
// injects "Authorization: Bearer <per-launch key>" into every request to that origin
// (webRequest.onBeforeSendHeaders), frames included; the page itself never holds a
// key, and the preload bridge exposes window.vibeDesktop. Emulated here with an
// extra header and an init script; the server side of the same path is covered by
// desktop-backend-smoke.mjs, which drives the shell's own BackendManager.
test.describe("desktop shell (emulated) @desktop", () => {
  test.use({ extraHTTPHeaders: { Authorization: `Bearer ${KEY}` } });
  test.beforeEach(async ({ page }) => {
    await page.addInitScript(() => {
      (window as unknown as { vibeDesktop: unknown }).vibeDesktop = {
        isDesktop: true,
        restartBackend: async () => true,
        getCredentialStatus: async () => ({ available: true, configured: [], migrated: [] }),
        setCredential: async () => ({ available: true, configured: [], migrated: [] }),
      };
    });
  });

  test("/zt: pre-flight, report frame through the header, Open in browser with a fresh ticket", async ({ page, context }, info) => {
    const seen = watch(page);
    await page.goto("/zt");
    await settle(page);
    expect(await page.evaluate((storageKey) => localStorage.getItem(storageKey), STORAGE_KEY)).toBeNull();
    await expect(page.getByTestId("zt-preflight").getByTestId("zt-preflight-counts")).toBeVisible();
    const open = page.getByRole("button", { name: /^Open / }).and(page.locator(":enabled"));
    const title = ((await open.first().getAttribute("aria-label")) ?? "").replace(/^Open /, "");
    await open.first().click();
    const frame = page.getByTitle(`Report: ${title}`);
    await expect(frame).toBeVisible();
    // No stored key: the frame URL carries no ticket and loads through the injected header.
    expect(await frame.getAttribute("src")).not.toContain("ticket=");
    const body = page.frameLocator(`iframe[title="Report: ${title}"]`).locator("body");
    expect((await body.innerText()).trim().length).toBeGreaterThan(20);
    const popup = context.waitForEvent("page");
    await page.getByRole("button", { name: "Open in browser" }).click();
    const opened = await popup;
    // The popup exists before its first navigation commits (url() is "" until then);
    // on a busy PC the URL arrives a moment later.
    await opened.waitForURL(/\/zt\/reports\/.+\?ticket=/, { timeout: 15_000 });
    expect(opened.url()).toMatch(/\/zt\/reports\/.+\?ticket=/);
    expect(opened.url()).not.toContain(KEY);
    await opened.close();
    await screenshot(page, info, "desktop-zt");
    await expectClean(page, seen, "desktop /zt", info);
  });

  for (const route of ROUTES) {
    test(`${route.path} renders cleanly with the injected header`, async ({ page }, info) => {
      const seen = watch(page);
      await page.goto(route.path);
      await settle(page);
      await screenshot(page, info, route.name);
      await expectClean(page, seen, route.path, info);
    });
  }
});
