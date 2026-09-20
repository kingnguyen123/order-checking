"""
Discord integration for Order Ledger's Discord Order Analysis feature.

Connection + channel discovery live here. Which channels to scan/monitor
is chosen from the Order Ledger web UI and persisted in SQLite (via
discord_storage.py) - NOT configured in .env. Only the bot token stays
in .env, since that's a secret rather than a preference.

This module is intentionally optional and self-contained: if discord.py
isn't installed, or DISCORD_TOKEN isn't configured, importing/using it
never raises past its own functions. app.py treats Discord as a feature
that can simply stay disabled - CSV analysis is never affected.
"""

import asyncio
import os
import threading
from datetime import datetime, timezone
from pathlib import Path

import discord

import discord_storage

try:
    from dotenv import load_dotenv
    # Explicit path (not just load_dotenv()'s default search) so this finds
    # the project root's .env regardless of the current working directory -
    # this module now lives in backend/, one level below the project root.
    load_dotenv(Path(__file__).parent.parent.parent / ".env")
except ImportError:
    pass  # python-dotenv not installed - fall back to real environment variables only

TOKEN = os.environ.get("DISCORD_TOKEN", "").strip()

_intents = discord.Intents.default()
_intents.message_content = True  # required to read embeds on messages we didn't send

client = discord.Client(intents=_intents)

_state = {"connected": False, "error": None}
_loop = None  # the asyncio loop the client is running on (set in _run, used by run_on_discord_loop)
_selected_channel_ids = set()  # in-memory mirror of discord_storage's persisted selection

_scans = {}  # scan_id -> progress dict, polled by GET /api/discord/scan/<id>
_scan_counter = 0
_scan_lock = threading.Lock()


def extract_profile_and_status(message):
    """Pulls (profile, status) out of a message's order-notification embed,
    or (None, None) if it doesn't look like one. Only the message's own
    author check matters here - we ignore OUR OWN messages (to avoid
    feedback loops) but still inspect messages from other bots/webhooks,
    since that's how these order notifications actually arrive."""
    if client.user is not None and message.author.id == client.user.id:
        return None, None
    for embed in message.embeds:
        status = (embed.title or "").strip()
        if not status:
            continue
        profile = None
        for field in embed.fields:
            field_name = (field.name or "").strip().lower().rstrip(":").strip()
            if field_name == "profile":
                profile = (field.value or "").strip()
                # Some bots wrap this field in Discord spoiler markdown
                # (||text||) - strip it so stored/displayed names are plain.
                if profile.startswith("||") and profile.endswith("||") and len(profile) >= 4:
                    profile = profile[2:-2].strip()
                break
        if profile and status:
            return profile, status
    return None, None


async def get_channel_tree():
    """The guild/category/text-channel structure the bot can see AND read
    message history in, e.g.:
        [{"id": "...", "name": "...", "categories": [
            {"id": "...", "name": "...", "channels": [{"id": "...", "name": "..."}]}
        ]}]
    """
    guilds_out = []
    for guild in client.guilds:
        me = guild.me
        if me is None:
            continue
        by_category = {}
        for channel in guild.text_channels:
            perms = channel.permissions_for(me)
            if not (perms.view_channel and perms.read_message_history):
                continue
            cat = channel.category
            key = (str(cat.id) if cat else "uncategorized", cat.name if cat else "Uncategorized")
            by_category.setdefault(key, []).append(channel)

        categories_out = []
        for (cat_id, cat_name), channels in by_category.items():
            channels_sorted = sorted(channels, key=lambda c: c.position)
            categories_out.append({
                "id": cat_id,
                "name": cat_name,
                "channels": [{"id": str(c.id), "name": c.name} for c in channels_sorted],
            })
        categories_out.sort(key=lambda c: c["name"].lower())

        guilds_out.append({"id": str(guild.id), "name": guild.name, "categories": categories_out})
    return guilds_out


def _flatten_channel_ids(tree):
    return {ch["id"] for g in tree for cat in g["categories"] for ch in cat["channels"]}


def run_on_discord_loop(coro, timeout=15):
    """Runs an async Discord call from another thread (e.g. the HTTP
    server) and blocks for its result. Raises RuntimeError if Discord
    isn't connected yet."""
    if _loop is None or not client.is_ready():
        raise RuntimeError("Discord is not connected")
    future = asyncio.run_coroutine_threadsafe(coro, _loop)
    return future.result(timeout=timeout)


def get_channel_tree_sync():
    return run_on_discord_loop(get_channel_tree())


def get_selected_channel_ids():
    return sorted(_selected_channel_ids)


def set_selected_channels(channel_ids):
    """Validates the requested IDs against what the bot can actually
    access right now, persists only the valid ones, and updates live
    monitoring immediately. Never trusts the caller's list outright."""
    tree = run_on_discord_loop(get_channel_tree())
    accessible_ids = _flatten_channel_ids(tree)
    valid_ids = [str(cid) for cid in channel_ids if str(cid) in accessible_ids]

    meta = {}
    for g in tree:
        for cat in g["categories"]:
            for ch in cat["channels"]:
                if ch["id"] in valid_ids:
                    meta[ch["id"]] = {"guild_id": g["id"], "channel_name": ch["name"], "category_name": cat["name"]}

    discord_storage.set_selected_channel_ids(valid_ids, meta)
    _selected_channel_ids.clear()
    _selected_channel_ids.update(valid_ids)
    return valid_ids


@client.event
async def on_message(message):
    """Live monitoring: only processes messages in currently-selected
    channels, so changing the selection in the UI takes effect immediately
    without a restart. Malformed/irrelevant messages are simply ignored -
    never raises past here."""
    try:
        if str(message.channel.id) not in _selected_channel_ids:
            return
        profile, status = extract_profile_and_status(message)
        if profile and status:
            timestamp = message.created_at.isoformat() if message.created_at else None
            discord_storage.insert_discord_order(message.id, message.channel.id, profile, status, timestamp)
            print(f"Discord: new order - Profile: {profile} | Status: {status}")
    except Exception as e:
        print(f"Discord: ignoring a message that couldn't be processed ({e}).")


async def _scan_channel(channel, entry, after=None, before=None):
    c = entry["channels"][str(channel.id)]
    messages_scanned = 0
    orders_found = 0
    new_records = 0
    try:
        async for message in channel.history(limit=None, after=after, before=before):
            messages_scanned += 1
            profile, status = extract_profile_and_status(message)
            if profile and status:
                orders_found += 1
                timestamp = message.created_at.isoformat() if message.created_at else None
                if discord_storage.insert_discord_order(message.id, channel.id, profile, status, timestamp):
                    new_records += 1
            if messages_scanned % 25 == 0:
                c["messages_scanned"] = messages_scanned
                c["orders_found"] = orders_found
                c["new_records"] = new_records
    except discord.Forbidden:
        c["error"] = "No permission to read this channel's history"
        print(f"Discord: scan of #{c['name']} stopped early - no permission to read history "
              f"(only got through {messages_scanned} message(s)).")
    except Exception as e:
        c["error"] = str(e)
        print(f"Discord: scan of #{c['name']} stopped early after {messages_scanned} message(s) ({e}). "
              "Any messages after that point were not scanned - re-run the scan once this is resolved.")
    finally:
        c["messages_scanned"] = messages_scanned
        c["orders_found"] = orders_found
        c["new_records"] = new_records
        c["done"] = True


def _newest_stored_datetime(channel_id):
    """Newest already-stored message time for a channel as an aware
    datetime, or None if there's nothing stored (or it can't be parsed)."""
    ts = discord_storage.get_latest_message_timestamp(channel_id)
    if not ts:
        return None
    try:
        dt = datetime.fromisoformat(ts)
    except ValueError:
        return None
    return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)


async def _run_scan(channel_ids, scan_id, after=None, before=None, incremental=False):
    """`incremental` only applies when no explicit `after` was given: each
    channel then resumes from its newest stored message instead of
    re-reading the whole history (a channel with nothing stored yet still
    gets a full scan)."""
    entry = _scans[scan_id]
    try:
        for cid in channel_ids:
            channel = client.get_channel(int(cid))
            c = entry["channels"][cid]
            if channel is None:
                c["error"] = "Channel not found or no longer accessible"
                c["done"] = True
                continue
            c["status"] = "scanning"
            channel_after = after
            if incremental and after is None:
                channel_after = _newest_stored_datetime(cid)
            await _scan_channel(channel, entry, after=channel_after, before=before)
    except Exception as e:
        entry["error"] = f"Scan stopped unexpectedly: {e}"
        print(f"Discord: scan {scan_id} stopped unexpectedly ({e}).")
    finally:
        entry["status"] = "complete"


def start_scan(channel_ids, after=None, before=None, incremental=False):
    """Validates the requested channels, starts scanning them in the
    background, and returns a scan_id immediately so the caller can poll
    progress via get_scan_progress(). `after`/`before` are optional
    timezone-aware datetimes restricting the scan to that date range (both
    ends optional - omit either/both to scan open-ended). Raises
    ValueError/RuntimeError for the caller (app.py) to turn into a clean
    error response."""
    global _scan_counter

    tree = run_on_discord_loop(get_channel_tree())
    accessible_ids = _flatten_channel_ids(tree)
    valid_ids = [str(cid) for cid in channel_ids if str(cid) in accessible_ids]
    if not valid_ids:
        raise ValueError("None of the requested channels are accessible right now")

    id_to_name = {ch["id"]: ch["name"] for g in tree for cat in g["categories"] for ch in cat["channels"]}

    with _scan_lock:
        _scan_counter += 1
        scan_id = str(_scan_counter)

    _scans[scan_id] = {
        "status": "running",
        "after": after.isoformat() if after else None,
        "before": before.isoformat() if before else None,
        "channels": {
            cid: {
                "name": id_to_name.get(cid, cid), "status": "queued",
                "messages_scanned": 0, "orders_found": 0, "new_records": 0, "done": False,
            }
            for cid in valid_ids
        },
    }
    asyncio.run_coroutine_threadsafe(_run_scan(valid_ids, scan_id, after, before, incremental), _loop)
    return scan_id


def get_scan_progress(scan_id):
    return _scans.get(scan_id)


@client.event
async def on_ready():
    _state["connected"] = True
    _state["error"] = None
    try:
        discord_storage.init_db()
        accessible_ids = _flatten_channel_ids(await get_channel_tree())
        persisted = discord_storage.get_selected_channel_ids()
        valid = [cid for cid in persisted if cid in accessible_ids]
        dropped = len(persisted) - len(valid)
        _selected_channel_ids.clear()
        _selected_channel_ids.update(valid)
        if dropped:
            print(f"Discord: {dropped} previously-selected channel(s) are no longer accessible and were dropped.")
    except Exception as e:
        print(f"Discord: could not load saved channel selection ({e}).")
    print(f"Discord: connected as {client.user} - {len(_selected_channel_ids)} channel(s) selected")


def _run():
    global _loop
    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)
    _loop = loop
    try:
        loop.run_until_complete(client.start(TOKEN))
    except discord.LoginFailure:
        _state["error"] = "invalid token"
        print("Discord: login failed - check DISCORD_TOKEN in your .env file.")
    except discord.PrivilegedIntentsRequired:
        _state["error"] = "missing privileged intent"
        print("Discord: enable 'Message Content Intent' for this bot in the Discord Developer Portal.")
    except Exception as e:
        _state["error"] = str(e)
        print(f"Discord: connection failed ({e}). Discord features will stay disabled.")
    finally:
        _state["connected"] = False
        _loop = None
        loop.close()


def start_background():
    """Start the Discord client on a background daemon thread, if a token
    is configured. Safe to call even when Discord isn't set up at all -
    it just does nothing and returns."""
    if not TOKEN:
        print("Discord: DISCORD_TOKEN not set - Discord features disabled (CSV analysis is unaffected).")
        return

    thread = threading.Thread(target=_run, name="discord-client", daemon=True)
    thread.start()


def is_connected():
    return _state["connected"]


def last_error():
    return _state["error"]
