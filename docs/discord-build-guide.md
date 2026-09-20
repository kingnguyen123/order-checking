# Building the Discord connection by hand

Goal: connect a bot to your Discord server, read order-notification messages, pull out **Profile** and **Status**, and store them without duplicates.

Build it in the phases below, in order. Each phase ends with something you can run and check before moving on.

---

## Phase 0 - Discord side setup (no code)

1. Go to https://discord.com/developers/applications and create an application, then open the **Bot** tab.
2. **Reset Token** and copy it. This is your `DISCORD_TOKEN`. Anyone with it controls the bot.
3. On the same tab, under **Privileged Gateway Intents**, turn on **Message Content Intent**. Without it, Discord hides embed and message content from your bot. discord.py raises `PrivilegedIntentsRequired` at connect time if this is off.
4. Open **OAuth2 > URL Generator**. Scope: `bot`. Permissions: **View Channels** and **Read Message History**. Open the generated URL and add the bot to your server.
5. In the project root, create `.env`:
   ```
   DISCORD_TOKEN=paste-token-here
   ```
   Make sure `.env` is in `.gitignore`. Never print or log the token.

Install:
```
pip install discord.py python-dotenv
```

---

## Phase 1 - Connect (hello world)

Standalone script, `scratch_connect.py`. Don't touch the web app yet.

```python
import os
import discord
from dotenv import load_dotenv

load_dotenv()
TOKEN = os.environ["DISCORD_TOKEN"]

intents = discord.Intents.default()
intents.message_content = True          # must match the portal toggle

client = discord.Client(intents=intents)

@client.event
async def on_ready():
    print(f"Connected as {client.user}")
    for guild in client.guilds:
        print(" server:", guild.name)
    await client.close()

client.run(TOKEN)
```

**Check:** it prints your bot name and your server name, then exits.

Common failures:
- `LoginFailure` - wrong or expired token.
- `PrivilegedIntentsRequired` - Message Content Intent is off in the portal.
- Connects but `guilds` is empty - the bot was never invited (Phase 0, step 4).

---

## Phase 2 - Discover channels

The bot can only read channels where it has **View Channel** and **Read Message History**. Filter to those so you never offer a channel that will fail later.

```python
for guild in client.guilds:
    me = guild.me
    for channel in guild.text_channels:
        perms = channel.permissions_for(me)
        if perms.view_channel and perms.read_message_history:
            cat = channel.category.name if channel.category else "Uncategorized"
            print(f"{cat} / #{channel.name}  id={channel.id}")
```

**Check:** you see the channels your order bot posts in, with their IDs.

Design note: use the numeric `channel.id` internally and the name only for display. Names change, IDs don't. If a channel ID ever comes from the browser, re-validate it against this list before using it.

---

## Phase 3 - Read history and look at the raw data

Before writing any parser, print what a real message looks like. Order bots post **embeds**, not plain text.

```python
channel = client.get_channel(YOUR_CHANNEL_ID)
async for message in channel.history(limit=20):
    print("author:", message.author, "| bot:", message.author.bot, "| embeds:", len(message.embeds))
    for embed in message.embeds:
        print("  title:", repr(embed.title))
        for field in embed.fields:
            print("   field:", repr(field.name), "=", repr(field.value))
```

**Check:** you can see the exact `title` (this is your status) and the `Profile` field, including quirks:
- Field name may be `Profile` or `Profile:` (trailing colon).
- Values may be wrapped in spoiler markdown, e.g. `||Co Mary PKC_jig (24)||`.
- Some embeds have no title, or no Profile field. Those are not orders.

Notes on `history()`:
- `limit=None` means all messages. It pages through the Discord API, so big channels are slow and can be rate limited.
- With no `after`/`before`, it goes newest to oldest.
- It always asks Discord's API. It is not limited to what the bot has cached.

---

## Phase 4 - Parse one message

Write this as a plain function that takes a message and returns `(profile, status)` or `(None, None)`. Keep it free of database and network code so you can test it with fake objects.

Rules that mattered last time:
- **Status** = `embed.title`, stripped. Do not hardcode a list of statuses. New ones appear (e.g. "Your card was declined") and should flow through untouched.
- **Profile** = the field whose name, lowercased with trailing `:` and spaces removed, equals `"profile"`.
- Strip a leading and trailing `||` from the value.
- Skip only **your own bot's** messages (`message.author.id == client.user.id`). Do not skip all bots. The order notifications come from another bot or webhook.
- **Never read, store, or log the Account or Proxy fields.** Only take Profile and Status.

Test it with a few hand-made cases: normal, `Profile:` with colon, spoiler-wrapped, no embeds, no title, no profile field.

**Debug tip (important):** while building, log every message that *has embeds but returned no order*, with the embed title and field names. If your parse count is lower than what you see in Discord, this is where the difference shows up. It's the first thing to check for "Discord shows 11, I got 4".

---

## Phase 5 - Store with de-duplication

SQLite, one table:

| column | notes |
|---|---|
| `discord_message_id` | `TEXT NOT NULL UNIQUE` |
| `discord_channel_id` | `TEXT NOT NULL` |
| `profile` | `TEXT NOT NULL` |
| `status` | `TEXT NOT NULL` |
| `message_timestamp` | ISO string from `message.created_at.isoformat()` |

Insert with `INSERT OR IGNORE`, and use `cursor.rowcount > 0` to know whether the row was new. That single choice makes re-scanning safe, and it stops a live message and a later scan from double-counting.

Rules:
- Open a short-lived connection per call. SQLite connections can't be shared across threads, and you'll call this from both the web server threads and the Discord thread.
- Store IDs as text, since Discord IDs are large integers.
- Keep the same database across restarts. It's what makes search work without re-scanning everything.

---

## Phase 6 - Live monitoring

```python
@client.event
async def on_message(message):
    if str(message.channel.id) not in selected_channel_ids:
        return
    profile, status = parse(message)
    if profile and status:
        insert_order(message.id, message.channel.id, profile, status, message.created_at.isoformat())
```

Wrap the body in `try/except` and just log. One malformed message must never kill the listener.

Keep `selected_channel_ids` as an in-memory set that you update when the user changes their selection, so changes apply without a restart.

---

## Phase 7 - Run it beside the web server

This is where the last build was hardest, so decide it up front.

- `client.run()` blocks and installs signal handlers that only work on the **main thread**. Your `http.server` needs the main thread, so don't use it.
- Instead, start a daemon thread that creates its own event loop and runs `loop.run_until_complete(client.start(TOKEN))`. Save that loop in a module variable.
- To call Discord from an HTTP request thread, use `asyncio.run_coroutine_threadsafe(coro, loop).result(timeout=...)`. Never call `await` on the client directly from the HTTP thread.
- Scans take a long time. `POST /scan` should start a background task, return a `scan_id`, and let the page poll `GET /scan/<id>` for progress.
- If there is no token or the connection fails, print one line and carry on. The rest of the app must still work.

---

## Things to decide before you start

1. **Scan strategy.** Re-scan full history every time (simple, slow on big channels) or only fetch messages newer than the newest stored timestamp (fast, a bit more code)?
2. **What counts as an order.** Any embed with a title and a Profile field, or only certain titles?
3. **Which channels.** Keep the selection in SQLite, not `.env`, and re-validate it against accessible channels on every reconnect.
4. **Progress and errors.** If a scan stops partway through a channel, surface it in the UI and the console. Don't swallow it, or you get silently partial data.

## Suggested file split

- `discord_client.py` - connection, channel discovery, scan and live handlers
- `discord_storage.py` - SQLite only
- `discord_analysis.py` - pure grouping and search functions, no Discord or SQL calls
- `app.py` - routes that call the above

Keeping parsing and analysis free of network and database code is what makes each piece testable by itself.
