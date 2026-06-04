"""
server.py
---------
FastMCP server that exposes the Smoke Shoppe XLSX analyst as MCP tools,
enabling other agents in a multi-agent system to call it over stdio or HTTP/SSE.

Stdio transport (works with Claude Desktop, CrewAI, LangChain, etc.):
    python server.py
    python server.py --file /path/to/other.xlsx

SSE/HTTP transport (for remote agents or orchestrators):
    python server.py --transport sse --port 8000

Exposed MCP tools:
  • query_xlsx   – natural-language Q&A via the agent (uses LLM)
  • describe_xlsx – sheet names + column headers, no LLM call
"""

import argparse
import sys
from pathlib import Path

from mcp.server.fastmcp import FastMCP

from agent import DEFAULT_XLSX, run_query
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
