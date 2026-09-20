"""
Offline tests for Jigging Analysis. Nothing here connects to Discord: messages,
channels and the client are fakes, and the database is a throwaway temp file.

Run from the project root:
    .venv\\Scripts\\python.exe -m unittest discover -s tests -v
"""
import asyncio
import sys
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace as NS

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "backend" / "jigging_analysis"))

import analysis      # noqa: E402
import dates         # noqa: E402
import parser as order_parser   # noqa: E402
import scan          # noqa: E402
import service       # noqa: E402
import storage       # noqa: E402

BOT_ID = 999
MSG_ID = 1000


def embed(title, **fields):
    return NS(title=title, fields=[NS(name=k.replace("_", " "), value=v) for k, v in fields.items()])


def message(embeds=(), author_id=1, when=None):
    global MSG_ID
    MSG_ID += 1
    return NS(id=MSG_ID, author=NS(id=author_id), embeds=list(embeds),
              created_at=when or datetime(2026, 9, 16, 15, 0, tzinfo=timezone.utc))


def order(status="Successful Checkout!", profile="Co Mary PKC_jig (24)", **kw):
    return message([embed(status, Profile=profile, Account="secret@x.com", Proxy="1.2.3.4")], **kw)


class ParserTests(unittest.TestCase):
    def test_normal_order(self):
        self.assertEqual(order_parser.parse(order()), ("Co Mary PKC_jig (24)", "Successful Checkout!"))

    def test_spoiler_markup_removed(self):
        self.assertEqual(order_parser.parse(order(profile="||Co Mary PKC_jig (24)||")),
                         ("Co Mary PKC_jig (24)", "Successful Checkout!"))

    def test_profile_field_name_variants(self):
        for name in ("Profile", "profile", "Profile:", " PROFILE : ", "Profile Name", "profile  name:"):
            m = message([NS(title="Successful Checkout!", fields=[NS(name=name, value="A (1)")])])
            self.assertEqual(order_parser.parse(m), ("A (1)", "Successful Checkout!"), name)

    def test_a_field_that_only_contains_the_word_profile_is_not_enough(self):
        m = message([NS(title="Successful Checkout!", fields=[NS(name="Profile Group", value="x"), NS(name="Proxy Profile", value="y")])])
        self.assertIsNone(order_parser.parse(m))

    def test_new_status_is_not_filtered(self):
        self.assertEqual(order_parser.parse(order(status="Your card was declined"))[1], "Your card was declined")

    def test_not_orders(self):
        self.assertIsNone(order_parser.parse(message()))                                         # no embeds
        self.assertIsNone(order_parser.parse(message([embed(None, Profile="A")])))               # no title
        self.assertIsNone(order_parser.parse(message([embed("Successful Checkout!", Site="T")])))  # no profile
        self.assertIsNone(order_parser.parse(message([embed("Successful Checkout!", Profile="  ")])))  # empty profile

    def test_only_own_messages_skipped(self):
        self.assertIsNone(order_parser.parse(order(author_id=BOT_ID), own_id=BOT_ID))
        self.assertIsNotNone(order_parser.parse(order(author_id=5), own_id=BOT_ID))   # other bots still count

    def test_sensitive_fields_never_returned(self):
        result = order_parser.parse(order())
        self.assertNotIn("secret@x.com", str(result))
        self.assertNotIn("1.2.3.4", str(result))
        described = order_parser.describe_rejected(order())
        self.assertNotIn("secret@x.com", str(described))     # names only, never values


class DatesTests(unittest.TestCase):
    def test_range_covers_the_whole_end_day(self):
        start, end = dates.day_bound("2026-09-16"), dates.day_bound("2026-09-16", end_of_day=True)
        self.assertEqual(end - start, timedelta(days=1))
        self.assertIsNotNone(start.tzinfo)

    def test_empty_is_none(self):
        self.assertIsNone(dates.day_bound(""))
        self.assertIsNone(dates.day_bound(None))
        self.assertIsNone(dates.to_utc_iso(None))

    def test_bad_format_raises(self):
        with self.assertRaises(ValueError):
            dates.day_bound("16/09/2026")


class StorageTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        self.addCleanup(self.tmp.cleanup)
        self._old = storage.DB_PATH
        storage.DB_PATH = Path(self.tmp.name) / "t.db"
        self.addCleanup(setattr, storage, "DB_PATH", self._old)
        storage.init_db()

    def test_duplicates_are_ignored(self):
        self.assertTrue(storage.record("1", "10", "A", "S", "2026-09-16T15:00:00+00:00"))
        self.assertFalse(storage.record("1", "10", "A", "S", "2026-09-16T15:00:00+00:00"))
        self.assertEqual(len(storage.search_orders_by_profile_terms(["a"])), 1)

    def test_latest_timestamp_per_channel(self):
        storage.record("1", "10", "A", "S", "2026-09-16T10:00:00+00:00")
        storage.record("2", "10", "A", "S", "2026-09-16T12:00:00.500000+00:00")
        storage.record("3", "20", "A", "S", "2026-09-17T00:00:00+00:00")
        self.assertEqual(storage.get_latest_message_timestamp("10"), "2026-09-16T12:00:00.500000+00:00")
        self.assertIsNone(storage.get_latest_message_timestamp("30"))

    def test_search_is_case_insensitive_partial_and_multi_term(self):
        for i, name in enumerate(["Co Mary A (1)", "co mary 12", "Kem X (2)", "Someone Else"]):
            storage.record(str(i), "10", name, "S", "2026-09-16T10:00:00+00:00")
        found = {r["profile"] for r in storage.search_orders_by_profile_terms(["CO MARY", "kem"])}
        self.assertEqual(found, {"Co Mary A (1)", "co mary 12", "Kem X (2)"})

    def test_date_range_uses_the_users_day_not_utc(self):
        start, end = dates.day_bound("2026-09-16"), dates.day_bound("2026-09-16", end_of_day=True)
        inside = [start + timedelta(hours=1), end - timedelta(minutes=1)]
        outside = [start - timedelta(hours=1), end + timedelta(hours=1)]
        for i, t in enumerate(inside + outside):
            storage.record(str(i), "10", f"P{i}", "S", dates.to_utc_iso(t))
        found = storage.search_orders_by_profile_terms(["p"], after=dates.to_utc_iso(start), before=dates.to_utc_iso(end))
        self.assertEqual({r["profile"] for r in found}, {"P0", "P1"})

    def test_search_terms_default_and_remembered(self):
        self.assertEqual(storage.get_search_terms(), storage.DEFAULT_SEARCH_TERMS)
        storage.set_search_terms(["a", "b"])
        self.assertEqual(storage.get_search_terms(), ["a", "b"])

    def test_selected_channels_roundtrip(self):
        storage.set_selected_channel_ids(["1", "2"])
        self.assertEqual(sorted(storage.get_selected_channel_ids()), ["1", "2"])
        storage.set_selected_channel_ids(["2"])
        self.assertEqual(storage.get_selected_channel_ids(), ["2"])


class AnalysisTests(unittest.TestCase):
    ROWS = [
        {"profile": "||Co Mary A (1)||", "status": "Successful Checkout!", "message_timestamp": "2026-09-16T10:00:00+00:00"},
        {"profile": "Co Mary A (1)", "status": "Order Canceled: Item Demand", "message_timestamp": "2026-09-16T11:00:00+00:00"},
        {"profile": "Kem B (2)", "status": "Successful Checkout!", "message_timestamp": "2026-09-16T12:00:00+00:00"},
    ]

    def test_group_by_status_cleans_and_dedupes(self):
        self.assertEqual(analysis.group_by_status(self.ROWS), {
            "Successful Checkout!": ["Co Mary A (1)", "Kem B (2)"],
            "Order Canceled: Item Demand": ["Co Mary A (1)"],
        })

    def test_group_by_profile_counts_and_orders_newest_first(self):
        p = analysis.group_by_profile(self.ROWS)["Co Mary A (1)"]
        self.assertEqual(p["total"], 2)
        self.assertEqual(p["history"][0]["status"], "Order Canceled: Item Demand")


class FakeChannel:
    """Stands in for a Discord text channel. history() yields the given
    messages, and can be told to blow up partway through."""
    def __init__(self, messages, cid=10, name="checkout", fail_after=None):
        self.id, self.name, self.guild = cid, name, NS(me=NS(id=BOT_ID))
        self._messages, self._fail_after = messages, fail_after
        self.history_args = None

    def history(self, limit=None, after=None, before=None):
        self.history_args = {"limit": limit, "after": after, "before": before}

        async def gen():
            for i, m in enumerate(self._messages):
                if self._fail_after is not None and i == self._fail_after:
                    raise RuntimeError("network dropped")
                yield m
        return gen()


class ScanChannelTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        self.addCleanup(self.tmp.cleanup)
        self._old = storage.DB_PATH
        storage.DB_PATH = Path(self.tmp.name) / "t.db"
        self.addCleanup(setattr, storage, "DB_PATH", self._old)
        storage.init_db()

    def run_scan(self, channel, **kw):
        return asyncio.run(scan.scan_channel(channel, **kw))

    def test_counts_add_up(self):
        msgs = [order(), order(status="Order Canceled: Item Demand"), message(),                  # 2 orders, 1 plain
                message([embed("Something odd", Site="Target")]),                                   # embed we can't read
                order(author_id=BOT_ID)]                                                            # our own message
        stats = self.run_scan(FakeChannel(msgs))
        self.assertEqual(stats["seen"], 5)
        self.assertEqual(stats["orders"], 2)
        self.assertEqual(stats["new"], 2)
        # the check to use whenever the numbers look wrong:
        self.assertEqual(stats["rejected"], 1)                       # only the odd one; our own message isn't counted
        self.assertEqual(stats["embeds"], stats["orders"] + stats["rejected"])

    def test_rescan_adds_nothing_new(self):
        msgs = [order(), order()]
        self.assertEqual(self.run_scan(FakeChannel(msgs))["new"], 2)
        again = self.run_scan(FakeChannel(msgs))
        self.assertEqual((again["orders"], again["new"]), (2, 0))

    def test_stops_early_with_a_reason_and_keeps_what_it_got(self):
        stats = self.run_scan(FakeChannel([order(), order(), order()], fail_after=2))
        self.assertEqual((stats["seen"], stats["new"]), (2, 2))
        self.assertIn("network dropped", stats["error"])

    def test_progress_callback_gets_final_stats(self):
        seen = []
        stats = self.run_scan(FakeChannel([order()] * 1), on_progress=lambda s: seen.append(dict(s)))
        self.assertEqual(seen[-1]["seen"], stats["seen"])

    def test_an_emoji_title_on_a_cp1252_console_does_not_end_the_scan(self):
        """Regression: printing a rejected embed titled '📊 ...' to a Windows
        console crashed the loop and stopped the channel after a few messages."""
        import io
        console = io.TextIOWrapper(io.BytesIO(), encoding="cp1252", errors="strict")
        scan.make_console_safe(console)
        old, sys.stdout = sys.stdout, console
        try:
            msgs = [order(), message([embed("📊 Daily stats", Total="5")]), order(), order()]
            stats = self.run_scan(FakeChannel(msgs))
        finally:
            sys.stdout = old
        self.assertIsNone(stats["error"])
        self.assertEqual((stats["seen"], stats["orders"], stats["rejected"]), (4, 3, 1))

    def test_arguments_reach_discord(self):
        ch = FakeChannel([])
        after = dates.day_bound("2026-09-16")
        self.run_scan(ch, limit=5, after=after)
        self.assertEqual(ch.history_args, {"limit": 5, "after": after, "before": None})


class FakeClient:
    def __init__(self, channels):
        self._channels = {c.id: c for c in channels}
        self.user = NS(id=BOT_ID)

    def get_channel(self, cid):
        return self._channels.get(cid)


class ServiceScanTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        self.addCleanup(self.tmp.cleanup)
        self._old = storage.DB_PATH
        storage.DB_PATH = Path(self.tmp.name) / "t.db"
        self.addCleanup(setattr, storage, "DB_PATH", self._old)
        storage.init_db()
        self.a, self.b = FakeChannel([order()], cid=10, name="a"), FakeChannel([order()], cid=20, name="b")
        self._client = service.client
        service.client = FakeClient([self.a, self.b])
        self.addCleanup(setattr, service, "client", self._client)

    def start(self, **kw):
        service._scans["t"] = {"status": "running", "channels": {
            cid: {"name": cid, "status": "queued", "messages_scanned": 0, "embeds": 0, "orders_found": 0,
                  "new_records": 0, "rejected": 0, "done": False} for cid in ("10", "20")}}
        asyncio.run(service._run_scan(["10", "20"], "t", kw.get("after"), kw.get("before"), kw.get("incremental", False)))
        return service._scans["t"]

    def test_full_scan_fills_progress_the_page_reads(self):
        job = self.start()
        self.assertEqual(job["status"], "complete")
        for cid in ("10", "20"):
            c = job["channels"][cid]
            self.assertEqual((c["messages_scanned"], c["orders_found"], c["new_records"], c["done"]), (1, 1, 1, True))
            self.assertIsNone(c["error"])
        self.assertIsNone(self.a.history_args["after"])

    def test_incremental_resumes_from_newest_stored(self):
        storage.record("1", "10", "A", "S", "2026-09-16T08:48:52.123456+00:00")
        self.start(incremental=True)
        self.assertEqual(self.a.history_args["after"], datetime(2026, 9, 16, 8, 48, 52, 123456, tzinfo=timezone.utc))
        self.assertIsNone(self.b.history_args["after"])          # nothing stored -> full scan

    def test_explicit_range_beats_incremental(self):
        storage.record("1", "10", "A", "S", "2026-09-16T08:48:52+00:00")
        after = dates.day_bound("2026-09-01")
        self.start(after=after, incremental=True)
        self.assertEqual(self.a.history_args["after"], after)

    def test_a_missing_channel_does_not_stop_the_others(self):
        service._scans["t"] = {"status": "running", "channels": {
            cid: {"name": cid, "status": "queued", "messages_scanned": 0, "orders_found": 0, "new_records": 0, "done": False}
            for cid in ("99", "20")}}
        asyncio.run(service._run_scan(["99", "20"], "t", None, None, False))
        job = service._scans["t"]
        self.assertIn("not found", job["channels"]["99"]["error"])
        self.assertEqual(job["channels"]["20"]["new_records"], 1)
        self.assertEqual(job["status"], "complete")


class ChannelLookupTests(unittest.TestCase):
    """find_channels() with a fake server: two #checkout channels in different categories."""
    def setUp(self):
        guild = NS(name="G", me=NS())
        def ch(cid, name, cat):
            c = NS(id=cid, name=name, guild=guild, category=NS(name=cat, id=cid * 10), position=cid,
                   permissions_for=lambda me: NS(view_channel=True, read_message_history=True))
            return c
        hidden = NS(id=9, name="secret", guild=guild, category=None, position=9,
                    permissions_for=lambda me: NS(view_channel=True, read_message_history=False))
        guild.text_channels = [ch(1, "checkout", "Orders"), ch(2, "checkout", "Old"), ch(3, "failed", "Orders"), hidden]
        self.client = NS(guilds=[guild])

    def test_ambiguous_then_qualified(self):
        matches, _ = scan.find_channels(self.client, "checkout")
        self.assertEqual(len(matches), 2)
        matches, _ = scan.find_channels(self.client, "orders/checkout")
        self.assertEqual([m[0].id for m in matches], [1])

    def test_partial_spaces_and_hash(self):
        self.assertEqual([m[0].id for m in scan.find_channels(self.client, "#fail")[0]], [3])

    def test_unreadable_channel_is_invisible_and_typos_get_suggestions(self):
        matches, suggestions = scan.find_channels(self.client, "secret")
        self.assertEqual(matches, [])
        matches, suggestions = scan.find_channels(self.client, "checkuot")
        self.assertEqual(matches, [])
        self.assertIn("checkout", suggestions)


if __name__ == "__main__":
    unittest.main()
