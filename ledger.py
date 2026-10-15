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

# Connection Pool for fast HTTP Requests
SESSION = requests.Session()
adapter = requests.adapters.HTTPAdapter(pool_connections=20, pool_maxsize=50)
SESSION.mount("https://", adapter)
SESSION.mount("http://", adapter)

GLOBAL_DB = None
DB_INITIALIZED = False

# Fast-path cache for SQL queries to save LLM tokens & execution time
SQL_CACHE = {}


# --------------------------------------------------
# HELPERS
# --------------------------------------------------

def get_json(url):
    response = SESSION.get(url, timeout=6)
    response.raise_for_status()
    return response.json()


def parse_time(value):
    if not value:
        return None

    try:
        dt = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=TZ)
        return dt.astimezone(TZ)
    except (ValueError, TypeError, OverflowError):
        return None


def normalize_sql(sql):
    sql = re.sub(r"```(?:sql)?", "", sql, flags=re.IGNORECASE)
    sql = sql.replace("```", "").strip()
    sql = sql.rstrip(";").strip()

    if not re.match(r"^(SELECT|WITH)\b", sql, re.IGNORECASE):
        raise ValueError("Only SELECT queries are allowed.")

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
# EAGER IN-MEMORY DB INITIALIZATION
# --------------------------------------------------

def init_database():
    global GLOBAL_DB, DB_INITIALIZED

    if DB_INITIALIZED and GLOBAL_DB is not None:
        return

    if not START_URL:
        raise RuntimeError("START_URL environment variable is missing.")

    source = get_json(START_URL)
    links = source.get("links", {})

    if not links.get("orders") or not links.get("rates"):
        raise RuntimeError("Orders or rates link is missing.")

    all_orders = []
    next_url = urljoin(START_URL, links["orders"])
    visited_urls = set()

    while next_url:
        if next_url in visited_urls:
            break
        visited_urls.add(next_url)
        page = get_json(next_url)

        page_orders = page.get("orders")
        if page_orders is None:
            page_orders = page.get("data", [])

        if isinstance(page_orders, list):
            all_orders.extend(page_orders)

        next_page = page.get("next") or page.get("next_page_path")
        next_url = urljoin(next_url, next_page) if next_page else None

    rates_data = get_json(urljoin(START_URL, links["rates"]))
    rates_raw = (
        rates_data.get("usd_per_unit")
        or rates_data.get("rates")
        or rates_data
    )

    rates = {
        str(k).upper(): float(v)
        for k, v in rates_raw.items()
        if isinstance(v, (int, float))
    }

    # Deduplication Logic
    latest = {}
    for order in all_orders:
        order_id = order.get("id") or order.get("order_id")
        if order_id is None:
            continue

        order_id = str(order_id).strip()
        new_time = parse_time(
            order.get("updated_at") or order.get("created_at") or order.get("date")
        )

        if order_id not in latest:
            latest[order_id] = (new_time, order)
        else:
            old_time = latest[order_id][0]
            if new_time and (old_time is None or new_time >= old_time):
                latest[order_id] = (new_time, order)

    connection = sqlite3.connect(":memory:", check_same_thread=False)
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
        rate = rates.get(currency, 1.0)

        amount = float(order.get("amount") or 0)
        created = parse_time(order.get("created_at") or order.get("date"))
        updated = parse_time(order.get("updated_at"))

        date_str = created.strftime("%Y-%m-%d") if created else ""

        prepared.append((
            str(order.get("id") or order.get("order_id") or "").strip(),
            str(order.get("customer") or order.get("customer_id") or "").strip(),
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
        INSERT INTO orders VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
    """, prepared)

    connection.commit()
    GLOBAL_DB = connection
    DB_INITIALIZED = True


# Pre-initialize DB on app launch
try:
    if START_URL:
        init_database()
except Exception as err:
    print("Database initialization deferred:", err)


# --------------------------------------------------
# ULTRA-FAST LLM SQL GENERATION
# --------------------------------------------------

SYSTEM_PROMPT = (
    "Return ONLY valid SQLite SELECT query. "
    "Table: orders(id, customer, region, product, qty, unit_price, amount, currency, status, created_at, updated_at, date_str, amount_usd). "
    "Rules: Use status='paid' for revenue/sales. For USD earnings use SUM(amount_usd). Filter dates like date_str LIKE 'YYYY-MM%'. "
    "For 'Which product...' return ONLY product name (e.g., SELECT product FROM orders ... GROUP BY product ORDER BY SUM(amount_usd) DESC LIMIT 1). "
    "Use LOWER() for string equality. No markdown."
)


def generate_sql(question):
    # Cache check for instant response
    question_key = question.strip().lower()
    if question_key in SQL_CACHE:
        return SQL_CACHE[question_key]

    if not AI_TOKEN:
        raise RuntimeError("AI PIPE API Token missing.")

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
        timeout=5,
    )

    response.raise_for_status()
    payload = response.json()
    sql = payload["choices"][0]["message"]["content"]
    clean_sql = normalize_sql(sql)

    # Save to cache
    SQL_CACHE[question_key] = clean_sql
    return clean_sql


# --------------------------------------------------
# QUESTION EXECUTION
# --------------------------------------------------

def answer_question(question: str):
    if not isinstance(question, str) or not question.strip():
        raise ValueError("Please provide a question.")

    init_database()
    sql = generate_sql(question)

    allowed_actions = {
        sqlite3.SQLITE_SELECT,
        sqlite3.SQLITE_READ,
        sqlite3.SQLITE_FUNCTION,
        getattr(sqlite3, "SQLITE_RECURSIVE", -1),
    }

    def authorize(action, arg1, arg2, database, trigger):
        if action not in allowed_actions:
            return sqlite3.SQLITE_DENY
        if action == sqlite3.SQLITE_READ and arg1 != "orders":
            return sqlite3.SQLITE_DENY
        return sqlite3.SQLITE_OK

    GLOBAL_DB.set_authorizer(authorize)

    try:
        cursor = GLOBAL_DB.execute(sql)
        rows = cursor.fetchall()

        if not rows:
            return 0

        # Scalar Response (Single Number or Value)
        if len(rows) == 1 and len(rows[0]) == 1:
            val = rows[0][0]
            if val is None:
                return 0
            if isinstance(val, float):
                return round(val, 2)
            return val

        # Top Entity String Result (e.g. "Mixer")
        if len(rows) == 1:
            row_dict = dict(rows[0])
            if len(row_dict) == 1:
                val = list(row_dict.values())[0]
                return round(val, 2) if isinstance(val, float) else val

        return [dict(row) for row in rows]

    finally:
        GLOBAL_DB.set_authorizer(None)