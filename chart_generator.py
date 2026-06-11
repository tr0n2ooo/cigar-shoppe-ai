"""
chart_generator.py
------------------
Generate Plotly figures from MCP tool outputs for inline Chainlit display.
Called from ui.py after each tool completes; returns None when no chart applies.

Public API:
    from chart_generator import make_chart
    fig = make_chart("get_top_profitable", raw_json_string)
    # fig is a plotly.graph_objects.Figure, or None
"""
from __future__ import annotations

import json
import logging
from typing import Any

try:
    import plotly.graph_objects as go
    from plotly.subplots import make_subplots
    _PLOTLY = True
except ImportError:
    _PLOTLY = False


# ── palette (matches Smoke Shoppe brand) ─────────────────────────────────────

_AMBER      = "#C8902A"   # aged amber — bar fills, titles
_AMBER_LIGHT = "#E8B040"  # highlight amber — trend lines
_BROWN      = "#4A2C17"   # deep brown — kept for data series that need contrast
_CREAM      = "#FFF8EE"

# Chart surface colours (dark theme — matches Chainlit UI)
_BG_PAPER   = "#1e1610"   # cedar surface for chart card
_BG_PLOT    = "#241c15"   # slightly lighter inner plot area
_GRID       = "#3d2f1e"   # walnut grid lines
_TEXT       = "#f0e6d0"   # aged parchment — all axis/title/legend text
_MUTED      = "#9e8060"   # pale tobacco — annotation and secondary text

_SLATE      = "#9e8060"   # vline annotations

_URGENCY_COLOR = {
    "critical": "#C0392B",
    "high":     "#E67E22",
    "medium":   "#F1C40F",
    "low":      "#27AE60",
}

_STOCK_COLOR = {
    "out_of_stock": "#C0392B",
    "critical":     "#E67E22",
    "low":          "#F39C12",
    "adequate":     "#27AE60",
}

_FONT = dict(family="Calibri, sans-serif", color=_TEXT, size=12)


def _base_layout(**overrides) -> dict:
    base = dict(
        font=_FONT,
        paper_bgcolor=_BG_PAPER,
        plot_bgcolor=_BG_PLOT,
        margin=dict(l=8, r=20, t=36, b=8),
        hoverlabel=dict(bgcolor="#2c2010", font_size=11, font_color=_TEXT),
        xaxis=dict(showgrid=True, gridcolor=_GRID, zeroline=False,
                   tickfont=dict(color=_TEXT), title_font=dict(color=_TEXT)),
        yaxis=dict(showgrid=False, automargin=True,
                   tickfont=dict(color=_TEXT), title_font=dict(color=_TEXT)),
    )
    base.update(overrides)
    return base


def _trim(label: str, n: int = 32) -> str:
    return label if len(label) <= n else label[:n - 1] + "…"


def _parse(output: str) -> dict | None:
    try:
        d = json.loads(output)
        return d if isinstance(d, dict) else None
    except Exception:
        return None


# ── chart builders ────────────────────────────────────────────────────────────

def _chart_top_profitable(data: dict) -> go.Figure | None:
    items = data.get("items", [])
    if not items:
        return None

    items = items[:15]
    labels   = [_trim(i["description"]) for i in reversed(items)]
    profits  = [i["ytd_profit"] for i in reversed(items)]
    colors   = [_STOCK_COLOR.get(i.get("stock_adequacy", "adequate"), _AMBER) for i in reversed(items)]
    hover    = [
        f"<b>{i['description']}</b><br>"
        f"Brand: {i.get('brand','')}<br>"
        f"YTD profit: ${i['ytd_profit']:,.0f}<br>"
        f"YTD revenue: ${i.get('ytd_revenue',0):,.0f}<br>"
        f"Margin: {i.get('margin_pct',0):.1f}%<br>"
        f"Velocity: {i.get('monthly_velocity',0):.1f}/mo<br>"
        f"Stock: {i.get('stock_adequacy','').replace('_',' ')}"
        for i in reversed(items)
    ]

    fig = go.Figure(go.Bar(
        x=profits, y=labels,
        orientation="h",
        marker_color=colors,
        hovertemplate="%{customdata}<extra></extra>",
        customdata=hover,
    ))
    ytd_months = data.get("ytd_months", "?")
    fig.update_layout(
        **_base_layout(
            title=dict(text=f"Top profitable SKUs — YTD ({ytd_months} months)", font=dict(size=13, color=_AMBER)),
            height=max(320, len(items) * 32 + 100),
            xaxis_title="YTD profit ($)",
            xaxis=dict(showgrid=True, gridcolor=_GRID, zeroline=False,
                       tickprefix="$", tickformat=",.0f"),
        )
    )
    # Legend for stock adequacy colours
    for label, color in [("Adequate", "#27AE60"), ("Low stock", "#F39C12"),
                          ("Critical", "#E67E22"), ("OOS", "#C0392B")]:
        fig.add_trace(go.Bar(x=[None], y=[None], marker_color=color,
                             name=label, showlegend=True))
    fig.update_layout(legend=dict(orientation="h", yanchor="bottom", y=1.02,
                                   xanchor="right", x=1, font_size=10))
    return fig


def _chart_reorder_signals(data: dict) -> go.Figure | None:
    items = data.get("items", [])
    if not items:
        return None

    items = items[:20]

    # Sort: OOS first (days=0), then by days ascending
    sorted_items = sorted(items, key=lambda i: (i.get("days_until_stockout") or 0), reverse=True)
    labels  = [_trim(i["description"]) for i in sorted_items]
    days    = [i.get("days_until_stockout") or 0 for i in sorted_items]
    colors  = [_URGENCY_COLOR.get(i.get("urgency", "low"), _AMBER) for i in sorted_items]
    hover   = [
        f"<b>{i['description']}</b><br>"
        f"Brand: {i.get('brand','')}<br>"
        f"Urgency: {i.get('urgency','')}<br>"
        f"On hand: {i.get('on_hand',0)}<br>"
        f"Days of stock: {'OOS' if not i.get('days_until_stockout') else int(i['days_until_stockout'])}<br>"
        f"Velocity: {i.get('monthly_velocity',0):.1f}/mo"
        for i in sorted_items
    ]

    fig = go.Figure(go.Bar(
        x=days, y=labels,
        orientation="h",
        marker_color=colors,
        hovertemplate="%{customdata}<extra></extra>",
        customdata=hover,
    ))

    # Reference lines at 7 and 14 days
    for x, label, dash in [(7, "7d", "dot"), (14, "14d", "dash")]:
        fig.add_vline(x=x, line_dash=dash, line_color=_SLATE, line_width=1,
                      annotation_text=label, annotation_font_size=9,
                      annotation_position="top")

    threshold = data.get("days_threshold", 30)
    fig.update_layout(
        **_base_layout(
            title=dict(text=f"Days of stock remaining (≤{threshold}-day window)", font=dict(size=13, color=_AMBER)),
            height=max(320, len(items) * 30 + 120),
            xaxis_title="Days of stock",
        )
    )
    for label, color in [("Critical <7d", "#C0392B"), ("High 7-14d", "#E67E22"),
                          ("Medium 14-21d", "#F1C40F"), ("Low 21+d", "#27AE60")]:
        fig.add_trace(go.Bar(x=[None], y=[None], marker_color=color,
                             name=label, showlegend=True))
    fig.update_layout(legend=dict(orientation="h", yanchor="bottom", y=1.02,
                                   xanchor="right", x=1, font_size=10))
    return fig


def _chart_slow_movers(data: dict) -> go.Figure | None:
    items = data.get("items", [])
    if not items:
        return None

    items = items[:15]
    items_sorted = sorted(items,
                          key=lambda i: i.get("months_of_excess_stock") or 0,
                          reverse=False)

    labels    = [_trim(i["description"]) for i in items_sorted]
    months_ex = [i.get("months_of_excess_stock") or 0 for i in items_sorted]
    inv_vals  = [i.get("inventory_value_at_cost", 0) for i in items_sorted]
    max_val   = max(inv_vals) if inv_vals else 1

    # Color intensity by inventory value at cost
    colors = [
        f"rgba({int(192 * v/max_val + 200*(1-v/max_val))}, "
        f"{int(57 * v/max_val + 130*(1-v/max_val))}, "
        f"{int(43 * v/max_val + 200*(1-v/max_val))}, 0.85)"
        for v in inv_vals
    ]
    # Simpler: use a fixed amber scale
    colors = [
        f"rgba(200, 134, 10, {max(0.25, min(1.0, 0.3 + 0.7 * v / max_val))})"
        for v in inv_vals
    ]

    hover = [
        f"<b>{i['description']}</b><br>"
        f"Brand: {i.get('brand','')}<br>"
        f"Months of excess: {i.get('months_of_excess_stock','?')}<br>"
        f"On hand: {i.get('on_hand',0)}<br>"
        f"Velocity: {i.get('monthly_velocity',0):.2f}/mo<br>"
        f"Inventory value: ${i.get('inventory_value_at_cost',0):,.0f}<br>"
        f"Action: {i.get('suggested_action','').replace('_',' ')}"
        for i in items_sorted
    ]

    fig = go.Figure(go.Bar(
        x=months_ex, y=labels,
        orientation="h",
        marker_color=colors,
        hovertemplate="%{customdata}<extra></extra>",
        customdata=hover,
    ))
    fig.add_vline(x=12, line_dash="dot", line_color=_SLATE, line_width=1,
                  annotation_text="12 mo", annotation_font_size=9)
    fig.update_layout(
        **_base_layout(
            title=dict(text="Slow movers — months of excess stock", font=dict(size=13, color=_AMBER)),
            height=max(320, len(items) * 30 + 100),
            xaxis_title="Months of excess stock",
        )
    )
    return fig


def _chart_discontinue(data: dict) -> go.Figure | None:
    items = data.get("items", [])
    if not items:
        return None

    items = items[:20]
    items_sorted = sorted(items,
                          key=lambda i: i.get("inventory_value_at_cost", 0),
                          reverse=False)

    labels   = [_trim(i["description"]) for i in items_sorted]
    inv_vals = [i.get("inventory_value_at_cost", 0) for i in items_sorted]
    ytd      = [i.get("ytd_units", 0) for i in items_sorted]
    colors   = ["#C0392B" if u == 0 else "#E67E22" if u == 1 else "#F39C12"
                for u in ytd]
    hover = [
        f"<b>{i['description']}</b><br>"
        f"Brand: {i.get('brand','')}<br>"
        f"YTD units sold: {i.get('ytd_units',0)}<br>"
        f"On hand: {i.get('on_hand',0)}<br>"
        f"Inventory value: ${i.get('inventory_value_at_cost',0):,.0f}<br>"
        f"Suggested action: {i.get('suggested_action','').replace('_',' ')}"
        for i in items_sorted
    ]

    fig = go.Figure(go.Bar(
        x=inv_vals, y=labels,
        orientation="h",
        marker_color=colors,
        hovertemplate="%{customdata}<extra></extra>",
        customdata=hover,
    ))
    fig.update_layout(
        **_base_layout(
            title=dict(text="Dead stock — capital tied up in discontinue candidates", font=dict(size=13, color=_AMBER)),
            height=max(320, len(items) * 30 + 100),
            xaxis_title="Inventory value at cost ($)",
            xaxis=dict(showgrid=True, gridcolor=_GRID, zeroline=False,
                       tickprefix="$", tickformat=",.0f"),
        )
    )
    for label, color in [("0 units YTD", "#C0392B"), ("1 unit YTD", "#E67E22"),
                          ("2 units YTD", "#F39C12")]:
        fig.add_trace(go.Bar(x=[None], y=[None], marker_color=color,
                             name=label, showlegend=True))
    fig.update_layout(legend=dict(orientation="h", yanchor="bottom", y=1.02,
                                   xanchor="right", x=1, font_size=10))
    return fig


def _chart_top_brands(data: dict) -> go.Figure | None:
    rows = data.get("rows", [])
    if not rows:
        return None

    rows = rows[:15]
    rows_sorted = sorted(rows, key=lambda r: r.get("revenue", 0), reverse=False)
    labels  = [_trim(str(r.get("brand", "?"))) for r in rows_sorted]
    revenue = [r.get("revenue", 0) for r in rows_sorted]
    units   = [r.get("units", 0) for r in rows_sorted]
    hover   = [
        f"<b>{r.get('brand','')}</b><br>"
        f"YTD revenue: ${r.get('revenue',0):,.0f}<br>"
        f"YTD units: {r.get('units',0):,}<br>"
        f"Avg price/stick: ${r.get('avg_price',0):.2f}"
        for r in rows_sorted
    ]

    fig = go.Figure(go.Bar(
        x=revenue, y=labels,
        orientation="h",
        marker_color=_AMBER,
        hovertemplate="%{customdata}<extra></extra>",
        customdata=hover,
    ))
    period = data.get("period", "YTD")
    fig.update_layout(
        **_base_layout(
            title=dict(text=f"Top brands by revenue — {period}", font=dict(size=13, color=_AMBER)),
            height=max(320, len(rows) * 32 + 100),
            xaxis_title="Revenue ($)",
            xaxis=dict(showgrid=True, gridcolor=_GRID, zeroline=False,
                       tickprefix="$", tickformat=",.0f"),
        )
    )
    return fig


def _chart_revenue_trend(data: dict) -> go.Figure | None:
    rows = data.get("rows", [])
    if not rows:
        return None

    labels  = [r.get("period", "") for r in rows]
    revenue = [r.get("revenue", 0) for r in rows]
    units   = [r.get("units", 0) for r in rows]
    hover   = [
        f"<b>{r.get('period','')}</b><br>"
        f"Revenue: ${r.get('revenue',0):,.0f}<br>"
        f"Units: {r.get('units',0):,}"
        for r in rows
    ]

    fig = make_subplots(specs=[[{"secondary_y": True}]])
    fig.add_trace(go.Bar(
        x=labels, y=revenue,
        name="Revenue ($)",
        marker_color=_AMBER,
        hovertemplate="%{customdata}<extra></extra>",
        customdata=hover,
    ), secondary_y=False)
    fig.add_trace(go.Scatter(
        x=labels, y=units,
        name="Units sold",
        mode="lines+markers",
        line=dict(color=_AMBER_LIGHT, width=2),
        marker=dict(size=6),
    ), secondary_y=True)

    fig.update_layout(
        **_base_layout(
            title=dict(text="Monthly revenue & unit trend", font=dict(size=13, color=_AMBER)),
            height=320,
            legend=dict(orientation="h", yanchor="bottom", y=1.02, xanchor="right", x=1, font_size=10),
            xaxis=dict(showgrid=False, tickangle=-35),
        )
    )
    fig.update_yaxes(title_text="Revenue ($)", tickprefix="$", tickformat=",.0f",
                     title_font=dict(color=_TEXT), tickfont=dict(color=_TEXT),
                     secondary_y=False, showgrid=True, gridcolor=_GRID)
    fig.update_yaxes(title_text="Units sold",
                     title_font=dict(color=_TEXT), tickfont=dict(color=_TEXT),
                     secondary_y=True, showgrid=False)
    return fig


def _chart_full_report(data: dict) -> list[go.Figure]:
    """Return up to four figures for get_full_inventory_report."""
    figs = []
    for key, fn in [
        ("reorder",    _chart_reorder_signals),
        ("profitable", _chart_top_profitable),
        ("slow",       _chart_slow_movers),
        ("discontinue",_chart_discontinue),
    ]:
        section = data.get(key, {})
        if section:
            f = fn(section)
            if f:
                figs.append(f)
    return figs


# ── dispatch table ────────────────────────────────────────────────────────────

_HANDLERS: dict[str, Any] = {
    "get_top_profitable":         _chart_top_profitable,
    "get_reorder_signals":        _chart_reorder_signals,
    "get_slow_movers":            _chart_slow_movers,
    "get_discontinue_candidates": _chart_discontinue,
    "get_top_brands_chart":       _chart_top_brands,
    "get_revenue_trend_chart":    _chart_revenue_trend,
    # aliases that some dispatcher tool names use
    "get_reorder_signals_json":   _chart_reorder_signals,
}


def make_chart(tool_name: str, output: str) -> "go.Figure | list[go.Figure] | None":
    """Parse a tool's JSON output and return a Plotly figure (or list), or None."""
    if not _PLOTLY:
        return None

    if tool_name == "get_full_inventory_report":
        data = _parse(output)
        if not data:
            return None
        figs = _chart_full_report(data)
        return figs if figs else None

    handler = _HANDLERS.get(tool_name)
    if not handler:
        return None

    data = _parse(output)
    if not data:
        return None

    try:
        return handler(data)
    except Exception as exc:
        logging.debug("chart_generator: %s failed: %s", tool_name, exc)
        return None
