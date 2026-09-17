"""
Shared order-CSV analysis logic, used by both analyze_orders.py (CLI) and
app.py (the web backend for the Order Ledger frontend). Keeping this in one
module means the CLI tool and the web app always agree on what counts as a
successful/cancelled order and how "new" vs "old" accounts are determined.
"""

import csv
import io
import re
from datetime import datetime

SUCCESS_STATUSES = {"ordered", "success", "completed", "shipped", "delivered"}
CANCEL_STATUSES = {"cancelled", "canceled", "cancel"}

DATE_FORMATS = ["%Y-%m-%d", "%m/%d/%Y", "%m/%d/%y", "%Y/%m/%d", "%d-%m-%Y"]

ISO_DATE_PREFIX = re.compile(r"^\d{4}-\d{2}-\d{2}")


def normalize_header(h):
    return h.strip().lower()


def find_col(fieldnames, *candidates):
    norm = {normalize_header(f): f for f in fieldnames}
    for c in candidates:
        if c in norm:
            return norm[c]
    return None


def date_key(date_str):
    """Sortable/comparable string key for a date. ISO dates pass through
    as-is (they already sort correctly as text); other recognized formats
    are normalized to ISO; anything else falls back to the raw string."""
    if not date_str:
        return None
    if ISO_DATE_PREFIX.match(date_str):
        return date_str
    for fmt in DATE_FORMATS:
        try:
            return datetime.strptime(date_str, fmt).isoformat()
        except ValueError:
            continue
    return date_str


GOOD_STREAK_LENGTH = 3  # "more than two consecutive" successes
GOOD_CLUSTER_DAYS = 30  # "multiple success orders within one month"


def _date_from_key(key):
    """Best-effort datetime for a date_key string, for day-difference math.
    Returns None if the key isn't a recognizable date (e.g. a raw,
    unparsed fallback string)."""
    if not key:
        return None
    m = ISO_DATE_PREFIX.match(key)
    if not m:
        return None
    try:
        return datetime.strptime(key[:10], "%Y-%m-%d")
    except ValueError:
        return None


def _good_account(orders):
    """A "good" account has either a streak of GOOD_STREAK_LENGTH+
    consecutive successful orders, or two-or-more successful orders that
    landed within GOOD_CLUSTER_DAYS of each other. Only dated orders can
    take part (order/time can't be judged otherwise). Returns
    (is_good, reasons)."""
    dated = [o for o in orders if o["date_key"]]
    dated.sort(key=lambda o: o["date_key"])

    streak = 0
    best_streak = 0
    for o in dated:
        if o["status"] in SUCCESS_STATUSES:
            streak += 1
            best_streak = max(best_streak, streak)
        else:
            streak = 0
    has_streak = best_streak >= GOOD_STREAK_LENGTH

    success_dates = sorted(
        d for d in (_date_from_key(o["date_key"]) for o in dated if o["status"] in SUCCESS_STATUSES) if d
    )
    has_cluster = any(
        (success_dates[i + 1] - success_dates[i]).days <= GOOD_CLUSTER_DAYS
        for i in range(len(success_dates) - 1)
    )

    reasons = []
    if has_streak:
        reasons.append(f"{best_streak} consecutive successful orders")
    if has_cluster:
        reasons.append(f"multiple successful orders within {GOOD_CLUSTER_DAYS} days")
    return has_streak or has_cluster, reasons


def rows_from_csv_text(name, text):
    """Parse one CSV file's text into row dicts using its Status, Source
    Email, Date, Product, and Retailer columns (case-insensitive; Date,
    Product and Retailer are optional). Returns (rows, warning) - warning
    is None on success, or a short reason the file was skipped."""
    reader = csv.DictReader(io.StringIO(text))
    if not reader.fieldnames:
        return [], "Empty file"

    status_col = find_col(reader.fieldnames, "status")
    email_col = find_col(reader.fieldnames, "source email")
    date_col = find_col(reader.fieldnames, "date")
    product_col = find_col(reader.fieldnames, "product")
    retailer_col = find_col(reader.fieldnames, "retailer")

    if not status_col or not email_col:
        return [], "Missing Status and/or Source Email column"

    rows = []
    for row in reader:
        email = (row.get(email_col) or "").strip()
        if not email:
            continue
        status_raw = (row.get(status_col) or "").strip()
        rows.append({
            "file": name,
            "email": email,
            "status": status_raw.lower(),
            "status_raw": status_raw,
            "date_raw": (row.get(date_col) or "").strip() if date_col else "",
            "product": (row.get(product_col) or "").strip() if product_col else "",
            "retailer": (row.get(retailer_col) or "").strip() if retailer_col else "",
        })
    return rows, None


def aggregate(rows):
    """Mutates each row in place to add 'date_key' and 'account_age', then
    groups by email. Returns a list of per-email summary records."""
    for r in rows:
        r["date_key"] = date_key(r["date_raw"]) if r["date_raw"] else None

    first_date_by_email = {}
    for r in rows:
        if r["date_key"] is None:
            continue
        email = r["email"]
        if email not in first_date_by_email or r["date_key"] < first_date_by_email[email]:
            first_date_by_email[email] = r["date_key"]

    for r in rows:
        if r["date_key"] is None:
            r["account_age"] = "unknown"
        elif r["date_key"] == first_date_by_email.get(r["email"]):
            r["account_age"] = "new"
        else:
            r["account_age"] = "old"

    by_email = {}
    for r in rows:
        rec = by_email.setdefault(r["email"], {
            "email": r["email"], "orders": [], "success_count": 0, "cancel_count": 0,
            "success_products": {}, "cancel_products": {}, "retailers": {},
        })
        rec["orders"].append(r)
        if r["retailer"]:
            rec["retailers"][r["retailer"]] = True
        if r["status"] in SUCCESS_STATUSES:
            rec["success_count"] += 1
            if r["product"]:
                rec["success_products"][r["product"]] = True
        elif r["status"] in CANCEL_STATUSES:
            rec["cancel_count"] += 1
            if r["product"]:
                rec["cancel_products"][r["product"]] = True

    records = []
    for rec in by_email.values():
        sc, cc = rec["success_count"], rec["cancel_count"]
        if sc and cc:
            category = "both"
        elif sc:
            category = "success"
        elif cc:
            category = "cancelled"
        else:
            category = "none"
        is_good, good_reasons = _good_account(rec["orders"])
        records.append({
            "email": rec["email"],
            "success_count": sc,
            "cancel_count": cc,
            "category": category,
            "first_date": first_date_by_email.get(rec["email"]),
            "success_products": list(rec["success_products"].keys()),
            "cancel_products": list(rec["cancel_products"].keys()),
            "retailers": list(rec["retailers"].keys()),
            "good_account": is_good,
            "good_reasons": good_reasons,
        })
    return records
