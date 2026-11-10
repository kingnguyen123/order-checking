"""
Runs proxy speed checks in the background for the Speed Check page, which
polls for progress. The testing itself is the terminal tool in
tools/proxy_speed_checker. Nothing touches disk, and only session IDs (never
passwords) are kept in results or sent to the browser; the page rebuilds full
proxy lines from the text it still holds.
"""
import asyncio
import sys
import threading
import uuid
from pathlib import Path
from types import SimpleNamespace

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "tools" / "proxy_speed_checker"))

from proxy_speed_checker import parse_proxy_lines, run_all  # noqa: E402  (raises ImportError without aiohttp)

DEFAULT_URL = "https://www.fastly.com"
MAX_CONCURRENCY = 500
MAX_TIMEOUT = 60
KEEP_FINISHED_JOBS = 5


def clamp(value, low, high):
    return max(low, min(high, value))


def result_to_row(result):
    return {
        "line": result.proxy.line_number,   # lets the page find the original pasted line
        "session": result.proxy.session_id,
        "status": result.status,            # fast, slow or failed
        "avgMs": None if result.avg_ms is None else round(result.avg_ms),
        "exitIp": result.exit_ip,
        "country": result.country,
        "error": result.error,
    }


def sort_key(row):
    # Working proxies fastest first, then failed ones in pasted order
    return (row["avgMs"] is None, row["avgMs"] or 0, row["line"])


class SpeedCheckJob:
    def __init__(self, total, duplicates, invalid_lines, url, threshold):
        self.id = uuid.uuid4().hex
        self.total = total
        self.duplicates = duplicates
        self.invalid_lines = invalid_lines
        self.url = url
        self.threshold = threshold
        self.state = "running"   # running -> stopping -> stopped, or running -> finished
        self.stop_requested = threading.Event()
        self.lock = threading.Lock()
        self.rows = []

    def add(self, result):
        with self.lock:
            self.rows.append(result_to_row(result))

    def request_stop(self):
        self.stop_requested.set()
        with self.lock:
            if self.state == "running":
                self.state = "stopping"

    def finish(self):
        with self.lock:
            self.state = "stopped" if self.stop_requested.is_set() else "finished"

    def is_active(self):
        with self.lock:
            return self.state in ("running", "stopping")

    def progress(self):
        with self.lock:
            return {
                "state": self.state,
                "total": self.total,
                "done": len(self.rows),
                "duplicates": self.duplicates,
                "invalidLines": self.invalid_lines,
                "url": self.url,
                "threshold": self.threshold,
                "rows": sorted(self.rows, key=sort_key),
            }


class SpeedCheckService:
    def __init__(self):
        self.jobs = {}
        self.lock = threading.Lock()

    def start(self, text, url=DEFAULT_URL, timeout=5, threshold=1000, concurrency=100):
        """Start checking the pasted proxies and return the job.
        Raises ValueError if no line is a valid proxy or the URL is bad."""
        proxies, duplicates, invalid = parse_proxy_lines(text.split("\n"))
        if not proxies:
            raise ValueError("No valid proxies to test. Use host:port:username:password, one per line.")
        url = (url or DEFAULT_URL).strip()
        if not url.startswith(("http://", "https://")):
            raise ValueError("Test URL must start with http:// or https://")

        args = SimpleNamespace(
            url=url,
            timeout=clamp(float(timeout), 1, MAX_TIMEOUT),
            threshold=max(1.0, float(threshold)),
            concurrency=clamp(int(concurrency), 1, MAX_CONCURRENCY),
        )
        job = SpeedCheckJob(len(proxies), duplicates, invalid, url, args.threshold)
        with self.lock:
            self._prune_finished_jobs()
            self.jobs[job.id] = job
        threading.Thread(target=self._run, args=(job, proxies, args), daemon=True).start()
        return job

    def _prune_finished_jobs(self):
        finished = [job_id for job_id, job in self.jobs.items() if not job.is_active()]
        for job_id in finished[:-KEEP_FINISHED_JOBS]:
            del self.jobs[job_id]

    def _run(self, job, proxies, args):
        try:
            # Each job gets its own event loop on its own thread
            asyncio.run(run_all(proxies, args, on_result=job.add, should_stop=job.stop_requested.is_set))
        finally:
            job.finish()

    def progress(self, job_id):
        job = self.jobs.get(job_id)
        return job.progress() if job else None

    def stop(self, job_id):
        """Skip the proxies still queued. Ones already being tested finish first.
        Returns False if there's no such job."""
        job = self.jobs.get(job_id)
        if job is None:
            return False
        job.request_stop()
        return True

    def stop_all(self):
        with self.lock:
            jobs = list(self.jobs.values())
        for job in jobs:
            job.request_stop()
