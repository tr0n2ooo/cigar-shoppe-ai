"""
ui.py
-----
Chainlit chatbot UI for the Smoke Shoppe AI.

All messages are routed through the dispatcher agent, which uses Claude to
select and call the appropriate specialist tool(s) — sales analytics, inventory
analysis, ordering recommendations — then synthesizes a coherent response.

Run:
    chainlit run ui.py
    chainlit run ui.py --port 8080
"""

import asyncio
import os

import chainlit as cl

from dispatcher_agent import run_dispatch


@cl.password_auth_callback
def auth_callback(username: str, password: str) -> cl.User | None:
    """Return a User if credentials match, None to reject."""
    expected_user = os.environ.get("UI_USERNAME", "admin")
    expected_pass = os.environ.get("UI_PASSWORD", "")
    if username == expected_user and password == expected_pass:
        return cl.User(identifier=username)
    return None


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


WELCOME = """\
# Smoke Shoppe AI

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


@cl.on_chat_start
async def on_chat_start():
    await cl.Message(content=WELCOME).send()


@cl.on_message
async def on_message(message: cl.Message):
    """
    Handle a user message with live, per-tool step feedback.

    Architecture
    ============
    run_dispatch() is synchronous and runs in a ThreadPoolExecutor via
    cl.make_async().  Its on_tool_start / on_tool_end callbacks are invoked
    from that worker thread.

    To update the Chainlit UI (which requires the asyncio event loop), the
    callbacks post lightweight events onto an asyncio.Queue via
    call_soon_threadsafe().  A companion async task (ui_updater) drains the
    queue and manages cl.Step objects — which requires the Chainlit context
    and must run on the event loop, not in the worker thread.

    This avoids any asyncio.run_coroutine_threadsafe / blocking-wait pattern
    that would deadlock the event loop.
    """
    loop = asyncio.get_event_loop()
    event_queue: asyncio.Queue = asyncio.Queue()

    # ── event types ──────────────────────────────────────────────────────────
    # ("start", tool_name)
    # ("end",   tool_name, output_snippet)
    # (None,)  — sentinel: stop the updater

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

    # ── thread-safe callbacks (called from worker thread) ────────────────────
    def on_tool_start(tool_name: str, _inputs: dict) -> None:
        loop.call_soon_threadsafe(event_queue.put_nowait, ("start", tool_name))

    def on_tool_end(tool_name: str, output: str) -> None:
        # Pass a short snippet so the completed step shows something useful
        snippet = (output[:300] + "…") if len(output) > 300 else output
        loop.call_soon_threadsafe(event_queue.put_nowait, ("end", tool_name, snippet))

    # ── kick off both tasks ───────────────────────────────────────────────────
    updater_task = asyncio.create_task(ui_updater())

    try:
        answer = await cl.make_async(run_dispatch)(
            message.content,
            on_tool_start=on_tool_start,
            on_tool_end=on_tool_end,
        )
    except Exception as exc:
        answer = f"**Error:** {exc}"
    finally:
        # Signal the updater to stop and wait for it to finish
        loop.call_soon_threadsafe(event_queue.put_nowait, (None,))
        await updater_task

    await cl.Message(content=answer).send()
