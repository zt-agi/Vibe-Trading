import { useEffect, useMemo, useRef, useState } from "react";
import { useSearchParams } from "react-router";
import { BookOpen, ExternalLink, RefreshCw, Search } from "lucide-react";
import { api, isAuthRequiredError } from "@/lib/api";
import type { DamnangArticle, DamnangIndex } from "@/lib/damnang";

const PAGE_SIZE = 24;

function matches(article: DamnangArticle, query: string): boolean {
  const terms = query.normalize("NFKC").toLowerCase().trim().split(/\s+/).filter(Boolean);
  if (!terms.length) return true;
  const text = [article.title, article.subtitle, article.bottleneck,
    ...article.topics, ...article.tickers].join(" ").normalize("NFKC").toLowerCase();
  return terms.every((term) => text.includes(term));
}

function dateLabel(article: DamnangArticle): string {
  const date = new Date(article.published_at || article.date);
  return Number.isNaN(date.getTime()) ? (article.date || "Date unavailable") :
    date.toLocaleDateString("en-US", { year: "numeric", month: "short", day: "numeric" });
}

export function ZtDamnang() {
  const [params, setParams] = useSearchParams();
  const [index, setIndex] = useState<DamnangIndex | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [loading, setLoading] = useState(true);
  const [visibleCount, setVisibleCount] = useState(PAGE_SIZE);
  const requestSeq = useRef(0);

  const query = params.get("q") ?? "";
  const audience = params.get("audience") ?? "all";
  const type = params.get("type") ?? "all";
  const topic = params.get("topic") ?? "all";
  const sort = params.get("sort") ?? "newest";

  const updateParam = (key: string, value: string, replace = false) => {
    const next = new URLSearchParams(params);
    if (!value || value === "all" || (key === "sort" && value === "newest")) next.delete(key);
    else next.set(key, value);
    setParams(next, { replace });
  };

  const load = async (refresh = false) => {
    const seq = ++requestSeq.current;
    setLoading(true);
    setError(null);
    try {
      const response = await api.getDamnangPosts(refresh);
      if (seq === requestSeq.current) setIndex(response);
    } catch (caught) {
      if (seq === requestSeq.current) {
        setError(isAuthRequiredError(caught)
          ? "Sign in to VT to load the research index."
          : caught instanceof Error ? caught.message : "Could not load the research index.");
      }
    } finally {
      if (seq === requestSeq.current) setLoading(false);
    }
  };

  useEffect(() => {
    void load();
    return () => { requestSeq.current += 1; };
  }, []);

  useEffect(() => { setVisibleCount(PAGE_SIZE); }, [query, audience, type, topic, sort]);

  const topics = useMemo(() => {
    const values = new Set<string>();
    for (const article of index?.articles ?? []) for (const value of article.topics) values.add(value);
    return [...values].sort();
  }, [index]);

  const results = useMemo(() => {
    const filtered = (index?.articles ?? []).filter((article) =>
      matches(article, query) &&
      (audience === "all" || article.audience === audience) &&
      (type === "all" || article.type === type) &&
      (topic === "all" || article.topics.includes(topic)));
    return filtered.sort((a, b) => sort === "oldest"
      ? a.published_at.localeCompare(b.published_at)
      : b.published_at.localeCompare(a.published_at));
  }, [index, query, audience, type, topic, sort]);

  const reset = () => setParams(new URLSearchParams());

  return <div className="min-h-full p-4 sm:p-6 lg:p-8">
    <div className="mx-auto flex max-w-6xl flex-col gap-6">
      <header className="border-b pb-5">
        <div className="mb-3 inline-flex items-center gap-2 rounded-md border px-2.5 py-1 text-xs text-muted-foreground">
          <BookOpen className="h-4 w-4" aria-hidden="true" /> Research source · publisher index
        </div>
        <h1 className="text-2xl font-semibold">Damnang Research</h1>
        <p className="mt-2 max-w-3xl text-sm text-muted-foreground">
          Search the public article index for AI infrastructure and semiconductor research. Titles, tags, and descriptions come from the publisher. Open the original for the article and its access terms.
        </p>
        <div className="mt-4 flex flex-wrap items-center gap-3 text-sm">
          <a className="inline-flex min-h-11 items-center gap-1 underline" href="https://map.damnang.com/" target="_blank" rel="noopener noreferrer">
            Open full archive <ExternalLink className="h-4 w-4" aria-hidden="true" />
          </a>
          <button type="button" className="inline-flex min-h-11 items-center gap-2 rounded-md border px-3 hover:bg-muted disabled:opacity-50" onClick={() => void load(true)} disabled={loading}>
            <RefreshCw className="h-4 w-4" aria-hidden="true" /> Refresh index
          </button>
        </div>
      </header>

      <section aria-label="Article filters" className="grid gap-3 rounded-lg border bg-card p-4 sm:grid-cols-2 lg:grid-cols-5">
        <label className="lg:col-span-2">
          <span className="mb-1 block text-sm font-medium">Search articles and companies</span>
          <span className="relative block"><Search className="pointer-events-none absolute left-3 top-3 h-4 w-4 text-muted-foreground" aria-hidden="true" />
            <input type="search" value={query} onChange={(event) => updateParam("q", event.target.value, true)} placeholder="HBM, packaging, MU…" className="min-h-11 w-full rounded-md border bg-background pl-9 pr-3 text-sm" />
          </span>
        </label>
        <label><span className="mb-1 block text-sm font-medium">Access</span>
          <select value={audience} onChange={(event) => updateParam("audience", event.target.value)} className="min-h-11 w-full rounded-md border bg-background px-2 text-sm">
            <option value="all">All access</option><option value="free">Free</option><option value="paid">Paid</option>
          </select>
        </label>
        <label><span className="mb-1 block text-sm font-medium">Format</span>
          <select value={type} onChange={(event) => updateParam("type", event.target.value)} className="min-h-11 w-full rounded-md border bg-background px-2 text-sm">
            <option value="all">All formats</option><option value="essay">Articles</option><option value="call">Expert calls</option>
          </select>
        </label>
        <label><span className="mb-1 block text-sm font-medium">Topic</span>
          <select value={topic} onChange={(event) => updateParam("topic", event.target.value)} className="min-h-11 w-full rounded-md border bg-background px-2 text-sm">
            <option value="all">All topics</option>{topics.map((value) => <option key={value} value={value}>{value.replace(/-/g, " ")}</option>)}
          </select>
        </label>
      </section>

      <div className="flex flex-wrap items-center justify-between gap-3 text-sm">
        <p role="status">{loading && !index ? "Loading articles…" : `${results.length} of ${index?.count ?? 0} articles`}
          {index && <> · Index {index.status}{index.retrieved_at ? ` · Retrieved ${new Date(index.retrieved_at).toLocaleString("en-US")}` : ""}</>}
        </p>
        <div className="flex items-center gap-3">
          <label className="flex items-center gap-2">Sort
            <select aria-label="Sort articles" value={sort} onChange={(event) => updateParam("sort", event.target.value)} className="min-h-11 rounded-md border bg-background px-2">
              <option value="newest">Newest</option><option value="oldest">Oldest</option>
            </select>
          </label>
          <button type="button" className="min-h-11 rounded-md border px-3 hover:bg-muted" onClick={reset}>Clear filters</button>
        </div>
      </div>

      {index?.status === "stale" && <p role="alert" className="rounded-md border border-amber-500/40 bg-amber-500/10 p-3 text-sm">The publisher could not be reached. Showing the last index retrieved in this VT process.</p>}
      {error && <div role="alert" className="rounded-md border border-red-500/40 bg-red-500/10 p-4 text-sm">
        <p>{error}</p><button type="button" className="mt-2 min-h-11 underline" onClick={() => void load(true)}>Try again</button>
      </div>}
      {!loading && !error && results.length === 0 && <div className="rounded-md border p-6 text-sm">No articles match these filters. <button type="button" className="min-h-11 underline" onClick={reset}>Reset search</button></div>}

      <section aria-label="Damnang articles" className="grid gap-4 md:grid-cols-2">
        {results.slice(0, visibleCount).map((article) => <article key={article.url} className="flex flex-col gap-3 rounded-lg border bg-card p-4">
          <div className="flex flex-wrap items-center gap-2 text-xs text-muted-foreground">
            <time dateTime={article.published_at || article.date}>{dateLabel(article)}</time>
            <span aria-hidden="true">·</span><span>{article.type === "call" ? "Expert call" : "Article"}</span>
            <span className="rounded border px-1.5 py-0.5 font-medium text-foreground">{article.audience === "free" ? "Free" : article.audience === "paid" ? "Paid" : "Access unknown"}</span>
          </div>
          <h2 className="text-lg font-semibold leading-snug"><a className="underline decoration-transparent hover:decoration-current focus:decoration-current" href={article.url} target="_blank" rel="noopener noreferrer">{article.title}</a></h2>
          {article.subtitle && <p className="text-sm">{article.subtitle}</p>}
          {article.bottleneck && <p className="text-sm text-muted-foreground">{article.bottleneck}</p>}
          <div className="mt-auto flex flex-wrap gap-1.5 text-xs">
            {article.tickers.map((value) => <span key={`ticker-${value}`} className="rounded bg-primary/10 px-2 py-1 text-primary">{value}</span>)}
            {article.topics.map((value) => <span key={`topic-${value}`} className="rounded bg-muted px-2 py-1">{value.replace(/-/g, " ")}</span>)}
          </div>
          <a className="inline-flex min-h-11 items-center gap-1 self-start text-sm font-medium underline" href={article.url} target="_blank" rel="noopener noreferrer">Open original <ExternalLink className="h-4 w-4" aria-hidden="true" /></a>
        </article>)}
      </section>
      {visibleCount < results.length && <button type="button" className="min-h-11 self-center rounded-md border px-5 hover:bg-muted" onClick={() => setVisibleCount((count) => count + PAGE_SIZE)}>
        Show more ({results.length - visibleCount} remaining)
      </button>}
    </div>
  </div>;
}
