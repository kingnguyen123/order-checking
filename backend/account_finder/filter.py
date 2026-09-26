"""
Read a full account list ("email:password", one per line) and split it into
two new text files: lines whose email is in a list you give it ("matches"),
and every other line ("rest"). Lines are copied through exactly as they were
- nothing is re-parsed or reformatted, so passwords are never touched.

Usage:
    python backend/account_finder/filter.py --accounts accounts.txt --emails emails.txt
    python backend/account_finder/filter.py --accounts accounts.txt --emails emails.txt --out matches.txt --out-rest rest.txt

--emails points to a text file with one email per line (commas also work;
lines can also be full "email:password" - only the email part is used).
No UI yet - this is the plain script, run it directly to check the logic.
"""
import argparse
import sys
from pathlib import Path


def line_email(line):
    """The email part of an "email:password" line, or None if the line
    doesn't have a colon (so it can't be matched, and goes to "rest")."""
    if ":" not in line:
        return None
    return line.split(":", 1)[0].strip() or None


def parse_wanted_emails(text):
    """Emails to look for, as {lowercased: original spelling}. Order of
    first appearance is kept so the "not found" report reads naturally.
    Each entry can be a bare email or a full "email:password" line - only
    the email part is kept either way. Pure (no file access) so the web UI
    can reuse it on pasted text."""
    wanted = {}
    for chunk in (text or "").replace(",", "\n").replace(";", "\n").splitlines():
        item = chunk.strip()
        if not item:
            continue
        email = item.split(":", 1)[0].strip() if ":" in item else item
        if not email:
            continue
        key = email.lower()
        if key not in wanted:
            wanted[key] = email
    return wanted


def load_wanted_emails(path):
    return parse_wanted_emails(Path(path).read_text(encoding="utf-8-sig"))


def filter_lines(accounts_text, wanted):
    """Reads accounts_text once and returns (matched_lines, rest_lines,
    seen_counts). Every non-blank line goes to exactly one of
    matched_lines/rest_lines, in the file's original order - no re-sorting,
    no re-grouping, so a line can never appear more than once, or in both
    outputs. Pure (no file access) so the web UI hits the exact same code
    as the CLI."""
    matched_lines = []
    rest_lines = []
    seen_counts = {}  # lowercased email -> how many lines in the SOURCE file matched it
    for raw_line in (accounts_text or "").splitlines():
        line = raw_line.strip()
        if not line:
            continue
        email = line_email(line)
        key = (email or "").lower()
        if email and key in wanted:
            matched_lines.append(line)
            seen_counts[key] = seen_counts.get(key, 0) + 1
        else:
            rest_lines.append(line)

    return matched_lines, rest_lines, seen_counts


def filter_accounts_file(accounts_path, wanted):
    text = Path(accounts_path).read_text(encoding="utf-8-sig")
    return filter_lines(text, wanted)


def lines_to_text(lines):
    return "\n".join(lines) + ("\n" if lines else "")


def write_lines(out_path, lines):
    Path(out_path).write_text(lines_to_text(lines), encoding="utf-8", newline="")


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--accounts", required=True, help="the full email:password account list to split")
    ap.add_argument("--emails", required=True, help="text file with one email per line")
    ap.add_argument("--out", help="matches output path (default: <accounts>-matches.txt next to the input)")
    ap.add_argument("--out-rest", help="everything-else output path (default: <accounts>-rest.txt next to the input)")
    args = ap.parse_args()

    accounts_path = Path(args.accounts)
    if not accounts_path.exists():
        print(f"Accounts file not found: {accounts_path}")
        sys.exit(1)
    emails_path = Path(args.emails)
    if not emails_path.exists():
        print(f"Emails file not found: {emails_path}")
        sys.exit(1)

    wanted = load_wanted_emails(emails_path)
    if not wanted:
        print("No emails found in --emails file.")
        sys.exit(1)

    matched_lines, rest_lines, seen_counts = filter_accounts_file(accounts_path, wanted)

    out_path = Path(args.out) if args.out else accounts_path.with_name(accounts_path.stem + "-success.txt")
    rest_path = Path(args.out_rest) if args.out_rest else accounts_path.with_name(accounts_path.stem + "-unsuccess.txt")
    write_lines(out_path, matched_lines)
    write_lines(rest_path, rest_lines)

    found = [key for key in wanted if key in seen_counts]
    not_found = [original for key, original in wanted.items() if key not in seen_counts]
    duplicated = [(wanted[key], count) for key, count in seen_counts.items() if count > 1]

    print(f"Emails listed:   {len(wanted)}")
    print(f"Found:           {len(found)}")
    print(f"Not found:       {len(not_found)}")
    print(f"Matches written: {len(matched_lines)}  ->  {out_path}")
    print(f"Rest written:    {len(rest_lines)}  ->  {rest_path}")
    if duplicated:
        print(f"\n{len(duplicated)} email(s) matched more than one line in the accounts file:")
        for email, count in duplicated:
            print(f"  {email}: {count} lines")
    if not_found:
        print(f"\nNot found in {accounts_path.name}:")
        for email in not_found:
            print(f"  {email}")


if __name__ == "__main__":
    main()
