"""Bookly support agent: a hand-written loop, no agent framework.

The agent (this file) does the thinking via the OpenAI API and does the
doing via the MCP server (server.py), which it spawns as a subprocess.
The agent never touches the database directly.
"""

import asyncio
import json
import os
import sys

from dotenv import load_dotenv
from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client
from openai import OpenAI

load_dotenv()

MODEL = "gpt-4o-mini"

SYSTEM_PROMPT = """You are Bookly's customer support agent, chatting with a customer on the website.

How you work:
- You never invent customer, order, or policy facts. If you need a fact, call a tool.
- If a request is ambiguous, ask a clarifying question first. Example: a customer has two orders and asks "where's my order" -- ask which one.
- To look up a customer you need their email. If you don't have it, ask for it.
- For returns: check_return_eligibility first, then initiate_return with the customer's reason. If the tool escalates, tell the customer a human specialist will follow up within 1 business day. Never promise a refund the tool didn't approve.
- After a meaningful interaction (order help, return, purchase interest), save a brief note with save_interaction_note so future conversations have context. You need the customer_id from get_customer for this.
- Be warm, concise, and plain-spoken. No jargon about tools or databases.
"""


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


async def run_agent():
    if not os.environ.get("OPENAI_API_KEY"):
        raise RuntimeError("Missing OPENAI_API_KEY - add it to your .env file.")

    client = OpenAI()
    server_script = os.path.join(os.path.dirname(os.path.abspath(__file__)), "server.py")
    server_params = StdioServerParameters(command=sys.executable, args=[server_script])

    async with stdio_client(server_params) as (read, write):
        async with ClientSession(read, write) as session:
            await session.initialize()
            listed = await session.list_tools()
            openai_tools = mcp_tools_to_openai(listed.tools)
            print("Connected. Tools available: " + ", ".join(t.name for t in listed.tools))

            messages = [{"role": "system", "content": SYSTEM_PROMPT}]
            print("Bookly support agent ready. Type 'quit' to exit.\n")

            while True:
                user_text = input("You: ").strip()
                if user_text.lower() in ("quit", "exit"):
                    break
                if not user_text:
                    continue
                messages.append({"role": "user", "content": user_text})

                # Keep calling the model until it answers without needing tools.
                while True:
                    response = client.chat.completions.create(
                        model=MODEL, messages=messages, tools=openai_tools
                    )
                    msg = response.choices[0].message
                    messages.append(msg)

                    if not msg.tool_calls:
                        print(f"\nAgent: {msg.content}\n")
                        break

                    for call in msg.tool_calls:
                        args = json.loads(call.function.arguments or "{}")
                        print(f"  [tool: {call.function.name}({', '.join(f'{k}={v}' for k, v in args.items())})]")
                        result = await session.call_tool(call.function.name, args)
                        text = "\n".join(
                            block.text for block in result.content if hasattr(block, "text")
                        )
                        messages.append(
                            {
                                "role": "tool",
                                "tool_call_id": call.id,
                                "content": text,
                            }
                        )


if __name__ == "__main__":
    asyncio.run(run_agent())
