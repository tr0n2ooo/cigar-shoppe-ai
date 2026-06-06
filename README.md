# Smoke Shoppe AI

A multi-agent AI analyst for the Smoke Shoppe. Ask questions about sales, inventory, cigar details, and social reputation in plain English — the agents query the data and respond with specific numbers and insights.

---

## Architecture

```
Chainlit UI (ui.py)
        │
        ▼
  agent.py  ─── sales analyst agentic loop (Anthropic SDK)
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

server.py               — sales analyst as MCP server (port 8000)
research_server.py      — cigar research as MCP server (port 8001)
social_intel_server.py  — social intelligence as MCP server (port 8002)

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
```

**Key design decisions:**
- **Anthropic SDK directly** for all agent loops — avoids CrewAI assistant prefill issues
- **Claude native web search** (`web_search_20250305`) for all research — no Brave/Serp keys needed
- **DuckDB** for SQL queries — agent writes SQL; only result rows enter context, not all 124K rows
- **Chainlit** for the web UI — themed with the Smoke Shoppe brand (dark amber/gold palette)
- **Three XLSX caches** — `Cigar_Research.xlsx` (blend/MSRP), `Cigar_Social.xlsx` (reputation), `Cigar_Buzz.xlsx` (buzz feed)
- **Graceful degradation** — Reddit and YouTube are optional enrichment; all agents work without them

---

## Data

All data files live in `data/`:

| File | Description |
|------|-------------|
| `Smoke_Shoppe_Transactions.xlsx` | 124,256 sales rows, 20 columns |
| `Smoke_Shoppe_Inventory.xlsx` | 3,713 inventory items, 37 columns |
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

### 3. Run the chatbot UI

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
python main.py social --buzz --target 20         # request 20 buzz cigars instead of 15
python main.py social --buzz --craziness 0       # safe mode: high-fit cigars only
python main.py social --buzz --craziness 10      # wild mode: pure buzz, ignore store fit
python main.py social --buzz --no-fit            # skip fit scoring entirely (faster)
python main.py social --status                   # show cache coverage + API config
```

---

## Project structure

```
.
├── main.py                    # Unified launcher — all commands above
├── ui.py                      # Chainlit chatbot UI
├── agent.py                   # Sales analyst agent (Anthropic SDK agentic loop)
├── server.py                  # FastMCP server — sales agent as MCP tools (port 8000)
├── cigar_researcher.py        # Cigar research agent + CLI + batch populator
├── research_server.py         # FastMCP server — research agent as MCP tools (port 8001)
├── social_intel_agent.py      # Social intelligence agent + CLI + batch populator
├── social_intel_server.py     # FastMCP server — social agent as MCP tools (port 8002)
├── chainlit.md                # Chatbot welcome screen copy
├── tools/
│   ├── sql_tool.py            # SqlQueryTool — loads XLSX into DuckDB, runs SQL
│   ├── reddit_tool.py         # Reddit PRAW wrapper (optional, degrades gracefully)
│   └── youtube_tool.py        # YouTube Data API wrapper (optional, degrades gracefully)
├── data/
│   ├── Smoke_Shoppe_Transactions.xlsx
│   ├── Smoke_Shoppe_Inventory.xlsx
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

All three servers implement the [Model Context Protocol](https://modelcontextprotocol.io) via FastMCP.

**Sales analyst tools** (`server.py`, port 8000):
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
    }
  }
}
```
