"""
Talks to the real web server (app.Handler) over HTTP, with a fake Discord
service behind it and a throwaway database. Nothing connects to Discord.
"""
import http.server
import json
import sys
import tempfile
import threading
import unittest
import urllib.error
import urllib.request
from pathlib import Path
from types import SimpleNamespace as NS

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "backend"))

import app        # noqa: E402
import storage    # noqa: E402  (the same module app.py uses)
import dates      # noqa: E402


class FakeService:
    """Same functions app.py calls on service.py, but answering from memory."""
    def __init__(self, connected=True):
        self.connected = connected
        self.started = []
        self.selected = ["10"]

    def is_connected(self): return self.connected
    def get_channel_tree_sync(self):
        return [{"id": "1", "name": "G", "categories": [{"id": "c", "name": "Orders", "channels": [{"id": "10", "name": "checkout"}]}]}]
    def get_selected_channel_ids(self): return self.selected
    def set_selected_channels(self, ids):
        self.selected = [i for i in map(str, ids) if i == "10"]       # only 10 is "accessible"
        return self.selected
    def start_scan(self, ids, after=None, before=None, incremental=False):
        self.started.append(NS(ids=ids, after=after, before=before, incremental=incremental))
        return "42"
    def get_scan_progress(self, scan_id):
        return {"status": "complete", "channels": {"10": {"name": "checkout", "messages_scanned": 3, "orders_found": 2,
                "new_records": 2, "rejected": 1, "done": True, "error": None}}} if scan_id == "42" else None


class HttpTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        cls._old_db = storage.DB_PATH
        storage.DB_PATH = Path(cls.tmp.name) / "t.db"
        storage.init_db()
        cls.server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), app.Handler)
        cls.base = f"http://127.0.0.1:{cls.server.server_address[1]}"
        threading.Thread(target=cls.server.serve_forever, daemon=True).start()

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()
        cls.server.server_close()
        storage.DB_PATH = cls._old_db
        cls.tmp.cleanup()

    def setUp(self):
        self.svc = FakeService()
        self._old = app.discord_service
        app.discord_service = self.svc
        self.addCleanup(setattr, app, "discord_service", self._old)

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

    # ---- scan

    def test_scan_passes_the_users_local_day_bounds(self):
        status, data = self.json("/api/discord/scan", {"channel_ids": ["10"], "after": "2026-09-16", "before": "2026-09-16", "incremental": True})
        self.assertEqual((status, data), (200, {"scan_id": "42"}))
        call = self.svc.started[-1]
        self.assertEqual(call.after, dates.day_bound("2026-09-16"))
        self.assertEqual(call.before, dates.day_bound("2026-09-16", end_of_day=True))
        self.assertTrue(call.incremental)

    def test_scan_without_dates_is_open_ended(self):
        self.json("/api/discord/scan", {"channel_ids": ["10"]})
        call = self.svc.started[-1]
        self.assertEqual((call.after, call.before, call.incremental), (None, None, False))

    def test_scan_needs_a_connection_and_valid_dates(self):
        self.svc.connected = False
        self.assertEqual(self.json("/api/discord/scan", {"channel_ids": ["10"]})[1], {"error": "Discord not connected"})
        self.svc.connected = True
        self.assertEqual(self.json("/api/discord/scan", {"channel_ids": ["10"], "after": "nope"})[0], 400)
        self.assertEqual(self.json("/api/discord/scan", {"channel_ids": ["10"], "after": "2026-09-20", "before": "2026-09-01"})[0], 400)

    def test_scan_progress(self):
        status, data = self.json("/api/discord/scan/42")
        self.assertEqual((status, data["status"], data["channels"]["10"]["rejected"]), (200, "complete", 1))
        self.assertEqual(self.json("/api/discord/scan/7")[0], 404)

    # ---- search

    def test_search_filters_by_the_users_day_and_remembers_keywords(self):
        start, end = dates.day_bound("2026-09-16"), dates.day_bound("2026-09-16", end_of_day=True)
        storage.record("s1", "10", "||Co Mary A (1)||", "Successful Checkout!", dates.to_utc_iso(start.replace(hour=1)))
        storage.record("s2", "10", "Co Mary B (2)", "Order Canceled: Item Demand", dates.to_utc_iso(end.replace(hour=0) - (end - start) / 2))
        storage.record("s3", "10", "Co Mary OLD (9)", "Successful Checkout!", dates.to_utc_iso(start - (end - start)))   # the day before

        status, data = self.json("/api/discord/profiles/search", {"names": ["co mary"], "after": "2026-09-16", "before": "2026-09-16"})
        self.assertEqual(status, 200)
        self.assertEqual(data["byStatus"], {"Successful Checkout!": ["Co Mary A (1)"], "Order Canceled: Item Demand": ["Co Mary B (2)"]})

        everything = self.json("/api/discord/profiles/search", {"names": ["co mary"]})[1]
        self.assertIn("Co Mary OLD (9)", everything["byStatus"]["Successful Checkout!"])

        self.json("/api/discord/profiles/search", {"names": ["kem", "jayden"]})
        self.assertEqual(self.json("/api/discord/search-terms")[1]["terms"], ["kem", "jayden"])

    def test_search_works_while_discord_is_disconnected(self):
        self.svc.connected = False
        status, data = self.json("/api/discord/profiles/search", {"names": ["zzz"]})
        self.assertEqual((status, data["byStatus"]), (200, {}))

    def test_bad_json_and_bad_dates_are_400(self):
        req = urllib.request.Request(self.base + "/api/discord/profiles/search", data=b"{not json", headers={"Content-Type": "application/json"})
        with self.assertRaises(urllib.error.HTTPError) as cm:
            urllib.request.urlopen(req)
        self.assertEqual(cm.exception.code, 400)
        self.assertEqual(self.json("/api/discord/profiles/search", {"names": ["a"], "before": "x"})[0], 400)


if __name__ == "__main__":
    unittest.main()
