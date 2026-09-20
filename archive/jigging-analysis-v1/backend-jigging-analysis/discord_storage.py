"""
Local SQLite storage for Order Ledger's Discord Order Analysis feature.

Kept completely separate from CSV analysis - nothing in app.py's CSV code
path, order_analysis.py, or analyze_orders.py imports this module.

Deliberately minimal: only Profile, Status, and the IDs needed to prevent
duplicate records and to remember which channels are selected to scan/
monitor. Discord's Account/Proxy embed fields are never stored here.

Every function opens a short-lived connection and closes it again, since
SQLite connections aren't safe to share across threads and this module is
called from both the HTTP server's threads and the Discord client thread.
"""

import sqlite3
from pathlib import Path

DB_PATH = Path(__file__).parent.parent.parent / "database" / "order_ledger.db"

# Seeded once into discord_settings the first time the app runs, so the
# profiles you always search for are remembered from the start instead of
# having to be retyped - the search bar still lets you add/change terms
# any time, and whatever you last search for becomes the new remembered set.
DEFAULT_SEARCH_TERMS = ["Co Mary", "Kem", "Jayden", "Cau Hung"]


def _connect():
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    return sqlite3.connect(DB_PATH)


def init_db():
    with _connect() as conn:
        conn.execute("""
            CREATE TABLE IF NOT EXISTS discord_orders (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                discord_message_id TEXT NOT NULL UNIQUE,
                discord_channel_id TEXT NOT NULL,
                profile TEXT NOT NULL,
                status TEXT NOT NULL,
                message_timestamp TEXT,
                created_at TEXT NOT NULL DEFAULT (datetime('now'))
            )
        """)
        conn.execute("""
            CREATE TABLE IF NOT EXISTS discord_selected_channels (
                channel_id TEXT PRIMARY KEY,
                guild_id TEXT,
                channel_name TEXT,
                category_name TEXT,
                selected_at TEXT NOT NULL DEFAULT (datetime('now'))
            )
        """)
        conn.execute("""
            CREATE TABLE IF NOT EXISTS discord_settings (
                key TEXT PRIMARY KEY,
                value TEXT
            )
        """)
        conn.commit()
        row = conn.execute("SELECT value FROM discord_settings WHERE key = 'search_terms'").fetchone()
        if row is None:
            conn.execute(
                "INSERT INTO discord_settings (key, value) VALUES ('search_terms', ?)",
                (", ".join(DEFAULT_SEARCH_TERMS),),
            )
            conn.commit()


def get_selected_channel_ids():
    with _connect() as conn:
        rows = conn.execute("SELECT channel_id FROM discord_selected_channels").fetchall()
    return [r[0] for r in rows]


def set_selected_channel_ids(channel_ids, meta=None):
    """Replaces the full selected-channels set in one transaction. `meta`
    is an optional {channel_id: {"guild_id", "channel_name", "category_name"}}
    dict used only for nicer display later - safe to omit."""
    meta = meta or {}
    with _connect() as conn:
        conn.execute("DELETE FROM discord_selected_channels")
        for cid in channel_ids:
            m = meta.get(cid, {})
            conn.execute(
                "INSERT INTO discord_selected_channels "
                "(channel_id, guild_id, channel_name, category_name) VALUES (?, ?, ?, ?)",
                (cid, m.get("guild_id"), m.get("channel_name"), m.get("category_name")),
            )
        conn.commit()


def get_latest_message_timestamp(channel_id):
    """ISO timestamp of the newest stored message for a channel, or None
    if nothing has been stored for it yet. Lets a scan fetch only what's
    new since last time instead of re-reading the whole channel."""
    with _connect() as conn:
        row = conn.execute(
            "SELECT MAX(message_timestamp) FROM discord_orders WHERE discord_channel_id = ?",
            (str(channel_id),),
        ).fetchone()
    return row[0] if row and row[0] else None


def get_search_terms():
    """The remembered list of profile search terms - starts out as
    DEFAULT_SEARCH_TERMS (seeded in init_db()) and is overwritten every
    time a search is actually run, so it always reflects what was last
    searched for."""
    with _connect() as conn:
        row = conn.execute("SELECT value FROM discord_settings WHERE key = 'search_terms'").fetchone()
    if not row or not row[0]:
        return list(DEFAULT_SEARCH_TERMS)
    return [t.strip() for t in row[0].split(",") if t.strip()]


def set_search_terms(terms):
    """Replaces the remembered search terms. Safe to call with an empty
    list (clears it back to nothing remembered)."""
    terms = [t.strip() for t in (terms or []) if t and t.strip()]
    with _connect() as conn:
        conn.execute(
            "INSERT INTO discord_settings (key, value) VALUES ('search_terms', ?) "
            "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
            (", ".join(terms),),
        )
        conn.commit()


def search_orders_by_profile_terms(terms, after=None, before=None):
    """Matches any stored order whose profile contains any of the given
    terms (case-insensitive, partial match) - so multiple profile names
    can be searched for at once. `after`/`before` are optional ISO-8601
    timestamp strings (same format message_timestamp is stored in) that
    narrow the match to that date range - both open-ended if omitted, so
    old callers/behavior are unaffected. Returns raw rows:
    [{"profile", "status", "message_timestamp"}, ...]"""
    terms = [t.strip() for t in (terms or []) if t and t.strip()]
    if not terms:
        return []
    clauses = " OR ".join(["lower(profile) LIKE ?"] * len(terms))
    params = [f"%{t.lower()}%" for t in terms]
    where = f"({clauses})"
    if after:
        where += " AND message_timestamp >= ?"
        params.append(after)
    if before:
        where += " AND message_timestamp < ?"
        params.append(before)
    with _connect() as conn:
        conn.row_factory = sqlite3.Row
        rows = conn.execute(
            f"SELECT profile, status, message_timestamp FROM discord_orders WHERE {where}"
            f" ORDER BY message_timestamp DESC",
            params,
        ).fetchall()
    return [dict(r) for r in rows]


def insert_discord_order(message_id, channel_id, profile, status, message_timestamp):
    """Returns True if a new record was inserted, False if that message ID
    was already stored (safe to call repeatedly - e.g. re-running a scan)."""
    with _connect() as conn:
        cur = conn.execute(
            "INSERT OR IGNORE INTO discord_orders "
            "(discord_message_id, discord_channel_id, profile, status, message_timestamp) "
            "VALUES (?, ?, ?, ?, ?)",
            (str(message_id), str(channel_id), profile, status, message_timestamp),
        )
        conn.commit()
        return cur.rowcount > 0
