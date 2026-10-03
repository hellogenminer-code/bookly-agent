"""Smoke test: call each tool directly and print what comes back."""
from server import (
    get_customer,
    get_order_status,
    get_policy,
    check_return_eligibility,
    initiate_return,
)

print("1. customer:", get_customer("maya.chen@example.com")["name"])
print("2. ORD-1001 status:", get_order_status("ORD-1001")["order"]["status"])
print("3. returns policy:", get_policy("returns")["policy"][:60], "...")
print("4. ORD-2001 eligibility:", check_return_eligibility("ORD-2001")["eligible"])
print("5. ORD-2001 return ($189.99):", initiate_return("ORD-2001", "changed my mind")["status"])
print("6. ORD-1002 return ($28.50):", initiate_return("ORD-1002", "changed my mind")["status"])