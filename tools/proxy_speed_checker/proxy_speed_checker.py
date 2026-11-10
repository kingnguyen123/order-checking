#!/usr/bin/env python3
"""Proxy speed checker.

Paste proxies (host:port:username:password, one per line) into the terminal,
finish with an empty line, and every line is tested concurrently through
aiohttp. Fast proxies are written to fast_proxies.txt (full lines, fastest
first) and every result goes to results.csv (session ID only, no password).
"""

import argparse
import asyncio
import csv
import re
import sys
import time
from dataclasses import dataclass
from typing import Callable, Iterable, List, Optional, Tuple

try:
    import aiohttp
except ImportError:  # pragma: no cover - friendly message for a missing dependency
    if __name__ == "__main__":
        sys.exit("aiohttp is not installed. Run:  pip install -r requirements.txt")
    raise  # imported by the web app, which handles this itself

IPINFO_URL = "https://ipinfo.io/json"
SESSION_RE = re.compile(r"session-([A-Za-z0-9]+)")


@dataclass
class Proxy:
    index: int  # 1-based position in the pasted list
    line: str  # cleaned line exactly as it will be written back out
    host: str
    port: int
    username: str
    password: str
    line_number: int = 0  # 1-based line in the pasted text

    @property
    def session_id(self) -> str:
        match = SESSION_RE.search(self.username)
        return match.group(1) if match else "line-{}".format(self.index)

    @property
    def url(self) -> str:
        return "http://{}:{}".format(self.host, self.port)


@dataclass
class Result:
    proxy: Proxy
    status: str = "failed"  # "fast", "slow" or "failed"
    avg_ms: Optional[float] = None
    exit_ip: str = ""
    country: str = ""
    error: str = ""


# --------------------------------------------------------------------------- input


def parse_line(raw: str, index: int) -> Optional[Proxy]:
    """Parse host:port:username:password. Returns None if the line is invalid."""
    line = "".join(raw.split())  # drop all whitespace, proxies never contain spaces
    parts = line.split(":", 3)  # exactly 4 parts; username keeps its _ and -
    if len(parts) != 4 or not all(parts):
        return None
    host, port, username, password = parts
    if not port.isdigit() or not 0 < int(port) < 65536:
        return None
    return Proxy(index, line, host, int(port), username, password)


def parse_proxy_lines(lines: Iterable[str]) -> Tuple[List[Proxy], int, List[int]]:
    """Parse pasted lines, skipping blanks and exact duplicates.

    Returns (proxies, duplicate_count, invalid_line_numbers).
    """
    seen = set()
    proxies: List[Proxy] = []
    duplicates = 0
    invalid: List[int] = []
    for line_number, raw in enumerate(lines, 1):
        if not raw.strip():
            continue
        proxy = parse_line(raw, len(proxies) + 1)
        if proxy is None:
            invalid.append(line_number)
            continue
        if proxy.line in seen:
            duplicates += 1
            continue
        seen.add(proxy.line)
        proxy.line_number = line_number
        proxies.append(proxy)
    return proxies, duplicates, invalid


def read_proxies() -> Tuple[List[Proxy], int, int]:
    """Read pasted lines until an empty line (or EOF).

    Blank lines before the first proxy are ignored so a stray Enter doesn't end
    input early. Returns (proxies, duplicate_count, invalid_count).
    """
    interactive = sys.stdin.isatty()
    if interactive:
        print("Paste your proxies (host:port:username:password), one per line.")
        print("Press Enter on an empty line when you're done.\n")

    def pasted_lines():
        started = False
        for raw in sys.stdin:
            if not raw.strip():
                if started and interactive:
                    return
            else:
                started = True
            yield raw

    proxies, duplicates, invalid = parse_proxy_lines(pasted_lines())
    if invalid:
        # Line numbers only, so a malformed line never leaks a password.
        shown = ", ".join(str(n) for n in invalid[:10])
        more = " and {} more".format(len(invalid) - 10) if len(invalid) > 10 else ""
        print("  skipped invalid lines: {}{}".format(shown, more))
    return proxies, duplicates, len(invalid)


# --------------------------------------------------------------------------- testing


def describe_error(exc: BaseException) -> str:
    if isinstance(exc, asyncio.TimeoutError):
        return "timeout"
    if isinstance(exc, aiohttp.ClientProxyConnectionError):
        return "proxy connection error"
    if isinstance(exc, aiohttp.ClientHttpProxyError):
        return "proxy error {}".format(getattr(exc, "status", ""))
    if isinstance(exc, aiohttp.ClientResponseError):
        return "HTTP {}".format(exc.status)
    name = type(exc).__name__
    text = str(exc).splitlines()[0][:80] if str(exc) else ""
    return "{}: {}".format(name, text) if text else name


async def timed_get(session: aiohttp.ClientSession, proxy: Proxy, url: str) -> float:
    """GET url through the proxy and return milliseconds until the response headers.

    The body isn't downloaded, so the test costs very little proxy bandwidth.
    """
    auth = aiohttp.BasicAuth(proxy.username, proxy.password)
    start = time.perf_counter()
    async with session.get(url, proxy=proxy.url, proxy_auth=auth, allow_redirects=True) as resp:
        elapsed = (time.perf_counter() - start) * 1000
        if resp.status >= 400:
            raise aiohttp.ClientResponseError(
                resp.request_info, resp.history, status=resp.status, message=resp.reason or ""
            )
        return elapsed


async def lookup_exit(session: aiohttp.ClientSession, proxy: Proxy) -> Tuple[str, str]:
    auth = aiohttp.BasicAuth(proxy.username, proxy.password)
    try:
        async with session.get(IPINFO_URL, proxy=proxy.url, proxy_auth=auth) as resp:
            data = await resp.json(content_type=None)
            return str(data.get("ip", "")), str(data.get("country", ""))
    except Exception:
        return "", ""


def make_session(args: argparse.Namespace) -> aiohttp.ClientSession:
    """One session shared by every test.

    force_close means no connection is ever reused, so lines that share host:port
    can't ride on each other's proxy session. Sharing the connector still lets the
    proxy host's DNS lookup be cached, instead of being redone (and timed) per proxy.
    """
    connector = aiohttp.TCPConnector(force_close=True, limit=0, ttl_dns_cache=600)
    return aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=args.timeout), connector=connector)


async def test_proxy(session: aiohttp.ClientSession, proxy: Proxy, args: argparse.Namespace) -> Result:
    result = Result(proxy)
    try:
        first = await timed_get(session, proxy, args.url)
    except Exception as exc:
        result.error = describe_error(exc)
        return result

    times = [first]
    try:
        times.append(await timed_get(session, proxy, args.url))
    except Exception as exc:
        # It worked once, so it still counts as working; keep the first timing.
        result.error = "retest failed ({})".format(describe_error(exc))
    result.avg_ms = sum(times) / len(times)
    result.status = "fast" if result.avg_ms < args.threshold else "slow"

    # Looked up only after timing is done: running it alongside the retest saves a
    # little time but the extra in-flight TLS handshake inflates the measurement.
    result.exit_ip, result.country = await lookup_exit(session, proxy)
    return result


async def run_all(
    proxies: List[Proxy],
    args: argparse.Namespace,
    on_result: Optional[Callable[[Result], None]] = None,
    should_stop: Optional[Callable[[], bool]] = None,
) -> List[Result]:
    """Test every proxy, at most args.concurrency at a time.

    on_result is called as each proxy finishes. If should_stop returns True,
    proxies that haven't started yet are skipped (and left out of the results).
    """
    semaphore = asyncio.Semaphore(args.concurrency)

    async with make_session(args) as session:

        async def worker(proxy: Proxy) -> Optional[Result]:
            async with semaphore:
                if should_stop is not None and should_stop():
                    return None
                result = await test_proxy(session, proxy, args)
            if on_result is not None:
                on_result(result)
            return result

        results = await asyncio.gather(*(worker(p) for p in proxies))
    return [r for r in results if r is not None]


def terminal_progress(total: int) -> Callable[[Result], None]:
    counts = {"done": 0, "working": 0}

    def report(result: Result) -> None:
        counts["done"] += 1
        if result.status != "failed":
            counts["working"] += 1
        sys.stderr.write(
            "\r  Testing... {}/{}  working: {}  failed: {}   ".format(
                counts["done"], total, counts["working"], counts["done"] - counts["working"]
            )
        )
        sys.stderr.flush()

    return report


# --------------------------------------------------------------------------- output


def write_outputs(results: List[Result], fast: List[Result], args: argparse.Namespace) -> None:
    with open(args.fast_file, "w", encoding="utf-8") as fh:
        for r in fast:
            fh.write(r.proxy.line + "\n")

    ordered = sorted(
        results,
        key=lambda r: (r.avg_ms is None, r.avg_ms if r.avg_ms is not None else 0, r.proxy.index),
    )
    with open(args.csv_file, "w", newline="", encoding="utf-8") as fh:
        writer = csv.writer(fh)
        writer.writerow(["session_id", "status", "avg_ms", "exit_ip", "country", "error"])
        for r in ordered:
            writer.writerow([
                r.proxy.session_id,
                r.status,
                "" if r.avg_ms is None else "{:.0f}".format(r.avg_ms),
                r.exit_ip,
                r.country,
                r.error,
            ])


def print_report(results: List[Result], fast: List[Result], args: argparse.Namespace) -> None:
    working = [r for r in results if r.avg_ms is not None]
    failed = [r for r in results if r.avg_ms is None]

    print()
    if fast:
        print("Fast proxies (< {:.0f} ms), fastest first:".format(args.threshold))
        width = max(len(r.proxy.session_id) for r in fast)
        width = max(width, len("SESSION"))
        print("  {:>4}  {:<{w}}  {:>8}  {:<15}  {}".format("#", "SESSION", "AVG MS", "EXIT IP", "COUNTRY", w=width))
        for i, r in enumerate(fast, 1):
            print("  {:>4}  {:<{w}}  {:>8.0f}  {:<15}  {}".format(
                i, r.proxy.session_id, r.avg_ms, r.exit_ip or "-", r.country or "-", w=width
            ))
        if args.show_full:
            print("\nCopy-ready list:\n")
            for r in fast:
                print(r.proxy.line)
    else:
        print("No proxies were faster than {:.0f} ms.".format(args.threshold))

    if failed:
        reasons = {}
        for r in failed:
            reasons[r.error] = reasons.get(r.error, 0) + 1
        top = sorted(reasons.items(), key=lambda kv: -kv[1])[:5]
        print("\nFailure reasons: " + ", ".join("{} x{}".format(k, v) for k, v in top))

    print("\nSummary")
    print("  Total tested : {}".format(len(results)))
    print("  Working      : {}".format(len(working)))
    print("  Fast         : {}".format(len(fast)))
    print("  Failed       : {}".format(len(failed)))
    if working:
        times = [r.avg_ms for r in working]
        print("  Fastest      : {:.0f} ms".format(min(times)))
        print("  Average      : {:.0f} ms".format(sum(times) / len(times)))
        print("  Slowest      : {:.0f} ms".format(max(times)))
    print("\nSaved {} fast proxies (full lines) to {}".format(len(fast), args.fast_file))
    print("Saved all results to {}".format(args.csv_file))


# --------------------------------------------------------------------------- main


def parse_args(argv: Optional[List[str]] = None) -> argparse.Namespace:
    p = argparse.ArgumentParser(description="Test proxy speed concurrently.")
    p.add_argument("--url", default="https://www.fastly.com", help="URL to test (default: %(default)s)")
    p.add_argument("--timeout", type=float, default=5.0, help="seconds per request (default: %(default)s)")
    p.add_argument("--threshold", type=float, default=1000.0, help="max avg ms to count as fast (default: %(default)s)")
    p.add_argument("--concurrency", type=int, default=100, help="proxies tested at once (default: %(default)s)")
    p.add_argument("--fast-file", default="fast_proxies.txt", help="output for fast proxies (default: %(default)s)")
    p.add_argument("--csv-file", default="results.csv", help="output for all results (default: %(default)s)")
    p.add_argument("--show-full", action="store_true",
                   help="also print the fast proxies as full lines (includes passwords) in the terminal")
    args = p.parse_args(argv)
    if args.concurrency < 1:
        p.error("--concurrency must be at least 1")
    if args.timeout <= 0:
        p.error("--timeout must be greater than 0")
    return args


def main() -> None:
    args = parse_args()
    proxies, duplicates, invalid = read_proxies()
    print("\nLoaded {} proxies ({} duplicates removed, {} invalid lines skipped).".format(
        len(proxies), duplicates, invalid
    ))
    if not proxies:
        sys.exit("Nothing to test.")
    print("Testing against {} | timeout {}s | fast < {:.0f} ms | concurrency {}\n".format(
        args.url, args.timeout, args.threshold, args.concurrency
    ), flush=True)

    results = asyncio.run(run_all(proxies, args, on_result=terminal_progress(len(proxies))))
    sys.stderr.write("\n")
    fast = sorted((r for r in results if r.status == "fast"), key=lambda r: r.avg_ms)
    write_outputs(results, fast, args)
    print_report(results, fast, args)


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        sys.exit("\nCancelled.")
