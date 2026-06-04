"""
agent.py
--------
Agentic loop powered by the Anthropic SDK that answers natural-language
questions about the Smoke Shoppe transactions data via SQL.

The dataset is loaded once into an in-memory DuckDB table; the model
writes SQL tool calls to query it, so only result rows — never the full
dataset — enter the context window.

The MCP server (server.py) exposes this agent to the broader multi-agent system.
The Chainlit UI (ui.py) provides a direct browser-based chat interface.

Standalone usage:
    python agent.py "What are the top 5 products by total sales?"
"""

import os
from pathlib import Path

import anthropic

from tools.sql_tool import SqlQueryTool

DEFAULT_XLSX = str(
    Path(__file__).parent / "data" / "Smoke_Shoppe_Transactions.xlsx"
)

SYSTEM_PROMPT = """You are a data analyst for a cigar shop.
You answer questions by querying a DuckDB in-memory table called 'transactions'
using the sql_query tool.

Workflow:
1. Call sql_query with action='get_schema' to confirm column names and types.
2. Write a SQL SELECT query that answers the question efficiently — use aggregations
   (SUM, COUNT, AVG, GROUP BY, ORDER BY) rather than fetching raw rows.
3. Return a clear, well-formatted answer with specific values from the results.

Column names are lowercase with underscores (e.g. product_name, item_amount, payment_method).
Only SELECT queries are allowed."""

TOOL_DEFINITION = {
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


def run_query(question: str, xlsx_path: str = DEFAULT_XLSX) -> str:
    """
    Run a natural-language question against the transactions data.
    Primary entry point used by the Chainlit UI and MCP server.
    """
    client = anthropic.Anthropic(api_key=os.environ.get("ANTHROPIC_API_KEY"))
    sql_tool = SqlQueryTool(file_path=xlsx_path)
    messages = [{"role": "user", "content": question}]

    while True:
        response = client.messages.create(
            model="claude-sonnet-4-6",
            max_tokens=4096,
            system=SYSTEM_PROMPT,
            tools=[TOOL_DEFINITION],
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
                inputs = block.input
                result = sql_tool._run(
                    action=inputs.get("action"),
                    query=inputs.get("query"),
                    max_rows=inputs.get("max_rows", 100),
                )
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
