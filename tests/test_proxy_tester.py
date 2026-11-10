"""
Offline tests for Proxy Tester's parsing and job handling (no network calls).
"""
import sys
import threading
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "backend"))

from proxy_tester import service as service_module  # noqa: E402
from proxy_tester.service import ProxyTestService  # noqa: E402
from proxy_tester import tester  # noqa: E402
from proxy_tester.tester import (  # noqa: E402
    Result, TargetCheck, check_target, leaderboards, parse_lines, parse_proxy, parse_target_url, sort_key,
)


def fake_test(speeds):
    """A test_proxy stand-in: speed by port, and a port that isn't listed is dead."""
    def run(proxy, timeout, size_bytes, target=None):
        speed = speeds.get(proxy.port)
        return Result(proxy, alive=speed is not None, speed_mbps=speed)
    return run


def wait_for_job(job):
    for _ in range(500):
        if not job.is_active():
            return
        threading.Event().wait(0.01)
    raise AssertionError("job never finished")


class ParseProxyTests(unittest.TestCase):
    def test_no_scheme_defaults_to_http(self):
        self.assertEqual(parse_proxy("1.2.3.4:8080").url, "http://1.2.3.4:8080")

    def test_colon_format_keeps_colons_in_password(self):
        proxy = parse_proxy("host.example:1314:user-US:pa:ss")
        self.assertEqual((proxy.username, proxy.password), ("user-US", "pa:ss"))
        self.assertEqual(proxy.url, "http://user-US:pa%3Ass@host.example:1314")

    def test_socks_resolves_dns_on_the_proxy(self):
        self.assertEqual(parse_proxy("socks5://u:p@1.2.3.4:1080").url, "socks5h://u:p@1.2.3.4:1080")

    def test_label_masks_password(self):
        self.assertEqual(parse_proxy("1.2.3.4:80:bob:secret").label, "http://bob:***@1.2.3.4:80")

    def test_rejected_lines_never_quote_the_line(self):
        lines = ["1.2.3.4:secret", "1.2.3.4:secret:bob:x", "http://bob:x@1.2.3.4:secret", "secret"]
        proxies, skipped = parse_lines(lines)
        self.assertEqual(proxies, [])
        self.assertEqual([number for number, _ in skipped], [1, 2, 3, 4])
        self.assertNotIn("secret", str(skipped))

    def test_blank_and_comment_lines_are_ignored(self):
        proxies, skipped = parse_lines(["", "# note", "  ", "1.2.3.4:80"])
        self.assertEqual([number for number, _ in proxies], [4])
        self.assertEqual(skipped, [])


class SortKeyTests(unittest.TestCase):
    def test_fastest_first_then_no_speed_then_dead(self):
        proxy = parse_proxy("1.2.3.4:80")
        dead = Result(proxy)
        no_speed = Result(proxy, alive=True, latency_ms=50)
        slow = Result(proxy, alive=True, speed_mbps=10)
        fast = Result(proxy, alive=True, speed_mbps=90)
        ranked = sorted([dead, no_speed, slow, fast], key=sort_key)
        self.assertEqual(ranked, [fast, slow, no_speed, dead])


    def test_target_pass_rate_outranks_speed(self):
        proxy = parse_proxy("1.2.3.4:80")
        fast_but_blocked = Result(proxy, alive=True, speed_mbps=500, target_attempts=3)
        partial = Result(proxy, alive=True, target_passed=2, target_attempts=3, target_ms=100)
        quick = Result(proxy, alive=True, target_passed=3, target_attempts=3, target_ms=200)
        slow = Result(proxy, alive=True, target_passed=3, target_attempts=3, target_ms=900)
        ranked = sorted([fast_but_blocked, partial, slow, quick], key=sort_key)
        self.assertEqual(ranked, [quick, slow, partial, fast_but_blocked])


class LeaderboardTests(unittest.TestCase):
    def proxy(self, port):
        return parse_proxy(f"1.2.3.4:{port}")

    def boards(self, results):
        return {title: [(r.proxy.port, score) for r, score in entries]
                for title, entries in leaderboards(results)}

    def test_each_test_ranks_on_its_own_measure(self):
        results = [
            Result(self.proxy(1), alive=True, latency_ms=300, speed_mbps=90,
                   target_passed=2, target_attempts=3, target_ms=100),
            Result(self.proxy(2), alive=True, latency_ms=100, speed_mbps=20,
                   target_passed=3, target_attempts=3, target_ms=900),
        ]
        boards = self.boards(results)
        self.assertEqual(boards["Target site"], [(2, "3/3, 900 ms"), (1, "2/3, 100 ms")])
        self.assertEqual(boards["Lowest latency"], [(2, "100 ms"), (1, "300 ms")])
        self.assertEqual(boards["Fastest download"], [(1, "90.00 Mbps"), (2, "20.00 Mbps")])

    def test_tests_that_were_not_run_get_no_board(self):
        boards = self.boards([Result(self.proxy(1), alive=True, latency_ms=100)])
        self.assertEqual(list(boards), ["Lowest latency"])

    def test_failed_and_dead_proxies_are_left_off(self):
        results = [
            Result(self.proxy(1), alive=True, latency_ms=100, target_attempts=3),
            Result(self.proxy(2), error="timeout"),
        ]
        boards = self.boards(results)
        self.assertEqual(boards["Target site"], [])
        self.assertEqual([port for port, _ in boards["Lowest latency"]], [1])

    def test_boards_stop_at_the_top_five(self):
        results = [Result(self.proxy(port), alive=True, latency_ms=port) for port in range(1, 9)]
        self.assertEqual([port for port, _ in self.boards(results)["Lowest latency"]], [1, 2, 3, 4, 5])


class FakeResponse:
    def __init__(self, status, body):
        self.status_code = status
        self.body = body

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def raise_for_status(self):
        if self.status_code >= 400:
            raise tester.requests.exceptions.HTTPError(response=self)

    def iter_content(self, size):
        for start in range(0, len(self.body), size):
            yield self.body[start:start + size]


class CheckTargetTests(unittest.TestCase):
    proxy = parse_proxy("1.2.3.4:80")
    target = TargetCheck("https://site.example/", "Expect More", attempts=3)

    def run_check(self, *responses):
        with mock.patch.object(tester, "open_url", side_effect=list(responses)):
            return check_target(self.proxy, self.target, 5)

    def test_real_page_passes_case_insensitively(self):
        passed, average, problem = self.run_check(*[FakeResponse(200, b"<title>EXPECT MORE</title>")] * 3)
        self.assertEqual((passed, problem), (3, ""))
        self.assertIsNotNone(average)

    def test_status_200_block_page_fails(self):
        passed, average, problem = self.run_check(*[FakeResponse(200, b"Access Denied")] * 3)
        self.assertEqual((passed, average, problem), (0, None, "expected text missing"))

    def test_counts_partial_passes_and_reports_the_block(self):
        passed, _, problem = self.run_check(
            FakeResponse(200, b"Expect More"), FakeResponse(403, b""), FakeResponse(200, b"Expect More"),
        )
        self.assertEqual((passed, problem), (2, "http 403"))

    def test_text_split_across_chunks_is_found(self):
        body = b"x" * (tester.CHUNK_SIZE - 3) + b"Expect More"
        self.assertEqual(self.run_check(*[FakeResponse(200, body)] * 3)[0], 3)

    def test_target_url_must_be_http(self):
        self.assertEqual(parse_target_url(" https://site.example/ "), "https://site.example/")
        for bad in ("site.example", "ftp://site.example", "https://"):
            with self.assertRaises(ValueError):
                parse_target_url(bad)


class ProxyTestServiceTests(unittest.TestCase):
    def test_ranks_results_and_reports_skipped_lines(self):
        speeds = {1: 10.0, 2: 90.0}
        with mock.patch.object(service_module, "test_proxy", fake_test(speeds)):
            service = ProxyTestService()
            job = service.start("1.1.1.1:1\n1.1.1.1:2\nbad line\n1.1.1.1:3", threads=2)
            wait_for_job(job)
        progress = service.progress(job.id)
        self.assertEqual(progress["state"], "finished")
        self.assertEqual(progress["done"], 3)
        self.assertEqual([row["line"] for row in progress["rows"]], [2, 1, 4])
        self.assertEqual([s["line"] for s in progress["skipped"]], [3])

    def test_progress_never_contains_passwords(self):
        with mock.patch.object(service_module, "test_proxy", fake_test({1: 5.0})):
            service = ProxyTestService()
            job = service.start("1.1.1.1:1:bob:secret")
            wait_for_job(job)
        progress = service.progress(job.id)
        self.assertEqual(progress["rows"][0]["proxy"], "http://bob:***@1.1.1.1:1")
        self.assertNotIn("secret", str(progress))

    def test_stop_skips_queued_proxies(self):
        release = threading.Event()

        def blocking(proxy, timeout, size_bytes, target=None):
            release.wait(5)
            return Result(proxy, alive=True)

        with mock.patch.object(service_module, "test_proxy", blocking):
            service = ProxyTestService()
            job = service.start("1.1.1.1:1\n1.1.1.1:2\n1.1.1.1:3", threads=1)
            self.assertTrue(service.stop(job.id))
            self.assertEqual(service.progress(job.id)["state"], "stopping")
            release.set()
            wait_for_job(job)
        progress = service.progress(job.id)
        self.assertEqual(progress["state"], "stopped")
        self.assertLess(progress["done"], 3)

    def test_no_valid_proxies_is_an_error(self):
        with self.assertRaises(ValueError):
            ProxyTestService().start("not a proxy")

    def test_only_recent_finished_jobs_are_kept(self):
        with mock.patch.object(service_module, "test_proxy", fake_test({})):
            service = ProxyTestService()
            jobs = [service.start("1.1.1.1:1") for _ in range(service_module.KEEP_FINISHED_JOBS + 3)]
            for job in jobs:
                wait_for_job(job)
            service.start("1.1.1.1:1")
        self.assertIsNone(service.progress(jobs[0].id))
        self.assertIsNotNone(service.progress(jobs[-1].id))


if __name__ == "__main__":
    unittest.main()
