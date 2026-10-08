# Order Ledger

A tool for analyzing bulk order-export CSVs (e.g. from Target, Walmart) to see, per customer email: which orders succeeded, which got cancelled, whether they're a new or repeat account, and whether they qualify as a "good account."

Two ways to use it:
- **CLI script** (`backend/order_analysis/analyze_orders.py`) — scans a folder of CSVs and writes summary/detail CSV reports.
- **Web app** (`backend/app.py` + `frontend/`) — a two-page local site: **Account Analysis** for CSV upload/analysis, and **Jigging Analysis** for the optional Discord order-tracking feature (see below). Both pages link to each other via a small nav bar at the top.

Both share the same parsing and classification rules, defined once in `backend/order_analysis/order_analysis.py`.

The project is organized into three top-level folders: **`backend/`** (all Python), **`frontend/`** (the two HTML pages), and **`database/`** (the local SQLite file for Discord data - created automatically, never committed).

## Quick start

**Web app** — double-click **`Run Order Ledger.bat`**, or run from the project root:
```
python backend/app.py
```
This starts a local server and opens your browser to it automatically. Drop your CSV files onto the page.

**CLI script** (run from the project root):
```
python backend/order_analysis/analyze_orders.py [csv_folder]
```
Defaults to a folder named `csv` in the project root. Writes `order_status_by_email.csv` and `order_details_with_age.csv` there too.

CSV analysis needs no extra packages — everything there uses only the Python standard library. The optional Discord Order Analysis feature (see below) needs a couple of packages; without them installed, Discord features simply stay disabled and CSV analysis works exactly the same.

## Expected CSV columns

Column matching is case-insensitive. Only **Status** and **Source Email** are required; the rest are optional and simply ignored if missing:

| Column | Required | Notes |
|---|---|---|
| Status | Yes | e.g. `ordered`, `cancelled` |
| Source Email | Yes | the account's email address |
| Date | No | used for account age and "good account" rules |
| Product | No | product name(s) per order |
| Retailer | No | e.g. `Target`, `Walmart` |

Recognized status values:
- **Success**: `ordered`, `success`, `completed`, `shipped`, `delivered`
- **Cancelled**: `cancelled`, `canceled`, `cancel`

## What gets computed, per account

- **Category** — `success` (success orders only), `cancelled` (cancelled only), `both`, or `none`.
- **Account Age** — for each individual order: `new` if it's that email's earliest order on record, `old` if the account already had an earlier order.
- **Good Account** — flagged when either:
  - it has 3 or more consecutive successful orders, or
  - it has 2 or more successful orders within 30 days of each other.
- **Products / Retailers** — the distinct product(s) and retailer(s) seen for that account.

## Web app features

- Drag-and-drop or click-to-browse CSV upload (parsed by the Python backend when running `backend/app.py`; falls back to parsing in-browser if no backend is reachable, e.g. when opened as a plain file).
- Search by email or product; filter by category, retailer, or "Good only."
- Sortable columns; click a row to expand and see every individual order for that account.
- **Copy Emails** — copies every currently-listed email to the clipboard.
- **Export CSV** — downloads the current filtered view as a summary CSV.

## Jigging Analysis (Discord, optional)

Reads order notifications that a checkout bot posts to Discord (embeds), and lets you search them by profile name. It lives on its own page, **Jigging Analysis** (nav bar, or `/discord.html`), because the account identifier there (the Discord "Profile") is different from a CSV's Source Email.

What it does:
- **Search** - type one or more profile keywords (comma-separated, partial names OK, or leave blank to search every profile), optionally pick a date range, and press Search. Search first fetches new messages from your selected Discord channels for that range, then shows what's been scanned this run, grouped by exact status. Status filter, keyword filter, channel filter, jig-name filter, per-status Copy, and a Jig Breakdown (jig name x status counts) are on the results. A search term that matched zero orders is flagged, so a typo can't silently hide real results.
- **Pick channels** - server > category > channel checkboxes, no channel IDs to type. Re-checked against what the bot can actually read every time it reconnects.
- **Live monitoring** - while connected, new orders in the selected channels are recorded as they arrive.
- **No database - orders live in memory for the life of the run.** Every time the app is started, scanning starts fresh; nothing from a previous run (including which channels were selected) is remembered. Re-scanning within the same run never duplicates a message (unique Discord message ID).
- Only the **Profile**, the embed **title** (the status), and (when present) a **Cancel Reason** field are ever read. Account and Proxy fields are never touched. Dates mean *your* local day. A "Drop Summary" message some bots post isn't an order and is skipped, not counted as unrecognized.

**Setup:** `pip install -r requirements.txt`, copy `.env.example` to `.env`, and set `DISCORD_TOKEN=your-bot-token`. The bot needs **View Channels** + **Read Message History**, and **Message Content Intent** turned on in the Discord Developer Portal. `python backend/app.py` starts the web app and the Discord connection together; without a token or the packages you get a one-line notice and everything else works.

**Never commit your real `.env` file** - it contains a secret token. `.gitignore` already excludes it.

**Tests** (offline - fake messages, an in-memory store, never touches Discord):
```
.venv\Scripts\python.exe -m unittest discover -s tests -v
```

## Project files

```
backend/
  app.py                   Web server (stdlib http.server): serves frontend/, JSON API
  order_analysis/          CSV parsing/classification (analyze_orders.py = command-line report)
  jigging_analysis/
    messages.py            Order/DateRange/OrderParser/OrderStore/OrderGrouper - no Discord, no database
    discord_client.py      the bot connection for the web app (background thread, live monitoring, scanning)
  profile_finder/          split a profile-export CSV by email list (filter.py = command-line script)
  account_finder/          split an email:password account list by email list (filter.py = command-line script)
frontend/
  index.html               Account Analysis page (CSV upload/analysis)
  discord.html             Jigging Analysis page
  profile-finder.html      Profile Finder page
  account-finder.html      Account Finder page
tests/                     Offline tests
docs/                      Build notes and rewrite guides
archive/                   Earlier version of the Discord code, kept for reference
csv/                       Default input folder for analyze_orders.py
Run Order Ledger.bat       Double-click launcher for backend/app.py (Windows)
```

## Desktop shortcut

A shortcut named **"Order Ledger"** (using `app_icon.ico`) can be placed on the Desktop, pointing at `Run Order Ledger.bat`. Don't move or delete `app_icon.ico` from the project folder, or the shortcut's icon will break.

## Notes

- CSV files are analyzed locally — either by the Python backend or directly in your browser. Nothing is uploaded anywhere.
- The `csv/` folder may contain real customer data (emails, order history) - avoid committing it or sharing it outside your own use.
