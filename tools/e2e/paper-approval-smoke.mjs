// Runs only against paper_approval_fixture.py's fresh E: manifest, never live 8899.
import assert from "node:assert/strict";
import { readFile, writeFile } from "node:fs/promises";
import { createRequire } from "node:module";
import path from "node:path";
const manifestPath = path.resolve(process.env.VT_PAPER_FIXTURE || "");
assert.match(manifestPath, /^E:[\\/]/i);
const fixture = JSON.parse(await readFile(manifestPath, "utf8"));
assert.equal(fixture.schema, "vt-paper-browser-fixture/1");
assert.equal(fixture.halt, true);
assert.notEqual(new URL(fixture.url).port, "8899");
assert.match(fixture.home, /^E:[\\/]/i);
assert.equal(await readFile(path.join(fixture.home, "agent.json"), "utf8"), '{"mcpServers": {}}');
await readFile(path.join(fixture.home, "live", "HALT"));
const require = createRequire(process.env.VT_DESKTOP_PLAYWRIGHT);
const { chromium } = require(process.env.VT_DESKTOP_PLAYWRIGHT);
const browser = await chromium.launch({ headless: true, channel: process.env.VT_E2E_CHANNEL || "msedge" });
const context = await browser.newContext({ viewport: { width: 1280, height: 820 }, extraHTTPHeaders: { Authorization: `Bearer ${fixture.key}` } });
const page = await context.newPage();
const result = { schema: "vt-paper-browser-acceptance/1", fixture: manifestPath, prices: fixture.prices, rows: [], console_errors: [] };
page.on("pageerror", error => result.console_errors.push(error.message));
async function row(id, fn) {
  try { result.rows.push({ id, status: "PASS", evidence: await fn() }); }
  catch (error) { result.rows.push({ id, status: "FAIL", error: error.message }); throw error; }
}
const headers = { Authorization: `Bearer ${fixture.key}` };
async function api(route, options = {}) {
  const response = await fetch(`${fixture.url}${route}`, { ...options, headers: { ...headers, "Content-Type": "application/json" } });
  return { status: response.status, body: await response.json() };
}
async function open(name) {
  await page.goto(`${fixture.url}/zt/approvals?proposal=${fixture.proposals[name].id}`);
  await page.getByRole("heading", { name: "Order approvals", exact: true }).waitFor();
  await page.getByRole("region", { name: "Proposal detail" }).waitFor();
  await page.getByRole("region", { name: "Proposal detail" }).getByTestId("status-chip").waitFor();
  if (name !== "expired") await page.getByTestId("hash-tail").waitFor();
}
try {
  await row("checkbox-gated-paper-fill-once", async () => {
    await open("approve");
    const approve = page.getByRole("button", { name: "Approve and submit once", exact: true });
    assert.equal(await approve.isDisabled(), true);
    await page.getByRole("checkbox").check();
    assert.equal(await approve.isEnabled(), true);
    const targets = await page.locator("main button, main select, main textarea, main input:not([type=checkbox])").evaluateAll(elements => elements.filter(e => e.getClientRects().length).map(e => ({ label: e.getAttribute("aria-label") || e.textContent?.trim(), width: e.getBoundingClientRect().width, height: e.getBoundingClientRect().height })));
    result.measured_targets = targets;
    assert.deepEqual(targets.filter(target => target.width < 44 || target.height < 44), [], "Visible paper controls must be at least 44 by 44 pixels");
    await approve.click();
    await page.getByRole("region", { name: "Proposal detail" }).getByTestId("status-chip").filter({ hasText: "FILLED" }).waitFor();
    const account = (await api("/zt/paper/account")).body;
    assert.equal(account.cash, 98000);
    assert.equal(account.positions[0].qty, 10);
    const repeat = await api(`/zt/orders/proposals/${fixture.proposals.approve.id}/approve`, { method: "POST", body: JSON.stringify({ confirm_hash: fixture.proposals.approve.hash }) });
    assert.equal(repeat.status, 409);
    await page.reload();
    await page.getByRole("region", { name: "Proposal detail" }).getByTestId("status-chip").filter({ hasText: "FILLED" }).waitFor();
    assert.equal((await api("/zt/paper/account")).body.cash, 98000);
    await page.screenshot({ path: path.join(path.dirname(manifestPath), "paper-filled.png") });
    return { cash: account.cash, qty: account.positions[0].qty, repeat_refused: repeat.status, reload_stable: true, targets };
  });
  await row("reason-gated-rejection", async () => {
    await open("reject");
    const reject = page.getByRole("button", { name: "Reject", exact: true });
    assert.equal(await reject.isDisabled(), true);
    await page.getByLabel("Reason for rejecting").fill("Paper browser QA rejects this proposal");
    await reject.click();
    await page.getByRole("region", { name: "Proposal detail" }).getByTestId("status-chip").filter({ hasText: "REJECTED" }).waitFor();
    const detail = (await api(`/zt/orders/proposals/${fixture.proposals.reject.id}`)).body;
    assert.ok(detail.ledger_events.some(e => e.to === "REJECTED" && e.reason === "Paper browser QA rejects this proposal"));
    return { status: detail.status, recorded_reason: true };
  });
  await row("expired-approval-refused", async () => {
    await open("expired");
    await page.getByRole("region", { name: "Proposal detail" }).getByTestId("status-chip").filter({ hasText: "EXPIRED" }).waitFor();
    const checkbox = page.getByRole("checkbox");
    const approve = page.getByRole("button", { name: "Approve and submit once", exact: true });
    assert.ok(await checkbox.count() === 0 || await checkbox.isDisabled());
    assert.ok(await approve.count() === 0 || await approve.isDisabled());
    const refused = await api(`/zt/orders/proposals/${fixture.proposals.expired.id}/approve`, { method: "POST", body: JSON.stringify({ confirm_hash: fixture.proposals.expired.hash }) });
    assert.equal(refused.status, 410);
    assert.equal((await api("/zt/paper/account")).body.cash, 98000);
    return { refused: refused.status, account_unchanged: true };
  });
} catch (error) { result.fatal = error.message; }
finally {
  await browser.close();
  result.status = result.fatal || result.console_errors.length ? "FAIL" : "PASS";
  await writeFile(path.join(path.dirname(manifestPath), "paper-results.json"), JSON.stringify(result, null, 2));
  console.log(JSON.stringify(result));
}
process.exitCode = result.status === "PASS" ? 0 : 1;
