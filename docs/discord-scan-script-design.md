# Design: a script that connects to Discord, opens the selected channels, and scans messages

This is the idea and structure for the script, not the finished code. For the Discord-side setup (bot token, intents, invite link) and the smaller building blocks, see `discord-build-guide.md`. This document is about how the pieces fit into one script.

## 1. What the script does

One run does this, then exits:

```
load config  ->  connect to Discord  ->  resolve the selected channels
      ->  for each channel: read messages  ->  parse each one  ->  record results
      ->  print a summary  ->  disconnect
```

Keep it a **one-shot command-line script** first. No web server, no background thread, no UI. Everything that made the last build hard (threads, polling, progress endpoints) is a wrapper you add later, once this core works.

## 2. Pieces and their jobs

| Piece | Job | Knows about Discord? |
|---|---|---|
| Config | Token, which channels, which time window | No |
| Connection | Log in, wait until ready, log out | Yes |
| Channel resolver | Turn "selected channels" into real channel objects the bot can read | Yes |
| Scanner | Loop over messages in one channel, count things | Yes |
| Parser | Message -> `(profile, status)` or nothing | No (takes a message-like object) |
| Recorder | Save or print each result, skip duplicates | No |

Rule: **parser and recorder must not import discord**. That lets you test them with fake data, which is where most of your bugs will be found.

## 3. Config (decide this first)

- **Token:** `DISCORD_TOKEN` from `.env`. Never printed, never logged.
- **Selected channels:** list of channel IDs. For the first version put them in a small file (`channels.txt`, one ID per line) or pass them as command-line arguments. Selecting from a UI comes later and only changes where this list comes from.
- **Scan window:** one of
  - last N messages (good for testing, start here with N=50)
  - date range (`--from 2026-09-16 --to 2026-09-16`)
  - everything since the newest message already recorded (incremental)
  - full history

Start with "last N", add the others one at a time.

## 4. Flow in detail

### 4.1 Connect
1. Create the client with the message content intent on.
2. Do the scan inside `on_ready`, because the guild and channel cache is only filled once the client is ready.
3. After the scan finishes, call `client.close()` so the script exits on its own.

### 4.2 Resolve the selected channels
For each configured ID:
1. `client.get_channel(int(id))`. If it returns `None`, the bot can't see it (wrong ID, not invited, or no permission). Log it and continue with the others.
2. Check `channel.permissions_for(channel.guild.me)` has `view_channel` and `read_message_history`. If not, log and skip.

Never let one bad channel stop the run.

### 4.3 Scan one channel
```
async for message in channel.history(limit=..., after=..., before=...):
    seen += 1
    result = parse(message)
    if result: record(result)
    else if message has embeds: log why it was rejected     <- see section 6
```
Things to know about `history()`:
- No `after`: newest to oldest. With `after`: oldest to newest. Order matters if you stop early.
- `after` and `before` must be **timezone-aware** datetimes (use UTC). A naive datetime gives wrong results.
- `limit=None` reads everything and can take a long time on busy channels.
- It always calls Discord's API. It is not limited to what the bot has cached.
- Wrap the loop in `try/except` **per channel**. If it fails halfway, record how far it got and say so. Do not swallow the error.

### 4.4 Parse (the contract)
Input: one message. Output: `(profile, status)` or `None`.
- Ignore only your own bot's messages. Order notifications come from *another* bot or a webhook.
- Look at `message.embeds`. Status = `embed.title`. Do not hardcode a list of statuses, new ones appear.
- Profile = the embed field whose name, lowercased and stripped of trailing `:`, is `profile`. Strip surrounding `||` (spoiler markup).
- Never read, store, or log the Account or Proxy fields.

### 4.5 Record
- Key each record by `message.id` (store as text). Ignore a record whose message ID was already stored. That single rule makes re-scanning safe.
- First version: print lines or write a CSV. Move to SQLite once parsing is right.
- Store: message ID, channel ID, profile, status, message timestamp. Nothing else.

### 4.6 Summary
Per channel print: `seen`, `orders parsed`, `new (not already stored)`, `embeds rejected`, and whether it finished or stopped early. This summary is your main debugging tool.

## 5. Skeleton (structure only, you fill in the bodies)

```python
# scan.py
import os, asyncio, argparse
import discord
from dotenv import load_dotenv

def parse(message):            # pure: message -> (profile, status) | None
    ...

def record(order):             # pure-ish: returns True if it was new
    ...

async def scan_channel(channel, limit, after, before):
    stats = {"seen": 0, "orders": 0, "new": 0, "rejected_embeds": 0, "error": None}
    try:
        async for message in channel.history(limit=limit, after=after, before=before):
            ...
    except Exception as e:
        stats["error"] = str(e)
    return stats

def resolve_channels(client, channel_ids):
    ...                         # returns readable channel objects, logs the rest

def main():
    load_dotenv()
    args = ...                  # channel ids, limit / date range
    intents = discord.Intents.default(); intents.message_content = True
    client = discord.Client(intents=intents)

    @client.event
    async def on_ready():
        try:
            for channel in resolve_channels(client, args.channels):
                stats = await scan_channel(channel, ...)
                print_summary(channel, stats)
        finally:
            await client.close()

    client.run(os.environ["DISCORD_TOKEN"])

if __name__ == "__main__":
    main()
```

`client.run()` is fine here because this script has nothing else using the main thread. It only becomes a problem when you embed the client in a web server (that's the later phase).

## 6. How to catch "Discord shows 11 orders, I got 4"

Missing orders almost always come from the parser rejecting messages, not from Discord. Build these checks in from the start:

1. **Count reconciliation.** Per channel: `seen`, messages with embeds, embeds parsed as orders, embeds rejected. If `embeds != parsed + rejected`, something is off.
2. **Log every rejected embed** with its title and its field names (not values). You'll see straight away if a field is called `Profile:` or `Profile ` or `Customer`, or if the title is empty.
3. **Test with a known day.** Pick a day where you know the true count. Scan exactly that window and compare.
4. **Check the window.** Off-by-a-day errors come from naive datetimes or a "before" that doesn't include the whole end day (add one day to it).
5. **Check for an early stop.** If the summary shows an error or the scan finished suspiciously fast, the loop ended partway.

## 7. Scan modes, in the order to build them

1. **Last N messages** (`limit=N`): proves connection, permissions, and parsing.
2. **Date range:** proves your datetime handling. Compare against a day you can count by hand.
3. **Incremental:** keep the newest recorded timestamp per channel and pass it as `after`. A channel with nothing recorded falls back to a full scan.
4. **Full history:** last, because it's slow. It's a backfill tool, not the everyday path.

## 8. Checkpoints (don't move on until each one passes)

- [ ] Script connects and lists the channels you selected, with names.
- [ ] A channel the bot can't read is reported, and the others still run.
- [ ] Last-50 scan prints each message's embed title and field names.
- [ ] `parse()` passes hand-made cases: normal, `Profile:` with colon, spoiler-wrapped, no embed, no title, no profile field, message from your own bot.
- [ ] Re-running the same scan records 0 new results (deduplication works).
- [ ] Scanning one known day matches the count you expect.
- [ ] Nothing in the output or logs contains the token, Account, or Proxy values.

## 9. What comes after (not part of this script)

- Store in SQLite instead of printing.
- Stay connected and handle `on_message` for live updates.
- Put the client on a background thread inside the web app, with scans started by a request and progress read by polling.
- Select channels from a UI and remember the choice.

Each of these builds on the one-shot script. If the core scan is trustworthy, the rest is plumbing.

## 10. Suggested files

```
jigging analysis/
  scan.py        # the script above: connect, resolve, scan, summary
  parser.py      # parse(message) - no discord imports
  storage.py     # record(order), get_latest_timestamp(channel) - no discord imports
  channels.txt   # selected channel IDs
```
