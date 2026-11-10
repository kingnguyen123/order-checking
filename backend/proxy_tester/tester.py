#!/usr/bin/env python3
"""Speed test a list of proxies and rank them.

Install:
    pip install "requests[socks]"

Run:
    python backend/proxy_tester/tester.py -i proxies.txt
    python backend/proxy_tester/tester.py -i proxies.txt --threads 50 --timeout 15
    python backend/proxy_tester/tester.py -i proxies.txt --download-size 25
    python backend/proxy_tester/tester.py -i proxies.txt --no-download

    # Rank by whether a specific site works through each proxy
    python backend/proxy_tester/tester.py -i proxies.txt --no-download \
        --target-url https://www.target.com/ --target-text "Expect More"

    # No input file: paste proxies, then press Ctrl-D (Ctrl-Z + Enter on Windows)
    python backend/proxy_tester/tester.py

Accepted proxy formats, one per line (blank lines and # comments are ignored):
    ip:port
    ip:port:username:password
    username:password@ip:port
    http://user:pass@ip:port
    socks5://user:pass@ip:port

Lines without a scheme are treated as HTTP proxies.

Passwords are masked in the table and in results.csv.
"""

from __future__ import annotations

import argparse
import csv
import sys
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass
from typing import List, Optional, Tuple
from urllib.parse import quote, unquote, urlsplit

import requests

LATENCY_URL = "https://www.google.com/generate_204"
DOWNLOAD_URL = "https://speed.cloudflare.com/__down?bytes={bytes}"
IP_INFO_URL = "https://ipinfo.io/json"

LATENCY_SAMPLES = 3
CHUNK_SIZE = 64 * 1024
RESULTS_FILE = "results.csv"
TABLE_PROXY_WIDTH = 56
TARGET_READ_LIMIT = 2_000_000
LEADERBOARD_SIZE = 5

# socks5h / socks4a resolve hostnames on the proxy, so DNS does not leak locally.
SCHEME_ALIASES = {"socks5": "socks5h", "socks4": "socks4a"}
SUPPORTED_SCHEMES = {"http", "https", "socks4", "socks4a", "socks5", "socks5h"}


@dataclass(frozen=True)
class Proxy:
    scheme: str
    host: str
    port: int
    username: Optional[str] = None
    password: Optional[str] = None

    def _url(self, password: Optional[str]) -> str:
        auth = ""
        if self.username is not None:
            auth = quote(self.username, safe="")
            if password is not None:
                auth += f":{password}"
            auth += "@"
        return f"{self.scheme}://{auth}{self.host}:{self.port}"

    @property
    def url(self) -> str:
        """Full proxy URL, including the password."""
        password = None if self.password is None else quote(self.password, safe="")
        return self._url(password)

    @property
    def label(self) -> str:
        """Safe to print or save: the password is masked."""
        return self._url(None if self.password is None else "***")


@dataclass
class Result:
    proxy: Proxy
    alive: bool = False
    latency_ms: Optional[float] = None
    speed_mbps: Optional[float] = None
    exit_ip: Optional[str] = None
    country: Optional[str] = None
    target_passed: int = 0
    target_attempts: int = 0
    target_ms: Optional[float] = None
    error: str = ""


@dataclass(frozen=True)
class TargetCheck:
    """A site that must work through the proxy. A blocked page can still return
    status 200, so `expect_text` should be something only the real page contains."""

    url: str
    expect_text: str = ""
    attempts: int = 3


class TargetPageError(Exception):
    """The site responded, but not with the expected page."""


# --- Parsing ---------------------------------------------------------------


def parse_port(value: str) -> int:
    # Messages here are shown to the user, so they never quote the value:
    # on a badly formatted line it could be a password
    if not value.isdigit() or not 1 <= int(value) <= 65535:
        raise ValueError("invalid port")
    return int(value)


def parse_proxy(line: str) -> Proxy:
    """Turn one input line into a Proxy. Raises ValueError if it is malformed."""
    line = line.strip()

    if "://" not in line and "@" in line:
        line = f"http://{line}"

    if "://" in line:
        parts = urlsplit(line)
        scheme = parts.scheme.lower()
        if scheme not in SUPPORTED_SCHEMES:
            raise ValueError("unsupported scheme")
        try:
            port = parts.port
        except ValueError:
            raise ValueError("invalid port") from None
        if not parts.hostname or port is None:
            raise ValueError("missing host or port")
        return Proxy(
            scheme=SCHEME_ALIASES.get(scheme, scheme),
            host=parts.hostname,
            port=port,
            username=None if parts.username is None else unquote(parts.username),
            password=None if parts.password is None else unquote(parts.password),
        )

    # maxsplit keeps any colons that are part of the password
    fields = line.split(":", 3)
    if len(fields) == 2:
        return Proxy("http", fields[0], parse_port(fields[1]))
    if len(fields) == 4:
        return Proxy("http", fields[0], parse_port(fields[1]), fields[2], fields[3])
    raise ValueError("expected ip:port or ip:port:username:password")


def parse_lines(lines: List[str]) -> Tuple[List[Tuple[int, Proxy]], List[Tuple[int, str]]]:
    """Parse input lines into (line number, proxy) pairs and (line number, reason)
    pairs for the lines that were skipped. The lines themselves are not echoed
    because they may contain a password."""
    proxies, skipped = [], []
    for number, line in enumerate(lines, start=1):
        if not line.strip() or line.lstrip().startswith("#"):
            continue
        try:
            proxies.append((number, parse_proxy(line)))
        except ValueError as exc:
            skipped.append((number, str(exc)))
    return proxies, skipped


def load_proxies(path: Optional[str]) -> List[Proxy]:
    """Read proxies from a file, or from pasted stdin when no file is given."""
    if path:
        with open(path, encoding="utf-8-sig") as handle:
            lines = handle.readlines()
    else:
        if sys.stdin.isatty():
            print("Paste proxies, one per line, then press Ctrl-D:", file=sys.stderr)
        lines = sys.stdin.readlines()

    proxies, skipped = parse_lines(lines)
    for number, reason in skipped:
        print(f"Skipping line {number}: {reason}", file=sys.stderr)
    return [proxy for _, proxy in proxies]


def parse_target_url(url: str) -> str:
    url = url.strip()
    parts = urlsplit(url)
    if parts.scheme.lower() not in ("http", "https") or not parts.hostname:
        raise ValueError("Target URL must start with http:// or https://")
    return url


# --- Testing ---------------------------------------------------------------


def open_url(proxy: Proxy, url: str, timeout: float, stream: bool = False) -> requests.Response:
    """GET a URL through the proxy on a brand new connection."""
    session = requests.Session()
    # Stops HTTP_PROXY / NO_PROXY in the environment from changing the route
    session.trust_env = False
    session.proxies = {"http": proxy.url, "https": proxy.url}
    return session.get(url, timeout=timeout, stream=stream)


def describe_error(exc: Exception) -> str:
    """Short reason for a failure. Exception text is avoided as it can contain
    the proxy URL, and with it the password."""
    if isinstance(exc, TargetPageError):
        return "expected text missing"
    if isinstance(exc, requests.exceptions.Timeout):
        return "timeout"
    if isinstance(exc, requests.exceptions.ProxyError):
        return "proxy error"
    if isinstance(exc, requests.exceptions.SSLError):
        return "ssl error"
    if isinstance(exc, requests.exceptions.ConnectionError):
        return "connection error"
    if isinstance(exc, requests.exceptions.HTTPError):
        return f"http {exc.response.status_code}"
    return type(exc).__name__


def check_alive(proxy: Proxy, timeout: float) -> None:
    """Raise if a small request cannot complete through the proxy."""
    with open_url(proxy, LATENCY_URL, timeout) as response:
        response.raise_for_status()


def measure_latency(proxy: Proxy, timeout: float) -> Optional[float]:
    """Average time in ms for a small request. Failed samples are left out."""
    samples = []
    for _ in range(LATENCY_SAMPLES):
        started = time.perf_counter()
        try:
            with open_url(proxy, LATENCY_URL, timeout) as response:
                response.raise_for_status()
        except requests.RequestException:
            continue
        samples.append((time.perf_counter() - started) * 1000)
    return sum(samples) / len(samples) if samples else None


def measure_speed(proxy: Proxy, size_bytes: int, timeout: float) -> float:
    """Download speed in Mbps.

    The transfer stops once it has run for `timeout` seconds and the speed is
    worked out from what arrived, so a slow proxy still gets a real number.
    """
    url = DOWNLOAD_URL.format(bytes=size_bytes)
    with open_url(proxy, url, timeout, stream=True) as response:
        response.raise_for_status()
        started = time.perf_counter()
        received = 0
        for chunk in response.iter_content(CHUNK_SIZE):
            received += len(chunk)
            if time.perf_counter() - started >= timeout:
                break
        elapsed = time.perf_counter() - started
    if not received or not elapsed:
        raise ValueError("no data received")
    return received * 8 / 1_000_000 / elapsed


def lookup_exit(proxy: Proxy, timeout: float) -> Tuple[Optional[str], Optional[str]]:
    """Return the (ip, country) that the proxy exits from."""
    with open_url(proxy, IP_INFO_URL, timeout) as response:
        response.raise_for_status()
        info = response.json()
    return info.get("ip"), info.get("country")


def page_contains(response: requests.Response, text: str, timeout: float) -> bool:
    """Look for text in a streamed page, stopping as soon as it turns up so that
    a large page does not use up proxy data."""
    needle = text.lower().encode("utf-8")
    started = time.perf_counter()
    seen = b""
    for chunk in response.iter_content(CHUNK_SIZE):
        seen += chunk
        if needle in seen.lower():
            return True
        if len(seen) >= TARGET_READ_LIMIT or time.perf_counter() - started >= timeout:
            break
    return False


def fetch_target(proxy: Proxy, target: TargetCheck, timeout: float) -> None:
    """Raise unless the site returns the expected page through the proxy."""
    with open_url(proxy, target.url, timeout, stream=True) as response:
        response.raise_for_status()
        if target.expect_text and not page_contains(response, target.expect_text, timeout):
            raise TargetPageError()


def check_target(
    proxy: Proxy, target: TargetCheck, timeout: float
) -> Tuple[int, Optional[float], str]:
    """Try the site several times, as one pass says little about a proxy. Returns
    (attempts passed, average ms of those passes, reason for the last failure)."""
    samples = []
    problem = ""
    for _ in range(target.attempts):
        started = time.perf_counter()
        try:
            fetch_target(proxy, target, timeout)
        except Exception as exc:
            problem = describe_error(exc)
            continue
        samples.append((time.perf_counter() - started) * 1000)
    average = sum(samples) / len(samples) if samples else None
    return len(samples), average, problem


def test_proxy(
    proxy: Proxy,
    timeout: float,
    size_bytes: Optional[int],
    target: Optional[TargetCheck] = None,
) -> Result:
    """Run every check on one proxy. Only the alive check can mark it dead:
    a failed speed, target or IP lookup leaves that field blank."""
    result = Result(proxy)

    try:
        check_alive(proxy, timeout)
    except Exception as exc:
        result.error = describe_error(exc)
        return result
    result.alive = True

    result.latency_ms = measure_latency(proxy, timeout)

    problems = []
    if target is not None:
        result.target_attempts = target.attempts
        result.target_passed, result.target_ms, problem = check_target(proxy, target, timeout)
        if problem:
            problems.append(f"target: {problem}")

    if size_bytes is not None:
        try:
            result.speed_mbps = measure_speed(proxy, size_bytes, timeout)
        except Exception as exc:
            problems.append(f"speed: {describe_error(exc)}")

    try:
        result.exit_ip, result.country = lookup_exit(proxy, timeout)
    except Exception as exc:
        problems.append(f"ip: {describe_error(exc)}")

    result.error = "; ".join(problems)
    return result


def run_tests(
    proxies: List[Proxy],
    threads: int,
    timeout: float,
    size_bytes: Optional[int],
    target: Optional[TargetCheck] = None,
) -> List[Result]:
    results = []
    with ThreadPoolExecutor(max_workers=threads) as executor:
        futures = [executor.submit(test_proxy, p, timeout, size_bytes, target) for p in proxies]
        for done, future in enumerate(as_completed(futures), start=1):
            result = future.result()
            results.append(result)
            alive = sum(r.alive for r in results)
            print(
                f"\rTested {done}/{len(proxies)} ({alive} alive)",
                end="",
                file=sys.stderr,
                flush=True,
            )
    print(file=sys.stderr)
    return results


# --- Output ----------------------------------------------------------------


def sort_key(result: Result) -> Tuple:
    """Alive first, then best target pass rate and quickest target response,
    then fastest download, then lowest latency."""
    pass_rate = result.target_passed / result.target_attempts if result.target_attempts else 0
    return (
        not result.alive,
        -pass_rate,
        result.target_ms is None,
        result.target_ms or 0,
        result.speed_mbps is None,
        -(result.speed_mbps or 0),
        result.latency_ms is None,
        result.latency_ms or 0,
    )


def result_row(result: Result) -> List[str]:
    return [
        result.proxy.label,
        "alive" if result.alive else "dead",
        "" if result.latency_ms is None else f"{result.latency_ms:.0f}",
        "" if result.speed_mbps is None else f"{result.speed_mbps:.2f}",
        result.exit_ip or "",
        result.country or "",
    ]


def target_cells(result: Result) -> List[str]:
    if not result.target_attempts:
        return ["", ""]
    return [
        f"{result.target_passed}/{result.target_attempts}",
        "" if result.target_ms is None else f"{result.target_ms:.0f}",
    ]


def shorten(text: str, width: int) -> str:
    """Cut the middle out of long text, so the user and the host both stay visible."""
    if len(text) <= width:
        return text
    keep = width - 3
    return f"{text[:keep - keep // 2]}...{text[-(keep // 2):]}"


def print_table(results: List[Result]) -> None:
    headers = ["Proxy", "Status", "Latency (ms)", "Speed (Mbps)", "Exit IP", "Country"]
    show_target = any(result.target_attempts for result in results)
    if show_target:
        headers += ["Target", "Target (ms)"]

    rows = []
    for result in results:
        cells = result_row(result) + (target_cells(result) if show_target else [])
        row = [cell or "-" for cell in cells]
        row[0] = shorten(row[0], TABLE_PROXY_WIDTH)
        rows.append(row)

    widths = [max(len(row[i]) for row in [headers] + rows) for i in range(len(headers))]
    line = "  ".join(f"{{:<{width}}}" for width in widths)

    print(line.format(*headers))
    print("  ".join("-" * width for width in widths))
    for row in rows:
        print(line.format(*row))


def leaderboards(results: List[Result]) -> List[Tuple[str, List[Tuple[Result, str]]]]:
    """The top proxies for each test, as (title, [(result, score text)]) pairs.
    A test that was not run gets no board, and a proxy that failed a test is
    left off that test's board."""
    boards = []

    if any(result.target_attempts for result in results):
        passed = [result for result in results if result.target_passed]
        passed.sort(key=lambda r: (-r.target_passed / r.target_attempts, r.target_ms))
        boards.append((
            "Target site",
            [(r, f"{r.target_passed}/{r.target_attempts}, {r.target_ms:.0f} ms") for r in passed],
        ))

    timed = [result for result in results if result.latency_ms is not None]
    timed.sort(key=lambda r: r.latency_ms)
    boards.append(("Lowest latency", [(r, f"{r.latency_ms:.0f} ms") for r in timed]))

    measured = [result for result in results if result.speed_mbps is not None]
    if measured:
        measured.sort(key=lambda r: -r.speed_mbps)
        boards.append(("Fastest download", [(r, f"{r.speed_mbps:.2f} Mbps") for r in measured]))

    return [(title, entries[:LEADERBOARD_SIZE]) for title, entries in boards]


def print_leaderboards(results: List[Result]) -> None:
    for title, entries in leaderboards(results):
        print(f"\n{title}")
        if not entries:
            print("  No proxy passed this test")
        for rank, (result, score) in enumerate(entries, start=1):
            print(f"  {rank}. {shorten(result.proxy.label, TABLE_PROXY_WIDTH):<{TABLE_PROXY_WIDTH}}  {score}")


def save_csv(results: List[Result], path: str) -> None:
    with open(path, "w", newline="", encoding="utf-8") as handle:
        writer = csv.writer(handle)
        writer.writerow(
            ["proxy", "status", "latency_ms", "speed_mbps", "exit_ip", "country",
             "target_passed", "target_ms", "error"]
        )
        for result in results:
            writer.writerow(result_row(result) + target_cells(result) + [result.error])


# --- Command line ----------------------------------------------------------


def positive_int(value: str) -> int:
    number = int(value)
    if number < 1:
        raise argparse.ArgumentTypeError("must be at least 1")
    return number


def positive_float(value: str) -> float:
    number = float(value)
    if number <= 0:
        raise argparse.ArgumentTypeError("must be greater than 0")
    return number


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Speed test a list of proxies and rank them.")
    parser.add_argument("-i", "--input", help="file with one proxy per line (default: paste into stdin)")
    parser.add_argument("-t", "--threads", type=positive_int, default=20, help="parallel proxies (default: 20)")
    parser.add_argument("--timeout", type=positive_float, default=10, help="seconds per request (default: 10)")
    parser.add_argument("--download-size", type=positive_float, default=10, help="download size in MB (default: 10)")
    parser.add_argument("--no-download", action="store_true", help="skip the download speed test")
    parser.add_argument("--target-url", help="also test this site, and rank by how often it works")
    parser.add_argument("--target-text", default="", help="text the real page must contain, to catch block pages")
    parser.add_argument("--target-attempts", type=positive_int, default=3, help="requests to the site per proxy (default: 3)")
    return parser.parse_args()


def main() -> int:
    args = parse_args()

    try:
        proxies = load_proxies(args.input)
    except OSError as exc:
        print(f"Could not read input file: {exc}", file=sys.stderr)
        return 1
    if not proxies:
        print("No valid proxies to test.", file=sys.stderr)
        return 1

    target = None
    if args.target_url:
        try:
            target = TargetCheck(parse_target_url(args.target_url), args.target_text, args.target_attempts)
        except ValueError as exc:
            print(exc, file=sys.stderr)
            return 1

    size_bytes = None if args.no_download else int(args.download_size * 1_000_000)
    results = run_tests(proxies, args.threads, args.timeout, size_bytes, target)
    results.sort(key=sort_key)

    print_table(results)
    print_leaderboards(results)
    save_csv(results, RESULTS_FILE)
    print(f"\nSaved {len(results)} results to {RESULTS_FILE}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
