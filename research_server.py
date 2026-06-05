"""
research_server.py
------------------
FastMCP server exposing the Cigar Research Agent as callable MCP tools.

The main sales agent (or any MCP client) can connect to this server to:
  • Look up structured info for any cigar (wrapper, binder, flavor notes, MSRP…)
  • Query the full research database
  • Check how many SKUs have been researched

Stdio transport (for Claude Desktop, CrewAI, etc.):
    python research_server.py

SSE/HTTP transport (for remote agents):
    python research_server.py --transport sse --port 8001

Or start via main.py:
    python main.py research-server
    python main.py research-server --transport sse --port 8001
"""

import argparse
import json
import sys

from mcp.server.fastmcp import FastMCP

from cigar_researcher import get_all_research, lookup_cigar, research_status


def build_server() -> FastMCP:
    mcp = FastMCP(
        name="cigar-research-agent",
        instructions=(
            "This MCP server provides detailed cigar information: wrapper, binder, filler, "
            "country of origin, factory, strength, flavor notes (manufacturer copy), MSRP and MAP. "
            "Data is web-researched and cached locally. Use 'lookup_cigar' for a specific SKU, "
            "'get_all_research' to retrieve the full database, and 'research_status' to check coverage."
        ),
    )

    @mcp.tool(
        name="lookup_cigar",
        description=(
            "Look up detailed information for a specific cigar SKU: wrapper, binder, filler, "
            "country of origin, factory, strength, flavor notes (manufacturer), MSRP, MAP. "
            "Returns cached data instantly; triggers live web research if not yet cached. "
            "Parameters: description (product name), brand, item_number (optional), "
            "use_cache=True (set False to force fresh research)."
        ),
    )
    def lookup_cigar_tool(
        description: str,
        brand: str = "",
        item_number: str = "",
        parent_company: str = "",
        use_cache: bool = True,
    ) -> str:
        result = lookup_cigar(
            description=description,
            brand=brand,
            item_number=item_number,
            parent_company=parent_company,
            use_cache=use_cache,
        )
        return json.dumps(result, default=str, indent=2)

    @mcp.tool(
        name="get_all_research",
        description=(
            "Return the complete cigar research database as a JSON array. "
            "Each element contains wrapper, binder, filler, flavor notes, MSRP, MAP, etc. "
            "Use 'research_status' first to see how many SKUs are covered."
        ),
    )
    def get_all_research_tool() -> str:
        rows = get_all_research()
        return json.dumps(rows, default=str)

    @mcp.tool(
        name="research_status",
        description=(
            "Return a summary of research coverage: how many inventory cigar SKUs have been "
            "researched vs. total SKUs, and the path to the local cache file."
        ),
    )
    def research_status_tool() -> str:
        return json.dumps(research_status(), default=str, indent=2)

    return mcp


def main() -> None:
    parser = argparse.ArgumentParser(description="Cigar Research Agent MCP Server")
    parser.add_argument(
        "--transport",
        choices=["stdio", "sse"],
        default="stdio",
        help="MCP transport: 'stdio' (default) or 'sse' for HTTP.",
    )
    parser.add_argument("--port", type=int, default=8001, help="Port for SSE transport.")
    parser.add_argument("--host", default="127.0.0.1", help="Host for SSE transport.")
    args = parser.parse_args()

    server = build_server()

    if args.transport == "stdio":
        print("Cigar Research Agent MCP server starting (stdio)…", file=sys.stderr)
        server.run(transport="stdio")
    else:
        print(
            f"Cigar Research Agent MCP server starting (SSE) on "
            f"http://{args.host}:{args.port}",
            file=sys.stderr,
        )
        server.run(transport="sse", host=args.host, port=args.port)


if __name__ == "__main__":
    main()
