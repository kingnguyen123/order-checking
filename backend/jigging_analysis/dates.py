"""
Date helpers. No Discord, no database.

The date pickers give plain days ("2026-09-16"). A "day" means a day on YOUR
clock, but Discord stores times in UTC - so an evening order can belong to
the next UTC day. We therefore turn each picked day into a real moment in
your local timezone, and convert to UTC only when comparing with stored times.
"""

from datetime import datetime, timedelta, timezone


def day_bound(text, end_of_day=False):
    """'2026-09-16' -> timezone-aware datetime at local midnight, or None if
    text is empty. end_of_day=True returns the NEXT midnight, so a range
    [from, to] includes all of the 'to' day (use it as an exclusive end)."""
    if not text:
        return None
    dt = datetime.strptime(text, "%Y-%m-%d").astimezone()   # naive -> assumed local, made aware
    return dt + timedelta(days=1) if end_of_day else dt


def to_utc_iso(dt):
    """Aware datetime -> ISO string in UTC, the same shape stored message
    times use ('2026-09-16T08:48:52+00:00'), so text comparison is valid."""
    return dt.astimezone(timezone.utc).isoformat() if dt else None
