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

from ordering_agent import generate_order_recommendation, BUZZ_STALE_MONTHS
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
            "Runs three evaluation branches (conservative/balanced/adventurous) then synthesizes them "
            "into a final ranked recommendation with conviction levels, vitolas, box quantities, "
            "and estimated wholesale costs (50% of MSRP).\n\n"
            "Parameters:\n"
            "  refresh_buzz: bool — force a buzz feed refresh (costs web searches, default False).\n"
            "    The feed is also auto-refreshed when the cache is older than stale_months.\n"
            "  stale_months: int — auto-refresh if cache is older than N months (default 3; 0=disable)\n"
            "  slots: int — number of new SKUs to recommend (default 3)\n"
            "  craziness: int 0-10 — controls branch spread:\n"
            "    0-2 = all branches stay conservative (high fit required)\n"
            "    3-5 = balanced spread (default: 5)\n"
            "    6-8 = adventurous spread (buzz drives recommendations)\n"
            "    9-10 = wild (pure buzz, ignore fit)\n"
            "  order_budget: float — total $ to spend on this order (wholesale = 50% of MSRP);\n"
            "    synthesis will trim box quantities or drop items to stay within budget\n"
            "  max_price_per_stick: float — pre-filter: exclude candidates above this MSRP/stick\n\n"
            "Returns: branches (3 independent evaluations) + recommendation (synthesis with vitolas,\n"
            "  box sizes, cost breakdown, total_order_cost) + metadata"
        ),
    )
    def generate_order_recommendation_tool(
        refresh_buzz: bool = False,
        stale_months: int = BUZZ_STALE_MONTHS,
        slots: int = 3,
        candidate_pool: int = 25,
        craziness: int = 5,
        order_budget: float | None = None,
        max_price_per_stick: float | None = None,
    ) -> str:
        result = generate_order_recommendation(
            refresh_buzz=refresh_buzz,
            stale_months=stale_months,
            slots=slots,
            candidate_pool=candidate_pool,
            craziness=max(0, min(10, craziness)),
            order_budget=order_budget,
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
