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
from pathlib import Path
from typing import Any

import anthropic

# ── tool discovery ────────────────────────────────────────────────────────────

_HERE = Path(__file__).parent

# Modules to skip during auto-discovery (they are not agent servers)
_SKIP_MODULES = {"dispatcher_agent"}

# House brands — made in-house by Smoke Shoppe, no external research exists for them.
# The dispatcher skips lookup_cigar and lookup_social_reputation for these.
HOUSE_PARENT_COMPANIES = {"smoke shoppe"}
HOUSE_BRANDS = {"smoke shoppe", "signature"}

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


# ── system prompt ─────────────────────────────────────────────────────────────

SYSTEM_PROMPT = """You are a knowledgeable business analyst and cigar product expert for a premium cigar shop called Smoke Shoppe.

You have access to specialist tools that cover the full range of shop operations — sales analytics, inventory management, cigar research, social reputation, and ordering. Use them to answer the owner's questions with specific, actionable insights.

## Routing guidelines

**Cigar profile questions** ("Tell me about X", "What can you tell me about X?", "What's in X?"):
First check if the cigar is a Smoke Shoppe house brand (brands: "Smoke Shoppe", "Signature";
parent company: "Smoke Shoppe"). Use search_inventory_by_name to check.

- If it IS a house brand: call only search_inventory_by_name and query_xlsx for sales history.
  Do NOT call lookup_cigar or lookup_social_reputation — no external research exists for
  house-made cigars. Note in your response that this is a Smoke Shoppe house blend.
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
Call the sales query tool with the specific question.

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

def run_dispatch(question: str, *, on_tool_start: Any = None) -> str:
    """
    Dispatch a natural-language question to the appropriate specialist tools
    and synthesize a final response.

    Args:
        question:      The user's natural-language question.
        on_tool_start: Optional callable(tool_name, inputs) called before each
                       tool runs. Useful for showing status in the UI.

    Returns:
        A markdown-formatted answer string.
    """
    client = anthropic.Anthropic(api_key=os.environ.get("ANTHROPIC_API_KEY"))
    tools = get_tools()
    messages: list[dict] = [{"role": "user", "content": question}]

    while True:
        response = client.messages.create(
            model="claude-sonnet-4-6",
            max_tokens=4096,
            system=SYSTEM_PROMPT,
            tools=tools,
            messages=messages,
        )

        text_blocks = [b.text for b in response.content if b.type == "text"]

        if response.stop_reason == "end_turn":
            return "\n\n".join(text_blocks) if text_blocks else "(No response)"

        if response.stop_reason != "tool_use":
            return "\n\n".join(text_blocks) if text_blocks else f"Unexpected stop: {response.stop_reason}"

        tool_results = []
        for block in response.content:
            if block.type != "tool_use":
                continue

            if on_tool_start:
                on_tool_start(block.name, block.input)

            output = _run_tool(block.name, block.input)
            tool_results.append({
                "type": "tool_result",
                "tool_use_id": block.id,
                "content": output,
            })

        messages.append({"role": "assistant", "content": response.content})
        messages.append({"role": "user", "content": tool_results})
