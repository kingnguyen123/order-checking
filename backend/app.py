import http.server
import json
import socket
import sys
import webbrowser
from pathlib import Path

from order_analysis.order_analysis import rows_from_csv_text

# The Jigging Analysis modules (parser, storage, scan, ...) import each other
# by plain name, so their folder goes on the import path.
sys.path.insert(0, str(Path(__file__).parent / "jigging_analysis"))

import analysis
import storage
from dates import day_bound, to_utc_iso

# service.py needs discord.py. If it isn't installed the rest of the app
# (CSV analysis, and searching already-saved Discord data) still works.
try:
    import service as discord_service
except Exception as e:
    discord_service = None
    print(f"Discord integration unavailable ({e}). Install with: pip install -r requirements.txt")

HOST = "127.0.0.1"
DEFAULT_PORT = 8000
DISCORD_PAGE_FILE = Path(__file__).parent.parent / "frontend" / "discord.html"
PROJECT_ROOT = Path(__file__).parent.parent
FRONTEND_DIR = PROJECT_ROOT / "frontend"
FRONTEND_FILE = FRONTEND_DIR / "index.html"


class Handler(http.server.BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def _send_json(self, status, payload):
        body = json.dumps(payload).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        if self.path in ("/", "/index.html"):
            self._serve_page(FRONTEND_FILE)
        elif self.path in ("/discord", "/discord.html"):
            self._serve_page(DISCORD_PAGE_FILE)
        elif self.path == "/api/discord/channels":
            self._handle_discord_channels()
        elif self.path == "/api/discord/search-terms":
            self._handle_discord_search_terms()
        elif self.path.startswith("/api/discord/scan/"):
            self._handle_discord_scan_progress(self.path[len("/api/discord/scan/"):])
        else:
            self.send_error(404)

    def _serve_page(self, file_path):
        if not file_path.exists():
            self.send_error(404, f"{file_path.name} not found")
            return
        body = file_path.read_bytes()
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_POST(self):
        if self.path == "/api/analyze":
            self._handle_analyze()
        elif self.path == "/api/discord/channels/selection":
            self._handle_discord_selection()
        elif self.path == "/api/discord/scan":
            self._handle_discord_scan_start()
        elif self.path == "/api/discord/profiles/search":
            self._handle_discord_profile_search()
        else:
            self.send_error(404)

    def _read_json(self):
        """The request body as a dict, or None (after sending a 400) if it isn't valid JSON."""
        length = int(self.headers.get("Content-Length", 0) or 0)
        raw = self.rfile.read(length) if length else b"{}"
        try:
            return json.loads(raw or b"{}")
        except json.JSONDecodeError:
            self._send_json(400, {"error": "Invalid JSON"})
            return None

    def _handle_analyze(self):
        length = int(self.headers.get("Content-Length", 0) or 0)
        raw = self.rfile.read(length) if length else b"{}"
        try:
            payload = json.loads(raw or b"{}")
        except json.JSONDecodeError:
            self._send_json(400, {"error": "Invalid JSON"})
            return

        results = []
        for f in payload.get("files", []):
            name = str(f.get("name") or "upload.csv")
            text = f.get("text") or ""
            rows, warning = rows_from_csv_text(name, text)
            results.append({
                "name": name,
                "count": len(rows),
                "warning": warning,
                "rows": [
                    {
                        "email": r["email"],
                        "status": r["status"],
                        "statusRaw": r["status_raw"],
                        "dateRaw": r["date_raw"],
                        "product": r["product"],
                        "retailer": r["retailer"],
                    }
                    for r in rows
                ],
            })
        self._send_json(200, {"files": results})

    # ---------- Jigging Analysis (Discord) ----------

    def _discord_ready(self):
        return discord_service is not None and discord_service.is_connected()

    def _handle_discord_channels(self):
        """GET - the server/category/channel tree the bot can read, plus which are selected."""
        if not self._discord_ready():
            self._send_json(200, {"connected": False, "guilds": [], "selected": []})
            return
        try:
            self._send_json(200, {
                "connected": True,
                "guilds": discord_service.get_channel_tree_sync(),
                "selected": discord_service.get_selected_channel_ids(),
            })
        except Exception as e:
            self._send_json(200, {"connected": False, "error": str(e), "guilds": [], "selected": []})

    def _handle_discord_selection(self):
        """POST {channel_ids} - remember which channels to scan/monitor."""
        payload = self._read_json()
        if payload is None:
            return
        if not self._discord_ready():
            self._send_json(200, {"error": "Discord not connected", "selected": []})
            return
        try:
            self._send_json(200, {"selected": discord_service.set_selected_channels(payload.get("channel_ids", []))})
        except Exception as e:
            self._send_json(200, {"error": str(e), "selected": discord_service.get_selected_channel_ids()})

    def _date_range(self, payload):
        """(after, before) from the page's 'YYYY-MM-DD' strings, as aware local
        datetimes (before = the start of the day AFTER the picked end day).
        Raises ValueError with a message for the page if the input is bad."""
        try:
            after = day_bound(payload.get("after"))
            before = day_bound(payload.get("before"), end_of_day=True)
        except ValueError:
            raise ValueError("Dates must be in YYYY-MM-DD format")
        if after and before and after >= before:
            raise ValueError("'From' date must be before 'To' date")
        return after, before

    def _handle_discord_scan_start(self):
        """POST {channel_ids, after, before, incremental} - start a background scan, return its id."""
        payload = self._read_json()
        if payload is None:
            return
        if not self._discord_ready():
            self._send_json(200, {"error": "Discord not connected"})
            return
        try:
            after, before = self._date_range(payload)
        except ValueError as e:
            self._send_json(400, {"error": str(e)})
            return
        try:
            scan_id = discord_service.start_scan(
                payload.get("channel_ids", []), after=after, before=before,
                incremental=bool(payload.get("incremental")),
            )
            self._send_json(200, {"scan_id": scan_id})
        except Exception as e:
            self._send_json(200, {"error": str(e)})

    def _handle_discord_scan_progress(self, scan_id):
        """GET - progress of a scan started above (the page polls this)."""
        progress = discord_service.get_scan_progress(scan_id) if discord_service else None
        if progress is None:
            self._send_json(404, {"error": "Unknown scan_id"})
            return
        self._send_json(200, progress)

    def _handle_discord_profile_search(self):
        """POST {names, after, before} - search what's already saved. Works even
        while Discord is disconnected, because it only reads the local database."""
        payload = self._read_json()
        if payload is None:
            return
        try:
            after, before = self._date_range(payload)
        except ValueError as e:
            self._send_json(400, {"error": str(e)})
            return

        names = payload.get("names", [])
        try:
            rows = storage.search_orders_by_profile_terms(names, after=to_utc_iso(after), before=to_utc_iso(before))
            self._send_json(200, {
                "byStatus": analysis.group_by_status(rows),
                "byProfile": analysis.group_by_profile(rows),
            })
        except Exception as e:
            self._send_json(200, {"error": str(e), "byStatus": {}, "byProfile": {}})
            return
        try:
            storage.set_search_terms(names)     # remembered for next time; never worth failing a search over
        except Exception:
            pass

    def _handle_discord_search_terms(self):
        """GET - the last-used search keywords, to prefill the search box."""
        try:
            self._send_json(200, {"terms": storage.get_search_terms()})
        except Exception as e:
            self._send_json(200, {"terms": [], "error": str(e)})

    def log_message(self, fmt, *args):
        print(f"  {self.address_string()} - {fmt % args}")


def find_open_port(start):
    port = start
    for _ in range(20):
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
            if s.connect_ex((HOST, port)) != 0:
                return port
        port += 1
    return start


def main():
    if not FRONTEND_FILE.exists():
        print(f"Could not find {FRONTEND_FILE} - is the frontend/ folder present?")
        return

    try:
        storage.init_db()
    except Exception as e:
        print(f"Discord: local database unavailable ({e}). Profile search will be empty until this is fixed.")

    port = find_open_port(DEFAULT_PORT)
    server = http.server.ThreadingHTTPServer((HOST, port), Handler)
    url = f"http://{HOST}:{port}/"

    print(f"Order Ledger running at {url}")

    if discord_service is not None:
        try:
            discord_service.start_background()
        except Exception as e:
            print(f"Discord: startup failed unexpectedly ({e}). Continuing without Discord.")

    print("Press Ctrl+C to stop.\n")
    webbrowser.open(url)

    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nStopping...")
    finally:
        server.shutdown()


if __name__ == "__main__":
    main()
