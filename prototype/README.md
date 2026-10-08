# OfferWatch prototype
Python 3.10+, standard library only (tested here on Linux with 3.10, 3.11, 3.12 and 3.13; **not yet on Windows**).
There is no installation, no API key, no LLM, no email and no billing. Nothing is ever sent.

```
python -m unittest discover -s prototype -v          # from the repository root (93 tests)
cd prototype
python offerwatch.py doctor                          # offline environment check + rehearsal
python rehearsal.py <empty-folder-outside-repo>      # two customers x five pages, synthetic
python -I review/repro_findings.py                   # current behaviour of every review finding
python compare.py before.json after.json comparison_demo.html   # stateless two-snapshot comparison
```

## Pipeline
`offerwatch.py run` does the following, without sending anything:
1. **collect** (capture.py): allowlisted pages, attested access rules, robots.txt, public addresses only, TLS verified, no redirects, bounded size and time;
2. **validate** each record against the customer's ledger;
3. **compare** by replaying accepted observations (store.py);
4. **prepare a DRAFT report** per customer (report.py).

| File | Role |
|---|---|
| `capture.py` | Guarded fetch, extraction (raw + normalised values, evidence excerpts, anomaly flags), manual-entry builder |
| `store.py` | Per-customer SQLite ledger. Observation, review, transition, report and delivery records are distinct, with tenant checks on every write. |
| `report.py` | Draft HTML. Every expected page gets an explicit state (CHANGED, UNCHANGED, BASELINE, SOURCE CHANGED, PENDING REVIEW, INCOMPLETE, CHECK FAILED, STALE, NOT CHECKED) with the age of the shown value and the reason nothing newer is available. |
| `offerwatch.py` | Operator CLI: run, review (batch), release, ack-delivery, delivery-unknown, import, add-source, attest-access, backup, restore, prune, status, task-xml, doctor |
| `compare.py`, `owcore.py` | Stateless comparator and shared helpers (tenant ids, conservative normalisation, safe links) |
| `rehearsal.py` | Offline two-customer rehearsal used by tests and `doctor` |

## Documents
| Document | Covers |
|---|---|
| [OPERATIONS.md](OPERATIONS.md) | Exact Windows steps, Task Scheduler (prepared, disabled), sleep/offline limits, backup/restore, retention |
| [AUTOMATION_POLICY.md](AUTOMATION_POLICY.md) | When an unchanged capture is processed without a new human review |
| [EFFORT.md](EFFORT.md) | Measured computer time vs estimated operator minutes, and the main bottleneck |
| [review/FINDINGS.md](review/FINDINGS.md) | Fixed, deferred and newly found issues |
| [samples/](samples/) | Synthetic draft reports, the rehearsal summary and a sample task definition |

All fixtures and samples are synthetic, using example.com/.net/.org hosts. Operational data lives in `%LOCALAPPDATA%\OfferWatch`; code refuses to store it inside a git working tree. **This repository is public**: never copy customer data, allowlists, ledgers, reports or logs into it.

Passing tests and an offline rehearsal are not evidence of live continuous monitoring.
