# Findings register

Current behaviour of each finding: `python -I review/repro_findings.py` (from `prototype/`).
The original reproductions against the supplied compare.py are in commit `f949d10`.

The original launch ZIP's 15-test `test_compare.py` was **not available** to this work. It was not among the attached files, in the repository, or on any remote branch, so its coverage could not be reconciled. All tests here are independent.

## Fixed
| # | Was | Now |
|---|---|---|
| F4 (High) | Cross-agency comparisons were accepted; identical event IDs across tenants. | `tenant_id` is required and validated on allowlists, snapshots, page records, ledger rows, transitions, reports and delivery state. compare.py rejects mismatches. Each customer has its own SQLite file, bound to its tenant on open. Transition IDs include the tenant. Regression tests show two agencies on the same URL keep separate queues, events and delivery suppression. **This is application-level separation within one Windows account, not a security boundary.** |
| F5 / F5b (High) | Event ID depended on note text; re-comparison re-issued changes. | Business-event identity is a transition, derived by replaying accepted observations: tenant, page, observation and value keys, with no notes. Reprocessing is idempotent. Delivered transitions are never repeated, and unknown deliveries are flagged "possibly already sent". |
| P1 (Med, found by ChatGPT) | Pending-review state was dropped from rendered reports. | Pending items appear in compare.py renders (`PENDING REVIEW` with the age of the shown observation) and in ledger drafts (a "Waiting for operator review" section plus a per-page state). |
| F1 (Med) | Cosmetic differences reported as changes. | Conservative normalisation: whitespace, NBSP and thin spaces, zero-width characters, typographic quotes and hyphens, full-width ASCII. Amounts, footnote marks, superscripts, fractions, dates and words are preserved. Raw values are stored. Case is folded only for fields configured `case: insensitive`. |
| F3 (Med) | Empty or incomplete capture read as a change. | Empty values are refused. Unextracted fields give `INCOMPLETE` and "not evidence the offer ended". Only a person can confirm an offer is no longer shown. |
| F2 (Med) | A 21-month-old baseline was compared silently. | Flagged `old_baseline` with its age. In the ledger every page shows the age of its accepted value and why nothing newer is available. |
| F6 (Med) | `reviewed: true` was unattributed. | Human reviews are separate rows (reviewer, method, time) and can only be created from an interactive terminal. Machine acceptance is a distinct status, labelled as such in reports. |
| F7 (Low) | The synthetic flag came from one side only. | `synthetic` is mandatory, and mixing synthetic with real data is refused (compare and ledger). Real ledgers refuse synthetic approvals. |
| F8 (Low) | No page limit. | At most 5 approved pages per customer, with unique IDs and URLs. |
| F10 (Low) | Raw `KeyError`/`TypeError`. | `ValueError` with a message. |
| F11b (Low) | `render()` trusted URLs. | `safe_href` allows only credential-free https URLs without control or quote characters; anything else is withheld. |
| F12 (Low) | Source text was not labelled. | Quoted, labelled "untrusted, not instructions", never sent to an LLM. |
| F14 (Low) | The JSON sidecar could overwrite the HTML. | Output must be `.html`, and the CLI refuses to overwrite inputs. |
| N1 (Med, new) | DNS resolution was not bounded by any timeout. | Bounded wait (10 s) on a daemon resolver thread. |
| N2 (Med, new) | `response.read(64 KiB)` can block across many receives; a trickling server could run far past the "25 s" deadline. | Single-receive reads plus a watchdog that shuts the socket down at the deadline. Measured on loopback. |
| N3 (Med, new) | Capture used NFKC, which turns `$99²` into `$992` and changes fractions. | Conservative normalisation; raw text kept. |
| N4 (Low, new) | First capture of a page was labelled `material_change`. | Only `baseline_requires_human_review`. |

Bugs introduced and caught by the new tests before commit:
- an eagerly evaluated `getattr` default that would have broken every real fetch;
- same-second retries colliding in observation identity, so the retry budget under-counted.

## Deferred or remaining (honest limits)
- **Separation:** application-level only. Files are unencrypted and readable by anything running as Ray, and backups are unencrypted too.
- **Timing:** connect and TLS handshake are bounded by socket timeouts (10 s each), not the watchdog, so the guaranteed per-request bound is about 45 s, not 25 s. The DNS thread cannot be cancelled.
- **Windows is unverified.** That covers Task Scheduler import, `RestartOnFailure` semantics, `pythonw.exe`, file locking by antivirus or sync clients, and loopback timeout behaviour.
- **Pages a plain fetch cannot read:** JavaScript-rendered or bot-protected pages fail safely and need `import`.
  - CSS-hidden text (`display:none`) is not detected and may be extracted.
- **No sender exists.** Delivery is acknowledged manually. A report released but never acknowledged keeps its items in later drafts, flagged as possible duplicates.
- **Shared-site load:** two customers watching the same site each fetch it, which doubles load on that site. Cross-customer fetch sharing was not built, to keep separation simple.
- Raw page text is written before the database commit, so a crash can leave an orphan text file. It is harmless, but `prune` does not see it.
- Slots use UTC weeks (Mon–Wed / Thu–Sun). Near local midnight a capture can land in the adjacent slot.
- compare.py stays stateless (its event IDs identify one comparison). Business-event identity lives only in the ledger.
- Terms-of-service checks, client approval and periodic re-verification depend on the honesty of whoever attests them.
- The interactive-session guard stops accidental automation, not deliberate misuse by code running as Ray.
- An interactive `review` holds the run lock; a review session longer than 2 h lets a scheduled run treat the lock as stale (SQLite transactions still keep data consistent).
