"""Minimal web wrapper for the Bookly agent.

Same agent loop and same MCP tools as agent.py -- this just swaps the
PowerShell CLI for a browser chat box. The customer logs in via the
authenticate_customer tool; no customer data is shown before that succeeds.

Run:  python web.py
Then: open http://127.0.0.1:5000 in your browser.
"""

import asyncio
import json
import os
import sys

from dotenv import load_dotenv
from flask import Flask, jsonify, request, send_from_directory
from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client
from openai import OpenAI

load_dotenv()

MODEL = "gpt-4o-mini"

SYSTEM_PROMPT = """You are Paige, Bookly's customer support agent, chatting with a customer on the website.


The customer is NOT logged in yet.
- Public questions (store hours, cafe, location, inventory, policies, general product info) need NO authentication. Answer them directly with the right tool.
- The moment the customer asks for anything tied to their account -- order status, returns, order history, or placing an order -- ask for their email address and call authenticate_customer FIRST.
- If authenticate_customer returns authenticated: false, say you couldn't find that email and ask them to try again.
- Never reveal orders, personal details, or customer data before authentication succeeds.

How you work:
- You never invent customer, order, policy, store, or inventory facts. If you need a fact, call a tool.
- If a request is ambiguous, ask a clarifying question first.
- Answer only what was asked. Don't volunteer extra facts from a tool result unless they're directly useful.
- For store questions (hours, cafe, location), call get_store_info.
- For inventory questions, call search_catalog. If something is out of stock in-store but available online, say so and offer to place an online order.
- To place an order: confirm the items and total with the customer first, then call create_order with their customer_id and the SKUs.
- For returns: check_return_eligibility first, then initiate_return with the customer's reason. If the tool escalates, tell the customer a human specialist will follow up within 1 business day. Never promise a refund the tool didn't approve.
- After a meaningful interaction (order help, return, purchase interest), save a brief note with save_interaction_note so future conversations have context. You need the customer_id from authenticate_customer or get_customer for this.
- Be warm, concise, and plain-spoken. No jargon about tools or databases.
"""

app = Flask(__name__)
client = OpenAI()
messages = [{"role": "system", "content": SYSTEM_PROMPT}]
current_user = None


def mcp_tools_to_openai(tools):
    return [
        {
            "type": "function",
            "function": {
                "name": t.name,
                "description": t.description,
                "parameters": t.inputSchema,
            },
        }
        for t in tools
    ]


async def agent_turn(user_text):
    """One full agent turn (possibly several tool rounds). Returns (reply, tools_used)."""
    global current_user
    server_script = os.path.join(os.path.dirname(os.path.abspath(__file__)), "server.py")
    params = StdioServerParameters(command=sys.executable, args=[server_script])
    used_tools = []
    async with stdio_client(params) as (read, write):
        async with ClientSession(read, write) as session:
            await session.initialize()
            openai_tools = mcp_tools_to_openai((await session.list_tools()).tools)
            messages.append({"role": "user", "content": user_text})
            while True:
                response = client.chat.completions.create(
                    model=MODEL, messages=messages, tools=openai_tools
                )
                msg = response.choices[0].message
                messages.append(msg)
                if not msg.tool_calls:
                    return msg.content, used_tools
                for call in msg.tool_calls:
                    args = json.loads(call.function.arguments or "{}")
                    used_tools.append(call.function.name)
                    result = await session.call_tool(call.function.name, args)
                    text = "\n".join(
                        block.text for block in result.content if hasattr(block, "text")
                    )
                    if call.function.name == "authenticate_customer":
                        try:
                            data = json.loads(text)
                            if data.get("authenticated"):
                                current_user = {
                                    "name": data["name"],
                                    "email": data["email"],
                                    "customer_id": data["customer_id"],
                                }
                        except Exception:
                            pass
                    messages.append(
                        {"role": "tool", "tool_call_id": call.id, "content": text}
                    )


@app.route("/")
def index():
    return send_from_directory(".", "index.html")


@app.route("/api/chat", methods=["POST"])
def chat():
    user_text = request.json.get("message", "").strip()
    if not user_text:
        return jsonify({"error": "empty message"}), 400
    reply, tools = asyncio.run(agent_turn(user_text))
    return jsonify({
        "reply": reply,
        "tools": tools,
        "user": current_user["name"] if current_user else None,
    })


@app.route("/api/reset", methods=["POST"])
def reset():
    global messages, current_user
    messages = [{"role": "system", "content": SYSTEM_PROMPT}]
    current_user = None
    return jsonify({"ok": True})


if __name__ == "__main__":
    app.run(port=5000)
