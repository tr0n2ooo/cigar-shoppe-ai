"""
sales_server.py
---------------
FastMCP server that exposes the Smoke Shoppe sales analyst as MCP tools,
enabling other agents in a multi-agent system to call it over stdio or HTTP/SSE.

Stdio transport (works with Claude Desktop, CrewAI, LangChain, etc.):
    python sales_server.py
    python sales_server.py --file /path/to/other.xlsx

SSE/HTTP transport (for remote agents or orchestrators):
    python sales_server.py --transport sse --port 8000

Exposed MCP tools:
  • query_xlsx          – natural-language Q&A via the agent (uses LLM)
  • describe_xlsx       – sheet names + column headers, no LLM call
  • analyze_fit_profile – score a candidate cigar against the store's sales profile
"""

import argparse
import json
import sys
from pathlib import Path

from mcp.server.fastmcp import FastMCP

from sales_agent import DEFAULT_XLSX, run_query, analyze_sales_fit
from tools.sql_tool import SqlQueryTool


def build_server(xlsx_path: str) -> FastMCP:
    file_name = Path(xlsx_path).name
    mcp = FastMCP(
        name=f"xlsx-analyst ({file_name})",
        instructions=(
            "This MCP server provides access to a cigar shop Excel file analyst. "
            "Use 'query_xlsx' for natural-language questions and 'describe_xlsx' "
            "to quickly inspect the file's structure."
        ),
    )

    sql_tool = SqlQueryTool(file_path=xlsx_path)

    @mcp.tool(
        name="query_xlsx",
        description=(
            f"Ask a natural-language question about '{file_name}'. "
            "The agent will query the data via SQL and return a detailed answer. "
            "Examples: 'What are the top 5 products by revenue?', "
            "'How many unique products are there?', "
            "'What is the total sales by category?'"
        ),
    )
    def query_xlsx(question: str) -> str:
        return run_query(question, xlsx_path)

    @mcp.tool(
        name="describe_xlsx",
        description=(
            f"Return the column names, types, and row count for '{file_name}' without "
            "running a full agent query. Use this for quick structural discovery."
        ),
    )
    def describe_xlsx() -> str:
        return sql_tool._run(action="get_schema")

    @mcp.tool(
        name="analyze_fit_profile",
        description=(
            "Score a candidate cigar against Smoke Shoppe's historical sales profile. "
            "Returns an overall_fit_score (0-100) plus per-dimension scores for wrapper, "
            "strength, vitola, price, and brand. Also returns comparable top sellers from "
            "transactions data and flags inaccessible brands (Davidoff, OpusX, etc.). "
            "Use this to evaluate how well a new cigar would sell before ordering it. "
            "Parameters: description (product name), brand, wrapper (e.g. 'Maduro'), "
            "strength (e.g. 'Medium-Full'), vitola (e.g. 'Toro'), msrp (price per stick)."
        ),
    )
    def analyze_fit_profile_tool(
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
            xlsx_path=xlsx_path,
        )
        return json.dumps(result, default=str, indent=2)

    return mcp


def main():
    parser = argparse.ArgumentParser(description="Smoke Shoppe XLSX Analyst MCP Server")
    parser.add_argument(
        "--file",
        default=DEFAULT_XLSX,
        help="Path to the .xlsx file (default: data/Smoke_Shoppe_Transactions.xlsx)",
    )
    parser.add_argument(
        "--transport",
        choices=["stdio", "sse"],
        default="stdio",
        help="MCP transport: 'stdio' (default) or 'sse' for HTTP.",
    )
    parser.add_argument("--port", type=int, default=8000, help="Port for SSE transport.")
    parser.add_argument("--host", default="127.0.0.1", help="Host for SSE transport.")
    args = parser.parse_args()

    xlsx_path = str(Path(args.file).resolve())
    if not Path(xlsx_path).exists():
        print(f"File not found: {xlsx_path}", file=sys.stderr)
        sys.exit(1)

    mcp_server = build_server(xlsx_path)

    if args.transport == "stdio":
        print(f"XLSX Analyst MCP server starting (stdio) for: {xlsx_path}", file=sys.stderr)
        mcp_server.run(transport="stdio")
    else:
        print(f"XLSX Analyst MCP server starting (SSE) on http://{args.host}:{args.port}", file=sys.stderr)
        print(f"  File: {xlsx_path}", file=sys.stderr)
        mcp_server.run(transport="sse", host=args.host, port=args.port)


if __name__ == "__main__":
    main()
