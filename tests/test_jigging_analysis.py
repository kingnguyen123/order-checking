"""
Offline tests for Jigging Analysis. Nothing here connects to Discord: messages,
channels and the client are fakes. There is no database - orders live in an
OrderStore kept in memory for the life of the test (or the app).

Run from the project root:
    .venv\\Scripts\\python.exe -m unittest discover -s tests -v
"""
import asyncio
import io
import sys
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace as NS

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "backend" / "jigging_analysis"))

from discord_client import DiscordService, make_console_safe                # noqa: E402
from messages import DateRange, Order, OrderGrouper, OrderParser, OrderStore  # noqa: E402

BOT_ID = 999
MSG_ID = 1000

parser = OrderParser()


def embed(title, **fields):
    return NS(title=title, fields=[NS(name=k.replace("_", " "), value=v) for k, v in fields.items()])


def message(embeds=(), author_id=1, when=None, channel_name="checkout"):
    global MSG_ID
    MSG_ID += 1
    return NS(id=MSG_ID, author=NS(id=author_id), embeds=list(embeds),
              channel=NS(id=10, name=channel_name),
              created_at=when or datetime(2026, 9, 16, 15, 0, tzinfo=timezone.utc))


def order_message(status="Successful Checkout!", profile="Co Mary PKC_jig (24)",
                  proxy="proxy.example.com:1007:user123:pass456", **kw):
    return message([embed(status, Profile=profile, Account="secret@x.com", Proxy=proxy)], **kw)


def make_order(message_id="1", profile="Kem (1)", status="Successful Checkout!", channel_name="checkout", day=16, proxy_host=""):
    return Order(message_id, "10", channel_name, profile, status,
                 datetime(2026, 9, day, 12, tzinfo=timezone.utc), proxy_host)


# ---------------------------------------------------------------------------
# OrderParser: reading the profile and status
# ---------------------------------------------------------------------------
class ParserTests(unittest.TestCase):
    def test_normal_order(self):
        order = parser.parse_order(order_message())
        self.assertEqual((order.profile, order.status), ("Co Mary PKC_jig (24)", "Successful Checkout!"))

    def test_spoiler_markup_removed(self):
        order = parser.parse_order(order_message(profile="||Co Mary PKC_jig (24)||"))
        self.assertEqual(order.profile, "Co Mary PKC_jig (24)")

    def test_spoiler_markup_with_inner_spaces_removed(self):
        order = parser.parse_order(order_message(profile="|| Green Business W# 480 ||"))
        self.assertEqual(order.profile, "Green Business W# 480")

    def test_profile_field_name_variants(self):
        for name in ("Profile", "profile", "Profile:", " PROFILE : ", "Profile Name", "profile  name:"):
            m = message([NS(title="Successful Checkout!", fields=[NS(name=name, value="A (1)")])])
            order = parser.parse_order(m)
            self.assertEqual(order.profile, "A (1)", name)

    def test_a_field_that_only_contains_the_word_profile_is_not_enough(self):
        m = message([NS(title="Successful Checkout!",
                        fields=[NS(name="Profile Group", value="x"), NS(name="Proxy Profile", value="y")])])
        self.assertIsNone(parser.parse_order(m))

    def test_new_status_is_not_filtered(self):
        # Not dropped just because it's unrecognized - "Your card was declined" IS
        # recognized (see StatusNormalizationTests) and normalizes from there.
        order = parser.parse_order(order_message(status="Something Totally New"))
        self.assertEqual(order.status, "Something Totally New")

    def test_not_orders(self):
        self.assertIsNone(parser.parse_order(message()))                                            # no embeds
        self.assertIsNone(parser.parse_order(message([embed(None, Profile="A")])))                  # no title
        self.assertIsNone(parser.parse_order(message([embed("Successful Checkout!", Site="T")])))    # no profile
        self.assertIsNone(parser.parse_order(message([embed("Successful Checkout!", Profile="  ")])))  # empty profile

    def test_sensitive_fields_never_returned(self):
        order = parser.parse_order(order_message())
        self.assertNotIn("secret@x.com", str(order))              # Account: never read at all
        self.assertNotIn("user123", str(order))                   # Proxy credentials: never kept
        self.assertNotIn("pass456", str(order))
        self.assertNotIn("1007", str(order))                      # port: never kept either
        self.assertEqual(order.proxy_host, "proxy.example.com")   # only the host is
        described = parser.describe_structure(order_message())
        self.assertNotIn("secret@x.com", str(described))     # names only, never values

    def test_message_ids_and_channel_ids_are_strings(self):
        order = parser.parse_order(order_message())
        self.assertIsInstance(order.message_id, str)
        self.assertIsInstance(order.channel_id, str)


# ---------------------------------------------------------------------------
# OrderParser: Drop Summary messages are not orders
# ---------------------------------------------------------------------------
class SkipTests(unittest.TestCase):
    def test_drop_summary_is_skipped_and_not_parsed(self):
        msg = message([embed("📊 Drop Summary — #checkout — today", Total_Orders="9")])
        self.assertTrue(parser.should_skip(msg))
        self.assertIsNone(parser.parse_order(msg))

    def test_a_real_order_is_not_skipped(self):
        self.assertFalse(parser.should_skip(order_message()))


# ---------------------------------------------------------------------------
# OrderParser: a canceled Hayha order gets its status fixed
# ---------------------------------------------------------------------------
class CancelReasonTests(unittest.TestCase):
    def test_hayha_successful_title_with_cancel_reason_becomes_canceled(self):
        m = message([embed("Successful Checkout!", Profile_Name="||Kem (1)||", Cancel_Reason="Address Mismatch")])
        self.assertEqual(parser.parse_order(m).status, "Order Canceled: Address Mismatch")

    def test_hayha_without_cancel_reason_stays_successful(self):
        m = message([embed("Successful Checkout!", Profile_Name="||Kem (1)||")])
        self.assertEqual(parser.parse_order(m).status, "Successful Checkout!")

    def test_empty_cancel_reason_changes_nothing(self):
        m = message([embed("Successful Checkout!", Profile_Name="||Kem (1)||", Cancel_Reason="  ")])
        self.assertEqual(parser.parse_order(m).status, "Successful Checkout!")

    def test_shikari_canceled_title_is_kept_as_is(self):
        m = message([embed("Order Canceled: Item Demand", Profile="||Co Mary (17)||")])
        self.assertEqual(parser.parse_order(m).status, "Order Canceled: Item Demand")

    def test_cancel_reason_on_a_non_successful_title_does_not_rewrite_it(self):
        # The cancel-reason rewrite only fires for a "successful"-titled embed; a title
        # that already says something else is left alone by THAT step (normalization,
        # tested separately, is what then maps "Declined" -> the canonical status).
        m = message([embed("Weird Status! :warning:", Profile="||Kem (1)||", Cancel_Reason="Some reason")])
        self.assertEqual(parser.parse_order(m).status, "Weird Status! :warning:")

    def test_cancel_reason_value_is_never_hidden_as_sensitive(self):
        # Cancel Reason is explicitly allowed to be read - it isn't private.
        m = message([embed("Successful Checkout!", Profile_Name="||Kem (1)||", Cancel_Reason="Address Mismatch")])
        self.assertIn("Address Mismatch", parser.parse_order(m).status)


# ---------------------------------------------------------------------------
# OrderParser: different bots word the same outcome differently - collapse
# known variants onto one canonical status so the status list in the UI
# doesn't show near-duplicate entries for the same real thing.
# ---------------------------------------------------------------------------
class StatusNormalizationTests(unittest.TestCase):
    def test_item_demand_with_policy_prefix_merges_with_plain_item_demand(self):
        m = message([embed("Order Canceled: Policy - Item Demand", Profile="||Co Mary (1)||")])
        self.assertEqual(parser.parse_order(m).status, "Order Canceled: Item Demand")

    def test_plain_item_demand_is_already_canonical(self):
        m = message([embed("Order Canceled: Item Demand", Profile="||Co Mary (1)||")])
        self.assertEqual(parser.parse_order(m).status, "Order Canceled: Item Demand")

    def test_quantity_limit_with_policy_prefix_merges_with_plain_quantity_limit(self):
        m = message([embed("Order Canceled: Policy - Quantity Limit", Profile="||Co Mary (1)||")])
        self.assertEqual(parser.parse_order(m).status, "Order Canceled: Quantity Limit")

    def test_review_hold_cancel_reason_and_review_hold_title_merge(self):
        via_cancel_reason = message([embed("Order Canceled: REVIEW_HOLD", Profile="||Co Mary (1)||")])
        via_title = message([embed("Successful Checkout (Review Hold)", Profile="||Co Mary (2)||")])
        self.assertEqual(parser.parse_order(via_cancel_reason).status, "Review Hold")
        self.assertEqual(parser.parse_order(via_title).status, "Review Hold")

    def test_card_declined_variants_merge(self):
        for title in ("Payment Declined!", "Your card was declined", "Card Declined! :warning:"):
            m = message([embed(title, Profile="||Co Mary (1)||")])
            self.assertEqual(parser.parse_order(m).status, "Order Canceled: Card Declined", title)

    def test_plain_success_is_unaffected(self):
        m = message([embed("Successful Checkout!", Profile="||Co Mary (1)||")])
        self.assertEqual(parser.parse_order(m).status, "Successful Checkout!")


# ---------------------------------------------------------------------------
# OrderParser: proxy HOST only - never the port/username/password that come
# with it in the same field (for ranking proxy providers by order volume)
# ---------------------------------------------------------------------------
class ProxyHostTests(unittest.TestCase):
    def test_only_the_host_is_kept(self):
        order = parser.parse_order(order_message(proxy="target.millyresi.com:1007:penguin_892_f406932f:vXhJHvIzrP0__-tgt-US-s-4aa065b5403e"))
        self.assertEqual(order.proxy_host, "target.millyresi.com")

    def test_credentials_are_never_kept_anywhere_on_the_order(self):
        order = parser.parse_order(order_message(proxy="gate.someproxy.com:8080:myuser:mypassword"))
        self.assertNotIn("myuser", str(order))
        self.assertNotIn("mypassword", str(order))
        self.assertNotIn("8080", str(order))

    def test_spoiler_wrapped_proxy_value(self):
        order = parser.parse_order(order_message(proxy="||gate.someproxy.com:8080:u:p||"))
        self.assertEqual(order.proxy_host, "gate.someproxy.com")

    def test_proxy_field_name_variants(self):
        for name in ("Proxy", "proxy", "Proxy Group", "Proxies"):
            m = message([NS(title="Successful Checkout!",
                            fields=[NS(name="Profile", value="A (1)"), NS(name=name, value="host.example.com:1:u:p")])])
            self.assertEqual(parser.parse_order(m).proxy_host, "host.example.com", name)

    def test_no_colon_means_the_whole_value_is_the_host(self):
        order = parser.parse_order(order_message(proxy="plainhostname"))
        self.assertEqual(order.proxy_host, "plainhostname")

    def test_missing_proxy_field_leaves_it_blank(self):
        m = message([embed("Successful Checkout!", Profile="A (1)")])
        self.assertEqual(parser.parse_order(m).proxy_host, "")

    def test_proxy_profile_field_name_does_not_count_as_proxy(self):
        # "Proxy Profile" is a trap seen elsewhere in these tests (it's not a
        # profile field); it must not be read as a proxy field either.
        m = message([NS(title="Successful Checkout!",
                        fields=[NS(name="Profile", value="A (1)"), NS(name="Proxy Profile", value="host:1:u:p")])])
        self.assertEqual(parser.parse_order(m).proxy_host, "")


# ---------------------------------------------------------------------------
# DateRange
# ---------------------------------------------------------------------------
class DateRangeTests(unittest.TestCase):
    def test_one_day_is_24_hours(self):
        r = DateRange.from_strings("2026-09-16", "2026-09-16")
        self.assertEqual(r.end_day - r.start_day, timedelta(hours=24))
        self.assertIsNotNone(r.start_day.tzinfo)

    def test_reversed_range_raises(self):
        with self.assertRaises(ValueError):
            DateRange.from_strings("2026-09-17", "2026-09-16")

    def test_empty_range_contains_everything(self):
        self.assertTrue(DateRange().contains(datetime(2000, 1, 1, tzinfo=timezone.utc)))

    def test_blank_text_means_no_limit(self):
        self.assertEqual(DateRange.from_strings("", ""), DateRange())

    def test_bad_format_raises(self):
        with self.assertRaises(ValueError):
            DateRange.from_strings("16/09/2026")


# ---------------------------------------------------------------------------
# OrderStore
# ---------------------------------------------------------------------------
class OrderStoreTests(unittest.TestCase):
    def test_duplicate_order_is_not_added_twice(self):
        store = OrderStore()
        self.assertTrue(store.record(make_order("1")))
        self.assertFalse(store.record(make_order("1")))
        self.assertEqual(store.count(), 1)

    def test_empty_search_returns_everything(self):
        store = OrderStore()
        store.record(make_order("1"))
        store.record(make_order("2", profile="Co Mary (2)"))
        self.assertEqual(len(store.search([])), 2)

    def test_search_is_case_insensitive(self):
        store = OrderStore()
        store.record(make_order("1", profile="Kem RM_jig (34)"))
        self.assertEqual(len(store.search(["kem"])), 1)

    def test_search_matches_any_of_multiple_terms(self):
        store = OrderStore()
        store.record(make_order("1", profile="Co Mary A (1)"))
        store.record(make_order("2", profile="Kem X (2)"))
        store.record(make_order("3", profile="Someone Else"))
        found = {o.profile for o in store.search(["co mary", "kem"])}
        self.assertEqual(found, {"Co Mary A (1)", "Kem X (2)"})

    def test_search_respects_date_range(self):
        store = OrderStore()
        store.record(make_order("1", day=15))
        store.record(make_order("2", day=16))
        found = store.search([], DateRange.from_strings("2026-09-16", "2026-09-16"))
        self.assertEqual([o.message_id for o in found], ["2"])

    def test_latest_timestamp_per_channel(self):
        store = OrderStore()
        store.record(make_order("1", day=15))
        store.record(make_order("2", day=16))
        self.assertEqual(store.latest_timestamp("10").day, 16)
        self.assertIsNone(store.latest_timestamp("999"))

    def test_search_terms_default_and_remembered(self):
        store = OrderStore()
        self.assertEqual(store.get_search_terms(), OrderStore.DEFAULT_SEARCH_TERMS)
        store.set_search_terms(["a", "b"])
        self.assertEqual(store.get_search_terms(), ["a", "b"])

    def test_selected_channels_roundtrip(self):
        store = OrderStore()
        store.set_selected_ids(["1", "2"])
        self.assertEqual(store.get_selected_ids(), {"1", "2"})
        store.set_selected_ids(["2"])
        self.assertEqual(store.get_selected_ids(), {"2"})

    def test_results_are_sorted_oldest_first(self):
        store = OrderStore()
        store.record(make_order("1", day=20))
        store.record(make_order("2", day=15))
        store.record(make_order("3", day=18))
        self.assertEqual([o.message_id for o in store.search([])], ["2", "3", "1"])


# ---------------------------------------------------------------------------
# OrderGrouper
# ---------------------------------------------------------------------------
class OrderGrouperTests(unittest.TestCase):
    def test_repeat_orders_collapse_in_profiles_per_status_but_not_orders_per_profile(self):
        orders = [make_order("1"), make_order("2")]          # same profile, same status
        g = OrderGrouper(orders)
        self.assertEqual(g.profiles_per_status(), {"Successful Checkout!": ["Kem (1)"]})
        self.assertEqual(g.orders_per_profile()["Kem (1)"]["total"], 2)

    def test_orders_per_profile_counts_each_status(self):
        g = OrderGrouper([make_order("1"), make_order("2", status="Order Canceled: Item Demand")])
        entry = g.orders_per_profile()["Kem (1)"]
        self.assertEqual(entry["statuses"], {"Successful Checkout!": 1, "Order Canceled: Item Demand": 1})

    def test_channels_per_profile(self):
        g = OrderGrouper([make_order("1", channel_name="checkout"), make_order("2", channel_name="failed")])
        self.assertEqual(g.channels_per_profile(), {"Kem (1)": ["checkout", "failed"]})

    def test_count_per_search_term_shows_a_term_that_matched_nothing(self):
        g = OrderGrouper([make_order("1")])
        self.assertEqual(g.count_per_search_term(["Kem", "steve"]), {"Kem": 1, "steve": 0})

    def test_orders_per_proxy_host_counts_regardless_of_status_and_sorts_by_count(self):
        g = OrderGrouper([
            make_order("1", status="Successful Checkout!", proxy_host="a.com"),
            make_order("2", status="Order Canceled: Item Demand", proxy_host="a.com"),
            make_order("3", status="Successful Checkout!", proxy_host="b.com"),
            make_order("4", status="Successful Checkout!", proxy_host=""),   # no proxy captured - excluded
        ])
        self.assertEqual(g.orders_per_proxy_host(), {"a.com": 2, "b.com": 1})


# ---------------------------------------------------------------------------
# DiscordService.scan_channel
# ---------------------------------------------------------------------------
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


def new_service():
    return DiscordService(bot=None, store=OrderStore(), parser=OrderParser(), token=None)


class ScanChannelTests(unittest.TestCase):
    def run_scan(self, channel, **kw):
        return asyncio.run(new_service().scan_channel(channel, **kw))

    def test_counts_add_up(self):
        msgs = [order_message(), order_message(status="Order Canceled: Item Demand"), message(),  # 2 orders, 1 plain
                message([embed("Something odd", Site="Target")]),                                   # embed we can't read
                order_message(author_id=BOT_ID)]                                                    # our own message
        stats = self.run_scan(FakeChannel(msgs))
        self.assertEqual(stats.seen, 5)
        self.assertEqual(stats.orders, 2)
        self.assertEqual(stats.new, 2)
        self.assertEqual(stats.rejected, 1)           # only the odd one; our own message isn't counted
        self.assertTrue(stats.is_consistent)          # embeds == orders + rejected

    def test_drop_summary_is_counted_as_ignored_not_rejected(self):
        msgs = [order_message(), message([embed("📊 Drop Summary — #checkout — today", Total="9")])]
        stats = self.run_scan(FakeChannel(msgs))
        self.assertEqual((stats.orders, stats.ignored, stats.rejected), (1, 1, 0))
        self.assertTrue(stats.is_consistent)

    def test_rescan_with_the_same_store_adds_nothing_new(self):
        service = new_service()
        msgs = [order_message(), order_message()]
        self.assertEqual(asyncio.run(service.scan_channel(FakeChannel(msgs))).new, 2)
        again = asyncio.run(service.scan_channel(FakeChannel(msgs)))
        self.assertEqual((again.orders, again.new), (2, 0))

    def test_stops_early_with_a_reason_and_keeps_what_it_got(self):
        stats = self.run_scan(FakeChannel([order_message(), order_message(), order_message()], fail_after=2))
        self.assertEqual((stats.seen, stats.new), (2, 2))
        self.assertIn("network dropped", stats.error)

    def test_progress_callback_fires_every_25_messages(self):
        # Fires only at multiples of 25 - NOT once at the end for a short scan,
        # so a channel under 25 messages gets no live progress update (the
        # final stats are still returned/stored once scanning finishes).
        seen = []
        msgs = [order_message() for _ in range(30)]
        stats = self.run_scan(FakeChannel(msgs), on_progress=lambda s: seen.append(s.seen))
        self.assertEqual(seen, [25])
        self.assertEqual(stats.seen, 30)

    def test_short_scan_never_calls_progress(self):
        seen = []
        self.run_scan(FakeChannel([order_message()]), on_progress=lambda s: seen.append(s.seen))
        self.assertEqual(seen, [])

    def test_an_emoji_title_on_a_cp1252_console_does_not_end_the_scan(self):
        """Regression: printing a rejected embed titled '📊 ...' to a Windows
        console crashed the loop and stopped the channel after a few messages."""
        console = io.TextIOWrapper(io.BytesIO(), encoding="cp1252", errors="strict")
        make_console_safe(console)
        old, sys.stdout = sys.stdout, console
        try:
            msgs = [order_message(), message([embed("📊 Daily stats", Total="5")]), order_message(), order_message()]
            stats = self.run_scan(FakeChannel(msgs))
        finally:
            sys.stdout = old
        self.assertIsNone(stats.error)
        self.assertEqual((stats.seen, stats.orders, stats.rejected), (4, 3, 1))

    def test_arguments_reach_discord(self):
        ch = FakeChannel([])
        after = DateRange.from_strings("2026-09-16").start_day
        self.run_scan(ch, after=after)
        self.assertEqual(ch.history_args, {"limit": None, "after": after, "before": None})


# ---------------------------------------------------------------------------
# DiscordService background scans (start_scan / _run_scan / get_scan_progress)
# ---------------------------------------------------------------------------
class FakeBot:
    def __init__(self, channels):
        self._channels = {c.id: c for c in channels}
        self.user = NS(id=BOT_ID)
        self.guilds = []   # readable_channels() iterates guilds; channels come via get_channel in these tests


class ServiceScanTests(unittest.TestCase):
    def setUp(self):
        self.a = FakeChannel([order_message()], cid=10, name="a")
        self.b = FakeChannel([order_message()], cid=20, name="b")
        self.service = DiscordService(bot=FakeBot([self.a, self.b]), store=OrderStore(), parser=OrderParser(), token=None)
        # readable_channels() normally comes from bot.guilds/permissions; tests
        # exercise _run_scan directly, so stub it to the two fake channels.
        self.service.readable_channels = lambda: [self.a, self.b]

    def start(self, date_range=None, incremental=False):
        asyncio.run(self.service._run_scan("t", ["10", "20"], date_range or DateRange(), incremental))
        return self.service.scans.setdefault("t", {"status": "done", "channels": {}})

    def test_full_scan_fills_progress_the_page_reads(self):
        self.service.scans["t"] = {"status": "running", "channels": {}}
        job = self.start()
        self.assertEqual(job["status"], "done")
        for cid in ("10", "20"):
            c = job["channels"][cid]
            self.assertEqual((c["seen"], c["orders"], c["new"]), (1, 1, 1))
            self.assertIsNone(c["error"])
        self.assertIsNone(self.a.history_args["after"])

    def test_incremental_resumes_from_newest_stored(self):
        self.service.store.record(make_order("1", channel_name="a"))     # stored under channel_id "10" via make_order default
        self.service.scans["t"] = {"status": "running", "channels": {}}
        self.start(incremental=True)
        self.assertEqual(self.a.history_args["after"], datetime(2026, 9, 16, 12, tzinfo=timezone.utc))
        self.assertIsNone(self.b.history_args["after"])          # nothing stored -> full scan

    def test_explicit_range_beats_incremental(self):
        self.service.store.record(make_order("1"))
        after = DateRange.from_strings("2026-09-01").start_day
        self.service.scans["t"] = {"status": "running", "channels": {}}
        self.start(date_range=DateRange(start_day=after), incremental=True)
        self.assertEqual(self.a.history_args["after"], after)

    def test_a_missing_channel_does_not_stop_the_others(self):
        self.service.readable_channels = lambda: [self.b]    # "10" is no longer accessible
        self.service.scans["t"] = {"status": "running", "channels": {}}
        job = self.start()
        self.assertIn("not found", job["channels"]["10"]["error"])
        self.assertEqual(job["channels"]["20"]["new"], 1)
        self.assertEqual(job["status"], "done")


if __name__ == "__main__":
    unittest.main()
