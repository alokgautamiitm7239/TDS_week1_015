import os
import re
import sqlite3
import requests
from datetime import datetime
from urllib.parse import urljoin
from zoneinfo import ZoneInfo

# --------------------------------------------------
# CONFIGURATION
# --------------------------------------------------

START_URL = os.getenv("START_URL")

AI_TOKEN = (
    os.getenv("AIPIPE_API_KEY")
    or os.getenv("AIPIPE_TOKEN")
    or os.getenv("OPENAI_API_KEY")
)

AI_ENDPOINT = os.getenv(
    "AIPIPE_ENDPOINT",
    "https://aipipe.org/openai/v1/chat/completions"
)

AI_MODEL = os.getenv("AIPIPE_MODEL", "gpt-4.1-nano")

TZ = ZoneInfo("Asia/Kolkata")
SESSION = requests.Session()

db = None
DB_INITIALIZED = False


# --------------------------------------------------
# HELPERS
# --------------------------------------------------

def get_json(url):
    response = SESSION.get(url, timeout=45)
    response.raise_for_status()
    return response.json()


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

    except (ValueError, TypeError, OverflowError):
        return None


def normalize_sql(sql):
    sql = re.sub(r"```(?:sql)?", "", sql, flags=re.IGNORECASE)
    sql = sql.replace("```", "").strip()
    sql = sql.rstrip(";").strip()

    # Allow only read-only SELECT queries and CTEs.
    if not re.match(r"^(SELECT|WITH)\b", sql, re.IGNORECASE):
        raise ValueError("Only SELECT queries are allowed.")

    # Exactly one statement; reject comments and statement chaining.
    if ";" in sql or "--" in sql or "/*" in sql or "*/" in sql:
        raise ValueError("Invalid SQL format.")

    forbidden = re.compile(
        r"\b(INSERT|UPDATE|DELETE|DROP|ALTER|CREATE|REPLACE|"
        r"ATTACH|DETACH|PRAGMA|VACUUM|REINDEX|"
        r"LOAD_EXTENSION|WRITEFILE|READFILE)\b",
        re.IGNORECASE,
    )

    if forbidden.search(sql):
        raise ValueError("Unsafe SQL operation detected.")

    return sql


# --------------------------------------------------
# LOAD DATA AND BUILD SQLITE DATABASE
# --------------------------------------------------

def init_database():
    global db, DB_INITIALIZED

    if DB_INITIALIZED:
        return

    if not START_URL:
        raise RuntimeError(
            "START_URL environment variable is missing."
        )

    source = get_json(START_URL)
    links = source.get("links", {})

    if not links.get("orders") or not links.get("rates"):
        raise RuntimeError("Orders or rates link is missing.")

    # Download every page of orders.
    all_orders = []
    next_url = urljoin(START_URL, links["orders"])
    visited_urls = set()

    while next_url:
        if next_url in visited_urls:
            raise RuntimeError("Repeated pagination URL detected.")

        visited_urls.add(next_url)
        page = get_json(next_url)

        page_orders = page.get("orders")
        if page_orders is None:
            page_orders = page.get("data", [])

        if isinstance(page_orders, list):
            all_orders.extend(page_orders)

        next_page = page.get("next") or page.get("next_page_path")
        next_url = urljoin(next_url, next_page) if next_page else None

    # Get USD conversion rates.
    rates_data = get_json(urljoin(START_URL, links["rates"]))

    rates = (
        rates_data.get("usd_per_unit")
        or rates_data.get("rates")
        or rates_data
    )

    if not isinstance(rates, dict):
        raise RuntimeError("Invalid currency rates response.")

    rates = {
        str(currency).upper(): float(rate)
        for currency, rate in rates.items()
        if isinstance(rate, (int, float))
    }

    # Deduplicate orders: retain the latest updated_at version.
    latest = {}

    for order in all_orders:
        order_id = order.get("id") or order.get("order_id")

        if order_id is None:
            continue

        order_id = str(order_id).strip()
        new_time = parse_time(order.get("updated_at") or order.get("created_at") or order.get("date"))

        if order_id not in latest:
            latest[order_id] = (new_time, order)
            continue

        old_time = latest[order_id][0]

        if new_time and (old_time is None or new_time >= old_time):
            latest[order_id] = (new_time, order)

    # Build in-memory database.
    connection = sqlite3.connect(
        ":memory:",
        check_same_thread=False
    )
    connection.row_factory = sqlite3.Row

    connection.execute("""
        CREATE TABLE orders (
            id TEXT,
            customer TEXT,
            region TEXT,
            product TEXT,
            qty REAL,
            unit_price REAL,
            amount REAL,
            currency TEXT,
            status TEXT,
            created_at TEXT,
            updated_at TEXT,
            date_str TEXT,
            amount_usd REAL
        )
    """)

    prepared = []

    for _, order in latest.values():
        currency = str(order.get("currency") or "USD").upper().strip()

        if currency not in rates:
            if currency == "USD":
                rate = 1.0
            else:
                raise RuntimeError(
                    f"Missing USD conversion rate for {currency}."
                )
        else:
            rate = rates[currency]

        amount = float(order.get("amount") or 0)
        created = parse_time(
            order.get("created_at") or order.get("date")
        )
        updated = parse_time(order.get("updated_at"))

        date_str = (
            created.strftime("%Y-%m-%d")
            if created
            else ""
        )

        prepared.append((
            str(order.get("id") or order.get("order_id") or "").strip(),
            str(
                order.get("customer")
                or order.get("customer_id")
                or ""
            ).strip(),
            str(order.get("region") or "").strip(),
            str(order.get("product") or "").strip(),
            float(order.get("qty") or 0),
            float(order.get("unit_price") or 0),
            amount,
            currency,
            str(order.get("status") or "").lower().strip(),
            created.isoformat() if created else "",
            updated.isoformat() if updated else "",
            date_str,
            amount * rate,
        ))

    connection.executemany("""
        INSERT INTO orders (
            id, customer, region, product, qty, unit_price, amount,
            currency, status, created_at, updated_at, date_str, amount_usd
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
    """, prepared)

    connection.commit()

    db = connection
    DB_INITIALIZED = True


# --------------------------------------------------
# NATURAL LANGUAGE TO SQL
# --------------------------------------------------

SYSTEM_PROMPT = """
You are a careful SQLite analyst for an order ledger.

Convert the user's question into ONE valid SQLite SELECT query.
Return only SQL, without markdown or explanations.

Only this table exists:

orders(
    id, customer, region, product, qty, unit_price, amount,
    currency, status, created_at, updated_at, date_str, amount_usd
)

Important rules:

1. Use only the orders table and its listed columns.
2. Never invent tables, columns, data, or values.
3. Use status = 'paid' for paid orders and paid revenue/sales.
4. Use status = 'refunded' for refunded orders.
5. Unless the question explicitly asks otherwise, revenue means paid revenue (status = 'paid').
6. Use SUM(amount_usd) for revenue converted to USD.
7. Use SUM(amount) for revenue in original currency only when asked.
8. COUNT(DISTINCT customer) means distinct customers.
9. COUNT(DISTINCT id) means distinct orders.
10. Use product LIKE '%term%' or LOWER(product) = 'term' when suitable.
11. Use date_str for date filters. For a month like April 2026, use: date_str LIKE '2026-04%'.
12. For questions about order quantities, use qty.
13. If a question asks "Which product...", return ONLY the product column name as the first column in SELECT (e.g., SELECT product FROM orders ... GROUP BY product ORDER BY SUM(amount_usd) DESC LIMIT 1).
14. Do not include extra metrics in the SELECT clause unless specifically requested alongside the entity.
15. Use SQLite-compatible SQL without markdown or multiple statements.
"""


def generate_sql(question):
    if not AI_TOKEN:
        raise RuntimeError(
            "AI Pipe token missing. Set AIPIPE_API_KEY."
        )

    response = SESSION.post(
        AI_ENDPOINT,
        headers={
            "Authorization": f"Bearer {AI_TOKEN}",
            "Content-Type": "application/json",
        },
        json={
            "model": AI_MODEL,
            "messages": [
                {"role": "system", "content": SYSTEM_PROMPT},
                {"role": "user", "content": question},
            ],
            "temperature": 0,
        },
        timeout=45,
    )

    response.raise_for_status()
    payload = response.json()

    sql = payload["choices"][0]["message"]["content"]

    if not sql:
        raise RuntimeError("AI returned an empty SQL query.")

    return normalize_sql(sql)


# --------------------------------------------------
# EXECUTE QUESTION
# --------------------------------------------------

def answer_question(question: str):
    if not isinstance(question, str) or not question.strip():
        raise ValueError("Please provide a question.")

    init_database()

    sql = generate_sql(question)
    # SQLite authorizer: allow reading only from orders.
    allowed_actions = {
        sqlite3.SQLITE_SELECT,
        sqlite3.SQLITE_READ,
        sqlite3.SQLITE_FUNCTION,
        getattr(sqlite3, "SQLITE_RECURSIVE", -1),
    }

    def authorize(action, arg1, arg2, database, trigger):
        if action not in allowed_actions:
            return sqlite3.SQLITE_DENY

        if action == sqlite3.SQLITE_READ:
            if arg1 != "orders":
                return sqlite3.SQLITE_DENY

        return sqlite3.SQLITE_OK

    db.set_authorizer(authorize)

    try:
        cursor = db.execute(sql)
        rows = cursor.fetchall()

        if not rows:
            return 0

        # Return single scalar value if query result is 1x1 (e.g., "Mixer" or total amount)
        if len(rows) == 1 and len(rows[0]) == 1:
            value = rows[0][0]

            if value is None:
                return 0

            if isinstance(value, float):
                return round(value, 2)

            return value

        # If question asks "Which product...", return just the single name if LIMIT 1
        if len(rows) == 1:
            first_val = rows[0][0]
            if len(rows[0]) == 1 or isinstance(first_val, str):
                return first_val

        return [dict(row) for row in rows]

    finally:
        db.set_authorizer(None)