"""
inventory_server.py
-------------------
FastMCP server exposing the Inventory Analysis Agent as MCP tools.

Tools:
  • get_low_stock_selling       — items selling but low/out of stock (reorder signals)
  • get_stockout_risk           — items likely to run out within N days
  • get_slow_movers             — excess-stock candidates for discounting
  • get_discontinue_candidates  — dead-stock candidates to remove from catalog
  • get_top_profitable          — most profitable items to push in selling
  • get_full_inventory_report   — all five analyses in one call

Stdio transport:
    python inventory_server.py

SSE/HTTP transport:
    python inventory_server.py --transport sse --port 8004

Or via main.py:
    python main.py inventory-server
    python main.py inventory-server --transport sse --port 8004
"""

import argparse
import json
import sys

from mcp.server.fastmcp import FastMCP

from inventory_agent import (
    analyze_reorder,
    analyze_slow_movers,
    analyze_discontinue_candidates,
    analyze_top_profitable,
    run_all_analyses,
    summarize_with_claude,
)
from tools.inventory_tool import run_shop_sql_df


def build_server() -> FastMCP:
    mcp = FastMCP(
        name="cigar-inventory-agent",
        instructions=(
            "Inventory analysis agent for Smoke Shoppe. "
            "Use these tools to answer questions about current stock health: "
            "what to reorder, what's about to run out, what to discount, "
            "what to discontinue, and what's most profitable. "
            "All analyses use real-time data from the verified inventory file."
        ),
    )

    @mcp.tool(
        name="get_reorder_signals",
        description=(
            "Unified reorder signal analysis covering all stock-health problems in one view:\n\n"
            "  • out_of_stock  — On Hand = 0, item still actively selling (lost sales NOW)\n"
            "  • stockout_risk — On Hand > 0 but supply runs out within days_threshold days\n"
            "                    at the current YTD velocity\n\n"
            "Minimum Level and Reorder Quantity fields are ignored — all signals are driven\n"
            "purely by sales velocity and current on-hand quantity.\n\n"
            "Items are sorted by urgency tier first (critical → high → medium → low), "
            "then by monthly_velocity descending within each tier — fastest-moving problems first.\n\n"
            "urgency is derived from days_until_stockout:\n"
            "  critical  < 7 days  |  high  7-14 days  |  medium  14-21 days  |  low  21+ days\n\n"
            "Each item includes: status, urgency, on_hand, "
            "monthly_velocity, days_until_stockout, months_of_stock, "
            "ytd/mtd units, selling_price, ytd_profit, velocity_trend, "
            "velocity_min_level (2-week safety stock), velocity_reorder_qty (30-day supply).\n\n"
            "Parameters:\n"
            "  category: str — inventory category (default 'Cigars')\n"
            "  days_threshold: int — stockout risk window in days (default 30)\n"
            "  min_ytd_units: int — minimum annual sales to count as 'selling' (default 3)\n"
            "  top_n: int — max items to return (default 75)\n"
            "  summarize: bool — add Claude natural-language interpretation (default False)"
        ),
    )
    def get_reorder_signals(
        category: str = "Cigars",
        days_threshold: int = 30,
        min_ytd_units: int = 3,
        top_n: int = 75,
        summarize: bool = False,
    ) -> str:
        result = analyze_reorder(
            category=category,
            days_threshold=days_threshold,
            min_ytd_units=min_ytd_units,
            top_n=top_n,
        )
        if summarize:
            result["claude_summary"] = summarize_with_claude(result)
        return json.dumps(result, indent=2, default=str)

    @mcp.tool(
        name="get_slow_movers",
        description=(
            "Find items with significant stock on hand but very slow sales velocity — "
            "candidates for discounting to clear excess inventory.\n\n"
            "Flags items where:\n"
            "  • On Hand ≥ min_on_hand (meaningful stock sitting)\n"
            "  • Monthly velocity ≤ max_monthly_units (barely moving)\n\n"
            "Each item includes: on_hand, monthly_velocity, months_of_excess_stock, "
            "inventory_value_at_cost, selling_price, cost, ytd_profit, "
            "suggested_action ('discount' or 'clearance').\n\n"
            "Sorted by months_of_excess_stock descending — worst excess first.\n\n"
            "Parameters:\n"
            "  category: str — inventory category (default 'Cigars')\n"
            "  max_monthly_units: float — monthly velocity threshold (default 1.0 unit/month)\n"
            "  min_on_hand: int — minimum stock to qualify (default 5)\n"
            "  top_n: int — max items to return (default 50)\n"
            "  summarize: bool — add Claude natural-language interpretation (default False)"
        ),
    )
    def get_slow_movers(
        category: str = "Cigars",
        max_monthly_units: float = 1.0,
        min_on_hand: int = 5,
        top_n: int = 50,
        summarize: bool = False,
    ) -> str:
        result = analyze_slow_movers(
            category=category,
            max_monthly_units=max_monthly_units,
            min_on_hand=min_on_hand,
            top_n=top_n,
        )
        if summarize:
            result["claude_summary"] = summarize_with_claude(result)
        return json.dumps(result, indent=2, default=str)

    @mcp.tool(
        name="get_discontinue_candidates",
        description=(
            "Find items with extremely low annual sales that should be considered for discontinuation.\n\n"
            "Criteria: YTD Units ≤ max_ytd_units AND On Hand ≥ min_on_hand.\n"
            "These are products that sold only a handful of units all year — essentially dead stock.\n\n"
            "Each item includes: on_hand, inventory_value_at_cost, ytd/mtd/wtd units, "
            "selling_price, cost, suggested_action "
            "('discontinue_immediately' if YTD=0, 'clearance_then_discontinue' otherwise).\n\n"
            "Sorted by inventory_value_at_cost descending — highest exposure first.\n\n"
            "Parameters:\n"
            "  category: str — inventory category (default 'Cigars')\n"
            "  max_ytd_units: int — max YTD units to still flag as dead (default 2)\n"
            "  min_on_hand: int — must have this much stock to bother flagging (default 1)\n"
            "  top_n: int — max items to return (default 50)\n"
            "  summarize: bool — add Claude natural-language interpretation (default False)"
        ),
    )
    def get_discontinue_candidates(
        category: str = "Cigars",
        max_ytd_units: int = 2,
        min_on_hand: int = 1,
        top_n: int = 50,
        summarize: bool = False,
    ) -> str:
        result = analyze_discontinue_candidates(
            category=category,
            max_ytd_units=max_ytd_units,
            min_on_hand=min_on_hand,
            top_n=top_n,
        )
        if summarize:
            result["claude_summary"] = summarize_with_claude(result)
        return json.dumps(result, indent=2, default=str)

    @mcp.tool(
        name="get_top_profitable",
        description=(
            "Rank items by YTD profit — the products to actively push in selling.\n\n"
            "Also flags stock_adequacy for each item so sellers know if supply can support a push:\n"
            "  adequate (>1.5 months) / low (0.5-1.5 months) / critical (<0.5 months) / out_of_stock\n\n"
            "Each item includes: rank, ytd_profit, ytd_sales_retail, margin_pct, "
            "monthly_velocity, on_hand, months_of_stock, stock_adequacy.\n\n"
            "Parameters:\n"
            "  category: str — inventory category (default 'Cigars')\n"
            "  min_ytd_units: int — minimum units sold to appear (default 1)\n"
            "  top_n: int — number of top items to return (default 25)\n"
            "  summarize: bool — add Claude natural-language interpretation (default False)"
        ),
    )
    def get_top_profitable(
        category: str = "Cigars",
        min_ytd_units: int = 1,
        top_n: int = 25,
        summarize: bool = False,
    ) -> str:
        result = analyze_top_profitable(
            category=category,
            min_ytd_units=min_ytd_units,
            top_n=top_n,
        )
        if summarize:
            result["claude_summary"] = summarize_with_claude(result)
        return json.dumps(result, indent=2, default=str)

    @mcp.tool(
        name="get_full_inventory_report",
        description=(
            "Run all four inventory analyses in one call and return a combined report.\n\n"
            "Returns a dict with keys:\n"
            "  reorder, slow_movers, discontinue_candidates, top_profitable\n\n"
            "Each section uses default thresholds. For fine-grained control, "
            "call the individual tools instead.\n\n"
            "Parameters:\n"
            "  category: str — inventory category (default 'Cigars')\n"
            "  summarize: bool — add Claude summaries to each section (default False)"
        ),
    )
    def get_full_inventory_report(
        category: str = "Cigars",
        summarize: bool = False,
    ) -> str:
        result = run_all_analyses(category=category)
        if summarize:
            for key in result:
                result[key]["claude_summary"] = summarize_with_claude(result[key])
        return json.dumps(result, indent=2, default=str)

    @mcp.tool(
        name="search_inventory_by_name",
        description=(
            "Search the live inventory for specific cigars by name, brand, or description fragment.\n\n"
            "Returns every matching SKU with:\n"
            "  on_hand, on_order, selling_price, cost, category.\n"
            "  selling_price, cost, brand, parent_company, category.\n\n"
            "Use this whenever the user asks about a specific product:\n"
            "  'Do we have X in stock?', 'How many of X do we carry?',\n"
            "  'What sizes of X do we stock?', 'What's the price of X?'\n\n"
            "Parameters:\n"
            "  search_term: str — case-insensitive substring matched against Description and Brand.\n"
            "  category: str — optional category filter (default: all categories)."
        ),
    )
    def search_inventory_by_name(
        search_term: str,
        category: str = "",
    ) -> str:
        term = search_term.replace("'", "''")
        cat_filter = f"AND LOWER(Category) = '{category.lower()}'" if category else ""
        sql = f"""
            SELECT
                Description,
                Brand,
                "Parent Company",
                Category,
                "On Hand",
                "On Order",
                "Selling Price",
                "Cost"
            FROM inventory
            WHERE (
                LOWER(Description) LIKE '%{term.lower()}%'
                OR LOWER(Brand) LIKE '%{term.lower()}%'
            )
            {cat_filter}
            ORDER BY "On Hand" DESC
            LIMIT 30
        """
        df = run_shop_sql_df(sql)
        if df.empty:
            return json.dumps({"found": 0, "search_term": search_term, "items": []})
        rows = df.to_dict(orient="records")
        for r in rows:
            on_hand = r.get("On Hand", 0) or 0
            r["stock_status"] = "in_stock" if on_hand > 0 else "out_of_stock"
        return json.dumps({"found": len(rows), "search_term": search_term, "items": rows}, default=str)

    return mcp


def main() -> None:
    parser = argparse.ArgumentParser(description="Smoke Shoppe Inventory Agent MCP Server")
    parser.add_argument(
        "--transport",
        choices=["stdio", "sse"],
        default="stdio",
        help="MCP transport: 'stdio' (default) or 'sse' for HTTP.",
    )
    parser.add_argument("--port", type=int, default=8004, help="Port for SSE transport.")
    parser.add_argument("--host", default="127.0.0.1", help="Host for SSE transport.")
    args = parser.parse_args()

    server = build_server()

    if args.transport == "stdio":
        print("Inventory Agent MCP server starting (stdio)…", file=sys.stderr)
        server.run(transport="stdio")
    else:
        print(
            f"Inventory Agent MCP server starting (SSE) on "
            f"http://{args.host}:{args.port}",
            file=sys.stderr,
        )
        server.run(transport="sse", host=args.host, port=args.port)


if __name__ == "__main__":
    main()
