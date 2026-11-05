"""Tests for the link library and the link checker.

Run them with:  python3 -m unittest discover -s tests -v

Two kinds of test:
  1. The library itself is sound: every link is official, dated, has a
     backup, and no worker-facing words make a legal claim.
  2. The checker reacts the right way to gone, moved, changed and stale pages.
     These use made-up pages, so they never touch the internet.
"""

import datetime as dt
import json
import re
import sys
import tempfile
import unittest
import urllib.parse
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "tools"))

import check_links as cl  # noqa: E402

LIBRARY = json.loads((ROOT / "links" / "links.json").read_text())
LINKS = LIBRARY["links"]
OFFICIAL = LIBRARY["official_domains"]
WORKER_TYPES = set(LIBRARY["worker_types"])
STATE_CODES = set(LIBRARY["state_codes"])


def banned_phrases():
    lines = (ROOT / "rules" / "banned-words.txt").read_text().splitlines()
    return [l.strip() for l in lines if l.strip() and not l.startswith("#")]


def find_banned(text):
    norm = cl.normalize(text)
    return [p for p in banned_phrases() if re.search(r"(?<![a-z])" + re.escape(p) + r"(?![a-z])", norm)]


def host_is_official(url):
    host = urllib.parse.urlsplit(url).hostname or ""
    return any(host == d["domain"] or host.endswith("." + d["domain"]) for d in OFFICIAL)


class LibraryIsSound(unittest.TestCase):

    def test_ids_and_addresses_are_unique(self):
        ids = [l["id"] for l in LINKS]
        urls = [l["url"] for l in LINKS]
        self.assertEqual(len(ids), len(set(ids)), "two links share an id")
        self.assertEqual(len(urls), len(set(urls)), "the same address is listed twice")

    def test_every_link_has_its_fields(self):
        needed = {"id", "topic", "agency", "source_type", "worker_types", "states", "url",
                  "covers", "check", "checked_on", "checked_by", "checked_by_person",
                  "recheck_by", "backup"}
        for l in LINKS:
            with self.subTest(link=l.get("id")):
                self.assertEqual(needed - set(l), set(), "missing fields")
                self.assertTrue(l["check"].get("title_contains"), "needs an expected title")
                self.assertTrue(l["check"].get("key_sentence"), "needs a key sentence")

    def test_every_address_is_https_and_official(self):
        for l in LINKS:
            with self.subTest(link=l["id"]):
                self.assertTrue(l["url"].startswith("https://"), "must be https")
                self.assertTrue(host_is_official(l["url"]),
                                "not on the official-domains list in links.json")
                self.assertNotIn("?", l["url"], "no tracking or session bits in addresses")

    def test_official_domains_explain_themselves(self):
        for d in OFFICIAL:
            with self.subTest(domain=d["domain"]):
                self.assertTrue(d.get("who"), "say who runs this site")

    def test_worker_types_and_states_use_known_words(self):
        for l in LINKS:
            with self.subTest(link=l["id"]):
                self.assertTrue(l["worker_types"])
                self.assertLessEqual(set(l["worker_types"]), WORKER_TYPES)
                self.assertTrue(l["states"])
                self.assertLessEqual(set(l["states"]), STATE_CODES | {"ALL"})

    def test_dates_line_up(self):
        for l in LINKS:
            with self.subTest(link=l["id"]):
                checked = dt.date.fromisoformat(l["checked_on"])
                recheck = dt.date.fromisoformat(l["recheck_by"])
                days = cl.BLOCKED_RECHECK_DAYS if l["check"].get("blocks_checker") else cl.RECHECK_DAYS
                self.assertEqual(recheck - checked, dt.timedelta(days=days),
                                 "recheck_by must be 90 days after checked_on (30 for sites that block the checker)")

    def test_every_link_has_a_working_backup(self):
        urls = {l["url"] for l in LINKS}
        for l in LINKS:
            b = l["backup"]
            with self.subTest(link=l["id"]):
                self.assertTrue(b.get("label"), "backup needs a label")
                self.assertTrue(b.get("url") or b.get("phone"), "backup needs an address or a phone")
                if b.get("url"):
                    self.assertNotEqual(b["url"], l["url"], "backup can't be the same page")
                    self.assertIn(b["url"], urls, "backup page must also be in the library, so it gets checked")
                if b.get("phone"):
                    self.assertRegex(b["phone"], r"^\d-\d{3}-\d{3}-\d{4}$|^\d{3}-\d{3}-\d{4}$")

    def test_backup_phone_is_checked_on_some_page(self):
        checked_phones = {cl.digits(l["check"]["phone"]) for l in LINKS if l["check"].get("phone")}
        for l in LINKS:
            phone = l["backup"].get("phone")
            if phone:
                with self.subTest(link=l["id"]):
                    self.assertIn(cl.digits(phone), checked_phones,
                                  "a backup phone must be one the checker looks for on an official page")

    def test_worker_facing_words_make_no_legal_claims(self):
        for l in LINKS:
            with self.subTest(link=l["id"]):
                for field in (l["topic"], l["covers"], l["backup"]["label"]):
                    self.assertEqual(find_banned(field), [], f"legal-claim words in: {field}")

    def test_banned_word_check_catches_claims(self):
        self.assertEqual(find_banned("They can’t fire you for this"), ["they can't", "can't fire"])
        self.assertEqual(find_banned("This is Illegal."), ["illegal"])
        self.assertEqual(find_banned("Protections at work"), [])  # only whole words


class TextReading(unittest.TestCase):

    def test_html_uses_main_part_and_skips_scripts(self):
        page = ("<html><head><title>Employee Rights | NLRB</title><script>var x='menu';</script></head>"
                "<body><nav>Menu Home</nav><main><h1>Employee Rights</h1><p>Workers may join.</p>"
                "<script>alert(1)</script></main><footer>Footer</footer></body></html>")
        title, text = cl.text_from_html(page)
        self.assertEqual(title, "Employee Rights | NLRB")
        self.assertIn("Workers may join.", text)
        self.assertNotIn("Menu", text)
        self.assertNotIn("alert", text)

    def test_markdown_copy_turns_into_plain_words(self):
        md = "# Title\n\nSee **the** [regional offices](https://x.gov/r).\n\n![logo](https://x.gov/l.png)"
        self.assertEqual(cl.normalize(cl.text_from_markdown(md)), "title see the regional offices.")

    def test_small_formatting_changes_do_not_count(self):
        self.assertEqual(cl.normalize("Workers’  rights\n"), cl.normalize("workers' rights"))
        self.assertEqual(cl.fingerprint("A  B"), cl.fingerprint("a b"))

    def test_harmless_address_differences(self):
        self.assertTrue(cl.same_address("https://www.flra.gov/", "https://www.flra.gov"))
        self.assertTrue(cl.same_address("https://WWW.nlrb.gov/x/", "https://www.nlrb.gov/x#top"))
        self.assertFalse(cl.same_address("https://www.nlrb.gov/a", "https://www.nlrb.gov/b"))


LINK = {
    "id": "t", "topic": "Test", "url": "https://www.nlrb.gov/test", "checked_on": "2026-11-05",
    "recheck_by": "2027-02-03",
    "check": {"title_contains": "Employee Rights",
              "key_sentence": "Most employees in the private sector are covered",
              "phone": "1-844-762-6572"},
    "backup": {"label": "NLRB: Contact Us", "url": "https://www.nlrb.gov/contact-us"},
}
GOOD_TEXT = "Most employees in the private sector are covered by the Act. Call 1-844-762-6572."
TODAY = dt.date(2026, 11, 9)


def good_page(**changes):
    p = cl.Page(status=200, final_url=LINK["url"], title="Employee Rights | NLRB", text=GOOD_TEXT)
    for k, v in changes.items():
        setattr(p, k, v)
    return p


def levels(result):
    return [p["level"] for p in result["problems"]]


class CheckerReacts(unittest.TestCase):

    def test_healthy_page_passes(self):
        r = cl.check_one(LINK, good_page(), None, TODAY)
        self.assertEqual(r["problems"], [])
        self.assertEqual(r["show"], "main")

    def test_gone_page_switches_to_backup(self):
        for page in (cl.Page(status=404, final_url=LINK["url"]), cl.Page(status=410),
                     cl.Page(error="could not connect")):
            r = cl.check_one(LINK, page, None, TODAY)
            self.assertEqual(r["show"], "backup")
            self.assertEqual(levels(r), [cl.FIX_NOW])

    def test_blocked_checker_keeps_link_up_but_asks_for_a_look(self):
        r = cl.check_one(LINK, cl.Page(status=403, final_url=LINK["url"]), None, TODAY)
        self.assertEqual(r["show"], "main")
        self.assertEqual(levels(r), [cl.LOOK])

    def test_site_known_to_block_the_checker_is_a_note_not_an_alarm(self):
        link = dict(LINK, check=dict(LINK["check"], blocks_checker=True))
        r = cl.check_one(link, cl.Page(status=403, final_url=LINK["url"]), None, TODAY)
        self.assertEqual(r["problems"], [])
        self.assertEqual(len(r["notes"]), 1)
        self.assertEqual(r["show"], "main")

    def test_known_blocker_that_goes_missing_still_alarms(self):
        link = dict(LINK, check=dict(LINK["check"], blocks_checker=True))
        r = cl.check_one(link, cl.Page(status=404, final_url=LINK["url"]), None, TODAY)
        self.assertEqual(r["show"], "backup")

    def test_moved_page_is_flagged(self):
        r = cl.check_one(LINK, good_page(final_url="https://www.nlrb.gov/somewhere-else"), None, TODAY)
        self.assertEqual(levels(r), [cl.LOOK])
        self.assertIn("somewhere-else", r["problems"][0]["what"])

    def test_harmless_redirect_is_not_flagged(self):
        r = cl.check_one(LINK, good_page(final_url=LINK["url"] + "/"), None, TODAY)
        self.assertEqual(r["problems"], [])

    def test_changed_title_is_flagged(self):
        r = cl.check_one(LINK, good_page(title="Page not found | NLRB"), None, TODAY)
        self.assertEqual(levels(r), [cl.LOOK])

    def test_missing_key_sentence_switches_to_backup(self):
        r = cl.check_one(LINK, good_page(text="Call 1-844-762-6572."), None, TODAY)
        self.assertEqual(r["show"], "backup")
        self.assertIn(cl.FIX_NOW, levels(r))

    def test_missing_phone_is_flagged(self):
        r = cl.check_one(LINK, good_page(text="Most employees in the private sector are covered."), None, TODAY)
        self.assertEqual(levels(r), [cl.LOOK])

    def test_changed_text_is_flagged_against_saved_fingerprint(self):
        old = {"value": cl.fingerprint("something older"), "method": "direct"}
        r = cl.check_one(LINK, good_page(), old, TODAY)
        self.assertEqual(levels(r), [cl.LOOK])

    def test_same_text_matches_saved_fingerprint(self):
        old = {"value": cl.fingerprint(GOOD_TEXT), "method": "direct"}
        self.assertEqual(cl.check_one(LINK, good_page(), old, TODAY)["problems"], [])

    def test_new_reading_method_does_not_raise_false_alarm(self):
        old = {"value": cl.fingerprint("text as a web reader saw it"), "method": "snapshot:firecrawl"}
        r = cl.check_one(LINK, good_page(), old, TODAY)
        self.assertEqual(r["problems"], [])
        self.assertEqual(r["fingerprint"]["method"], "direct")

    def test_due_for_recheck_asks_for_a_look(self):
        r = cl.check_one(LINK, good_page(), None, dt.date(2027, 2, 10))
        self.assertEqual(levels(r), [cl.LOOK])
        self.assertEqual(r["show"], "main")

    def test_long_overdue_switches_to_backup(self):
        r = cl.check_one(LINK, good_page(), None, dt.date(2027, 3, 10))
        self.assertEqual(r["show"], "backup")


class BackupsAreChecked(unittest.TestCase):

    def test_broken_backup_and_missing_backup_phone_are_flagged(self):
        contact = dict(LINK, id="contact", url="https://www.nlrb.gov/contact-us",
                       backup={"label": "Phone", "phone": "1-844-762-6572"})
        topic = dict(LINK, id="topic")
        links = [topic, contact]
        results = {"topic": cl.check_one(topic, good_page(), None, TODAY),
                   "contact": cl.check_one(contact, cl.Page(status=404), None, TODAY)}
        results["contact"]["phone_missing"] = True
        cl.check_backups(links, results)
        whats = " ".join(p["what"] for p in results["topic"]["problems"])
        self.assertIn("backup page is broken", whats)
        self.assertIn("backup phone", " ".join(p["what"] for p in results["contact"]["problems"]))


class LiveFetchOnThisComputer(unittest.TestCase):
    """Opens real web addresses, but only on a tiny test server on this computer,
    so it proves the live-fetch code without touching the internet."""

    @classmethod
    def setUpClass(cls):
        import http.server, os, threading
        os.environ["no_proxy"] = os.environ["NO_PROXY"] = "127.0.0.1,localhost"

        class Handler(http.server.BaseHTTPRequestHandler):
            def do_GET(self):
                if self.path == "/page":
                    body = (b"<html><head><title>Employee Rights</title></head><body><nav>Menu</nav>"
                            b"<main><p>Most employees in the private sector are covered.</p></main></body></html>")
                    self.send_response(200)
                    self.send_header("Content-Type", "text/html; charset=utf-8")
                    self.end_headers()
                    self.wfile.write(body)
                elif self.path == "/old":
                    self.send_response(301)
                    self.send_header("Location", "/page")
                    self.end_headers()
                else:
                    self.send_response(404)
                    self.end_headers()

            def log_message(self, *args):
                pass

        cls.server = http.server.HTTPServer(("127.0.0.1", 0), Handler)
        cls.base = f"http://127.0.0.1:{cls.server.server_port}"
        threading.Thread(target=cls.server.serve_forever, daemon=True).start()

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()

    def test_reads_title_and_main_text(self):
        page = cl.fetch_live(self.base + "/page")
        self.assertEqual(page.status, 200)
        self.assertEqual(page.title, "Employee Rights")
        self.assertIn("private sector", page.text)
        self.assertNotIn("Menu", page.text)

    def test_follows_and_reports_a_redirect(self):
        page = cl.fetch_live(self.base + "/old")
        self.assertEqual(page.final_url, self.base + "/page")
        link = dict(LINK, url=self.base + "/old")
        r = cl.check_one(link, page, None, TODAY)
        self.assertIn("now sends people to", r["problems"][0]["what"])

    def test_reports_a_missing_page(self):
        self.assertEqual(cl.fetch_live(self.base + "/nothing").status, 404)

    def test_unreachable_site_is_an_error_not_a_crash(self):
        page = cl.fetch_live("http://127.0.0.1:9/", tries=1)
        self.assertTrue(page.error)


class WholeRun(unittest.TestCase):
    """Runs the full checker on a saved copy, writing into a scratch folder."""

    def test_run_writes_report_and_status(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmp = Path(tmp)
            (tmp / "links").mkdir()
            snap = tmp / "snap"
            snap.mkdir()
            lib = {"links": [dict(LINK, id="ok"), dict(LINK, id="gone", url="https://www.nlrb.gov/gone")]}
            (tmp / "links" / "links.json").write_text(json.dumps(lib))
            (snap / "ok.meta.json").write_text(json.dumps(
                {"status_code": 200, "final_url": LINK["url"], "title": "Employee Rights", "method": "test"}))
            (snap / "ok.md").write_text(GOOD_TEXT)
            (snap / "gone.meta.json").write_text(json.dumps(
                {"status_code": 404, "final_url": "https://www.nlrb.gov/gone", "title": "", "method": "test"}))
            saved = (cl.LINKS_FILE, cl.BASELINE_FILE, cl.REPORT_FILE, cl.STATUS_FILE)
            cl.LINKS_FILE, cl.BASELINE_FILE = tmp / "links/links.json", tmp / "links/baseline.json"
            cl.REPORT_FILE, cl.STATUS_FILE = tmp / "links/REPORT.md", tmp / "links/link-status.json"
            try:
                import contextlib, io
                with contextlib.redirect_stdout(io.StringIO()):
                    code = cl.run(["--snapshot", str(snap), "--today", "2026-11-09"])
            finally:
                cl.LINKS_FILE, cl.BASELINE_FILE, cl.REPORT_FILE, cl.STATUS_FILE = saved
            self.assertEqual(code, 1)
            status = json.loads((tmp / "links/link-status.json").read_text())
            self.assertEqual(status["links"]["ok"]["show"], "main")
            self.assertEqual(status["links"]["gone"]["show"], "backup")
            report = (tmp / "links/REPORT.md").read_text()
            self.assertIn("1 need fixing now", report)
            baseline = json.loads((tmp / "links/baseline.json").read_text())
            self.assertIn("ok", baseline)
            self.assertNotIn("gone", baseline)


if __name__ == "__main__":
    unittest.main()
