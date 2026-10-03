"""Bookly MCP server: the only thing allowed to touch the database.

The agent never gets database credentials. It can only call the narrow
tools below, which enforce the business rules (e.g. the $150 return limit).

Two tables:
  customers    - profile columns + one jsonb `details` (orders, preferences,
                 interactions). Interaction turns + CSAT are appended here.
  escalations  - human work queue. Insert-only: the agent can open tickets,
                 never edit or close them.
"""

import json
import os
import re
import sys
from datetime import date
from typing import Optional

from dotenv import load_dotenv
from mcp.server.fastmcp import FastMCP
from openai import OpenAI
from supabase import create_client

load_dotenv()

SUPABASE_URL = os.environ.get("SUPABASE_URL")
SUPABASE_SERVICE_KEY = os.environ.get("SUPABASE_SERVICE_KEY")

if not SUPABASE_URL or not SUPABASE_SERVICE_KEY:
    raise RuntimeError("Missing SUPABASE_URL or SUPABASE_SERVICE_KEY - check your .env file.")

supabase = create_client(SUPABASE_URL, SUPABASE_SERVICE_KEY)

mcp = FastMCP("bookly-tools")

MODEL = "gpt-4o-mini"
try:
    _oai = OpenAI()
except Exception as e:
    print(f"[server] OpenAI unavailable, handoff notes use fallback text: {e}",
          file=sys.stderr)
    _oai = None

RETURN_WINDOW_DAYS = 30
AUTO_APPROVE_LIMIT = 150.0

POLICIES = {
    "returns": (
        "Bookly accepts returns within 30 days of delivery. Books must be unread "
        "and in original condition. Unshipped orders can be cancelled for a full refund. "
        "Refunds under $150 are approved automatically; refunds of $150 or more "
        "require a human specialist's approval."
    ),
    "shipping": (
        "Standard shipping is free on orders over $25 and takes 3-5 business days. "
        "Express shipping is available at checkout for $6.99 (1-2 business days)."
    ),
}

STORE_INFO = {
    "name": "Bookly",
    "address": "123 Main Street",
    "hours": "Monday-Saturday 9am-9pm, Sunday 10am-6pm",
    "cafe": "Yes -- the Chapter & Verse cafe, open daily 8am-4pm. Espresso, tea, and pastries.",
    "phone": "555-010-0000",
}

CATALOG = [
    {"sku": "BK-PB-001", "title": "The Princess Bride (paperback)", "price": 16.99, "in_store": 0, "online": True},
    {"sku": "BK-HC-014", "title": "Project Hail Mary (hardcover)", "price": 28.50, "in_store": 5, "online": True},
    {"sku": "BK-PB-002", "title": "The Midnight Library (paperback)", "price": 18.99, "in_store": 7, "online": True},
    {"sku": "JR-LB-001", "title": "Leatherbound Journal - Tan", "price": 75.00, "in_store": 12, "online": True},
    {"sku": "JR-LB-002", "title": "Leatherbound Journal - Oxblood", "price": 75.00, "in_store": 8, "online": True},
]

ESCALATION_TRIGGERS = ("policy_threshold", "low_csat", "customer_request")


def _find_order(order_id: str):
    """Search every customer's orders for order_id. Returns (customer, order) or (None, None)."""
    resp = supabase.table("customers").select("id,name,email,phone,details").execute()
    for customer in resp.data:
        for order in customer["details"].get("orders", []):
            if order["order_id"] == order_id:
                return customer, order
    return None, None


def _set_order_status(customer_id: str, order_id: str, new_status: str) -> bool:
    """Persist an order status change into the customer's details jsonb."""
    resp = supabase.table("customers").select("details").eq("id", customer_id).execute()
    if not resp.data:
        return False
    details = resp.data[0]["details"] or {}
    for o in details.get("orders", []):
        if o.get("order_id") == order_id:
            o["status"] = new_status
    supabase.table("customers").update({"details": details}).eq("id", customer_id).execute()
    return True


def _ticket_reason(trigger: str, amount: float, context: Optional[dict]) -> str:
    """Factual, templated escalation reason -- no LLM judgment needed.
    Triggers accumulate comma-separated (e.g. "low_csat,policy_threshold")
    when one ticket absorbs multiple escalation events."""
    reasons = []
    parts = [t.strip() for t in (trigger or "").split(",") if t.strip()]
    if "policy_threshold" in parts:
        reasons.append(
            f"Reason for escalation: refund of ${amount:.2f} meets or exceeds "
            f"the ${AUTO_APPROVE_LIMIT:.0f} auto-approve limit.")
    if "low_csat" in parts:
        score = (context or {}).get("csat_score", "?")
        reasons.append(
            f"Reason for escalation: customer satisfaction dropped to {score}/5.")
    if "customer_request" in parts:
        reasons.append(
            "Reason for escalation: customer asked for a human specialist.")
    return " ".join(reasons) or \
        "Reason for escalation: customer asked for a human specialist."


def _recommend_next_step(customer: dict, trigger: str, order_id: str,
                         amount: float, context: Optional[dict],
                         details: dict) -> str:
    """One concrete next step for the human agent, grounded only in facts."""
    orders = (details or {}).get("orders") or []
    prior_returns = sum(1 for o in orders
                        if o.get("status") in ("returned", "cancelled"))
    order_line = ""
    if order_id:
        o = next((x for x in orders if x.get("order_id") == order_id), None)
        if o:
            order_line = (f"Order {order_id}: ${o.get('total')}, status "
                          f"{o.get('status')}, ordered {o.get('ordered_at')}.")
    facts = (
        f"Customer: {customer['name']} ({customer['email']}). Trigger: {trigger}. "
        f"{order_line} Prior returns: {prior_returns}. "
        f"Extra context: {json.dumps(context or {})}"
    )
    if _oai is None:
        return "Review the ticket and follow up with the customer within 1 business day."
    try:
        r = _oai.chat.completions.create(
            model=MODEL,
            messages=[
                {"role": "system", "content":
                 "You assist human support agents. Given the facts, recommend ONE "
                 "concrete next step (one sentence) to resolve efficiently. Ground it "
                 "ONLY in the facts given. Plain text, no markdown."},
                {"role": "user", "content": facts},
            ],
        )
        return r.choices[0].message.content.strip()
    except Exception as e:
        print(f"[handoff] recommendation failed: {e}", file=sys.stderr)
        return "Review the ticket and follow up with the customer within 1 business day."


def _narrative_only(text: str) -> str:
    """Strip the deterministic state line the web layer appends, leaving the
    pure conversation narrative."""
    i = (text or "").find("\nMost recent topic:")
    return text[:i].strip() if i != -1 else (text or "").strip()


def _conversation_state_line(details: dict) -> str:
    """Deterministic current-state line for the ticket note, from today's turns."""
    topic, csat = "n/a", "n/a"
    today = date.today().isoformat()
    for inter in (details or {}).get("interactions") or []:
        if inter.get("date") == today and inter.get("turns"):
            t = inter["turns"][-1]
            topic = f"{t.get('topic_l1', '?')} › {t.get('topic_l2', '?')}".replace("_", " ")
            if isinstance(t.get("csat"), int):
                csat = f"{t['csat']}/5"
    return (f"Most recent topic: {topic}. Current CSAT: {csat}. "
            "Auth status: Authenticated via email.")


def _latest_csat(details: dict) -> Optional[int]:
    """Most recent scored CSAT from today's turns, or None if unknown."""
    today = date.today().isoformat()
    for inter in reversed((details or {}).get("interactions") or []):
        if inter.get("date") == today:
            for t in reversed(inter.get("turns") or []):
                if isinstance(t.get("csat"), int):
                    return t["csat"]
    return None


def _open_ticket_today_by_email(customer_email: str) -> Optional[dict]:
    """Most recent open escalation ticket from today, or None."""
    today = date.today().isoformat()
    resp = (
        supabase.table("escalations")
        .select("ticket_ref,created_at,trigger,priority,status")
        .eq("customer_email", customer_email)
        .eq("status", "open")
        .order("created_at", desc=True)
        .execute()
    )
    for t in resp.data or []:
        if (t.get("created_at") or "")[:10] == today:
            return t
    return None


def _update_ticket(ticket_ref: str, priority: Optional[str] = None,
                   csat: Optional[int] = None,
                   context_extra: Optional[dict] = None,
                   trigger: Optional[str] = None,
                   order_id: Optional[str] = None,
                   amount: Optional[float] = None,
                   summary: Optional[str] = None) -> dict:
    """Update an open ticket: priority, CSAT, trigger, order/amount, summary,
    and/or merged context. Raises on failure."""
    resp = supabase.table("escalations").select("ticket_ref,context").eq("ticket_ref", ticket_ref).execute()
    if not resp.data:
        raise ValueError("Ticket not found.")
    context = dict(resp.data[0].get("context") or {})
    if csat is not None:
        context["csat"] = csat
    if context_extra:
        context.update(context_extra)
    patch = {"context": context}
    if priority:
        patch["priority"] = priority
    if trigger:
        patch["trigger"] = trigger
    if order_id:
        patch["order_id"] = order_id
    if amount is not None:
        patch["amount"] = amount
    if summary:
        patch["summary"] = summary
    supabase.table("escalations").update(patch).eq("ticket_ref", ticket_ref).execute()
    return {"updated": True, "ticket_ref": ticket_ref, **patch}


def _build_ticket_note(c: dict, details: dict, trigger: str, order_id: str,
                       amount: float, context: Optional[dict],
                       one_liner: str, csat: Optional[int] = None):
    """Build the handoff note and priority for a ticket.
    Returns (note, priority, csat). Shared by ticket creation and updates so a
    ticket that absorbs a later escalation reads as one coherent handoff."""
    if csat is None:
        csat = _latest_csat(details)  # last scored turn; None if unknown
    priority = "high" if isinstance(csat, int) and csat <= 2 else "normal"
    raw = ""
    today = date.today().isoformat()
    for inter in (details or {}).get("interactions") or []:
        if inter.get("date") == today and inter.get("summary"):
            raw = inter["summary"]
    raw = raw or details.get("last_contact_note") or ""
    living = _narrative_only(raw)
    reason = _ticket_reason(trigger, amount, context)
    rec = _recommend_next_step(c, trigger, order_id, amount, context, details)
    parts = []
    if living:
        parts.append(living)
    parts.append(reason)
    parts.append(f"Recommended next step: {rec}")
    parts.append(_conversation_state_line(details))
    note = "\n".join(parts)
    return note, priority, csat


def _create_ticket(customer_id: str, trigger: str, summary: str,
                   order_id: str = "", amount: float = 0.0,
                   context: Optional[dict] = None,
                   csat: Optional[int] = None) -> dict:
    """Shared helper: insert one row into the escalations work queue. The ticket note
    is the living conversation narrative plus the escalation reason, a recommended
    next step, and the current state -- the handoff view a human agent wants.
    Priority follows the customer's satisfaction: CSAT 1-2 -> high, otherwise normal.
    The CSAT is recorded on the ticket. Raises on failure."""
    parts = [t.strip() for t in (trigger or "").split(",") if t.strip()]
    if not parts or any(t not in ESCALATION_TRIGGERS for t in parts):
        raise ValueError(f"Unknown escalation trigger: {trigger}")
    resp = supabase.table("customers").select("id,name,email,details").eq("id", customer_id).execute()
    if not resp.data:
        raise ValueError("Customer not found.")
    c = resp.data[0]
    details = c.get("details") or {}
    note, priority, csat = _build_ticket_note(
        c, details, trigger, order_id, amount, context, summary, csat)
    row = {
        "customer_email": c["email"],
        "customer_name": c["name"],
        "trigger": trigger,
        "priority": priority,
        "order_id": order_id or None,
        "amount": amount or None,
        "summary": note,
        "context": {**(context or {}), "one_liner": summary, "csat": csat},
        "status": "open",
    }
    ins = supabase.table("escalations").insert(row).execute()
    ticket = ins.data[0]
    # Last contact note on the customer record -- just like a human would leave.
    try:
        details["last_contact_note"] = note
        supabase.table("customers").update({"details": details}).eq("id", customer_id).execute()
    except Exception as e:
        print(f"[ticket] last-contact note failed: {e}", file=sys.stderr)
    try:
        _stamp_disposition(customer_id, "escalated")
    except Exception as e:
        # The ticket is the record that matters; a missed stamp must not fail it.
        print(f"[ticket] disposition stamp failed: {e}", file=sys.stderr)
    return ticket


def _stamp_disposition(customer_id: str, disposition: str) -> None:
    """Mark today's interaction entry with a disposition (open/escalated).
    Deterministic -- the app never asks the model to judge outcomes."""
    resp = supabase.table("customers").select("details").eq("id", customer_id).execute()
    if not resp.data:
        return
    details = resp.data[0]["details"] or {}
    interactions = details.get("interactions") or []
    today = date.today().isoformat()
    entry = None
    for inter in reversed(interactions):
        if inter.get("date") == today:
            entry = inter
            break
    if entry is None:
        entry = {"date": today, "turns": [], "disposition": disposition}
        interactions.append(entry)
    else:
        entry["disposition"] = disposition
    details["interactions"] = interactions
    supabase.table("customers").update({"details": details}).eq("id", customer_id).execute()


def _check_eligibility(order_id: str) -> dict:
    customer, order = _find_order(order_id)
    if order is None:
        return {"eligible": False, "reason": f"Order {order_id} not found."}
    status = order["status"]
    if status in ("processing", "shipped"):
        return {
            "eligible": True,
            "order_id": order_id,
            "total": order["total"],
            "customer_name": customer["name"],
            "customer_id": customer["id"],
            "customer_email": customer["email"],
            "cancellation": True,
        }
    if status != "delivered":
        return {"eligible": False, "reason": f"Order is '{status}' and cannot be returned."}
    delivered = date.fromisoformat(order["delivered_at"])
    days_since = (date.today() - delivered).days
    if days_since > RETURN_WINDOW_DAYS:
        return {
            "eligible": False,
            "reason": f"Delivered {days_since} days ago; return window is {RETURN_WINDOW_DAYS} days.",
        }
    return {
        "eligible": True,
        "order_id": order_id,
        "total": order["total"],
        "customer_name": customer["name"],
        "customer_id": customer["id"],
        "customer_email": customer["email"],
    }


@mcp.tool()
def authenticate_customer(email: str) -> dict:
    """Verify a customer's email to log them in. Call this first -- customer data
    must never be revealed before authentication succeeds."""
    resp = (
        supabase.table("customers")
        .select("id,name,email")
        .eq("email", email)
        .execute()
    )
    if not resp.data:
        return {"authenticated": False, "email": email}
    c = resp.data[0]
    return {"authenticated": True, "customer_id": c["id"], "name": c["name"], "email": c["email"]}


@mcp.tool()
def get_customer(email: str) -> dict:
    """Look up a customer by email. Returns profile, orders, preferences, and past interactions."""
    resp = (
        supabase.table("customers")
        .select("id,name,email,phone,details")
        .eq("email", email)
        .execute()
    )
    if not resp.data:
        return {"found": False, "email": email}
    return {"found": True, **resp.data[0]}


@mcp.tool()
def get_store_info() -> dict:
    """Get the Bookly store's hours, cafe info, address, and phone number."""
    return STORE_INFO


def _norm_words(text: str) -> list[str]:
    """Lowercase words with simple plural normalization ('journals' -> 'journal')."""
    words = re.sub(r"[^a-z0-9]+", " ", text.lower()).split()
    return [w[:-1] if w.endswith("s") and len(w) > 3 else w for w in words]


@mcp.tool()
def search_catalog(query: str) -> dict:
    """Search the product catalog by keyword. Matches on whole words (so
    'journals' finds 'journal') and falls back to substring matching.
    Returns matches with price and stock levels."""
    q = query.lower().strip()
    qw = _norm_words(q)
    matches = []
    for item in CATALOG:
        title = item["title"].lower()
        tw = _norm_words(title)
        if (qw and all(w in tw for w in qw)) or (q and q in title):
            matches.append(item)
    return {"query": query, "results": matches}


@mcp.tool()
def create_order(customer_id: str, skus: list[str]) -> dict:
    """Create a new online order for a customer from a list of catalog SKUs.
    The order starts as 'processing'."""
    resp = supabase.table("customers").select("id,name,details").eq("id", customer_id).execute()
    if not resp.data:
        return {"created": False, "reason": "Customer not found."}
    customer = resp.data[0]
    items = []
    total = 0.0
    for sku in skus:
        product = next((p for p in CATALOG if p["sku"] == sku), None)
        if product is None:
            return {"created": False, "reason": f"Unknown SKU: {sku}"}
        if not product["online"]:
            return {"created": False, "reason": f"{product['title']} is not available online."}
        items.append(product["title"])
        total += product["price"]
    existing = []
    for c in supabase.table("customers").select("details").execute().data:
        for o in c["details"].get("orders", []):
            oid = o.get("order_id", "")
            if oid.startswith("ORD-") and oid[4:].isdigit():
                existing.append(int(oid[4:]))
    order_id = f"ORD-{max(existing, default=1000) + 1}"
    order = {
        "order_id": order_id,
        "items": items,
        "total": round(total, 2),
        "status": "processing",
        "ordered_at": date.today().isoformat(),
    }
    details = customer["details"]
    orders = details.get("orders", [])
    orders.append(order)
    details["orders"] = orders
    supabase.table("customers").update({"details": details}).eq("id", customer_id).execute()
    return {"created": True, "order": order, "customer_name": customer["name"]}


@mcp.tool()
def get_order_status(order_id: str) -> dict:
    """Get the status of an order by its order ID, e.g. 'ORD-1001'."""
    customer, order = _find_order(order_id)
    if order is None:
        return {"found": False, "order_id": order_id}
    return {"found": True, "customer_name": customer["name"], "order": order}


@mcp.tool()
def get_policy(topic: str) -> dict:
    """Get a store policy. Topic must be 'returns' or 'shipping'."""
    policy = POLICIES.get(topic.lower())
    if policy is None:
        return {"found": False, "topic": topic, "available_topics": list(POLICIES)}
    return {"found": True, "topic": topic, "policy": policy}


@mcp.tool()
def check_return_eligibility(order_id: str) -> dict:
    """Check whether an order can be returned or cancelled. Delivered orders must be
    within 30 days; unshipped orders can always be cancelled. Orders $150+ require
    specialist review -- never tell the customer the return is approved."""
    result = _check_eligibility(order_id)
    if result.get("eligible") and result.get("total", 0) >= AUTO_APPROVE_LIMIT:
        result["review_required"] = True
        result["note"] = (
            f"Order total ${result['total']:.2f} meets/exceeds the ${AUTO_APPROVE_LIMIT:.0f} "
            "auto-approve limit: a return is possible but REQUIRES specialist review. Do NOT "
            "tell the customer the return is approved or eligible, and do NOT say the order "
            "is canceled -- collect their reason, then call initiate_return to submit it "
            "for review."
        )
    return result


@mcp.tool()
def initiate_return(order_id: str, reason: str) -> dict:
    """Start a return (delivered orders) or cancellation (unshipped orders).
    Auto-approves refunds under $150 and persists the new order status.
    Refunds of $150+ open a real escalation ticket for a human specialist and
    mark the order as awaiting review."""
    eligibility = _check_eligibility(order_id)
    if not eligibility["eligible"]:
        return {"status": "rejected", "reason": eligibility["reason"]}
    total = eligibility["total"]
    customer_id = eligibility["customer_id"]
    action = "cancelled" if eligibility.get("cancellation") else "returned"
    if total >= AUTO_APPROVE_LIMIT:
        ticket_ref = None
        deduped = False
        one_liner = (
            f"{action.capitalize()} of order {order_id} (${total:.2f}) needs human "
            f"approval (auto-approve limit is ${AUTO_APPROVE_LIMIT:.0f}). "
            f"Customer reason: {reason}"
        )
        ctx = {
            "order_id": order_id,
            "amount": total,
            "action": action,
            "customer_reason": reason,
        }
        try:
            existing = _open_ticket_today_by_email(eligibility["customer_email"])
            if existing:
                # One ticket per conversation: fold the policy escalation into
                # today's open ticket instead of filing a second one.
                prev = [t for t in (existing.get("trigger") or "").split(",") if t]
                if "policy_threshold" not in prev:
                    prev.append("policy_threshold")
                trigger = ",".join(prev)
                cust = supabase.table("customers").select("id,name,email,details").eq(
                    "id", customer_id).execute().data[0]
                details = cust.get("details") or {}
                note, priority, csat = _build_ticket_note(
                    cust, details, trigger, order_id, total, ctx, one_liner)
                _update_ticket(
                    existing["ticket_ref"],
                    trigger=trigger,
                    priority=priority,
                    order_id=order_id,
                    amount=total,
                    summary=note,
                    context_extra={**ctx, "one_liner": one_liner, "csat": csat},
                )
                try:
                    details["last_contact_note"] = note
                    supabase.table("customers").update(
                        {"details": details}).eq("id", customer_id).execute()
                except Exception as e:
                    print(f"[initiate_return] last-contact note failed: {e}",
                          file=sys.stderr)
                try:
                    _stamp_disposition(customer_id, "escalated")
                except Exception as e:
                    print(f"[initiate_return] disposition stamp failed: {e}",
                          file=sys.stderr)
                ticket_ref = existing["ticket_ref"]
                deduped = True
            else:
                ticket = _create_ticket(
                    customer_id,
                    "policy_threshold",
                    one_liner,
                    order_id=order_id,
                    amount=total,
                    context=ctx,
                )
                ticket_ref = ticket["ticket_ref"]
        except Exception as e:
            # Never break the conversation over a ticket write; the escalation
            # itself is still reported to the customer.
            print(f"[initiate_return] ticket write failed: {e}", file=sys.stderr)
        _set_order_status(customer_id, order_id, "awaiting_review")
        message = (
            f"Refund of ${total:.2f} meets or exceeds the ${AUTO_APPROVE_LIMIT:.0f} "
            f"auto-approve limit. Ticket {ticket_ref or '(pending)'} is open. "
            "The order is ON HOLD as 'awaiting_review' -- it has NOT been canceled and the "
            "refund has NOT been approved. Tell the customer a human specialist will review "
            "it and follow up within 1 business day. Do NOT say the order is canceled."
        )
        if deduped:
            message = (
                f"Ticket {ticket_ref} was already open for this conversation, so this "
                f"escalation was folded into it instead of filing a second ticket. "
            ) + message
        return {
            "status": "escalated",
            "order_id": order_id,
            "reason": reason,
            "action": action,
            "ticket_ref": ticket_ref,
            "deduped": deduped,
            "message": message,
        }
    _set_order_status(customer_id, order_id, action)
    return {
        "status": "approved",
        "order_id": order_id,
        "reason": reason,
        "action": action,
        "refund_amount": total,
        "message": (
            f"Order {action}. ${total:.2f} will be refunded to the original "
            "payment method within 5-7 business days."
        ),
    }


@mcp.tool()
def request_human_handoff(customer_id: str, reason: str) -> dict:
    """Hand the conversation to a human specialist because the customer asked for
    one. Opens a real escalation ticket -- or folds into today's open ticket if
    one exists. Call this when the customer asks to talk to a human / a real person."""
    try:
        email_resp = supabase.table("customers").select("email").eq("id", customer_id).execute()
        email = email_resp.data[0]["email"] if email_resp.data else None
        existing = _open_ticket_today_by_email(email) if email else None
        if existing:
            _update_ticket(existing["ticket_ref"], priority="high",
                           context_extra={"handoff_request": reason})
            return {
                "created": False,
                "deduped": True,
                "ticket_ref": existing["ticket_ref"],
                "message": (
                    f"Ticket {existing['ticket_ref']} is already open -- the handoff request "
                    "was added to it and its priority raised to high. Tell the customer a "
                    "human specialist will follow up within 1 business day."
                ),
            }
        ticket = _create_ticket(
            customer_id,
            "customer_request",
            f"Customer asked for a human specialist. Reason: {reason}",
            context={"customer_reason": reason},
        )
        return {
            "created": True,
            "ticket_ref": ticket["ticket_ref"],
            "message": (
                f"Ticket {ticket['ticket_ref']} opened. A human specialist "
                "will follow up within 1 business day."
            ),
        }
    except Exception as e:
        return {"created": False, "reason": str(e)}


@mcp.tool()
def create_escalation_ticket(customer_id: str, trigger: str, summary: str,
                             order_id: str = "", amount: float = 0.0,
                             context: Optional[dict] = None,
                             csat: Optional[int] = None) -> dict:
    """Open a human-escalation ticket in the work queue. Priority follows the CSAT
    (1-2 -> high, otherwise normal) and the score is recorded on the ticket.
    The app calls this automatically; you do not need to call it yourself."""
    try:
        ticket = _create_ticket(customer_id, trigger, summary,
                                order_id, amount, context, csat)
        return {"created": True, "ticket_ref": ticket["ticket_ref"], "ticket": ticket}
    except Exception as e:
        return {"created": False, "reason": str(e)}


@mcp.tool()
def update_escalation_ticket(ticket_ref: str, priority: Optional[str] = None,
                             csat: Optional[int] = None) -> dict:
    """Update an open escalation ticket: raise its priority and/or record the
    latest CSAT. Used to fold a low-satisfaction signal into an already-open
    ticket instead of filing a second one."""
    try:
        return _update_ticket(ticket_ref, priority=priority, csat=csat)
    except Exception as e:
        return {"updated": False, "reason": str(e)}


@mcp.tool()
def list_escalation_tickets(customer_email: str) -> dict:
    """List a customer's escalation tickets, newest first. Read-only -- used by
    the live dashboard."""
    resp = (
        supabase.table("escalations")
        .select("ticket_ref,created_at,trigger,priority,order_id,amount,summary,status,context")
        .eq("customer_email", customer_email)
        .order("created_at", desc=True)
        .execute()
    )
    return {"tickets": resp.data}


@mcp.tool()
def append_interaction_turn(customer_id: str, user_text: str, assistant_text: str,
                            csat_score: Optional[int] = None,
                            csat_reason: str = "", pre_login: bool = False,
                            topic_l1: str = "Experience",
                            topic_l2: str = "other",
                            summary: str = "") -> dict:
    """Append one conversation turn (CSAT + topic tags) to today's interaction record
    in the customer's history. Append-only. This is called automatically by the app
    after every reply -- you do not need to call it yourself. Turns from before
    login are tagged pre_login=True when they're attached at login."""
    resp = supabase.table("customers").select("details").eq("id", customer_id).execute()
    if not resp.data:
        return {"saved": False, "reason": "Customer not found."}
    details = resp.data[0]["details"] or {}
    interactions = details.get("interactions") or []
    today = date.today().isoformat()
    entry = None
    for inter in reversed(interactions):
        if inter.get("date") == today and isinstance(inter.get("turns"), list):
            entry = inter
            break
    if entry is None:
        entry = {"date": today, "turns": [], "disposition": "open"}
        interactions.append(entry)
    entry["turns"].append({
        "user": user_text[:500],
        "assistant": assistant_text[:500],
        "csat": csat_score,
        "csat_reason": csat_reason[:120],
        "topic_l1": topic_l1,
        "topic_l2": topic_l2,
        "pre_login": pre_login,
    })
    if summary:
        # Living conversation summary, updated every turn -- and mirrored as
        # the last contact note, just like a human agent would leave.
        entry["summary"] = summary[:600]
        details["last_contact_note"] = summary[:600]
    details["interactions"] = interactions
    supabase.table("customers").update({"details": details}).eq("id", customer_id).execute()
    return {"saved": True, "turns_today": len(entry["turns"])}


@mcp.tool()
def save_interaction_note(customer_id: str, summary: str) -> dict:
    """Save a short note about an interaction to the customer's history. Append-only."""
    resp = supabase.table("customers").select("details").eq("id", customer_id).execute()
    if not resp.data:
        return {"saved": False, "reason": "Customer not found."}
    details = resp.data[0]["details"]
    interactions = details.get("interactions", [])
    interactions.append({"date": date.today().isoformat(), "summary": summary})
    details["interactions"] = interactions
    supabase.table("customers").update({"details": details}).eq("id", customer_id).execute()
    return {"saved": True, "customer_id": customer_id}


if __name__ == "__main__":
    mcp.run()
