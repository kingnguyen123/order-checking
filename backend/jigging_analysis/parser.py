"""
Turns one Discord message into (profile, status). No Discord connection, no
database - it only looks at a message-like object, so it can be tested with
fake data (see tests/test_jigging_analysis.py).

What an order notification looks like: an embed whose TITLE is the status
("Successful Checkout!", "Order Canceled: Item Demand", ...) and which has a
field called "Profile". Only those two things are ever read. The Account and
Proxy fields are never touched.
"""


def _clean_profile(value):
    """Strip whitespace and Discord spoiler markup: '||Co Mary (1)||' -> 'Co Mary (1)'."""
    profile = (value or "").strip()
    if profile.startswith("||") and profile.endswith("||") and len(profile) >= 4:
        profile = profile[2:-2].strip()
    return profile


# Different checkout bots name this field differently. Add a name here when a
# channel shows up as "unrecognized" and its embeds have a different field name.
PROFILE_FIELD_NAMES = {"profile", "profile name"}


def _is_profile_field(name):
    """Case, spacing and a trailing colon don't matter: 'Profile', 'profile:',
    'Profile Name' all count."""
    normalized = " ".join((name or "").lower().rstrip(": ").split())
    return normalized in PROFILE_FIELD_NAMES


def parse(message, own_id=None):
    """Return (profile, status), or None when the message is not an order.

    own_id: the bot's own user id. Only OUR OWN messages are skipped - the
    order notifications come from a different bot or a webhook.
    Statuses are never checked against a fixed list, so a brand-new status
    (e.g. "Your card was declined") flows through untouched."""
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


def describe_rejected(message):
    """For debugging: what did an embed we could NOT parse look like?
    Returns a list of (title, [field names]). Field NAMES only - never values,
    so nothing sensitive is ever printed."""
    return [(e.title, [f.name for f in e.fields]) for e in message.embeds]
