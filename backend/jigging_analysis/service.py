"""
The Discord connection for the web app (app.py).

What lives here:
  - the bot client, running on a background thread next to the web server
  - the channel tree (server > category > channel) the UI shows as checkboxes
  - which channels are selected (remembered in SQLite)
  - starting a scan in the background and reporting its progress
  - live monitoring: new messages in selected channels are recorded as they arrive

The actual reading/parsing of messages is NOT here - it's scan.scan_channel()
and parser.parse(), the same code the command-line script uses.

This module is optional: if discord.py isn't installed or there's no token,
app.py simply runs without it.
"""
import asyncio
import os
import threading
from datetime import datetime, timezone

import discord

import scan
import storage
from parser import parse

TOKEN = (os.environ.get("DISCORD_TOKEN") or "").strip()

_intents = discord.Intents.default()
_intents.message_content = True          # must also be ON in the Developer Portal
client = discord.Client(intents=_intents)

_state = {"connected": False, "error": None}
_loop = None                             # the event loop the client runs on
_selected_channel_ids = set()            # in-memory copy of the saved selection

_scans = {}                              # scan_id -> progress dict (polled by the page)
_scan_counter = 0
_scan_lock = threading.Lock()


# ---------- channels ----------

async def get_channel_tree():
    """[{id, name, categories: [{id, name, channels: [{id, name}]}]}] for every
    server the bot is in, listing only channels it can actually read."""
    by_guild = {}
    for channel, _label in scan.readable_channels(client):
        guild = channel.guild
        cat = channel.category
        cat_key = (str(cat.id) if cat else "uncategorized", cat.name if cat else "Uncategorized")
        by_guild.setdefault(guild, {}).setdefault(cat_key, []).append(channel)

    tree = []
    for guild, cats in by_guild.items():
        categories = []
        for (cat_id, cat_name), channels in cats.items():
            channels.sort(key=lambda c: c.position)
            categories.append({
                "id": cat_id, "name": cat_name,
                "channels": [{"id": str(c.id), "name": c.name} for c in channels],
            })
        categories.sort(key=lambda c: c["name"].lower())
        tree.append({"id": str(guild.id), "name": guild.name, "categories": categories})
    return tree


def _flatten_channel_ids(tree):
    return {ch["id"] for g in tree for cat in g["categories"] for ch in cat["channels"]}


def run_on_discord_loop(coro, timeout=15):
    """Run an async Discord call from another thread (the web server's) and
    wait for the result. The client must only be touched on its own loop."""
    if _loop is None or not client.is_ready():
        raise RuntimeError("Discord is not connected")
    return asyncio.run_coroutine_threadsafe(coro, _loop).result(timeout=timeout)


def get_channel_tree_sync():
    return run_on_discord_loop(get_channel_tree())


def get_selected_channel_ids():
    return sorted(_selected_channel_ids)


def set_selected_channels(channel_ids):
    """Save the user's channel choice. IDs are checked against what the bot
    can really read right now - anything else is dropped, never trusted."""
    tree = run_on_discord_loop(get_channel_tree())
    accessible = _flatten_channel_ids(tree)
    valid = [str(cid) for cid in channel_ids if str(cid) in accessible]

    meta = {}
    for g in tree:
        for cat in g["categories"]:
            for ch in cat["channels"]:
                if ch["id"] in valid:
                    meta[ch["id"]] = {"guild_id": g["id"], "channel_name": ch["name"], "category_name": cat["name"]}

    storage.set_selected_channel_ids(valid, meta)
    _selected_channel_ids.clear()
    _selected_channel_ids.update(valid)
    return valid


# ---------- live monitoring ----------

@client.event
async def on_message(message):
    """A new message arrived. If it's in a selected channel and is an order,
    record it. Must never raise - one odd message can't kill the listener."""
    try:
        if str(message.channel.id) not in _selected_channel_ids:
            return
        result = parse(message, own_id=client.user.id if client.user else None)
        if result:
            profile, status = result
            storage.record(message.id, message.channel.id, profile, status, message.created_at.isoformat(), channel_name=message.channel.name)
            print(f"Discord: new order - Profile: {profile} | Status: {status}")
    except Exception as e:
        print(f"Discord: ignoring a message that couldn't be processed ({e}).")


# ---------- scanning in the background ----------

def _newest_stored_datetime(channel_id):
    """Newest already-stored message time for a channel, or None."""
    ts = storage.get_latest_message_timestamp(channel_id)
    if not ts:
        return None
    try:
        dt = datetime.fromisoformat(ts)
    except ValueError:
        return None
    return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)


def _copy_stats(entry, stats):
    """scan_channel's stats -> the field names the page reads."""
    entry["messages_scanned"] = stats["seen"]
    entry["embeds"] = stats["embeds"]
    entry["orders_found"] = stats["orders"]
    entry["new_records"] = stats["new"]
    entry["rejected"] = stats["rejected"]


async def _run_scan(channel_ids, scan_id, after, before, incremental):
    """Scan each channel in turn. `incremental` (with no explicit `after`)
    resumes each channel from its newest stored message instead of reading
    the whole history; a channel with nothing stored gets a full scan."""
    job = _scans[scan_id]
    try:
        for cid in channel_ids:
            entry = job["channels"][cid]
            channel = client.get_channel(int(cid))
            if channel is None:
                entry["error"] = "Channel not found or no longer accessible"
                entry["done"] = True
                continue

            entry["status"] = "scanning"
            channel_after = after
            if incremental and after is None:
                channel_after = _newest_stored_datetime(cid)

            stats = await scan.scan_channel(
                channel, limit=None, after=channel_after, before=before,
                on_progress=lambda s, e=entry: _copy_stats(e, s),
            )
            _copy_stats(entry, stats)
            entry["error"] = stats["error"]
            entry["done"] = True
    except Exception as e:
        job["error"] = f"Scan stopped unexpectedly: {e}"
        print(f"Discord: scan {scan_id} stopped unexpectedly ({e}).")
    finally:
        job["status"] = "complete"


def start_scan(channel_ids, after=None, before=None, incremental=False):
    """Start scanning in the background and return a scan_id right away; the
    page polls get_scan_progress(). after/before are aware datetimes or None."""
    global _scan_counter

    tree = run_on_discord_loop(get_channel_tree())
    accessible = _flatten_channel_ids(tree)
    valid = [str(cid) for cid in channel_ids if str(cid) in accessible]
    if not valid:
        raise ValueError("None of the requested channels are accessible right now")

    names = {ch["id"]: ch["name"] for g in tree for cat in g["categories"] for ch in cat["channels"]}
    with _scan_lock:
        _scan_counter += 1
        scan_id = str(_scan_counter)

    _scans[scan_id] = {
        "status": "running",
        "channels": {
            cid: {"name": names.get(cid, cid), "status": "queued", "messages_scanned": 0, "embeds": 0,
                  "orders_found": 0, "new_records": 0, "rejected": 0, "done": False}
            for cid in valid
        },
    }
    asyncio.run_coroutine_threadsafe(_run_scan(valid, scan_id, after, before, incremental), _loop)
    return scan_id


def get_scan_progress(scan_id):
    return _scans.get(scan_id)


# ---------- connection lifecycle ----------

@client.event
async def on_ready():
    _state["connected"] = True
    _state["error"] = None
    try:
        storage.init_db()
        accessible = _flatten_channel_ids(await get_channel_tree())
        saved = storage.get_selected_channel_ids()
        valid = [cid for cid in saved if cid in accessible]
        _selected_channel_ids.clear()
        _selected_channel_ids.update(valid)
        if len(saved) != len(valid):
            print(f"Discord: {len(saved) - len(valid)} previously-selected channel(s) are no longer accessible and were dropped.")
    except Exception as e:
        print(f"Discord: could not load saved channel selection ({e}).")
    print(f"Discord: connected as {client.user} - {len(_selected_channel_ids)} channel(s) selected")


def _run():
    """Thread body. client.run() can't be used here (it needs the main
    thread), so this makes its own event loop and drives client.start()."""
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
        print("Discord: turn on 'Message Content Intent' for this bot in the Discord Developer Portal.")
    except Exception as e:
        _state["error"] = str(e)
        print(f"Discord: connection failed ({e}). Discord features will stay disabled.")
    finally:
        _state["connected"] = False
        _loop = None
        loop.close()


def start_background():
    """Start the client on a background thread. Does nothing (and says so)
    when there's no token, so the rest of the app is unaffected."""
    if not TOKEN:
        print("Discord: DISCORD_TOKEN not set - Discord features disabled (CSV analysis is unaffected).")
        return
    threading.Thread(target=_run, name="discord-client", daemon=True).start()


def is_connected():
    return _state["connected"]


def last_error():
    return _state["error"]
