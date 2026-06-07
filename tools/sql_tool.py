"""
tools/sql_tool.py
-----------------
Loads the transactions XLSX into an in-process DuckDB instance once, then lets
callers run arbitrary SQL SELECT queries against it.

Only the result rows are returned — the full dataset never enters the context
window. The connection is cached per file path so subsequent queries reuse the
same in-memory table.

Table name: transactions
Column names: normalised (spaces → underscores, lowercased)
"""

import json
import re
from pathlib import Path
from typing import Optional

import duckdb
import pandas as pd

# Module-level cache: file_path → (conn, column_info)
_cache: dict[str, tuple[duckdb.DuckDBPyConnection, list[dict]]] = {}

TABLE = "transactions"


def _load(file_path: str) -> tuple[duckdb.DuckDBPyConnection, list[dict]]:
    if file_path in _cache:
        return _cache[file_path]

    df = pd.read_excel(file_path, engine="openpyxl")

    # Normalise column names: lowercase, spaces → underscores
    df.columns = [re.sub(r"\s+", "_", c.strip()).lower() for c in df.columns]

    conn = duckdb.connect()
    conn.register(TABLE, df)

    schema = conn.execute(f"DESCRIBE {TABLE}").fetchdf()
    columns = schema[["column_name", "column_type"]].to_dict(orient="records")

    _cache[file_path] = (conn, columns)
    return conn, columns


class SqlQueryTool:
    """Query the transactions Excel data using SQL via an in-memory DuckDB table."""

    name = "sql_query"
    description = (
        f"Query the transactions Excel data using SQL. "
        f"The data is loaded into an in-memory DuckDB table called '{TABLE}'. "
        "Use action='get_schema' first to discover column names and types, "
        "then action='run_sql' to execute SELECT queries. "
        "Supports aggregations (SUM, COUNT, AVG), GROUP BY, ORDER BY, WHERE, date filters, etc. "
        "Only result rows are returned — never the full dataset."
    )

    def __init__(self, file_path: str):
        self.file_path = str(Path(file_path).resolve())

    def run(self, action: str, query: Optional[str] = None, max_rows: int = 100) -> str:
        return self._run(action, query, max_rows)

    def _run(
        self,
        action: str,
        query: Optional[str] = None,
        max_rows: int = 100,
    ) -> str:
        try:
            conn, columns = _load(self.file_path)

            if action == "get_schema":
                row_count = conn.execute(f"SELECT COUNT(*) FROM {TABLE}").fetchone()[0]
                return json.dumps({
                    "table": TABLE,
                    "row_count": row_count,
                    "columns": columns,
                })

            if action == "run_sql":
                if not query:
                    return json.dumps({"error": "query is required for run_sql"})

                # Block anything that isn't a SELECT
                normalized = query.strip().upper()
                if not normalized.startswith("SELECT"):
                    return json.dumps({"error": "Only SELECT queries are allowed."})

                df = conn.execute(query).fetchdf()
                total = len(df)
                if total > max_rows:
                    df = df.head(max_rows)

                return json.dumps({
                    "total_rows_returned": total,
                    "rows_shown": len(df),
                    "rows": df.to_dict(orient="records"),
                }, default=str)

            return json.dumps({"error": f"Unknown action: {action}"})

        except Exception as exc:
            return json.dumps({"error": str(exc)})
