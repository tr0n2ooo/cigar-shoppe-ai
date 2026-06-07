# Smoke Shoppe AI

A multi-agent AI analyst for the Smoke Shoppe. Ask questions about sales, inventory, cigar details, and social reputation in plain English — the agents query the data and respond with specific numbers and insights.

---

## Architecture

```
Chainlit UI (ui.py)
        │
        ▼
  sales_agent.py  ── sales analyst agentic loop (Anthropic SDK)
        │
   ┌────┴──────────────────┐
   ▼                       ▼
SqlQueryTool          lookup_cigar_info
(tools/sql_tool.py)   (cigar_researcher.py)
        │                   │
   DuckDB (in-memory)   Cigar_Research.xlsx
   loaded from           (blend, MSRP, ratings cache)
   transactions XLSX          │
                        Claude web search (native)

sales_server.py         — sales analyst as MCP server (port 8000)
research_server.py      — cigar research as MCP server (port 8001)
social_intel_server.py  — social intelligence as MCP server (port 8002)
ordering_server.py      — ordering agent as MCP server (port 8003)
inventory_server.py     — inventory analyst as MCP server (port 8004)

social_intel_agent.py   — reputation & buzz agent (Anthropic SDK)
        │
   ┌────┴──────────────────────────────┐
   ▼                                   ▼
Claude web search (native)        Optional enrichment
(Halfwheel, CA, BMP, Reddit,      Reddit PRAW (REDDIT_CLIENT_ID)
 YouTube, Halfwheel new releases)  YouTube Data API (YOUTUBE_API_KEY)
        │                                   │
   Cigar_Social.xlsx               Cigar_Buzz.xlsx
   (per-SKU reputation cache)      (new/upcoming buzz feed)
                                           │
                                           ▼
                                  ordering_agent.py
                                  Tree of Thought ordering analysis
                                  (conservative / balanced / adventurous)
                                           │
                                  Smoke_Shoppe_Inventory_Verified.xlsx
                                  (verified inventory — never passed whole
                                   to LLM context, DuckDB only)

inventory_agent.py      — inventory analysis agent (SQL + DuckDB, no LLM required)
        │
   ┌────┴───────────────────────────────────┐
   ▼                                        ▼
run_shop_sql_df()                  Smoke_Shoppe_Inventory_Verified.xlsx
(DuckDB: inventory + transactions)   Discontinued / Discontinued Reason
                                     columns written back by agent
```

**Key design decisions:**
- **Anthropic SDK directly** for all agent loops — avoids CrewAI assistant prefill issues
- **Claude native web search** (`web_search_20250305`) for all research — no Brave/Serp keys needed
- **DuckDB** for all data access — agents write SQL; only result rows enter context, never full DataFrames
- **Chainlit** for the web UI — themed with the Smoke Shoppe brand (dark amber/gold palette)
- **Four XLSX caches** — `Cigar_Research.xlsx` (blend/MSRP), `Cigar_Social.xlsx` (reputation), `Cigar_Buzz.xlsx` (buzz feed), `Smoke_Shoppe_Inventory_Verified.xlsx` (verified inventory)
- **Graceful degradation** — Reddit and YouTube are optional enrichment; all agents work without them
- **Tree of Thought ordering** — three independent branches (conservative/balanced/adventurous) synthesized into a final recommendation with vitola, box quantity, and wholesale cost estimates
- **Transaction-based inventory analytics** — all YTD/MTD figures come from `Smoke_Shoppe_Transactions.xlsx`, never from the inventory file's POS-exported columns

---

## Data

All data files live in `data/`:

| File | Description |
|------|-------------|
| `Smoke_Shoppe_Transactions.xlsx` | 124,256 sales rows, 20 columns (2023–2026) |
| `Smoke_Shoppe_Inventory.xlsx` | 3,713 inventory items, 37 columns (source of truth) |
| `Smoke_Shoppe_Inventory_Verified.xlsx` | Verified copy: items never in transactions zeroed; negatives clamped; `Discontinued` + `Discontinued Reason` columns added and maintained by the inventory agent. |
| `Brand_Reference.xlsx` | 248-row brand → parent company lookup |
| `Cigar_Research.xlsx` | Per-SKU: wrapper, binder, filler, flavor notes, MSRP/MAP, ratings |
| `Cigar_Social.xlsx` | Per-SKU: overall/quality/value/community scores, top quotes, sources |
| `Cigar_Buzz.xlsx` | New/upcoming cigars: buzz score, sentiment, summary, release status |

---

## Quickstart

### 1. Install dependencies

```bash
pip install -r requirements.txt
# or: uv sync
```

### 2. Set environment variables

```bash
# Required
export ANTHROPIC_API_KEY="sk-ant-..."

# Optional — Reddit community data (structured upvote/comment metrics)
# ⚠️  Reddit API now requires pre-approval before credentials work.
#     Apply at: https://www.reddit.com/wiki/api (free non-commercial tier, ~2-4 week review)
#     Without these, social_intel_agent falls back to Claude web search for Reddit content.
export REDDIT_CLIENT_ID="..."
export REDDIT_CLIENT_SECRET="..."

# Optional — YouTube video data (Google Cloud Console → YouTube Data API v3, free tier)
export YOUTUBE_API_KEY="..."
```

All agents work without Reddit/YouTube keys — Claude's web search covers those sources
as a fallback. The optional keys add structured engagement metrics (upvote ratios, video counts).

### 3. Build the verified inventory (first-time setup)

```bash
python main.py verify-inventory
```

This generates `data/Smoke_Shoppe_Inventory_Verified.xlsx` — required by the ordering agent, social intelligence agent, and inventory agent. Run it again any time the source inventory file is updated.

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
python main.py order                             # recommend new SKUs to order (uses cached buzz feed,
                                                 #   auto-refreshes if cache is older than 3 months)
python main.py order --refresh                   # force buzz feed refresh before analyzing
python main.py order --stale-months 1            # auto-refresh if cache is older than 1 month
python main.py order --stale-months 0            # disable auto-refresh (always use cache as-is)
python main.py order --slots 5                   # recommend 5 new SKUs (default 3)
python main.py order --pool 30                   # consider top 30 buzz candidates (default 25)
python main.py order --craziness 7               # more adventurous branching (default 5, range 0-10)
python main.py order --budget 500                # total order budget in $ (default $1,000)
python main.py order --max-price 22              # filter out cigars above $22/stick MSRP
python main.py order --json                      # output raw JSON instead of pretty-print

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

# Manual inventory decisions (written to Smoke_Shoppe_Inventory_Verified.xlsx):
python main.py inventory --mark-discontinued "Macanudo Ascots" --reason "Vendor dropped SKU"
python main.py inventory --mark-discontinued "All Los Statos" --reason "Line discontinued"
python main.py inventory --mark-discontinued "All Los Statos, All Magic Toast" --reason "Clearing out"
python main.py inventory --mark-discontinued "689674013297" --reason "Owner decision"
python main.py inventory --re-enable "Sobremesa El Americano"   # lock against auto-discontinuation

# ── Inventory verification ───────────────────────────────────────────────────
python main.py verify-inventory                  # build Smoke_Shoppe_Inventory_Verified.xlsx
python main.py verify-inventory --summary        # print stats only, no file written
```

---

## Inventory agent details

The inventory agent (`inventory_agent.py`) answers five key stock-health questions entirely via SQL — no LLM is needed for the data layer. An optional `--summarize` flag adds a Claude interpretation.

**All YTD/MTD figures come from `Smoke_Shoppe_Transactions.xlsx`**, not from the inventory file's POS-exported columns. Items that never appear in the transaction file are excluded (verifier-zeroed). The catch-all "Open" entry is always excluded.

### Analyses

| Analysis | What it finds | Key threshold |
|----------|--------------|---------------|
| `--low-stock` | Items actively selling but OOS, below minimum, or likely to run out soon | `--days N` stockout window (default 30) |
| `--slow-movers` | Items with stock sitting but very slow velocity — discount candidates | ≤ 1 unit/month |
| `--discontinue` | Dead stock with ≤ 2 units sold YTD — seasonality-filtered | ≤ 2 YTD units |
| `--profitable` | Top items by YTD gross profit | top 25 by default |

### Seasonality (discontinue + slow movers)

Both analyses compare current-year performance against the **same calendar window** in the prior year (e.g. Jan 1–May 20 vs Jan 1–May 20 the year before). Items whose prior-year sales show a clear back-half seasonal pattern are automatically excluded from discontinue candidates or flagged with a 🍂 / 📉 note in slow movers.

### Discontinued column

The inventory agent maintains `Discontinued` and `Discontinued Reason` columns in `Smoke_Shoppe_Inventory_Verified.xlsx`.

| Value | Meaning |
|-------|---------|
| `Yes` | Discontinued (auto or manual) |
| `No` | Manually re-enabled — **locked** against future auto-discontinuation |
| *(blank)* | Untouched |

- Every `--low-stock` run automatically refreshes auto-discontinued flags first so the exclusion list is always current.
- Manual `--mark-discontinued` reasons survive the auto-refresh.
- `--re-enable` sets `No` and is only overridable by a new `--mark-discontinued`.
- Natural-language queries accepted: `"All Magic Toast"`, `"Los Statos, Knuckle Sandwich"`, exact item numbers, or description substrings.

---

## Ordering agent details

The ordering agent uses **Tree of Thought** reasoning to recommend which new cigars to add to inventory.

**How it works:**
1. Loads the buzz feed (`Cigar_Buzz.xlsx`) — new/upcoming cigars with social excitement scores
2. Filters out any cigars already in stock (fuzzy-matched against `Smoke_Shoppe_Inventory_Verified.xlsx`)
3. Enriches each candidate with a fit profile (how well it matches the store's proven sales patterns)
4. Runs three independent evaluation branches with different risk tolerances:
   - **Conservative** — proven fit required (fit 75%, buzz 25%)
   - **Balanced** — equal weight to fit and social momentum (50/50)
   - **Adventurous** — chases buzz; accepts profile mismatches (buzz 70%, fit 30%)
5. Synthesizes branches into a final ranked recommendation with vitola, box quantity, and estimated wholesale cost (50% of MSRP)

**Craziness parameter (0–10):** controls how far the branches spread. At 5 (default), conservative branch runs at 2, balanced at 5, adventurous at 8. At 0, all branches stay risk-averse; at 10, the adventurous branch goes pure buzz.

**Budget enforcement:** synthesis trims box quantities (min 1 box) or drops lowest-conviction items to stay within the order budget.

**Recency weighting:** recently announced cigars receive a scoring bonus (≤14 days: +25pts, 15–45 days: +15pts, 46–90 days: +8pts, 91–180 days: +3pts) because customers follow cigar news and ask for the newest releases.

---

## Project structure

```
.
├── main.py                    # Unified launcher — all commands above
├── ui.py                      # Chainlit chatbot UI
├── sales_agent.py             # Sales analyst agent (Anthropic SDK agentic loop)
├── sales_server.py            # FastMCP server — sales agent as MCP tools (port 8000)
├── cigar_researcher.py        # Cigar research agent + CLI + batch populator
├── research_server.py         # FastMCP server — research agent as MCP tools (port 8001)
├── social_intel_agent.py      # Social intelligence agent + CLI + batch populator
├── social_intel_server.py     # FastMCP server — social agent as MCP tools (port 8002)
├── ordering_agent.py          # Tree of Thought ordering agent + CLI
├── ordering_server.py         # FastMCP server — ordering agent as MCP tools (port 8003)
├── inventory_agent.py         # Inventory analysis agent + CLI (no LLM required)
├── inventory_server.py        # FastMCP server — inventory agent as MCP tools (port 8004)
├── inventory_verifier.py      # Builds Smoke_Shoppe_Inventory_Verified.xlsx
├── chainlit.md                # Chatbot welcome screen copy
├── tools/
│   ├── sql_tool.py            # SqlQueryTool — loads XLSX into DuckDB, runs SQL
│   ├── inventory_tool.py      # DuckDB-backed inventory + transaction access
│   ├── reddit_tool.py         # Reddit PRAW wrapper (optional, degrades gracefully)
│   └── youtube_tool.py        # YouTube Data API wrapper (optional, degrades gracefully)
├── data/
│   ├── Smoke_Shoppe_Transactions.xlsx
│   ├── Smoke_Shoppe_Inventory.xlsx
│   ├── Smoke_Shoppe_Inventory_Verified.xlsx  # generated by: python main.py verify-inventory
│   ├── Brand_Reference.xlsx
│   ├── Cigar_Research.xlsx    # populated by: python main.py research --batch
│   ├── Cigar_Social.xlsx      # populated by: python main.py social --batch
│   └── Cigar_Buzz.xlsx        # populated by: python main.py social --buzz
└── public/
    ├── theme.css              # Smoke Shoppe brand theme (dark amber/gold)
    └── logo-option-3-wordmark.svg
```

---

## MCP integration

All five servers implement the [Model Context Protocol](https://modelcontextprotocol.io) via FastMCP.

**Sales analyst tools** (`sales_server.py`, port 8000):
| Tool | Description |
|------|-------------|
| `query_xlsx` | Natural-language Q&A over transactions and inventory data |
| `describe_xlsx` | Fast structural overview — no LLM call |

**Cigar research tools** (`research_server.py`, port 8001):
| Tool | Description |
|------|-------------|
| `lookup_cigar` | Returns wrapper, binder, filler, strength, flavor notes, MSRP/MAP for a SKU |
| `get_all_research` | Dump the full research cache as JSON |
| `research_status` | Cache coverage summary |

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
| `generate_order_recommendation` | Full Tree of Thought analysis — returns ranked order list with vitolas, box quantities, and wholesale cost breakdown |
| `get_fit_profile` | Score a single candidate cigar against the store's sales profile |

**Inventory agent tools** (`inventory_server.py`, port 8004):
| Tool | Description |
|------|-------------|
| `get_reorder_signals` | OOS, below-minimum, and stockout-risk items; excludes discontinued; auto-refreshes discontinued flags |
| `get_slow_movers` | Excess stock with low velocity — discount candidates with seasonality context |
| `get_discontinue_candidates` | Dead stock with ≤ 2 YTD units — back-half seasonal items automatically excluded |
| `get_top_profitable` | Top items by YTD gross profit with stock adequacy indicator |
| `get_full_inventory_report` | All four analyses in one call |

**Connect via Claude Desktop** — add to `claude_desktop_config.json`:
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
