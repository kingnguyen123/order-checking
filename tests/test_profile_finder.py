"""
Offline tests for Profile Finder's filter logic (no CSV ever touches disk).
"""
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "backend" / "profile_finder"))

from filter import filter_rows, parse_wanted_emails  # noqa: E402

CSV = """Email Address,Profile Name,Shipping Name
a@x.com,Co Mary PKC_jig (24),John
b@x.com,Kem B (2),Jane
c@x.com,Someone Else,Bob
"""


class ParseWantedTests(unittest.TestCase):
    def test_mixed_emails_and_profile_names(self):
        wanted = parse_wanted_emails("a@x.com\nKem B (2)\n, notfound@x.com")
        self.assertEqual(set(wanted), {"a@x.com", "kem b (2)", "notfound@x.com"})


class FilterRowsTests(unittest.TestCase):
    def test_matches_by_email_or_profile_name(self):
        wanted = parse_wanted_emails("a@x.com\nKem B (2)")
        fieldnames, matched, rest, seen = filter_rows(CSV, wanted)
        self.assertEqual({r["Email Address"] for r in matched}, {"a@x.com", "b@x.com"})
        self.assertEqual({r["Email Address"] for r in rest}, {"c@x.com"})

    def test_seen_counts_are_credited_to_whichever_key_matched(self):
        wanted = parse_wanted_emails("a@x.com\nKem B (2)")
        _, _, _, seen = filter_rows(CSV, wanted)
        self.assertEqual(seen, {"a@x.com": 1, "kem b (2)": 1})

    def test_matching_is_case_insensitive(self):
        wanted = parse_wanted_emails("A@X.COM\nkem b (2)")
        _, matched, _, _ = filter_rows(CSV, wanted)
        self.assertEqual({r["Email Address"] for r in matched}, {"a@x.com", "b@x.com"})

    def test_not_found_entry_reports_correctly(self):
        wanted = parse_wanted_emails("notfound@x.com")
        _, matched, rest, seen = filter_rows(CSV, wanted)
        self.assertEqual(matched, [])
        self.assertEqual(len(rest), 3)
        self.assertEqual(seen, {})

    def test_works_with_only_an_email_column(self):
        csv_text = "Email Address,Other\na@x.com,1\nb@x.com,2\n"
        wanted = parse_wanted_emails("a@x.com")
        _, matched, rest, _ = filter_rows(csv_text, wanted)
        self.assertEqual([r["Email Address"] for r in matched], ["a@x.com"])
        self.assertEqual([r["Email Address"] for r in rest], ["b@x.com"])

    def test_works_with_only_a_profile_name_column(self):
        csv_text = "Profile Name,Other\nKem B (2),1\nCo Mary (1),2\n"
        wanted = parse_wanted_emails("Kem B (2)")
        _, matched, rest, _ = filter_rows(csv_text, wanted)
        self.assertEqual([r["Profile Name"] for r in matched], ["Kem B (2)"])
        self.assertEqual([r["Profile Name"] for r in rest], ["Co Mary (1)"])

    def test_raises_when_neither_column_exists(self):
        csv_text = "Status,Other\nordered,1\n"
        with self.assertRaises(ValueError):
            filter_rows(csv_text, parse_wanted_emails("a@x.com"))

    def test_every_row_lands_in_exactly_one_output(self):
        wanted = parse_wanted_emails("a@x.com\nKem B (2)")
        _, matched, rest, _ = filter_rows(CSV, wanted)
        self.assertEqual(len(matched) + len(rest), 3)


if __name__ == "__main__":
    unittest.main()
