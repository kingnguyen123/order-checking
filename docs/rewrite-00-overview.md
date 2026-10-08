# Rewrite Guide: Overview

You're about to rewrite this whole backend (frontend will become React). This doc is the map — read it first, then read the per-tool doc for whichever piece you're rebuilding:

- `rewrite-01-account-analysis.md` — CSV order analysis
- `rewrite-02-jigging-analysis.md` — Discord order tracking (the biggest one)
- `rewrite-03-profile-finder.md` — CSV email splitter
- `rewrite-04-account-finder.md` — `email:password` list splitter

Each doc describes the *current* implementation precisely enough to rebuild from, plus the real bugs that were found and fixed in it (so you don't reintroduce them) and some concrete ideas for what to do better this time.

## What this app is

A local, single-user desktop tool (not a hosted service) for analyzing order-checkout data and Discord order notifications. Everything runs on `127.0.0.1`, started by double-clicking a `.bat` file. There is no auth, no multi-user concern, no deployment target other than "the user's own Windows machine." Keep that framing — a lot of the current design (stdlib-only HTTP server, SQLite file, no build step) is a direct consequence of "must run with nothing but a Python install."

## Current architecture

```
backend/
  app.py                    <- one http.server.ThreadingHTTPServer, routes by exact path string
  order_analysis/
    order_analysis.py        <- pure parsing/aggregation logic
    analyze_orders.py         <- CLI wrapper (folder of CSVs -> summary CSVs)
  jigging_analysis/
    parser.py                <- pure: Discord message -> (profile, status)
    dates.py                 <- pure: local-day string -> UTC range
    analysis.py              <- pure: grouping/search over fetched rows
    storage.py                <- the only module that touches SQLite
    scan.py                   <- library + CLI: drives channel.history() through parser+storage
    service.py                 <- the live bot: background thread, event loop, HTTP-facing API
  profile_finder/
    filter.py                 <- pure filter logic + CLI, shared by the web endpoint
  account_finder/
    filter.py                 <- same idea, for email:password lists

frontend/                   <- today: 4 static HTML+React-via-CDN pages, no build step
  index.html                 <- Account Analysis
  discord.html                <- Jigging Analysis
  profile-finder.html
  account-finder.html

database/order_ledger.db    <- SQLite, used ONLY by jigging_analysis
tests/                       <- unittest, run with `python -m unittest discover -s tests`
```

Each tool folder under `backend/` is independent — none of them import each other. `app.py` is the only place that imports from all of them and wires HTTP routes to their functions.

## The HTTP layer convention (app.py)

- One `Handler(http.server.BaseHTTPRequestHandler)` class. `do_GET`/`do_POST` are a flat `if/elif` chain matching `self.path` by exact string (one exception: `/api/discord/scan/<id>` is matched by prefix and the id sliced off manually — there's no real router).
- `_send_json(status, payload)` — always JSON, always sets Content-Length, always 200 unless explicitly told otherwise. Note: several handlers deliberately send `200` with an `{"error": ...}` body instead of a 4xx/5xx status, specifically for "this isn't really an error, it's just an empty/disconnected state the page should render normally" (e.g. Discord not connected). Others (bad JSON, bad dates, no email column) correctly use 400. Decide up front, per-endpoint, which failures are "real" HTTP errors vs. "valid empty state" — don't mix the convention within one endpoint.
- `_read_json()` — reads `Content-Length` bytes, parses JSON, sends a 400 itself and returns `None` if parsing fails. Every handler starts with `payload = self._read_json(); if payload is None: return`.
- Static pages are served by reading the `.html` file's bytes off disk and writing them straight back with a `text/html` header — no templating at all. **This whole responsibility disappears in the React rewrite** — a dev server (Vite, etc.) or the built static bundle serves the frontend, and `app.py`'s job shrinks to being a pure JSON API. Decide early whether the new backend still serves the built frontend (simplest for "double-click and go") or the two run as separate processes (simpler dev loop, needs CORS).

## Data-sensitivity rules — carry these into the rewrite unchanged

These aren't stylistic — they were explicit, repeated user requirements:

1. **Discord**: only the embed's title (→ status) and a field named "Profile"/"Profile Name" (→ profile) are ever read, stored, or logged. The "Account" and "Proxy" embed fields must never be touched.
2. **Debug logging of unrecognized data**: only field *names* may ever be printed (`describe_rejected` in `parser.py`) — never field *values*, since a rejected embed might be exactly the one with sensitive fields.
3. **The bot token** (`DISCORD_TOKEN`) must never be printed, logged, or committed. It lives in a gitignored `.env`.
4. **Profile Finder / Account Finder**: these process CSVs and account lists that routinely contain card numbers, CVVs, billing addresses, and live passwords. Both tools are **fully stateless** — everything happens in memory, per HTTP request; nothing is ever written to disk server-side and nothing touches the SQLite database. This was a deliberate choice specifically *because* of the sensitivity of that data, not an oversight to "fix" — preserve it.
5. **Duplicate protection** (Discord messages) relies on Discord's message ID being a real, permanent, unique key, enforced with `INSERT OR IGNORE` in SQLite. This is the *entire* dedup mechanism — re-scanning the same channel twice must always be safe and must never double-record. Whatever the rewrite uses for storage, preserve an equivalent guarantee.

## Testing pattern (keep this discipline)

- Pure-logic modules (`parser.py`, `analysis.py`, `dates.py`, `order_analysis.py`, both `filter.py`s) take plain data in, return plain data out — no Discord objects, no SQLite, no HTTP. They're tested with `types.SimpleNamespace` fakes and plain dicts, zero mocking libraries needed.
- `storage.py` is tested against a **temporary** SQLite file (`storage.DB_PATH` monkey-patched to a temp path in `setUp`/restored in `tearDown`) — the real `database/order_ledger.db` is never touched by a test run. Every test that could plausibly touch the real DB should verify this (the project's convention was hashing the real DB file before/after a test run and asserting it's byte-identical).
- The HTTP layer is tested by actually spinning up `http.server.ThreadingHTTPServer` on an ephemeral port (`("127.0.0.1", 0)`) with a fake standing in for `service.py` (a small class with the same method names, canned return values) — so the real request/response cycle, JSON encoding, and routing are all genuinely exercised, without ever touching real Discord.

Keep this three-layer separation (pure logic / storage / HTTP) in the rewrite — it's what made every bug below fast to reproduce and fix in isolation.

## Real bugs that were hit and fixed (don't reintroduce these classes of bug)

1. **Windows console + emoji → crash.** An embed title containing an emoji (e.g. "📊 Drop Summary") raised `UnicodeEncodeError` on Windows' default console codepage, silently ending a channel's scan partway through with no clear error surfaced. Fixed by reconfiguring `stdout`/`stderr` with `errors="backslashreplace"` before any scanning happens. **Lesson:** never print untrusted/external text (Discord content, CSV cell values, etc.) to a console without an explicit encoding-error strategy — or don't print external text to a console at all; log to a file/structured logger instead.

2. **Silent field-name mismatch dropped 100% of one channel's data.** The profile-matching logic checked for an embed field named exactly `"Profile"`. One bot instead named its field `"Profile Name"` — every single order from that channel was silently rejected, with the only visible symptom being "the app is under-counting" days later. Fixed by matching against a **normalized set** of accepted names (case/whitespace/trailing-colon insensitive) instead of one exact string. **Lesson:** any time you're matching a field/column name against external data, assume the name will vary, match against a set with normalization, and log what didn't match (names only, see privacy rule #2) so gaps are visible immediately instead of discovered via a user bug report.

3. **A bare `with conn:` on a sqlite3 connection does not close it.** It commits/rolls back, but leaks the connection (and can hold a file lock). Fixed with a `@contextmanager` wrapper that always calls `conn.close()` in a `finally`. **Lesson:** know exactly what your DB library's context manager does and doesn't do — don't assume "context manager" implies "closes the resource."

4. **Empty filter meant "match nothing" instead of "match everything."** Leaving the Discord profile-search box blank returned zero results instead of all of them — an easy, very confusing "the app lost my data" bug report. Fixed by explicitly treating an empty terms list as "no constraint" (`WHERE 1=1`) rather than short-circuiting to an empty result. **Lesson:** explicitly design and test the "no filter given" case for every filter — default to "show everything," not "show nothing," unless there's a specific reason otherwise.

5. **A single mistyped filter silently hid ~40% of real results, with no warning.** A remembered search-term list like `"Co mary, Kem, Jayden, steve Cau Hung"` (missing a comma) was parsed as one combined term `"steve Cau Hung"`, which matched nothing — quietly excluding every `"Cau Hung ..."` profile from the results with zero indication anything was wrong. **Lesson:** consider surfacing, per search term, whether it actually matched anything (a "0 results for 'steve Cau Hung'" hint would have caught this instantly) rather than only ever showing the combined result set.

6. **A `set()` used for "unique display" silently hid real duplicate events.** `group_by_status` deduplicates profile names within a status using a Python `set`, which is correct for "which distinct accounts got this status" but means two genuinely separate orders under the identical profile name and identical status collapse into a single displayed entry — hiding that it happened twice. **Lesson:** when grouping data for display, explicitly decide (and label) whether you're showing "unique entities" or "every event," per view — don't let a `set()`/`GROUP BY` make that decision by accident.

## Where duplicate logic already exists (a known, accepted maintenance risk)

`order_analysis.py`'s `aggregate()` (Python) and `frontend/index.html`'s `aggregateRows()` (JS) implement **the same aggregation twice** — the web page does its own client-side aggregation (so it can re-filter instantly without a round trip), while the CLI tool uses the Python version. They have already drifted once (the JS version gained `hasNewSuccess`/`hasRepeatSuccess`; the Python version never did). See `rewrite-01-account-analysis.md` for the full detail and a recommendation on what to do about it in the rewrite — this is worth a deliberate decision, not an accident, this time.

By contrast, Profile Finder and Account Finder each have **exactly one** implementation of their filter logic, called directly by both the CLI and the web endpoint. That's the pattern to copy wherever possible.
