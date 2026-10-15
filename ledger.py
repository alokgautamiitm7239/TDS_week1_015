
import os
import re
import requests
from datetime import datetime
from zoneinfo import ZoneInfo

START_URL = os.environ.get("START_URL")
if not START_URL:
    raise RuntimeError("START_URL environment variable is not set")

TZ = ZoneInfo("Asia/Kolkata")


def get_json(url):
    response = requests.get(url, timeout=10)
    response.raise_for_status()
    return response.json()


# Load the API and all order pages once when the server starts
source = get_json(START_URL)
links = source["links"]

all_orders = []
url = links["orders"]

while url:
    page = get_json(url)
    all_orders.extend(page.get("orders", []))
    url = page.get("next")

rates = get_json(links["rates"])["usd_per_unit"]


def parse_time(value):
    if not value:
        return None
    try:
        dt = datetime.fromisoformat(value.replace("Z", "+00:00"))
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=TZ)
        return dt.astimezone(TZ)
    except (ValueError, TypeError):
        return None


# Keep the latest version of every order ID
latest_orders = {}

for order in all_orders:
    order_id = order["id"]
    old = latest_orders.get(order_id)

    if old is None:
        latest_orders[order_id] = order
    else:
        old_time = parse_time(old.get("updated_at"))
        new_time = parse_time(order.get("updated_at"))

        if new_time and (old_time is None or new_time > old_time):
            latest_orders[order_id] = order


def amount_usd(order):
    currency = order.get("currency", "USD")
    rate = float(rates.get(currency, 1))
    return float(order.get("amount", 0)) * rate


orders = list(latest_orders.values())

for order in orders:
    order["amount_usd"] = round(amount_usd(order), 2)


regions = sorted({
    str(o.get("region", "")).strip()
    for o in orders if o.get("region")
}, key=len, reverse=True)

months = {
    "january": 1, "february": 2, "march": 3,
    "april": 4, "may": 5, "june": 6,
    "july": 7, "august": 8, "september": 9,
    "october": 10, "november": 11, "december": 12,
    "jan": 1, "feb": 2, "mar": 3, "apr": 4,
    "jun": 6, "jul": 7, "aug": 8, "sep": 9,
    "sept": 9, "oct": 10, "nov": 11, "dec": 12
}


def filter_question_orders(question, base_orders, use_updated_date=False):
    q = question.lower()
    result = list(base_orders)

    # Region filter
    for region in regions:
        if region.lower() in q:
            result = [
                o for o in result
                if str(o.get("region", "")).lower() == region.lower()
            ]
            break

    # Month/year filter; use India business time
    year_match = re.search(r"\b(20\d{2})\b", q)
    month_num = None

    for name, number in months.items():
        if re.search(r"\b" + re.escape(name) + r"\b", q):
            month_num = number
            break

    if year_match and month_num:
        year = int(year_match.group(1))
        date_field = "updated_at" if use_updated_date else "created_at"

        result = [
            o for o in result
            if (dt := parse_time(o.get(date_field))) is not None
            and dt.year == year
            and dt.month == month_num
        ]

    return result


def answer_question(question: str):
    q = question.lower()

    is_refund = "refund" in q or "refunded" in q

    if is_refund:
        # Use updated_at for the date a status change/refund was recorded
        selected = [
            o for o in orders
            if str(o.get("status", "")).lower() == "refunded"
        ]
        selected = filter_question_orders(
            question, selected, use_updated_date=True
        )
        print("Filtered orders:", len(selected))
        print("Regions:", sorted(set(o.get("region") for o in selected)))

        if any(word in q for word in ("amount", "value", "revenue", "total")):
            return round(sum(o["amount_usd"] for o in selected), 2)

        return len(selected)

    # Revenue and average order value use paid orders only
    paid = [
        o for o in orders
        if str(o.get("status", "")).lower() == "paid"
    ]
    selected = filter_question_orders(question, paid)

    if "average" in q or "average order value" in q or "mean" in q:
        if not selected:
            return 0
        return round(
            sum(o["amount_usd"] for o in selected) / len(selected), 2
        )

    # Customer questions
    if "customer" in q or "customers" in q:
        if "highest" in q or "most revenue" in q or "top customer" in q:
            totals = {}
            for o in selected:
                name = o.get("customer", "")
                totals[name] = totals.get(name, 0) + o["amount_usd"]
            if not totals:
                return None
            return max(totals, key=totals.get)

        unique_customers = sorted({
            o.get("customer", "") for o in selected if o.get("customer")
        })

        if "how many" in q or "count" in q or "number of" in q:
            return len(unique_customers)

        return {"count": len(unique_customers), "customers": unique_customers}

    # Product questions
    if "product" in q or "products" in q:
        totals = {}
        for o in selected:
            name = o.get("product", "")
            totals[name] = totals.get(name, 0) + o["amount_usd"]

        if not totals:
            return {} if "revenue" in q or "sales" in q else None

        if "highest" in q or "most revenue" in q or "top product" in q:
            return max(totals, key=totals.get)

        return {
            name: round(value, 2)
            for name, value in sorted(
                totals.items(), key=lambda item: item[1], reverse=True
            )
        }

    # Order count
    if "how many orders" in q or "number of orders" in q or "count of orders" in q:
        return len(selected)

    # Revenue / sales
    if "revenue" in q or "sales" in q or "paid order value" in q:
        return round(sum(o["amount_usd"] for o in selected), 2)

    return "I could not identify the requested calculation."
