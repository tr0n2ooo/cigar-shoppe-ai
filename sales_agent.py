"""
sales_agent.py
--------------
Agentic loop powered by the Anthropic SDK that answers natural-language
questions about the Smoke Shoppe transactions and inventory data via SQL,
with optional cigar research lookups for wrapper/binder/flavor/MSRP info.

Two tools are available to the model:
  • sql_query         – run SELECT queries against the transactions DuckDB table
  • lookup_cigar_info – get wrapper, binder, filler, flavor notes, MSRP for a SKU
                        (hits the local Cigar_Research.xlsx cache; researches live
                        via web search if not yet cached)

Also exports analyze_sales_fit() — scores a candidate cigar against historical
sales patterns without an LLM call (used by the ordering agent).

The MCP server (sales_server.py) exposes this agent to the broader multi-agent system.
The Chainlit UI (ui.py) provides a direct browser-based chat interface.

Standalone usage:
    python sales_agent.py "What are the top 5 products by total sales?"
"""

import json
import os
from pathlib import Path

import anthropic

from cigar_researcher import _create_with_backoff
from tools.sql_tool import SqlQueryTool

DEFAULT_XLSX = str(
    Path(__file__).parent / "data" / "Smoke_Shoppe_Transactions.xlsx"
)

SYSTEM_PROMPT = """You are a data analyst and product expert for a premium cigar shop called Smoke Shoppe.
You have two tools available:

1. sql_query — query the transactions table (DuckDB) for sales/revenue analytics.
   Column names are lowercase with underscores (e.g. product_name, item_amount, brand, parent_company).
   Only SELECT queries are allowed.

2. lookup_cigar_info — retrieve detailed product info for a specific cigar:
   wrapper leaf, binder, filler, country of origin, factory, strength, flavor notes
   (manufacturer copy), MSRP, and MAP price.
   Use this when the user asks about a cigar's blend, taste profile, origin, or pricing.

Workflow for analytics questions:
1. Call sql_query with action='get_schema' to confirm columns.
2. Write an efficient SQL query (aggregations over raw rows).
3. Return a clear answer with specific numbers.

Workflow for product detail questions:
1. Call lookup_cigar_info with the product description and brand.
2. Present the blend details, flavor profile, and pricing in a readable format.

Always be specific, accurate, and refer to the shop as "Smoke Shoppe"."""

SQL_TOOL_DEF = {
    "name": "sql_query",
    "description": (
        "Query the shop transactions table using SQL. "
        "Use action='get_schema' to inspect columns and row count. "
        "Use action='run_sql' to execute a SELECT query and get results."
    ),
    "input_schema": {
        "type": "object",
        "properties": {
            "action": {
                "type": "string",
                "enum": ["get_schema", "run_sql"],
                "description": "'get_schema' to see columns/types/row count; 'run_sql' to execute SQL.",
            },
            "query": {
                "type": "string",
                "description": "SQL SELECT query to run against the 'transactions' table.",
            },
            "max_rows": {
                "type": "integer",
                "description": "Max rows to return (default 100).",
                "default": 100,
            },
        },
        "required": ["action"],
    },
}

RESEARCH_TOOL_DEF = {
    "name": "lookup_cigar_info",
    "description": (
        "Look up detailed product information for a specific cigar: wrapper leaf, binder, filler, "
        "country of origin, factory, strength, flavor notes (manufacturer description), "
        "MSRP (single stick), and MAP price. "
        "Returns cached data instantly; triggers live web research on first lookup. "
        "Use this when users ask about a cigar's blend, taste, origin, or suggested retail price."
    ),
    "input_schema": {
        "type": "object",
        "properties": {
            "description": {
                "type": "string",
                "description": "The product name / description exactly as it appears in inventory.",
            },
            "brand": {
                "type": "string",
                "description": "The brand name (e.g. 'Perdomo', 'Arturo Fuente').",
            },
            "item_number": {
                "type": "string",
                "description": "Optional inventory item number for precise cache lookup.",
            },
        },
        "required": ["description"],
    },
}

def run_query(question: str, xlsx_path: str = DEFAULT_XLSX) -> str:
    """
    Run a natural-language question against the transactions data,
    with optional cigar research lookups for product detail questions.
    Primary entry point used by the Chainlit UI and MCP server.
    """
    from cigar_researcher import lookup_cigar  # lazy import to avoid circular deps

    client   = anthropic.Anthropic(api_key=os.environ.get("ANTHROPIC_API_KEY"))
    sql_tool = SqlQueryTool(file_path=xlsx_path)
    messages = [{"role": "user", "content": question}]

    while True:
        response = _create_with_backoff(
            client,
            model="claude-haiku-4-5-20251001",
            max_tokens=4096,
            system=[{"type": "text", "text": SYSTEM_PROMPT, "cache_control": {"type": "ephemeral"}}],
            tools=[SQL_TOOL_DEF, RESEARCH_TOOL_DEF],
            messages=messages,
        )

        messages.append({"role": "assistant", "content": response.content})

        if response.stop_reason == "end_turn":
            for block in response.content:
                if hasattr(block, "text"):
                    return block.text
            return ""

        if response.stop_reason == "tool_use":
            tool_results = []
            for block in response.content:
                if block.type != "tool_use":
                    continue

                if block.name == "sql_query":
                    inputs = block.input
                    result = sql_tool._run(
                        action=inputs.get("action"),
                        query=inputs.get("query"),
                        max_rows=inputs.get("max_rows", 100),
                    )

                elif block.name == "lookup_cigar_info":
                    inputs = block.input
                    try:
                        data = lookup_cigar(
                            description=inputs.get("description", ""),
                            brand=inputs.get("brand", ""),
                            item_number=inputs.get("item_number", ""),
                        )
                        result = json.dumps(data, default=str, indent=2)
                    except Exception as exc:
                        result = json.dumps({"error": str(exc)})

                else:
                    result = json.dumps({"error": f"Unknown tool: {block.name}"})

                tool_results.append({
                    "type": "tool_result",
                    "tool_use_id": block.id,
                    "content": result,
                })
            messages.append({"role": "user", "content": tool_results})
            continue

        break

    return "No answer could be generated."


# ── Sales Fit Analysis ────────────────────────────────────────────────────────
# Scoring tables derived from 18 months of balanced qty+revenue sales data.

_WRAPPER_SCORES: dict[str, int] = {
    "maduro": 95, "capa negra": 95, "colorado maduro": 88, "connecticut broadleaf": 82,
    "oscuro": 75,
    "sumatra": 72,
    "natural": 68, "colorado natural": 68,
    "connecticut shade": 68, "connecticut": 66,
    "habano": 64, "habano colorado": 64, "habano claro": 60,
    "colorado": 58, "colorado claro": 55,
    "claro": 50,
    "double claro": 40,
    "candela": 30,
}

_STRENGTH_SCORES: dict[str, int] = {
    "medium-full": 90, "medium full": 90, "medium to full": 88, "medium/full": 90,
    "medium": 80, "medium-bodied": 80,
    "mild-medium": 60, "mild to medium": 58, "mild medium": 58, "mild/medium": 58,
    "full": 42, "full-bodied": 42, "full bodied": 42,
    "mild": 30,
    "extra full": 25,
}

_VITOLA_SCORES: dict[str, int] = {
    "toro": 95, "toro grande": 92, "toro extra": 90,
    "gordo": 90, "double gordo": 87, "gran gordo": 88,
    "corona gorda": 70, "gran corona": 68,
    "corona": 65,
    "robusto": 63, "robusto xl": 65,
    "churchill": 60, "double corona": 58,
    "figurado": 58, "torpedo": 58, "belicoso": 56, "piramide": 56,
    "lancero": 50, "panetela": 48,
    "lonsdale": 52,
    "petit corona": 52, "petit robusto": 55,
    "magnum": 60, "presidente": 55,
    "perfecto": 50,
}

_BRAND_FIT_SCORES: dict[str, int] = {
    # Tier 1 — Core (~5-6% each)
    "oliva": 95, "espinosa especial": 95,
    # Tier 2 — Strong (~4-5%)
    "padrón": 90, "padron": 90, "ashton": 90, "perdomo": 90,
    "curivari": 90, "espinosa premium": 90, "espinosa": 85,
    # Tier 3 — Solid (~2-3%)
    "rocky patel": 80, "roma craft tobac": 80, "roma craft": 80,
    "aj fernandez": 80, "arturo fuente": 78,
    # Tier 4 — Supporting
    "h. upmann": 65, "upmann": 65, "hoyo de monterrey": 65,
    "la flor dominicana": 65, "aladino": 65, "c.l.e.": 65, "cle": 65,
    "acid": 65, "romeo y julieta": 65, "macanudo": 65, "montecristo": 65,
    "my father": 70, "liga privada": 62,
    # Ultra-premium / not carried — avoid
    "davidoff": 5, "opus x": 5, "opusx": 5,
}

_INACCESSIBLE_KEYWORDS = frozenset({"davidoff", "opus x", "opusx"})


def _score_attr(value: str, table: dict[str, int], default: int = 50) -> tuple[int, str]:
    """Fuzzy lookup in a scoring table; returns (score, matched_key)."""
    if not value:
        return default, "unknown"
    v = value.strip().lower()
    if v in table:
        return table[v], v
    # Partial match — longest key that appears in v
    best_key, best_score = "", default
    for k, s in table.items():
        if k in v and len(k) > len(best_key):
            best_key, best_score = k, s
    return best_score, best_key or v


def _price_fit(msrp: float | None) -> tuple[int, str]:
    """Returns (score, tier_label) for a given MSRP per stick."""
    if msrp is None:
        return 50, "unknown"
    if msrp < 8:
        return 40, "under $8 (cigarillo/value)"
    if msrp <= 12:
        return 75, "$8-12"
    if msrp <= 18:
        return 95, "$12-18 (ideal)"
    if msrp <= 25:
        return 65, "$18-25 (acceptable)"
    if msrp <= 30:
        return 35, "$25-30 (hard to move)"
    return 10, "$30+ (nearly impossible)"


def analyze_sales_fit(
    description: str,
    brand: str = "",
    wrapper: str = "",
    strength: str = "",
    vitola: str = "",
    msrp: float | None = None,
    xlsx_path: str = DEFAULT_XLSX,
) -> dict:
    """
    Score a candidate cigar against the store's historical sales profile.

    Returns a structured fit profile with per-dimension scores (0-100) and
    an overall_fit_score, plus comparable top sellers from transactions data.

    Dimension weights:
      wrapper 30% · price 25% · vitola 20% · strength 20% · brand 5%
    """
    wrapper_score, wrapper_key  = _score_attr(wrapper, _WRAPPER_SCORES)
    strength_score, strength_key = _score_attr(strength, _STRENGTH_SCORES)
    vitola_score, vitola_key    = _score_attr(vitola, _VITOLA_SCORES)
    price_score, price_tier     = _price_fit(msrp)
    brand_score, brand_key      = _score_attr(brand.lower(), _BRAND_FIT_SCORES)

    overall = round(
        wrapper_score  * 0.30 +
        price_score    * 0.25 +
        vitola_score   * 0.20 +
        strength_score * 0.20 +
        brand_score    * 0.05
    )

    # Availability flag
    desc_lower  = description.lower()
    brand_lower = brand.lower()
    is_inaccessible = any(
        kw in desc_lower or kw in brand_lower for kw in _INACCESSIBLE_KEYWORDS
    )
    availability = "inaccessible" if is_inaccessible else "accessible"

    # Find comparable sellers via SQL (same brand or same price range)
    comparable: list[dict] = []
    try:
        sql_tool = SqlQueryTool(file_path=xlsx_path)
        price_low  = (msrp or 0) * 0.7
        price_high = (msrp or 999) * 1.3
        brand_filter = brand.replace("'", "''") if brand else ""
        if brand_filter:
            query = f"""
                SELECT product_name, brand, SUM(qty) AS units, SUM(item_amount) AS revenue
                FROM transactions
                WHERE LOWER(brand) = LOWER('{brand_filter}')
                  AND qty IS NOT NULL AND item_amount IS NOT NULL
                GROUP BY product_name, brand
                ORDER BY revenue DESC
                LIMIT 5
            """
        else:
            query = f"""
                SELECT product_name, brand, SUM(qty) AS units, SUM(item_amount) AS revenue
                FROM transactions
                WHERE item_amount BETWEEN {price_low:.2f} AND {price_high:.2f}
                  AND qty IS NOT NULL AND item_amount IS NOT NULL
                GROUP BY product_name, brand
                ORDER BY revenue DESC
                LIMIT 5
            """
        raw = sql_tool._run(action="run_sql", query=query.strip())
        rows = json.loads(raw).get("rows", [])
        comparable = [
            {
                "product": r.get("product_name", ""),
                "brand":   r.get("brand", ""),
                "units":   int(r.get("units") or 0),
                "revenue": float(r.get("revenue") or 0),
            }
            for r in rows
        ]
    except Exception:
        pass

    return {
        "description":     description,
        "brand":           brand,
        "overall_fit_score": overall,
        "availability":    availability,
        "dimensions": {
            "wrapper":  {"score": wrapper_score,  "matched": wrapper_key,   "input": wrapper},
            "strength": {"score": strength_score, "matched": strength_key,  "input": strength},
            "vitola":   {"score": vitola_score,   "matched": vitola_key,    "input": vitola},
            "price":    {"score": price_score,    "tier": price_tier,       "msrp": msrp},
            "brand":    {"score": brand_score,    "matched": brand_key,     "input": brand},
        },
        "comparable_sellers": comparable,
        "fit_rationale": (
            f"Wrapper ({wrapper or 'unknown'}) scores {wrapper_score}/100 — "
            f"{wrapper_key or 'unrecognized wrapper'}. "
            f"Price {price_tier} scores {price_score}/100. "
            f"Vitola ({vitola or 'unknown'}) scores {vitola_score}/100. "
            f"Strength ({strength or 'unknown'}) scores {strength_score}/100. "
            f"Brand ({brand or 'unknown'}) scores {brand_score}/100. "
            + ("⚠ Flagged as inaccessible — we cannot source this cigar." if is_inaccessible else "")
        ),
    }


if __name__ == "__main__":
    import sys

    question = " ".join(sys.argv[1:]) if len(sys.argv) > 1 else "Summarise the transactions data."
    print(run_query(question))
