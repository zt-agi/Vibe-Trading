export interface DamnangArticle {
  id: string;
  title: string;
  subtitle: string;
  published_at: string;
  date: string;
  audience: "free" | "paid" | "unknown";
  type: "essay" | "call" | "other";
  url: string;
  topics: string[];
  tickers: string[];
  bottleneck: string;
  verified_at: string;
}

export interface DamnangIndex {
  source: string;
  source_url: string;
  retrieved_at: string;
  status: "live" | "cached" | "stale";
  count: number;
  articles: DamnangArticle[];
  authority: "PUBLISHER_METADATA_ONLY_NON_PIT";
}
