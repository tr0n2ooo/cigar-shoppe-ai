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

# Override labels for tools that benefit from a friendlier or more informative status message.
# Any tool not listed here gets a formatted fallback: "lookup_cigar" → "Running lookup cigar…"
_TOOL_LABEL_OVERRIDES: dict[str, str] = {
    "generate_order_recommendation": "Generating order recommendation — this runs 3 parallel branches and takes ~30 s…",
    "get_full_inventory_report":     "Running full inventory analysis (all four analyses)…",
    "lookup_cigar":                  "Looking up cigar research & ratings…",
    "lookup_social_reputation":      "Fetching community reputation & buzz scores…",
    "search_inventory_by_name":      "Checking current stock levels…",
    "query_xlsx":                    "Querying sales data…",
    "get_reorder_signals":           "Analyzing reorder signals…",
    "get_slow_movers":               "Finding slow movers for discounting…",
    "get_discontinue_candidates":    "Identifying discontinue candidates…",
    "get_top_profitable":            "Ranking most profitable items…",
}


def _tool_label(tool_name: str) -> str:
    if tool_name in _TOOL_LABEL_OVERRIDES:
        return _TOOL_LABEL_OVERRIDES[tool_name]
    # Fallback: convert snake_case → "Running look up cigar…"
    readable = tool_name.replace("_", " ")
    return f"Running {readable}…"

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
    status_msg = cl.Message(content="Thinking…")
    await status_msg.send()

    # Track which tools fired so we can show live status updates
    tool_status: list[str] = []

    def on_tool_start(tool_name: str, _inputs: dict) -> None:
        tool_status.append(_tool_label(tool_name))

    try:
        answer = await cl.make_async(run_dispatch)(
            message.content,
            on_tool_start=on_tool_start,
        )

        # Show which tools ran as a subtle prefix if more than one
        if len(tool_status) > 1:
            tools_note = "  \n".join(f"_{t}_" for t in tool_status)
            answer = f"{tools_note}\n\n---\n\n{answer}"

        status_msg.content = answer
    except Exception as exc:
        status_msg.content = f"**Error:** {exc}"

    await status_msg.update()
