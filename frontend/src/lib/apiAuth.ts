import { safeGet, safeRemove, safeSet } from "@/lib/storage";

const STORAGE_KEY = "vibe_trading_api_auth_key";

export function getApiAuthKey(): string {
  return safeGet(STORAGE_KEY) || "";
}

export function setApiAuthKey(value: string): void {
  const trimmed = value.trim();
  if (trimmed) {
    safeSet(STORAGE_KEY, trimmed);
  } else {
    safeRemove(STORAGE_KEY);
  }
}

export function authHeaders(): Record<string, string> {
  const key = getApiAuthKey();
  return key ? { Authorization: `Bearer ${key}` } : {};
}

/**
 * Append a short-lived, single-use SSE ticket to an EventSource URL.
 *
 * A browser `EventSource` cannot set an `Authorization` header, so we exchange
 * the stored API key (sent in a header on this POST) for a one-shot ticket via
 * `POST /auth/sse-ticket`, then open the stream with `?ticket=`. This keeps the
 * long-lived key out of URLs, browser history, and proxy/access logs.
 *
 * When no key is stored the backend is in loopback dev mode (auth bypassed), so
 * the URL is returned unchanged and no ticket round-trip is made. Tickets are
 * single-use: every connect/reconnect must mint a fresh one, so callers invoke
 * this per connection attempt rather than caching the result.
 */
export async function withAuthTicket(url: string): Promise<string> {
  const key = getApiAuthKey();
  if (!key) return url;
  const res = await fetch("/auth/sse-ticket", {
    method: "POST",
    headers: authHeaders(),
  });
  if (!res.ok) {
    throw new Error(`Failed to obtain SSE ticket (HTTP ${res.status})`);
  }
  const data: unknown = await res.json();
  const ticket = (data as { ticket?: unknown } | null)?.ticket;
  if (typeof ticket !== "string" || !ticket) {
    throw new Error("SSE ticket response missing ticket");
  }
  const sep = url.includes("?") ? "&" : "?";
  return `${url}${sep}ticket=${encodeURIComponent(ticket)}`;
}

// ---------------------------------------------------------------------------
// ZT add-on: one-click web sign-in and desktop report tickets
// ---------------------------------------------------------------------------

/** Fragment parameter the launchers use: http://127.0.0.1:8899/#vt_key=<key>. */
export const API_KEY_FRAGMENT_PARAM = "vt_key";
const MAX_API_KEY_LENGTH = 512;

/**
 * One-click sign-in. When the URL fragment carries `vt_key=<key>`, store the
 * key where {@link getApiAuthKey} reads it and remove it from the address bar
 * and the current history entry (`history.replaceState`), keeping any other
 * fragment parameters. Runs before any other module reads the URL (see
 * `src/bootstrapAuth.ts`). A fragment never reaches the server, and the key is
 * never logged. An empty, oversized or whitespace-bearing value is removed from
 * the URL but not stored.
 *
 * @returns `true` when a key was stored.
 */
export function consumeApiAuthKeyFromFragment(win: Window = window): boolean {
  let hash = "";
  try {
    hash = win.location.hash || "";
  } catch {
    return false;
  }
  if (!hash.includes(`${API_KEY_FRAGMENT_PARAM}=`)) return false;

  let found = false;
  let raw = "";
  const kept: string[] = [];
  for (const part of hash.replace(/^#/, "").split("&")) {
    const eq = part.indexOf("=");
    const name = eq >= 0 ? part.slice(0, eq) : part;
    if (name === API_KEY_FRAGMENT_PARAM) {
      found = true;
      raw = eq >= 0 ? part.slice(eq + 1) : "";
    } else if (part) {
      kept.push(part);
    }
  }
  if (!found) return false;

  // Take the key out of the URL first, whatever its value turns out to be.
  const rest = kept.length ? `#${kept.join("&")}` : "";
  try {
    win.history.replaceState(win.history.state, "", `${win.location.pathname}${win.location.search}${rest}`);
  } catch {
    try {
      win.location.replace(`${win.location.pathname}${win.location.search}${rest || "#"}`);
    } catch {
      /* nothing else can be done without a reload */
    }
  }

  let value = raw;
  try {
    value = decodeURIComponent(raw);
  } catch {
    /* keep the raw value */
  }
  value = value.trim();
  if (!value || value.length > MAX_API_KEY_LENGTH || /[\s\u0000-\u001f\u007f]/.test(value)) return false;
  setApiAuthKey(value);
  return true;
}

/**
 * Append a freshly minted single-use ticket to `url`, even when no key is
 * stored in this browser. The desktop shell injects the Authorization header
 * itself (the page never sees its key), so {@link withAuthTicket}'s "no key,
 * no ticket" shortcut would hand the system browser an unauthenticated URL.
 * Falls back to the plain URL when minting fails (loopback dev mode needs none).
 */
export async function withFreshTicket(url: string): Promise<string> {
  try {
    const res = await fetch("/auth/sse-ticket", { method: "POST", headers: authHeaders() });
    if (!res.ok) return url;
    const data: unknown = await res.json();
    const ticket = (data as { ticket?: unknown } | null)?.ticket;
    if (typeof ticket !== "string" || !ticket) return url;
    const sep = url.includes("?") ? "&" : "?";
    return `${url}${sep}ticket=${encodeURIComponent(ticket)}`;
  } catch {
    return url;
  }
}
