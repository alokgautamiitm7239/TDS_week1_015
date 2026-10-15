
import os
import re
import requests
from datetime import datetime
from zoneinfo import ZoneInfo

# Keep your personalized URL in the START_URL environment variable.
START_URL = os.environ.get("START_URL")

if not START_URL:
    raise RuntimeError(
        "START_URL environment variable is not set."
    )

TZ = ZoneInfo("Asia/Kolkata")


def get_json(url):
    response = requests.get(url, timeout=20)
    response.raise_for_status()
    return response.json()


# -------------------- LOAD API DATA --------------------

source = get_json(START_URL)
links = source["links"]

all_orders = []
url = links["orders"]

while url:
    page = get_json(url)
    all_orders.extend(page.get("orders", []))
    url = page.get("next")

rates_data = get_json(links["rates"])
rates = rates_data["usd_per_unit"]


# -------------------- DATE HANDLING --------------------

def parse_time(value):
    if not value:
        return None

    try:
        dt = datetime.fromisoformat(
            str(value).replace("Z", "+00:00")
        )

        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=TZ)

        return dt.astimezone(TZ)

    except (ValueError, TypeError):
        return None


# -------------------- LATEST ORDER VERSION --------------------

latest_orders = {}

for order in all_orders:
    order_id = order.get("id")

    if order_id is None:
        continue

    previous = latest_orders.get(order_id)

    if previous is None:
        latest_orders[order_id] = order
        continue

    previous_time = parse_time(previous.get("updated_at"))
    current_time = parse_time(order.get("updated_at"))

    if current_time and (
        previous_time is None or current_time > previous_time
    ):
        latest_orders[order_id] = order

orders = list(latest_orders.values())


# -------------------- USD CONVERSION --------------------

def amount_usd(order):
    currency = str(order.get("currency", "USD")).upper()
    rate = rates.get(currency)

    if rate is None:
        raise ValueError(
            f"Missing USD conversion rate for {currency}"
        )

    amount = float(order.get("amount", 0))
    return amount * float(rate)


for order in orders:
    order["amount_usd"] = amount_usd(order)


# -------------------- AVAILABLE FILTER VALUES --------------------

regions = sorted(
    {
        str(o.get("region", "")).strip()
        for o in orders
        if o.get("region")
    },
    key=len,
    reverse=True,
)

products = sorted(
    {
        str(o.get("product", "")).strip()
        for o in orders
        if o.get("product")
    },
    key=len,
    reverse=True,
)

months = {
    "january": 1, "february": 2, "march": 3,
    "april": 4, "may": 5, "june": 6,
    "july": 7, "august": 8, "september": 9,
    "october": 10, "november": 11, "december": 12,
    "jan": 1, "feb": 2, "mar": 3, "apr": 4,
    "jun": 6, "jul": 7, "aug": 8, "sep": 9,
    "sept": 9, "oct": 10, "nov": 11, "dec": 12,
}


def contains_phrase(question, phrase):
    """Match a phrase without matching it inside another word."""
    pattern = r"(?<!\w)" + re.escape(phrase.lower()) + r"(?!\w)"
    return re.search(pattern, question.lower()) is not None


# -------------------- QUESTION FILTERS --------------------

def filter_question_orders(
    question,
    base_orders,
    use_updated_date=False,
):
    q = question.lower()
    result = list(base_orders)

    # Region filter: e.g. North region
    for region in regions:
        if contains_phrase(q, region):
            result = [
                o for o in result
                if str(o.get("region", "")).strip().lower()
                == region.lower()
            ]
            break

    # Product filter: e.g. Juicer or Juicers
    for product in products:
        matched = contains_phrase(q, product)

        if not matched and not product.lower().endswith("s"):
            matched = contains_phrase(q, product + "s")

        if matched:
            result = [
                o for o in result
                if str(o.get("product", "")).strip().lower()
                == product.lower()
            ]
            break

    # Month and year filter
    year_match = re.search(r"\b(20\d{2})\b", q)
    month_num = None

    for name, number in months.items():
        if contains_phrase(q, name):
            month_num = number
            break

    if year_match and month_num:
        year = int(year_match.group(1))
        date_field = (
            "updated_at" if use_updated_date else "created_at"
        )

        filtered = []

        for order in result:
            dt = parse_time(order.get(date_field))

            if (
                dt is not None
                and dt.year == year
                and dt.month == month_num
            ):
                filtered.append(order)

        result = filtered

    return result


# -------------------- CUSTOMER HELPERS --------------------

def unique_customers(selected):
    # Ignore differences in capitalization and extra spaces.
    customers = {}

    for order in selected:
        name = str(order.get("customer", "")).strip()

        if name:
            customers.setdefault(name.casefold(), name)

    return customers


# -------------------- QUESTION ANSWER ENGINE --------------------

def answer_question(question: str):
    q = question.lower()

    is_refund = (
        "refund" in q
        or "refunded" in q
        or "refunds" in q
    )

    # Refund questions use the latest order status and updated date.
    if is_refund:
        selected = [
            o for o in orders
            if str(o.get("status", "")).strip().lower()
            == "refunded"
        ]

        selected = filter_question_orders(
            question,
            selected,
            use_updated_date=True,
        )

        asks_amount = any(
            word in q
            for word in ("amount", "value", "revenue", "total", "money")
        )

        if asks_amount:
            return round(
                sum(o["amount_usd"] for o in selected), 2
            )

        return len(selected)

    # Paid orders are used for revenue, sales, and paid-order questions.
    paid = [
        o for o in orders
        if str(o.get("status", "")).strip().lower() == "paid"
    ]

    selected = filter_question_orders(question, paid)

    # Average paid order value
    if any(
        word in q
        for word in ("average", "mean", "avg")
    ):
        if not selected:
            return 0

        return round(
            sum(o["amount_usd"] for o in selected)
            / len(selected),
            2,
        )

    # Customer questions must be handled before product summaries.
    if "customer" in q or "customers" in q or "client" in q:
        customer_map = unique_customers(selected)

        if any(
            phrase in q
            for phrase in (
                "highest revenue",
                "most revenue",
                "top customer",
                "spent the most",
                "highest spending",
            )
        ):
            totals = {}

            for order in selected:
                name = str(order.get("customer", "")).strip()

                if name:
                    key = name.casefold()
                    totals[key] = (
                        totals.get(key, 0)
                        + order["amount_usd"]
                    )

            if not totals:
                return None

            top_key = max(totals, key=totals.get)
            return customer_map.get(top_key, top_key)

        if any(
            phrase in q
            for phrase in (
                "how many",
                "count",
                "number of",
                "different customers",
                "unique customers",
            )
        ):
            return len(customer_map)

        return {
            "count": len(customer_map),
            "customers": sorted(customer_map.values()),
        }

    # Product revenue and ranking questions
    if "product" in q or "products" in q or "item" in q:
        totals = {}

        for order in selected:
            name = str(order.get("product", "")).strip()

            if name:
                totals[name] = (
                    totals.get(name, 0)
                    + order["amount_usd"]
                )

        if any(
            phrase in q
            for phrase in (
                "highest revenue",
                "most revenue",
                "top product",
                "best selling product",
                "best-selling product",
            )
        ):
            if not totals:
                return None

            return max(totals, key=totals.get)

        return {
            name: round(value, 2)
            for name, value in sorted(
                totals.items(),
                key=lambda item: item[1],
                reverse=True,
            )
        }

    # Order count
    if any(
        phrase in q
        for phrase in (
            "how many orders",
            "number of orders",
            "count of orders",
            "order count",
        )
    ):
        return len(selected)

    # Revenue / sales
    if any(
        word in q
        for word in (
            "revenue",
            "sales",
            "paid order value",
            "total value",
        )
    ):
        return round(
            sum(o["amount_usd"] for o in selected), 2
        )

    return "I could not identify the requested calculation."
