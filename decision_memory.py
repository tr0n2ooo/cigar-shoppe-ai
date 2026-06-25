"""
decision_memory.py
------------------
Long-term memory for past order recommendations.

Records each recommendation to data/Order_History.json and evaluates past
decisions against actual sales data to surface what worked and what didn't.
The feedback summary is injected into the ToT synthesis prompt so the agent
avoids repeating past mistakes and doubles down on proven winners.

ReAct loop context
------------------
This module implements the cross-session "Observe" step: before reasoning
about a new order the agent observes the outcomes of previous decisions.
The feedback becomes part of the "Reason" context in _synthesize():

  Past decisions (Observe) → feedback summary (Reason) → new order (Act)
"""

from __future__ import annotations

import json
import logging
import re
from datetime import date
from pathlib import Path

log = logging.getLogger(__name__)

DATA_DIR = Path(__file__).parent / "data"
HISTORY_FILE = DATA_DIR / "Order_History.json"
TRANSACTIONS_FILE = DATA_DIR / "Smoke_Shoppe_Transactions.xlsx"


def record_recommendation(result: dict) -> None:
    """Append an order recommendation result to the long-term history file."""
    meta = result.get("metadata", {})
    rec = result.get("recommendation", {})
    orders = rec.get("recommended_orders", [])

    if not orders:
        return  # nothing to record if no new cigars were recommended

    entry = {
        "date": date.today().isoformat(),
        "horizon_days": meta.get("horizon_days"),
        "order_budget": meta.get("order_budget"),
        "new_cigar_budget": meta.get("new_cigar_budget"),
        "craziness": meta.get("craziness"),
        "recommended_cigars": [
            {
                "name": item.get("name") or item.get("Name", ""),
                "brand": item.get("brand") or item.get("Brand", ""),
                "vitola": item.get("vitola", ""),
                "boxes": item.get("boxes"),
                "msrp_per_stick": item.get("msrp_per_stick"),
                "cost_estimate": item.get("cost_estimate"),
                "buzz_score": item.get("buzz_score"),
                "fit_score": item.get("fit_score"),
                "reasoning": (item.get("reasoning") or "")[:300],
            }
            for item in orders
        ],
    }

    history = _load_history()
    history.append(entry)
    try:
        HISTORY_FILE.write_text(json.dumps(history, indent=2, default=str))
        log.info(
            "Recorded order recommendation to %s (%d total entries).",
            HISTORY_FILE.name,
            len(history),
        )
    except OSError as exc:
        log.warning("Could not write order history: %s", exc)


def load_feedback_summary(lookback: int = 5) -> str:
    """
    Return a plain-text feedback block for injection into ordering prompts.

    Compares past recommendations against actual transaction data to identify
    which cigars sold well and which didn't move after being recommended.

    Parameters
    ----------
    lookback : How many recent recommendation runs to include (default 5).
    """
    history = _load_history()
    if not history:
        log.info("[MEMORY] No past order decisions found — first run, no feedback to inject.")
        return ""

    recent = history[-lookback:]
    log.info("[MEMORY] Loaded %d past recommendation run(s) from %s  (using last %d)",
             len(history), HISTORY_FILE.name, len(recent))
    sales_index = _build_sales_index()

    lines: list[str] = [
        "── LONG-TERM MEMORY: Past Order Decision Feedback ──────────────────────────",
        f"(Last {len(recent)} recommendation run(s) — use this to avoid repeating mistakes"
        " and favour proven winners)",
    ]

    for entry in recent:
        rec_date = entry.get("date", "unknown date")
        cigars = entry.get("recommended_cigars", [])
        if not cigars:
            continue

        budget = entry.get("new_cigar_budget")
        budget_str = f"  new-cigar budget ${budget:,.0f}" if budget else ""
        lines.append(f"\n{rec_date}{budget_str}:")

        for c in cigars:
            name = c.get("name", "?")
            brand = c.get("brand", "")
            units, revenue = _lookup_sales(name, brand, sales_index)

            if units is None:
                outcome = "no sales data — may not have been ordered yet"
            elif units == 0:
                outcome = "⚠  in transactions but 0 units sold — slow mover"
            elif units < 5:
                outcome = f"modest: {units} unit(s), ${revenue:.0f} revenue"
            else:
                outcome = f"✓ good seller: {units} unit(s), ${revenue:.0f} revenue"

            lines.append(f"  • {name} ({brand}): {outcome}")

    lines.append("─────────────────────────────────────────────────────────────────────────")
    result = "\n".join(lines)
    bullet_lines = [ln.strip() for ln in lines if ln.strip().startswith("•")]
    log.info("[MEMORY] Injecting feedback into ToT synthesis prompt  (%d cigar outcomes):",
             len(bullet_lines))
    for bl in bullet_lines:
        log.info("  %s", bl)
    return result


def history_count() -> int:
    """Return the number of recorded recommendation runs."""
    return len(_load_history())


# ── internals ─────────────────────────────────────────────────────────────────

def _load_history() -> list[dict]:
    if not HISTORY_FILE.exists():
        return []
    try:
        return json.loads(HISTORY_FILE.read_text())
    except (json.JSONDecodeError, OSError) as exc:
        log.warning("Could not load order history: %s", exc)
        return []


def _build_sales_index() -> dict[str, tuple[int, float]]:
    """Return {normalized_key: (total_units, total_revenue)} from transactions."""
    if not TRANSACTIONS_FILE.exists():
        return {}
    try:
        import pandas as pd

        df = pd.read_excel(TRANSACTIONS_FILE, engine="openpyxl")
        df.columns = [c.strip().lower().replace(" ", "_") for c in df.columns]

        index: dict[str, tuple[int, float]] = {}
        for _, row in df.iterrows():
            pname = str(row.get("product_name", "") or "").strip()
            brand = str(row.get("brand", "") or "").strip()
            qty = float(row.get("quantity", 0) or 0)
            amt = float(row.get("item_amount", 0) or 0)
            key = _norm(f"{brand} {pname}")
            prev_q, prev_r = index.get(key, (0, 0.0))
            index[key] = (int(prev_q + qty), prev_r + amt)
        return index
    except Exception as exc:
        log.warning("Could not build sales index for feedback: %s", exc)
        return {}


def _lookup_sales(
    name: str,
    brand: str,
    index: dict[str, tuple[int, float]],
) -> tuple[int | None, float]:
    """Fuzzy-match a recommended cigar name against the sales index."""
    if not index:
        return None, 0.0

    # Try exact composite key first
    key = _norm(f"{brand} {name}")
    if key in index:
        q, r = index[key]
        return q, r

    # Substring match: find any index key sharing enough words with the name
    name_words = [w for w in _norm(name).split() if len(w) > 3]
    if not name_words:
        return None, 0.0

    threshold = max(1, len(name_words) // 2)
    best_key, best_overlap = None, 0
    for idx_key in index:
        overlap = sum(1 for w in name_words if w in idx_key)
        if overlap > best_overlap and overlap >= threshold:
            best_overlap = overlap
            best_key = idx_key

    if best_key:
        q, r = index[best_key]
        return q, r

    return None, 0.0


def _norm(s: str) -> str:
    return re.sub(r"[^a-z0-9 ]", "", s.lower()).strip()
