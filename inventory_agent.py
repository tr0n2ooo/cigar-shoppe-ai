"""
inventory_agent.py
------------------
Inventory analysis agent for Smoke Shoppe.

Analyzes current inventory (Smoke_Shoppe_Inventory_Verified.xlsx) to identify:
  • Reorder signals: items already OOS, below min level, or running out soon
  • Slow movers: excess stock candidates for discounting
  • Discontinue candidates: dead stock to remove from the catalog
  • Top profitable items: what to push in selling

All analyses use DuckDB SQL against the verified inventory — the data layer
requires no LLM call.  Pass --summarize to add a Claude interpretation on top.

Exports used by the ordering agent:
  analyze_reorder()          — reorder signals (OOS, below-min, stockout risk)
  analyze_slow_movers()
  analyze_discontinue_candidates()
  analyze_top_profitable()
  run_all_analyses()

Usage:
  python inventory_agent.py --low-stock [--days 30]
  python inventory_agent.py --slow-movers
  python inventory_agent.py --discontinue
  python inventory_agent.py --profitable [--top 25]
  python inventory_agent.py --all
  python inventory_agent.py --category "Cigars"   (default: Cigars)
  python inventory_agent.py --json
  python inventory_agent.py --summarize           (add Claude interpretation)
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import re
from datetime import date
from pathlib import Path
from typing import Any

import anthropic
import pandas as pd

from tools.inventory_tool import clear_inventory_cache, run_inventory_sql_df, run_shop_sql_df

INVENTORY_FILE = Path(__file__).parent / "data" / "Smoke_Shoppe_Inventory_Verified.xlsx"

# ── velocity helpers ──────────────────────────────────────────────────────────


def _ytd_months() -> float:
    """Months elapsed since Jan 1 of the current year, minimum 1.0."""
    today = date.today()
    days_elapsed = (today - date(today.year, 1, 1)).days + 1
    return max(days_elapsed / 30.44, 1.0)


def _velocity_trend(monthly_velocity: float, mtd_units: float, day_of_month: int) -> str:
    """
    Compare the current month's annualized rate to the YTD average.
    Returns 'accelerating', 'stable', or 'decelerating'.
    """
    if monthly_velocity <= 0 or day_of_month < 5:
        return "stable"
    mtd_rate = mtd_units / max(day_of_month / 30.44, 0.5)
    ratio = mtd_rate / monthly_velocity
    if ratio >= 1.5:
        return "accelerating"
    if ratio <= 0.5:
        return "decelerating"
    return "stable"


def _prior_year_window(path: str) -> tuple[int, int, int]:
    """Return (prior_year, cutoff_month, cutoff_day) matching the max transaction date.
    Gives a fair apples-to-apples same-window comparison for seasonality."""
    row = run_shop_sql_df('SELECT MAX("Date") AS max_date FROM transactions', inv_path=path)
    max_date = row.iloc[0, 0]  # pandas Timestamp
    today = date.today()
    return today.year - 1, int(max_date.month), int(max_date.day)


def _seasonality(
    ytd_units: float,
    py_ytd_units: float,
    py_full_units: float,
    ytd_months: float,
    prior_year: int,
) -> dict:
    """Classify an item's seasonal pattern using prior-year data.

    Labels:
      back_half_seasonal  — 0 sales in same prior-year window, but sold after that date
      back_half_weighted  — most prior-year sales came after the current window
      consistent          — similar spread fore/aft in prior year
      no_prior_data       — item never appeared in prior-year transactions
    """
    py_h2 = max(py_full_units - py_ytd_units, 0)

    if py_full_units == 0:
        return {
            "label": "no_prior_data",
            "note": f"no {prior_year} sales on record",
            "py_ytd_units": 0,
            "py_full_units": 0,
            "projected_annual": None,
        }

    seasonal_weight = py_ytd_units / py_full_units   # fraction sold in current window

    if py_ytd_units == 0:
        # Sold nothing Jan–cutoff last year but had back-half sales
        return {
            "label": "back_half_seasonal",
            "note": f"back-half seasonal — {int(py_h2)} units sold after this date in {prior_year}",
            "py_ytd_units": 0,
            "py_full_units": int(py_full_units),
            "projected_annual": None,
        }

    projected = ytd_units / seasonal_weight

    if py_h2 > py_ytd_units:
        label = "back_half_weighted"
        note = (
            f"back-half weighted — {int(py_ytd_units)} in {prior_year} same window, "
            f"{int(py_h2)} in back half"
        )
    else:
        label = "consistent"
        note = f"{int(py_ytd_units)} units in {prior_year} same window"

    return {
        "label": label,
        "note": note,
        "py_ytd_units": int(py_ytd_units),
        "py_full_units": int(py_full_units),
        "projected_annual": round(projected, 1),
    }


# ── analysis functions ────────────────────────────────────────────────────────


def _refresh_discontinued_flags(category: str, path: str) -> int:
    """Run discontinue analysis and write Discontinued / Discontinued Reason back to
    the inventory file.  Clears the DuckDB cache so the next query sees the fresh data.
    Returns the number of items marked discontinued."""
    result = analyze_discontinue_candidates(category=category, inv_path=path, top_n=99_999)
    items = result["items"]

    discontinue_map: dict[str, str] = {}
    for item in items:
        num = str(item["item_number"]).strip()
        ytd = item["ytd_units"]
        if item["suggested_action"] == "discontinue_immediately":
            reason = "0 units sold YTD; no demand"
        else:
            reason = f"Only {ytd} unit{'s' if ytd != 1 else ''} sold YTD; minimal demand"
        discontinue_map[num] = reason

    df = pd.read_excel(path, header=1)
    item_nums = df["Item Number"].astype(str).str.strip()

    # Collect manually-set entries to preserve across auto-refresh.
    # Manual discontinues: "Yes" with a reason that doesn't match auto prefixes.
    # Re-enabled items: "No" — these must never be auto-flagged again.
    _auto_prefixes = ("0 units sold YTD", "Only ")
    manual_map: dict[str, str] = {}
    reenable_set: set[str] = set()
    if "Discontinued" in df.columns and "Discontinued Reason" in df.columns:
        for num, disc, reason in zip(
            item_nums,
            df["Discontinued"].fillna(""),
            df["Discontinued Reason"].fillna(""),
        ):
            if disc == "No":
                reenable_set.add(num)
            elif disc == "Yes" and not str(reason).startswith(_auto_prefixes):
                manual_map[num] = str(reason)

    # Auto results + manual discontinues, minus anything the user has re-enabled.
    merged = {**discontinue_map, **manual_map}  # manual reason takes precedence over auto
    for num in reenable_set:
        merged.pop(num, None)

    def _disc_value(num: str) -> str:
        if num in reenable_set:
            return "No"
        return "Yes" if num in merged else ""

    df["Discontinued"] = item_nums.map(_disc_value)
    df["Discontinued Reason"] = item_nums.map(lambda x: merged.get(x, ""))

    with pd.ExcelWriter(path, engine="openpyxl") as writer:
        df.to_excel(writer, index=False, startrow=1)

    clear_inventory_cache(path)
    return len(merged)


def analyze_reorder(
    category: str = "Cigars",
    days_threshold: int = 30,
    min_ytd_units: int = 3,
    top_n: int = 99_999,
    inv_path: str | None = None,
) -> dict:
    """
    Unified reorder signal analysis covering two situations:

      • out_of_stock  — On Hand = 0, still selling (already losing sales)
      • stockout_risk — On Hand > 0 but supply runs out within days_threshold days
                        at the current YTD velocity

    Minimum Level and Reorder Quantity fields are ignored — all signals are
    driven purely by sales velocity and current on-hand quantity.

    urgency is derived from days_until_stockout (0 for OOS items):
      critical  — < 7 days   🔴
      high      — 7-14 days  🟠
      medium    — 14-21 days 🟡
      low       — 21+ days   🟢

    Results sorted by urgency tier first, then monthly_velocity descending
    so the fastest-moving problems appear at the top of each tier.

    Discontinue candidates are excluded automatically — items flagged for
    discontinuation should not be reordered regardless of their stock status.
    """
    path = str(inv_path or INVENTORY_FILE)

    # Always refresh discontinued flags before checking stock so exclusions are current.
    _refresh_discontinued_flags(category=category, path=path)

    today = date.today()
    year, month = today.year, today.month
    day_of_month = today.day
    ytd_months = _ytd_months()
    months_threshold = days_threshold / 30.44

    sql = f"""
    WITH tx AS (
      SELECT
        "Item Number" AS item_number,
        SUM(Quantity) FILTER (WHERE EXTRACT(YEAR FROM "Date") = {year})
          AS ytd_units,
        SUM(Quantity) FILTER (WHERE EXTRACT(YEAR FROM "Date") = {year}
                               AND EXTRACT(MONTH FROM "Date") = {month})
          AS mtd_units,
        SUM("Item Amount") FILTER (WHERE EXTRACT(YEAR FROM "Date") = {year})
          AS ytd_revenue
      FROM transactions
      GROUP BY "Item Number"
    )
    SELECT
        i.Description,
        i.Brand,
        i."Parent Company",
        i."Item Number",
        i."On Hand",
        i.Allocated,
        i.Cost,
        i."Selling Price",
        i."% Mark Up",
        COALESCE(t.ytd_units, 0)   AS ytd_units,
        COALESCE(t.mtd_units, 0)   AS mtd_units,
        COALESCE(t.ytd_revenue, 0) AS ytd_revenue
    FROM inventory i
    LEFT JOIN tx t ON i."Item Number" = t.item_number
    WHERE i.Category = '{category}'
      AND (i."Verification Notes" IS NULL OR i."Verification Notes" NOT LIKE 'zeroed%')
      AND LOWER(i.Description) != 'open'
      AND (i."Discontinued" IS NULL OR i."Discontinued" != 'Yes')
      AND COALESCE(t.ytd_units, 0) >= {min_ytd_units}
      AND (
            i."On Hand" = 0
            OR (
                i."On Hand" > 0
                AND i."On Hand" / (COALESCE(t.ytd_units, 0.001) / {ytd_months}) < {months_threshold}
               )
          )
    """

    df = run_shop_sql_df(sql, inv_path=path)

    _urgency_rank = {"critical": 0, "high": 1, "medium": 2, "low": 3}

    items = []
    for _, row in df.iterrows():
        on_hand = float(row["On Hand"] or 0)
        ytd_units = float(row["ytd_units"] or 0)
        mtd_units = float(row["mtd_units"] or 0)
        ytd_revenue = float(row["ytd_revenue"] or 0)
        cost = float(row["Cost"] or 0)
        monthly_vel = ytd_units / ytd_months
        ytd_profit = ytd_revenue - (ytd_units * cost)

        if on_hand == 0:
            status = "out_of_stock"
            days_left = 0.0
            months_left = 0.0
        else:
            months_left = on_hand / monthly_vel if monthly_vel > 0 else 9999
            days_left = months_left * 30.44
            status = "stockout_risk"

        urgency = (
            "critical" if days_left < 7
            else "high" if days_left < 14
            else "medium" if days_left < 21
            else "low"
        )

        items.append({
            "description": str(row["Description"] or "").strip(),
            "brand": str(row["Brand"] or "").strip(),
            "parent_company": str(row["Parent Company"] or "").strip(),
            "item_number": str(row["Item Number"] or "").strip(),
            "on_hand": int(on_hand),
            "allocated": int(row["Allocated"] or 0),
            "cost": round(cost, 2),
            "selling_price": round(float(row["Selling Price"] or 0), 2),
            "ytd_units": int(ytd_units),
            "mtd_units": int(mtd_units),
            "monthly_velocity": round(monthly_vel, 1),
            "days_until_stockout": round(days_left, 0),
            "months_of_stock": round(months_left, 2) if months_left < 9999 else None,
            "ytd_revenue": round(ytd_revenue, 2),
            "ytd_profit": round(ytd_profit, 2),
            "markup_pct": round(float(row["% Mark Up"] or 0), 1),
            "status": status,
            "urgency": urgency,
            "velocity_trend": _velocity_trend(monthly_vel, mtd_units, day_of_month),
        })

    # Sort: urgency tier ASC, then monthly_velocity DESC within each tier
    items.sort(key=lambda x: (_urgency_rank[x["urgency"]], -x["monthly_velocity"]))
    items = items[:top_n]

    count_sql = f"""
        SELECT
            COUNT(*) AS total,
            COUNT(*) FILTER (WHERE "Discontinued" = 'Yes') AS disc_count
        FROM inventory WHERE Category = '{category}'
    """
    counts = run_inventory_sql_df(count_sql, file_path=path).iloc[0]

    return {
        "analysis": "reorder_signals",
        "generated_at": today.isoformat(),
        "category": category,
        "ytd_months": round(ytd_months, 1),
        "days_threshold": days_threshold,
        "min_ytd_units_threshold": min_ytd_units,
        "total_inventory_items": int(counts["total"]),
        "discontinued_excluded": int(counts["disc_count"]),
        "flagged_count": len(items),
        "items": items,
    }


def analyze_slow_movers(
    category: str = "Cigars",
    max_monthly_units: float = 1.0,
    min_on_hand: int = 5,
    top_n: int = 50,
    inv_path: str | None = None,
) -> dict:
    """
    Items with meaningful stock on hand but very slow sales velocity.
    These are candidates for discounting to clear excess inventory.

    max_monthly_units: YTD monthly velocity must be at or below this to qualify.
    min_on_hand: must have at least this many units sitting.

    Results sorted by months_of_excess_stock descending (most excess first).
    """
    path = str(inv_path or INVENTORY_FILE)
    today = date.today()
    year, month = today.year, today.month
    ytd_months = _ytd_months()
    prior_year, cutoff_month, cutoff_day = _prior_year_window(path)

    sql = f"""
    WITH tx AS (
      SELECT
        "Item Number" AS item_number,
        SUM(Quantity) FILTER (WHERE EXTRACT(YEAR FROM "Date") = {year})
          AS ytd_units,
        SUM(Quantity) FILTER (WHERE EXTRACT(YEAR FROM "Date") = {year}
                               AND EXTRACT(MONTH FROM "Date") = {month})
          AS mtd_units,
        SUM("Item Amount") FILTER (WHERE EXTRACT(YEAR FROM "Date") = {year})
          AS ytd_revenue,
        SUM(Quantity) FILTER (
          WHERE EXTRACT(YEAR FROM "Date") = {prior_year}
            AND (EXTRACT(MONTH FROM "Date") < {cutoff_month}
                 OR (EXTRACT(MONTH FROM "Date") = {cutoff_month}
                     AND EXTRACT(DAY FROM "Date") <= {cutoff_day}))
        ) AS py_ytd_units,
        SUM(Quantity) FILTER (WHERE EXTRACT(YEAR FROM "Date") = {prior_year})
          AS py_full_units
      FROM transactions
      GROUP BY "Item Number"
    )
    SELECT
        i.Description,
        i.Brand,
        i."Parent Company",
        i."Item Number",
        i."On Hand",
        i.Cost,
        i."Selling Price",
        i."Retail Price",
        i."% Mark Up",
        COALESCE(t.ytd_units, 0)    AS ytd_units,
        COALESCE(t.mtd_units, 0)    AS mtd_units,
        COALESCE(t.ytd_revenue, 0)  AS ytd_revenue,
        COALESCE(t.py_ytd_units, 0) AS py_ytd_units,
        COALESCE(t.py_full_units, 0) AS py_full_units
    FROM inventory i
    LEFT JOIN tx t ON i."Item Number" = t.item_number
    WHERE i.Category = '{category}'
      AND (i."Verification Notes" IS NULL OR i."Verification Notes" NOT LIKE 'zeroed%')
      AND LOWER(i.Description) != 'open'
      AND i."On Hand" >= {min_on_hand}
      AND (COALESCE(t.ytd_units, 0) / {ytd_months}) <= {max_monthly_units}
    ORDER BY
        (i."On Hand" / NULLIF(COALESCE(t.ytd_units, 0) / {ytd_months}, 0)) DESC NULLS LAST,
        i."On Hand" DESC
    LIMIT {top_n}
    """

    df = run_shop_sql_df(sql, inv_path=path)

    items = []
    for _, row in df.iterrows():
        on_hand = float(row["On Hand"] or 0)
        cost = float(row["Cost"] or 0)
        ytd_units = float(row["ytd_units"] or 0)
        ytd_revenue = float(row["ytd_revenue"] or 0)
        py_ytd = float(row["py_ytd_units"] or 0)
        py_full = float(row["py_full_units"] or 0)
        monthly_vel = ytd_units / ytd_months
        months_of_excess = (on_hand / monthly_vel) if monthly_vel > 0 else None
        ytd_profit = ytd_revenue - (ytd_units * cost)
        season = _seasonality(ytd_units, py_ytd, py_full, ytd_months, prior_year)

        items.append({
            "description": str(row["Description"] or "").strip(),
            "brand": str(row["Brand"] or "").strip(),
            "parent_company": str(row["Parent Company"] or "").strip(),
            "item_number": str(row["Item Number"] or "").strip(),
            "on_hand": int(on_hand),
            "cost": round(cost, 2),
            "selling_price": round(float(row["Selling Price"] or 0), 2),
            "retail_price": round(float(row["Retail Price"] or 0), 2),
            "ytd_units": int(ytd_units),
            "mtd_units": int(row["mtd_units"]) if row["mtd_units"] == row["mtd_units"] else 0,
            "monthly_velocity": round(monthly_vel, 2),
            "months_of_excess_stock": round(months_of_excess, 1) if months_of_excess else None,
            "inventory_value_at_cost": round(on_hand * cost, 2),
            "ytd_revenue": round(ytd_revenue, 2),
            "ytd_profit": round(ytd_profit, 2),
            "markup_pct": round(float(row["% Mark Up"] or 0), 1),
            "seasonality": season["label"],
            "seasonality_note": season["note"],
            "prior_year_ytd_units": season["py_ytd_units"],
            "prior_year_full_units": season["py_full_units"],
            "suggested_action": (
                "monitor_seasonal" if season["label"] == "back_half_seasonal"
                else "clearance" if monthly_vel == 0
                else "discount"
            ),
        })

    total_sql = f"SELECT COUNT(*) FROM inventory WHERE Category = '{category}'"
    total = run_inventory_sql_df(total_sql, file_path=path).iloc[0, 0]

    return {
        "analysis": "slow_movers",
        "generated_at": today.isoformat(),
        "category": category,
        "ytd_months": round(ytd_months, 1),
        "max_monthly_units_threshold": max_monthly_units,
        "min_on_hand_threshold": min_on_hand,
        "total_inventory_items": int(total),
        "flagged_count": len(items),
        "items": items,
    }


def analyze_discontinue_candidates(
    category: str = "Cigars",
    max_ytd_units: int = 2,
    min_on_hand: int = 1,
    top_n: int = 50,
    inv_path: str | None = None,
) -> dict:
    """
    Items with extremely low annual sales that should be considered for discontinuation.

    Criteria:
      - YTD Units ≤ max_ytd_units (negligible all year — at most a couple sales/year)
      - On Hand ≥ min_on_hand (stock sitting that must be dealt with)

    Note: We don't filter on WTD/MTD = 0 because for very-low-velocity items the
    handful of YTD sales often register within a single week/month period.

    Results sorted by inventory_value_at_cost descending (highest capital exposure first).
    """
    path = str(inv_path or INVENTORY_FILE)
    today = date.today()
    year, month = today.year, today.month
    ytd_months = _ytd_months()
    prior_year, cutoff_month, cutoff_day = _prior_year_window(path)

    sql = f"""
    WITH tx AS (
      SELECT
        "Item Number" AS item_number,
        SUM(Quantity) FILTER (WHERE EXTRACT(YEAR FROM "Date") = {year})
          AS ytd_units,
        SUM(Quantity) FILTER (WHERE EXTRACT(YEAR FROM "Date") = {year}
                               AND EXTRACT(MONTH FROM "Date") = {month})
          AS mtd_units,
        SUM("Item Amount") FILTER (WHERE EXTRACT(YEAR FROM "Date") = {year})
          AS ytd_revenue,
        SUM(Quantity) FILTER (
          WHERE EXTRACT(YEAR FROM "Date") = {prior_year}
            AND (EXTRACT(MONTH FROM "Date") < {cutoff_month}
                 OR (EXTRACT(MONTH FROM "Date") = {cutoff_month}
                     AND EXTRACT(DAY FROM "Date") <= {cutoff_day}))
        ) AS py_ytd_units,
        SUM(Quantity) FILTER (WHERE EXTRACT(YEAR FROM "Date") = {prior_year})
          AS py_full_units
      FROM transactions
      GROUP BY "Item Number"
    )
    SELECT
        i.Description,
        i.Brand,
        i."Parent Company",
        i."Item Number",
        i."On Hand",
        i.Cost,
        i."Selling Price",
        i."Retail Price",
        i."% Mark Up",
        COALESCE(t.ytd_units, 0)     AS ytd_units,
        COALESCE(t.mtd_units, 0)     AS mtd_units,
        COALESCE(t.ytd_revenue, 0)   AS ytd_revenue,
        COALESCE(t.py_ytd_units, 0)  AS py_ytd_units,
        COALESCE(t.py_full_units, 0) AS py_full_units
    FROM inventory i
    LEFT JOIN tx t ON i."Item Number" = t.item_number
    WHERE i.Category = '{category}'
      AND (i."Verification Notes" IS NULL OR i."Verification Notes" NOT LIKE 'zeroed%')
      AND LOWER(i.Description) != 'open'
      AND COALESCE(t.ytd_units, 0) <= {max_ytd_units}
      AND i."On Hand" >= {min_on_hand}
    ORDER BY (i."On Hand" * i.Cost) DESC NULLS LAST
    LIMIT {top_n}
    """

    df = run_shop_sql_df(sql, inv_path=path)

    items = []
    seasonal_excluded = 0
    for _, row in df.iterrows():
        on_hand = float(row["On Hand"] or 0)
        cost = float(row["Cost"] or 0)
        ytd_units = float(row["ytd_units"] or 0)
        ytd_revenue = float(row["ytd_revenue"] or 0)
        py_ytd = float(row["py_ytd_units"] or 0)
        py_full = float(row["py_full_units"] or 0)
        inv_value = on_hand * cost
        ytd_profit = ytd_revenue - (ytd_units * cost)
        season = _seasonality(ytd_units, py_ytd, py_full, ytd_months, prior_year)

        # Skip items that are simply in their off-season — they sold in the
        # back half of the prior year and are expected to do so again.
        if season["label"] == "back_half_seasonal":
            seasonal_excluded += 1
            continue

        items.append({
            "description": str(row["Description"] or "").strip(),
            "brand": str(row["Brand"] or "").strip(),
            "parent_company": str(row["Parent Company"] or "").strip(),
            "item_number": str(row["Item Number"] or "").strip(),
            "on_hand": int(on_hand),
            "cost": round(cost, 2),
            "selling_price": round(float(row["Selling Price"] or 0), 2),
            "retail_price": round(float(row["Retail Price"] or 0), 2),
            "ytd_units": int(ytd_units),
            "mtd_units": int(row["mtd_units"]) if row["mtd_units"] == row["mtd_units"] else 0,
            "inventory_value_at_cost": round(inv_value, 2),
            "ytd_revenue": round(ytd_revenue, 2),
            "ytd_profit": round(ytd_profit, 2),
            "markup_pct": round(float(row["% Mark Up"] or 0), 1),
            "seasonality": season["label"],
            "seasonality_note": season["note"],
            "prior_year_ytd_units": season["py_ytd_units"],
            "prior_year_full_units": season["py_full_units"],
            "suggested_action": (
                "discontinue_immediately" if ytd_units == 0
                else "clearance_then_discontinue"
            ),
        })

    total_sql = f"SELECT COUNT(*) FROM inventory WHERE Category = '{category}'"
    total = run_inventory_sql_df(total_sql, file_path=path).iloc[0, 0]

    return {
        "analysis": "discontinue_candidates",
        "generated_at": today.isoformat(),
        "category": category,
        "ytd_months": round(ytd_months, 1),
        "prior_year": prior_year,
        "seasonality_window": f"Jan 1 – {cutoff_month}/{cutoff_day}",
        "max_ytd_units_threshold": max_ytd_units,
        "min_on_hand_threshold": min_on_hand,
        "total_inventory_items": int(total),
        "seasonal_excluded": seasonal_excluded,
        "flagged_count": len(items),
        "items": items,
    }


def analyze_top_profitable(
    category: str = "Cigars",
    min_ytd_units: int = 1,
    top_n: int = 25,
    inv_path: str | None = None,
) -> dict:
    """
    Items ranked by YTD profit — the most profitable products to push in selling.

    Also includes a 'stock_adequacy' flag so sellers know if supply is healthy.
    Results sorted by YTD Profit descending.
    """
    path = str(inv_path or INVENTORY_FILE)
    today = date.today()
    year, month = today.year, today.month
    ytd_months = _ytd_months()

    sql = f"""
    WITH tx AS (
      SELECT
        "Item Number" AS item_number,
        SUM(Quantity) FILTER (WHERE EXTRACT(YEAR FROM "Date") = {year})
          AS ytd_units,
        SUM(Quantity) FILTER (WHERE EXTRACT(YEAR FROM "Date") = {year}
                               AND EXTRACT(MONTH FROM "Date") = {month})
          AS mtd_units,
        SUM("Item Amount") FILTER (WHERE EXTRACT(YEAR FROM "Date") = {year})
          AS ytd_revenue
      FROM transactions
      GROUP BY "Item Number"
    )
    SELECT
        i.Description,
        i.Brand,
        i."Parent Company",
        i."Item Number",
        i."On Hand",
        i.Cost,
        i."Selling Price",
        i."% Mark Up",
        t.ytd_units,
        t.mtd_units,
        t.ytd_revenue,
        t.ytd_revenue - (t.ytd_units * i.Cost) AS ytd_profit
    FROM inventory i
    INNER JOIN tx t ON i."Item Number" = t.item_number
    WHERE i.Category = '{category}'
      AND (i."Verification Notes" IS NULL OR i."Verification Notes" NOT LIKE 'zeroed%')
      AND LOWER(i.Description) != 'open'
      AND t.ytd_units >= {min_ytd_units}
      AND t.ytd_revenue > 0
      AND (t.ytd_revenue - (t.ytd_units * i.Cost)) > 0
    ORDER BY ytd_profit DESC
    LIMIT {top_n}
    """

    df = run_shop_sql_df(sql, inv_path=path)

    items = []
    for rank, (_, row) in enumerate(df.iterrows(), 1):
        on_hand = float(row["On Hand"] or 0)
        cost = float(row["Cost"] or 0)
        ytd_units = float(row["ytd_units"] or 0)
        ytd_revenue = float(row["ytd_revenue"] or 0)
        ytd_profit = float(row["ytd_profit"] or 0)
        monthly_vel = ytd_units / ytd_months
        months_of_stock = (on_hand / monthly_vel) if monthly_vel > 0 else None

        stock_adequacy = (
            "out_of_stock" if on_hand == 0
            else "critical" if (months_of_stock or 99) < 0.5
            else "low" if (months_of_stock or 99) < 1.5
            else "adequate"
        )

        items.append({
            "rank": rank,
            "description": str(row["Description"] or "").strip(),
            "brand": str(row["Brand"] or "").strip(),
            "parent_company": str(row["Parent Company"] or "").strip(),
            "item_number": str(row["Item Number"] or "").strip(),
            "on_hand": int(on_hand),
            "cost": round(cost, 2),
            "selling_price": round(float(row["Selling Price"] or 0), 2),
            "ytd_units": int(ytd_units),
            "mtd_units": int(row["mtd_units"]) if row["mtd_units"] == row["mtd_units"] else 0,
            "monthly_velocity": round(monthly_vel, 1),
            "months_of_stock": round(months_of_stock, 1) if months_of_stock else None,
            "ytd_revenue": round(ytd_revenue, 2),
            "ytd_profit": round(ytd_profit, 2),
            "markup_pct": round(float(row["% Mark Up"] or 0), 1),
            "stock_adequacy": stock_adequacy,
        })

    total_sql = f"SELECT COUNT(*) FROM inventory WHERE Category = '{category}'"
    total = run_inventory_sql_df(total_sql, file_path=path).iloc[0, 0]

    return {
        "analysis": "top_profitable",
        "generated_at": today.isoformat(),
        "category": category,
        "ytd_months": round(ytd_months, 1),
        "min_ytd_units_threshold": min_ytd_units,
        "total_inventory_items": int(total),
        "flagged_count": len(items),
        "items": items,
    }


def run_all_analyses(
    category: str = "Cigars",
    inv_path: str | None = None,
) -> dict:
    """Run all four analyses and return a combined dict."""
    return {
        "reorder": analyze_reorder(category=category, inv_path=inv_path),
        "slow_movers": analyze_slow_movers(category=category, inv_path=inv_path),
        "discontinue_candidates": analyze_discontinue_candidates(category=category, inv_path=inv_path),
        "top_profitable": analyze_top_profitable(category=category, inv_path=inv_path),
    }


# ── Claude summarization ──────────────────────────────────────────────────────

_SUMMARIZE_SYSTEM = """You are an inventory analyst for Smoke Shoppe, a premium cigar shop.
You receive structured inventory analysis data and produce concise, actionable summaries.
Focus on what management should do immediately, what to monitor, and any surprises in the data.
Keep responses to 3-5 bullet points per analysis — specific, direct, no fluff."""


def summarize_with_claude(analysis_result: dict) -> str:
    """Use Claude to produce a natural-language summary of an analysis result."""
    client = anthropic.Anthropic(api_key=os.environ.get("ANTHROPIC_API_KEY"))

    analysis_type = analysis_result.get("analysis", "unknown")
    flagged = analysis_result.get("flagged_count", 0)
    items = analysis_result.get("items", [])

    top_items_json = json.dumps(items[:15], indent=2)

    user_msg = (
        f"Analysis: {analysis_type}\n"
        f"Category: {analysis_result.get('category')}\n"
        f"YTD months of data: {analysis_result.get('ytd_months')}\n"
        f"Total items in category: {analysis_result.get('total_inventory_items')}\n"
        f"Items flagged: {flagged}\n\n"
        f"Top flagged items (up to 15):\n{top_items_json}\n\n"
        "Provide 3-5 concise, actionable bullet points. "
        "Lead each bullet with the most important insight or action."
    )

    response = client.messages.create(
        model="claude-sonnet-4-6",
        max_tokens=600,
        system=_SUMMARIZE_SYSTEM,
        messages=[{"role": "user", "content": user_msg}],
    )
    return response.content[0].text if response.content else ""


# ── CLI helpers ───────────────────────────────────────────────────────────────

def _fmt_currency(val: float | None) -> str:
    if val is None:
        return "n/a"
    return f"${val:,.2f}"


def _print_reorder_signals(result: dict, summarize: bool = False) -> None:
    items = result["items"]
    print(f"\n{'='*72}")
    print(f"  REORDER SIGNALS  —  {result['category']}")
    excluded = result.get("discontinued_excluded", 0)
    excluded_str = f"  |  {excluded} discontinued excluded" if excluded else ""
    print(
        f"  {result['flagged_count']} items  |  "
        f"YTD data: {result['ytd_months']} months  |  "
        f"Stockout window: {result['days_threshold']} days"
        + excluded_str
    )
    print(f"{'='*72}")

    urgency_icons = {"critical": "🔴", "high": "🟠", "medium": "🟡", "low": "🟢"}
    status_labels = {
        "out_of_stock": "OUT OF STOCK",
        "stockout_risk": "STOCKOUT RISK",
    }
    trend_icons = {"accelerating": "⬆", "decelerating": "⬇", "stable": "→"}

    current_urgency = None
    for item in items:
        if item["urgency"] != current_urgency:
            current_urgency = item["urgency"]
            label = {"critical": "CRITICAL", "high": "HIGH", "medium": "MEDIUM", "low": "LOW"}[current_urgency]
            print(f"\n  ── {label} ──────────────────────────────────────────────────")

        icon = urgency_icons[item["urgency"]]
        trend = trend_icons.get(item["velocity_trend"], "")
        status = status_labels[item["status"]]
        days = int(item["days_until_stockout"])
        days_str = "0 days left" if days == 0 else f"~{days}d left"

        print(
            f"\n  {icon} [{status}] {item['description']} ({item['brand']})\n"
            f"     On Hand: {item['on_hand']}  ({days_str})\n"
            f"     YTD: {item['ytd_units']} units  MTD: {item['mtd_units']}"
            f"  ~{item['monthly_velocity']}/mo {trend}\n"
            f"     Price: {_fmt_currency(item['selling_price'])}  "
            f"YTD Profit: {_fmt_currency(item['ytd_profit'])}"
        )

    if summarize:
        print(f"\n--- Claude Summary ---")
        print(summarize_with_claude(result))
    print(f"\n{'='*72}")


def _print_slow_movers(result: dict, summarize: bool = False) -> None:
    items = result["items"]
    print(f"\n{'='*72}")
    print(f"  SLOW MOVERS / DISCOUNT CANDIDATES  —  {result['category']}")
    print(
        f"  {result['flagged_count']} items  |  "
        f"Threshold: ≤{result['max_monthly_units_threshold']}/mo  |  "
        f"Min on hand: {result['min_on_hand_threshold']}"
    )
    print(f"{'='*72}")
    season_icons = {
        "back_half_seasonal": "🍂",
        "back_half_weighted": "📉",
        "consistent": "",
        "no_prior_data": "❓",
    }
    for item in items:
        action_icon = "🗑" if item["suggested_action"] == "clearance" else (
            "👀" if item["suggested_action"] == "monitor_seasonal" else "🏷"
        )
        months_str = (
            f"{item['months_of_excess_stock']} mo of stock"
            if item["months_of_excess_stock"] else "no recent sales"
        )
        s_icon = season_icons.get(item.get("seasonality", ""), "")
        s_note = item.get("seasonality_note", "")
        print(
            f"\n  {action_icon} {item['description']} ({item['brand']})\n"
            f"     On Hand: {item['on_hand']}  Velocity: ~{item['monthly_velocity']}/mo  ({months_str})\n"
            f"     Price: {_fmt_currency(item['selling_price'])}  "
            f"Cost: {_fmt_currency(item['cost'])}  "
            f"Inv Value: {_fmt_currency(item['inventory_value_at_cost'])}\n"
            f"     YTD: {item['ytd_units']} units  YTD Revenue: {_fmt_currency(item['ytd_revenue'])}  "
            f"YTD Profit: {_fmt_currency(item['ytd_profit'])}  "
            f"Action: {item['suggested_action'].replace('_', ' ')}\n"
            f"     {s_icon} Seasonality: {s_note}"
        )
    if summarize:
        print(f"\n--- Claude Summary ---")
        print(summarize_with_claude(result))
    print(f"\n{'='*72}")


def _print_discontinue_candidates(result: dict, summarize: bool = False) -> None:
    items = result["items"]
    seasonal_excl = result.get("seasonal_excluded", 0)
    seasonal_str = f"  |  {seasonal_excl} seasonal excluded" if seasonal_excl else ""
    print(f"\n{'='*72}")
    print(f"  DISCONTINUE CANDIDATES  —  {result['category']}")
    print(
        f"  {result['flagged_count']} items  |  "
        f"YTD ≤ {result['max_ytd_units_threshold']} units  |  "
        f"Min on hand: {result['min_on_hand_threshold']}"
        f"{seasonal_str}"
    )
    print(f"{'='*72}")
    season_icons = {"back_half_weighted": "📉", "consistent": "", "no_prior_data": "❓"}
    for item in items:
        action_icon = "❌" if item["suggested_action"] == "discontinue_immediately" else "🏷❌"
        s_icon = season_icons.get(item.get("seasonality", ""), "")
        s_note = item.get("seasonality_note", "")
        print(
            f"\n  {action_icon} {item['description']} ({item['brand']})\n"
            f"     On Hand: {item['on_hand']}  "
            f"Inv Value: {_fmt_currency(item['inventory_value_at_cost'])}\n"
            f"     YTD: {item['ytd_units']} units  MTD: {item['mtd_units']}\n"
            f"     Price: {_fmt_currency(item['selling_price'])}  "
            f"Cost: {_fmt_currency(item['cost'])}  "
            f"Action: {item['suggested_action'].replace('_', ' ')}\n"
            f"     {s_icon} Seasonality: {s_note}"
        )
    if summarize:
        print(f"\n--- Claude Summary ---")
        print(summarize_with_claude(result))
    print(f"\n{'='*72}")


def _print_top_profitable(result: dict, summarize: bool = False) -> None:
    items = result["items"]
    print(f"\n{'='*72}")
    print(f"  TOP PROFITABLE ITEMS  —  {result['category']}")
    print(f"  Top {result['flagged_count']} by YTD Profit  |  YTD data: {result['ytd_months']} months")
    print(f"{'='*72}")
    stock_icons = {"adequate": "✅", "low": "🟡", "critical": "🟠", "out_of_stock": "🔴"}
    for item in items:
        stock_icon = stock_icons.get(item["stock_adequacy"], "")
        months_str = f"{item['months_of_stock']} mo" if item["months_of_stock"] else "n/a"
        print(
            f"\n  #{item['rank']}  {stock_icon} {item['description']} ({item['brand']})\n"
            f"     YTD Profit: {_fmt_currency(item['ytd_profit'])}  "
            f"YTD Revenue: {_fmt_currency(item['ytd_revenue'])}  "
            f"Markup: {item['markup_pct']}%\n"
            f"     Units/mo: {item['monthly_velocity']}  "
            f"On Hand: {item['on_hand']} ({months_str} stock)  "
            f"Price: {_fmt_currency(item['selling_price'])}"
        )
    if summarize:
        print(f"\n--- Claude Summary ---")
        print(summarize_with_claude(result))
    print(f"\n{'='*72}")


def _resolve_items(query: str, df: "pd.DataFrame") -> "pd.Series":
    """Return a boolean mask for rows in df matching query.

    Supports comma-separated terms — each is resolved independently and
    the results are unioned:
        "All Los Statos, All Magic Toast, Knuckle Sandwich"

    Per-term resolution order:
      1. Exact item-number match
      2. Case-insensitive substring match on Description
      3. Case-insensitive substring match on Brand
      4. Case-insensitive substring match on Parent Company

    Leading "All " is stripped from each term — it's a user intent signal
    but all matches are always returned regardless.

    Raises ValueError listing any terms that matched nothing.
    """
    terms = [
        re.sub(r"^all\s+", "", t.strip(), flags=re.IGNORECASE).strip()
        for t in query.split(",")
        if t.strip()
    ]

    combined = pd.Series(False, index=df.index)
    unmatched: list[str] = []

    for term in terms:
        term_lower = term.lower()
        escaped    = re.escape(term_lower)

        # 1. Exact item-number match
        mask = df["Item Number"].astype(str).str.strip() == term
        # 2. Substring on Description
        if not mask.any():
            mask = df["Description"].astype(str).str.lower().str.contains(escaped, na=False)
        # 3. Substring on Brand (catches "Alec Bradley", "Oliva", etc.)
        if not mask.any() and "Brand" in df.columns:
            mask = df["Brand"].astype(str).str.lower().str.contains(escaped, na=False)
        # 4. Substring on Parent Company
        if not mask.any() and "Parent Company" in df.columns:
            mask = df["Parent Company"].astype(str).str.lower().str.contains(escaped, na=False)

        if mask.any():
            combined |= mask
        else:
            unmatched.append(term)

    if unmatched:
        raise ValueError(
            f"No items found matching: {', '.join(repr(t) for t in unmatched)}"
        )

    return combined


def discontinue_item(
    item_number: str,
    reason: str,
    inv_path: str | None = None,
) -> list[dict]:
    """
    Manually mark one or more items as discontinued.

    item_number accepts:
      • An exact item / barcode number  →  matches that one SKU
      • A description substring         →  matches all SKUs containing the text
      • "All <term>"                    →  same as above; "All" signals intent
                                           but all matches are always returned

    Examples:
      "689674013297"        — exact SKU
      "Magic Toast"         — all Magic Toast variants
      "All Los Statos"      — all Los Statos items
      "Espinosa Knuckle"    — all Espinosa Knuckle vitolas

    reason is required. Returns a list of dicts, one per discontinued item.
    Raises ValueError if nothing matches or reason is blank.
    """
    if not reason or not reason.strip():
        raise ValueError("A discontinue reason is required.")

    path = str(inv_path or INVENTORY_FILE)
    df = pd.read_excel(path, header=1)
    mask = _resolve_items(item_number, df)

    if "Discontinued" not in df.columns:
        df["Discontinued"] = ""
    if "Discontinued Reason" not in df.columns:
        df["Discontinued Reason"] = ""

    results = []
    for _, row in df[mask].iterrows():
        results.append({
            "action": "discontinued",
            "item_number": str(row["Item Number"]).strip(),
            "description": str(row["Description"]).strip(),
            "brand": str(row["Brand"]).strip(),
            "on_hand": int(row["On Hand"] or 0),
            "reason": reason.strip(),
            "was_already_discontinued": str(row.get("Discontinued", "")).strip() == "Yes",
        })

    df.loc[mask, "Discontinued"] = "Yes"
    df.loc[mask, "Discontinued Reason"] = reason.strip()

    with pd.ExcelWriter(path, engine="openpyxl") as writer:
        df.to_excel(writer, index=False, startrow=1)

    clear_inventory_cache(path)
    return results


def reenable_item(
    item_number: str,
    inv_path: str | None = None,
) -> list[dict]:
    """
    Manually re-enable discontinued items, setting Discontinued = 'No'.

    A 'No' value is sticky: the auto-refresh will never overwrite it.
    Only an explicit --mark-discontinued call can discontinue it again.

    Accepts the same natural-language query forms as discontinue_item:
      exact item number, description substring, or "All <term>".

    Returns a list of dicts, one per re-enabled item.
    """
    path = str(inv_path or INVENTORY_FILE)
    df = pd.read_excel(path, header=1)
    mask = _resolve_items(item_number, df)

    if "Discontinued" not in df.columns:
        df["Discontinued"] = ""
    if "Discontinued Reason" not in df.columns:
        df["Discontinued Reason"] = ""

    results = []
    for _, row in df[mask].iterrows():
        results.append({
            "action": "re-enabled",
            "item_number": str(row["Item Number"]).strip(),
            "description": str(row["Description"]).strip(),
            "brand": str(row["Brand"]).strip(),
            "on_hand": int(row["On Hand"] or 0),
            "previous_status": str(row.get("Discontinued", "")).strip() or "not discontinued",
        })

    df.loc[mask, "Discontinued"] = "No"
    df.loc[mask, "Discontinued Reason"] = ""

    with pd.ExcelWriter(path, engine="openpyxl") as writer:
        df.to_excel(writer, index=False, startrow=1)

    clear_inventory_cache(path)
    return results


# ── CLI entry point ───────────────────────────────────────────────────────────

if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(levelname)s: %(message)s")

    parser = argparse.ArgumentParser(description="Smoke Shoppe Inventory Analysis Agent")
    parser.add_argument("--low-stock",        action="store_true", help="Reorder signals: OOS, below-min, and stockout risk")
    parser.add_argument("--slow-movers",      action="store_true", help="Slow-moving items to discount")
    parser.add_argument("--discontinue",      action="store_true", help="Discontinue candidates (dead stock)")
    parser.add_argument("--profitable",       action="store_true", help="Top profitable items to push")
    parser.add_argument("--all",              action="store_true", help="Run all four analyses")
    parser.add_argument("--mark-discontinued", metavar="ITEM_NUMBER",
                        help="Manually mark an item discontinued by item number (or description substring)")
    parser.add_argument("--re-enable",        metavar="ITEM_NUMBER",
                        help="Re-enable a discontinued item; locks it against future auto-discontinuation")
    parser.add_argument("--reason",           metavar="TEXT",
                        help="Required with --mark-discontinued: why the item is being discontinued")
    parser.add_argument("--category",    default="Cigars",    help="Inventory category (default: Cigars)")
    parser.add_argument("--days",        type=int, default=30, help="Stockout risk window in days (default 30)")
    parser.add_argument("--top",         type=int, default=None, help="Limit results to top N items (default: all)")
    parser.add_argument("--min-ytd",     type=int, default=3,  help="Min YTD units to count as 'selling' (default 3)")
    parser.add_argument("--summarize",   action="store_true", help="Add Claude natural-language summary")
    parser.add_argument("--json",        action="store_true", help="Output raw JSON")
    args = parser.parse_args()

    if args.re_enable:
        try:
            results = reenable_item(args.re_enable)
        except ValueError as e:
            print(f"Error: {e}", file=__import__("sys").stderr)
            raise SystemExit(1)
        if args.json:
            print(json.dumps(results, indent=2))
        else:
            print(f"\n  Re-enabled {len(results)} item(s) — locked against auto-discontinuation:\n")
            for r in results:
                print(
                    f"    ✅ {r['description']} ({r['brand']})\n"
                    f"       Item #: {r['item_number']}  |  On Hand: {r['on_hand']}  "
                    f"|  Was: {r['previous_status']}"
                )
            print()
        raise SystemExit(0)

    if args.mark_discontinued:
        if not args.reason:
            parser.error("--reason is required when using --mark-discontinued")
        try:
            results = discontinue_item(args.mark_discontinued, args.reason)
        except ValueError as e:
            print(f"Error: {e}", file=__import__("sys").stderr)
            raise SystemExit(1)
        if args.json:
            print(json.dumps(results, indent=2))
        else:
            new_count = sum(1 for r in results if not r["was_already_discontinued"])
            already_count = len(results) - new_count
            summary = f"{new_count} newly discontinued"
            if already_count:
                summary += f", {already_count} already discontinued (reason updated)"
            print(f"\n  Discontinued {len(results)} item(s) — {summary}:\n")
            for r in results:
                tag = " (updated)" if r["was_already_discontinued"] else ""
                print(
                    f"    ❌ {r['description']} ({r['brand']}){tag}\n"
                    f"       Item #: {r['item_number']}  |  On Hand: {r['on_hand']}"
                )
            print(f"\n  Reason: {results[0]['reason']}\n")
        raise SystemExit(0)

    run_any = any([args.low_stock, args.slow_movers, args.discontinue, args.profitable, args.all])
    if not run_any:
        parser.print_help()
        raise SystemExit(0)

    if args.all:
        result = run_all_analyses(category=args.category)
        if args.json:
            print(json.dumps(result, indent=2, default=str))
        else:
            _print_reorder_signals(result["reorder"], args.summarize)
            _print_slow_movers(result["slow_movers"], args.summarize)
            _print_discontinue_candidates(result["discontinue_candidates"], args.summarize)
            _print_top_profitable(result["top_profitable"], args.summarize)
        raise SystemExit(0)

    if args.low_stock:
        kw = {} if args.top is None else {"top_n": args.top}
        result = analyze_reorder(
            category=args.category, days_threshold=args.days,
            min_ytd_units=args.min_ytd, **kw,
        )
        if args.json:
            print(json.dumps(result, indent=2, default=str))
        else:
            _print_reorder_signals(result, args.summarize)

    if args.slow_movers:
        kw = {} if args.top is None else {"top_n": args.top}
        result = analyze_slow_movers(category=args.category, **kw)
        if args.json:
            print(json.dumps(result, indent=2, default=str))
        else:
            _print_slow_movers(result, args.summarize)

    if args.discontinue:
        kw = {} if args.top is None else {"top_n": args.top}
        result = analyze_discontinue_candidates(category=args.category, **kw)
        if args.json:
            print(json.dumps(result, indent=2, default=str))
        else:
            _print_discontinue_candidates(result, args.summarize)

    if args.profitable:
        kw = {} if args.top is None else {"top_n": args.top}
        result = analyze_top_profitable(category=args.category, **kw)
        if args.json:
            print(json.dumps(result, indent=2, default=str))
        else:
            _print_top_profitable(result, args.summarize)
