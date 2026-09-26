"""
Read a profile-export CSV and split its rows into two new CSVs: rows whose
Email Address is in a list you give it ("matches"), and every other row
("rest"). Both keep all original columns.

Usage:
    python backend/profile_finder/filter.py --csv profiles.csv --emails emails.txt
    python backend/profile_finder/filter.py --csv profiles.csv --emails emails.txt --out matches.csv --out-rest rest.csv

--emails points to a text file with one email per line (commas also work).
No UI yet - this is the plain script, run it directly to check the logic.
"""
import argparse
import csv
import io
import sys
from pathlib import Path

EMAIL_COLUMN_CANDIDATES = ["email address", "email", "source email", "e-mail"]


def find_email_column(fieldnames):
    norm = {(fn or "").strip().lower(): fn for fn in fieldnames}
    for candidate in EMAIL_COLUMN_CANDIDATES:
        if candidate in norm:
            return norm[candidate]
    return None


def parse_wanted_emails(text):
    """Emails to look for, as {lowercased: original spelling}. Order of
    first appearance is kept so the "not found" report reads naturally.
    Pure (no file access) so the web UI can reuse it on pasted text."""
    wanted = {}
    for chunk in (text or "").replace(",", "\n").replace(";", "\n").splitlines():
        email = chunk.strip()
        if not email:
            continue
        key = email.lower()
        if key not in wanted:
            wanted[key] = email
    return wanted


def load_wanted_emails(path):
    return parse_wanted_emails(Path(path).read_text(encoding="utf-8-sig"))


def filter_rows(csv_text, wanted):
    """Reads csv_text once and returns (fieldnames, matched_rows, rest_rows,
    seen_counts). Every row goes to exactly one of matched_rows/rest_rows,
    in the file's original order - no re-sorting, no re-grouping, so a row
    can never appear more than once, or in both outputs.
    Pure (no file access) so the web UI hits the exact same code as the CLI."""
    reader = csv.DictReader(io.StringIO(csv_text))
    fieldnames = reader.fieldnames or []
    email_col = find_email_column(fieldnames)
    if email_col is None:
        raise ValueError(f"No email column found. Columns present: {fieldnames}")

    matched_rows = []
    rest_rows = []
    seen_counts = {}  # lowercased email -> how many rows in the SOURCE file matched it
    for row in reader:
        email = (row.get(email_col) or "").strip()
        key = email.lower()
        if email and key in wanted:
            matched_rows.append(row)
            seen_counts[key] = seen_counts.get(key, 0) + 1
        else:
            rest_rows.append(row)

    return fieldnames, matched_rows, rest_rows, seen_counts


def filter_csv(csv_path, wanted):
    text = Path(csv_path).read_text(encoding="utf-8-sig")
    return filter_rows(text, wanted)


def rows_to_csv_text(fieldnames, rows):
    buf = io.StringIO()
    writer = csv.DictWriter(buf, fieldnames=fieldnames)
    writer.writeheader()
    writer.writerows(rows)
    return buf.getvalue()


def write_csv(out_path, fieldnames, rows):
    Path(out_path).write_text(rows_to_csv_text(fieldnames, rows), encoding="utf-8", newline="")


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--csv", required=True, help="the profile-export CSV to filter")
    ap.add_argument("--emails", required=True, help="text file with one email per line")
    ap.add_argument("--out", help="matches CSV path (default: <csv>-matches.csv next to the input)")
    ap.add_argument("--out-rest", help="everything-else CSV path (default: <csv>-rest.csv next to the input)")
    args = ap.parse_args()

    csv_path = Path(args.csv)
    if not csv_path.exists():
        print(f"CSV not found: {csv_path}")
        sys.exit(1)
    emails_path = Path(args.emails)
    if not emails_path.exists():
        print(f"Emails file not found: {emails_path}")
        sys.exit(1)

    wanted = load_wanted_emails(emails_path)
    if not wanted:
        print("No emails found in --emails file.")
        sys.exit(1)

    fieldnames, matched_rows, rest_rows, seen_counts = filter_csv(csv_path, wanted)

    out_path = Path(args.out) if args.out else csv_path.with_name(csv_path.stem + "-success.csv")
    rest_path = Path(args.out_rest) if args.out_rest else csv_path.with_name(csv_path.stem + "-unsuccess.csv")
    write_csv(out_path, fieldnames, matched_rows)
    write_csv(rest_path, fieldnames, rest_rows)

    found = [key for key in wanted if key in seen_counts]
    not_found = [original for key, original in wanted.items() if key not in seen_counts]
    duplicated = [(wanted[key], count) for key, count in seen_counts.items() if count > 1]

    print(f"Emails listed:   {len(wanted)}")
    print(f"Found:           {len(found)}")
    print(f"Not found:       {len(not_found)}")
    print(f"Matches written: {len(matched_rows)}  ->  {out_path}")
    print(f"Rest written:    {len(rest_rows)}  ->  {rest_path}")
    if duplicated:
        print(f"\n{len(duplicated)} email(s) matched more than one row in the CSV:")
        for email, count in duplicated:
            print(f"  {email}: {count} rows")
    if not_found:
        print(f"\nNot found in {csv_path.name}:")
        for email in not_found:
            print(f"  {email}")


if __name__ == "__main__":
    main()
