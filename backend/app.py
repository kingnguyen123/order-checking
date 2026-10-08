import http.server
import json
import os
import socket
import sys
import webbrowser
from pathlib import Path

from order_analysis.order_analysis import rows_from_csv_text
from profile_finder.filter import parse_wanted_emails, filter_rows, rows_to_csv_text
from account_finder.filter import parse_wanted_emails as parse_wanted_account_emails, filter_lines, lines_to_text

# The Jigging Analysis modules (messages, discord_client) import each other
# by plain name, so their folder goes on the import path.
sys.path.insert(0, str(Path(__file__).parent / "jigging_analysis"))

from messages import DateRange, OrderGrouper, OrderParser, OrderStore

# Orders live in memory only (no database) - every app run starts fresh.
# OrderStore/OrderParser never import discord, so searching whatever's
# already been scanned this run keeps working even if discord.py/dotenv
# aren't installed; only the live connection and scanning are disabled then.
discord_store = OrderStore()
discord_parser = OrderParser()

try:
    from dotenv import load_dotenv
    load_dotenv(Path(__file__).resolve().parent.parent / ".env")   # project root .env, for DISCORD_TOKEN

    from discord_client import DiscordService, OrderBot
    discord_bot = OrderBot(discord_store, discord_parser)
    discord_service = DiscordService(discord_bot, discord_store, discord_parser,
                                      (os.environ.get("DISCORD_TOKEN") or "").strip())
except Exception as e:
    discord_service = None
    print(f"Discord integration unavailable ({e}). Install with: pip install -r requirements.txt")

HOST = "0.0.0.0"          # listen on every network interface, so other devices on the same LAN can reach it
LOCAL_HOST = "127.0.0.1"  # what this machine itself uses - for the port-availability check and auto-opened browser
DEFAULT_PORT = 8000
DISCORD_PAGE_FILE = Path(__file__).parent.parent / "frontend" / "discord.html"
PROJECT_ROOT = Path(__file__).parent.parent
FRONTEND_DIR = PROJECT_ROOT / "frontend"
FRONTEND_FILE = FRONTEND_DIR / "index.html"
PROFILE_FINDER_FILE = FRONTEND_DIR / "profile-finder.html"
ACCOUNT_FINDER_FILE = FRONTEND_DIR / "account-finder.html"


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
        elif self.path in ("/profile-finder", "/profile-finder.html"):
            self._serve_page(PROFILE_FINDER_FILE)
        elif self.path in ("/account-finder", "/account-finder.html"):
            self._serve_page(ACCOUNT_FINDER_FILE)
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
        elif self.path == "/api/profile-finder/filter":
            self._handle_profile_finder_filter()
        elif self.path == "/api/account-finder/filter":
            self._handle_account_finder_filter()
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
                        "address": r["address"],
                    }
                    for r in rows
                ],
            })
        self._send_json(200, {"files": results})

    # ---------- Profile Finder ----------

    def _handle_profile_finder_filter(self):
        """POST {csvText, emails} - split a pasted CSV's rows into the ones
        whose Email Address OR Profile Name matches the given list
        ("matches") and everything else ("rest"). Same filter.py code the
        command-line script uses, so results always match."""
        payload = self._read_json()
        if payload is None:
            return
        wanted = parse_wanted_emails(payload.get("emails") or "")
        if not wanted:
            self._send_json(400, {"error": "No emails given"})
            return
        try:
            fieldnames, matched_rows, rest_rows, seen_counts = filter_rows(payload.get("csvText") or "", wanted)
        except ValueError as e:
            self._send_json(400, {"error": str(e)})
            return

        not_found = [original for key, original in wanted.items() if key not in seen_counts]
        duplicated = [{"email": wanted[key], "count": count} for key, count in seen_counts.items() if count > 1]
        self._send_json(200, {
            "fieldnames": fieldnames,
            "rows": matched_rows,
            "emailCount": len(wanted),
            "foundCount": len(wanted) - len(not_found),
            "notFound": not_found,
            "duplicated": duplicated,
            "matchedCsvText": rows_to_csv_text(fieldnames, matched_rows),
            "restCsvText": rows_to_csv_text(fieldnames, rest_rows),
            "restCount": len(rest_rows),
        })

    # ---------- Account Finder ----------

    def _handle_account_finder_filter(self):
        """POST {accountsText, emails} - split a pasted "email:password" list
        into the lines whose email matches the given list ("matches") and
        everything else ("rest"). Same filter.py code the command-line
        script uses, so results always match. Lines are copied through
        unchanged - passwords are never parsed or reformatted."""
        payload = self._read_json()
        if payload is None:
            return
        wanted = parse_wanted_account_emails(payload.get("emails") or "")
        if not wanted:
            self._send_json(400, {"error": "No emails given"})
            return
        matched_lines, rest_lines, seen_counts = filter_lines(payload.get("accountsText") or "", wanted)

        not_found = [original for key, original in wanted.items() if key not in seen_counts]
        duplicated = [{"email": wanted[key], "count": count} for key, count in seen_counts.items() if count > 1]
        self._send_json(200, {
            "matchedCount": len(matched_lines),
            "restCount": len(rest_lines),
            "emailCount": len(wanted),
            "foundCount": len(wanted) - len(not_found),
            "notFound": not_found,
            "duplicated": duplicated,
            "matchedText": lines_to_text(matched_lines),
            "restText": lines_to_text(rest_lines),
        })

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
                "guilds": discord_service.get_channel_tree(),
                "selected": sorted(discord_store.get_selected_ids()),
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
            self._send_json(200, {"error": str(e), "selected": sorted(discord_store.get_selected_ids())})

    def _date_range(self, payload):
        """DateRange from the page's 'YYYY-MM-DD' strings. Raises ValueError
        with a page-facing message if the input is bad."""
        try:
            return DateRange.from_strings(payload.get("after"), payload.get("before"))
        except ValueError as e:
            if str(e).startswith("'from'"):
                raise
            raise ValueError("Dates must be in YYYY-MM-DD format") from e

    def _handle_discord_scan_start(self):
        """POST {channel_ids, after, before, incremental} - start a background scan, return its id."""
        payload = self._read_json()
        if payload is None:
            return
        if not self._discord_ready():
            self._send_json(200, {"error": "Discord not connected"})
            return
        try:
            date_range = self._date_range(payload)
        except ValueError as e:
            self._send_json(400, {"error": str(e)})
            return
        try:
            scan_id = discord_service.start_scan(
                payload.get("channel_ids", []), date_range,
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
        """POST {names, after, before} - search what's already been scanned this
        run. Works even while Discord is disconnected, since it only reads the
        in-memory store (there's no database - nothing survives a restart)."""
        payload = self._read_json()
        if payload is None:
            return
        try:
            date_range = self._date_range(payload)
        except ValueError as e:
            self._send_json(400, {"error": str(e)})
            return

        names = payload.get("names", [])
        discord_store.set_search_terms(names)   # remembered for next time
        orders = discord_store.search(names, date_range)
        grouper = OrderGrouper(orders)
        self._send_json(200, {
            "profilesPerStatus": grouper.profiles_per_status(),
            "ordersPerProfile": grouper.orders_per_profile(),
            "channelsPerProfile": grouper.channels_per_profile(),
            "countPerSearchTerm": grouper.count_per_search_term(names),
            "ordersPerProxyHost": grouper.orders_per_proxy_host(),
            "total": len(orders),
        })

    def _handle_discord_search_terms(self):
        """GET - the last-used search keywords, to prefill the search box."""
        self._send_json(200, {"terms": discord_store.get_search_terms()})

    def log_message(self, fmt, *args):
        print(f"  {self.address_string()} - {fmt % args}")


def find_open_port(start):
    port = start
    for _ in range(20):
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
            if s.connect_ex((LOCAL_HOST, port)) != 0:
                return port
        port += 1
    return start


def find_lan_ip():
    """This machine's address on the local network, for sharing with other
    devices (e.g. '192.168.1.23'). Doesn't actually send anything - opening
    a UDP socket "connected" to a public address just makes the OS pick
    which local network interface/IP would be used. Falls back to localhost
    (not shareable) if that fails, e.g. no network connection at all."""
    with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as s:
        try:
            s.connect(("8.8.8.8", 80))
            return s.getsockname()[0]
        except OSError:
            return LOCAL_HOST


def main():
    if not FRONTEND_FILE.exists():
        print(f"Could not find {FRONTEND_FILE} - is the frontend/ folder present?")
        return

    # An emoji in a Discord embed title/channel name would otherwise raise
    # UnicodeEncodeError on Windows' default console codepage and silently
    # kill whatever was printing it (e.g. mid-scan).
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(errors="backslashreplace")
        except Exception:
            pass

    port = find_open_port(DEFAULT_PORT)
    server = http.server.ThreadingHTTPServer((HOST, port), Handler)
    local_url = f"http://{LOCAL_HOST}:{port}/"
    lan_ip = find_lan_ip()

    print(f"Order Ledger running at {local_url}")
    if lan_ip != LOCAL_HOST:
        print(f"On the same network, others can open: http://{lan_ip}:{port}/")
        print("(Windows may ask to allow Python through the firewall the first time - allow it on Private networks.)")

    if discord_service is not None:
        try:
            discord_service.start_background()
        except Exception as e:
            print(f"Discord: startup failed unexpectedly ({e}). Continuing without Discord.")

    print("Press Ctrl+C to stop.\n")
    webbrowser.open(local_url)

    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nStopping...")
    finally:
        server.shutdown()


if __name__ == "__main__":
    main()
