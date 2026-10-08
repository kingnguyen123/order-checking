# Rewrite Guide: Jigging Analysis (Discord order tracking)

Read `rewrite-00-overview.md` first for shared conventions. This is the largest and most stateful tool — take it in layer order, it's designed to be read that way.

## What it does

Connects to Discord as a bot. Watches (or scans on demand) specific channels for "order notification" embeds posted by checkout bots. Extracts exactly two things from each — a **profile name** and a **status** (e.g. "Successful Checkout!") — and stores them in SQLite. Lets the user search/filter what's been captured, live-monitors selected channels for new orders as they arrive, and can incrementally or fully re-scan history.

## What counts as an "order notification" — the core contract

A Discord message qualifies if it has at least one **embed** whose:
- **title** is non-empty → becomes the `status` (verbatim, whatever the bot wrote — never checked against a fixed list of known statuses, so a brand-new status like "Your card was declined" flows through untouched)
- **fields** include one named `"Profile"` or `"Profile Name"` (see `PROFILE_FIELD_NAMES` below) → its value, after stripping whitespace and Discord spoiler markup (`||text||`), becomes the `profile`

**Nothing else is ever read.** Specifically, "Account" and "Proxy" fields that some bots include must never be touched, stored, or logged — this was an explicit, repeated requirement, not an oversight to fix.

## Layer 1 — `parser.py` (pure, no Discord/DB imports)

```python
PROFILE_FIELD_NAMES = {"profile", "profile name"}

def _is_profile_field(name):
    normalized = " ".join((name or "").lower().rstrip(": ").split())
    return normalized in PROFILE_FIELD_NAMES
```
Normalization handles case, extra whitespace, and a trailing colon — `"Profile"`, `"profile:"`, `"Profile Name"` all match. **This set exists specifically because different checkout bots name the field differently — add a name here whenever a channel comes back "unrecognized" and its embeds show a different field name spelling** (see bug #2 in the overview doc — this is the exact bug that created this requirement).

```python
def parse(message, own_id=None):
    if own_id is not None and message.author.id == own_id:
        return None
    for embed in message.embeds:
        status = (embed.title or "").strip()
        if not status:
            continue
        for field in embed.fields:
            if _is_profile_field(field.name):
                profile = _clean_profile(field.value)
                if profile:
                    return profile, status
                break
    return None
```
Returns `(profile, status)` or `None`. The bot's own messages (`own_id`) are skipped outright — they're never orders. `_clean_profile` strips `||spoiler||` wrapping.

```python
def describe_rejected(message):
    return [(e.title, [f.name for f in e.fields]) for e in message.embeds]
```
Debug helper for messages that had an embed but didn't parse — returns field **names** only, never values, so it's always safe to print (per the privacy rules).

## Layer 2 — `dates.py` (pure)

```python
def day_bound(text, end_of_day=False):
    if not text:
        return None
    dt = datetime.strptime(text, "%Y-%m-%d").astimezone()   # naive -> assumed local, made aware
    return dt + timedelta(days=1) if end_of_day else dt

def to_utc_iso(dt):
    return dt.astimezone(timezone.utc).isoformat() if dt else None
```
A picked day (`"2026-09-16"`) becomes an aware datetime **at local midnight**, not UTC midnight — Discord stores UTC timestamps, so a local evening order could otherwise land on the wrong UTC day if you naively used UTC day boundaries. `end_of_day=True` returns the *start of the next day*, so a range is used as `[after, before)` — half-open, which is why filtering is `>= after AND < before`, not `<=`. `to_utc_iso` converts to the exact string shape stored timestamps use, so range comparisons in SQLite are plain text `>=`/`<` (SQLite has no native datetime type).

## Layer 3 — `analysis.py` (pure, operates on rows already fetched from storage)

```python
def group_by_status(rows):
    # {status: [profile, ...]} sorted, DEDUPLICATED (see overview bug #6)
def group_by_profile(rows):
    # {profile: {"total", "statuses": {status: count}, "history": [{"status","timestamp"}, ...]}}
def profile_channels(rows):
    # {profile: [channel_name, ...]} sorted, deduplicated
```
`group_by_status` uses a `set()` per status — two separate orders with the identical profile name and identical status collapse into one displayed entry. That's *correct* for "which distinct accounts got this status" but hides that it happened twice. `group_by_profile` doesn't have this problem (it counts per status). **Decide deliberately in the rewrite which of these two semantics each view needs** — don't let a `set()` pick for you (overview bug #6 is this exact issue, discovered via a real "why does my count look low" report).

`_clean_profile` here is a second, separate copy of the same spoiler-stripping in `parser.py` — kept separate on purpose (module boundary: parsing vs. display), but worth merging into one shared helper in the rewrite if your module layout allows it.

## Layer 4 — `storage.py` (the only module touching SQLite)

Every function opens a fresh connection and closes it in `finally` — SQLite connections aren't thread-safe to share, and this module is called from **both** the Discord client's asyncio thread and the HTTP server's request threads.

```python
@contextmanager
def _connect():
    conn = sqlite3.connect(DB_PATH)
    try:
        with conn:      # commits/rolls back, but does NOT close - see overview bug #3
            yield conn
    finally:
        conn.close()
```

### Schema

```sql
CREATE TABLE discord_orders (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    discord_message_id TEXT NOT NULL UNIQUE,   -- the entire dedup mechanism
    discord_channel_id TEXT NOT NULL,
    profile TEXT NOT NULL,
    status TEXT NOT NULL,
    message_timestamp TEXT,                     -- UTC ISO string
    created_at TEXT NOT NULL DEFAULT (datetime('now')),
    channel_name TEXT                           -- added later, see below
);

CREATE TABLE discord_selected_channels (
    channel_id TEXT PRIMARY KEY,
    guild_id TEXT, channel_name TEXT, category_name TEXT,
    selected_at TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE TABLE discord_settings (
    key TEXT PRIMARY KEY, value TEXT
);
```

**`channel_name` on `discord_orders` is denormalized on purpose.** An earlier design instead `JOIN`ed against `discord_selected_channels` to get the channel's display name — which broke the moment a channel was *deselected* (that table gets fully replaced on every selection change), silently turning a friendly channel name back into a raw numeric ID in old search results. The fix: copy the channel's name onto the row **at record time**, so it survives deselection/renaming. `init_db()` also does a one-time backfill of this column for rows saved before it existed, sourced from whatever's currently in `discord_selected_channels`.

`discord_settings` currently holds exactly one key, `'search_terms'`, as a comma-joined string — the last-used search box content, reused as the default next time (seeded to `"Co Mary, Kem, Jayden, Cau Hung"` the first time the app ever runs).

### Key functions

```python
def record(message_id, channel_id, profile, status, message_timestamp, channel_name=None):
    # INSERT OR IGNORE keyed on message_id. Returns True only if actually new.
```
This one call, keyed on Discord's real message ID, is the **entire** duplicate-prevention mechanism. Re-scanning a channel (full history or incremental) is always safe because of this — preserve an equivalent guarantee in the rewrite no matter what storage engine you pick.

```python
def search_orders_by_profile_terms(terms, after=None, before=None):
    terms = [t.strip() for t in (terms or []) if t and t.strip()]
    if terms:
        where = "(" + " OR ".join(["lower(profile) LIKE ?"] * len(terms)) + ")"
        params = [f"%{t.lower()}%" for t in terms]
    else:
        where = "1=1"    # <- empty terms means "match everything", not "match nothing" (overview bug #4)
    # ... AND message_timestamp >= ? / < ? if after/before given
```
Case-insensitive substring match, ORed across every given term (so multiple names search at once). **An empty terms list matches every row** — this was a real bug fix (see overview bug #4); don't regress it to short-circuiting on empty input.

```python
def get_latest_message_timestamp(channel_id):
    # MAX(message_timestamp) for that channel, or None. Powers incremental scans.
def get_search_terms() / set_search_terms(terms):
    # last-used search box content, remembered as the new default
def set_selected_channel_ids(channel_ids, meta=None):
    # DELETE + reinsert the whole selected-channels set, in one transaction
```

## Layer 5 — `scan.py` (dual-purpose: importable library **and** standalone CLI)

Every function here except `main()` requires an **already-connected** Discord client (`client.guilds` is empty before `on_ready` fires). It's written to be both `import`ed by `service.py` (the live web app) and run directly (`python scan.py --channel-names checkout`) for manual debugging without booting the whole app — this dual-purpose pattern is what let real bugs get reproduced directly against live Discord data (see overview bugs #1 and #2, both found this way).

```python
def make_console_safe(*streams):
    for stream in (streams or (sys.stdout, sys.stderr)):
        stream.reconfigure(errors="backslashreplace")
make_console_safe()   # called at import time
```
Fixes overview bug #1 (emoji in an embed title crashing the whole scan on Windows). Call the equivalent of this early in the rewrite if you print any Discord-sourced text to a console.

**Channel discovery:**
```python
def readable_channels(client):
    # every text channel, across every guild the bot is in, where it has
    # BOTH View Channel AND Read Message History permission
def find_channels(client, query):
    # query = "name" or "category/name"; spaces -> hyphens (matches Discord's own rule)
    # tries: exact name match -> substring match -> difflib fuzzy suggestions (never guesses)
def resolve_names(client, queries) / resolve_ids(client, channel_ids):
    # user-typed names/ids -> real channel objects; ambiguous match = print all options, pick none
```

**The scan loop itself:**
```python
async def scan_channel(channel, limit=None, after=None, before=None, on_progress=None):
    stats = {"seen": 0, "embeds": 0, "orders": 0, "new": 0, "rejected": 0, "error": None}
    own_id = channel.guild.me.id
    async for message in channel.history(limit=limit, after=after, before=before):
        stats["seen"] += 1
        if message.author.id == own_id:
            continue                          # own messages: not seen-as-unrecognized either
        if message.embeds:
            stats["embeds"] += 1
        result = parse(message, own_id=own_id)
        if result:
            profile, status = result
            stats["orders"] += 1
            if record(message.id, channel.id, profile, status, message.created_at.isoformat(), channel_name=channel.name):
                stats["new"] += 1
        elif message.embeds:
            stats["rejected"] += 1
            for title, field_names in describe_rejected(message):
                print(f"[rejected] #{channel.name} msg {message.id} title={title!r} fields={field_names}")
        if on_progress and stats["seen"] % 25 == 0:
            on_progress(stats)
    # ... except discord.Forbidden / Exception -> stats["error"] = ..., never re-raised
    return stats
```
`channel.history(...)` is `discord.py`'s async generator over Discord's real paginated REST API — `limit`/`after`/`before` are sent as real query parameters, so date/count filtering happens on Discord's side, not by fetching everything and filtering locally. **Invariant to check when counts look wrong:** `embeds == orders + rejected`. One channel's error never stops the others — caught and stored in `stats["error"]`, never re-raised.

**CLI entry point** (`main()`, only reachable via `python scan.py`, never imported): `--channels` (ids), `--channel-names` (fuzzy-matched names), `--limit`, `--all` (no limit), `--from`/`--to` (YYYY-MM-DD). Default limit is 50 messages/channel *unless* `--all`/`--from`/`--to` was given, in which case there's no limit. Runs its own throwaway `discord.Client` via `client.run(token)` (blocking, fine for a one-shot CLI — **do not** use `client.run()` inside the live web app; see Layer 6).

## Layer 6 — `service.py` (the live bot behind the running web app)

**Threading model — the one thing to get exactly right in a rewrite:**
```python
def _run():
    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)
    loop.run_until_complete(client.start(TOKEN))   # NOT client.run() - that wants the main thread

def start_background():
    if not TOKEN:
        print("...disabled, rest of the app is unaffected"); return
    threading.Thread(target=_run, name="discord-client", daemon=True).start()
```
The Discord client runs on its own background thread with its own asyncio event loop, because the main thread is busy running the HTTP server. `client.run()` (used by the CLI) internally wants to own the thread it's called from — using it here would conflict with the HTTP server. Any HTTP-handling thread that needs to call into Discord does so via:
```python
def run_on_discord_loop(coro, timeout=15):
    if _loop is None or not client.is_ready():
        raise RuntimeError("Discord is not connected")
    return asyncio.run_coroutine_threadsafe(coro, _loop).result(timeout=timeout)
```
This is the *only* safe way to call Discord API code from a different thread than the one running its event loop.

**Missing token = graceful degradation, not a crash.** `start_background()` just prints and returns if `DISCORD_TOKEN` isn't set — the rest of the app (CSV analysis, searching already-saved Discord data) must keep working with no Discord connection at all. Every Discord-touching HTTP handler checks `discord_service.is_connected()` first and returns a normal (200, not error) "disconnected" response if not.

**Live monitoring:**
```python
@client.event
async def on_message(message):
    try:
        if str(message.channel.id) not in _selected_channel_ids:
            return
        result = parse(message, own_id=client.user.id if client.user else None)
        if result:
            profile, status = result
            storage.record(message.id, message.channel.id, profile, status,
                            message.created_at.isoformat(), channel_name=message.channel.name)
    except Exception as e:
        print(f"Discord: ignoring a message that couldn't be processed ({e}).")
```
Only fires for channels the user has selected. Wrapped so a single malformed message can **never** kill the live listener — this must always be true for any equivalent handler in the rewrite.

**Channel tree + selection**, for the UI's checkbox list:
```python
async def get_channel_tree():
    # {guild -> categories -> channels}, filtered to scan.readable_channels() only

def set_selected_channels(channel_ids):
    # re-validates every id against get_channel_tree() RIGHT NOW - never trusts
    # the client-sent list blindly, since permissions/channels can change
    # between page loads
```

**Background scans with pollable progress:**
```python
def start_scan(channel_ids, after=None, before=None, incremental=False):
    # validates ids against what's currently accessible
    # allocates a scan_id, seeds a progress dict in _scans[scan_id]
    # asyncio.run_coroutine_threadsafe(_run_scan(...), _loop)  <- fire and forget
    return scan_id                                            # <- returns IMMEDIATELY

def get_scan_progress(scan_id):
    return _scans.get(scan_id)   # the page polls this
```
`incremental=True` with no explicit `after` resumes each channel from `_newest_stored_datetime(channel_id)` (via `storage.get_latest_message_timestamp`) instead of rescanning full history; a channel with nothing stored yet still gets a full scan regardless.

**Startup (`on_ready`):** re-validates the saved channel selection against what's currently readable, dropping (and printing a count of) anything no longer accessible — permissions or channel deletions can happen between app runs.

## HTTP endpoints (app.py)

Every one of these must degrade gracefully (normal 200 response, not a crash) if `discord.py` isn't installed at all — `discord_service` is `None` in that case, checked via `self._discord_ready()`.

| Method | Path | Request | Response |
|---|---|---|---|
| GET | `/api/discord/channels` | — | `{connected, guilds, selected}` |
| POST | `/api/discord/channels/selection` | `{channel_ids}` | `{selected}` (post-validation) |
| POST | `/api/discord/scan` | `{channel_ids, after, before, incremental}` (`after`/`before` are `"YYYY-MM-DD"` or absent) | `{scan_id}` |
| GET | `/api/discord/scan/<id>` | — | the progress dict, or 404 |
| POST | `/api/discord/profiles/search` | `{names, after, before}` | `{byStatus, byProfile, byProfileChannels}` |
| GET | `/api/discord/search-terms` | — | `{terms}` |

`/api/discord/profiles/search` works **even while Discord is disconnected** — it only reads SQLite. As a side effect, it remembers `names` as the new default search terms (`storage.set_search_terms`), wrapped so a failure to save that preference never fails the search response itself. `_date_range(payload)` (shared helper) converts the two date strings via `day_bound` and validates `after < before`, raising a `ValueError` the caller turns into a 400.

## What the backend does **not** do (frontend-only today — decide if it moves server-side)

These currently live entirely in `discord.html`'s JavaScript, computed from the raw `byStatus`/`byProfileChannels` response:
- **Keyword/"category" filter** — which of the searched terms a profile's name contains (`profileCategories`), used to group/filter results by keyword.
- **Channel filter** — which channel(s) a profile matches, from `byProfileChannels`.
- **Jig-name extraction** (`extractJigName`) — strips a matched category's own prefix off a profile name, then trailing `"(number)"` or trailing digits, to get a bare "jig" identifier (e.g. `"Kem rcase (11)"` with category `"Kem"` → `"rcase"`). Used for the "Jig Breakdown" grouping and the jig-name search box.
- **"Search all" semantics for the category filter** — if no search terms were given, the category filter is skipped entirely client-side (there are no categories to filter by), rather than being computed and then matching zero profiles.

If the React rewrite wants any of this filtering to happen server-side (e.g. to support pagination, or very large result sets), it needs to be ported into `analysis.py` — right now it only exists as frontend string manipulation.

## Things worth reconsidering in the rewrite

- **`group_by_status`'s dedup-by-set** (overview bug #6) — decide per-view whether you want unique-accounts or every-event, and make it explicit (e.g. two clearly-named functions, not one function whose behavior is a `set()` implementation detail).
- **No visibility into per-term match counts** (overview bug #5) — a UI that shows "`Co mary`: 12, `Kem`: 8, `steve Cau Hung`: 0" per searched term would have caught the malformed-search-term bug instantly. Worth a dedicated field in the search response (e.g. `matchCountByTerm`).
- **`PROFILE_FIELD_NAMES` is a manually-maintained set.** Every new bot with a new field spelling requires a code change. Consider whether a more permissive heuristic (e.g. "the field whose name contains 'profile'") is safe enough — current code deliberately does *not* do this (there's a test guarding against over-matching `"Profile Group"`/`"Proxy Profile"`), so if you loosen it, keep that test.
- **The Discord client + HTTP server threading dance** is the trickiest part of this tool. If your new backend framework has native async support (e.g. an ASGI framework), you may be able to run the Discord client and the HTTP server on the **same** event loop instead of a separate thread — simpler, but changes the concurrency model throughout `service.py`. Worth a deliberate before/after design sketch rather than a mechanical port.
- **`_scans` (background scan progress) is an in-memory dict, lost on restart.** Fine for a single-user local tool; flag it explicitly if the rewrite ever needs scans to survive a restart.
