#!/usr/bin/env python3
"""Weekly link checker for the union planner's link library.

What it does, in plain words:
  For every official page in links/links.json it opens the page and asks:
    - Is the page gone?            (not found, or the site can't be reached)
    - Did the page move?           (the address now sends you somewhere else)
    - Did the page change?         (title, key sentence, phone number or the
                                    page's text "fingerprint" is different)
    - Is our own check too old?    (past its "recheck by" date)
  It writes the answers to links/REPORT.md (for people) and
  links/link-status.json (for the website, which shows the backup when a
  link is broken). If anything needs a person, it exits with an error so
  GitHub emails the owner.

It uses only Python's built-in tools. No extra packages.

How to run it:
  python3 tools/check_links.py                    check the live pages
  python3 tools/check_links.py --update-baseline  check, then save today's
                                                  fingerprints as the new normal
  python3 tools/check_links.py --snapshot DIR     check saved copies instead of
                                                  the live web (used when the
                                                  computer can't reach the sites)
"""

from __future__ import annotations

import argparse
import datetime as dt
import hashlib
import html
import json
import re
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from html.parser import HTMLParser
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
LINKS_FILE = ROOT / "links" / "links.json"
BASELINE_FILE = ROOT / "links" / "baseline.json"
REPORT_FILE = ROOT / "links" / "REPORT.md"
STATUS_FILE = ROOT / "links" / "link-status.json"

RECHECK_DAYS = 90          # a person re-reads every page at least this often
BLOCKED_RECHECK_DAYS = 30  # ...and monthly for sites that block automatic checks
OVERDUE_GRACE_DAYS = 30    # past recheck-by by this much -> show the backup
USER_AGENT = "union-planner-link-checker/1.0 (weekly check of official pages)"
TIMEOUT_SECONDS = 30

# How serious a problem is.
FIX_NOW = "fix now"   # the worker would land on a broken or wrong page
LOOK = "look"         # a person should look, but the link stays up meanwhile


# ---------------------------------------------------------------- reading text

class _TextExtractor(HTMLParser):
    """Pulls the title and the visible words out of a web page."""

    SKIP = {"script", "style", "noscript", "svg", "template", "head"}

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.title = ""
        self._in_title = False
        self._skip_depth = 0
        self._main_depth = 0
        self.all_text: list[str] = []
        self.main_text: list[str] = []

    def handle_starttag(self, tag, attrs):
        if tag == "title":
            self._in_title = True
        if tag in self.SKIP:
            self._skip_depth += 1
        if tag == "main" or (tag != "meta" and dict(attrs).get("role") == "main"):
            self._main_depth += 1

    def handle_endtag(self, tag):
        if tag == "title":
            self._in_title = False
        if tag in self.SKIP and self._skip_depth:
            self._skip_depth -= 1
        if tag == "main" and self._main_depth:
            self._main_depth -= 1

    def handle_data(self, data):
        if self._in_title:
            self.title += data
            return
        if self._skip_depth:
            return
        self.all_text.append(data)
        if self._main_depth:
            self.main_text.append(data)


def text_from_html(page: str) -> tuple[str, str]:
    """Returns (title, visible text). Uses the page's <main> part when it has one."""
    parser = _TextExtractor()
    parser.feed(page)
    body = parser.main_text if "".join(parser.main_text).strip() else parser.all_text
    return parser.title.strip(), " ".join(body)


_MD_IMAGE = re.compile(r"!\[[^\]]*\]\([^)]*\)")
_MD_LINK = re.compile(r"\[([^\]]*)\]\([^)]*\)")
_MD_MARKS = re.compile(r"[*_#>`|]+")


def text_from_markdown(md: str) -> str:
    """Turns a saved markdown copy of a page into plain words."""
    md = _MD_IMAGE.sub(" ", md)
    md = _MD_LINK.sub(r"\1", md)
    return _MD_MARKS.sub(" ", md)


def normalize(text: str) -> str:
    """Lowercase, straight quotes, single spaces: so tiny formatting changes don't count."""
    text = html.unescape(text)
    text = (text.replace("’", "'").replace("‘", "'")
                .replace("“", '"').replace("”", '"')
                .replace(" ", " ").replace("–", "-").replace("—", "-"))
    return re.sub(r"\s+", " ", text).strip().lower()


def fingerprint(text: str) -> str:
    return hashlib.sha256(normalize(text).encode("utf-8")).hexdigest()[:16]


def digits(text: str) -> str:
    return re.sub(r"\D", "", text)


# ---------------------------------------------------------------- fetching

class Page:
    """What we saw when we opened one address."""

    def __init__(self, status=None, final_url=None, title="", text="", error=None, method="direct"):
        self.status = status
        self.final_url = final_url
        self.title = title
        self.text = text
        self.error = error
        self.method = method


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *args, **kwargs):  # follow redirects ourselves, to see each hop
        return None


def fetch_live(url: str, tries: int = 2) -> Page:
    """Opens a page on the live web, following up to 10 redirects."""
    opener = urllib.request.build_opener(_NoRedirect)
    last_error = None
    for attempt in range(tries):
        current = url
        try:
            for _ in range(10):
                req = urllib.request.Request(current, headers={
                    "User-Agent": USER_AGENT,
                    "Accept": "text/html,application/xhtml+xml",
                    "Accept-Language": "en-US,en;q=0.8",
                })
                try:
                    resp = opener.open(req, timeout=TIMEOUT_SECONDS)
                except urllib.error.HTTPError as e:
                    if e.code in (301, 302, 303, 307, 308) and e.headers.get("Location"):
                        current = urllib.parse.urljoin(current, e.headers["Location"])
                        continue
                    return Page(status=e.code, final_url=current, method="direct")
                raw = resp.read(3_000_000)
                charset = resp.headers.get_content_charset() or "utf-8"
                title, text = text_from_html(raw.decode(charset, errors="replace"))
                return Page(status=resp.status, final_url=current, title=title, text=text, method="direct")
            return Page(error="too many redirects", final_url=current, method="direct")
        except (urllib.error.URLError, TimeoutError, ConnectionError, OSError) as e:
            last_error = str(getattr(e, "reason", e))
            if attempt + 1 < tries:
                time.sleep(5)
    return Page(error=last_error or "could not connect", method="direct")


def fetch_from_snapshot(folder: Path, link_id: str) -> Page:
    """Reads a saved copy: <id>.meta.json plus <id>.md, made by a web reader."""
    meta_path = folder / f"{link_id}.meta.json"
    if not meta_path.exists():
        return Page(error="no saved copy of this page", method="snapshot")
    meta = json.loads(meta_path.read_text())
    if meta.get("error"):
        return Page(error=meta["error"], method="snapshot")
    md_path = folder / f"{link_id}.md"
    md = md_path.read_text() if md_path.exists() else ""
    return Page(status=meta.get("status_code"), final_url=meta.get("final_url"),
                title=meta.get("title", ""), text=text_from_markdown(md),
                method=f"snapshot:{meta.get('method', 'unknown')}")


# ---------------------------------------------------------------- judging

def same_address(a: str, b: str) -> bool:
    """True when two addresses only differ in ways that don't matter
    (http vs https, a trailing slash, capital letters in the site name, #section)."""
    def key(u: str):
        p = urllib.parse.urlsplit(u)
        path = p.path.rstrip("/") or "/"
        return (p.netloc.lower(), path, p.query)
    return key(a) == key(b)


def parse_date(s: str) -> dt.date:
    return dt.date.fromisoformat(s)


def check_one(link: dict, page: Page, baseline: dict | None, today: dt.date) -> dict:
    """Decides what's wrong with one link. Returns {"show", "problems", "fingerprint"}."""
    problems: list[tuple[str, str]] = []
    notes: list[str] = []
    expect = link.get("check", {})
    new_fp = None
    phone_missing = False

    # 1. Gone or unreachable.
    if page.error:
        problems.append((FIX_NOW, f"Couldn't open the page ({page.error}). Showing the backup."))
    elif page.status in (404, 410):
        problems.append((FIX_NOW, f"The page is gone (the site answered {page.status}). Showing the backup."))
    elif page.status in (401, 403, 429) and expect.get("blocks_checker"):
        notes.append(f"This site blocks automatic checks (answered {page.status}), as expected. "
                     f"A person re-reads it every {BLOCKED_RECHECK_DAYS} days instead.")
    elif page.status in (401, 403, 429) or (page.status or 0) >= 500:
        problems.append((LOOK, f"The site wouldn't let the checker in or was down (answered {page.status}). "
                               "The link stays up; open it yourself to confirm it still works."))
    elif page.status != 200:
        problems.append((LOOK, f"Unusual answer from the site ({page.status}). Open it yourself to confirm."))
    else:
        # 2. Moved.
        if page.final_url and not same_address(link["url"], page.final_url):
            problems.append((LOOK, f"The address now sends people to {page.final_url}. "
                                   "Approve the new address or find the right page."))
        text = normalize(page.text)
        # 3. Changed.
        title_bit = expect.get("title_contains")
        if title_bit and normalize(title_bit) not in normalize(page.title):
            problems.append((LOOK, f"The page title changed. Expected it to include \"{title_bit}\", "
                                   f"now it's \"{page.title.strip()}\"."))
        sentence = expect.get("key_sentence")
        if sentence and normalize(sentence) not in text:
            problems.append((FIX_NOW, "The key sentence is no longer on the page, so it may not "
                                      "cover this topic any more. Showing the backup until a person checks."))
        phone = expect.get("phone")
        if phone and digits(phone) not in digits(page.text):
            phone_missing = True
            problems.append((LOOK, f"The phone number {phone} is no longer on the page. Check it before "
                                   "the site shows it again."))
        new_fp = {"value": fingerprint(page.text), "method": page.method, "saved_on": today.isoformat()}
        if baseline and baseline.get("method") == page.method and baseline.get("value") != new_fp["value"]:
            problems.append((LOOK, "The page's text changed since the last saved check. "
                                   "Read it to make sure it still says what our card expects."))

    # 4. Our own check is too old.
    recheck_by = parse_date(link["recheck_by"])
    if today > recheck_by + dt.timedelta(days=OVERDUE_GRACE_DAYS):
        problems.append((FIX_NOW, f"No person has re-read this page since {link['checked_on']}, "
                                  f"{OVERDUE_GRACE_DAYS}+ days past its recheck date. Showing the backup."))
    elif today > recheck_by:
        problems.append((LOOK, f"Due for a person to re-read it (recheck by {link['recheck_by']})."))

    show = "backup" if any(level == FIX_NOW for level, _ in problems) else "main"
    return {"show": show, "problems": [{"level": lvl, "what": msg} for lvl, msg in problems],
            "notes": notes,
            "fingerprint": new_fp, "phone_missing": phone_missing}


def check_backups(links: list[dict], results: dict) -> None:
    """A backup is only useful if it works. Flags any link whose backup page is
    itself broken, or whose backup phone wasn't found where we check it."""
    broken_urls = {l["url"] for l in links if results[l["id"]]["show"] == "backup"}
    bad_phones = {digits(l["check"]["phone"]) for l in links
                  if l["check"].get("phone") and results[l["id"]]["phone_missing"]}
    for l in links:
        b, r = l["backup"], results[l["id"]]
        if b.get("url") in broken_urls:
            r["problems"].append({"level": LOOK, "what": "Its backup page is broken too. Pick another backup."})
        if b.get("phone") and digits(b["phone"]) in bad_phones:
            r["problems"].append({"level": LOOK, "what": f"Its backup phone {b['phone']} wasn't found on the "
                                                        "official page this week. Confirm the number."})


# ---------------------------------------------------------------- report

def build_report(links: list[dict], results: dict, today: dt.date, method: str) -> str:
    fix = [l for l in links if any(p["level"] == FIX_NOW for p in results[l["id"]]["problems"])]
    look = [l for l in links if l not in fix and results[l["id"]]["problems"]]
    ok = [l for l in links if not results[l["id"]]["problems"] and not results[l["id"]].get("notes")]
    by_person = [l for l in links if not results[l["id"]]["problems"] and results[l["id"]].get("notes")]
    not_by_person = [l for l in links if not l.get("checked_by_person")]

    out = [f"# Link check, {today.isoformat()}", ""]
    if not fix and not look:
        out.append(f"All {len(links)} links are fine. Nothing to do this week.")
    else:
        out.append(f"{len(fix)} need fixing now, {len(look)} need a look, "
                   f"{len(ok) + len(by_person)} are fine (out of {len(links)}).")
    out.append("")
    out.append(f"How the pages were read: {method}.")
    out.append("")

    def section(title, items):
        if not items:
            return
        out.append(f"## {title}")
        out.append("")
        for l in items:
            out.append(f"- **{l['topic']}** ({l['url']})")
            for p in results[l["id"]]["problems"]:
                out.append(f"  - {p['what']}")
            if results[l["id"]]["show"] == "backup":
                b = l["backup"]
                bits = [x for x in (b.get("label"), b.get("url"), b.get("phone")) if x]
                out.append(f"  - Workers see the backup instead: {', '.join(bits)}")
        out.append("")

    section("Fix now (workers are seeing the backup)", fix)
    section("Needs a look (link still shown)", look)
    if by_person:
        out.append("## Fine, but only a person can check these")
        out.append("")
        for l in by_person:
            out.append(f"- {l['topic']} ({l['url']}): the site blocks automatic checks. "
                       f"Open it yourself by {l['recheck_by']}.")
        out.append("")
    if ok:
        out.append("## Fine")
        out.append("")
        for l in ok:
            who = "a person" if l.get("checked_by_person") else "a web reader"
            out.append(f"- {l['topic']}: last read by {who} on {l['checked_on']}, recheck by {l['recheck_by']}")
        out.append("")
    if not_by_person:
        out.append("## Not yet read by a person")
        out.append("")
        out.append(f"{len(not_by_person)} links have only been read by a web reader so far. "
                   "Before launch, a person opens each one and confirms it fits its topic.")
        out.append("")
    return "\n".join(out)


# ---------------------------------------------------------------- main

def load_json(path: Path, default):
    return json.loads(path.read_text()) if path.exists() else default


def run(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--snapshot", type=Path, help="folder of saved page copies to check instead of the live web")
    ap.add_argument("--update-baseline", action="store_true", help="save today's page fingerprints as the new normal")
    ap.add_argument("--today", help="pretend today is this date (YYYY-MM-DD), for testing")
    args = ap.parse_args(argv)

    today = parse_date(args.today) if args.today else dt.date.today()
    library = load_json(LINKS_FILE, None)
    if library is None:
        print(f"Can't find {LINKS_FILE}", file=sys.stderr)
        return 2
    links = library["links"]
    baseline = load_json(BASELINE_FILE, {})

    results = {}
    for link in links:
        page = fetch_from_snapshot(args.snapshot, link["id"]) if args.snapshot else fetch_live(link["url"])
        results[link["id"]] = check_one(link, page, baseline.get(link["id"]), today)
        if not args.snapshot:
            time.sleep(1)  # be polite to the agencies' servers

    check_backups(links, results)

    # Save fingerprints: always for links never seen with this method, and
    # for all links when asked. A changed page is only re-saved on request.
    for link in links:
        fp = results[link["id"]]["fingerprint"]
        old = baseline.get(link["id"])
        if fp and (args.update_baseline or not old or old.get("method") != fp["method"]):
            baseline[link["id"]] = fp
    BASELINE_FILE.write_text(json.dumps(baseline, indent=2, sort_keys=True) + "\n")

    method = (f"saved copies from {args.snapshot.name}" if args.snapshot
              else "opened directly on the live web")
    REPORT_FILE.write_text(build_report(links, results, today, method) + "\n")
    STATUS_FILE.write_text(json.dumps({
        "checked_on": today.isoformat(),
        "links": {lid: {"show": r["show"], "problems": [p["what"] for p in r["problems"]],
                        "notes": r.get("notes", [])}
                  for lid, r in results.items()},
    }, indent=2) + "\n")

    needs_person = sum(1 for r in results.values() if r["problems"])
    print(REPORT_FILE.read_text())
    return 1 if needs_person else 0


if __name__ == "__main__":
    sys.exit(run())
