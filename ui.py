"""
ui.py
-----
Chainlit chatbot UI for the Smoke Shoppe AI.

All messages are routed through the dispatcher agent, which uses Claude to
select and call the appropriate specialist tool(s) — sales analytics, inventory
analysis, ordering recommendations — then synthesizes a coherent response.

Run via main.py (recommended — sets host/port automatically):
    python main.py ui                        # binds to 0.0.0.0:8000 (all interfaces)
    python main.py ui --port 8080            # custom port, still all interfaces

Run directly with Chainlit (must pass --host for LAN/public access):
    chainlit run ui.py --host 0.0.0.0 --port 8000   # all interfaces
    chainlit run ui.py                               # localhost only (127.0.0.1)
"""

import asyncio
import json
import logging
import os

import chainlit as cl

# LOG_LEVEL=INFO  → dispatcher routing, ToT branches, RAG/MMR flow, agentic memory
# LOG_LEVEL=DEBUG → all of the above + per-call token/cache stats
_log_level = os.environ.get("LOG_LEVEL", "").upper()
if _log_level in ("DEBUG", "INFO", "WARNING", "ERROR"):
    logging.getLogger().setLevel(getattr(logging, _log_level))
    logging.basicConfig(
        level=getattr(logging, _log_level),
        format="%(asctime)s %(levelname)s %(message)s",
        datefmt="%H:%M:%S",
        force=True,
    )

from dispatcher_agent import run_dispatch
from chart_generator import make_chart


# ── order export action callbacks ─────────────────────────────────────────────

@cl.action_callback("export_xlsx")
async def on_export_xlsx(action: cl.Action):
    from order_export import to_xlsx
    raw = cl.user_session.get("last_order_result")
    if not raw:
        await cl.Message(content="No order result in this session — run an order recommendation first.").send()
        return
    result = json.loads(raw)
    path = to_xlsx(result)
    await cl.Message(
        content="Your order recommendation as an Excel spreadsheet:",
        elements=[cl.File(name=path.name, path=str(path), display="inline")],
    ).send()


@cl.action_callback("export_pdf")
async def on_export_pdf(action: cl.Action):
    from order_export import to_pdf
    raw = cl.user_session.get("last_order_result")
    if not raw:
        await cl.Message(content="No order result in this session — run an order recommendation first.").send()
        return
    result = json.loads(raw)
    path = to_pdf(result)
    await cl.Message(
        content="Your order recommendation as a PDF purchase order:",
        elements=[cl.File(name=path.name, path=str(path), display="inline")],
    ).send()


# Override labels for tools — cigar-shop themed, shown in the animated step UI.
# Any tool not listed here gets a clean formatted fallback.
_TOOL_LABEL_OVERRIDES: dict[str, str] = {
    "generate_order_recommendation": "🛒 Rolling up your order list — 3 parallel cuts, may take ~30 s",
    "get_full_inventory_report":     "📋 Walking the full humidor — four analyses at once",
    "lookup_cigar":                  "🔬 Pulling blend notes, vitola specs & critical ratings",
    "lookup_social_reputation":      "💬 Checking the lounge buzz — scores, sentiment & community picks",
    "search_inventory_by_name":      "🏪 Peeking in the humidor for current stock",
    "query_xlsx":                    "📊 Cracking the sales ledger",
    "get_reorder_signals":           "📦 Sniffing out what's running dry",
    "get_slow_movers":               "🐢 Hunting down the slow burners — discount candidates",
    "get_discontinue_candidates":    "💀 Flagging the dead sticks — discontinue candidates",
    "get_top_profitable":            "💰 Tallying the money-makers",
}


def _tool_label(tool_name: str) -> str:
    if tool_name in _TOOL_LABEL_OVERRIDES:
        return _TOOL_LABEL_OVERRIDES[tool_name]
    # Fallback: convert snake_case → "Lookup cigar"
    return "🚬 " + tool_name.replace("_", " ").capitalize()


CUSTOMER_WELCOME = """\
# Welcome to Smoke Shoppe

Looking for your next great smoke? Tell me what you enjoy and I'll find the perfect cigar from what we have in stock.

**Find something you'll love**
- *I like mild, creamy cigars — what would you recommend?*
- *What's a good full-bodied cigar under $15?*
- *I'm new to cigars — where should I start?*

**Learn about specific cigars**
- *Tell me about the Padron 1964 Anniversary series*
- *What's the blend on the Oliva Serie V?*
- *What do people think of the AJ Fernandez Enclave?*

**See what we carry**
- *Do you have any Davidoff in stock?*
- *What sizes do you carry of the Arturo Fuente Hemingway?*
"""

MANAGER_WELCOME = """\
# Smoke Shoppe — Store Manager

Ask anything about your shop in plain English. I'll route your question to the right analysis and respond with specific, actionable insights.

**Cigar research & reputation**
- *What can you tell me about the AJ Fernandez Goat?*
- *What's the blend and rating on the Padron 1964 Anniversary?*
- *What do customers think of the Oliva Serie V?*

**Sales & revenue**
- *What are the top 10 brands by revenue this year?*
- *Which employee processed the most transactions?*

**Inventory health**
- *What's running low that I need to reorder?*
- *What should I discount to clear shelf space?*
- *What dead stock should I discontinue?*
- *What are my most profitable items right now?*

**Ordering**
- *What should I order for next month?*
- *What new cigars should I bring in under $20/stick?*

**Combined**
- *Show me what's low stock AND what I should discount.*
"""


_MAX_HISTORY_TURNS = 20  # max user+assistant pairs kept in context (40 messages)


@cl.on_chat_start
async def on_chat_start():
    cl.user_session.set("history", [])
    cl.user_session.set("is_manager", False)
    await cl.Message(content=CUSTOMER_WELCOME).send()


async def _handle_manager_login() -> None:
    """Prompt for credentials and upgrade the session to manager mode if valid."""
    username_msg = await cl.AskUserMessage(
        content="**Store Manager Login**\n\nUsername:",
        timeout=60,
    ).send()
    if not username_msg:
        await cl.Message(content="Login timed out.").send()
        return

    password_msg = await cl.AskUserMessage(
        content="Password:",
        timeout=60,
    ).send()
    if not password_msg:
        await cl.Message(content="Login timed out.").send()
        return

    expected_user = os.environ.get("UI_USERNAME", "admin")
    expected_pass = os.environ.get("UI_PASSWORD", "")

    if username_msg["output"] == expected_user and password_msg["output"] == expected_pass:
        cl.user_session.set("is_manager", True)
        cl.user_session.set("history", [])
        await cl.Message(content=MANAGER_WELCOME).send()
    else:
        await cl.Message(content="Invalid credentials. Type `/manager` to try again.").send()


@cl.on_message
async def on_message(message: cl.Message):
    """Handle a user message — routes to customer or manager dispatcher based on session role."""
    content = message.content.strip()

    # ── /manager: upgrade to store manager mode ───────────────────────────────
    if content.lower() == "/manager":
        if cl.user_session.get("is_manager", False):
            await cl.Message(
                content="You're already in Store Manager mode. Type `/customer` to return to the customer view."
            ).send()
        else:
            await _handle_manager_login()
        return

    # ── /customer: return to customer view ────────────────────────────────────
    if content.lower() == "/customer":
        cl.user_session.set("is_manager", False)
        cl.user_session.set("history", [])
        await cl.Message(content=CUSTOMER_WELCOME).send()
        return

    loop = asyncio.get_event_loop()
    event_queue: asyncio.Queue = asyncio.Queue()

    # ── event types ──────────────────────────────────────────────────────────
    # ("start",        tool_name)
    # ("end",          tool_name, output_snippet)
    # ("chart",        figure_or_list)
    # ("order_result", raw_json_str)
    # ("rate_limit",   wait_secs, attempt)
    # (None,)  — sentinel: stop the updater

    # Figures and order result collected during dispatch — attached to the
    # final answer message so everything appears in-context together.
    pending_figures: list = []
    pending_order_json: list[str] = []  # at most one element

    # ── async UI updater ─────────────────────────────────────────────────────
    async def ui_updater() -> None:
        """Drain event_queue and animate cl.Step objects."""
        active: dict[str, cl.Step] = {}
        while True:
            event = await event_queue.get()
            if event[0] is None:
                # Close any steps that never received an end event
                for step in active.values():
                    step.output = "(completed)"
                    await step.update()
                break

            if event[0] == "start":
                _, tool_name = event
                step = cl.Step(name=_tool_label(tool_name), type="tool")
                await step.send()
                active[tool_name] = step

            elif event[0] == "end":
                _, tool_name, snippet = event
                step = active.pop(tool_name, None)
                if step:
                    step.output = snippet
                    await step.update()

            elif event[0] == "chart":
                _, fig_or_list = event
                figs = fig_or_list if isinstance(fig_or_list, list) else [fig_or_list]
                pending_figures.extend(figs)

            elif event[0] == "order_result":
                _, raw = event
                pending_order_json[:] = [raw]  # keep only the most recent

            elif event[0] == "rate_limit":
                _, wait_secs, attempt = event
                step = cl.Step(
                    name=f"⏳ API rate limit — retrying in {wait_secs}s (attempt {attempt}/3)",
                    type="tool",
                )
                step.output = (
                    "Anthropic's API is temporarily rate-limiting requests. "
                    f"Waiting {wait_secs} seconds before retry {attempt} of 3 — "
                    "the response will arrive, just a bit later than usual. ☕"
                )
                await step.send()
                await step.update()

    # ── thread-safe callbacks (called from worker thread) ────────────────────
    def on_tool_start(tool_name: str, _inputs: dict) -> None:
        loop.call_soon_threadsafe(event_queue.put_nowait, ("start", tool_name))

    def on_tool_end(tool_name: str, output: str) -> None:
        snippet = (output[:300] + "…") if len(output) > 300 else output
        loop.call_soon_threadsafe(event_queue.put_nowait, ("end", tool_name, snippet))
        # Generate chart from structured tool output when available
        fig = make_chart(tool_name, output)
        if fig is not None:
            loop.call_soon_threadsafe(event_queue.put_nowait, ("chart", fig))
        # Stash order result so export buttons can be attached to the answer
        if tool_name == "generate_order_recommendation":
            loop.call_soon_threadsafe(event_queue.put_nowait, ("order_result", output))

    def on_rate_limit(wait_secs: int, attempt: int) -> None:
        loop.call_soon_threadsafe(event_queue.put_nowait, ("rate_limit", wait_secs, attempt))

    # ── kick off both tasks ───────────────────────────────────────────────────
    updater_task = asyncio.create_task(ui_updater())

    history: list[dict] = cl.user_session.get("history") or []
    mode = "manager" if cl.user_session.get("is_manager", False) else "customer"

    try:
        answer = await cl.make_async(run_dispatch)(
            message.content,
            history=history,
            on_tool_start=on_tool_start,
            on_tool_end=on_tool_end,
            on_rate_limit=on_rate_limit,
            mode=mode,
        )
    except Exception as exc:
        answer = f"**Error:** {exc}"
    finally:
        # Signal the updater to stop and wait for it to finish
        loop.call_soon_threadsafe(event_queue.put_nowait, (None,))
        await updater_task

    elements = [
        cl.Plotly(figure=fig, display="inline", size="large")
        for fig in pending_figures
    ]

    actions = []
    if pending_order_json:
        cl.user_session.set("last_order_result", pending_order_json[0])
        actions = [
            cl.Action(
                name="export_xlsx",
                label="📊 Export XLSX",
                payload={},
                tooltip="Download order recommendation as an Excel spreadsheet",
            ),
            cl.Action(
                name="export_pdf",
                label="📄 Export PDF",
                payload={},
                tooltip="Download order recommendation as a PDF purchase order",
            ),
        ]

    await cl.Message(content=answer, elements=elements, actions=actions).send()

    # Append this turn to history (plain text only — no raw tool outputs)
    history.append({"role": "user",      "content": message.content})
    history.append({"role": "assistant", "content": answer})
    # Trim to the most recent N turns to bound context growth
    if len(history) > _MAX_HISTORY_TURNS * 2:
        history = history[-(_MAX_HISTORY_TURNS * 2):]
    cl.user_session.set("history", history)
