# Rewrite Guide: Account Analysis (order_analysis)

Read `rewrite-00-overview.md` first for shared conventions.

## What it does

You drop one or more order-export CSVs. The tool groups every order row by **Source Email** into one summary per account: how many succeeded, how many were cancelled, which product(s), which retailer(s), which address(es), whether the first-ever order was "new" or the account already existed, and whether the account counts as "good" (a pattern of repeat successes).

## Files

- `backend/order_analysis/order_analysis.py` — pure parsing/aggregation, shared by the CLI and (partially — see gotcha below) the web app.
- `backend/order_analysis/analyze_orders.py` — CLI: scans a folder of CSVs, writes two summary CSVs.
- `app.py`'s `_handle_analyze` — the web endpoint. Thin: parses each uploaded file's text into rows and returns them raw. **No aggregation happens server-side.**

## Constants

```python
SUCCESS_STATUSES = {"ordered", "success", "completed", "shipped", "delivered"}
CANCEL_STATUSES = {"cancelled", "canceled", "cancel"}
```
Matched against the **lowercased** status string. Anything not in either set (e.g. "Your card was declined") counts toward neither success nor cancel totals, but the row still exists and still shows in per-order detail.

```python
DATE_FORMATS = ["%Y-%m-%d", "%m/%d/%Y", "%m/%d/%y", "%Y/%m/%d", "%d-%m-%Y"]
GOOD_STREAK_LENGTH = 3   # 3+ consecutive successes counts as "good"
GOOD_CLUSTER_DAYS = 30   # 2+ successes within 30 days counts as "good"
```

## Column detection

`find_col(fieldnames, *candidates)` — normalizes every header to `strip().lower()`, then returns the original header string for the first candidate that matches. Case-insensitive, order-independent (CSVs from different exports can have columns in any order).

Columns used, and their candidate header names:
| Field | Candidates | Required? |
|---|---|---|
| status | `"status"` | yes |
| email | `"source email"` | yes |
| date | `"date"` | no |
| product | `"product"` | no |
| retailer | `"retailer"` | no |
| address | `"address"`, `"shipping address"` (first match wins) | no |

If Status or Source Email is missing, the whole file is rejected with a warning (`"Missing Status and/or Source Email column"`) rather than partially processed. An empty file (no header row at all) gets `"Empty file"`. Rows with a blank email value are silently skipped (not counted, no warning) — only non-empty-email rows become real order rows.

## Normalization (applied at parse time, before anything else sees the value)

**`normalize_product(raw)`** — drops a leading `"Category: "` prefix some exports include:
```
"Pokemon Trading Card Game: 30th Celebration Poster Collection" -> "30th Celebration Poster Collection"
```
Implementation: strip, then if `":"` is present, take everything after the *first* colon, stripped. No colon → returned unchanged.

**`normalize_address(raw)`** — the Address column holds `"Name, Street, City, State Zip"` as one comma-separated value; only the street matters here:
```
"Mary Davis, 3146 N Wild Rose St Rm 43, Wichita, KS 67226" -> "3146 N Wild Rose St Rm 43"
```
Implementation, by comma-segment count after `strip()`-ing each segment:
- `>= 4` segments → drop the first (name) and last two (city, "state zip") → `parts[1:-2]`
- `== 3` segments → assume `Name, Street, "City State Zip"` (the last two got merged with no comma between them) → `parts[1:-1]`
- `== 2` segments → assume `Name, Street` with no city/state/zip present at all → `parts[1:]`
- `<= 1` segment → can't safely identify a name to drop → returned unchanged (best-effort fallback, never guess when ambiguous)

Both normalizers are duplicated as plain JavaScript in `frontend/index.html` (`normalizeProduct`/`normalizeAddress`) for the client-side fallback parser used when the Python backend isn't reachable. Keep them in exact lockstep if you keep this fallback path in the rewrite — or better, delete the fallback path and require the backend (see "what to reconsider" below).

## `rows_from_csv_text(name, text)` → `(rows, warning)`

One row dict per non-blank-email CSV row:
```python
{
    "file": name, "email": str, "status": lowercased, "status_raw": original casing,
    "date_raw": str, "product": normalized, "retailer": str, "address": normalized,
}
```

## `date_key(date_str)` — sortable string key for a date

ISO-prefixed strings (`^\d{4}-\d{2}-\d{2}`) pass through unchanged (already sort correctly as text). Otherwise tries each format in `DATE_FORMATS` via `strptime`, returning `.isoformat()` of the first that parses. If nothing parses, returns the raw string as-is (a non-crashing fallback — it'll sort weirdly, but nothing breaks).

## `aggregate(rows)` — Python, CLI-only (see gotcha)

Mutates each row in place to add:
- `date_key` (via the function above, or `None` if `date_raw` is empty)
- `account_age`: `"unknown"` if no date_key; `"new"` if this row's `date_key` equals that email's **minimum** `date_key` across all its rows; `"old"` otherwise. **Tie behavior:** if two rows for the same email share the exact earliest `date_key`, *both* are `"new"` — this is an equality check, not a "pick the first one" check.

Then groups by email into:
```python
{
    "email": str, "success_count": int, "cancel_count": int,
    "category": "both" | "success" | "cancelled" | "none",
    "first_date": str | None,
    "success_products": [str, ...], "cancel_products": [str, ...],   # unique, insertion order
    "retailers": [str, ...], "addresses": [str, ...],                 # unique, insertion order
    "good_account": bool, "good_reasons": [str, ...],
}
```
`category` logic: `"both"` if success_count>0 AND cancel_count>0; else `"success"` if success_count>0; else `"cancelled"` if cancel_count>0; else `"none"`.

**`_good_account(orders)`** — only orders with a real `date_key` participate (undated orders can't prove a streak or a cluster). Two independent conditions, either one qualifies:
- **Streak**: sort dated orders by `date_key`; walk in order counting consecutive successes; resets to 0 on anything else; qualifies if the best streak seen is `>= GOOD_STREAK_LENGTH` (3).
- **Cluster**: collect all success dates (real `datetime`s, via `_date_from_key`, which only accepts a strict `%Y-%m-%d` prefix); sort; qualifies if any adjacent pair is `<= GOOD_CLUSTER_DAYS` (30) days apart.

Returns `(is_good, reasons)` where `reasons` is a list of human-readable strings for whichever condition(s) matched (both can match simultaneously).

## ⚠️ The duplication gotcha

**This `aggregate()` function is only called by `analyze_orders.py` (the CLI).** The actual web page does NOT call it — `/api/analyze` returns raw per-row data only, and `frontend/index.html`'s `aggregateRows()` (plain JS) re-implements the *entire* aggregation above from scratch, including its own copies of `normalize_product`/`normalize_address`/`computeGoodAccount`.

Why it's built this way: the web UI needs to re-aggregate instantly whenever the user changes the retailer filter or address search (both **re-scope** which rows count, not just which accounts are *displayed* — see below), and a network round-trip per keystroke/filter-change would feel slow.

This has already drifted once: the JS version has two extra per-account flags (`hasNewSuccess`, `hasRepeatSuccess` — see below) that the Python version has never gained, because nothing forced them to be added in both places at once.

**For the rewrite, make a deliberate choice** instead of copying this by accident:
1. **Server-side aggregation always** — the API accepts the current retailer/address/etc. scoping as parameters and returns pre-aggregated records; the frontend re-requests on every filter change. Simpler backend (one implementation), more network chatter, and requires the API to grow query parameters for every filter that needs to re-scope aggregation (not just ones that filter the *display*).
2. **Client-side aggregation always**, raw rows only ever fetched once — commit to this being the only implementation (React can absolutely own this logic), and either delete `aggregate()`/`analyze_orders.py` or accept they're a second, deliberately-maintained implementation with a **golden test**: one fixture CSV, one expected aggregated-output fixture, asserted identical from both a Python test and a JS/TS test. That test is what would have caught the `hasNewSuccess` drift immediately instead of silently.

Either is defensible. Picking neither (i.e. repeating today's setup without the golden test) is the one option to avoid.

## `hasNewSuccess` / `hasRepeatSuccess` (currently JS-only — not in Python's `aggregate()`)

Two extra per-account booleans computed in the web UI, used for the "Success New" / "Success Repeat" filter chips:
- `hasNewSuccess` — `true` if **any** of the account's orders is both a success AND `accountAge === "new"` (its first-ever order succeeded).
- `hasRepeatSuccess` — `true` if any order is both a success AND `accountAge === "old"` (a later, non-first order succeeded).

These are **not mutually exclusive** — an account whose first order succeeded *and* whose third order also succeeded is `true` for both. If you port this server-side, compute it in the same per-row pass that already computes `account_age` and success/cancel counts.

## API: `POST /api/analyze`

Request:
```json
{"files": [{"name": "export.csv", "text": "<raw csv text>"}]}
```
The CSV text is read client-side (`FileReader`), never touches disk server-side.

Response:
```json
{
  "files": [
    {
      "name": "export.csv", "count": 42, "warning": null,
      "rows": [
        {"email": "a@x.com", "status": "ordered", "statusRaw": "Ordered",
         "dateRaw": "2026-09-16", "product": "Poster", "retailer": "Target", "address": "3146 N Wild Rose St"}
      ]
    }
  ]
}
```
`warning` is `null` on success, or the string described above — when present, `rows` is empty for that file and the whole file is excluded from any aggregation.

## Client-side scoping filters (define the API contract if you move aggregation server-side)

Two filters currently re-scope the *entire* aggregation, not just which resulting accounts are shown — meaning they change success/cancel counts, product lists, and address lists too, because they operate on the **raw rows before aggregation**:
- **Retailer dropdown** — keeps only rows where `row.retailer === selectedRetailer`.
- **Address search** — keeps only rows where `row.address` contains the search substring. This was a bug fix: it originally filtered *after* aggregation (deciding which accounts to show, while still displaying their *entire* unrelated order history/addresses) — a real user complaint ("other addresses tied to my search still show up"). Fixed by moving it to operate on raw rows, exactly like the retailer filter, so an account matched by address search shows *only* the matching order(s)/address(es), with counts recomputed from just that subset.

Everything else (category chips, "Good only" toggle, the plain email/product search box, sort) filters the *already-aggregated* account list and does not change counts.

## CLI: `analyze_orders.py`

```
python backend/order_analysis/analyze_orders.py [csv_folder]   # default folder: ./csv
```
Reads every `*.csv` in the folder, skips (with a printed reason) any that `rows_from_csv_text` rejects, calls `aggregate()`, and writes two files **next to the script**:

- `order_status_by_email.csv` — columns: `Source Email, Success Count, Cancelled Count, Success Dates, Cancelled Dates, Success Products, Cancelled Products, Retailers, Addresses, Good Account, Good Account Reason`. One row per email, sorted by email.
- `order_details_with_age.csv` — columns: `Date, Status, Source Email, Retailer, Product, Address, Account Age`. One row per order, sorted by `(email, date_raw)`.

Also prints a console summary: counts of success-only/cancelled-only/both/good accounts, with their product lists.

## Things worth reconsidering in the rewrite

- **Pick one aggregation implementation** (see gotcha above) — this is the single biggest cleanup opportunity in this tool.
- **The client-side CSV-parsing fallback** (`parseFilesLocally` in the current frontend, used when the Python backend can't be reached) duplicates *both* the CSV-column-detection logic and the normalizers a second time. If the React rewrite always assumes a running backend (reasonable for a local desktop tool), consider dropping this fallback path entirely rather than porting it — one less thing to keep in sync.
- **Date parsing** (`date_key`) silently falls back to sorting by the raw string when no format matches. Consider surfacing "N orders have an unparseable date" somewhere in the UI instead of letting them sort silently wrong.
- **Retailer/address scoping** happening via full re-aggregation on every keystroke is fine at the data sizes this tool has seen so far; if CSVs get large, consider debouncing the address-search input or moving that specific recompute to a memoized worker.
