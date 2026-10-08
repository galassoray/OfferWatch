# Exception-based review policy (rule `unchanged-complete-v1`)

Humans review **exceptions**, not every page. Machine processing and human approval are separate
statuses and are never relabelled as each other.

## Statuses (stored separately)
| Column | Values | Who sets it |
|---|---|---|
| `check_status` | `ok`, `failed` | the capture itself |
| `machine_status` + `machine_reasons` | `unchanged_complete`, `material_change`, `incomplete_extraction`, `extraction_anomaly`, `baseline_requires_human_review`, `source_url_changed`, `stale_capture`, `periodic_reverification_due`, `fetch_failed` | the ledger's rules |
| `acceptance` | `auto_accepted` (machine, rule named) · `human_accepted` (a person, interactive) · `fixture_accepted` (synthetic rehearsals only) · `pending_review` · `rejected` · `superseded` · `not_applicable` (failed checks) | rules or a person |
| `reviews` table | reviewer, method (`interactive_cli`, `manual_entry`, `synthetic_fixture`), time, decision, corrections, confirmed absences | a person only |

Reports label every value's basis. "Automatically re-checked; matched accepted values (rule unchanged-complete-v1). Not individually human-reviewed." is used for machine acceptance. "Human-reviewed by NAME on DATE" appears only for interactive reviews.

## When a capture is processed without a new human review
All of the following must hold:
1. The page already has a **human-approved** observation (the onboarding baseline).
2. The check succeeded, and the URL equals the approved URL (no redirects are followed).
3. **Every** tracked field was extracted, unless a reviewer has already confirmed that field is absent.
4. There are no extraction anomalies. Each field has a single distinct match, nothing was truncated, and the visible-text length is within 0.5×–2× of the last human-approved capture.
5. Every value equals the current accepted value after conservative normalisation. That normalisation covers whitespace, invisible characters, typographic quotes and hyphens, and full-width characters. Case is folded only for fields configured `case: insensitive`.
6. The capture is at most 96 h old when processed.
7. The last human-approved observation of the page is less than 56 days old (periodic re-verification).

Anything else goes to the review queue with its reasons. That includes material changes, missing fields, anomalies, stale data, URL changes and uncertain results.

Failed checks need no review. They never change accepted values, and they are reported as `CHECK FAILED`, or as `STALE` once the last accepted value is older than 96 h.

Accepting or correcting an older capture re-assesses later automatic acceptances. A capture that no longer matches the new baseline goes back to the queue (`baseline_changed_by_review`).

## Batch review
`py -3 offerwatch.py review` walks every customer's queue in one sitting, one item per page:
- The newest pending capture is shown; older pending captures of that page are superseded if it is accepted.
- Each item shows the reasons, the accepted value → the captured value, a short quoted excerpt labelled as untrusted data, and the source URL.
- Choices: accept · correct then accept (`ABSENT` confirms an offer is no longer shown) · reject · skip.
- Decisions are recorded only after the operator types the customer id. A new draft is then prepared.

The reviewer opens a live page only when the excerpt is not enough to decide.

## Never
- Machine processing is never labelled human approval.
- No code path creates a human review without an interactive terminal (`store.interactive_session` checks stdin and stdout are TTYs).
- Synthetic fixture approvals are refused by any non-synthetic ledger.
- Release and delivery acknowledgement are also interactive-only. Nothing is marked sent automatically.

The guard stops accidental automation; it is not a defence against deliberately malicious code running as the same Windows user.
