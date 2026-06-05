"""
agent.py
--------
Agentic loop powered by the Anthropic SDK that answers natural-language
questions about the Smoke Shoppe transactions and inventory data via SQL,
with optional cigar research lookups for wrapper/binder/flavor/MSRP info.

Two tools are available to the model:
  • sql_query         – run SELECT queries against the transactions DuckDB table
  • lookup_cigar_info – get wrapper, binder, filler, flavor notes, MSRP for a SKU
                        (hits the local Cigar_Research.xlsx cache; researches live
                        via web search if not yet cached)

The MCP server (server.py) exposes this agent to the broader multi-agent system.
The Chainlit UI (ui.py) provides a direct browser-based chat interface.

Standalone usage:
    python agent.py "What are the top 5 products by total sales?"
"""

import json
import os
from pathlib import Path

import anthropic

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
        response = client.messages.create(
            model="claude-sonnet-4-6",
            max_tokens=4096,
            system=SYSTEM_PROMPT,
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


if __name__ == "__main__":
    import sys

    question = " ".join(sys.argv[1:]) if len(sys.argv) > 1 else "Summarise the transactions data."
    print(run_query(question))
