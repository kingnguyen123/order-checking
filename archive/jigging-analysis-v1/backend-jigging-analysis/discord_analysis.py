"""
Grouping/search logic over Discord order records.

Pure functions only - operates on rows already fetched from
discord_storage.py (each row: {"profile", "status", "message_timestamp"}).
No Discord or SQLite calls here, so this stays easy to reason about and
test independent of both. Kept separate from discord_client.py (networking)
and discord_storage.py (persistence) per the project's module boundaries.
"""


def _clean_profile(profile):
    """Strips Discord spoiler markdown (||text||) some bots wrap profile
    field values in, so names display as plain text. Only display-side -
    stored/matched values are untouched, so search terms still work either
    way. Older rows scanned before extract_profile_and_status started
    stripping this at the source still show up clean here."""
    p = (profile or "").strip()
    if p.startswith("||") and p.endswith("||") and len(p) >= 4:
        p = p[2:-2].strip()
    return p


def group_by_status(rows):
    """{status: [profile, profile, ...]} (sorted, deduplicated) - lets you
    ask "who got hit with this exact status?" without merging statuses
    together (e.g. a Review Hold stays separate from a plain success)."""
    by_status = {}
    for r in rows:
        by_status.setdefault(r["status"], set()).add(_clean_profile(r["profile"]))
    return {status: sorted(profiles) for status, profiles in by_status.items()}


def group_by_profile(rows):
    """{profile: {"total": N, "statuses": {status: count}, "history": [...]}}
    - the per-profile view, newest order first."""
    by_profile = {}
    for r in rows:
        profile = _clean_profile(r["profile"])
        p = by_profile.setdefault(profile, {"total": 0, "statuses": {}, "history": []})
        p["total"] += 1
        p["statuses"][r["status"]] = p["statuses"].get(r["status"], 0) + 1
        p["history"].append({"status": r["status"], "timestamp": r["message_timestamp"]})
    for p in by_profile.values():
        p["history"].sort(key=lambda h: h["timestamp"] or "", reverse=True)
    return by_profile
