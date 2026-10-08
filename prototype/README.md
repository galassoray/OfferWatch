# OfferWatch offline prototype
Python 3.10+; standard library only. No installation or API key required. (Tests were run on Python 3.13.)

From the repository root:
```
python -m unittest discover -s prototype -v
```
From this folder:
```
python compare.py before.json after.json comparison_demo.html
python -I review/repro_findings.py        # review reproductions, see review/FINDINGS.md
```
Open comparison_demo.html locally. All fixtures are synthetic. The report makes no real-world change claims. Validation requires timezone-aware observations, reviewed successful captures and HTTPS reference URLs; it compares supplied records rather than checking their truth. compare.py makes no network calls. A failed check never implies removal. Stable event IDs are generated, but persistent deduplication and delivery state are not implemented (see review/FINDINGS.md F4, F5). Unknown fields are not full schema enforcement; production hardening and independently reviewed collection are outstanding.

The business baseline report separately uses primary-source retrieved observations. Its observation date records research retrieval, not a guaranteed origin refresh time. Revalidate before customer use. No full third-party page copies are bundled.

## Capture / import adapter (capture.py)
This adapter produces page records for compare.py. It never crawls, follows links or redirects, logs in, handles CAPTCHAs, submits forms, schedules, sends messages, spends money or calls an LLM.

```
python capture.py fetch    --allowlist allowlist.json --captures captures [--only ID]
python capture.py review   --allowlist allowlist.json --captures captures --file captures/<tenant>/<file>.json --reviewer NAME
python capture.py import   --allowlist allowlist.json --captures captures --id ID --observed-at 2026-10-06T16:00:00Z --field 'advertised_offer=$79 tune-up' --reviewer NAME [--failed REASON]
python capture.py assemble --allowlist allowlist.json --captures captures --as-of 2026-10-09T18:00:00Z --out week.json
python capture.py prune    --allowlist allowlist.json --captures captures --keep-days 35 [--apply]
```
- **Allowlist** (see `allowlist.example.json`): one tenant, 1–5 exact HTTPS URLs on port 443. Each URL is a hostname (no IP), with no credentials or fragment. Each page records `approved_by`/`approved_at`. A synthetic allowlist refuses all network access.
- **Access rules before any request:** each page needs `access_rules_checked_by`/`_at`, a person's record that terms were read and the page is public with no login and no CAPTCHA. robots.txt is then checked. A 404/410 robots.txt allows access. Any other failure, including a redirect, disallows it. The User-Agent must carry a real contact.
- **Network guard:** DNS answers are refused if *any* address is non-public. The connection is pinned to the vetted IP with TLS verified for the hostname. Redirects are recorded, never followed. Requests carry no cookies and use identity encoding only. Responses are limited to text/html, 1.5 MB, a 10 s socket timeout and a 25 s total deadline. Pages are captured at most once per 24 h.
- **Output:** every automated record has `reviewed: false`, `review_status: review_required` and `source_times` (requested, received, server Date, Last-Modified). Values are NFKC/whitespace-normalised and capped at 200 characters. `missing_fields` is recorded. The visible page text is kept locally as `untrusted_source_text` for the reviewer. A failed check carries no facts.
- **Review:** only `review` at an interactive terminal can mark a record reviewed, after the reviewer types the page ID. compare.py rejects automated records without `reviewed_by`, `reviewed_at` and `review_method=interactive_cli`.
- **Manual fallback:** `import` records what a named person saw on an allowlisted page.
- **`assemble`** builds a snapshot from the latest human-reviewed record (or failed check) per page for one tenant. It lists newer unreviewed captures under `pending_review`.

The real HTTPS transport (`PinnedHTTPSConnection`) has **not** been exercised against a live server. All tests use fakes. See RUNTIME.md for the $0 runtime plan and its limits. This repository is public: keep real allowlists and captures out of it (`.gitignore`).
