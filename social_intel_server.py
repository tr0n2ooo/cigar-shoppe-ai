"""
social_intel_server.py
----------------------
FastMCP server exposing the Social Intelligence Agent as callable MCP tools.

Clients (sales agent, demand forecasting agent, etc.) can connect to get:
  • Per-SKU reputation scores (overall, quality, value, community)
  • Buzz feed for new/upcoming cigars
  • Social cache coverage status

Stdio transport (for Claude Desktop, other agents):
    python social_intel_server.py

SSE/HTTP transport (for remote agents):
    python social_intel_server.py --transport sse --port 8002

Or via main.py:
    python main.py social-server
    python main.py social-server --transport sse --port 8002
"""

import argparse
import json
import sys

from mcp.server.fastmcp import FastMCP

from social_intel_agent import (
    lookup_social,
    get_all_social,
    get_buzz_feed,
    social_status,
    BUZZ_MAX_SEARCHES,
    REPUTATION_MAX_SEARCHES,
    DEFAULT_FIT_PROFILE,
)


def build_server() -> FastMCP:
    mcp = FastMCP(
        name="cigar-social-intel",
        instructions=(
            "Provides social reputation data and buzz intelligence for cigars. "
            "Use 'lookup_social_reputation' for per-SKU scores (overall, quality, value, community). "
            "Use 'get_buzz_feed' to see new/upcoming cigars with high social momentum. "
            "Use 'social_status' to check cache coverage and API configuration."
        ),
    )

    @mcp.tool(
        name="lookup_social_reputation",
        description=(
            "Look up the social reputation and sentiment scores for a specific cigar. "
            "Returns: overall score (0-100), quality score (pro reviews), value score "
            "(quality/price), community score (Reddit/YouTube sentiment), review count, "
            "Reddit mentions, YouTube videos, top quotes, and source breakdown. "
            "Hits cache instantly; triggers live web research on first lookup. "
            "Parameters: description (product name), brand, item_number (optional), "
            "use_cache=True (set False to force fresh research)."
        ),
    )
    def lookup_social_reputation_tool(
        description: str,
        brand: str = "",
        item_number: str = "",
        parent_company: str = "",
        use_cache: bool = True,
    ) -> str:
        result = lookup_social(
            description=description,
            brand=brand,
            item_number=item_number,
            parent_company=parent_company,
            use_cache=use_cache,
        )
        return json.dumps(result, default=str, indent=2)

    @mcp.tool(
        name="get_buzz_feed",
        description=(
            "Return new and upcoming cigars generating social buzz, scored for fit with this store. "
            "Each item includes: buzz_score (0-100), fit_score (0-100, how well it matches our customers), "
            "fit_notes (quick fit summary), release status, sentiment, and a 1-2 sentence summary. "
            "Set refresh=True to trigger a fresh web search pass. "
            f"max_searches: 3-4=quick/cheap, {BUZZ_MAX_SEARCHES}=default thorough sweep. "
            "craziness 0-10: 0=safe/high-fit only, 10=pure buzz ignore fit (default 5=balanced). "
            "fit_profile: override the store profile text (default uses Smoke Shoppe sales data). "
            "Use this to answer: what new cigars should we consider ordering? "
            "What's trending that fits our customers? What's a safe bet vs. an interesting gamble?"
        ),
    )
    def get_buzz_feed_tool(
        refresh: bool = False,
        max_searches: int = BUZZ_MAX_SEARCHES,
        target_count: int = 15,
        craziness: int = 5,
        fit_profile: str = DEFAULT_FIT_PROFILE,
    ) -> str:
        items = get_buzz_feed(
            refresh=refresh,
            max_searches=max_searches,
            target_count=target_count,
            fit_profile=fit_profile or None,
            craziness=craziness,
        )
        # Sort: balanced score of buzz + fit weighted by craziness level
        def sort_key(item):
            buzz = item.get("Buzz Score") or 0
            fit  = item.get("Fit Score") or 50   # neutral default
            fit_weight = max(0, (10 - craziness) / 10)
            buzz_weight = craziness / 10
            return -(buzz * buzz_weight + fit * fit_weight)
        items_sorted = sorted(items, key=sort_key)
        return json.dumps(items_sorted, default=str, indent=2)

    @mcp.tool(
        name="get_all_social_data",
        description=(
            "Return the complete social reputation database as a JSON array. "
            "Each element contains all scores, quotes, and source data for one cigar SKU. "
            "Use 'social_status' first to see how many SKUs are covered."
        ),
    )
    def get_all_social_data_tool() -> str:
        rows = get_all_social()
        return json.dumps(rows, default=str)

    @mcp.tool(
        name="social_status",
        description=(
            "Return a summary of social intelligence coverage: how many inventory cigar SKUs "
            "have been researched, how many buzz items are cached, and whether Reddit/YouTube "
            "enrichment APIs are configured."
        ),
    )
    def social_status_tool() -> str:
        return json.dumps(social_status(), default=str, indent=2)

    return mcp


def main() -> None:
    parser = argparse.ArgumentParser(description="Cigar Social Intelligence MCP Server")
    parser.add_argument(
        "--transport",
        choices=["stdio", "sse"],
        default="stdio",
        help="MCP transport: 'stdio' (default) or 'sse' for HTTP.",
    )
    parser.add_argument("--port", type=int, default=8002, help="Port for SSE transport.")
    parser.add_argument("--host", default="127.0.0.1", help="Host for SSE transport.")
    args = parser.parse_args()

    server = build_server()

    if args.transport == "stdio":
        print("Cigar Social Intel MCP server starting (stdio)…", file=sys.stderr)
        server.run(transport="stdio")
    else:
        print(
            f"Cigar Social Intel MCP server starting (SSE) on "
            f"http://{args.host}:{args.port}",
            file=sys.stderr,
        )
        server.run(transport="sse", host=args.host, port=args.port)


if __name__ == "__main__":
    main()
