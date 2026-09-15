"""
Scan a folder of order-export CSVs and:
  1. Report, per Source Email, whether they have a successful ("ordered")
     order, a cancelled order, or both - and which product(s) they
     successfully ordered / had cancelled.
  2. Tag every individual order row with "Account Age": New if that order
     is the earliest order on record for that Source Email, Old if the
     account already had an earlier-dated order before this one.

Usage:
    python analyze_orders.py [csv_folder]

Defaults: csv_folder="csv"

Uses the Date, Status, Source Email, and Product columns; other columns
(Order #, Retailer, etc.) are ignored. Files may have different column
sets/order as long as they all include Status and Source Email; Product
is optional (left blank if the file doesn't have it).

The parsing/aggregation rules live in order_analysis.py, shared with the
Order Ledger web app (app.py) so both tools always agree.

Outputs (written next to this script):
    order_status_by_email.csv   - one row per email: success/cancel counts
                                   and the product(s) ordered/cancelled
    order_details_with_age.csv  - one row per order: Date, Status, Source
                                   Email, Product, Account Age
"""

import csv
import sys
from pathlib import Path

from order_analysis import SUCCESS_STATUSES, CANCEL_STATUSES, aggregate, rows_from_csv_text


def main():
    csv_dir = Path(sys.argv[1]) if len(sys.argv) > 1 else Path("csv")
    summary_output = Path("order_status_by_email.csv")
    detail_output = Path("order_details_with_age.csv")

    if not csv_dir.is_dir():
        print(f"CSV folder not found: {csv_dir}")
        sys.exit(1)

    csv_files = sorted(csv_dir.glob("*.csv"))
    if not csv_files:
        print(f"No CSV files found in {csv_dir}")
        sys.exit(1)

    rows = []
    for file in csv_files:
        text = file.read_text(encoding="utf-8-sig")
        file_rows, warning = rows_from_csv_text(file.name, text)
        if warning:
            print(f"Skipping {file.name}: {warning}")
            continue
        rows.extend(file_rows)

    records = aggregate(rows)  # also mutates rows in place with date_key/account_age
    records_by_email = {r["email"]: r for r in records}

    success_only = sorted(r["email"] for r in records if r["category"] == "success")
    cancelled_only = sorted(r["email"] for r in records if r["category"] == "cancelled")
    both = sorted(r["email"] for r in records if r["category"] == "both")
    good = sorted(r["email"] for r in records if r["good_account"])

    print(f"Scanned {len(csv_files)} file(s), {len(rows)} order row(s), {len(records)} unique email(s)\n")

    def products_str(products):
        return "; ".join(products)

    print(f"=== Success only ({len(success_only)}) ===")
    for e in success_only:
        products = products_str(records_by_email[e]["success_products"])
        print(f"{e}  [{products}]" if products else e)

    print(f"\n=== Cancelled only ({len(cancelled_only)}) ===")
    for e in cancelled_only:
        print(e)

    print(f"\n=== Both success and cancelled ({len(both)}) ===")
    for e in both:
        products = products_str(records_by_email[e]["success_products"])
        print(f"{e}  [success: {products}]" if products else e)

    print(f"\n=== Good accounts ({len(good)}) ===")
    print("(3+ consecutive successful orders, or multiple successful orders within a month)")
    for e in good:
        print(f"{e}  [{'; '.join(records_by_email[e]['good_reasons'])}]")

    with summary_output.open("w", newline="", encoding="utf-8") as f:
        writer = csv.writer(f)
        writer.writerow([
            "Source Email", "Success Count", "Cancelled Count",
            "Success Dates", "Cancelled Dates",
            "Success Products", "Cancelled Products", "Retailers",
            "Good Account", "Good Account Reason",
        ])
        for email in sorted(records_by_email):
            rec = records_by_email[email]
            success_dates = [r["date_raw"] for r in rows if r["email"] == email and r["status"] in SUCCESS_STATUSES]
            cancel_dates = [r["date_raw"] for r in rows if r["email"] == email and r["status"] in CANCEL_STATUSES]
            writer.writerow([
                email,
                rec["success_count"],
                rec["cancel_count"],
                "; ".join(success_dates),
                "; ".join(cancel_dates),
                products_str(rec["success_products"]),
                products_str(rec["cancel_products"]),
                products_str(rec["retailers"]),
                "Yes" if rec["good_account"] else "No",
                "; ".join(rec["good_reasons"]),
            ])

    rows_sorted = sorted(rows, key=lambda r: (r["email"], r["date_raw"]))
    with detail_output.open("w", newline="", encoding="utf-8") as f:
        writer = csv.writer(f)
        writer.writerow(["Date", "Status", "Source Email", "Retailer", "Product", "Account Age"])
        for r in rows_sorted:
            writer.writerow([r["date_raw"], r["status"], r["email"], r["retailer"], r["product"], r["account_age"]])

    print(f"\nPer-email summary written to {summary_output}")
    print(f"Per-order detail (with Account Age) written to {detail_output}")


if __name__ == "__main__":
    main()
