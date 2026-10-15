
import json
from datetime import datetime


with open("orders.json", "r", encoding="utf-8") as f:
    orders = json.load(f)

with open("rates.json", "r", encoding="utf-8") as f:
    rates = json.load(f)


# Keep latest updated version of each order
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


paid_orders = [
    order for order in latest_orders.values()
    if order["status"] == "paid"
]

for order in paid_orders:
    order["amount_usd"] = round(
        order["amount"] * rates[order["currency"]], 2
    )


def answer_question(question: str):
    q = question.lower()

    if "revenue" in q or "sales" in q:
        return round(sum(o["amount_usd"] for o in paid_orders), 2)

    if "refund" in q:
        return "Please specify the refund details needed."

    if "customer" in q:
        customers = sorted(set(o["customer"] for o in paid_orders))
        return {"count": len(customers), "customers": customers}

    if "product" in q:
        product_revenue = {}
        for order in paid_orders:
            product = order["product"]
            product_revenue[product] = (
                product_revenue.get(product, 0) + order["amount_usd"]
            )
        return {
            product: round(amount, 2)
            for product, amount in sorted(
                product_revenue.items(),
                key=lambda item: item[1],
                reverse=True
            )
        }

    return "I could not identify the requested calculation."
