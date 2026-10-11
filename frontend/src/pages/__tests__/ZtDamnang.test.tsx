import { fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import { MemoryRouter } from "react-router";
import { ZtDamnang } from "../ZtDamnang";

const apiMock = vi.hoisted(() => ({ getDamnangPosts: vi.fn() }));
vi.mock("@/lib/api", async (importOriginal) => {
  const actual = await importOriginal<typeof import("@/lib/api")>();
  return { ...actual, api: { ...actual.api, getDamnangPosts: apiMock.getDamnangPosts } };
});

const ARTICLES = [
  { id: "hbm", title: "How HBM is made", subtitle: "Micron memory", published_at: "2026-10-08T00:00:00Z", date: "2026-10-08", audience: "paid", type: "essay", url: "https://www.damnang.com/p/hbm", topics: ["memory-hbm"], tickers: ["MU"], bottleneck: "Packaging capacity", verified_at: "2026-10-11T00:00:00Z" },
  { id: "optics", title: "Optical packaging", subtitle: "", published_at: "2026-10-07T00:00:00Z", date: "2026-10-07", audience: "free", type: "essay", url: "https://www.damnang.com/p/optics", topics: ["optics"], tickers: ["COHR"], bottleneck: "Laser assembly", verified_at: "2026-10-11T00:00:00Z" },
] as const;

beforeEach(() => {
  apiMock.getDamnangPosts.mockResolvedValue({ source: "Damnang Research public article index", source_url: "https://map.damnang.com/data/posts.json", retrieved_at: "2026-10-11T07:00:00Z", status: "live", count: 2, authority: "PUBLISHER_METADATA_ONLY_NON_PIT", articles: ARTICLES });
});

afterEach(() => vi.clearAllMocks());

test("searches publisher metadata, filters access, and opens the original safely", async () => {
  render(<MemoryRouter><ZtDamnang /></MemoryRouter>);
  expect(await screen.findByRole("heading", { name: "Damnang Research" })).toBeInTheDocument();
  await waitFor(() => expect(screen.getByRole("status")).toHaveTextContent("2 of 2 articles"));
  fireEvent.change(screen.getByRole("searchbox", { name: "Search articles and companies" }), { target: { value: "MU packaging" } });
  expect(screen.getByRole("status")).toHaveTextContent("1 of 2 articles");
  const card = screen.getByRole("article");
  const link = within(card).getByRole("link", { name: /Open original/ });
  expect(link).toHaveAttribute("href", "https://www.damnang.com/p/hbm");
  expect(link).toHaveAttribute("target", "_blank");
  expect(link).toHaveAttribute("rel", "noopener noreferrer");
  fireEvent.change(screen.getByRole("combobox", { name: "Access" }), { target: { value: "free" } });
  expect(screen.getByRole("status")).toHaveTextContent("0 of 2 articles");
  fireEvent.click(screen.getByRole("button", { name: "Clear filters" }));
  expect(screen.getByRole("status")).toHaveTextContent("2 of 2 articles");
});

test("shows an explicit stale warning and refreshes the source", async () => {
  apiMock.getDamnangPosts.mockResolvedValueOnce({ source: "Damnang", source_url: "https://map.damnang.com/data/posts.json", retrieved_at: "2026-10-11T07:00:00Z", status: "stale", count: 2, authority: "PUBLISHER_METADATA_ONLY_NON_PIT", articles: ARTICLES });
  render(<MemoryRouter><ZtDamnang /></MemoryRouter>);
  expect(await screen.findByRole("alert")).toHaveTextContent("last index retrieved");
  fireEvent.click(screen.getByRole("button", { name: "Refresh index" }));
  await waitFor(() => expect(apiMock.getDamnangPosts).toHaveBeenLastCalledWith(true));
});
