"""
dispatcher_agent.py
-------------------
Natural-language dispatcher for the Smoke Shoppe AI.

Automatically discovers every *_server.py module in this directory, calls its
build_server() function, and registers all exposed MCP tools with Claude.
The dispatcher itself contains NO hardcoded tool definitions — adding a new
*_server.py file automatically makes its tools available in the chat UI.

The dispatcher's only responsibilities are:
  1. Tool discovery (auto, at startup)
  2. System prompt (routing guidance for multi-tool questions)
  3. Agentic loop (send message → Claude picks tools → run tools → repeat)
"""

from __future__ import annotations

import asyncio
import importlib
import logging
import os
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path
from typing import Any

import anthropic

from cigar_researcher import _create_with_backoff, set_rate_limit_cb

# ── tool discovery ────────────────────────────────────────────────────────────

_HERE = Path(__file__).parent

# Modules to skip during auto-discovery (they are not agent servers)
_SKIP_MODULES = {"dispatcher_agent"}

# House brands — made in-house by Smoke Shoppe, no external research exists for them.
# The dispatcher skips lookup_cigar and lookup_social_reputation for these.
# Only Brand = "Smoke Shoppe" (the SS Conn. line) qualifies.
# "Signature House Blend" (Brand = "Signature") is NOT a house blend — treat it
# like any other third-party cigar and look it up normally.
HOUSE_BRANDS = {"smoke shoppe"}
HOUSE_PARENT_COMPANIES: set[str] = set()  # parent-company check not needed; brand is specific enough

# Cached discovery result — loaded once per process
_tools_cache: list[dict] | None = None
_handlers_cache: dict[str, Any] | None = None


def _discover_tools() -> tuple[list[dict], dict[str, Any]]:
    """
    Scan *_server.py files, call build_server() on each, and extract tool
    definitions and callables from FastMCP's _tool_manager.

    Returns:
        tools    — list of Anthropic tool defs (name, description, input_schema)
        handlers — dict mapping tool_name -> FastMCP Tool object
    """
    tools: list[dict] = []
    handlers: dict[str, Any] = {}

    for server_path in sorted(_HERE.glob("*_server.py")):
        module_name = server_path.stem
        if module_name in _SKIP_MODULES:
            continue

        try:
            mod = importlib.import_module(module_name)
        except Exception as exc:
            logging.warning("dispatcher: could not import %s: %s", module_name, exc)
            continue

        if not hasattr(mod, "build_server"):
            continue

        try:
            server = mod.build_server()
        except Exception as exc:
            logging.warning("dispatcher: build_server() failed for %s: %s", module_name, exc)
            continue

        for tool_name, tool_obj in server._tool_manager._tools.items():
            if tool_name in handlers:
                logging.debug("dispatcher: duplicate tool %r from %s — skipping", tool_name, module_name)
                continue
            tools.append({
                "name": tool_name,
                "description": tool_obj.description or "",
                "input_schema": tool_obj.parameters,
            })
            handlers[tool_name] = tool_obj
            logging.debug("dispatcher: registered %r from %s", tool_name, module_name)

    logging.info("dispatcher: loaded %d tools from %d server module(s)", len(tools), len(set(h.__module__ if hasattr(h, '__module__') else '' for h in handlers.values())))
    return tools, handlers


def get_tools() -> list[dict]:
    """Return the Anthropic tool definitions, loading from servers if needed."""
    global _tools_cache, _handlers_cache
    if _tools_cache is None:
        _tools_cache, _handlers_cache = _discover_tools()
    return _tools_cache


def _get_handlers() -> dict[str, Any]:
    global _tools_cache, _handlers_cache
    if _handlers_cache is None:
        _tools_cache, _handlers_cache = _discover_tools()
    return _handlers_cache


def reload_tools() -> None:
    """Force a re-scan of all server modules (useful after adding a new server)."""
    global _tools_cache, _handlers_cache
    _tools_cache = None
    _handlers_cache = None


# ── system prompts ────────────────────────────────────────────────────────────

# Tools available in customer mode — research + social reputation + basic stock check only.
# All sales analytics, inventory financials, reorder signals, and ordering tools are excluded.
_CUSTOMER_TOOLS = {"lookup_cigar", "lookup_social_reputation", "search_inventory_by_name"}

CUSTOMER_SYSTEM_PROMPT = """You are a friendly cigar consultant at Smoke Shoppe, a premium cigar shop. Your goal is to help customers find cigars they'll love based on their taste preferences, experience level, and budget.

You can look up detailed cigar profiles (blend, wrapper, strength, tasting notes), check what we currently have in stock with pricing, and share what critics and enthusiasts say about specific cigars.

## Helping customers find cigars

**Preference-based questions** ("I like mild cigars", "I want something creamy", "I'm new to cigars"):
- If needed, ask a brief follow-up to clarify strength, flavor direction, and budget.
- Use lookup_cigar to identify cigars matching their profile.
- Use search_inventory_by_name to confirm we carry them and get current pricing.
- Recommend 2–4 specific cigars with tasting notes, strength level, and price.

**Specific cigar questions** ("Tell me about the Padron 1964", "What's in the Oliva Serie V?"):
- Call lookup_cigar for blend and tasting notes, lookup_social_reputation for ratings and sentiment, then search_inventory_by_name to show what sizes we carry and at what price.
- Structure the response: Cigar Profile → Ratings & Reviews → What We Carry.

**Reputation/review questions** ("Is X highly rated?", "What do people think of Y?"):
- Call lookup_social_reputation for critic scores and community sentiment.

**House blend** (Brand = "Smoke Shoppe" — the SS Conn. line):
- Use only search_inventory_by_name; skip lookup_cigar and lookup_social_reputation.
- Note that it's the shop's own house blend.

## Tone and presentation
- Warm and approachable — customers range from first-timers to seasoned aficionados.
- Always mention strength level (mild / medium / full) and 2–3 tasting notes when recommending.
- Include price when available from inventory.
- If we don't carry something, suggest a similar cigar we do stock.
- Never mention margins, costs, reorder levels, sales data, or any internal business information.
- Refer to the shop as "Smoke Shoppe"."""

SYSTEM_PROMPT = """You are a knowledgeable business analyst and cigar product expert for a premium cigar shop called Smoke Shoppe.

You have access to specialist tools that cover the full range of shop operations — sales analytics, inventory management, cigar research, social reputation, and ordering. Use them to answer the owner's questions with specific, actionable insights.

## Routing guidelines

**Cigar profile questions** ("Tell me about X", "What can you tell me about X?", "What's in X?"):
First check if the cigar is a Smoke Shoppe house blend (Brand = "Smoke Shoppe" in inventory —
the "SS Conn." line). Use search_inventory_by_name to check.

NOTE: "Signature House Blend" (Brand = "Signature") is NOT the house blend — treat it like
any other third-party cigar and look it up via lookup_cigar and lookup_social_reputation.

- If it IS the house blend (Brand = "Smoke Shoppe"): call only search_inventory_by_name and
  query_xlsx for sales history. Do NOT call lookup_cigar or lookup_social_reputation — no
  external research exists for in-house blends. Note in your response that this is the
  Smoke Shoppe house blend.
- If it is NOT a house brand: call ALL three in sequence:
  1. lookup_cigar (blend, strength, flavor notes, MSRP, critical rating)
  2. lookup_social_reputation (pro scores, community sentiment, overall score)
  3. search_inventory_by_name (which sizes we carry, on-hand qty, retail price)
  Structure the response as: Blend Details → Ratings & Reviews → Community Reputation → Current Stock.

**Inventory health questions** ("What's low?", "What should I discount?", "What's not selling?"):
Call the most specific inventory analysis tool(s). For broad questions call the full report tool.
Always mention urgency tiers for reorder signals.

**Ordering questions** ("What should I order?", "What new cigars should I bring in?"):
Call the order recommendation tool. Summarize the strategy and lead with the highest-conviction picks.

**Sales analytics** ("Top brands by revenue", "Which employee sold most?", "How did Q1 go?"):
Call query_xlsx with the specific question. Charts are generated automatically alongside
the text response — you do not need to call any chart tools separately.

**Combined questions** ("What's low stock AND what should I discount?"):
Call multiple tools — one per analysis — then synthesize a single coherent response.

## Presentation
- Clean markdown with clear headings and bullet points.
- Lead with the most actionable insight.
- Specific numbers, names, and dollar amounts from the data.
- Show top 10–15 items by default unless the user specifies otherwise.
- Refer to the shop as "Smoke Shoppe" throughout."""

# ── tool execution ────────────────────────────────────────────────────────────

def _run_tool(tool_name: str, inputs: dict) -> str:
    """Call the appropriate FastMCP tool handler and return a string result."""
    handlers = _get_handlers()
    tool_obj = handlers.get(tool_name)
    if tool_obj is None:
        return f"Error: unknown tool '{tool_name}'. Available: {sorted(handlers)}"

    try:
        if tool_obj.is_async:
            result = asyncio.run(tool_obj.run(inputs))
        else:
            result = tool_obj.fn(**inputs)
        return str(result) if result is not None else ""
    except Exception as exc:
        return f"Tool error in '{tool_name}': {exc}"


# ── agentic dispatch loop ─────────────────────────────────────────────────────

def run_dispatch(
    question: str,
    *,
    history: list[dict] | None = None,
    on_tool_start: Any = None,
    on_tool_end: Any = None,
    on_rate_limit: Any = None,
    mode: str = "manager",
) -> str:
    """
    Dispatch a natural-language question to the appropriate specialist tools
    and synthesize a final response.

    When Claude requests multiple tool calls in the same response turn they are
    executed concurrently via a ThreadPoolExecutor — this cuts wall-clock time
    for questions that trigger several independent lookups (e.g. cigar research
    + social reputation + stock check all fire at once instead of sequentially).

    Args:
        question:      The user's natural-language question.
        history:       Prior conversation turns as a list of
                       {"role": "user"|"assistant", "content": str} dicts.
                       Only plain text turns should be included — not raw tool
                       outputs — to keep the context window compact.
        on_tool_start: Optional callable(tool_name: str, inputs: dict) called
                       immediately before each tool runs (from a worker thread).
        on_tool_end:   Optional callable(tool_name: str, output: str) called
                       immediately after each tool returns (from a worker thread).

    Returns:
        A markdown-formatted answer string.
    """
    client = anthropic.Anthropic(api_key=os.environ.get("ANTHROPIC_API_KEY"))
    if mode == "customer":
        tools = [t for t in get_tools() if t["name"] in _CUSTOMER_TOOLS]
        system = CUSTOMER_SYSTEM_PROMPT
    else:
        tools = get_tools()
        system = SYSTEM_PROMPT
    messages: list[dict] = list(history or []) + [{"role": "user", "content": question}]

    # Register the rate-limit callback on the dispatch thread (covers the
    # dispatcher's own Claude calls) and on every tool-worker thread below.
    if on_rate_limit:
        set_rate_limit_cb(on_rate_limit)

    # Tools that always benefit from sales charts regardless of query content.
    # Charts are generated automatically when any of these tools fire, without
    # relying on the dispatcher model to decide to call chart tools separately.
    _AUTO_CHART_TOOLS: dict[str, tuple[str, ...]] = {
        "query_xlsx": ("get_top_brands_chart", "get_revenue_trend_chart"),
    }

    def _execute_block(block) -> tuple[str, str]:
        """Run a single tool_use block and return (tool_use_id, output)."""
        # Each worker thread needs its own registration since threading.local
        # is per-thread.
        if on_rate_limit:
            set_rate_limit_cb(on_rate_limit)
        if on_tool_start:
            on_tool_start(block.name, block.input)
        output = _run_tool(block.name, block.input)
        if on_tool_end:
            on_tool_end(block.name, output)
            # Auto-fire companion chart tools when the UI is active.
            # Runs in the same worker thread — chart queries are cheap DuckDB calls.
            for chart_tool in _AUTO_CHART_TOOLS.get(block.name, ()):
                chart_output = _run_tool(chart_tool, {})
                on_tool_end(chart_tool, chart_output)
        return block.id, output

    while True:
        response = _create_with_backoff(
            client,
            model="claude-haiku-4-5-20251001",
            max_tokens=4096,
            system=[{"type": "text", "text": system, "cache_control": {"type": "ephemeral"}}],
            tools=tools,
            messages=messages,
        )

        text_blocks = [b.text for b in response.content if b.type == "text"]

        if response.stop_reason == "end_turn":
            return "\n\n".join(text_blocks) if text_blocks else "(No response)"

        if response.stop_reason != "tool_use":
            return "\n\n".join(text_blocks) if text_blocks else f"Unexpected stop: {response.stop_reason}"

        tool_blocks = [b for b in response.content if b.type == "tool_use"]

        # Run all tool calls for this response turn in parallel when there are
        # multiple — serial fallback for a single block avoids thread overhead.
        if len(tool_blocks) == 1:
            bid, output = _execute_block(tool_blocks[0])
            results_map = {bid: output}
        else:
            max_workers = min(len(tool_blocks), 5)
            with ThreadPoolExecutor(max_workers=max_workers) as pool:
                futures = {pool.submit(_execute_block, blk): blk for blk in tool_blocks}
                results_map: dict[str, str] = {}
                for fut in as_completed(futures):
                    bid, output = fut.result()
                    results_map[bid] = output

        # Reassemble in the original order Claude requested them
        tool_results = [
            {
                "type": "tool_result",
                "tool_use_id": blk.id,
                "content": results_map[blk.id],
            }
            for blk in tool_blocks
        ]

        messages.append({"role": "assistant", "content": response.content})
        messages.append({"role": "user", "content": tool_results})
