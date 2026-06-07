"""
ordering_server.py
------------------
FastMCP server exposing the Tree of Thought Ordering Agent as MCP tools.

Clients (e.g. a Chainlit UI, another agent, or Claude Desktop) can connect to:
  • generate_order_recommendation — full ToT analysis, returns ranked order list
  • get_fit_profile               — single-cigar fit analysis (delegates to sales agent)

Stdio transport:
    python ordering_server.py

SSE/HTTP transport:
    python ordering_server.py --transport sse --port 8003

Or via main.py:
    python main.py order-server
    python main.py order-server --transport sse --port 8003
"""

import argparse
import json
import sys

from mcp.server.fastmcp import FastMCP

from ordering_agent import generate_order_recommendation, BUZZ_STALE_MONTHS, DEFAULT_MONTHLY_BUDGET
from sales_agent import analyze_sales_fit, DEFAULT_XLSX


def build_server() -> FastMCP:
    mcp = FastMCP(
        name="cigar-ordering-agent",
        instructions=(
            "Tree of Thought ordering agent for Smoke Shoppe. "
            "Use 'generate_order_recommendation' to get a ranked list of cigars to order, "
            "evaluated across conservative, balanced, and adventurous strategies. "
            "Use 'get_fit_profile' to score a single candidate cigar against our sales profile."
        ),
    )

    @mcp.tool(
        name="generate_order_recommendation",
        description=(
            "Evaluate candidate cigars using Tree of Thought reasoning and recommend which ones to order. "
            "Also fetches low-stock reorder signals from the inventory agent and incorporates them into "
            "the order plan. When the restock budget cannot cover all flagged items, Claude prioritizes "
            "them by urgency, velocity, and profit with per-item reasoning.\n\n"
            "Parameters:\n"
            "  horizon_days: int — planning horizon in days (default 30). Controls:\n"
            "    • default budget: $5,000 × (horizon_days / 30), so 7 days ≈ $1,167, 90 days = $15,000\n"
            "    • stockout-risk window: only items likely to run out within horizon_days are flagged\n"
            "  order_budget: float — total $ wholesale budget. Defaults to $5,000 × (horizon_days/30).\n"
            "  new_cigar_pct: float 0-100 — % of budget for new cigar recommendations (default 10).\n"
            "    Remainder goes to restocking low-stock items. When the restock share cannot cover all\n"
            "    flagged items, Claude selects the highest-value subset and explains each decision.\n"
            "    Set to 0 for restock-only, 100 for new-cigars-only.\n"
            "  refresh_buzz: bool — force a buzz feed refresh (default False).\n"
            "    The feed is also auto-refreshed when older than stale_months.\n"
            "  stale_months: int — auto-refresh buzz cache if older than N months (default 3; 0=disable)\n"
            "  slots: int — number of new SKUs to recommend (default 3)\n"
            "  craziness: int 0-10 — controls ToT branch spread:\n"
            "    0-2 = conservative  |  3-5 = balanced (default 5)  |  6-10 = adventurous\n"
            "  max_price_per_stick: float — pre-filter: exclude candidates above this MSRP/stick\n\n"
            "Returns: branches + recommendation (new cigars, with vitolas/box sizes/costs) + "
            "restock (prioritized low-stock items with reasoning) + budget_warnings + metadata"
        ),
    )
    def generate_order_recommendation_tool(
        horizon_days: int = 30,
        order_budget: float | None = None,
        new_cigar_pct: float = 10,
        refresh_buzz: bool = False,
        stale_months: int = BUZZ_STALE_MONTHS,
        slots: int = 3,
        candidate_pool: int = 25,
        craziness: int = 5,
        max_price_per_stick: float | None = None,
    ) -> str:
        result = generate_order_recommendation(
            refresh_buzz=refresh_buzz,
            stale_months=stale_months,
            slots=slots,
            candidate_pool=candidate_pool,
            craziness=max(0, min(10, craziness)),
            order_budget=order_budget,
            new_cigar_pct=new_cigar_pct,
            horizon_days=horizon_days,
            max_price_per_stick=max_price_per_stick,
        )
        return json.dumps(result, default=str, indent=2)

    @mcp.tool(
        name="get_fit_profile",
        description=(
            "Score a single candidate cigar against Smoke Shoppe's historical sales profile. "
            "Returns an overall_fit_score (0-100) plus per-dimension scores for:\n"
            "  • wrapper (Maduro 95, Connecticut 66, Habano 64, ...)\n"
            "  • strength (Medium-Full 90, Medium 80, Full 42, Mild 30, ...)\n"
            "  • vitola (Toro 95, Gordo 90, Corona 65, Robusto 63, ...)\n"
            "  • price ($12-18 = 95, $8-12 = 75, $18-25 = 65, $30+ = 10)\n"
            "  • brand (Oliva/Espinosa 95, Padrón/Perdomo 90, Rocky Patel 80, ...)\n"
            "Also returns comparable top sellers from our transactions data and an "
            "availability flag (inaccessible for Davidoff, OpusX, etc.).\n\n"
            "Parameters: description, brand, wrapper, strength, vitola, msrp (price per stick)"
        ),
    )
    def get_fit_profile_tool(
        description: str,
        brand: str = "",
        wrapper: str = "",
        strength: str = "",
        vitola: str = "",
        msrp: float | None = None,
    ) -> str:
        result = analyze_sales_fit(
            description=description,
            brand=brand,
            wrapper=wrapper,
            strength=strength,
            vitola=vitola,
            msrp=msrp,
            xlsx_path=DEFAULT_XLSX,
        )
        return json.dumps(result, default=str, indent=2)

    return mcp


def main() -> None:
    parser = argparse.ArgumentParser(description="Smoke Shoppe Ordering Agent MCP Server")
    parser.add_argument(
        "--transport",
        choices=["stdio", "sse"],
        default="stdio",
        help="MCP transport: 'stdio' (default) or 'sse' for HTTP.",
    )
    parser.add_argument("--port", type=int, default=8003, help="Port for SSE transport.")
    parser.add_argument("--host", default="127.0.0.1", help="Host for SSE transport.")
    args = parser.parse_args()

    server = build_server()

    if args.transport == "stdio":
        print("Ordering Agent MCP server starting (stdio)…", file=sys.stderr)
        server.run(transport="stdio")
    else:
        print(
            f"Ordering Agent MCP server starting (SSE) on "
            f"http://{args.host}:{args.port}",
            file=sys.stderr,
        )
        server.run(transport="sse", host=args.host, port=args.port)


if __name__ == "__main__":
    main()
