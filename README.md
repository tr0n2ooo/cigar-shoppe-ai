# Smoke Shoppe AI — v0.13

**Live at [cigar.tr0n2ooo.synology.me](https://cigar.tr0n2ooo.synology.me)**

A multi-agent AI analyst for the Smoke Shoppe. Ask questions about sales, inventory, cigar details, and social reputation in plain English — the dispatcher routes each question to the right agents and responds with specific numbers and insights.

---

## Roadmap

The full backlog is tracked on GitHub at [tr0n2ooo/cigar-shoppe-ai](https://github.com/tr0n2ooo/cigar-shoppe-ai/issues) ([project board](https://github.com/users/tr0n2ooo/projects/1)). Issues are organized into four milestones:

### v1.0 — Customer & Ecommerce
Bring the AI to customers and connect it to the online store.
- **Customer-facing recommender UI** *(completed in v0.12)* — customer chat with cigar recommendations based on flavor preferences; no authentication required
- **WordPress/WooCommerce sync** — automated pipeline to keep product listings, stock, and pricing in sync with inventory data
- **Role-based access** — owner / staff / read-only user tiers with per-role tool visibility
- **Mobile-responsive UI** *(completed in v0.12)* — optimized for shop-floor use on phone and tablet
- **REST API layer** — expose agent tools for WooCommerce plugins, mobile apps, and dashboards

### v2.0 — Marketing Automation
Turn the AI's inventory and research knowledge into automated marketing content.
- **Cigar of the Week** — weekly WordPress blog post auto-selected from discount candidates, posted to Facebook and Instagram with long-term memory to prevent quarterly repeats
- **Weekly email digest** — owner-facing sales and reorder summary delivered by email
- **New arrivals social posts** — auto-generate announcements when new SKUs enter inventory
- **Cigar pairing guide generator** — shareable content from research agent data (drinks, foods, occasions)
- **Monthly newsletter** — customer-facing email with top sellers, new arrivals, and a brand spotlight

### v3.0 — Deep Analytics
Deepen business intelligence and enrich the owner UI.
- **Sales forecasting** — predict 30/60/90-day demand by SKU using historical velocity and seasonality
- **Bundle & promotion generator** — identify SKUs that co-sell and suggest bundle pricing
- **Customer loyalty analysis** — repeat buyer patterns and brand preferences from transaction data
- **Price optimization** — flag margin outliers and velocity-based pricing opportunities
- **Saved/pinned queries** — one-click shortcuts for common owner workflows
- **PDF/Excel report export** — download any agent response as a formatted report

### v4.0 — Operations & Scale
Streamline operations and enable growth beyond a single shop.
- **Vendor reorder email drafts** — auto-compose purchase orders after reorder signal generation
- **POS integration** — replace manual XLSX uploads with live point-of-sale data sync
- **Automated XLSX refresh** — scheduled pull from a shared drive or Dropbox
- **Barcode scanner inventory count** — mobile UPC scanning for physical stock checks
- **Multi-tenant SaaS mode** — deployable for other cigar and tobacco shops
- **Monitoring & uptime alerting** — health-check endpoint + Uptime Kuma integration
- **Automated data backup** — scheduled backup of `data/` to a secondary storage location

---

## Changelog

### v0.13 (2026-06-25)
**Prompt caching and demo/verbose terminal logging**

- **Prompt caching** — `system` prompts in `dispatcher_agent.py` and `sales_agent.py` converted from plain strings to content-blocks lists with `cache_control: {"type": "ephemeral"}`, enabling Anthropic's prompt cache. Cache hits cost ~10% of normal input token price; manager mode (large tool schemas + long system prompt) comfortably exceeds the 4,096-token Haiku minimum. Per-call token and cache stats appear at `LOG_LEVEL=DEBUG`.

- **Demo/verbose terminal logging** — set `LOG_LEVEL=INFO` (or `LOG_LEVEL=DEBUG`) in `.env` to activate structured terminal narration of all key agentic activity, designed for live demos and class walkthroughs:

  - **Dispatcher routing** — a `[DISPATCHER]` line for every tool Claude selects, showing the tool name, the MCP server module it routes to (illustrating cross-agent dispatch), and key inputs. Distinguishes first-turn routing calls from post-tool synthesis calls.
  - **Claude reasoning labels** — every `_create_with_backoff()` call carries a `_label` that prints immediately before the API call, explaining *why* Claude is being invoked — e.g. `"ToT branch evaluation: CONSERVATIVE strategy — selecting 3 new cigar(s) from 22 candidates"`. Covers all 9 call sites: dispatcher, sales agent, inventory agent, ordering agent (restock prioritization, branch evaluation ×3, synthesis), cigar researcher (full research, size-only), and social intel agent (reputation research, buzz feed discovery).
  - **ToT phase narration** — `_terminal_verbose_printer` auto-wired to `ordering_agent.set_verbose_callback()` when `LOG_LEVEL` is set; streams `[ToT MEMORY]`, `[ToT CANDIDATES]`, `[ToT BRANCH]`, `[ToT SYNTHESIS]`, and `[ToT RECORD]` phase events with emoji icons and structured bullet lists of picks.
  - **RAG pipeline trace** — `[RAG]` log lines at each stage: query string + index size, broad cosine retrieval candidate count, MMR re-ranking (λ value, relevance/diversity percentages, before/after names), and final selected cigars after BGE reranking.
  - **Agentic memory trace** — `[MEMORY]` log lines in `decision_memory.py` when past runs are loaded (count, filename), and a bullet-by-bullet listing of the feedback being injected into the ToT synthesis prompt.

### v0.12 (2026-06-11)
**Customer-facing UI, mobile polish, and chart/theme fixes**

- **Dual-mode UI** — the app now opens to a customer-facing recommender chat with no authentication required. Customers ask for cigar recommendations by preference, strength, or budget; the dispatcher uses only research, social reputation, and inventory stock-check tools — no financial or operational data is exposed. Type `/manager` to authenticate with existing `UI_USERNAME`/`UI_PASSWORD` credentials and switch to the full store-manager view; type `/customer` to return.
- **Mobile-responsive CSS** — `public/theme.css` now includes phone (≤768px) and tablet (769–1024px) media queries: horizontal scroll on wide tables and code blocks, full-width message area overriding Chainlit's wide-layout padding, `font-size: 16px` on textarea to prevent iOS auto-zoom, 44px minimum touch targets on buttons, and Plotly chart containers capped at 100% width.
- **Chart color fix** — all Plotly charts now render with legible colors on the dark theme. Previously `_FONT` used `#333333` (near-black) and titles used `_BROWN` (`#4A2C17`), both invisible on the dark `#1a1410` background. Charts now have explicit dark cedar backgrounds (`#1e1610` / `#241c15`), parchment text (`#f0e6d0`), walnut grid lines (`#3d2f1e`), and amber titles.
- **Composer theme fix** — the text input area now uses theme-aware colors: cream background with dark text in light mode; dark cedar background with parchment text in dark mode. Scoped via `html.dark` (Chainlit's Tailwind class) and `@media (prefers-color-scheme: dark)`.
- **`CHAINLIT_AUTH_SECRET`** is no longer required — the global Chainlit password gate has been removed. `UI_USERNAME` and `UI_PASSWORD` are still used for the inline `/manager` login.

### v0.11 (2026-06-10)
**Inline charts in the Chainlit UI**
- `chart_generator.py` — parses structured tool outputs and produces Plotly figures; returns `None` gracefully for tools that don't warrant a chart
- Charts render automatically in the chat UI after the relevant tool completes, with no user action required
- **Inventory charts** (auto-generated): days-of-stock remaining by urgency tier (`get_reorder_signals`), YTD profit by SKU with stock-adequacy color coding (`get_top_profitable`), months of excess stock (`get_slow_movers`), capital tied up in dead stock (`get_discontinue_candidates`), 4-panel summary (`get_full_inventory_report`)
- **Sales charts** (dispatcher-callable): top brands by YTD revenue (`get_top_brands_chart`), monthly revenue + unit trend dual-axis (`get_revenue_trend_chart`) — new tools added to `sales_server.py`
- All charts use the Smoke Shoppe amber/brown palette with hover tooltips; rendered via `cl.Plotly` (interactive, not static images)

### v0.10 (2026-06-10)
**Performance — speed optimizations**
- **Parallel ToT branches** — the three ordering-agent thought branches (`conservative`, `balanced`, `adventurous`) now run concurrently via `ThreadPoolExecutor(max_workers=3)`; each branch is an independent Claude call with no shared mutable state, so parallelization is safe and cuts branch time by ~⅔ (~45 s sequential → ~15 s concurrent)
- **Selective model routing** — lighter tasks switch to `claude-haiku-4-5` (3–5× faster inference): dispatcher routing (`dispatcher_agent.py`), SQL generation and sales analytics (`sales_agent.py`), and inventory summarization (`inventory_agent.py`); heavier reasoning tasks (ordering branches and synthesis) stay on `claude-sonnet-4-6`
- **Branch `max_tokens` trimmed** — ordering branch token limit reduced from 3000 → 1500; branches rarely exceed 1200 tokens in practice, so this shaves generation latency with negligible quality impact

### v0.9 (2026-06-09)
**Agentic AI Course Concepts (CMU Capstone)**
- **Long-term memory** (`decision_memory.py`) — every completed order run is persisted to `data/Order_History.json`; the next run loads that history, evaluates each past pick against actual transaction data, and injects a performance-feedback block into the ToT synthesis prompt so the agent learns from its own decisions over time
- **RAG layer** (`research_rag.py`) — semantic search over `Cigar_Research.xlsx` using ChromaDB with `all-MiniLM-L6-v2` embeddings; retrieval uses MMR (Maximal Marginal Relevance, λ=0.7) to balance relevance against diversity; optional BGE cross-encoder reranking (sentence-transformers `ms-marco-MiniLM-L-6-v2`) applied when available
- **New MCP tools** on `research_server` (port 8001): `search_similar_cigars`, `rag_index_status`, `rebuild_rag_index`
- **`--verbose` demo mode** on the ordering CLI — streams a behind-the-scenes narrative (🧠 memory, 📋 candidates, 🌿 ToT branches, ⚖️ synthesis, 💾 record) as the agent works; all events also stored in `result["_verbose_events"]` for programmatic access
- **`_verbose_events`** included in every `generate_order_recommendation` result dict

### v0.8 (2026-06-07)
**Performance & UX**
- Animated per-tool `cl.Step` feedback in the chatbot — each tool shows a spinner while running, then a checkmark + result snippet when done
- Cigar-themed step labels (🔬 blend research, 🏪 humidor check, 📦 reorder signals, 💰 money-makers, etc.)
- Parallel tool execution in the dispatcher — when Claude requests multiple tools in one turn (e.g. research + social + stock check) they now run concurrently via `ThreadPoolExecutor`, cutting wall-clock time proportionally
- Rate-limit warning step: when the Anthropic API throttles a request, a `⏳ API rate limit — retrying in Xs` step appears so the user knows what's happening
- Rate-limit backoff (`_create_with_backoff`) now covers **all** Claude API callers — dispatcher, inventory summarizer, and sales agent were previously unprotected

**Inventory**
- Minimum Level and Reorder Quantity spreadsheet fields removed from all reorder logic — replaced by velocity-derived computed fields
- `markup_pct` replaced by `margin_pct` computed from first principles
- Thread-safe DuckDB: `threading.Lock` guards all `.execute()` calls in `tools/inventory_tool.py`

**Data & house brand logic**
- ASW → SS rename in all 9 item descriptions
- House brand exclusion narrowed: only `Brand = "Smoke Shoppe"` skips external research
- Custom logo fix: `public/logo_dark.svg` / `public/logo_light.svg` added

**Deployment**
- Docker image built for `linux/amd64`; deployed to Synology NAS via Container Manager
- Reverse-proxied at `cigar.tr0n2ooo.synology.me` (Synology DDNS + Let's Encrypt)

### v0.7 (2026-06-06)
Natural-language dispatcher routing chat messages to all specialist tools. Auto-discovers every `*_server.py` tool via FastMCP introspection. Added cigar research + social reputation + inventory stock lookups to chat UI. Password auth via Chainlit. Docker packaging.

### v0.6
Ordering agent restock integration, budget control, parent company grouping.

### v0.5
Inventory Analysis Agent — four SQL-backed analyses (reorder signals, slow movers, discontinue candidates, top profitable).

### v0.4
Ordering agent (Tree of Thought). Verified inventory file.

### v0.3
Social media intelligence agent.

### v0.2
Data cleaning. Cigar research agent with local XLSX cache.

### v0.1
Sales analyst agent. DuckDB over transactions. FastMCP server.

---

## Agentic AI Design Patterns

This project implements several patterns from the CMU course on agentic AI. Each pattern is wired into the live system, not just described.

### ReAct (Reasoning + Acting)

The ordering agent runs two interleaved ReAct loops:

**Loop 1 — New cigar stocking decision**

| Step | What happens |
|------|-------------|
| Observe | Load long-term memory: past decisions + their sales outcomes |
| Reason | "What are the patterns in what sold vs. what didn't?" |
| Act | Retrieve buzz-feed candidates; filter already-in-stock |
| Observe | Enrich each candidate with fit-profile scores |
| Reason | Three independent thought branches evaluate (see Tree of Thought) |
| Act | Synthesize branches into a final order recommendation |
| Observe | Record the decision; future runs will observe its outcome |

**Loop 2 — Restock decision**

| Step | What happens |
|------|-------------|
| Observe | Query inventory for items at or near stockout |
| Reason | "Which items are selling fast enough to reorder? What's seasonal?" |
| Act | Produce a prioritized restock list with seasonality-adjusted quantities |

The ReAct loop structure is visible in `--verbose` mode, which narrates each step as it executes.

### Tree of Thought (ToT)

The new-cigar section of every order run uses Tree of Thought, implemented in `ordering_agent.py`:

1. **Thought generation** — three independent branches (`conservative`, `balanced`, `adventurous`) each evaluate the full candidate pool using a different strategy weighting (fit vs. buzz score ratio)
2. **Branch evaluation** — each branch is a separate Claude call with a branch-specific system prompt; branches run **concurrently** via `ThreadPoolExecutor(max_workers=3)` since they share no mutable state; branches are pruned by a 70-point threshold on fit and buzz scores
3. **DFS state management** — the `craziness` parameter (0–10) controls branch spread; the `OrderingAgent` class holds short-term state (hypothetical order being built) across branch iterations
4. **Synthesis / voting** — a final synthesis step reviews all three branch outputs and resolves conflicts by consensus; high-conviction items appear in 2–3 branches

The `--craziness` flag controls branch diversity: at craziness=0 all three branches stay conservative; at craziness=10 the adventurous branch chases pure buzz score regardless of fit.

### Long-term Memory

Implemented in `decision_memory.py`:

- **Write** — `record_recommendation(result)` appends each completed order to `data/Order_History.json` with the recommended cigars, date, and budget
- **Read + evaluate** — `load_feedback_summary()` compares past picks against `Smoke_Shoppe_Transactions.xlsx` using fuzzy name matching; returns a performance block ("Perdomo BBA Mad. Churchill: ✓ good seller: 635 units, $6,674 revenue")
- **Injection** — the feedback block is prepended to the ToT synthesis prompt, instructing Claude to avoid repeating poor performers and favor proven winners
- **Feedback loop** — over multiple runs, the agent observes its own past decisions, evaluates their real-world outcomes, and adjusts future recommendations accordingly

### RAG (Retrieval-Augmented Generation)

Implemented in `research_rag.py`, backed by `data/Cigar_Research.xlsx` (55 researched SKUs):

| Stage | Implementation |
|-------|---------------|
| **Indexing** | Each cigar row is converted to a dense text document (brand, wrapper, strength, flavor notes, MSRP, rating) and embedded via `all-MiniLM-L6-v2` into a ChromaDB collection persisted at `data/chroma_research/` |
| **Retrieval** | Cosine similarity query over-fetches 3× the requested results (k×3 candidates) |
| **MMR re-ranking** | Maximal Marginal Relevance (λ=0.7) iteratively selects results that maximize `λ × relevance − (1−λ) × redundancy`, promoting diversity across wrapper styles, strengths, and origins |
| **BGE reranking** | Cross-encoder (`ms-marco-MiniLM-L-6-v2`) jointly scores each `(query, document)` pair for precision; falls back gracefully if `sentence-transformers` is not installed |

**Why SQL for inventory/transactions and RAG for research?** The sales and inventory data are relational — exact joins, date ranges, aggregations. SQL handles those perfectly and returns precise answers. The research data is unstructured text (flavor notes, blend descriptions, origin stories) — those require semantic similarity, not exact match.

### Multi-Agent Architecture

Six specialized agents, each with a dedicated MCP server, routed by a natural-language dispatcher:

```
dispatcher_agent.py  (auto-discovers all *_server.py tools at startup)
       │
   ┌───┼────────────┬─────────────┬──────────────┬──────────────┐
   ▼   ▼            ▼             ▼              ▼              ▼
 sales research  social_intel  ordering       inventory
(8000) (8001)    (8002)        (8003)         (8004)
```

Adding a new `*_server.py` automatically makes its tools available in the dispatcher with no code changes — the dispatcher introspects FastMCP tool schemas at startup.

### MCP (Model Context Protocol)

All six servers expose tools via FastMCP. MCP decouples the tool implementation from the agent that uses it — any MCP client (Claude Desktop, another agent, a Chainlit UI) can call these tools without knowing the implementation details. This is how the dispatcher and the ordering agent share inventory and research tools without circular imports.

---

## Architecture

```
Chainlit UI (ui.py)
        │
        ▼
dispatcher_agent.py  ── natural-language dispatcher (Anthropic SDK agentic loop)
        │
        │  auto-discovers all *_server.py tools at startup via FastMCP introspection
        │
   ┌────┬────────────┬──────────────┬──────────────┬──────────────┐
   ▼    ▼            ▼              ▼              ▼              ▼
sales  research   social_intel  ordering       inventory
server server     server        server         server
(8000) (8001)     (8002)        (8003)         (8004)
   │       │           │             │              │
   ▼       ▼           ▼             ▼              ▼
sales  cigar_      social_       ordering_      inventory_
agent  researcher  intel_agent   agent          agent
                   │             │
               Cigar_Social  decision_memory.py  ← long-term memory
               Cigar_Buzz    research_rag.py     ← RAG / ChromaDB
                             │
                         data/chroma_research/   ← persistent vector index

ordering_agent.py  — Tree of Thought (conservative / balanced / adventurous branches)
        │
   ┌────┴──────────────┐
   ▼                   ▼
Cigar_Buzz.xlsx    inventory_agent.analyze_reorder()
(new SKU           (low-stock reorder signals)
 candidates)
```

**Key design decisions:**
- **Dispatcher agent** auto-discovers every `*_server.py`'s tools at startup — adding a new server requires no changes to the dispatcher
- **Anthropic SDK directly** for all agent loops — no third-party agent framework
- **Claude native web search** (`web_search_20250305`) for all research — no Brave/Serp keys needed
- **DuckDB** for all structured data access — agents write SQL; only result rows enter context, never full DataFrames
- **SQL for relational data, RAG for semantic data** — inventory/transactions use DuckDB; cigar research text uses ChromaDB with MMR + BGE reranker
- **Long-term memory** persists order decisions across sessions and evaluates them against actual sales
- **Chainlit** for the web UI — themed with the Smoke Shoppe brand (dark amber/gold palette)
- **Graceful degradation** — Reddit, YouTube, and BGE reranker are optional; all agents work without them
- **Tree of Thought ordering** — three branches run in parallel via `ThreadPoolExecutor`; synthesized into a final recommendation with vitola, box quantity, and wholesale cost estimates
- **Selective model routing** — `claude-haiku-4-5` for routing, SQL generation, and summarization; `claude-sonnet-4-6` reserved for ordering branches and synthesis where reasoning quality matters

---

## Data

All data files live in `data/`:

| File / Directory | Description |
|------|-------------|
| `Smoke_Shoppe_Transactions.xlsx` | 124,256 sales rows, 20 columns (2023–2026) |
| `Smoke_Shoppe_Inventory.xlsx` | 3,713 inventory items, 37 columns (source of truth) |
| `Smoke_Shoppe_Inventory_Verified.xlsx` | Verified copy: items never in transactions zeroed; negatives clamped; `Discontinued` + `Discontinued Reason` columns added |
| `Brand_Reference.xlsx` | 248-row brand → parent company lookup (used for order grouping) |
| `Cigar_Research.xlsx` | Per-SKU: wrapper, binder, filler, flavor notes, MSRP/MAP, ratings — also indexed into ChromaDB for RAG |
| `Cigar_Social.xlsx` | Per-SKU: overall/quality/value/community scores, top quotes, sources |
| `Cigar_Buzz.xlsx` | New/upcoming cigars: buzz score, sentiment, summary, release status |
| `Order_History.json` | Long-term memory: past order recommendations with date, budget, picks |
| `chroma_research/` | ChromaDB vector index over Cigar_Research.xlsx (auto-built on first RAG query) |

---

## Quickstart

### 1. Install dependencies

```bash
uv sync
# or: pip install anthropic chainlit chromadb duckdb mcp numpy openpyxl pandas plotly reportlab sentence-transformers
```

### 2. Set environment variables

```bash
# Required
export ANTHROPIC_API_KEY="sk-ant-..."

# Optional — Reddit community data (structured upvote/comment metrics)
export REDDIT_CLIENT_ID="..."
export REDDIT_CLIENT_SECRET="..."

# Optional — YouTube video data
export YOUTUBE_API_KEY="..."
```

All agents work without Reddit/YouTube keys — Claude's web search covers those sources as a fallback.

### 3. Build the verified inventory (first-time setup)

```bash
python main.py verify-inventory
```

### 4. Run the chatbot UI

```bash
python main.py ui
```

Opens at [http://localhost:8000](http://localhost:8000).

---

## All commands

```bash
# ── Chatbot UI ──────────────────────────────────────────────────────────────
python main.py ui                              # Chainlit UI on port 8000

# ── Sales analyst ───────────────────────────────────────────────────────────
python main.py server                              # MCP server (stdio)
python main.py server --transport sse --port 8000  # MCP server (HTTP/SSE)
python main.py query "What were last month's top 10 products by revenue?"

# ── Cigar research agent ─────────────────────────────────────────────────────
python main.py research-server                              # MCP server (stdio)
python main.py research-server --transport sse --port 8001  # MCP server (HTTP/SSE)
python main.py research "Perdomo BBA Mad. Churchill" "Perdomo"  # research one cigar
python main.py research --batch                  # populate Cigar_Research.xlsx for all inventory
python main.py research --batch --limit 20       # first 20 uncached items only
python main.py research --batch --force          # re-research even if already cached
python main.py research --batch --since "last 6 months"        # only cigars sold recently
python main.py research --batch --since "last 6 months" --top  # highest balanced qty+revenue first
python main.py research --status                 # show cache coverage

# ── RAG (semantic cigar search) ──────────────────────────────────────────────
python research_rag.py search "bold maduro with chocolate under $20"  # semantic search
python research_rag.py search "medium Connecticut wrapper creamy" --k 3 --lambda 0.8
python research_rag.py rebuild                   # force rebuild of ChromaDB index
python research_rag.py status                    # show index stats

# ── Social intelligence agent ────────────────────────────────────────────────
python main.py social-server                              # MCP server (stdio)
python main.py social-server --transport sse --port 8002  # MCP server (HTTP/SSE)
python main.py social "Perdomo BBA Mad. Churchill" "Perdomo"  # reputation for one cigar
python main.py social --batch                    # populate Cigar_Social.xlsx for all inventory
python main.py social --batch --limit 20         # first 20 uncached items only
python main.py social --batch --force            # re-research even if already cached
python main.py social --batch --since "last 6 months"        # only cigars sold recently
python main.py social --batch --since "last 6 months" --top  # highest balanced qty+revenue first
python main.py social --buzz                     # refresh Cigar_Buzz.xlsx (new/upcoming cigars)
python main.py social --buzz --max-searches 4    # quick/cheap sweep (fewer API calls)
python main.py social --buzz --target 25         # request 25 buzz cigars (default)
python main.py social --buzz --since-months 3    # cigars announced in the last 3 months (default)
python main.py social --buzz --craziness 0       # safe mode: high-fit cigars only
python main.py social --buzz --craziness 10      # wild mode: pure buzz, ignore store fit
python main.py social --buzz --no-fit            # skip fit scoring entirely (faster)
python main.py social --status                   # show cache coverage + API config

# ── Ordering agent ────────────────────────────────────────────────────────────
python main.py order-server                              # MCP server (stdio)
python main.py order-server --transport sse --port 8003  # MCP server (HTTP/SSE)

# Defaults: 30-day horizon, $5,000 budget, 10% new cigars / 90% restock
python main.py order

# Demo / verbose mode — streams behind-the-scenes narration as the agent works
python main.py order --verbose
python main.py order --verbose --slots 2 --craziness 3

# Planning horizon — scales the default budget and stockout-risk window
python main.py order --horizon 7               # 7-day horizon (~$1,167 budget)
python main.py order --horizon 90              # 90-day horizon (~$15,000 budget)

# Budget control
python main.py order --budget 3000             # explicit $3,000 total budget
python main.py order --new-cigar-pct 20        # 20% new cigars, 80% restock (default 10%)
python main.py order --new-cigar-pct 0         # restock only — skip new-cigar analysis
python main.py order --new-cigar-budget 500    # fixed $500 for new cigars; rest to restock

# New cigar selection
python main.py order --refresh                 # force buzz feed refresh before analyzing
python main.py order --stale-months 1          # auto-refresh if cache is older than 1 month
python main.py order --stale-months 0          # disable auto-refresh (always use cache as-is)
python main.py order --slots 5                 # recommend 5 new SKUs (default 3)
python main.py order --pool 30                 # consider top 30 buzz candidates (default 25)
python main.py order --craziness 7             # more adventurous branching (default 5, range 0-10)
python main.py order --max-price 22            # filter out cigars above $22/stick MSRP
python main.py order --json                    # output raw JSON instead of pretty-print

# Export
python main.py order --export xlsx             # → exports/order_YYYY-MM-DD.xlsx
python main.py order --export pdf              # → exports/order_YYYY-MM-DD.pdf
python main.py order --export both             # → both formats in one run
python main.py order --export xlsx --export-path /tmp/order.xlsx  # explicit path

# ── Inventory agent ───────────────────────────────────────────────────────────
python main.py inventory-server                              # MCP server (stdio)
python main.py inventory-server --transport sse --port 8004  # MCP server (HTTP/SSE)

python main.py inventory --low-stock             # items selling but OOS / below-min / stockout risk
python main.py inventory --low-stock --days 7    # tighten stockout window to 7 days (default 30)
python main.py inventory --low-stock --days 90   # widen to 90-day lookahead
python main.py inventory --low-stock --top 10    # limit to top 10 (default: all)
python main.py inventory --low-stock --min-ytd 5 # require 5+ units sold YTD to count as "selling"

python main.py inventory --slow-movers           # excess stock candidates for discounting
python main.py inventory --discontinue           # dead stock candidates (seasonality-aware)
python main.py inventory --profitable            # top profitable items to push in selling
python main.py inventory --all                   # run all four analyses

python main.py inventory --category "Cigars"     # filter by category (default: Cigars)
python main.py inventory --summarize             # add Claude natural-language interpretation
python main.py inventory --json                  # output raw JSON

# Manual inventory decisions:
python main.py inventory --mark-discontinued "Macanudo Ascots" --reason "Vendor dropped SKU"
python main.py inventory --mark-discontinued "All Los Statos" --reason "Line discontinued"
python main.py inventory --re-enable "Sobremesa El Americano"

# ── Inventory verification ───────────────────────────────────────────────────
python main.py verify-inventory                  # build Smoke_Shoppe_Inventory_Verified.xlsx
python main.py verify-inventory --summary        # print stats only, no file written
```

---

## Ordering agent details

Every order run covers two things at once: **restocking low-stock items** and **recommending new SKUs** to try. Budget is split between them.

### Long-term memory feedback loop

Before any analysis begins, the ordering agent loads `data/Order_History.json` and evaluates each past recommendation against actual sales data:

1. Past picks are fuzzy-matched against `Smoke_Shoppe_Transactions.xlsx` by cigar name
2. Match quality (units sold, total revenue) is converted to a plain-text feedback block
3. The block is injected into the ToT **synthesis** prompt — Claude is explicitly told to avoid repeating poor performers and favor proven winners
4. After the run completes, the new recommendation is appended to `Order_History.json`

Over multiple runs, this creates a genuine learning loop: the agent observes the outcomes of its own decisions and adjusts future recommendations accordingly. This is the **long-term Observe step** in the cross-session ReAct loop.

### Budget and horizon

The default budget is **$5,000 × (horizon_days / 30)**, so a 7-day run defaults to ~$1,167 and a 90-day run to $15,000. Pass `--budget` to override.

`--new-cigar-pct` (default 10) controls how the budget is divided:

| Scenario | Budget split |
|----------|-------------|
| Default | 10% new cigars, 90% restock |
| `--new-cigar-pct 0` | 100% restock, new-cigar analysis skipped |
| `--new-cigar-pct 100` | 100% new cigars, restock signals still shown but not budgeted |
| `--new-cigar-budget 500` | Fixed $500 for new cigars; remainder goes to restock |

**Budget exhaustion guard:** if total restock demand meets or exceeds the full order budget, the entire budget is allocated to restock and the new-cigar Tree of Thought analysis is skipped.

### Restock section

At the start of every run, the ordering agent calls the inventory agent's `analyze_reorder()`:

1. **Annotates costs** using actual `Cost` values from the inventory (not MSRP estimates)
2. **Rounds up to whole boxes** using a brand-keyed lookup table (Padrón 26/25/10, Oliva 24, My Father 23, Arturo Fuente 25, etc.)
3. **Applies seasonality** — prior-year same-window sales are used to scale reorder quantities (factor clamped to 0.4×–3.0× baseline velocity)
4. **Prioritizes when over budget** — Claude selects the highest-value subset with per-item reasoning

### New-cigar Tree of Thought

1. Load the buzz feed (`Cigar_Buzz.xlsx`) — new/upcoming cigars with social excitement scores
2. Filter out any cigars already in stock (fuzzy-matched against `Smoke_Shoppe_Inventory_Verified.xlsx`)
3. Enrich each candidate with a fit profile (wrapper, strength, vitola, price, brand — scored against proven sales patterns)
4. Run three independent evaluation branches **in parallel** (`ThreadPoolExecutor(max_workers=3)`):
   - **Conservative** — proven fit required (fit 75%, buzz 25%)
   - **Balanced** — equal weight to fit and social momentum (50/50)
   - **Adventurous** — chases buzz; accepts profile mismatches (buzz 70%, fit 30%)
5. Inject long-term memory feedback into synthesis
6. Synthesize branches into a final ranked recommendation with vitola, box quantity, and estimated wholesale cost

**Craziness (0–10):** at 5 (default), branches run at conservative=2, balanced=5, adventurous=8.

**Recency weighting:** recently announced cigars receive a scoring bonus (≤14 days: +25pts, 15–45 days: +15pts, 46–90 days: +8pts, 91–180 days: +3pts).

### Demo mode (--verbose)

`--verbose` streams a real-time behind-the-scenes narrative as the agent works:

```
═══════════════════════════════════════════════════════════════════════
  🎓  DEMO MODE — BEHIND THE SCENES
  Narrating each agentic step as it happens.
  ReAct loop │ Tree of Thought │ Long-term Memory │ RAG
═══════════════════════════════════════════════════════════════════════

🧠  [MEMORY]  +0.0s
   Loading past order decisions from long-term memory…
   file: data/Order_History.json

🧠  [MEMORY]  +0.1s
   Found past decisions — performance feedback injected into synthesis.
      • Perdomo BBA Mad. Churchill (Perdomo): ✓ good seller: 635 units, $6674 revenue
      • Oliva Serie V Melanio (Oliva): ✓ good seller: 971 units, $12542 revenue

📋  [CANDIDATES]  +1.3s
   15 candidates ready for evaluation (pool=25, already-in-stock filtered out).
      • AJ Fernandez Enclave (buzz=94)
      • Rocky Patel Vintage 1990 (buzz=87)
      • My Father El Centurion (buzz=85)

🌿  [BRANCH]  +1.3s
   Starting CONSERVATIVE branch — proven fit required…

🌿  [BRANCH]  +1.3s
   Starting BALANCED branch — equal weight to fit and buzz…

🌿  [BRANCH]  +1.4s
   Starting ADVENTUROUS branch — chasing buzz score…

🌿  [BRANCH]  +14.2s
   CONSERVATIVE branch complete — 2 selection(s).
      • Rocky Patel Vintage 1990 [high confidence]

🌿  [BRANCH]  +14.9s
   BALANCED branch complete — 2 selection(s).

🌿  [BRANCH]  +15.6s
   ADVENTUROUS branch complete — 2 selection(s).

⚖️   [SYNTHESIS]  +15.6s
   Synthesizing 3 branches into final recommendation (with long-term memory feedback).

⚖️   [SYNTHESIS]  +23.4s
   Synthesis complete — 2 cigar(s) recommended.
      • Rocky Patel Vintage 1990 — Toro [high conviction, agreed by: conservative, balanced, adventurous]

💾  [RECORD]  +35.2s
   Recommendation saved to long-term memory (data/Order_History.json).

═══════════════════════════════════════════════════════════════════════
  📋  RECOMMENDATION OUTPUT
═══════════════════════════════════════════════════════════════════════
[normal output follows]
```

All events are also stored in `result["_verbose_events"]` as a list of dicts for programmatic access.

### Export to XLSX / PDF

After any order run, add `--export xlsx`, `--export pdf`, or `--export both` to save a formatted purchase-order document:

```
python main.py order --export xlsx
# → exports/order_2026-06-10.xlsx

python main.py order --export pdf
# → exports/order_2026-06-10.pdf

python main.py order --export both --export-path /tmp/order
# → /tmp/order.xlsx  and  /tmp/order.pdf
```

**XLSX** (via `openpyxl`) — four sheets: *Summary* (run parameters + cost breakdown), *New Cigars* (ranked recommendations with conviction, branches, vitola, cost), *Restock* (low-stock items with urgency, velocity, box quantities), *By Vendor* (consolidated PO grouped by parent company with subtotals).

**PDF** (via `reportlab`) — single printable document with the same four sections, formatted as a purchase order. Suitable for printing or emailing to a distributor rep.

The export module (`order_export.py`) also has a public API:

```python
from order_export import to_xlsx, to_pdf
xlsx_path = to_xlsx(result)          # result from generate_order_recommendation()
pdf_path  = to_pdf(result, "/tmp/")  # save to a specific directory
```

### Order grouped by parent company

The output includes an `order_by_parent_company` section that combines both restock and new-cigar items under each vendor, sorted by total wholesale cost. This makes it straightforward to build one PO per vendor.

---

## RAG details

The research RAG layer (`research_rag.py`) provides semantic search over the 55-cigar research database.

### Why RAG here, not SQL?

Cigar research data is unstructured text — flavor notes like "rich caramel sweetness and warming spice" or "dark chocolate and earthiness with a cedar spine" don't map to SQL predicates. A query like "find me something similar to a Padron 1964 but lighter" requires semantic understanding, not exact matching.

### Retrieval pipeline

1. **Dense retrieval** — query is embedded by `all-MiniLM-L6-v2`; ChromaDB returns top 3k results by cosine similarity
2. **MMR re-ranking** — Maximal Marginal Relevance iteratively selects the next result that maximizes `λ × relevance − (1−λ) × max_similarity_to_selected`. At λ=0.7, relevance wins most of the time but the algorithm avoids returning five nearly-identical Connecticut-wrapper results when variety is possible
3. **BGE cross-encoder reranking** — `ms-marco-MiniLM-L-6-v2` jointly scores each `(query, document)` pair, capturing interaction between query and document that the bi-encoder embedding misses; applied when `sentence-transformers` is installed

### Index management

```bash
python research_rag.py rebuild    # rebuild after updating Cigar_Research.xlsx
python research_rag.py status     # show how many cigars are indexed
```

The index persists at `data/chroma_research/` and is loaded automatically on first query.

---

## Inventory agent details

The inventory agent (`inventory_agent.py`) answers four key stock-health questions entirely via SQL — no LLM is needed for the data layer. An optional `--summarize` flag adds a Claude interpretation.

**All YTD/MTD figures come from `Smoke_Shoppe_Transactions.xlsx`**, not from the inventory file's POS-exported columns.

### Analyses

| Analysis | What it finds | Key threshold |
|----------|--------------|---------------|
| `--low-stock` | Items actively selling but OOS, below minimum, or likely to run out soon | `--days N` stockout window (default 30) |
| `--slow-movers` | Items with stock sitting but very slow velocity — discount candidates | ≤ 1 unit/month |
| `--discontinue` | Dead stock with ≤ 2 units sold YTD — seasonality-filtered | ≤ 2 YTD units |
| `--profitable` | Top items by YTD gross profit | top 25 by default |

### Seasonality (discontinue + slow movers)

Both analyses compare current-year performance against the **same calendar window** in the prior year. Items whose prior-year sales show a clear back-half seasonal pattern are automatically excluded from discontinue candidates.

---

## MCP integration

All servers implement the [Model Context Protocol](https://modelcontextprotocol.io) via FastMCP.

**Sales analyst tools** (`sales_server.py`, port 8000):
| Tool | Description |
|------|-------------|
| `query_xlsx` | Natural-language Q&A over transactions and inventory data |
| `describe_xlsx` | Fast structural overview — no LLM call |
| `analyze_fit_profile` | Score a candidate cigar against the store's proven sales profile |
| `get_top_brands_chart` | Top brands by YTD revenue as chart-ready JSON (rendered as bar chart in UI) |
| `get_revenue_trend_chart` | Monthly revenue + units trend as chart-ready JSON (dual-axis line/bar in UI) |

**Cigar research tools** (`research_server.py`, port 8001):
| Tool | Description |
|------|-------------|
| `lookup_cigar` | Returns wrapper, binder, filler, strength, flavor notes, MSRP/MAP for a SKU |
| `get_all_research` | Dump the full research cache as JSON |
| `research_status` | Cache coverage summary |
| `search_similar_cigars` | **RAG** — semantic search over the research database with MMR and optional BGE reranking. Parameters: `query`, `k` (default 5), `mmr_lambda` (default 0.7) |
| `rag_index_status` | Stats about the ChromaDB vector index (indexed count, path) |
| `rebuild_rag_index` | Force a rebuild of the vector index after updating Cigar_Research.xlsx |

**Social intelligence tools** (`social_intel_server.py`, port 8002):
| Tool | Description |
|------|-------------|
| `lookup_social_reputation` | Overall/quality/value/community scores, quotes, sources for a SKU |
| `get_buzz_feed` | New/upcoming cigars sorted by buzz score; accepts `refresh=True` |
| `get_all_social_data` | Dump the full social cache as JSON |
| `social_status` | Coverage summary + Reddit/YouTube API configuration status |

**Ordering agent tools** (`ordering_server.py`, port 8003):
| Tool | Description |
|------|-------------|
| `generate_order_recommendation` | Full Tree of Thought analysis — returns restock signals + ranked new-cigar recommendations, grouped by parent company, with wholesale cost breakdown and `_verbose_events` log. Accepts `horizon_days`, `order_budget`, `new_cigar_pct`. |
| `get_fit_profile` | Score a single candidate cigar against the store's sales profile |

**Inventory agent tools** (`inventory_server.py`, port 8004):
| Tool | Description |
|------|-------------|
| `get_reorder_signals` | OOS and stockout-risk items; velocity-driven only; outputs `velocity_min_level` and `velocity_reorder_qty` |
| `get_slow_movers` | Excess stock with low velocity — discount candidates with seasonality context |
| `get_discontinue_candidates` | Dead stock with ≤ 2 YTD units — back-half seasonal items automatically excluded |
| `get_top_profitable` | Top items by YTD gross profit with stock adequacy indicator |
| `get_full_inventory_report` | All four analyses in one call |
| `search_inventory_by_name` | Search live inventory by name/brand fragment |

---

## Project structure

```
.
├── main.py                    # Unified launcher — all commands above
├── ui.py                      # Chainlit chatbot UI
├── dispatcher_agent.py        # Natural-language dispatcher — auto-discovers all *_server.py tools
├── sales_agent.py             # Sales analyst agent (Anthropic SDK agentic loop)
├── sales_server.py            # FastMCP server — sales agent as MCP tools (port 8000)
├── cigar_researcher.py        # Cigar research agent + CLI + batch populator
├── research_server.py         # FastMCP server — research agent + RAG tools (port 8001)
├── research_rag.py            # RAG layer: ChromaDB index, MMR, BGE reranker
├── social_intel_agent.py      # Social intelligence agent + CLI + batch populator
├── social_intel_server.py     # FastMCP server — social agent as MCP tools (port 8002)
├── ordering_agent.py          # Tree of Thought ordering agent + CLI + verbose demo mode + export flags
├── ordering_server.py         # FastMCP server — ordering agent as MCP tools (port 8003)
├── chart_generator.py         # Parse tool outputs → Plotly figures for inline Chainlit display
├── order_export.py            # Export order results to XLSX (openpyxl) or PDF (reportlab)
├── decision_memory.py         # Long-term memory: record decisions, evaluate outcomes
├── inventory_agent.py         # Inventory analysis agent + CLI (no LLM required)
├── inventory_server.py        # FastMCP server — inventory agent as MCP tools (port 8004)
├── inventory_verifier.py      # Builds Smoke_Shoppe_Inventory_Verified.xlsx
├── chainlit.md                # Chatbot welcome screen (fallback for unknown locales)
├── chainlit_en-US.md          # English welcome screen (loaded by Chainlit for en-US browsers)
├── Dockerfile                 # linux/amd64 container image (python:3.12-slim + uv)
├── docker-compose.yml         # Synology NAS deployment — mounts ./data, reads .env
├── tools/
│   ├── sql_tool.py            # SqlQueryTool — loads transactions XLSX into DuckDB, runs SQL
│   ├── inventory_tool.py      # Thread-safe DuckDB helpers (threading.Lock on all .execute() calls)
│   ├── xlsx_tool.py           # XlsxReaderTool — generic XLSX reader
│   ├── reddit_tool.py         # Reddit PRAW wrapper (optional, degrades gracefully)
│   └── youtube_tool.py        # YouTube Data API wrapper (optional, degrades gracefully)
├── data/                      # gitignored — copy manually to server
│   ├── Smoke_Shoppe_Transactions.xlsx
│   ├── Smoke_Shoppe_Inventory.xlsx
│   ├── Smoke_Shoppe_Inventory_Verified.xlsx  # generated by: python main.py verify-inventory
│   ├── Brand_Reference.xlsx                  # brand → parent company lookup
│   ├── Cigar_Research.xlsx    # populated by: python main.py research --batch
│   ├── Cigar_Social.xlsx      # populated by: python main.py social --batch
│   ├── Cigar_Buzz.xlsx        # populated by: python main.py social --buzz
│   ├── Order_History.json     # long-term memory: past order decisions (auto-created)
│   └── chroma_research/       # ChromaDB vector index (auto-built on first RAG query)
└── public/
    ├── theme.css              # Smoke Shoppe brand theme (dark amber/gold)
    ├── logo_dark.svg          # Chainlit /logo endpoint (dark theme)
    ├── logo_light.svg         # Chainlit /logo endpoint (light theme)
    └── logo-option-3-wordmark.svg
```

---

## Docker deployment (Synology NAS)

The app ships as a `linux/amd64` Docker image. Data files are mounted as a volume so they survive container updates.

### CI build (recommended)

Pushing a version tag triggers the GitHub Actions workflow, which builds and pushes the image to `ghcr.io` automatically:

```bash
git tag v0.13 && git push origin v0.13
# → ghcr.io/tr0n2ooo/cigar-shoppe-ai:0.13 and :latest
```

Then on the NAS:
```bash
ssh user@nas-ip
cd /volume1/docker/cigar-shoppe
docker compose pull && docker compose down && docker compose up -d
docker compose logs -f
```

### Manual build & export (offline / air-gapped)
```bash
docker build --platform linux/amd64 -t cigar-shoppe-ai:0.13 .
docker save cigar-shoppe-ai:0.13 | gzip > cigar-shoppe-ai-0.13.tar.gz
```

### Transfer to NAS
```bash
scp cigar-shoppe-ai-0.13.tar.gz user@nas-ip:/volume1/docker/cigar-shoppe/
scp data/Smoke_Shoppe_Inventory_Verified.xlsx user@nas-ip:/volume1/docker/cigar-shoppe/data/
scp data/Cigar_Research.xlsx user@nas-ip:/volume1/docker/cigar-shoppe/data/
```

### Deploy on NAS (manual image)
```bash
ssh user@nas-ip
cd /volume1/docker/cigar-shoppe
docker load -i cigar-shoppe-ai-0.13.tar.gz
docker compose down && docker compose up -d
docker compose logs -f
```

### `.env` file (create on NAS alongside `docker-compose.yml`)
```
ANTHROPIC_API_KEY=sk-ant-...
YOUTUBE_API_KEY=...          # optional
UI_USERNAME=cigar            # used for /manager login in the chat UI
UI_PASSWORD=your-password    # used for /manager login in the chat UI
# CHAINLIT_AUTH_SECRET no longer required (global login gate removed in v0.12)
```

---

## Connect via Claude Desktop
```json
{
  "mcpServers": {
    "smoke-shoppe-sales": {
      "command": "python",
      "args": ["/absolute/path/to/main.py", "server"],
      "env": { "ANTHROPIC_API_KEY": "sk-ant-..." }
    },
    "smoke-shoppe-research": {
      "command": "python",
      "args": ["/absolute/path/to/main.py", "research-server"],
      "env": { "ANTHROPIC_API_KEY": "sk-ant-..." }
    },
    "smoke-shoppe-social": {
      "command": "python",
      "args": ["/absolute/path/to/main.py", "social-server"],
      "env": {
        "ANTHROPIC_API_KEY": "sk-ant-...",
        "REDDIT_CLIENT_ID": "...",
        "REDDIT_CLIENT_SECRET": "...",
        "YOUTUBE_API_KEY": "..."
      }
    },
    "smoke-shoppe-ordering": {
      "command": "python",
      "args": ["/absolute/path/to/main.py", "order-server"],
      "env": { "ANTHROPIC_API_KEY": "sk-ant-..." }
    },
    "smoke-shoppe-inventory": {
      "command": "python",
      "args": ["/absolute/path/to/main.py", "inventory-server"],
      "env": { "ANTHROPIC_API_KEY": "sk-ant-..." }
    }
  }
}
```
