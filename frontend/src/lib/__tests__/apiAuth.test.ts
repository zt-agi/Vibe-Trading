import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import {
  API_KEY_FRAGMENT_PARAM,
  authHeaders,
  consumeApiAuthKeyFromFragment,
  getApiAuthKey,
  setApiAuthKey,
  withAuthTicket,
  withFreshTicket,
} from "../apiAuth";

describe("apiAuth", () => {
  beforeEach(() => {
    localStorage.clear();
  });

  afterEach(() => {
    vi.unstubAllGlobals();
  });

  describe("getApiAuthKey", () => {
    it("returns empty string when nothing stored", () => {
      expect(getApiAuthKey()).toBe("");
    });
    it("returns stored key", () => {
      localStorage.setItem("vibe_trading_api_auth_key", "my-secret");
      expect(getApiAuthKey()).toBe("my-secret");
    });
    it("returns empty string when storage access is blocked", () => {
      vi.spyOn(Storage.prototype, "getItem").mockImplementation(() => {
        throw new DOMException("blocked", "SecurityError");
      });
      expect(getApiAuthKey()).toBe("");
    });
  });

  describe("setApiAuthKey", () => {
    it("stores trimmed value", () => {
      setApiAuthKey("  abc-123  ");
      expect(localStorage.getItem("vibe_trading_api_auth_key")).toBe("abc-123");
    });
    it("removes key when value is empty/whitespace", () => {
      setApiAuthKey("abc");
      setApiAuthKey("   ");
      expect(localStorage.getItem("vibe_trading_api_auth_key")).toBeNull();
    });
    it("removes key when value is empty string", () => {
      setApiAuthKey("abc");
      setApiAuthKey("");
      expect(localStorage.getItem("vibe_trading_api_auth_key")).toBeNull();
    });
    it("does not throw when storage writes are blocked", () => {
      vi.spyOn(Storage.prototype, "setItem").mockImplementation(() => {
        throw new DOMException("blocked", "SecurityError");
      });
      expect(() => setApiAuthKey("abc")).not.toThrow();
    });
  });

  describe("authHeaders", () => {
    it("returns empty object when no key set", () => {
      expect(authHeaders()).toEqual({});
    });
    it("returns Bearer header when key exists", () => {
      setApiAuthKey("token-xyz");
      expect(authHeaders()).toEqual({ Authorization: "Bearer token-xyz" });
    });
  });

  describe("withAuthTicket", () => {
    it("returns url unchanged and makes no request when no key (dev/loopback)", async () => {
      const fetchSpy = vi.fn();
      vi.stubGlobal("fetch", fetchSpy);
      await expect(withAuthTicket("http://api/stream")).resolves.toBe("http://api/stream");
      expect(fetchSpy).not.toHaveBeenCalled();
    });

    it("mints a ticket via header-authed POST and appends ?ticket=", async () => {
      setApiAuthKey("token-xyz");
      const fetchSpy = vi.fn().mockResolvedValue(
        new Response(JSON.stringify({ ticket: "TICKET-123" }), {
          status: 200,
          headers: { "content-type": "application/json" },
        }),
      );
      vi.stubGlobal("fetch", fetchSpy);

      const url = await withAuthTicket("http://api/stream");

      expect(url).toBe("http://api/stream?ticket=TICKET-123");
      expect(fetchSpy).toHaveBeenCalledTimes(1);
      const [path, init] = fetchSpy.mock.calls[0];
      expect(path).toBe("/auth/sse-ticket");
      expect(init.method).toBe("POST");
      expect(init.headers).toEqual({ Authorization: "Bearer token-xyz" });
    });

    it("never puts the raw API key in the returned URL", async () => {
      setApiAuthKey("super-secret-key");
      vi.stubGlobal(
        "fetch",
        vi.fn().mockResolvedValue(
          new Response(JSON.stringify({ ticket: "one-shot" }), {
            status: 200,
            headers: { "content-type": "application/json" },
          }),
        ),
      );
      const url = await withAuthTicket("http://api/stream");
      expect(url).not.toContain("super-secret-key");
      expect(url).not.toContain("api_key=");
      expect(url).toContain("ticket=one-shot");
    });

    it("joins with & when the url already has a query string", async () => {
      setApiAuthKey("k");
      vi.stubGlobal(
        "fetch",
        vi.fn().mockResolvedValue(
          new Response(JSON.stringify({ ticket: "t1" }), {
            status: 200,
            headers: { "content-type": "application/json" },
          }),
        ),
      );
      const url = await withAuthTicket("http://api/stream?replay=active");
      expect(url).toBe("http://api/stream?replay=active&ticket=t1");
    });

    it("throws when the ticket endpoint returns a non-OK status", async () => {
      setApiAuthKey("k");
      vi.stubGlobal("fetch", vi.fn().mockResolvedValue(new Response("", { status: 401 })));
      await expect(withAuthTicket("http://api/stream")).rejects.toThrow(/HTTP 401/);
    });

    it("throws when the response is missing a ticket", async () => {
      setApiAuthKey("k");
      vi.stubGlobal(
        "fetch",
        vi.fn().mockResolvedValue(
          new Response(JSON.stringify({}), {
            status: 200,
            headers: { "content-type": "application/json" },
          }),
        ),
      );
      await expect(withAuthTicket("http://api/stream")).rejects.toThrow(/missing ticket/);
    });
  });
});

// ZT add-on: one-click web sign-in through the URL fragment.
describe("consumeApiAuthKeyFromFragment", () => {
  const KEY = "Zt-Test_key.0123456789~abcdef";

  beforeEach(() => {
    localStorage.clear();
    window.history.replaceState({ idx: 3 }, "", "/");
  });

  afterEach(() => {
    window.history.replaceState(null, "", "/");
  });

  function spyConsole() {
    return (["log", "info", "warn", "error", "debug"] as const).map((method) =>
      vi.spyOn(console, method).mockImplementation(() => undefined),
    );
  }

  it("stores the key, removes it from the URL and keeps the history state", () => {
    const logs = spyConsole();
    window.history.replaceState({ idx: 3 }, "", `/zt?report=A.html#${API_KEY_FRAGMENT_PARAM}=${KEY}`);
    const replace = vi.spyOn(window.history, "replaceState");
    const lengthBefore = window.history.length;

    expect(consumeApiAuthKeyFromFragment()).toBe(true);

    expect(getApiAuthKey()).toBe(KEY);
    expect(window.location.hash).toBe("");
    expect(window.location.pathname + window.location.search).toBe("/zt?report=A.html");
    expect(window.location.href).not.toContain(KEY);
    expect(replace).toHaveBeenCalledWith({ idx: 3 }, "", "/zt?report=A.html");
    expect(window.history.length).toBe(lengthBefore);
    for (const spy of logs) expect(spy).not.toHaveBeenCalled();
  });

  it("decodes a percent-encoded key (a launcher escapes + / =) and keeps other fragment parameters", () => {
    window.history.replaceState(null, "", `/#tab=2&vt_key=${encodeURIComponent("a+b/c=")}&x`);
    expect(consumeApiAuthKeyFromFragment()).toBe(true);
    expect(getApiAuthKey()).toBe("a+b/c=");
    expect(window.location.hash).toBe("#tab=2&x");
  });

  it.each([
    ["an empty value", "vt_key="],
    ["whitespace", `vt_key=${encodeURIComponent("abc def")}`],
    ["a control character", "vt_key=abc%0Adef"],
    ["an oversized value", `vt_key=${"k".repeat(600)}`],
  ])("strips but does not store %s", (_label, fragment) => {
    setApiAuthKey("previous-key");
    window.history.replaceState(null, "", `/settings#${fragment}`);
    expect(consumeApiAuthKeyFromFragment()).toBe(false);
    expect(window.location.hash).toBe("");
    expect(getApiAuthKey()).toBe("previous-key");
  });

  it("leaves URLs without the parameter alone", () => {
    const replace = vi.spyOn(window.history, "replaceState");
    window.history.replaceState(null, "", "/agent#section-2");
    replace.mockClear();
    expect(consumeApiAuthKeyFromFragment()).toBe(false);
    expect(replace).not.toHaveBeenCalled();
    expect(window.location.hash).toBe("#section-2");
    window.history.replaceState(null, "", "/agent#my_vt_key=1");
    expect(consumeApiAuthKeyFromFragment()).toBe(false);
    expect(window.location.hash).toBe("#my_vt_key=1");
  });

  it("still removes the key when storage is blocked", () => {
    vi.spyOn(Storage.prototype, "setItem").mockImplementation(() => {
      throw new DOMException("blocked", "SecurityError");
    });
    window.history.replaceState(null, "", `/#vt_key=${KEY}`);
    expect(() => consumeApiAuthKeyFromFragment()).not.toThrow();
    expect(window.location.href).not.toContain(KEY);
  });

  it("runs from the bootstrap module before the app reads the URL", async () => {
    window.history.replaceState(null, "", `/zt#vt_key=${KEY}`);
    vi.resetModules();
    await import("@/bootstrapAuth");
    expect(getApiAuthKey()).toBe(KEY);
    expect(window.location.pathname).toBe("/zt");
    expect(window.location.hash).toBe("");
  });

  it("main.tsx imports the bootstrap first", async () => {
    const { readFileSync } = await import("node:fs");
    const { resolve } = await import("node:path");
    const main = readFileSync(resolve(__dirname, "../../main.tsx"), "utf8");
    const firstImport = main.split("\n").find((line) => line.startsWith("import "));
    expect(firstImport).toMatch(/^import "\.\/bootstrapAuth";/);
  });
});

describe("withFreshTicket", () => {
  beforeEach(() => localStorage.clear());
  afterEach(() => vi.unstubAllGlobals());

  it("mints a ticket even when no key is stored (the desktop injects the header)", async () => {
    const fetchSpy = vi.fn().mockResolvedValue(
      new Response(JSON.stringify({ ticket: "T 1" }), { status: 200, headers: { "content-type": "application/json" } }),
    );
    vi.stubGlobal("fetch", fetchSpy);
    await expect(withFreshTicket("/zt/reports/A.html")).resolves.toBe("/zt/reports/A.html?ticket=T%201");
    expect(fetchSpy).toHaveBeenCalledWith("/auth/sse-ticket", { method: "POST", headers: {} });
  });

  it("falls back to the plain URL when minting fails", async () => {
    vi.stubGlobal("fetch", vi.fn().mockResolvedValue(new Response("", { status: 403 })));
    await expect(withFreshTicket("/zt/reports/A.html")).resolves.toBe("/zt/reports/A.html");
    vi.stubGlobal("fetch", vi.fn().mockRejectedValue(new TypeError("offline")));
    await expect(withFreshTicket("/zt/reports/A.html?x=1")).resolves.toBe("/zt/reports/A.html?x=1");
  });
});
