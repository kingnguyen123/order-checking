"""
Discord scan. Two ways to use this file:

1. As a command-line script (connect, scan the channels you name, print a
   summary, disconnect). Run from the project root:

     python backend/jigging_analysis/scan.py --channel-names checkout failed
     python backend/jigging_analysis/scan.py --channel-names "orders/checkout" --limit 200
     python backend/jigging_analysis/scan.py --channel-names checkout --from 2026-09-16 --to 2026-09-16
     python backend/jigging_analysis/scan.py --channels 123456789012345678 --all

   (--limit defaults to 50 messages per channel when no dates are given.)

2. As a library: the web app (service.py) imports scan_channel() and
   find_channels() from here, so there is one copy of the scanning logic.

Everything in this file except main() takes an already-connected client, so
it only works once the bot is ready (client.guilds is empty before that).
"""
import argparse
import difflib
import os
import sys
from pathlib import Path

import discord
from dotenv import load_dotenv

from dates import day_bound
from parser import describe_rejected, parse
from storage import record

load_dotenv(Path(__file__).resolve().parents[2] / ".env")   # project root .env


def make_console_safe(*streams):
    """Make printing unable to crash. The Windows console often uses a codepage
    that can't show emoji, and an embed title like '📊 Stats' would raise
    UnicodeEncodeError inside the scan loop and silently end the whole channel's
    scan. With errors='backslashreplace' the character is printed as an escape
    instead."""
    for stream in (streams or (sys.stdout, sys.stderr)):
        try:
            stream.reconfigure(errors="backslashreplace")
        except Exception:
            pass


make_console_safe()


# ---------- finding channels ----------

def readable_channels(client):
    """Every text channel the bot can read, as (channel, label) pairs.
    'Readable' = View Channel + Read Message History (both are needed to scan)."""
    out = []
    for guild in client.guilds:
        for channel in guild.text_channels:
            perms = channel.permissions_for(guild.me)
            if perms.view_channel and perms.read_message_history:
                category = channel.category.name if channel.category else "-"
                out.append((channel, f"{guild.name} / {category} / #{channel.name}"))
    return out


def resolve_ids(client, channel_ids):
    found = []
    for cid in channel_ids:
        channel = client.get_channel(cid)
        if channel is None or not hasattr(channel, "history"):
            print(f"[skip] {cid}: bot can't see this channel (wrong ID, not invited, or not a text channel)")
            continue
        perms = channel.permissions_for(channel.guild.me)
        if not (perms.view_channel and perms.read_message_history):
            print(f"[skip] #{channel.name}: missing View Channel / Read Message History")
            continue
        found.append(channel)
    return found


def find_channels(client, query):
    """query is 'name' or 'category/name'. Returns (matches, suggestions)."""
    query = query.strip().lstrip("#").lower()
    cat, _, name = query.rpartition("/")
    cat = cat.strip()
    name = name.strip().replace(" ", "-")       # Discord turns spaces in names into hyphens

    def in_category(channel):
        return not cat or (channel.category and channel.category.name.lower() == cat)

    everything = readable_channels(client)

    exact = [(c, label) for c, label in everything if c.name.lower() == name and in_category(c)]
    if exact:
        return exact, []

    partial = [(c, label) for c, label in everything if name in c.name.lower() and in_category(c)]
    if partial:
        return partial, []

    names = [c.name.lower() for c, _ in everything]
    return [], difflib.get_close_matches(name, names, n=3, cutoff=0.5)


def resolve_names(client, queries):
    """Turn typed names into channel objects. Never guesses when ambiguous."""
    chosen = []
    for q in queries:
        matches, suggestions = find_channels(client, q)
        if len(matches) == 1:
            chosen.append(matches[0][0])
        elif len(matches) > 1:
            print(f"[ambiguous] '{q}' matches {len(matches)} channels - use category/name:")
            for _, label in matches:
                print("     ", label)
        else:
            hint = f" - did you mean: {', '.join('#' + s for s in suggestions)}?" if suggestions else ""
            print(f"[not found] '{q}'{hint}")
    return chosen


def unique(channels):
    seen, out = set(), []
    for channel in channels:
        if channel.id not in seen:
            seen.add(channel.id)
            out.append(channel)
    return out


# ---------- scanning ----------

async def scan_channel(channel, limit=None, after=None, before=None, on_progress=None):
    """Read one channel's messages, parse each, record the orders.

    Returns a stats dict:
      seen      messages looked at (including our own bot's, which are then skipped)
      embeds    of the rest, how many had at least one embed
      orders    embeds recognised as an order (profile + status found)
      new       of those, how many were not already stored
      rejected  messages with embeds we could NOT read as an order
      error     None, or why the scan stopped early

    The invariant to check when counts look wrong:  embeds == orders + rejected.
    on_progress(stats), if given, is called every 25 messages and once at the end."""
    stats = {"seen": 0, "embeds": 0, "orders": 0, "new": 0, "rejected": 0, "error": None}
    own_id = channel.guild.me.id
    try:
        async for message in channel.history(limit=limit, after=after, before=before):
            stats["seen"] += 1
            if message.author.id == own_id:     # our own bot's messages are not orders and not "unrecognized"
                continue
            if message.embeds:
                stats["embeds"] += 1

            result = parse(message, own_id=own_id)
            if result:
                profile, status = result
                stats["orders"] += 1
                if record(message.id, channel.id, profile, status, message.created_at.isoformat()):
                    stats["new"] += 1
            elif message.embeds:
                stats["rejected"] += 1
                for title, field_names in describe_rejected(message):   # names only, never values
                    print(f"    [rejected] #{channel.name} msg {message.id} title={title!r} fields={field_names}")

            if on_progress and stats["seen"] % 25 == 0:
                on_progress(stats)
    except discord.Forbidden:
        stats["error"] = "No permission to read this channel's history"
    except Exception as e:                      # one bad channel must not stop the others
        stats["error"] = f"{type(e).__name__}: {e}"
    if stats["error"]:
        print(f"Discord: scan of #{channel.name} stopped early after {stats['seen']} message(s): {stats['error']}")
    if on_progress:
        on_progress(stats)
    return stats


def print_summary(channel, stats):
    line = (f"#{channel.name}: seen={stats['seen']} embeds={stats['embeds']} "
            f"orders={stats['orders']} new={stats['new']} rejected={stats['rejected']}")
    if stats["error"]:
        line += f"  STOPPED EARLY: {stats['error']}"
    print(line)


# ---------- command-line entry point ----------

def main():
    ap = argparse.ArgumentParser(description="Scan Discord channels for order notifications.")
    ap.add_argument("--channels", nargs="+", type=int, default=[], help="channel IDs")
    ap.add_argument("--channel-names", nargs="+", default=[], help='names, e.g. checkout "orders/failed"')
    ap.add_argument("--limit", type=int, help="max messages per channel")
    ap.add_argument("--all", action="store_true", help="no limit (full history)")
    ap.add_argument("--from", dest="date_from", help="YYYY-MM-DD (your local day)")
    ap.add_argument("--to", dest="date_to", help="YYYY-MM-DD (whole day included)")
    args = ap.parse_args()

    if not args.channels and not args.channel_names:
        ap.error("give --channels and/or --channel-names")

    if args.limit is not None:
        limit = args.limit
    elif args.all or args.date_from or args.date_to:
        limit = None
    else:
        limit = 50
    after = day_bound(args.date_from)
    before = day_bound(args.date_to, end_of_day=True)

    intents = discord.Intents.default()
    intents.message_content = True                      # must also be ON in the Developer Portal
    client = discord.Client(intents=intents)
    started = False

    @client.event
    async def on_ready():
        nonlocal started
        if started:                                     # on_ready can fire again after a reconnect
            return
        started = True
        try:
            print(f"Connected as {client.user}")
            channels = unique(resolve_ids(client, args.channels) + resolve_names(client, args.channel_names))
            for channel in channels:
                print_summary(channel, await scan_channel(channel, limit, after, before))
        finally:
            await client.close()

    token = (os.environ.get("DISCORD_TOKEN") or "").strip()
    if not token:
        print("DISCORD_TOKEN is not set - put it in the .env file at the project root.")
        return
    try:
        client.run(token)
    except discord.LoginFailure:
        print("Login failed - check DISCORD_TOKEN in .env")
    except discord.PrivilegedIntentsRequired:
        print("Turn on Message Content Intent for the bot in the Developer Portal")


if __name__ == "__main__":
    main()
