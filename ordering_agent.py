"""
ordering_agent.py
-----------------
Tree of Thought ordering agent for Smoke Shoppe.

Evaluates candidate cigars (from the buzz feed or provided directly) using three
independent "thought branches" — conservative, balanced, adventurous — then
synthesizes them into a final order recommendation.

The `craziness` parameter (0–10) controls how far the branches spread:
  Low craziness  → all three branches stay near the fit-focused end of the spectrum.
  High craziness → branches spread wide, with the adventurous branch chasing pure buzz.

Usage:
  python ordering_agent.py                         # 30-day horizon, $5,000 budget, 10% new cigars
  python ordering_agent.py --horizon 7             # 7-day horizon (~$1,167 default budget)
  python ordering_agent.py --horizon 90            # 90-day horizon (~$15,000 default budget)
  python ordering_agent.py --budget 3000           # explicit budget (overrides horizon default)
  python ordering_agent.py --new-cigar-pct 20      # 20% of budget for new cigars, 80% restock
  python ordering_agent.py --new-cigar-budget 500  # $500 for new cigars, rest for restock
  python ordering_agent.py --refresh               # force buzz feed refresh before analyzing
  python ordering_agent.py --stale-months 1        # auto-refresh if cache older than 1 month
  python ordering_agent.py --stale-months 0        # disable auto-refresh
  python ordering_agent.py --slots 5               # recommend 5 new SKUs (default 3)
  python ordering_agent.py --craziness 7           # more adventurous branching (default 5)
  python ordering_agent.py --max-price 22          # filter out cigars above $22/stick
  python ordering_agent.py --json                  # output raw JSON (for piping)
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import re
import sys
from datetime import date
from pathlib import Path
from typing import Any

import anthropic

from sales_agent import analyze_sales_fit, DEFAULT_XLSX
from tools.inventory_tool import run_inventory_sql, run_shop_sql_df
from social_intel_agent import get_buzz_feed, DEFAULT_FIT_PROFILE, _craziness_guidance, BUZZ_FILE
from cigar_researcher import _create_with_backoff
from inventory_agent import analyze_reorder

INVENTORY_FILE = Path(__file__).parent / "data" / "Smoke_Shoppe_Inventory_Verified.xlsx"

# Default staleness window — if the buzz cache file is older than this many months,
# treat it as stale and automatically refresh before running the ordering analysis.
BUZZ_STALE_MONTHS = 3


def _buzz_cache_is_stale(stale_months: int = BUZZ_STALE_MONTHS) -> bool:
    """
    Return True if Cigar_Buzz.xlsx doesn't exist or its last-modified time is
    older than `stale_months` months ago.  A missing file is always stale.
    """
    if not BUZZ_FILE.exists():
        return True
    import time
    age_days = (time.time() - BUZZ_FILE.stat().st_mtime) / 86_400
    return age_days > stale_months * 30

# Vitola words stripped when building line-name keys for inventory matching
# (same spirit as cigar_researcher._VITOLA_WORDS — kept local to avoid coupling)
_VITOLA_RE = re.compile(
    r"\b(robusto|toro|churchill|corona|gordo|belicoso|torpedo|figurado|"
    r"lancero|lonsdale|magnum|perfecto|pyramid|piramide|rothschild|"
    r"presidente|gran|grande|petit|double|triple|short|long|"
    r"no\.?\s*\d+|\d+(?:\s*x\s*\d+)?)\b",
    re.IGNORECASE,
)

# Strips common company-name suffixes so "AJ Fernandez Cigars" == "AJ Fernandez"
_BRAND_SUFFIX_RE = re.compile(
    r"\b(cigars?|tobacco|co\.?|company|inc\.?|llc|ltd\.?)\s*$",
    re.IGNORECASE,
)

# Strips a leading brand abbreviation from inventory descriptions
# e.g. "AJF New World Decenio" → "New World Decenio"
#      "RP Vintage 1992"       → "Vintage 1992"
# Only fires on 2-5 ALL-CAPS letters at the start of the string.
_LEADING_ABBREV_RE = re.compile(r"^[A-Z]{2,5}\s+")


def _normalize_brand(brand: str) -> str:
    """Strip company suffixes: 'AJ Fernandez Cigars' → 'AJ Fernandez'."""
    return _BRAND_SUFFIX_RE.sub("", brand.strip()).strip()


def _clean_desc(brand_norm: str, name: str) -> str:
    """
    Strip leading brand noise from an inventory description so keys are
    comparable against buzz-feed entries that use clean product names.

    Two patterns handled:
      1. ALL-CAPS abbreviation  — 'AJF New World …'  → 'New World …'
      2. Full brand name        — 'Oliva Serie V …'  → 'Serie V …'
                                  (when brand='Oliva Cigars' → norm='Oliva')
    """
    name = name.strip()
    # 1. Strip ALL-CAPS abbreviation prefix (e.g. "AJF", "RP", "LFD")
    name = _LEADING_ABBREV_RE.sub("", name)
    # 2. Strip the normalised brand name if the description starts with it
    if brand_norm:
        prefix = brand_norm.lower()
        if name.lower().startswith(prefix):
            name = name[len(prefix):].strip()
    return name


def _line_key(brand: str, name: str) -> str:
    """
    Normalize brand + product name into a comparable 'line key' by:
      - stripping company suffixes from brand ("AJ Fernandez Cigars" → "AJ Fernandez")
      - stripping leading brand abbreviations / brand name from description
        ("AJF New World Decenio" → "New World Decenio",
         "Oliva Serie V Melanio" when brand=Oliva → "Serie V Melanio")
      - lowercasing and stripping vitola/size words
      - collapsing whitespace and punctuation
    Two SKUs from the same blend line (different vitolas) will share a key.
    """
    brand_norm = _normalize_brand(brand)
    name_norm  = _clean_desc(brand_norm, name)
    text = re.sub(_VITOLA_RE, " ", f"{brand_norm} {name_norm}".lower())
    text = re.sub(r"[^a-z0-9 ]", " ", text)
    return re.sub(r"\s+", " ", text).strip()


def _name_only_key(brand: str, name: str) -> str:
    """
    Name-only fallback key — brand stripped, prefix/abbreviation cleaned,
    vitola words removed.  Used as a secondary match when brand names differ
    between inventory and buzz feed (e.g. 'AJ Fernandez Cigars' vs 'AJ Fernandez').
    Prefix-based matching is used at lookup time to handle extra descriptor tokens
    in inventory descriptions (e.g. 'Osc.Nat.').
    """
    brand_norm = _normalize_brand(brand)
    name_norm  = _clean_desc(brand_norm, name)
    text = re.sub(_VITOLA_RE, " ", name_norm.lower())
    text = re.sub(r"[^a-z0-9 ]", " ", text)
    return re.sub(r"\s+", " ", text).strip()


def _load_inventory_line_keys() -> tuple[set[str], list[str]]:
    """
    Return (full_keys, name_keys) for every cigar currently in stock (On Hand > 0).

    full_keys — brand-normalized + abbrev-stripped line keys (primary match, exact)
    name_keys — name-only keys (secondary fallback, prefix-matched at lookup time)

    Uses a targeted SQL query — never loads the full inventory into memory.
    """
    try:
        rows = run_inventory_sql(
            "SELECT Brand, Description FROM inventory "
            "WHERE Category = 'Cigars' AND \"On Hand\" > 0",
            file_path=str(INVENTORY_FILE),
        )
        full_keys: set[str] = set()
        name_keys: list[str] = []
        for brand, desc in rows:
            b = str(brand or "").strip()
            d = str(desc  or "").strip()
            if b or d:
                full_keys.add(_line_key(b, d))
                nk = _name_only_key(b, d)
                if len(nk) > 4:          # skip keys that are too short to be meaningful
                    name_keys.append(nk)
        return full_keys, name_keys
    except Exception as exc:
        logging.warning("Could not load inventory for stock check: %s", exc)
        return set(), []


def _is_in_stock(brand: str, name: str, full_keys: set[str], name_keys: list[str]) -> bool:
    """
    True if a buzz candidate matches any stocked line.

    Primary check  — exact match on the full brand+name key.
    Fallback check — prefix match on name-only key: the buzz candidate's name
                     key matches if it is a prefix of an inventory name key (or
                     vice-versa).  This handles inventory descriptions with extra
                     descriptor tokens (e.g. 'Osc.Nat.') not present in buzz names.
    """
    if _line_key(brand, name) in full_keys:
        return True
    buzz_nk = _name_only_key(brand, name)
    if len(buzz_nk) <= 4:
        return False
    return any(
        inv_nk.startswith(buzz_nk) or buzz_nk.startswith(inv_nk)
        for inv_nk in name_keys
    )

# ── restock helpers ───────────────────────────────────────────────────────────

DEFAULT_MONTHLY_BUDGET = 5_000.0

# Brand → typical sticks per box (lowercase keys, checked via substring)
_BOX_SIZES: dict[str, int] = {
    # Padrón — 1964 Anniversary and Family Reserve handled in _estimate_box_size
    "padrón":              26,
    "padron":              26,
    # Oliva
    "oliva":               24,
    # My Father / La Flor Dominicana
    "my father":           23,
    "la flor dominicana":  24,
    # Perdomo
    "perdomo":             24,
    # A. Fuente / Opus X
    "arturo fuente":       25,
    # Davidoff / Camacho / Cusano (General Cigar premium)
    "davidoff":            25,
    "camacho":             20,
    # Altadis brands
    "montecristo":         27,
    "romeo y julieta":     25,
    "h. upmann":           25,
    "partagas":            25,
    # General Cigar / STG
    "macanudo":            25,
    "punch":               25,
    "cohiba":              27,
    # Swisher / Drew Estate
    "liga privada":        24,
    "drew estate":         24,
    # Rocky Patel
    "rocky patel":         20,
    # Alec Bradley
    "alec bradley":        20,
    # CAO
    "cao":                 20,
    # Espinosa
    "espinosa":            20,
    # Ashton
    "ashton":              25,
    # Aging Room / Boutique Blends
    "aging room":          20,
    # Warped (boutique — smaller boxes)
    "warped":              16,
    # Crowned Heads
    "crowned heads":       25,
    # Tatuaje (boutique)
    "tatuaje":             25,
    # AJ Fernandez
    "aj fernandez":        20,
    "a.j. fernandez":      20,
}


def _estimate_box_size(brand: str, description: str) -> int:
    """
    Estimate standard box size (sticks/box) from brand + description.

    Checks description-level overrides first (specific sub-lines), then falls
    back to the brand lookup table, then to 20 (the industry default).
    """
    b = (brand or "").lower().strip()
    d = (description or "").lower().strip()

    # Description-level overrides
    if ("padron" in b or "padrón" in b):
        if "family reserve" in d:
            return 10
        if "1964" in d:
            return 25
    if "opus x" in d or "opusx" in d:
        return 25
    if "small batch" in d or "limited" in d:
        return 10  # boutique / limited-run boxes are often 10

    # Brand table lookup (longest-match wins to avoid "cao" matching "camacho")
    matches = [(k, v) for k, v in _BOX_SIZES.items() if k in b]
    if matches:
        return max(matches, key=lambda kv: len(kv[0]))[1]

    return 20  # industry default


def _annotate_restock_costs(
    items: list[dict],
    item_window_units: dict[str, int] | None = None,
    horizon_days: int = 30,
) -> list[dict]:
    """
    Add box-aligned reorder quantities and costs to each reorder item.

    Demand is computed in sticks (velocity × horizon, with seasonal adjustment),
    then rounded UP to the nearest whole box — because cigars can only be
    ordered in full boxes. Minimum Level and Reorder Quantity fields are ignored.

    Fields added:
      box_size            — sticks per box (estimated from brand knowledge)
      demand_units        — raw demand in sticks before box-rounding
      reorder_boxes       — whole boxes to order (ceil(demand_units / box_size))
      reorder_qty         — sticks actually ordered (reorder_boxes × box_size)
      reorder_cost        — wholesale cost (reorder_qty × cost_per_stick)
      seasonal_factor     — ratio of prior-year same-window rate to current velocity
      seasonal_demand_units — demand forecast from seasonal data (sticks, pre-rounding)
    """
    from math import ceil
    result = []
    for item in items:
        cost_per    = float(item.get("cost") or 0.0)
        item_num    = str(item.get("item_number") or "").strip()
        velocity    = float(item.get("monthly_velocity") or 0.0)
        brand       = str(item.get("brand") or "")
        description = str(item.get("description") or "")

        # Baseline demand: velocity × horizon (minimum 1 box worth)
        demand_units = max(6, ceil(velocity * horizon_days / 30.44))

        seasonal_factor        = None
        seasonal_demand_units  = None

        # Seasonal override: use prior-year same-window demand if available
        if item_window_units and item_num in item_window_units and velocity > 0:
            prior_qty = int(item_window_units[item_num])
            if prior_qty > 0:
                prior_monthly_rate    = prior_qty / (horizon_days / 30.44)
                raw_factor            = prior_monthly_rate / velocity
                seasonal_factor       = round(max(0.4, min(3.0, raw_factor)), 2)
                seasonal_demand_units = ceil(velocity * seasonal_factor * horizon_days / 30.44)
                # Only raise demand, never shrink below configured minimum
                demand_units = max(demand_units, seasonal_demand_units)

        # Box-align: always round UP to the nearest full box
        box_size      = _estimate_box_size(brand, description)
        reorder_boxes = ceil(demand_units / box_size) if box_size > 0 else 1
        reorder_qty   = reorder_boxes * box_size

        result.append({
            **item,
            "box_size":             box_size,
            "demand_units":         demand_units,
            "reorder_boxes":        reorder_boxes,
            "reorder_qty":          reorder_qty,
            "reorder_cost":         round(reorder_qty * cost_per, 2),
            "seasonal_factor":      seasonal_factor,
            "seasonal_demand_units": seasonal_demand_units,
        })
    return result


_BRAND_PARENT_CACHE: dict[str, str] | None = None

def _load_brand_parent_lookup() -> dict[str, str]:
    """Return {canonical_brand_lower: parent_company} from Brand_Reference.xlsx."""
    global _BRAND_PARENT_CACHE
    if _BRAND_PARENT_CACHE is not None:
        return _BRAND_PARENT_CACHE
    from pathlib import Path
    import pandas as pd
    ref_path = Path(__file__).parent / "data" / "Brand_Reference.xlsx"
    try:
        df = pd.read_excel(ref_path, header=1)
        lookup: dict[str, str] = {}
        for _, row in df.iterrows():
            brand  = str(row.get("Canonical Brand Name") or "").strip()
            parent = str(row.get("Parent Company / Manufacturer") or "").strip()
            if brand and parent and parent != "— not yet mapped —":
                lookup[brand.lower()] = parent
        _BRAND_PARENT_CACHE = lookup
    except Exception:
        _BRAND_PARENT_CACHE = {}
    return _BRAND_PARENT_CACHE


def _resolve_parent_company(brand: str) -> str:
    """Return parent company for a brand name, falling back to the brand itself."""
    lookup = _load_brand_parent_lookup()
    return lookup.get(brand.lower().strip(), brand) or brand


def _group_order_by_parent_company(
    restock_items: list[dict],
    recommended_orders: list[dict],
) -> dict[str, dict]:
    """
    Build a parent-company-keyed order summary combining restock and new cigars.

    Each group: {"restock": [...], "new_cigars": [...], "group_total_cost": float}
    Sorted by group_total_cost descending so the biggest orders appear first.
    """
    groups: dict[str, dict] = {}

    def _get_group(parent: str) -> dict:
        if parent not in groups:
            groups[parent] = {"restock": [], "new_cigars": [], "group_total_cost": 0.0}
        return groups[parent]

    for item in restock_items:
        parent = str(item.get("parent_company") or "").strip() or _resolve_parent_company(
            str(item.get("brand") or "")
        )
        g = _get_group(parent or "Unknown")
        g["restock"].append(item)
        g["group_total_cost"] = round(g["group_total_cost"] + float(item.get("reorder_cost") or 0), 2)

    for order in recommended_orders:
        brand  = str(order.get("brand") or "")
        parent = _resolve_parent_company(brand)
        g = _get_group(parent or brand or "Unknown")
        g["new_cigars"].append(order)
        g["group_total_cost"] = round(g["group_total_cost"] + float(order.get("cost_estimate") or 0), 2)

    return dict(sorted(groups.items(), key=lambda kv: kv[1]["group_total_cost"], reverse=True))


def _compute_horizon_seasonality(
    horizon_days: int,
    category: str = "Cigars",
) -> dict:
    """
    Compute seasonality data for the upcoming horizon window using the same
    calendar window in the prior year as a baseline.

    Returns:
        seasonal_factor   — store-wide daily-rate ratio vs prior-year annual avg.
                            1.0 = average; 1.25 = 25% above average (peak period).
        period_str        — human-readable window, e.g. "Jun 6 – Jul 5"
        context_note      — one-liner for prompts describing the seasonal trend
        item_window_units — {item_number: int} prior-year window units per SKU
    """
    from datetime import timedelta
    today = date.today()

    # Same calendar window one year back (handle Feb 29 edge case)
    try:
        py_start = date(today.year - 1, today.month, today.day)
    except ValueError:
        py_start = date(today.year - 1, today.month, 28)
    py_end = py_start + timedelta(days=horizon_days)

    window_end_display = today + timedelta(days=horizon_days - 1)
    period_str = (
        f"{today.strftime('%b %-d')} – {window_end_display.strftime('%b %-d')}"
    )

    try:
        # Category is on the inventory table, not the stripped transactions table,
        # so we join to filter correctly.
        store_sql = f"""
        SELECT
          COALESCE(SUM(t.Quantity) FILTER (
            WHERE t."Date" >= DATE '{py_start}' AND t."Date" < DATE '{py_end}'
          ), 0) AS window_units,
          COALESCE(SUM(t.Quantity) FILTER (
            WHERE EXTRACT(YEAR FROM t."Date") = {py_start.year}
          ), 0) AS annual_units
        FROM transactions t
        JOIN inventory i ON t."Item Number" = i."Item Number"
        WHERE i.Category = '{category}' AND t.Quantity > 0
        """
        row = run_shop_sql_df(store_sql).iloc[0]
        window_units = float(row["window_units"])
        annual_units = float(row["annual_units"])

        if annual_units > 0 and window_units > 0:
            window_daily = window_units / horizon_days
            annual_daily = annual_units / 365.0
            seasonal_factor = round(window_daily / annual_daily, 3)
        else:
            seasonal_factor = 1.0

        item_sql = f"""
        SELECT t."Item Number", COALESCE(SUM(t.Quantity), 0) AS window_units
        FROM transactions t
        JOIN inventory i ON t."Item Number" = i."Item Number"
        WHERE t."Date" >= DATE '{py_start}' AND t."Date" < DATE '{py_end}'
          AND i.Category = '{category}' AND t.Quantity > 0
        GROUP BY t."Item Number"
        """
        item_df = run_shop_sql_df(item_sql)
        item_window_units: dict[str, int] = {
            str(r["Item Number"]).strip(): int(r["window_units"])
            for _, r in item_df.iterrows()
        }

        pct = round((seasonal_factor - 1.0) * 100)
        if pct >= 15:
            trend = f"📈 {pct}% above annual average — peak period, order generously"
        elif pct <= -15:
            trend = f"📉 {abs(pct)}% below annual average — slow period, order conservatively"
        else:
            trend = "➡ near annual average"
        context_note = (
            f"{period_str} is {trend} "
            f"(seasonal_factor={seasonal_factor:.2f}, based on {py_start}–{py_end} prior year)"
        )

    except Exception as exc:
        logging.warning("Could not compute horizon seasonality: %s", exc)
        seasonal_factor   = 1.0
        context_note      = f"{period_str}: seasonal data unavailable"
        item_window_units = {}

    return {
        "seasonal_factor":   seasonal_factor,
        "period_str":        period_str,
        "context_note":      context_note,
        "item_window_units": item_window_units,
    }


# ── prompts ───────────────────────────────────────────────────────────────────

_RESTOCK_PRIORITIZATION_SYSTEM = """You are an inventory buyer for Smoke Shoppe, a premium cigar retail store.

Your task: given a list of low-stock items and a limited restock budget, decide which items to include
in the reorder and which to defer. The budget cannot be exceeded.

PRIORITIZATION LOGIC — think through these in order before deciding:
  1. OUT-OF-STOCK first — on_hand=0 means sales are actively being lost right now.
  2. URGENCY tier — critical (< 7 days) > high (7–14 days) > medium (14–21 days) > low (21+ days)
  3. VELOCITY — within the same urgency tier, higher monthly_velocity = more lost revenue per day
  4. PROFIT — higher ytd_profit signals items customers love that also make money for the store
  5. COST EFFICIENCY — if items are otherwise comparable, prefer cheaper reorders so more items fit

KEY RULES:
  - Never let the sum of included reorder_costs exceed the budget
  - Fully restocking one critical item beats half-covering three medium ones
  - If including an expensive item would crowd out multiple cheaper critical items, cut the expensive one
  - Be explicit about trade-offs: "Cut X despite high velocity because its $400 cost would have
    excluded three critical OOS items totaling $180"

Output ONLY valid JSON — no prose, no markdown fences.
Keep reasons very brief (5–8 words max) to stay within response limits.

{
  "reasoning": "<2-4 sentences: overall approach and the key trade-offs you made>",
  "include": [
    {"item_number": "<item_number>", "reason": "<5-8 word phrase>"}
  ],
  "exclude": [
    {"item_number": "<item_number>", "reason": "<5-8 word phrase>"}
  ]
}"""

_BRANCH_SYSTEM = """You are an expert cigar buyer for Smoke Shoppe, a premium cigar shop.

Your task: evaluate a list of candidate cigars using the {strategy_name} strategy and
recommend which ones to order. Think step by step before making your final selections.

{strategy_description}

For each candidate you'll receive:
  - buzz_score: social/online excitement (0-100)
  - overall_fit_score: how well the cigar matches our proven sales profile (0-100)
  - fit dimensions: per-attribute breakdown (wrapper, strength, vitola, price, brand)
  - fit_notes: quick fit summary from the social intel agent
  - comparable_sellers: top comparable cigars we already sell
  - availability: whether we can actually source this cigar

IMPORTANT CONSTRAINTS:
  - Never recommend a cigar flagged as "inaccessible" (Davidoff, OpusX, etc.)
  - Availability issues are a hard filter — we cannot order what we cannot source
  - Pay attention to price — our customers rarely buy above $30/stick
  - If a total order budget is given, keep your combined cost_estimates within it

RECENCY WEIGHTING — apply a scoring boost based on how recently the cigar was announced:
  🔥 Announced ≤ 14 days ago : +25 points to your composite score
  Announced 15–45 days ago   : +15 points
  Announced 46–90 days ago   : +8 points
  Announced 91–180 days ago  : +3 points
  Older or unknown           : no boost
  Rationale: customers follow cigar news and will ask for the newest releases.
  A recently announced cigar with moderate buzz often outperforms an older one
  with higher buzz, because the excitement is current and actionable.

VITOLA SELECTION: If the cigar comes in multiple vitolas, pick the one that best fits our
profile (Toro is #1, Gordo #2, avoid Robusto or Corona unless nothing else available).

BOX SIZE REFERENCE — use your knowledge of each specific cigar line:
  Most premium cigars:          20 sticks/box  (default if unknown)
  Padrón (most lines):          26/box  |  Padrón 1964 Anniversary: 25/box
  Oliva, Perdomo (most lines):  20/box  |  some lines 25/box
  Rocky Patel (most lines):     20/box  |  some lines 25/box
  My Father, La Flor Dominicana: 23/box or 20/box depending on line
  Boutique / limited editions:  often 10, 12, or 15/box
  When unsure, default to 20/box

COST FORMULA (our wholesale cost = 50% of MSRP):
  cost_estimate = boxes × box_size × (msrp_per_stick × 0.50)
  Use the MSRP from the candidate data. If unknown, estimate from the price tier in fit_notes.

Output ONLY valid JSON in this exact format — no prose, no markdown fences:
{{
  "branch": "<strategy_name>",
  "strategy_rationale": "<1-2 sentences explaining this branch's lens>",
  "selections": [
    {{
      "name": "<cigar name>",
      "brand": "<brand>",
      "vitola": "<specific vitola to order — e.g. Toro, Gordo, Robusto>",
      "rank": <1-based integer>,
      "reason": "<why this fits the strategy — 1-2 sentences>",
      "risk": "<main concern or null>",
      "box_size": <sticks per box for this specific cigar>,
      "boxes": <trial order quantity in boxes: 1, 2, or 3>,
      "msrp_per_stick": <estimated MSRP per stick as a number>,
      "cost_estimate": <boxes × box_size × msrp_per_stick × 0.50>,
      "confidence": "<high|medium|low>"
    }}
  ]
}}"""

_BRANCH_STRATEGIES = {
    "conservative": {
        "name": "Conservative",
        "description": (
            "CONSERVATIVE STRATEGY: Minimize risk. Only recommend cigars with "
            "overall_fit_score ≥ 65. Weight your evaluation: fit 75%, buzz 25%. "
            "Favor proven brands already in our portfolio or closely adjacent. "
            "Prefer $12-18 price points. Prioritize cigars that are near-certain sellers "
            "for our existing maduro-loving, medium-full-preferring customer base. "
            "A boring recommendation that actually sells beats an exciting one that doesn't."
        ),
    },
    "balanced": {
        "name": "Balanced",
        "description": (
            "BALANCED STRATEGY: Weigh proven fit and social momentum equally. "
            "Consider cigars with overall_fit_score ≥ 40. Weight: fit 50%, buzz 50%. "
            "Accept moderate profile mismatches if buzz_score is ≥ 70. "
            "This is where you can try adjacent brands or slightly pricier cigars "
            "if the social excitement justifies the risk. Note any mismatches clearly."
        ),
    },
    "adventurous": {
        "name": "Adventurous",
        "description": (
            "ADVENTUROUS STRATEGY: Chase what's hot. Social buzz is the primary signal. "
            "Weight: buzz 70%, fit 30%. No hard fit filter (except inaccessible brands). "
            "Consider cigars that might stretch our customer base to new wrappers, "
            "strengths, or price points. High buzz cigars can attract new customers "
            "and generate excitement. Note fit issues honestly but don't let them veto "
            "a cigar with outstanding social momentum."
        ),
    },
}

_SYNTHESIS_SYSTEM = """You are synthesizing three expert cigar ordering analyses for Smoke Shoppe.

Three branches evaluated the same candidates through different lenses:
  - Conservative: minimize risk, prioritize proven fit
  - Balanced: equal weight to fit and buzz
  - Adventurous: chase buzz, accept mismatches

Your job: produce a final, unified order recommendation.

Rules:
  1. Cigars recommended by ALL THREE branches are "high conviction" picks — include them.
  2. Cigars recommended by TWO branches are strong candidates — include unless there's a clear reason not to.
  3. Cigars only in the adventurous branch are bold picks — include at most one, and flag it clearly.
  4. Never exceed the requested number of slots.
  5. Never recommend inaccessible cigars.
  6. Explain any disagreements between branches and how you resolved them.
  7. RECENCY TIE-BREAKER: When two candidates are otherwise comparable, prefer the more recently
     announced one. Customers follow cigar news — fresh releases drive foot traffic and conversation.
     Note the announcement date in your rationale when recency influenced the decision.

VITOLA: Each recommendation must specify the exact vitola to order (Toro preferred, then Gordo).
  Take the vitola from the branch selections; if branches disagree, pick the one that best fits
  our store profile (Toro #1, Gordo #2).

BOX QUANTITIES & COST:
  Use box_size and boxes from the branch selections (or average/consensus if branches differ).
  cost_per_box = box_size × msrp_per_stick × 0.50
  cost_estimate = boxes × cost_per_box
  total_order_cost = sum of all cost_estimates

BUDGET: If a total order budget is provided in the user message:
  - Ensure total_order_cost ≤ budget.
  - If over budget, reduce boxes on the lowest-conviction items first (min 1 box).
  - If still over budget after reducing to 1 box each, drop the lowest-conviction item(s).
  - Set within_budget accordingly.

Output ONLY valid JSON — no prose, no markdown fences:
{{
  "summary": "<2-3 sentence overview of the recommendation>",
  "recommended_orders": [
    {{
      "rank": <1-based>,
      "name": "<cigar name>",
      "brand": "<brand>",
      "vitola": "<specific vitola — Toro, Gordo, Robusto, etc.>",
      "conviction": "<high|medium|bold>",
      "branches_agreed": ["conservative", "balanced", "adventurous"],
      "rationale": "<why we should order this — 2-3 sentences>",
      "box_size": <sticks per box>,
      "boxes": <number of boxes to order>,
      "msrp_per_stick": <estimated MSRP per stick>,
      "cost_estimate": <boxes × box_size × msrp_per_stick × 0.50>,
      "watch_out_for": "<risk or null>"
    }}
  ],
  "total_order_cost": <sum of all cost_estimates>,
  "within_budget": <true|false|null if no budget was given>,
  "not_recommended": [
    {{"name": "<name>", "brand": "<brand>", "reason": "<why we're passing>"}}
  ],
  "ordering_strategy": "<conservative|balanced|adventurous — which branch best fits current conditions>",
  "branch_consensus": "<brief note on where branches agreed/disagreed>"
}}"""


# ── Tree of Thought ordering agent ─────────────────────────────────────────────

class OrderingAgent:
    """
    Evaluates potential cigar orders using Tree of Thought reasoning.

    Three branches (conservative / balanced / adventurous) each evaluate the
    same candidate set with different fit-vs-buzz weightings. A synthesis step
    combines the branches into a final recommendation.
    """

    def __init__(self, model: str = "claude-sonnet-4-6") -> None:
        self.client = anthropic.Anthropic(api_key=os.environ.get("ANTHROPIC_API_KEY"))
        self.model  = model

    # ── fit profile enrichment ────────────────────────────────────────────────

    def _enrich_with_fit(
        self,
        candidates: list[dict],
        max_price_per_stick: float | None,
        xlsx_path: str = DEFAULT_XLSX,
    ) -> list[dict]:
        """
        Add a detailed fit profile to each candidate by calling analyze_sales_fit.
        Filters out candidates above max_price_per_stick if set.
        """
        enriched = []
        for item in candidates:
            name      = item.get("Name") or item.get("name", "")
            brand     = item.get("Brand") or item.get("brand", "")
            fit_notes = item.get("Fit Notes") or item.get("fit_notes", "")
            summary   = item.get("Summary") or item.get("summary", "")

            # Parse attributes from fit_notes (encoded as "Maduro ✓, Toro ✓, $16 ✓")
            # Fall back to summary text for richer attribute hints
            search_text = f"{fit_notes} {summary}"
            wrapper  = _guess_wrapper(search_text)
            vitola   = _guess_vitola(search_text)
            strength = _guess_strength(search_text)
            msrp     = _guess_price(fit_notes)

            if max_price_per_stick and msrp and msrp > max_price_per_stick:
                continue

            fit_profile = analyze_sales_fit(
                description=name,
                brand=brand,
                wrapper=wrapper,
                strength=strength,
                vitola=vitola,
                msrp=msrp,
                xlsx_path=xlsx_path,
            )

            enriched.append({**item, "_fit_profile": fit_profile})

        return enriched

    # ── restock prioritization ────────────────────────────────────────────────

    def _prioritize_restock(
        self,
        annotated: list[dict],
        restock_budget: float,
        total_cost: float,
    ) -> dict:
        """
        Ask Claude to decide which low-stock items fit within restock_budget and why.
        Returns {reasoning, include: [{item_number, reason}], exclude: [{item_number, reason}]}.
        Falls back to greedy urgency-order fill if the model returns no JSON.
        """
        _URGENCY_ICON = {"critical": "🔴", "high": "🟠", "medium": "🟡", "low": "🟢"}
        shortfall = round(total_cost - restock_budget, 2)

        item_lines = []
        for i, item in enumerate(annotated, 1):
            days   = item.get("days_until_stockout")
            days_s = f"{int(days)}d" if days else "OOS"
            icon   = _URGENCY_ICON.get(item.get("urgency", ""), "")
            box_size     = item.get("box_size", 20)
            reorder_boxes = item.get("reorder_boxes", 1)
            item_lines.append(
                f"{i}. {item.get('description', '?')}"
                + (f" ({item.get('brand', '')})" if item.get("brand") else "")
                + f"\n   item_number: {item.get('item_number', '?')}"
                f"\n   {icon} status: {item.get('status', '?').replace('_', ' ')}  |  "
                f"urgency: {item.get('urgency', '?')}  |  "
                f"days of stock: {days_s}  |  on_hand: {item.get('on_hand', 0)}"
                f"\n   velocity: {item.get('monthly_velocity', 0):.1f} units/mo  |  "
                f"ytd_profit: ${item.get('ytd_profit', 0):,.0f}  |  "
                f"ytd_units: {item.get('ytd_units', 0)}"
                f"\n   reorder: {reorder_boxes} box(es) × {box_size} sticks × "
                f"${item.get('cost', 0):.2f}/stick = ${item.get('reorder_cost', 0):,.2f} wholesale"
            )

        user_message = (
            f"RESTOCK BUDGET: ${restock_budget:,.0f} wholesale\n"
            f"TOTAL COST TO RESTOCK ALL {len(annotated)} ITEMS: ${total_cost:,.0f}  "
            f"(shortfall: ${shortfall:,.0f})\n\n"
            f"ITEMS NEEDING REORDER (sorted by urgency then velocity):\n\n"
            + "\n\n".join(item_lines)
            + "\n\nTask: Decide which items to include in the restock order within the budget. "
            "Show your reasoning, then list included and excluded items with a one-sentence "
            "reason for each."
            "\n\nOutput ONLY the JSON — start with { and end with }."
        )

        result: dict = {}
        messages = [{"role": "user", "content": user_message}]
        try:
            for _turn in range(6):
                response = _create_with_backoff(
                    self.client,
                    model=self.model,
                    max_tokens=8192,
                    system=_RESTOCK_PRIORITIZATION_SYSTEM,
                    messages=messages,
                )
                messages.append({"role": "assistant", "content": response.content})

                if response.stop_reason == "end_turn":
                    for block in response.content:
                        if hasattr(block, "text") and block.text:
                            candidate = _extract_json_object(block.text)
                            if candidate:
                                result = candidate
                                break
                    break

                if response.stop_reason == "max_tokens":
                    logging.debug("Restock prioritization hit max_tokens — continuing…")
                    messages.append({
                        "role": "user",
                        "content": "Continue. Output ONLY the JSON — start with { and end with }.",
                    })
                    continue

                break
        except Exception as exc:
            logging.warning("Restock prioritization Claude call failed: %s", exc)

        if not result:
            logging.warning("Restock prioritization returned no JSON — falling back to urgency order")
            include, exclude, running = [], [], 0.0
            for item in annotated:
                if running + item["reorder_cost"] <= restock_budget + 0.005:
                    include.append({"item_number": item["item_number"], "reason": "within budget (urgency order)"})
                    running += item["reorder_cost"]
                else:
                    exclude.append({"item_number": item["item_number"], "reason": "budget exhausted"})
            result = {
                "reasoning": "Prioritized by urgency tier then monthly velocity (fallback — model did not return JSON).",
                "include": include,
                "exclude": exclude,
            }

        return result

    def _build_restock_section(
        self,
        reorder_result: dict,
        restock_budget: float | None,
        horizon_days: int = 30,
        horizon_seasonality: dict | None = None,
    ) -> dict:
        """
        Convert analyze_reorder() output into an order-ready restock section.

        When budget covers all items → include all, no Claude call needed.
        When budget is insufficient → delegate to _prioritize_restock for Claude-powered
        selection with per-item reasoning.

        horizon_seasonality, if provided, contains per-item prior-year window units
        used to scale reorder_qty to match seasonal demand.
        """
        all_items         = reorder_result.get("items", [])
        item_window_units = (horizon_seasonality or {}).get("item_window_units")
        annotated         = _annotate_restock_costs(all_items, item_window_units, horizon_days)
        total_cost        = round(sum(i["reorder_cost"] for i in annotated), 2)

        if not annotated:
            return {
                "items": [], "items_skipped": [],
                "total_cost_all_items": 0.0, "total_cost_ordered": 0.0,
                "restock_budget_allocated": restock_budget,
                "flagged_count": 0,
                "reasoning": None, "warnings": [],
            }

        # Budget covers everything (or no budget constraint) — include all items
        if restock_budget is None or total_cost <= restock_budget + 0.005:
            return {
                "items": annotated,
                "items_skipped": [],
                "total_cost_all_items": total_cost,
                "total_cost_ordered": total_cost,
                "restock_budget_allocated": restock_budget,
                "flagged_count": reorder_result.get("flagged_count", 0),
                "reasoning": None,
                "warnings": [],
            }

        # Budget is insufficient — ask Claude to prioritize
        logging.info(
            "Restock budget $%s cannot cover all %d items ($%s needed) — "
            "calling Claude to prioritize…",
            f"{restock_budget:,.0f}", len(annotated), f"{total_cost:,.0f}",
        )
        prio = self._prioritize_restock(annotated, restock_budget, total_cost)

        include_nums    = {e["item_number"] for e in prio.get("include", [])}
        include_reasons = {e["item_number"]: e.get("reason", "") for e in prio.get("include", [])}
        exclude_reasons = {e["item_number"]: e.get("reason", "") for e in prio.get("exclude", [])}

        ordered, skipped, running = [], [], 0.0
        for item in annotated:
            num = item["item_number"]
            if num in include_nums and running + item["reorder_cost"] <= restock_budget + 0.005:
                ordered.append({**item, "include_reason": include_reasons.get(num, "")})
                running += item["reorder_cost"]
            else:
                skipped.append({**item, "exclude_reason": exclude_reasons.get(num, "")})

        shortfall = round(total_cost - restock_budget, 2)
        return {
            "items": ordered,
            "items_skipped": skipped,
            "total_cost_all_items": total_cost,
            "total_cost_ordered": round(running, 2),
            "restock_budget_allocated": restock_budget,
            "flagged_count": reorder_result.get("flagged_count", 0),
            "reasoning": prio.get("reasoning", ""),
            "warnings": [
                f"⚠  Restock needs ${total_cost:,.0f} but only ${restock_budget:,.0f} allocated "
                f"(${shortfall:,.0f} shortfall). {len(skipped)} item(s) deferred — "
                "see 'reasoning' for prioritization logic."
            ],
        }

    # ── branch evaluation ─────────────────────────────────────────────────────

    def _evaluate_branch(
        self,
        branch_key: str,
        candidates: list[dict],
        slots: int,
        order_budget: float | None = None,
        seasonal_context: str | None = None,
    ) -> dict:
        """Run one ToT branch — returns parsed JSON dict with selections."""
        strategy = _BRANCH_STRATEGIES[branch_key]
        system   = _BRANCH_SYSTEM.format(
            strategy_name=strategy["name"],
            strategy_description=strategy["description"],
        )

        # Build candidate summary for the prompt
        candidate_lines = []
        for i, c in enumerate(candidates, 1):
            fit   = c.get("_fit_profile", {})
            dims  = fit.get("dimensions", {})
            avail = fit.get("availability", "accessible")
            comparable = fit.get("comparable_sellers", [])
            comp_str = (
                ", ".join(f"{s['product']} (${s['revenue']:,.0f} rev)" for s in comparable[:3])
                if comparable else "none found"
            )

            announced = c.get("Announced Date") or c.get("announced_date")
            days_ago  = _recency_days(announced)
            recency   = _recency_label(days_ago)

            candidate_lines.append(
                f"{i}. {c.get('Name') or c.get('name')} ({c.get('Brand') or c.get('brand')})\n"
                f"   buzz_score={c.get('Buzz Score') or c.get('buzz_score', 0)}  "
                f"overall_fit_score={fit.get('overall_fit_score', 'n/a')}  "
                f"availability={avail}\n"
                f"   announced={announced or 'unknown'}  recency={recency}\n"
                f"   wrapper={dims.get('wrapper', {}).get('input') or 'unknown'}  "
                f"wrapper_score={dims.get('wrapper', {}).get('score', '?')}\n"
                f"   strength={dims.get('strength', {}).get('input') or 'unknown'}  "
                f"strength_score={dims.get('strength', {}).get('score', '?')}\n"
                f"   vitola={dims.get('vitola', {}).get('input') or 'unknown'}  "
                f"vitola_score={dims.get('vitola', {}).get('score', '?')}\n"
                f"   price={dims.get('price', {}).get('tier', 'unknown')}  "
                f"price_score={dims.get('price', {}).get('score', '?')}  "
                f"msrp=${dims.get('price', {}).get('msrp') or '?'}\n"
                f"   fit_notes: {c.get('Fit Notes') or c.get('fit_notes') or 'none'}\n"
                f"   summary: {(c.get('Summary') or c.get('summary') or '')[:120]}\n"
                f"   comparable_sellers: {comp_str}"
            )

        budget_line = (
            f"TOTAL ORDER BUDGET (new cigars): ${order_budget:,.0f}  "
            f"(keep sum of all cost_estimates ≤ this)\n\n"
            if order_budget else ""
        )
        seasonal_line = (
            f"SEASONAL CONTEXT: {seasonal_context}\n"
            "→ Adjust box quantities to reflect this period's expected demand. "
            "In a peak period, add 1 extra box on high-conviction items. "
            "In a slow period, default to 1 box to avoid overstock.\n\n"
            if seasonal_context else ""
        )
        user_message = (
            f"STORE FIT PROFILE:\n{DEFAULT_FIT_PROFILE}\n\n"
            + budget_line
            + seasonal_line
            + f"CANDIDATES ({len(candidates)} cigars):\n\n"
            + "\n\n".join(candidate_lines)
            + f"\n\nTASK: Using the {strategy['name']} strategy, select exactly {slots} cigar(s) "
            f"to recommend ordering (or fewer if not enough quality candidates meet the bar). "
            f"Remember: never recommend inaccessible cigars. "
            f"Include vitola, box_size, boxes, msrp_per_stick, and cost_estimate for each.\n\n"
            "Output ONLY the JSON object — start with { and end with }."
        )

        messages = [{"role": "user", "content": user_message}]
        result_json: dict = {}

        for _turn in range(6):
            response = _create_with_backoff(
                self.client,
                model=self.model,
                max_tokens=3000,
                system=system,
                messages=messages,
            )
            messages.append({"role": "assistant", "content": response.content})

            if response.stop_reason == "end_turn":
                for block in response.content:
                    if hasattr(block, "text") and block.text:
                        candidate = _extract_json_object(block.text)
                        if candidate:
                            result_json = candidate
                            break
                break

            if response.stop_reason == "max_tokens":
                messages.append({
                    "role": "user",
                    "content": "Continue. Output ONLY the JSON — start with { and end with }.",
                })
                continue

            break

        if not result_json:
            logging.warning("Branch %s returned no JSON", branch_key)
            result_json = {"branch": strategy["name"], "selections": []}

        return result_json

    # ── synthesis ─────────────────────────────────────────────────────────────

    def _synthesize(
        self,
        branches: dict[str, dict],
        slots: int,
        candidates: list[dict],
        order_budget: float | None = None,
        seasonal_context: str | None = None,
    ) -> dict:
        """Combine three branch recommendations into a final order recommendation."""
        branch_summaries = []
        for key, result in branches.items():
            sels = result.get("selections", [])
            rationale = result.get("strategy_rationale", "")
            items_str = "\n".join(
                f"  {s.get('rank', i+1)}. {s.get('name')} ({s.get('brand')}) "
                f"— vitola: {s.get('vitola', '?')}  "
                f"box_size: {s.get('box_size', '?')}  boxes: {s.get('boxes', '?')}  "
                f"msrp: ${s.get('msrp_per_stick', '?')}  "
                f"cost_estimate: ${s.get('cost_estimate', '?')}  "
                f"recency: {_recency_label(_recency_days(s.get('announced_date')))}  "
                f"[confidence: {s.get('confidence', '?')}]  {s.get('reason', '')}"
                for i, s in enumerate(sels)
            )
            branch_summaries.append(
                f"=== {key.upper()} BRANCH ===\n"
                f"Rationale: {rationale}\n"
                f"Selections:\n{items_str or '  (none)'}"
            )

        all_names = [c.get("Name") or c.get("name", "") for c in candidates]
        budget_line = (
            f"\nTOTAL ORDER BUDGET (new cigars): ${order_budget:,.0f}  "
            f"— ensure total_order_cost ≤ this; reduce boxes or drop items if needed.\n"
            if order_budget else "\nNo order budget specified — within_budget should be null.\n"
        )
        seasonal_line = (
            f"\nSEASONAL CONTEXT: {seasonal_context}\n"
            "→ When finalizing box quantities, reflect this period's seasonal demand. "
            "Branches may have already adjusted boxes — honor those adjustments or "
            "increase/decrease by 1 box as appropriate for the season.\n"
            if seasonal_context else ""
        )

        user_message = (
            "\n\n".join(branch_summaries)
            + f"\n\nFULL CANDIDATE LIST (for your 'not_recommended' section):\n"
            + "\n".join(f"  - {n}" for n in all_names)
            + budget_line
            + seasonal_line
            + f"\nTASK: Synthesize the above branches into a final order recommendation "
            f"of {slots} cigar(s). Include vitola, box_size, boxes, msrp_per_stick, "
            f"and cost_estimate for each item. Compute total_order_cost. "
            "Output ONLY the JSON — start with { and end with }."
        )

        messages = [{"role": "user", "content": user_message}]
        result_json: dict = {}

        for _turn in range(6):
            response = _create_with_backoff(
                self.client,
                model=self.model,
                max_tokens=4000,
                system=_SYNTHESIS_SYSTEM,
                messages=messages,
            )
            messages.append({"role": "assistant", "content": response.content})

            if response.stop_reason == "end_turn":
                for block in response.content:
                    if hasattr(block, "text") and block.text:
                        candidate = _extract_json_object(block.text)
                        if candidate:
                            result_json = candidate
                            break
                break

            if response.stop_reason == "max_tokens":
                messages.append({
                    "role": "user",
                    "content": "Continue. Output ONLY the JSON — start with { and end with }.",
                })
                continue

            break

        return result_json

    # ── main entry point ──────────────────────────────────────────────────────

    def generate_order_recommendation(
        self,
        candidates: list[dict] | None = None,
        refresh_buzz: bool = False,
        stale_months: int = BUZZ_STALE_MONTHS,
        slots: int = 3,
        candidate_pool: int = 25,
        max_price_per_stick: float | None = None,
        order_budget: float | None = None,
        new_cigar_pct: float = 10,
        horizon_days: int = 30,
        craziness: int = 5,
        xlsx_path: str = DEFAULT_XLSX,
    ) -> dict:
        """
        Full Tree of Thought ordering analysis, now including low-stock reorder recommendations.

        candidates          — explicit list of cigar dicts (name, brand, buzz_score, etc.)
                              If None, uses the current buzz feed (auto-refreshed if stale).
        refresh_buzz        — force a buzz feed refresh regardless of cache age.
        stale_months        — treat the buzz cache as stale if it hasn't been refreshed
                              within this many months (default 3). Set to 0 to disable
                              auto-refresh based on age.
        slots               — how many new SKUs to recommend.
        candidate_pool      — number of buzz candidates to request on refresh
                              (or the minimum desired from cache). Default 25.
        max_price_per_stick — filter out candidates above this MSRP per stick.
        order_budget        — total $ budget for the whole order (wholesale cost).
                              Defaults to $5,000 × (horizon_days / 30) if not specified.
                              Synthesis will trim quantities / drop items to stay within budget.
        new_cigar_pct       — 0–100: percentage of order_budget for new cigar recommendations.
                              Default 10 (10% new cigars, 90% restocking). Set to 0 for
                              restock-only, 100 for new-cigars-only.
        horizon_days        — planning horizon in days (default 30 = one month). Controls:
                                • default budget scaling ($5,000/mo)
                                • stockout-risk window passed to analyze_reorder()
        craziness           — 0-10, controls branch spread:
                              0 = all branches stay conservative
                              10 = adventurous branch goes wild
        xlsx_path           — path to transactions XLSX (for comparable-sellers SQL).

        Returns a dict with: branches, recommendation, restock, budget_warnings, metadata.
        """
        craziness     = max(0, min(10, craziness))
        new_cigar_pct = max(0.0, min(100.0, new_cigar_pct))
        horizon_days  = max(1, horizon_days)

        # ── Default budget: $5,000/month scaled to horizon ────────────────────
        if order_budget is None:
            order_budget = round(DEFAULT_MONTHLY_BUDGET * horizon_days / 30, 2)
            logging.info(
                "No budget specified — using default $%s (%d-day horizon at $5,000/mo)",
                f"{order_budget:,.0f}", horizon_days,
            )

        # ── Budget split ──────────────────────────────────────────────────────
        budget_warnings: list[str] = []
        new_cigar_budget = round(order_budget * new_cigar_pct / 100, 2)
        restock_budget   = round(order_budget * (1 - new_cigar_pct / 100), 2)
        logging.info(
            "Budget: $%s total  |  %.0f%% new cigars ($%s)  |  "
            "%.0f%% restock ($%s)  |  horizon: %d days",
            f"{order_budget:,.0f}", new_cigar_pct, f"{new_cigar_budget:,.0f}",
            100 - new_cigar_pct, f"{restock_budget:,.0f}", horizon_days,
        )

        # ── Seasonality context ───────────────────────────────────────────────
        logging.info("Computing horizon seasonality (%d days)…", horizon_days)
        horizon_seasonality = _compute_horizon_seasonality(horizon_days)
        seasonal_context    = horizon_seasonality.get("context_note")

        # ── Restock: call inventory low-stock analysis ────────────────────────
        logging.info(
            "Fetching low-stock reorder signals from inventory agent (horizon=%dd)…",
            horizon_days,
        )
        try:
            reorder_result = analyze_reorder(days_threshold=horizon_days)
        except Exception as exc:
            logging.warning("Could not fetch reorder signals: %s", exc)
            reorder_result = {"items": [], "flagged_count": 0}

        # ── Budget exhaustion: if restock demand ≥ full budget, reallocate everything ──
        _raw_restock = reorder_result.get("items", [])
        if _raw_restock:
            _preview = _annotate_restock_costs(
                _raw_restock,
                horizon_seasonality.get("item_window_units"),
                horizon_days,
            )
            _total_restock_preview = sum(i.get("reorder_cost", 0) for i in _preview)
            if _total_restock_preview >= order_budget:
                logging.info(
                    "Budget exhaustion: restock needs $%s ≥ total $%s — skipping new-cigar ToT",
                    f"{_total_restock_preview:,.0f}", f"{order_budget:,.0f}",
                )
                budget_warnings.append(
                    f"⚠  Restock demand (${_total_restock_preview:,.0f}) meets or exceeds the "
                    f"total budget of ${order_budget:,.0f}. Full budget allocated to restocking; "
                    "no new cigar recommendations generated."
                )
                new_cigar_budget = 0.0
                restock_budget   = order_budget

        restock_section = self._build_restock_section(
            reorder_result, restock_budget, horizon_days, horizon_seasonality
        )

        # Surface restock budget warnings at the top level
        budget_warnings.extend(restock_section.get("warnings", []))

        if new_cigar_budget <= 0:
            if new_cigar_pct <= 0:
                budget_warnings.append(
                    "⚠  No budget allocated for new cigar recommendations "
                    f"(new_cigar_pct={new_cigar_pct:.0f}%). "
                    "Raise new_cigar_pct to include new SKUs in this order."
                )
            import time as _time
            buzz_age_days = (
                round((_time.time() - BUZZ_FILE.stat().st_mtime) / 86_400, 1)
                if BUZZ_FILE.exists() else None
            )
            return {
                "metadata": {
                    "candidates_evaluated": 0,
                    "already_in_stock_filtered": False,
                    "candidate_pool_requested": candidate_pool,
                    "slots_requested": slots,
                    "craziness": craziness,
                    "branch_craziness": {},
                    "max_price_per_stick": max_price_per_stick,
                    "order_budget": order_budget,
                    "horizon_days": horizon_days,
                    "new_cigar_pct": new_cigar_pct,
                    "new_cigar_budget": 0.0,
                    "restock_budget": restock_budget,
                    "combined_order_cost": restock_section.get("total_cost_ordered", 0),
                    "craziness_guidance": _craziness_guidance(craziness),
                    "seasonal_factor":    horizon_seasonality.get("seasonal_factor"),
                    "seasonal_period":    horizon_seasonality.get("period_str"),
                    "seasonal_context":   seasonal_context,
                    "buzz_cache_age_days": buzz_age_days,
                    "buzz_auto_refreshed": False,
                },
                "budget_warnings": budget_warnings,
                "branches": {},
                "recommendation": {
                    "summary": "Budget fully allocated to restocking. No new cigar recommendations generated.",
                    "recommended_orders": [],
                    "total_order_cost": 0,
                    "within_budget": True,
                    "not_recommended": [],
                },
                "restock": restock_section,
                "order_by_parent_company": _group_order_by_parent_company(
                    restock_section.get("items", []), []
                ),
            }

        # 1. Get candidates
        buzz_auto_refreshed = False
        if candidates is None:
            # Auto-detect staleness unless the caller already requested a refresh
            stale = not refresh_buzz and stale_months > 0 and _buzz_cache_is_stale(stale_months)
            do_refresh = refresh_buzz or stale
            buzz_auto_refreshed = do_refresh

            if stale:
                logging.info(
                    "Buzz cache is older than %d months — triggering automatic refresh…",
                    stale_months,
                )
            logging.info("Loading buzz feed (refresh=%s, pool=%d)…", do_refresh, candidate_pool)
            candidates = get_buzz_feed(
                refresh=do_refresh,
                target_count=candidate_pool,
                craziness=craziness,
            )
            if len(candidates) < candidate_pool:
                logging.info(
                    "Buzz cache has %d items (requested %d). "
                    "Run `python main.py social --buzz --target %d` to expand it.",
                    len(candidates), candidate_pool, candidate_pool,
                )

        if not candidates:
            return {
                "error": "No candidate cigars found. Run `python main.py social --buzz` first.",
                "recommendation": None,
            }

        # 2. Filter out anything we already stock
        full_keys, name_keys = _load_inventory_line_keys()
        if full_keys or name_keys:
            before = len(candidates)
            candidates = [
                c for c in candidates
                if not _is_in_stock(
                    c.get("Brand") or c.get("brand", ""),
                    c.get("Name")  or c.get("name",  ""),
                    full_keys,
                    name_keys,
                )
            ]
            removed = before - len(candidates)
            if removed:
                logging.info(
                    "Filtered %d candidate(s) already in inventory; %d remain.",
                    removed, len(candidates),
                )

        if not candidates:
            return {
                "error": "All buzz-feed candidates are already in your inventory.",
                "recommendation": None,
            }

        # 3. Enrich with fit profiles (filter by per-stick price cap if set)
        logging.info("Enriching %d candidates with fit profiles…", len(candidates))
        enriched = self._enrich_with_fit(candidates, max_price_per_stick, xlsx_path)

        if not enriched:
            return {
                "error": "All candidates were filtered out (max price too low or no data).",
                "recommendation": None,
            }

        # 3. Determine branch craziness levels
        branch_crazy = {
            "conservative": max(0, craziness - 3),
            "balanced":     craziness,
            "adventurous":  min(10, craziness + 3),
        }

        logging.info(
            "Running ToT branches (conservative=%d, balanced=%d, adventurous=%d)…",
            branch_crazy["conservative"], branch_crazy["balanced"], branch_crazy["adventurous"],
        )

        # 4. Run three branches (with new-cigar budget slice, not the full budget)
        # When new_cigar_pct < 100, only the new-cigar slice is passed so the
        # synthesis doesn't over-allocate into the restock share.
        tot_budget = new_cigar_budget if order_budget is not None else None

        branches: dict[str, dict] = {}
        for branch_key in ("conservative", "balanced", "adventurous"):
            logging.info("  Evaluating %s branch…", branch_key)
            branches[branch_key] = self._evaluate_branch(
                branch_key=branch_key,
                candidates=enriched,
                slots=slots,
                order_budget=tot_budget,
                seasonal_context=seasonal_context,
            )

        # 5. Synthesize
        logging.info("Synthesizing branches…")
        recommendation = self._synthesize(
            branches, slots, enriched,
            order_budget=tot_budget,
            seasonal_context=seasonal_context,
        )

        import time
        buzz_age_days = (
            round((time.time() - BUZZ_FILE.stat().st_mtime) / 86_400, 1)
            if BUZZ_FILE.exists() else None
        )

        # Combined order cost (new cigars + restock)
        tot_new_cost = recommendation.get("total_order_cost") or 0
        tot_restock_cost = restock_section.get("total_cost_ordered") or 0
        combined_cost = round(tot_new_cost + tot_restock_cost, 2)

        return {
            "metadata": {
                "candidates_evaluated": len(enriched),
                "already_in_stock_filtered": len(full_keys) > 0,
                "candidate_pool_requested": candidate_pool,
                "slots_requested": slots,
                "craziness": craziness,
                "branch_craziness": branch_crazy,
                "max_price_per_stick": max_price_per_stick,
                "order_budget": order_budget,
                "horizon_days": horizon_days,
                "new_cigar_pct": new_cigar_pct,
                "new_cigar_budget": new_cigar_budget,
                "restock_budget": restock_budget,
                "combined_order_cost": combined_cost,
                "craziness_guidance": _craziness_guidance(craziness),
                "seasonal_factor":    horizon_seasonality.get("seasonal_factor"),
                "seasonal_period":    horizon_seasonality.get("period_str"),
                "seasonal_context":   seasonal_context,
                "buzz_cache_age_days": buzz_age_days,
                "buzz_auto_refreshed": buzz_auto_refreshed,
            },
            "budget_warnings": budget_warnings,
            "branches": branches,
            "recommendation": recommendation,
            "restock": restock_section,
            "order_by_parent_company": _group_order_by_parent_company(
                restock_section.get("items", []),
                recommendation.get("recommended_orders", []),
            ),
        }


# ── public API ────────────────────────────────────────────────────────────────

_agent: OrderingAgent | None = None


def _get_agent() -> OrderingAgent:
    global _agent
    if _agent is None:
        _agent = OrderingAgent()
    return _agent


def generate_order_recommendation(
    candidates: list[dict] | None = None,
    refresh_buzz: bool = False,
    stale_months: int = BUZZ_STALE_MONTHS,
    slots: int = 3,
    candidate_pool: int = 25,
    max_price_per_stick: float | None = None,
    order_budget: float | None = None,
    new_cigar_pct: float = 10,
    horizon_days: int = 30,
    craziness: int = 5,
) -> dict:
    """Public entry point — wraps OrderingAgent.generate_order_recommendation."""
    return _get_agent().generate_order_recommendation(
        candidates=candidates,
        refresh_buzz=refresh_buzz,
        stale_months=stale_months,
        slots=slots,
        candidate_pool=candidate_pool,
        max_price_per_stick=max_price_per_stick,
        order_budget=order_budget,
        new_cigar_pct=new_cigar_pct,
        horizon_days=horizon_days,
        craziness=craziness,
    )


# ── helpers ───────────────────────────────────────────────────────────────────

def _extract_json_object(text: str) -> dict:
    """Pull a JSON object {...} from a model response."""
    import re
    if not text:
        return {}
    text = text.strip()
    # Try whole text first
    try:
        val = json.loads(text)
        if isinstance(val, dict):
            return val
    except json.JSONDecodeError:
        pass
    # Find {...} block
    match = re.search(r'\{[\s\S]*\}', text)
    if match:
        try:
            return json.loads(match.group(0))
        except json.JSONDecodeError:
            pass
    # Strip fences
    clean = re.sub(r'^```(?:json)?\s*', '', text, flags=re.MULTILINE)
    clean = re.sub(r'```\s*$', '', clean, flags=re.MULTILINE).strip()
    try:
        return json.loads(clean)
    except json.JSONDecodeError:
        pass
    return {}


def _recency_days(announced_date: str | None) -> int | None:
    """
    Parse an 'Announced Date' string (many formats) and return how many days
    ago it was relative to today.  Returns None if unparseable.

    Handles: "2026-05-15", "May 2026", "May 15, 2026", "Q2 2026", "2026", etc.
    """
    if not announced_date:
        return None
    s = str(announced_date).strip()
    today = date.today()

    from datetime import datetime
    # Try common explicit formats first
    for fmt in ("%Y-%m-%d", "%B %d, %Y", "%b %d, %Y", "%B %Y", "%b %Y"):
        try:
            d = datetime.strptime(s, fmt).date()
            return (today - d).days
        except ValueError:
            pass
    # Quarter: "Q1 2026" → mid-quarter
    m = re.match(r"Q([1-4])\s+(\d{4})", s)
    if m:
        q, yr = int(m.group(1)), int(m.group(2))
        month = (q - 1) * 3 + 2   # middle month of quarter
        try:
            d = date(yr, month, 15)
            return (today - d).days
        except ValueError:
            pass
    # Bare year: "2026"
    if re.fullmatch(r"\d{4}", s):
        try:
            d = date(int(s), 6, 15)   # assume mid-year
            return (today - d).days
        except ValueError:
            pass
    return None


def _recency_label(days: int | None) -> str:
    """Human-readable recency label for display in the prompt."""
    if days is None:
        return "unknown"
    if days <= 14:
        return f"🔥 {days}d ago (very recent)"
    if days <= 45:
        return f"{days}d ago (recent)"
    if days <= 90:
        return f"{days}d ago (last 3 months)"
    if days <= 180:
        return f"{days}d ago (last 6 months)"
    return f"{days}d ago (older)"


def _guess_wrapper(fit_notes: str) -> str:
    """Extract wrapper type from fit_notes string like 'Maduro ✓, Toro ✓, $16 ✓'."""
    if not fit_notes:
        return ""
    notes_lower = fit_notes.lower()
    for wrapper in ("maduro", "connecticut", "habano", "sumatra", "natural",
                    "oscuro", "colorado", "claro", "broadleaf"):
        if wrapper in notes_lower:
            return wrapper.capitalize()
    return ""


def _guess_vitola(fit_notes: str) -> str:
    """Extract vitola from fit_notes string."""
    if not fit_notes:
        return ""
    notes_lower = fit_notes.lower()
    for vitola in ("toro", "gordo", "robusto", "corona", "churchill",
                   "torpedo", "belicoso", "figurado", "lancero", "lonsdale"):
        if vitola in notes_lower:
            return vitola.capitalize()
    return ""


def _guess_strength(fit_notes: str) -> str:
    """Extract strength from fit_notes string."""
    if not fit_notes:
        return ""
    notes_lower = fit_notes.lower()
    for strength in ("medium-full", "medium full", "full", "medium", "mild"):
        if strength in notes_lower:
            return strength.title()
    return ""


def _guess_price(fit_notes: str) -> float | None:
    """Extract MSRP from fit_notes string like '... $16 ✓'."""
    import re
    if not fit_notes:
        return None
    match = re.search(r'\$(\d+(?:\.\d+)?)', fit_notes)
    if match:
        try:
            return float(match.group(1))
        except ValueError:
            pass
    return None


# ── CLI ───────────────────────────────────────────────────────────────────────

def _print_recommendation(result: dict) -> None:
    """Pretty-print a recommendation result to stdout."""
    if result.get("error"):
        print(f"\nError: {result['error']}")
        return

    meta = result.get("metadata", {})
    rec  = result.get("recommendation", {})

    budget        = meta.get("order_budget")
    max_price     = meta.get("max_price_per_stick")
    new_cigar_pct = meta.get("new_cigar_pct", 10)
    horizon_days  = meta.get("horizon_days", 30)
    new_cigar_bud = meta.get("new_cigar_budget")
    restock_bud   = meta.get("restock_budget")

    print(f"\n{'='*70}")
    print(f"  SMOKE SHOPPE — ORDER RECOMMENDATION")
    seasonal_ctx  = meta.get("seasonal_context", "")
    horizon_str   = f"{horizon_days}-day horizon"
    budget_str    = f"${budget:,.0f} budget" if budget else ""
    split_str     = f"{new_cigar_pct:.0f}% new / {100-new_cigar_pct:.0f}% restock"
    price_str     = f"  Max price/stick: ${max_price:.0f}" if max_price else ""
    print(f"  {horizon_str}  |  {budget_str}  |  {split_str}  |  "
          f"Craziness: {meta.get('craziness', '?')}/10  |  "
          f"Candidates: {meta.get('candidates_evaluated', '?')}  |  "
          f"Slots: {meta.get('slots_requested', '?')}"
          + (f"  |{price_str}" if price_str else ""))
    if seasonal_ctx:
        print(f"  Seasonality: {seasonal_ctx}")
    print(f"{'='*70}\n")

    # Budget warnings (restock shortfall, no new-cigar budget, etc.)
    for w in result.get("budget_warnings", []):
        print(w)
    if result.get("budget_warnings"):
        print()

    if not rec:
        print("Synthesis returned no recommendation.")
        return

    print(f"SUMMARY: {rec.get('summary', '')}\n")
    print(f"Strategy chosen: {rec.get('ordering_strategy', '?').upper()}")
    print(f"Branch consensus: {rec.get('branch_consensus', '')}\n")

    orders = rec.get("recommended_orders", [])
    if orders:
        print(f"RECOMMENDED ORDERS ({len(orders)}):")
        print("-" * 70)
        for o in orders:
            conviction = o.get("conviction", "?")
            icon = {"high": "★★★", "medium": "★★☆", "bold": "★☆☆"}.get(conviction, "?")
            agreed = ", ".join(o.get("branches_agreed", []))
            box_size = o.get("box_size", "?")
            boxes    = o.get("boxes", "?")
            msrp     = o.get("msrp_per_stick")
            cost     = o.get("cost_estimate")
            vitola   = o.get("vitola", "?")
            msrp_str = f"  MSRP ${msrp:.2f}/stick" if isinstance(msrp, (int, float)) else ""
            cost_str = f"  Est. cost ${cost:,.0f}" if isinstance(cost, (int, float)) else ""
            print(f"\n  {o.get('rank', '?')}. {o.get('name')} ({o.get('brand')})  — {vitola}")
            print(f"     Conviction: {icon} {conviction.upper()}  |  Branches: {agreed}")
            print(f"     Order: {boxes} box(es) × {box_size} sticks/box"
                  + msrp_str + cost_str)
            print(f"     {o.get('rationale', '')}")
            if o.get("watch_out_for"):
                print(f"     ⚠ {o['watch_out_for']}")

        # Budget summary
        total = rec.get("total_order_cost")
        within = rec.get("within_budget")
        if total is not None:
            print(f"\n  {'─'*50}")
            budget_status = ""
            if within is True:
                budget_status = f"  ✓ within ${budget:,.0f} budget" if budget else ""
            elif within is False:
                budget_status = f"  ✗ OVER ${budget:,.0f} budget" if budget else ""
            print(f"  TOTAL ESTIMATED WHOLESALE COST: ${total:,.0f}{budget_status}")

    not_rec = rec.get("not_recommended", [])
    if not_rec:
        print(f"\n\nPASSED ON ({len(not_rec)}):")
        for nr in not_rec:
            print(f"  - {nr.get('name')} ({nr.get('brand')}): {nr.get('reason', '')}")

    # ── Restock section ───────────────────────────────────────────────────────
    restock = result.get("restock", {})
    restock_items    = restock.get("items", [])
    restock_skipped  = restock.get("items_skipped", [])
    restock_cost_all = restock.get("total_cost_all_items", 0)
    restock_cost_ord = restock.get("total_cost_ordered", 0)
    restock_alloc    = restock.get("restock_budget_allocated")
    restock_count    = restock.get("flagged_count", 0)
    restock_reason   = restock.get("reasoning")

    print(f"\n\n{'='*70}")
    print(f"  LOW-STOCK REORDER  ({restock_count} items flagged, {horizon_days}-day window)")
    if restock_alloc is not None:
        print(f"  Budget allocated: ${restock_alloc:,.0f}  |  "
              f"Total needed: ${restock_cost_all:,.0f}  |  "
              f"Ordering: ${restock_cost_ord:,.0f}")
    else:
        print(f"  Wholesale cost to restock all: ${restock_cost_all:,.0f}  "
              f"(no budget constraint applied)")
    print(f"{'='*70}")

    if restock_reason:
        print(f"\n  PRIORITIZATION REASONING:\n  {restock_reason}\n")

    _urgency_icon = {"critical": "🔴", "high": "🟠", "medium": "🟡", "low": "🟢"}

    if restock_items:
        print(f"ITEMS TO REORDER ({len(restock_items)}, ${restock_cost_ord:,.0f} wholesale):")
        print("-" * 70)
        for item in restock_items:
            icon     = _urgency_icon.get(item.get("urgency", ""), "")
            status   = item.get("status", "").replace("_", " ")
            desc     = item.get("description", "?")
            brand    = item.get("brand", "")
            on_hand  = item.get("on_hand", 0)
            reorder_qty = item.get("reorder_qty", 0)
            cost     = item.get("cost", 0.0)
            tot_cost = item.get("reorder_cost", 0.0)
            vel      = item.get("monthly_velocity", 0)
            days     = item.get("days_until_stockout")
            days_str = f"{int(days)}d" if days is not None else "OOS"
            incl_reason = item.get("include_reason", "")
            print(
                f"\n  {icon} {desc}"
                + (f" ({brand})" if brand else "")
            )
            print(f"     Status: {status}  |  On hand: {on_hand}  |  "
                  f"Days of stock: {days_str}  |  Velocity: {vel:.1f}/mo")
            box_size      = item.get("box_size", 20)
            reorder_boxes = item.get("reorder_boxes", 1)
            demand_units  = item.get("demand_units", reorder_qty)
            sf            = item.get("seasonal_factor")
            seasonal_tag  = ""
            if sf is not None:
                if sf >= 1.15:
                    seasonal_tag = f"  📈 seasonal ×{sf:.2f}"
                elif sf <= 0.85:
                    seasonal_tag = f"  📉 seasonal ×{sf:.2f}"
            print(f"     Reorder: {reorder_boxes} box(es) × {box_size} sticks/box "
                  f"= {reorder_qty} sticks  ×  ${cost:.2f}/stick  =  ${tot_cost:,.2f} wholesale"
                  + seasonal_tag)
            if demand_units != reorder_qty:
                print(f"     (demand: {demand_units} sticks → rounded up to {reorder_boxes} full box(es))")
            if incl_reason:
                print(f"     → {incl_reason}")
    else:
        print("\n  No low-stock items flagged — inventory looks healthy.")

    if restock_skipped:
        print(f"\n  DEFERRED — budget exhausted ({len(restock_skipped)} items, "
              f"${sum(i.get('reorder_cost', 0) for i in restock_skipped):,.0f} wholesale):")
        print("  " + "-" * 60)
        for item in restock_skipped:
            icon = _urgency_icon.get(item.get("urgency", ""), "")
            excl_reason = item.get("exclude_reason", "")
            print(f"    {icon} {item.get('description', '?')}  "
                  f"(${item.get('reorder_cost', 0):,.0f} wholesale)")
            if excl_reason:
                print(f"       → {excl_reason}")

    # Combined totals
    combined = meta.get("combined_order_cost")
    if combined is not None:
        tot_new = rec.get("total_order_cost") or 0
        print(f"\n  {'─'*50}")
        print(f"  New cigars:  ${tot_new:,.0f}")
        print(f"  Restocking:  ${restock_cost_ord:,.0f}")
        status_str = ""
        if budget:
            status_str = f"  ✓ within ${budget:,.0f}" if combined <= budget else f"  ✗ over ${budget:,.0f}"
        print(f"  COMBINED:    ${combined:,.0f}{status_str}")

    # ── Order grouped by parent company ──────────────────────────────────────
    grouped = result.get("order_by_parent_company", {})
    if grouped:
        print(f"\n\n{'='*70}")
        print("  ORDER SUMMARY BY PARENT COMPANY")
        print(f"{'='*70}")
        for parent, group in grouped.items():
            group_total = group.get("group_total_cost", 0)
            print(f"\n  {parent}  (${group_total:,.2f} wholesale)")
            print(f"  {'─'*60}")
            for item in group.get("restock", []):
                boxes     = item.get("reorder_boxes", 1)
                box_size  = item.get("box_size", 20)
                qty       = item.get("reorder_qty", 0)
                cost      = item.get("reorder_cost", 0)
                desc      = item.get("description", "?")
                print(f"    [RESTOCK] {desc}")
                print(f"             {boxes} box(es) × {box_size}/box = {qty} sticks  "
                      f"→  ${cost:,.2f}")
            for order in group.get("new_cigars", []):
                boxes     = order.get("boxes", 1)
                box_size  = order.get("box_size", 20)
                cost      = order.get("cost_estimate", 0)
                name      = order.get("name", "?")
                vitola    = order.get("vitola", "")
                conv      = order.get("conviction", "")
                print(f"    [NEW]     {name}" + (f" — {vitola}" if vitola else ""))
                print(f"             {boxes} box(es) × {box_size}/box  "
                      f"→  ${cost:,.2f}  [{conv}]")

    print(f"\n{'='*70}")

    print("\nBRANCH DETAIL:")
    for branch_key, branch_result in result.get("branches", {}).items():
        sels = branch_result.get("selections", [])
        names = ", ".join(
            f"{s.get('name', '?')} ({s.get('vitola', '?')})" for s in sels
        ) or "(none)"
        print(f"  {branch_key.capitalize():13s}: {names}")


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(levelname)s: %(message)s")

    parser = argparse.ArgumentParser(description="Smoke Shoppe Ordering Agent (Tree of Thought)")
    parser.add_argument("--refresh",      action="store_true", help="Force buzz feed refresh before analyzing")
    parser.add_argument("--stale-months", type=int,   default=BUZZ_STALE_MONTHS,
                        help=f"Auto-refresh if buzz cache is older than N months (default {BUZZ_STALE_MONTHS}; 0=disable)")
    parser.add_argument("--slots",        type=int,   default=3,    help="Number of new SKUs to recommend (default 3)")
    parser.add_argument("--pool",         type=int,   default=25,   help="Candidate pool size: how many buzz cigars to consider (default 25)")
    parser.add_argument("--craziness",    type=int,   default=5,    help="0-10: 0=safe, 10=pure buzz (default 5)")
    parser.add_argument("--horizon",      type=int,   default=30,
                        help="Planning horizon in days (default 30). Scales the default budget "
                             "($5,000/mo) and sets the stockout-risk window for reorder signals.")
    parser.add_argument("--budget",       type=float, default=None,
                        help="Total order budget in $ wholesale. Defaults to $5,000 × (horizon/30) "
                             "if not set (e.g. $5,000 for 30 days, $1,167 for 7 days).")
    parser.add_argument("--new-cigar-pct", type=float, default=10,
                        help="Percent of budget for new cigars (0-100, default 10). "
                             "Remainder goes to restocking low-stock items. "
                             "Mutually exclusive with --new-cigar-budget.")
    parser.add_argument("--new-cigar-budget", type=float, default=None,
                        help="Fixed dollar amount for new cigars. "
                             "Overrides --new-cigar-pct when set (budget must also be specified).")
    parser.add_argument("--max-price",    type=float, default=None, help="Max MSRP per stick to consider (filters candidates)")
    parser.add_argument("--json",         action="store_true", help="Output raw JSON instead of pretty-print")
    args = parser.parse_args()

    # Resolve new_cigar_pct from either form
    effective_budget = args.budget  # None → auto-computed inside generate_order_recommendation
    if args.new_cigar_budget is not None:
        resolved_budget = args.budget or DEFAULT_MONTHLY_BUDGET * args.horizon / 30
        new_cigar_pct = min(100.0, max(0.0, args.new_cigar_budget / resolved_budget * 100))
    else:
        new_cigar_pct = max(0.0, min(100.0, args.new_cigar_pct))

    result = generate_order_recommendation(
        refresh_buzz=args.refresh,
        stale_months=args.stale_months,
        slots=args.slots,
        candidate_pool=args.pool,
        craziness=max(0, min(10, args.craziness)),
        order_budget=effective_budget,
        new_cigar_pct=new_cigar_pct,
        horizon_days=args.horizon,
        max_price_per_stick=args.max_price,
    )

    if args.json:
        print(json.dumps(result, indent=2, default=str))
    else:
        _print_recommendation(result)
