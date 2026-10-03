"""Bookly MCP server: the only thing allowed to touch the database.

The agent never gets database credentials. It can only call the narrow
tools below, which enforce the business rules (e.g. the $150 return limit).
"""

import os
from datetime import date

from dotenv import load_dotenv
from mcp.server.fastmcp import FastMCP
from supabase import create_client

load_dotenv()

SUPABASE_URL = os.environ.get("SUPABASE_URL")
SUPABASE_SERVICE_KEY = os.environ.get("SUPABASE_SERVICE_KEY")

if not SUPABASE_URL or not SUPABASE_SERVICE_KEY:
    raise RuntimeError("Missing SUPABASE_URL or SUPABASE_SERVICE_KEY - check your .env file.")

supabase = create_client(SUPABASE_URL, SUPABASE_SERVICE_KEY)

mcp = FastMCP("bookly-tools")

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


def _find_order(order_id: str):
    """Search every customer's orders for order_id. Returns (customer, order) or (None, None)."""
    resp = supabase.table("customers").select("id,name,email,phone,details").execute()
    for customer in resp.data:
        for order in customer["details"].get("orders", []):
            if order["order_id"] == order_id:
                return customer, order
    return None, None


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


@mcp.tool()
def search_catalog(query: str) -> dict:
    """Search the product catalog by title keyword. Returns matches with price
    and stock levels (in_store count and whether it's available online)."""
    q = query.lower()
    matches = [item for item in CATALOG if q in item["title"].lower()]
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
    within 30 days; unshipped orders can always be cancelled."""
    return _check_eligibility(order_id)


@mcp.tool()
def initiate_return(order_id: str, reason: str) -> dict:
    """Start a return (delivered orders) or cancellation (unshipped orders).
    Auto-approves refunds under $150; escalates $150+ to a human specialist."""
    eligibility = _check_eligibility(order_id)
    if not eligibility["eligible"]:
        return {"status": "rejected", "reason": eligibility["reason"]}
    total = eligibility["total"]
    action = "cancelled" if eligibility.get("cancellation") else "returned"
    if total >= AUTO_APPROVE_LIMIT:
        return {
            "status": "escalated",
            "order_id": order_id,
            "reason": reason,
            "action": action,
            "message": (
                f"Refund of ${total:.2f} meets or exceeds the ${AUTO_APPROVE_LIMIT:.0f} "
                f"auto-approve limit. The order will be {action}, and a human specialist "
                "will review and follow up within 1 business day."
            ),
        }
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
