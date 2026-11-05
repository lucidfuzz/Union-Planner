# Union planner (public site)

Nothing is online yet. So far this folder holds the first piece of the plan: the link library
and the weekly link checker.

## What's here
- `links/links.json`: every official page the site will link to, with dates and backups.
- `links/REPORT.md` and `links/link-status.json`: made by each run (saved with the run on GitHub, not kept in the repo).
- `links/baseline.json`: each page's text "fingerprint" from the last accepted check.
- `tools/check_links.py`: the checker.
- `tests/test_links.py`: tests for the library and the checker.
- `rules/banned-words.txt`: words worker-facing text may never use.
- `.github/workflows/link-check.yml`: runs the checker every Monday.

## Commands
    python3 -m unittest discover -s tests -v     # run the tests
    python3 tools/check_links.py                 # check the live pages
    python3 tools/check_links.py --update-baseline   # accept today's page text as normal
