# Event-driven causal world model — enhancement plan for this project

**Date:** 2026-08-15 · **Input:** ZT's attention-guided causal market world-model design
(30 sections, M_t = {H, G^E, G^C, G^A, X, Q, Π}) · **Supersedes nothing** — this plan
maps the design onto the existing codebase and sequences the build. Objective adopted
verbatim: **max expected investment information gain per unit of computation + human
attention** — which is the N=1 attention-first doctrine already governing this desk.

## 1. Honest gap map: design component → what already exists → what is missing

| Design (§) | Exists today | Missing |
|---|---|---|
| Π_t portfolio state | Fidelity EWY book (short-put/CC ladder in scenario yaml); shadow book 235 positions | machine-readable Π with per-story exposures |
| X_≤t series layer | 10y prices 1.02m rows, PIT fundamentals, OCC, FINRA, flows, macro vintages (ALFRED) | pointers FROM graph INTO series (registry `monitors` is the seed) |
| G^E mechanism graph | actor_series_registry.yaml v0.2 (10 evidenced actors), actor_map nodes/edges w/ falsifiers | hyperedge store; products/contracts as first-class nodes |
| G^C causal + grades | gates G0–G6; C1 evidence chains; story falsifiers; event_study_panels **3,455,348 rows** | edge-grade field (A–H); impulse-response kernels K(h,r) |
| G^A attention graph | TRENDS-ATTN, AAII, ai_release_catalyst_log, GDELT downloader | A_s(t) decay-weighted intensity w/ syndication dedup; story-level attention |
| Q_t market belief | option skew benchmark (gap 7), forecast book | B_s per story from options/curves; wedge W = model − priced |
| Assertion ≠ fact | T1–T4 tiers, HEARSAY label, verbatim-quote gate | Assertion object in a store; headline→assertion pipeline |
| CausalStory object | story schema in ACTOR_CENTRIC plan D/D+ | implementation + status lifecycle + story memory (hot/warm/archive) |
| Bitemporal provenance | OBSERVED_PIT/available_from everywhere; ALFRED vintages | (t_valid, t_known) pair on graph objects; M_{t−} snapshot function |
| Event learning (§15–16) | event_study_panels with next-session formation, survivorship limits | per-edge kernels by horizon×regime; economic-realization joins |
| ExpansionScore (§2) | Phase-B impact×separability ranking | EventIntensity + InfoGain terms; automated candidate scoring |
| VOI data buying (§22) | free-first rule; gap-driven Phase C | VOI formula wired to gaps; paid-trigger ledger |
| 3 loops (§23) | PC1 scheduler ready; B3 gate | loop assignment per job; fast-loop alerting |
| Storage (§24) | append-only backfills; raw retained; catalog | incidence-table hypergraph (SQLite); graphs-as-projections rebuild script |
| Story-first UI (§28) | ASM pages, Chart [GP], actor panels | the wedge table (story × attention × beliefs × exposure × wedge) as page one |
| Anti-spurious feedback (§27) | C5 no-regrade; shadow book marks | dual scoring: PredictionScore vs CausalExplanationScore per closed story |

Two deliberate deviations from the design, per standing rules: **no paid feeds at start**
(LSEG/RavenPack/Polygon are named as paid-tier triggers only — GDELT + EDGAR + official
RSS + retained corpora are the free tier); **no execution layer** (read/analyse/alert
only; Π is observed, never traded by the system).

## 2. Build sequence — six milestones, each gated and dashboard-visible

### W1 (wk 1–2) — Substrate: hyperedge store + bitemporal spine
- `implementation/world_model/store.py`: SQLite with the incidence schema exactly as
  specified (hyperedge_id, node_id, role, edge_type, valid_from, valid_to, known_from,
  known_to, source_id, confidence). No graph DB.
- Canonical objects: Actor, Security, ProductService, Contract, Event, Assertion,
  CausalStory, Source, PortfolioPosition. Entity keys: CIK/ticker now; GLEIF/FIGI
  columns present but back-filled later (free APIs, low priority).
- **Projection rule:** the store is a projection of append-only observations; a
  `rebuild.py` regenerates it from raw so ontology changes never orphan history.
- Seed load: registry v0.2 actors, actor_map edges (w/ falsifiers), the 12 monitors,
  Π from the EWY scenario book definition.
- Gate: B1 enumerated load counts; G2 byte-identical rebuild; M_{t−} snapshot function
  passes a round-trip test (write obs at t1, snapshot t0 excludes it).

### W2 (wk 2–3) — U0 anchors + ExpansionScore
- U0 = EWY top holdings + MU + SOX chain names (from security_master, project_seed).
- One-time structural expansion, 2 hops, from FREE sources only: 10-K Item 1/1A +
  commitments note (customers, suppliers, competitors), 13F/N-PORT holders (from
  retained backfills), index/ETF membership (source_ids already in security_master).
- ExpansionScore implemented as specified; PortfolioExposure from Π, CausalImportance
  from registry impact, EventIntensity from G^A, costs explicit. Threshold set so the
  active graph stays ≤ ~300 nodes; everything else stays candidate.
- Gate: every admitted node carries ≥1 dated primary source (C1); expansion decisions
  logged append-only with their scores (auditable, prunable).

### W3 (wk 3–4) — Live pipeline: Headline → Assertion → Event → Story → Attention
- Free tier: GDELT company-news (script exists), EDGAR full-text + 8-K RSS, official
  IR/newsroom archives (collectors exist), catalyst-log release trackers.
- 11-step update cycle U(M,o) as one `ingest.py` per observation, honouring order:
  entity res → event res (dedup by content-hash + time window: 20 syndications = 1
  event) → story match → attention update → evidence update (T1–T4 rules) → causal
  update (grades, conservative) → expansion → market-reaction stamp → (scenario +
  portfolio deferred to W5) → learning row.
- A_s(t) with source/novelty/prominence weights and per-story τ; stories carry
  emerging→…→realized/refuted lifecycle; hot/warm/archive with reactivation.
- Gate: G5-style eval — 50 hand-labelled headlines; entity-resolution and event-dedup
  accuracy ≥80% (Wilson lower bound) before the pipeline feeds anything downstream.

### W4 (wk 4–6) — Causal grades + kernels from the event panels we already own
- Add `grade` (A–H exactly as specified) to every G^C edge. Contract-mechanism edges
  (A): start with disclosed purchase commitments/take-or-pay from 10-K commitments
  notes of U0 (ACTUS-style event generation deferred; terms+state recorded now).
- Mine `implementation/reports/event_study_panels` (3.45m rows) + earnings-call index
  to fit first K(h,r) kernels for the highest-value edges: memory-price→MU margin,
  capex-guidance→SOX names, export-events→KR names. Horizons 1d/5d/20d/1Q; regime =
  memory_cycle state from the EWY model. HLZ ledger counts every kernel fit.
- Economic realization joins: quarterly fundamentals (retained SEC panels) close the
  Event→ImmediateAR→DelayedAR→Realization chain; dual scoring (Prediction vs
  CausalExplanation) recorded per closed story — a profitable wrong-reason story does
  NOT reinforce its edge.
- Gate: Type-A battery for any kernel claimed as signal; kernels below the bar stay
  descriptive with wide CIs shown.

### W5 (wk 6–8) — Q_t, wedge, and the opportunity detector
- B_s per story from free instruments: option-implied moves/skew (gap-7 code), FRED
  curves, forecast-book ensemble as P_model. Wedge W = Impact_model − Impact_priced
  per story×security×horizon; OpportunityScore with the specified numerator/denominator
  (LiquidityCost proxied by spread/ADV from the panel).
- Alert taxonomy = the §29 alerts, not breaking news: wedge alerts, attention-belief
  divergence alerts, dormant-story reactivation. Delivered via the daily read.
- **Boundary preserved:** output ends at ranked candidates + trade-expression MENU
  (stock/pair/spread listed with rationale); no optimizer executes, ZT decides.
- Gate: every alert row carries story link, evidence chain, wedge arithmetic, and a
  pre-registered evaluation date; alerts enter the forecast book to be Brier-scored.

### W6 (wk 8–10) — Story-first dashboard + story-aware Π view
- Page one becomes the §28 table: Story | Attention | Model belief | Market belief |
  Π exposure | Wedge | Status — rendered from the store, drill-down: evidence →
  causal subgraph → affected names → priced vs modeled → remaining wedge → expression
  menu → Π consequence. Chart [GP] chips already give the chart layer.
- Π decomposition: Exposure_{Π,s} = Σ w_i ∂P_i/∂S_s using kernel betas; shows when
  "diversified" positions are one story bet (the EWY short-put ladder is the pilot).
- All pages keep render-profile rules + full QA harnesses (137 checks) + declaration
  of served actors/stories.

## 3. Three loops mapped to this desk
| Loop | Cadence here | Jobs |
|---|---|---|
| Fast | PC1 scheduler, 15–60 min | GDELT/EDGAR/RSS pulls, dedup, A_s update, wedge-alert check |
| Medium | on-event + daily | story reconstruction, elicitation (3-model ensemble), kernel application, daily read |
| Slow | weekly + earnings rounds | kernel refits, expansion/pruning, registry referee, dual scoring, VOI review |
LLMs sit only in medium/slow. Nothing sub-minute; this desk has no execution path.

## 4. VOI-driven data acquisition (§22, bounded by free-first)
`VOI(source) = E[Δ decision quality on open wedges] / cost`. Computed only for
registry `gaps`. Standing outputs: free candidates auto-queued to Phase-2 lanes
(LBNL queue report, KRX investor-type, TrendForce press releases, N-PORT chain
weights, FINRA short lanes). Paid sources (LSEG MRN, RavenPack, Polygon, OPRA) enter
a **paid-trigger ledger** with the wedge value that would justify them — bought only
when a specific open wedge's VOI clears the subscription cost, per the standing rule.

## 5. Risks and refusals
- **Overbuild risk is the main risk.** Every milestone ships a dashboard-visible
  artifact and is separately killable; W1–W2 alone already improve the actor pages.
- No collapse of G^A into G^C: enforced by schema (assertions cannot create C-graph
  edges above grade F without independent evidence rows) and by the QA harness.
- Look-ahead: bitemporal columns are NOT optional fields; ingest rejects rows without
  known_from. ALFRED discipline extends to news (first_reported_at from feed metadata).
- Attention math on syndicated free news will be noisy; dedup accuracy is gated (W3)
  and the G5 lesson applies — measure the pipeline before trusting it.

## 6. Immediate next actions (this week)
1. W1 store.py + schema + seed load from registry v0.2 (1 day).
2. M_{t−} snapshot test + rebuild.py (0.5 day).
3. U0 freeze from security_master + Π definition from the EWY scenario (0.5 day).
4. Draft the 50-headline eval set for the W3 gate while collectors run (1 day).
5. Stamp this plan + future world_model artifacts into the catalog with
   actor:/story: tags.
