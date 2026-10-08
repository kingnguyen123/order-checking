# Rewrite Guide: Profile Finder

Read `rewrite-00-overview.md` first for shared conventions.

## What it does

You give it one profile-export CSV (columns like `Email Address, Profile Name, Card Number, Shipping Address, ...`) and a list of emails. It splits the CSV into two new CSVs: rows whose email is in your list (**matches**), and every other row (**rest**) — every original column preserved untouched, on both outputs.

This tool is the simplest one in the codebase and the cleanest example of the "one implementation, both entry points call it" pattern — copy this pattern for the other tools where you can.

## File: `backend/profile_finder/filter.py`

No Discord, no SQLite, no HTTP imports at the top of the file — it's a self-contained script that's *also* imported by `app.py`.

```python
EMAIL_COLUMN_CANDIDATES = ["email address", "email", "source email", "e-mail"]

def find_email_column(fieldnames):
    norm = {(fn or "").strip().lower(): fn for fn in fieldnames}
    for candidate in EMAIL_COLUMN_CANDIDATES:
        if candidate in norm:
            return norm[candidate]
    return None
```
First candidate that matches (case-insensitive) wins; returns the *original* header spelling so the output CSV's header is untouched.

```python
def parse_wanted_emails(text):
    wanted = {}
    for chunk in (text or "").replace(",", "\n").replace(";", "\n").splitlines():
        email = chunk.strip()
        if not email:
            continue
        key = email.lower()
        if key not in wanted:
            wanted[key] = email
    return wanted
```
Accepts newline-, comma-, or semicolon-separated input (any mix). Returns `{lowercased: original spelling}` — the lowercase key is the matching key; the original spelling is kept only so the "not found" report can echo back what the user actually typed. First-seen spelling wins on a duplicate. **Pure** — no file I/O — specifically so the same function works on a pasted textarea value from the web UI and on a file's contents from the CLI.

```python
def load_wanted_emails(path):
    return parse_wanted_emails(Path(path).read_text(encoding="utf-8-sig"))
```
`utf-8-sig` — strips a BOM if the emails file was saved by something like Notepad/Excel that adds one.

```python
def filter_rows(csv_text, wanted):
    reader = csv.DictReader(io.StringIO(csv_text))
    fieldnames = reader.fieldnames or []
    email_col = find_email_column(fieldnames)
    if email_col is None:
        raise ValueError(f"No email column found. Columns present: {fieldnames}")

    matched_rows, rest_rows, seen_counts = [], [], {}
    for row in reader:
        email = (row.get(email_col) or "").strip()
        key = email.lower()
        if email and key in wanted:
            matched_rows.append(row)
            seen_counts[key] = seen_counts.get(key, 0) + 1
        else:
            rest_rows.append(row)
    return fieldnames, matched_rows, rest_rows, seen_counts
```
**Every row goes to exactly one of the two output lists** — a row can never be dropped, never appear in both, never appear twice. A row with a blank email goes to `rest_rows`, not nowhere (an earlier draft dropped blank-email rows silently; that was a real bug fix — don't reintroduce the drop). `seen_counts` tracks how many source rows matched each wanted email, purely for the "N emails matched more than one row" report — it does **not** affect what gets written; duplicates are a report, never a dedup.

```python
def rows_to_csv_text(fieldnames, rows):
    buf = io.StringIO()
    writer = csv.DictWriter(buf, fieldnames=fieldnames)
    writer.writeheader()
    writer.writerows(rows)
    return buf.getvalue()
```
Round-trips through the *original* `fieldnames` list — column order in the output CSVs exactly matches the input, always.

## CLI

```
python backend/profile_finder/filter.py --csv profiles.csv --emails emails.txt
python backend/profile_finder/filter.py --csv profiles.csv --emails emails.txt --out matches.csv --out-rest rest.csv
```
`--csv` and `--emails` required. `--out`/`--out-rest` default to `<csv-stem>-success.csv` / `<csv-stem>-unsuccess.csv` next to the input (these default names were deliberately renamed at some point from an earlier `-matches`/`-rest` naming — check the current file for whatever they're named as of your rewrite date, that's a naming decision, not a bug). Prints:
```
Emails listed:   N
Found:           N
Not found:       N
Matches written: N  ->  <path>
Rest written:    N  ->  <path>

<N> email(s) matched more than one row in the CSV:
  <email>: <count> rows
Not found in <csv>:
  <email>
```

## API: `POST /api/profile-finder/filter`

Request:
```json
{"csvText": "<raw csv text>", "emails": "<raw textarea text, any of newline/comma/semicolon separated>"}
```
Response:
```json
{
  "fieldnames": ["Email Address", "Profile Name", ...],
  "rows": [{"Email Address": "a@x.com", ...}, ...],
  "emailCount": 4, "foundCount": 3, "notFound": ["notfound@x.com"],
  "duplicated": [{"email": "a@x.com", "count": 2}],
  "matchedCsvText": "<full csv text>", "restCsvText": "<full csv text>", "restCount": 1
}
```
`rows` is the **matched** rows only (full objects, keyed by original column name) — used for the on-screen preview table. `matchedCsvText`/`restCsvText` are the pre-built downloadable CSV strings for the two export buttons.

**This endpoint is fully stateless** — it never writes to disk, never touches the SQLite database used by the Discord tool. The CSV format this tool handles routinely contains card numbers, CVVs, and billing addresses; statelessness here is a deliberate privacy choice, not something to "improve" toward persistence.

## Things worth reconsidering in the rewrite

- **Nothing structurally wrong here** — this is the tool to use as the template for how the others *should* work (one implementation, CLI and API both call it directly, zero duplicated logic).
- Consider whether `seen_counts`/`duplicated` should distinguish "same email appears on 2 rows with identical data" (probably a genuine export duplicate, harmless) from "same email appears on 2 rows with *different* data" (worth flagging louder) — today both report identically.
- If you ever need to handle CSVs too large to comfortably hold in memory twice (once as `matched_rows`, once as the rebuilt CSV text), consider streaming the split directly to two output buffers instead of collecting full row-dict lists first. Not a real problem at the data sizes this tool has seen.
