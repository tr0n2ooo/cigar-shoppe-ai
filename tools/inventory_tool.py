"""
tools/inventory_tool.py
-----------------------
DuckDB-backed access to the Smoke Shoppe inventory file.

All inventory queries in agent code should come through here — never load
the full inventory Excel into a pandas DataFrame and pass it to an LLM.
SQL queries project only the columns each caller actually needs.

Two connection helpers:

  get_inventory_conn()  — single-table: 'inventory'
  get_shop_conn()       — two-table:   'inventory' + 'transactions'
                          used for the sales-weighted sort / since-filter join

Public helpers:

  run_inventory_sql(sql)    → list[tuple]
  run_inventory_sql_df(sql) → pd.DataFrame
"""

from __future__ import annotations

from pathlib import Path

import duckdb
import pandas as pd

DATA_DIR             = Path(__file__).parent.parent / "data"
DEFAULT_INVENTORY    = str(DATA_DIR / "Smoke_Shoppe_Inventory_Verified.xlsx")
DEFAULT_TRANSACTIONS = str(DATA_DIR / "Smoke_Shoppe_Transactions.xlsx")

# Module-level cache: path(s) → connection
_inv_cache:  dict[str, duckdb.DuckDBPyConnection] = {}
_shop_cache: dict[str, duckdb.DuckDBPyConnection] = {}


def get_inventory_conn(file_path: str = DEFAULT_INVENTORY) -> duckdb.DuckDBPyConnection:
    """
    Return a cached DuckDB connection with one table:
      inventory — all columns from the inventory xlsx (header row 2 → header=1).

    Column names keep their original spacing; use double-quotes in SQL:
      "Item Number", "On Hand", "Parent Company", etc.
    """
    if file_path not in _inv_cache:
        df = pd.read_excel(file_path, header=1)
        conn = duckdb.connect()
        conn.register("inventory", df)
        _inv_cache[file_path] = conn
    return _inv_cache[file_path]


def get_shop_conn(
    inv_path: str = DEFAULT_INVENTORY,
    tx_path:  str = DEFAULT_TRANSACTIONS,
) -> duckdb.DuckDBPyConnection:
    """
    Return a cached DuckDB connection with two tables:
      inventory    — from the verified inventory xlsx
      transactions — from the transactions xlsx (header row 1 → header=0)

    Use this when you need to join inventory against sales data
    (e.g. sales-weighted sort, 'since' date filtering).
    """
    cache_key = f"{inv_path}|{tx_path}"
    if cache_key not in _shop_cache:
        inv_df = pd.read_excel(inv_path, header=1)
        tx_df  = pd.read_excel(
            tx_path, header=0,
            usecols=["Date", "Item Number", "Quantity", "Item Amount"],
        )
        tx_df["Date"] = pd.to_datetime(tx_df["Date"], format="%m/%d/%y")
        conn = duckdb.connect()
        conn.register("inventory", inv_df)
        conn.register("transactions", tx_df)
        _shop_cache[cache_key] = conn
    return _shop_cache[cache_key]


def clear_inventory_cache(inv_path: str = DEFAULT_INVENTORY) -> None:
    """Evict cached connections for inv_path so the next query reloads the file."""
    _inv_cache.pop(inv_path, None)
    for key in [k for k in _shop_cache if k.startswith(inv_path)]:
        _shop_cache.pop(key, None)


def run_inventory_sql(
    sql: str,
    file_path: str = DEFAULT_INVENTORY,
) -> list[tuple]:
    """Execute SQL against the inventory table; return rows as list of tuples."""
    return get_inventory_conn(file_path).execute(sql).fetchall()


def run_inventory_sql_df(
    sql: str,
    file_path: str = DEFAULT_INVENTORY,
) -> pd.DataFrame:
    """Execute SQL against the inventory table; return result as a DataFrame."""
    return get_inventory_conn(file_path).execute(sql).fetchdf()


def run_shop_sql_df(
    sql: str,
    inv_path: str = DEFAULT_INVENTORY,
    tx_path: str = DEFAULT_TRANSACTIONS,
) -> pd.DataFrame:
    """Execute SQL against inventory + transactions tables; return result as a DataFrame."""
    return get_shop_conn(inv_path, tx_path).execute(sql).fetchdf()
