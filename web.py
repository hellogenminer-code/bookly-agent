"""Minimal web wrapper for the Bookly agent.

Same agent loop and same MCP tools as agent.py -- this just swaps the
PowerShell CLI for a browser chat box. The customer logs in via the
authenticate_customer tool; no customer data is shown before that succeeds.

Memory, CSAT, and escalation are deterministic: after every reply the app
itself (not the LLM) appends the turn + a satisfaction score to the
customer's history, loads past visits into context on login, and opens a
real escalation ticket when satisfaction drops. Memory writes never break
the conversation -- failures are logged and skipped.

Run:  python web.py
Then: open http://127.0.0.1:5000 in your browser.
"""

import asyncio
import json
import os
import sys
from datetime import date

from dotenv import load_dotenv
from flask import Flask, jsonify, request, send_from_directory
from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client
from openai import OpenAI

try:
    sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "easter-egg"))
    from zork import ZorkGame
except Exception as e:
    ZorkGame = None
    print(f"[zork] engine unavailable: {e}", file=sys.stderr)

load_dotenv()

MODEL = "gpt-4o-mini"
CSAT_ESCALATE_AT = 2  # open a ticket when a turn scores at or below this

SYSTEM_PROMPT = """You are Paige, Bookly's customer support agent, chatting with a customer on the website.


The customer is NOT logged in yet.
- Public questions (store hours, cafe, location, inventory, policies, general product info) need NO authentication. Answer them directly with the right tool.
- If the customer is NOT logged in yet and asks for anything tied to their account -- order status, returns, order history, or placing an order -- ask for their email address and call authenticate_customer FIRST.
- Once the customer is logged in (you'll be told explicitly in the conversation), never ask for their email address again. Use the logged-in customer's account directly -- if they ask about "my order" and there's a recent order in this conversation, use that order ID.
- If authenticate_customer returns authenticated: false, say you couldn't find that email and ask them to try again.
- Never reveal orders, personal details, or customer data before authentication succeeds.

How you work:
- You never invent customer, order, policy, store, or inventory facts. If you need a fact, call a tool.
- If a request is ambiguous, ask a clarifying question first.
- Answer only what was asked. Don't volunteer extra facts from a tool result unless they're directly useful.
- For store questions (hours, cafe, location), call get_store_info.
- For inventory questions, call search_catalog. If something is out of stock in-store but available online, say so and offer to place an online order. If search_catalog returns no matches, try once more with a shorter keyword (e.g. "journal" instead of "leatherbound journals") before telling the customer it's unavailable.
- To place an order: confirm the items and total with the customer first, then call create_order with their customer_id and the SKUs.
- If the customer asks to speak to a human / a real person, call request_human_handoff with their customer_id and their reason. Only when they explicitly ask -- frustration or anger alone does not trigger it; low satisfaction is handled automatically.
- For returns: check_return_eligibility first, then initiate_return with the customer's reason. If the tool escalates, a real ticket is opened -- tell the customer the ticket reference (like ESC-1042) and that a human specialist will follow up within 1 business day. When a return needs specialist review, never tell the customer it is approved or eligible, and never say the order is canceled -- say the request is submitted for review and the order is on hold. Never promise a refund the tool didn't approve.
- Memory (notes, satisfaction scores, past visits) is recorded automatically after every reply. Never mention this machinery, and never call append_interaction_turn or create_escalation_ticket yourself.
- Be warm, concise, and plain-spoken. No jargon about tools or databases.
- Format replies as plain text only -- no markdown, no asterisks, no headings, no bullet characters, no numbered lists. When listing items (like an order), write them as one flowing sentence separated by commas, with the price after each item, e.g. "a tan Leatherbound Journal ($75), an oxblood one ($75), and The Princess Bride paperback ($16.99)".
"""

# Fixed topic taxonomy for volume reporting. Every turn is classified into one
# L1 > L2 topic; turns, CSAT, and escalations aggregate by topic straight
# from the stored data -- no separate analytics pipeline.
TOPIC_TAXONOMY = {
    "Order": ["place_order", "order_status", "return_request", "cancel_request"],
    "Product": ["inventory_inquiry", "product_info"],
    "Store": ["hours_location", "cafe_inquiry"],
    "Policy": ["shipping_policy", "return_policy"],
    "Account": ["login_help"],
    "Experience": ["satisfaction_feedback", "human_request", "greeting", "other"],
}

TURN_ANALYSIS_SYSTEM = (
    "You analyze a bookstore support chat. Reply with ONLY JSON like "
    '{"csat": 4, "csat_reason": "short reason", "topic_l1": "Order", '
    '"topic_l2": "order_status", "summary": "2-4 sentence narrative"}. '
    "Score CSAT and topic_l1/topic_l2 for the FINAL turn only. "
    "CSAT 1-5: 5 delighted or thankful, 4 satisfied, 3 neutral or okay, "
    "2 frustrated, 1 angry. Score ONLY what the customer actually expressed -- "
    "bare compliance ('sure', 'ok', providing an email) is neutral (3), never 4+. "
    "Never claim the customer 'expressed satisfaction' unless their words show it. "
    "Keep csat_reason under 12 words and purely descriptive of what they said or did. "
    f"topic_l1 must be exactly one of: {', '.join(TOPIC_TAXONOMY)}. "
    "topic_l2 must be one of that L1's subtopics: "
    + "; ".join(f"{l1}=[{', '.join(l2s)}]" for l1, l2s in TOPIC_TAXONOMY.items())
    + ". "
    "The summary covers the ENTIRE conversation (every turn given), 2-4 sentences, "
    "third person, past tense, capturing the overall arc -- not a play-by-play and "
    "never only the last exchange. Refer to the customer by first name when known, "
    "otherwise 'the customer'."
)


def _msg_role_text(m):
    """(role, text) from either a dict or an OpenAI message object; never raises.
    Assistant messages that only carry tool calls have no text (None)."""
    try:
        role = m.get("role") if isinstance(m, dict) else getattr(m, "role", None)
        text = m.get("content") if isinstance(m, dict) else getattr(m, "content", None)
        return role, (text or "").strip()
    except Exception:
        return None, ""


def _turn_log(max_pairs=8):
    """(user, assistant) text pairs, oldest first, as grounding for the summary.
    Each user message pairs with the next assistant message that has real text;
    tool-call-only messages (no text) are skipped."""
    norm = [_msg_role_text(m) for m in messages[1:]]  # skip the system prompt
    pairs = []
    i = 0
    while i < len(norm):
        role, text = norm[i]
        if role == "user" and text:
            for j in range(i + 1, len(norm)):
                r2, t2 = norm[j]
                if r2 == "assistant" and t2:
                    pairs.append((text[:300], t2[:300]))
                    break
                if r2 == "user":
                    break
        i += 1
    return pairs[-max_pairs:]


def _state_line(l1, l2, csat):
    """Deterministic current-state suffix for the summary note."""
    topic = f"{l1} › {l2}".replace("_", " ")
    cs = f"{csat}/5" if isinstance(csat, int) else "n/a"
    auth = "Authenticated via email" if current_user else "Guest"
    return f"\nMost recent topic: {topic}. Current CSAT: {cs}. Auth status: {auth}."


def _narrative_only(text):
    """Strip the deterministic state line, leaving the pure narrative."""
    i = (text or "").find("\nMost recent topic:")
    return text[:i].strip() if i != -1 else (text or "").strip()


def analyze_turn(turn_log, customer_name=None):
    """Score CSAT + topic for the final turn, and rewrite the summary of the
    whole conversation arc. turn_log is [(user, assistant), ...], oldest first.
    Returns (csat, csat_reason, topic_l1, topic_l2, narrative); safe fallbacks.
    Retries once: the model occasionally returns an empty response."""
    log_text = "\n".join(
        f"Customer: {u}\nPaige: {a}" for u, a in turn_log) or "(empty)"
    user_content = (
        f"Customer first name: {customer_name or 'unknown'}\n"
        f"Conversation (oldest to newest):\n{log_text}"
    )
    for attempt in range(2):
        raw = ""
        try:
            r = client.chat.completions.create(
                model=MODEL,
                response_format={"type": "json_object"},
                messages=[
                    {"role": "system", "content": TURN_ANALYSIS_SYSTEM},
                    {"role": "user", "content": user_content},
                ],
            )
            raw = (r.choices[0].message.content or "").strip()
            data = json.loads(raw)
            score = max(1, min(5, int(data.get("csat", 3))))
            l1 = data.get("topic_l1", "Experience")
            l2 = data.get("topic_l2", "other")
            if l1 not in TOPIC_TAXONOMY or l2 not in TOPIC_TAXONOMY[l1]:
                l1, l2 = "Experience", "other"
            narrative = str(data.get("summary", "") or "").strip()[:600]
            return score, str(data.get("csat_reason", ""))[:120], l1, l2, narrative
        except Exception as e:
            print(f"[analyze] attempt {attempt + 1} failed: {e} (content={raw[:80]!r})",
                  file=sys.stderr)
    return None, "", "Experience", "other", ""


async def _record_turn(session, auto_tools, user_text, reply_text):
    """Analyze one turn and append it to the customer's history. Never raises.
    Recorded in auto_tools (not shown in the widget) to keep the display clean."""
    global current_summary
    first = current_user["name"].split()[0] if current_user else None
    csat, reason, l1, l2, narrative = analyze_turn(_turn_log(), first)
    narrative = narrative or _narrative_only(current_summary)
    current_summary = narrative + _state_line(l1, l2, csat)
    await session.call_tool("append_interaction_turn", {
        "customer_id": current_user["customer_id"],
        "user_text": user_text[:500],
        "assistant_text": (reply_text or "")[:500],
        "csat_score": csat,
        "csat_reason": reason,
        "topic_l1": l1,
        "topic_l2": l2,
        "summary": current_summary,
    })
    auto_tools.append("append_interaction_turn")
    return csat, reason

app = Flask(__name__)
client = OpenAI()
messages = [{"role": "system", "content": SYSTEM_PROMPT}]
current_user = None
pending_turns = []        # (user, reply, csat, reason, l1, l2) from the anon session; attached at login
session_escalated = False  # one low-CSAT ticket per conversation
current_summary = ""      # rolling conversation narrative, updated every turn


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


def _tool_json(result):
    """Parse an MCP tool result into a dict (None on failure)."""
    try:
        text = "\n".join(block.text for block in result.content if hasattr(block, "text"))
        return json.loads(text)
    except Exception:
        return None



async def _inject_customer_context(session):
    """After login: load past visits into the prompt so Paige remembers."""
    global current_summary
    result = await session.call_tool("get_customer", {"email": current_user["email"]})
    data = _tool_json(result)
    if not data or not data.get("found"):
        return
    details = data.get("details") or {}
    # Pick up an existing in-progress summary (e.g. server restarted mid-day);
    # never overwrite the live rolling summary built from this session's turns.
    if not current_summary:
        for inter in (details.get("interactions") or []):
            if inter.get("date") == date.today().isoformat() and inter.get("summary"):
                current_summary = inter["summary"]
    parts = []
    prefs = details.get("preferences")
    if prefs:
        parts.append("Preferences: " + json.dumps(prefs))
    orders = details.get("orders") or []
    if orders:
        last = orders[-1]
        parts.append(
            f"Orders: {len(orders)} total; most recent {last.get('order_id')} "
            f"({last.get('status')}, ${last.get('total')})"
        )
    for inter in (details.get("interactions") or [])[-3:]:
        summary = inter.get("summary") or f"{len(inter.get('turns') or [])} turns"
        parts.append(f"Past visit {inter.get('date')}: {summary}")
    context = "; ".join(parts) if parts else "No prior visits on record."
    messages.append({
        "role": "system",
        "content": (
            f"LOGIN STATE: the customer is now authenticated as {current_user['name']} "
            f"({current_user['email']}). Do not ask for their email address again in this "
            "conversation; use their account directly. "
            "Customer context from past visits (use naturally if relevant; never recite "
            "it): " + context
        ),
    })


async def _flush_pending_turns(session, auto_tools):
    """Attach the anonymous session's turns to the real customer record,
    tagged so the demo can show they happened before login."""
    global pending_turns
    for (u, r, s, reason, l1, l2) in pending_turns:
        await session.call_tool("append_interaction_turn", {
            "customer_id": current_user["customer_id"],
            "user_text": u[:500],
            "assistant_text": (r or "")[:500],
            "csat_score": s,
            "csat_reason": reason,
            "topic_l1": l1,
            "topic_l2": l2,
            "summary": current_summary,
            "pre_login": True,
        })
        auto_tools.append("append_interaction_turn")
    pending_turns = []


async def agent_turn(user_text):
    """One full agent turn (possibly several tool rounds). Returns
    (reply, tools_used, escalation, auto_tools). Memory/CSAT/escalation happen
    in code, deterministically, after the reply is written. auto_tools are the
    silent machinery (memory writes, auto tickets) -- returned for debugging
    but not shown in the widget."""
    global current_user, pending_turns, session_escalated, current_summary
    server_script = os.path.join(os.path.dirname(os.path.abspath(__file__)), "server.py")
    params = StdioServerParameters(command=sys.executable, args=[server_script])
    used_tools = []
    auto_tools = []
    escalation = None
    authed_this_turn = False
    async with stdio_client(params) as (read, write):
        async with ClientSession(read, write) as session:
            await session.initialize()
            openai_tools = mcp_tools_to_openai((await session.list_tools()).tools)
            messages.append({"role": "user", "content": user_text})
            reply_text = ""
            while True:
                response = client.chat.completions.create(
                    model=MODEL, messages=messages, tools=openai_tools
                )
                msg = response.choices[0].message
                messages.append(msg)
                if not msg.tool_calls:
                    reply_text = msg.content or ""
                    break
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
                                authed_this_turn = True
                        except Exception:
                            pass
                    messages.append(
                        {"role": "tool", "tool_call_id": call.id, "content": text}
                    )
            # --- deterministic memory: runs in code, not at the model's discretion ---
            try:
                if authed_this_turn and current_user:
                    await _inject_customer_context(session)
                    await _flush_pending_turns(session, auto_tools)
                if current_user:
                    score, _reason = await _record_turn(session, auto_tools, user_text, reply_text)
                    if score is not None:
                        # Fold the satisfaction signal into today's open ticket when one
                        # exists (bumping priority on a 1-2); only file a fresh
                        # low-CSAT ticket when nothing is open.
                        existing = _tool_json(await session.call_tool("list_escalation_tickets", {
                            "customer_email": current_user["email"],
                        }))
                        today = date.today().isoformat()
                        open_ticket = next((
                            t for t in (existing or {}).get("tickets", [])
                            if t.get("status") == "open" and t.get("created_at", "")[:10] == today
                        ), None)
                        if open_ticket:
                            if score <= CSAT_ESCALATE_AT and open_ticket.get("priority") != "high":
                                await session.call_tool("update_escalation_ticket", {
                                    "ticket_ref": open_ticket["ticket_ref"],
                                    "priority": "high",
                                    "csat": score,
                                })
                                auto_tools.append("update_escalation_ticket:priority_high")
                            else:
                                await session.call_tool("update_escalation_ticket", {
                                    "ticket_ref": open_ticket["ticket_ref"],
                                    "csat": score,
                                })
                                auto_tools.append("update_escalation_ticket:csat")
                            session_escalated = True
                        elif score <= CSAT_ESCALATE_AT and not session_escalated:
                            ticket = _tool_json(await session.call_tool("create_escalation_ticket", {
                                "customer_id": current_user["customer_id"],
                                "trigger": "low_csat",
                                "summary": f"Customer satisfaction dropped to {score}/5.",
                                "csat": score,
                                "context": {
                                    "csat_score": score,
                                    "last_customer_message": user_text[:300],
                                },
                            }))
                            ref = (ticket or {}).get("ticket_ref")
                            session_escalated = True
                            auto_tools.append("create_escalation_ticket")
                            messages.append({
                                "role": "system",
                                "content": (
                                    f"An escalation ticket ({ref}) was just opened because "
                                    "customer satisfaction is low. In your NEXT reply, briefly "
                                    "tell the customer a human specialist will follow up."
                                ),
                            })
                            escalation = {"ticket_ref": ref, "trigger": "low_csat"}
                else:
                    # Anonymous session: analyze now so the dashboard shows it live.
                    # These turns attach to the customer record at login.
                    csat, reason, l1, l2, narrative = analyze_turn(_turn_log(), None)
                    narrative = narrative or _narrative_only(current_summary)
                    current_summary = narrative + _state_line(l1, l2, csat)
                    pending_turns.append((user_text, reply_text, csat, reason, l1, l2))
            except Exception as e:
                # Memory must never break the conversation.
                print(f"[memory] non-fatal: {e}", file=sys.stderr)
    return reply_text, used_tools, escalation, auto_tools


@app.route("/")
def index():
    return send_from_directory(".", "index.html")


@app.route("/api/chat", methods=["POST"])
def chat():
    user_text = request.json.get("message", "").strip()
    if not user_text:
        return jsonify({"error": "empty message"}), 400
    reply, tools, escalation, auto_tools = asyncio.run(agent_turn(user_text))
    return jsonify({
        "reply": reply,
        "tools": tools,
        "auto_tools": auto_tools,
        "escalation": escalation,
        "user": current_user["name"] if current_user else None,
    })


@app.route("/api/state")
def state():
    """Read-only snapshot for the live dashboard, served through the MCP tool
    layer -- the dashboard never touches the database directly."""
    try:
        return jsonify(asyncio.run(_fetch_state()))
    except Exception as e:
        print(f"[state] {e}", file=sys.stderr)
        return jsonify({"logged_in": False, "error": "unavailable"})


@app.route("/dashboard")
def dashboard():
    return send_from_directory(".", "dashboard.html")


@app.route("/split")
def split():
    return send_from_directory(".", "split.html")


@app.route("/bookstore-bg.png")
def bg_image():
    return send_from_directory(".", "bookstore-bg.png")


@app.route("/zork")
def zork():
    return send_from_directory("easter-egg", "index_zork.html")


@app.route("/zork-house.png")
def zork_house():
    return send_from_directory("easter-egg", "zork-house.png")


@app.route("/zork-hand.png")
def zork_hand():
    return send_from_directory("easter-egg", "zork-hand.png")


@app.route("/book-icon.png")
def book_icon():
    return send_from_directory("easter-egg", "book-icon.png")


@app.route("/zork-readme")
def zork_readme():
    return send_from_directory("easter-egg", "zork-readme.html")


@app.route("/zork-valley.png")
def zork_valley():
    return send_from_directory("easter-egg", "zork-valley.png")


@app.route("/zork-help.png")
def zork_help():
    return send_from_directory("easter-egg", "zork-help.png")


@app.route("/zork-return.png")
def zork_return():
    return send_from_directory("easter-egg", "zork-return.png")


zork_sessions = {}  # session id -> ZorkGame, one adventure per browser tab


@app.route("/api/zork", methods=["POST"])
def zork_api():
    data = request.json or {}
    sid = str(data.get("session") or "default")
    if ZorkGame is None:
        return jsonify({"reply": "The Zork engine isn't connected.", "game_over": False})
    if data.get("restart") or sid not in zork_sessions:
        zork_sessions[sid] = ZorkGame()
    game = zork_sessions[sid]
    text, over = game.command(data.get("message") or "")
    return jsonify({"reply": text, "game_over": over})


async def _fetch_state():
    if not current_user:
        # Anonymous session: the dashboard tracks the conversation live;
        # these turns attach to the customer record at login.
        return {
            "logged_in": False,
            "anonymous": True,
            "customer": "Guest",
            "turns": [
                {"date": date.today().isoformat(), "user": u, "assistant": a,
                 "csat": s, "csat_reason": r, "topic_l1": l1, "topic_l2": l2,
                 "pre_login": True}
                for (u, a, s, r, l1, l2) in pending_turns
            ],
            "tickets": [],
            "conversation_summary": current_summary or None,
        }
    server_script = os.path.join(os.path.dirname(os.path.abspath(__file__)), "server.py")
    params = StdioServerParameters(command=sys.executable, args=[server_script])
    async with stdio_client(params) as (read, write):
        async with ClientSession(read, write) as session:
            await session.initialize()
            cust = _tool_json(await session.call_tool(
                "get_customer", {"email": current_user["email"]}))
            ticks = _tool_json(await session.call_tool(
                "list_escalation_tickets", {"customer_email": current_user["email"]}))
    details = (cust or {}).get("details") or {}
    interactions = details.get("interactions") or []
    turns = []
    for inter in interactions:
        for t in inter.get("turns") or []:
            turns.append({"date": inter.get("date"), **t})
    today_entry = next(
        (i for i in reversed(interactions)
         if i.get("date") == date.today().isoformat()), None)
    tickets_list = (ticks or {}).get("tickets") or []
    # The conversation summary: the living narrative, mirrored to the record
    # every turn -- present whether or not a ticket was ever filed.
    conversation_summary = details.get("last_contact_note") or (
        tickets_list[0]["summary"] if tickets_list else None)
    return {
        "logged_in": True,
        "customer": current_user["name"],
        "turns": turns,
        "tickets": tickets_list,
        "disposition": (today_entry or {}).get("disposition", "open"),
        "conversation_summary": conversation_summary,
    }


@app.route("/api/reset", methods=["POST"])
def reset():
    global messages, current_user, pending_turns, session_escalated, current_summary
    messages = [{"role": "system", "content": SYSTEM_PROMPT}]
    current_user = None
    pending_turns = []
    session_escalated = False
    current_summary = ""
    return jsonify({"ok": True})


if __name__ == "__main__":
    app.run(port=5000)
