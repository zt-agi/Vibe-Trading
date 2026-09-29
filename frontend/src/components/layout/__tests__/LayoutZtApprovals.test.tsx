// ZT add-on: the /zt/approvals nav entry, and only the longest match is highlighted.
import { render, screen } from "@testing-library/react";
import { MemoryRouter, Route, Routes } from "react-router";
import { Layout } from "../Layout";

vi.mock("react-i18next", () => ({
  useTranslation: () => ({
    t: (key: string, options?: { defaultValue?: string }) => options?.defaultValue ?? key,
    i18n: { language: "en", languages: ["en"], changeLanguage: vi.fn().mockResolvedValue(undefined) },
  }),
}));

vi.mock("@/hooks/useDarkMode", () => ({ useDarkMode: () => ({ dark: false, toggle: vi.fn() }) }));

vi.mock("@/lib/api", () => ({
  api: {
    listSessions: vi.fn().mockResolvedValue([]),
    deleteSession: vi.fn().mockResolvedValue(undefined),
    renameSession: vi.fn().mockResolvedValue(undefined),
  },
}));

vi.mock("@/stores/agent", () => ({
  useAgentStore: (selector: (state: { sseStatus: string; sseRetryAttempt: number; streamingSessionId: null }) => unknown) =>
    selector({ sseStatus: "connected", sseRetryAttempt: 0, streamingSessionId: null }),
}));

function renderAt(path: string) {
  return render(
    <MemoryRouter initialEntries={[path]}>
      <Routes>
        <Route element={<Layout />}>
          <Route path="*" element={<div>page</div>} />
        </Route>
      </Routes>
    </MemoryRouter>,
  );
}

const ACTIVE = "bg-primary/10";

describe("ZT approvals navigation", () => {
  it("links to /zt/approvals and highlights only that entry there", () => {
    renderAt("/zt/approvals");
    const approvals = screen.getByRole("link", { name: "Order approvals" });
    expect(approvals).toHaveAttribute("href", "/zt/approvals");
    expect(approvals).toHaveClass(ACTIVE);
    expect(screen.getByRole("link", { name: "ZT Research" })).not.toHaveClass(ACTIVE);
  });

  it("keeps the other entries' highlighting", () => {
    renderAt("/zt");
    expect(screen.getByRole("link", { name: "ZT Research" })).toHaveClass(ACTIVE);
    expect(screen.getByRole("link", { name: "Order approvals" })).not.toHaveClass(ACTIVE);
  });

  it("still highlights nested routes of other entries", () => {
    renderAt("/alpha-zoo/bench");
    expect(screen.getByRole("link", { name: "layout.alphaZoo" })).toHaveClass(ACTIVE);
  });
});
