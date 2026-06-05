"""
ui.py
-----
Chainlit chatbot UI for the Smoke Shoppe XLSX analyst.

Run:
    chainlit run ui.py
    chainlit run ui.py --port 8080
"""

import chainlit as cl

from agent import DEFAULT_XLSX, run_query


@cl.on_chat_start
async def on_chat_start():
    await cl.Message(
        content=(
            "# Smoke Shoppe Data Analyst\n\n"
            "Ask questions about your sales data in plain English. "
            "I'll look it up and answer with specific numbers.\n\n"
            "**Try asking:**\n"
            "- *What are the top 5 products by total sales?*\n"
            "- *How many transactions were there in 2024?*\n"
            "- *What percentage of sales were paid by credit card?*\n"
            "- *Which employee processed the most transactions?*"
        )
    ).send()


@cl.on_message
async def on_message(message: cl.Message):
    thinking = cl.Message(content="")
    await thinking.send()

    try:
        answer = await cl.make_async(run_query)(message.content, DEFAULT_XLSX)
        thinking.content = answer
    except Exception as exc:
        thinking.content = f"Error: {exc}"

    await thinking.update()
