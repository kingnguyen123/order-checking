"""The Discord side: connect, list channels, scan history, listen live.

The web server (app.py) and the bot run on different threads. DiscordService is the
one safe bridge between them.
"""
import asyncio
import itertools
import sys
import threading
from dataclasses import asdict, dataclass

import discord

from messages import OrderParser, OrderStore


def make_console_safe(*streams):
    """Make printing unable to crash. The Windows console often uses a codepage
    that can't show emoji, and an embed title like '📊 Stats' would raise
    UnicodeEncodeError inside the scan loop and silently end the whole channel's
    scan. With errors='backslashreplace' the character is printed as an escape
    instead."""
    for stream in (streams or (sys.stdout, sys.stderr)):
        try:
            stream.reconfigure(errors="backslashreplace")  # type: ignore[attr-defined]
        except Exception:
            pass


make_console_safe()


# ---------------------------------------------------------------------------
# ScanStats: the numbers one channel's scan reports
# ---------------------------------------------------------------------------
@dataclass
class ScanStats:
    channel_id: str
    channel_name: str
    seen: int = 0              # every message read
    ignored: int = 0           # known non-orders (Drop Summary)
    embeds: int = 0            # messages with an embed, after skipping own and ignored ones
    orders: int = 0            # parsed into an order
    new: int = 0               # orders that weren't stored yet
    rejected: int = 0          # had an embed but didn't parse
    error: "str | None" = None

    @property
    def is_consistent(self):
        return self.embeds == self.orders + self.rejected

    def to_dict(self):
        return {**asdict(self), "is_consistent": self.is_consistent}


# ---------------------------------------------------------------------------
# OrderBot: the live listener. discord.py calls on_ready / on_message for us.
# ---------------------------------------------------------------------------
class OrderBot(discord.Client):
    def __init__(self, store: OrderStore, parser: OrderParser):
        intents = discord.Intents.default()
        intents.message_content = True       # must also be switched on in the Developer Portal
        super().__init__(intents=intents)
        self.store = store
        self.parser = parser

    async def on_ready(self):
        print(f"Discord: connected as {self.user}")

    async def on_message(self, message):
        # One malformed message must never stop the live listener.
        try:
            if str(message.channel.id) not in self.store.get_selected_ids():
                return
            if self.user and message.author.id == self.user.id:   # not ready yet -> self.user is None, can't be our own message
                return
            if self.parser.should_skip(message):
                return
            order = self.parser.parse_order(message)
            if order:
                self.store.record(order)
        except Exception as e:
            print(f"Discord: ignoring a message that couldn't be processed ({e}).")


# ---------------------------------------------------------------------------
# DiscordService: everything the web layer asks of Discord
# ---------------------------------------------------------------------------
class DiscordService:
    def __init__(self, bot, store: OrderStore, parser: OrderParser, token):
        self.bot = bot
        self.store = store
        self.parser = parser
        self.token = token
        self.loop = None                      # the bot's event loop, set once the thread starts
        self.scans = {}                       # scan_id -> progress dict
        self._scan_ids = itertools.count(1)

    # ---- connection ----------------------------------------------------
    def start_background(self):
        """Run the bot on its own thread. No token is allowed: Discord is just disabled."""
        if not self.token:
            print("Discord disabled: no DISCORD_TOKEN set. The rest of the app is unaffected.")
            return
        threading.Thread(target=self._run, name="discord-client", daemon=True).start()

    def _run(self):
        # The bot gets its own event loop. Use bot.start(), never bot.run():
        # run() wants to own the main thread, which the web server is using.
        self.loop = asyncio.new_event_loop()
        asyncio.set_event_loop(self.loop)
        try:
            self.loop.run_until_complete(self.bot.start(self.token))
        except discord.LoginFailure:
            print("Discord: login failed - check DISCORD_TOKEN in your .env file.")
        except discord.PrivilegedIntentsRequired:
            print("Discord: turn on 'Message Content Intent' for this bot in the Developer Portal.")
        except Exception as e:
            print(f"Discord: connection failed ({e}). Discord features will stay disabled.")
        finally:
            self.loop = None

    def is_connected(self):
        return self.loop is not None and self.bot.is_ready()

    def run_on_loop(self, coro, timeout=15):
        """The ONLY safe way for a web request to run Discord code."""
        if not self.is_connected():
            raise RuntimeError("Discord is not connected")
        assert self.loop is not None   # guaranteed by is_connected() above
        return asyncio.run_coroutine_threadsafe(coro, self.loop).result(timeout=timeout)

    def call_on_loop(self, func, timeout=15):
        """Run an ordinary function on the bot's thread and return what it returns."""
        async def runner():
            return func()
        return self.run_on_loop(runner(), timeout)

    # ---- channels ------------------------------------------------------
    def readable_channels(self):
        """Text channels where the bot can both view the channel and read its history."""
        for guild in self.bot.guilds:
            for channel in guild.text_channels:
                perms = channel.permissions_for(guild.me)
                if perms.view_channel and perms.read_message_history:
                    yield channel

    def _build_tree(self):
        guilds = {}
        for ch in self.readable_channels():
            g = guilds.setdefault(ch.guild.id, {"id": str(ch.guild.id), "name": ch.guild.name,
                                                "categories": {}})
            category = ch.category.name if ch.category else "(no category)"
            g["categories"].setdefault(category, []).append({"id": str(ch.id), "name": ch.name})
        return [{"id": g["id"], "name": g["name"],
                 "categories": [{"name": name, "channels": channels}
                                for name, channels in g["categories"].items()]}
                for g in guilds.values()]

    def get_channel_tree(self):
        """Servers -> categories -> channels, for the page's channel picker."""
        return self.call_on_loop(self._build_tree)

    def set_selected_channels(self, ids):
        """Keep only the IDs that are readable right now. Never trust the page's list."""
        valid = self.call_on_loop(lambda: {str(c.id) for c in self.readable_channels()})
        chosen = sorted({str(i) for i in ids} & valid)
        self.store.set_selected_ids(chosen)
        return chosen

    # ---- scanning ------------------------------------------------------
    async def scan_channel(self, channel, after=None, before=None, on_progress=None):
        """Read one channel's history. Never raises: problems go into stats.error."""
        stats = ScanStats(channel_id=str(channel.id), channel_name=channel.name)
        own_id = channel.guild.me.id
        try:
            # after / before are sent to Discord, so the date filtering happens there
            async for message in channel.history(limit=None, after=after, before=before):
                stats.seen += 1
                if message.author.id == own_id:
                    continue                      # skip before counting anything else
                if self.parser.should_skip(message):
                    stats.ignored += 1            # e.g. Drop Summary: not an order, not rejected
                    continue
                if message.embeds:
                    stats.embeds += 1
                order = self.parser.parse_order(message)
                if order:
                    stats.orders += 1
                    if self.store.record(order):
                        stats.new += 1
                elif message.embeds:
                    stats.rejected += 1
                    # field NAMES only, never values: a rejected embed may hold private data
                    for title, names in self.parser.describe_structure(message):
                        print(f"[rejected] #{channel.name} msg {message.id} "
                              f"title={title!r} fields={names}")
                if on_progress and stats.seen % 25 == 0:
                    on_progress(stats)
        except discord.Forbidden:
            stats.error = "missing permission"
        except Exception as e:
            stats.error = str(e)
        return stats

    def start_scan(self, ids, date_range, incremental=False):
        """Start a scan in the background and return its ID right away."""
        if not self.is_connected():
            raise RuntimeError("Discord is not connected")
        assert self.loop is not None   # guaranteed by is_connected() above
        scan_id = str(next(self._scan_ids))
        self.scans[scan_id] = {"status": "running", "channels": {}}
        asyncio.run_coroutine_threadsafe(
            self._run_scan(scan_id, [str(i) for i in ids], date_range, incremental), self.loop)
        return scan_id                            # don't wait: the page polls get_scan_progress

    async def _run_scan(self, scan_id, ids, date_range, incremental):
        progress = self.scans[scan_id]["channels"]
        try:
            by_id = {str(c.id): c for c in self.readable_channels()}     # re-check every ID now
            for channel_id in ids:
                channel = by_id.get(channel_id)
                if channel is None:
                    progress[channel_id] = {"error": "channel not found or not readable"}
                    continue
                after = date_range.start_day
                if incremental and after is None:
                    after = self.store.latest_timestamp(channel_id)      # None = scan everything
                stats = await self.scan_channel(
                    channel, after, date_range.end_day,
                    on_progress=lambda s, cid=channel_id: progress.__setitem__(cid, s.to_dict()))
                progress[channel_id] = stats.to_dict()
        finally:
            self.scans[scan_id]["status"] = "done"

    def get_scan_progress(self, scan_id):
        return self.scans.get(scan_id)
