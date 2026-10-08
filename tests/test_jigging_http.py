"""
Talks to the real web server (app.Handler) over HTTP, with a fake Discord
service behind it. Nothing connects to Discord. There is no database - orders
live in app.discord_store (an in-memory OrderStore), swapped for a fresh one
each test so tests never see each other's data.
"""
import http.server
import json
import sys
import threading
import unittest
import urllib.error
import urllib.request
from pathlib import Path
from types import SimpleNamespace as NS

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "backend"))
sys.path.insert(0, str(ROOT / "backend" / "jigging_analysis"))

import app                                    # noqa: E402
from messages import DateRange, OrderStore    # noqa: E402  (the same module app.py uses)


class FakeService:
    """Same functions app.py calls on discord_service, but answering from
    memory. Writes selection through to the shared OrderStore, exactly like
    the real DiscordService does, since app.py reads "selected" from the
    store, not from the service."""
    def __init__(self, store, connected=True):
        self.store = store
        self.connected = connected
        self.started = []

    def is_connected(self):
        return self.connected

    def get_channel_tree(self):
        return [{"id": "1", "name": "G", "categories": [{"name": "Orders", "channels": [{"id": "10", "name": "checkout"}]}]}]

    def set_selected_channels(self, ids):
        chosen = sorted(i for i in map(str, ids) if i == "10")   # only 10 is "accessible"
        self.store.set_selected_ids(chosen)
        return chosen

    def start_scan(self, ids, date_range, incremental=False):
        self.started.append(NS(ids=ids, date_range=date_range, incremental=incremental))
        return "42"

    def get_scan_progress(self, scan_id):
        if scan_id != "42":
            return None
        return {"status": "done", "channels": {"10": {
            "channel_id": "10", "channel_name": "checkout", "seen": 3, "ignored": 0,
            "embeds": 3, "orders": 2, "new": 2, "rejected": 1, "error": None, "is_consistent": True,
        }}}


class HttpTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), app.Handler)
        cls.base = f"http://127.0.0.1:{cls.server.server_address[1]}"
        threading.Thread(target=cls.server.serve_forever, daemon=True).start()

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()
        cls.server.server_close()

    def setUp(self):
        self._old_store = app.discord_store
        app.discord_store = OrderStore()          # fresh, empty store per test
        self.addCleanup(setattr, app, "discord_store", self._old_store)

        self.svc = FakeService(app.discord_store)
        self._old_service = app.discord_service
        app.discord_service = self.svc
        self.addCleanup(setattr, app, "discord_service", self._old_service)

    def call(self, path, body=None):
        req = urllib.request.Request(self.base + path, data=None if body is None else json.dumps(body).encode(),
                                     headers={"Content-Type": "application/json"})
        try:
            with urllib.request.urlopen(req) as r:
                return r.status, r.read()
        except urllib.error.HTTPError as e:
            return e.code, e.read()

    def json(self, path, body=None):
        status, raw = self.call(path, body)
        return status, json.loads(raw)

    # ---- pages

    def test_pages_are_served(self):
        self.assertEqual(self.call("/")[0], 200)
        status, raw = self.call("/discord.html")
        self.assertEqual(status, 200)
        self.assertIn(b"Jigging Analysis", raw)
        self.assertEqual(self.call("/nope")[0], 404)

    def test_csv_analysis_still_works(self):
        status, data = self.json("/api/analyze", {"files": [{"name": "a.csv", "text": "Status,Source Email\nordered,a@x.com\n"}]})
        self.assertEqual((status, data["files"][0]["count"]), (200, 1))

    # ---- channels

    def test_channels_when_connected_and_not(self):
        self.assertTrue(self.json("/api/discord/channels")[1]["connected"])
        self.svc.connected = False
        data = self.json("/api/discord/channels")[1]
        self.assertEqual((data["connected"], data["guilds"]), (False, []))

    def test_selection_drops_channels_the_bot_cannot_read(self):
        data = self.json("/api/discord/channels/selection", {"channel_ids": [10, 999]})[1]
        self.assertEqual(data["selected"], ["10"])
        # and it's visible back on GET /channels, read from the shared store
        self.assertEqual(self.json("/api/discord/channels")[1]["selected"], ["10"])

    # ---- scan

    def test_scan_passes_the_users_date_range(self):
        status, data = self.json("/api/discord/scan", {"channel_ids": ["10"], "after": "2026-09-16", "before": "2026-09-16", "incremental": True})
        self.assertEqual((status, data), (200, {"scan_id": "42"}))
        call = self.svc.started[-1]
        self.assertEqual(call.date_range, DateRange.from_strings("2026-09-16", "2026-09-16"))
        self.assertTrue(call.incremental)

    def test_scan_without_dates_is_open_ended(self):
        self.json("/api/discord/scan", {"channel_ids": ["10"]})
        call = self.svc.started[-1]
        self.assertEqual((call.date_range, call.incremental), (DateRange(), False))

    def test_scan_needs_a_connection_and_valid_dates(self):
        self.svc.connected = False
        self.assertEqual(self.json("/api/discord/scan", {"channel_ids": ["10"]})[1], {"error": "Discord not connected"})
        self.svc.connected = True
        self.assertEqual(self.json("/api/discord/scan", {"channel_ids": ["10"], "after": "nope"})[0], 400)
        self.assertEqual(self.json("/api/discord/scan", {"channel_ids": ["10"], "after": "2026-09-20", "before": "2026-09-01"})[0], 400)

    def test_scan_progress(self):
        status, data = self.json("/api/discord/scan/42")
        self.assertEqual((status, data["status"], data["channels"]["10"]["rejected"]), (200, "done", 1))
        self.assertEqual(self.json("/api/discord/scan/7")[0], 404)

    # ---- search

    def test_search_filters_by_the_users_day_and_remembers_keywords(self):
        start, end = DateRange.from_strings("2026-09-16").start_day, DateRange.from_strings(end_text="2026-09-16").end_day
        app.discord_store.set_selected_ids(["10"])
        # Spoiler markup (||...||) is only ever stripped once, at parse time
        # (OrderParser.parse_order) - an Order already in the store is always
        # clean, since there's no persisted old data from before that existed.
        self._record("s1", "Co Mary A (1)", "Successful Checkout!", start.replace(hour=1), "checkout")
        self._record("s2", "Co Mary B (2)", "Order Canceled: Item Demand", end.replace(hour=0) - (end - start) / 2, "checkout")
        self._record("s3", "Co Mary OLD (9)", "Successful Checkout!", start - (end - start), "checkout")  # the day before

        status, data = self.json("/api/discord/profiles/search", {"names": ["co mary"], "after": "2026-09-16", "before": "2026-09-16"})
        self.assertEqual(status, 200)
        self.assertEqual(data["profilesPerStatus"], {"Successful Checkout!": ["Co Mary A (1)"], "Order Canceled: Item Demand": ["Co Mary B (2)"]})
        self.assertEqual(data["channelsPerProfile"], {"Co Mary A (1)": ["checkout"], "Co Mary B (2)": ["checkout"]})
        self.assertEqual(data["total"], 2)

        everything = self.json("/api/discord/profiles/search", {"names": ["co mary"]})[1]
        self.assertIn("Co Mary OLD (9)", everything["profilesPerStatus"]["Successful Checkout!"])

        self.json("/api/discord/profiles/search", {"names": ["kem", "jayden"]})
        self.assertEqual(self.json("/api/discord/search-terms")[1]["terms"], ["kem", "jayden"])

    def test_empty_search_means_everything_and_counts_per_term_are_reported(self):
        self._record("s1", "Co Mary A (1)", "Successful Checkout!", None, "checkout")
        self._record("s2", "Someone Else", "Successful Checkout!", None, "checkout")
        status, data = self.json("/api/discord/profiles/search", {"names": []})
        self.assertEqual(status, 200)
        self.assertEqual(data["total"], 2)

        status, data = self.json("/api/discord/profiles/search", {"names": ["Co Mary", "nobody"]})
        self.assertEqual(data["countPerSearchTerm"], {"Co Mary": 1, "nobody": 0})

    def test_search_works_while_discord_is_disconnected(self):
        self.svc.connected = False
        status, data = self.json("/api/discord/profiles/search", {"names": ["zzz"]})
        self.assertEqual((status, data["total"]), (200, 0))

    def test_bad_json_and_bad_dates_are_400(self):
        req = urllib.request.Request(self.base + "/api/discord/profiles/search", data=b"{not json", headers={"Content-Type": "application/json"})
        with self.assertRaises(urllib.error.HTTPError) as cm:
            urllib.request.urlopen(req)
        self.assertEqual(cm.exception.code, 400)
        self.assertEqual(self.json("/api/discord/profiles/search", {"names": ["a"], "before": "x"})[0], 400)

    # ---- helpers

    def _record(self, message_id, profile, status, when, channel_name):
        from datetime import datetime, timezone
        from messages import Order
        app.discord_store.record(Order(message_id, "10", channel_name, profile, status,
                                       when or datetime(2026, 9, 16, 12, tzinfo=timezone.utc)))


if __name__ == "__main__":
    unittest.main()
