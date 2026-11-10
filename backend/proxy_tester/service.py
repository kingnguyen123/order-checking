"""
Runs proxy tests in the background for the Proxy Tester page, which polls for
progress. Nothing touches disk, and a job drops its proxies' passwords as soon
as they've been tested - only masked labels are kept or sent to the browser.
"""
import threading
import uuid
from concurrent.futures import ThreadPoolExecutor

from proxy_tester.tester import TargetCheck, parse_lines, parse_target_url, sort_key, test_proxy

MAX_THREADS = 100
MAX_TIMEOUT = 60
MAX_DOWNLOAD_MB = 100
MAX_TARGET_ATTEMPTS = 20
KEEP_FINISHED_JOBS = 5


def clamp(value, low, high):
    return max(low, min(high, value))


def result_to_row(line_number, result):
    return {
        "line": line_number,   # lets the page find the original pasted line
        "proxy": result.proxy.label,
        "alive": result.alive,
        "latencyMs": result.latency_ms,
        "speedMbps": result.speed_mbps,
        "exitIp": result.exit_ip,
        "country": result.country,
        "targetPassed": result.target_passed,
        "targetAttempts": result.target_attempts,
        "targetMs": result.target_ms,
        "error": result.error,
    }


class ProxyTestJob:
    def __init__(self, total, skipped, target_url):
        self.id = uuid.uuid4().hex
        self.total = total
        self.skipped = skipped
        self.target_url = target_url
        self.state = "running"   # running -> stopping -> stopped, or running -> finished
        self.stop_requested = threading.Event()
        self.lock = threading.Lock()
        self.ranked = []         # (sort key, row) pairs

    def add(self, line_number, result):
        with self.lock:
            self.ranked.append((sort_key(result), result_to_row(line_number, result)))

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
            ranked = sorted(self.ranked, key=lambda pair: pair[0])
            return {
                "state": self.state,
                "total": self.total,
                "done": len(ranked),
                "skipped": self.skipped,
                "targetUrl": self.target_url,
                "rows": [row for _, row in ranked],
            }


class ProxyTestService:
    def __init__(self):
        self.jobs = {}
        self.lock = threading.Lock()

    def start(self, text, threads=20, timeout=10, download_mb=10,
              target_url=None, target_text="", target_attempts=3):
        """Start testing the pasted proxies and return the job. `download_mb` of
        None skips the speed test, and `target_url` of None skips the site check.
        Raises ValueError if no line is a valid proxy or the target URL is bad."""
        proxies, skipped = parse_lines(text.split("\n"))
        skipped = [{"line": number, "reason": reason} for number, reason in skipped]
        if not proxies:
            raise ValueError("No valid proxies to test.")

        threads = clamp(int(threads), 1, MAX_THREADS)
        timeout = clamp(float(timeout), 1, MAX_TIMEOUT)
        size_bytes = None
        if download_mb is not None:
            size_bytes = int(clamp(float(download_mb), 0.1, MAX_DOWNLOAD_MB) * 1_000_000)

        target = None
        if target_url:
            target = TargetCheck(
                parse_target_url(target_url), target_text.strip(),
                clamp(int(target_attempts), 1, MAX_TARGET_ATTEMPTS),
            )

        job = ProxyTestJob(len(proxies), skipped, target.url if target else None)
        with self.lock:
            self._prune_finished_jobs()
            self.jobs[job.id] = job
        threading.Thread(
            target=self._run, args=(job, proxies, threads, timeout, size_bytes, target), daemon=True
        ).start()
        return job

    def _prune_finished_jobs(self):
        finished = [job_id for job_id, job in self.jobs.items() if not job.is_active()]
        for job_id in finished[:-KEEP_FINISHED_JOBS]:
            del self.jobs[job_id]

    def _run(self, job, proxies, threads, timeout, size_bytes, target):
        def test_one(item):
            if job.stop_requested.is_set():
                return
            line_number, proxy = item
            job.add(line_number, test_proxy(proxy, timeout, size_bytes, target))

        try:
            with ThreadPoolExecutor(max_workers=threads) as executor:
                list(executor.map(test_one, proxies))
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
