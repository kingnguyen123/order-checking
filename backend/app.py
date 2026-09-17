"""
Order Ledger web app - run with:

    python app.py

Starts a local web server that serves frontend/index.html and a small
JSON API the page uses to parse dropped CSV files on the Python side
(reusing the same rules as analyze_orders.py, via order_analysis.py).
Opens your browser automatically. Press Ctrl+C to stop.

Uses only the standard library, so there's nothing to install.
"""

import http.server
import json
import socket
import webbrowser
from pathlib import Path

from order_analysis import rows_from_csv_text

HOST = "127.0.0.1"
DEFAULT_PORT = 8000
FRONTEND_FILE = Path(__file__).parent / "frontend" / "index.html"


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
            if not FRONTEND_FILE.exists():
                self.send_error(404, "frontend/index.html not found")
                return
            body = FRONTEND_FILE.read_bytes()
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
        else:
            self.send_error(404)

    def do_POST(self):
        if self.path != "/api/analyze":
            self.send_error(404)
            return

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

    port = find_open_port(DEFAULT_PORT)
    server = http.server.ThreadingHTTPServer((HOST, port), Handler)
    url = f"http://{HOST}:{port}/"

    print(f"Order Ledger running at {url}")
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
