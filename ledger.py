
import os
import requests
from datetime import datetime

START_URL = os.environ.get("START_URL")

if not START_URL:
    raise RuntimeError("START_URL environment variable is not set")

def get_json(url):
    response = requests.get(url, timeout=30)
    response.raise_for_status()
    return response.json()

# Get the service links from the assignment API
data = get_json(START_URL)
links = data["links"]

# Download all order pages
orders = []
url = links["orders"]

while url:
    page_data = get_json(url)
    orders.extend(page_data.get("orders", []))
    url = page_data.get("next")

# Download currency conversion rates
rates_data = get_json(links["rates"])
rates = rates_data["usd_per_unit"]

# Keep the latest updated version of each order
latest_orders = {}

for order in orders:
    order_id = order["id"]
    old = latest_orders.get(order_id)

    if old is None:
        latest_orders[order_id] = order
    else:
        old_time = datetime.fromisoformat(
            old["updated_at"].replace("Z", "+00:00")
        )
        new_time = datetime.fromisoformat(
            order["updated_at"].replace("Z", "+00:00")
        )

        if new_time > old_time:
            latest_orders[order_id] = order

# Only paid orders count as revenue
paid_orders = [
    order for order in latest_orders.values()
    if order.get("status") == "paid"
]

for order in paid_orders:
    order["amount_usd"] = round(
        float(order["amount"]) * rates[order["currency"]], 2
    )

def answer_question(question: str):
    q = question.lower()

    if "refund" in q:
        filtered = [
            o for o in latest_orders.values()
            if o.get("status", "").lower() == "refunded"
            and o.get("region", "").lower() in q
            and "february" in q
            and o.get("created_at", "").startswith("2026-02")
        ]
        return len(filtered)

    if "customer" in q:
        customers = sorted(set(o["customer"] for o in paid_orders))
        return {"count": len(customers), "customers": customers}

    if "product" in q:
        product_revenue = {}

        for order in paid_orders:
            product = order["product"]
            product_revenue[product] = (
                product_revenue.get(product, 0)
                + order["amount_usd"]
            )

        return {
            product: round(amount, 2)
            for product, amount in sorted(
                product_revenue.items(),
                key=lambda item: item[1],
                reverse=True
            )
        }

    if "revenue" in q or "sales" in q:
        return round(
            sum(o["amount_usd"] for o in paid_orders), 2
        )

    return "I could not identify the requested calculation."
