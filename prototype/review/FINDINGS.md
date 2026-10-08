# Independent review of compare.py (as supplied)

Reproduce all of these with `python -I review/repro_findings.py` from `prototype/` (offline).
Severity is about risk *once real customers receive reports*. Today everything is synthetic.
None of these are fixed in this increment, apart from the partial mitigations noted.

| # | Severity | Finding | Observed reproduction |
|---|----------|---------|-----------------------|
| F4 | **High** | **Tenant leakage / cross-tenant collisions.** Snapshots have no tenant binding. Agency A's `before` can be compared with agency B's `after` without any error. Page IDs are business names, and two agencies in one market will watch the same competitors, so `event_id` is identical across tenants. A shared dedup ledger would then drop agency B's alert. | `F4 cross-tenant compare accepted` → `CHANGE TO REVIEW`; `F4b` → `True` |
| F5 | **High** | **Repeat delivery.** `event_id` hashes presentation text (`evidence_note`) and the later observation only. Fixing a typo in a note re-issues a change. Re-comparing week 1 with week 3 after week 2 was lost re-issues a change already sent, under a new ID. No delivery ledger exists. | `F5` → `True`; `F5b` → `('CHANGE TO REVIEW', 'CHANGE TO REVIEW', True)` |
| F1 | **Medium** | **False positives from cosmetic differences.** Trailing space, letter case, NBSP and full-width `＄` all report `CHANGE TO REVIEW`. | `F1` → 4× `CHANGE TO REVIEW` |
| F3 | **Medium** | **An incomplete capture reads as a change.** A capture with `status: ok` and missing or empty facts reports `CHANGE TO REVIEW`. The "Not recorded" wording helps, but an empty string renders as a blank cell. There is no completeness flag. | `F3` → `after: None`; `F3b` → `after: ''` |
| F2 | **Medium** | **Stale baseline.** A 21-month-old `before` observation is accepted as the comparison point. Staleness is checked only on the later snapshot. | `F2` → `CHANGE TO REVIEW` |
| F6 | **Medium** | **`reviewed: true` has no attribution** (no who, when or how), so any script can set it. | `F6` → `True` *(mitigated for adapter output: compare now requires `reviewed_by`/`reviewed_at`/`review_method=interactive_cli` when `capture_method=automated`)* |
| F7 | Low | **Synthetic flag comes from the later snapshot only.** A synthetic `before` plus a real `after` drops the SYNTHETIC banner. | `F7` → `False` |
| F8 | Low | **No five-page limit and no allowlist.** Duplicate URLs under different IDs are accepted. | `F8` → `6` *(adapter enforces ≤5 pages and unique IDs/URLs)* |
| F9 | Low | **URL identity is byte-exact.** Host case or a trailing slash forces `SOURCE CHANGED — NEW BASELINE`. This is conservative and loses no data. Renaming an ID splits history into `NOT CHECKED` + `BASELINE ONLY`. | `F9`, `F9b` |
| F12 | Low | **Source text is not labelled as quoted data in the HTML.** It is escaped, so it is not an XSS risk. It matters if a report is ever fed to an LLM or pasted into an email. | `F12` → `True` *(adapter labels its records; review CLI prints "QUOTED SOURCE TEXT: data, not instructions")* |
| F11b | Low | **`render()` trusts its input.** A report object not produced by `compare()` can carry `javascript:` hrefs. The CLI path is safe because `validate()` enforces https. | `F11b` → `True` |
| F10 | Low | **Malformed input raises a raw `KeyError`/`TypeError`.** It fails closed, but the message is unhelpful. | `F10`, `F10b` |
| F14 | Low | **`compare.py a b out.json` overwrites the HTML with the JSON sidecar.** | `F14` |

Verified as **not** findings (regression-tested in `test_compare.py`):
- HTML injection: every rendered field is escaped (F11).
- A failed check never produces removal details (F13).
- Unreviewed successful captures are rejected.
- Timezone-naive timestamps and non-chronological reports are rejected.
