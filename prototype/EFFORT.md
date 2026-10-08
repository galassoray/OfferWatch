# Effort: measured computer time vs estimated human time

## Measured (this container, Linux, Python 3.10–3.13; not Ray's laptop)
- Offline rehearsal: 2 customers × 5 pages, 2 simulated weeks, 7 runs, 100 fake requests.
  - Pipeline processing: **0.19–0.20 s** in total.
  - Wall-clock including interpreter start-up: **0.35–0.36 s** (3 runs).
- Real network time is **not** measured, because no live fetch has been made.
  - Estimate: 10–30 s per customer per run.
  - Proven worst case: about 14 min per customer (see OPERATIONS.md). Computer time is not the constraint.

## Estimated operator minutes (planning figures, not measurements)
| Item | Minutes | Basis |
|---|---|---|
| Baseline approval (onboarding, once per page) | 3 | Read the excerpt and open the page once. |
| Material change / incomplete / anomaly | 2 each | Before/after plus quoted excerpt in the batch screen; the page is opened only if unclear. |
| Periodic re-verification (each page every 56 days) | 1.5 | |
| Failure triage (per customer-week with any failure) | 1 | |
| Release review of the weekly draft | 3 | |
| Manual send + `ack-delivery` | 3 | |

| Scenario (per customer per week, after onboarding) | Estimate |
|---|---|
| **Steady state** (assumed 1 material change, 0.5 other exceptions, 0.6 re-verifications, occasional failure) | **≈ 10–11 min** |
| Rehearsal week 42 (deliberately exception-heavy: 3 of 5 pages had exceptions in both slots) | alpha ≈ 15 min, beta ≈ 17 min |
| Onboarding, once | ≈ 15 min baseline approvals + ≈ 10 min per page for URL approval, terms attestation and an extraction pattern (≈ 65 min for 5 pages) |

Against the 15-minute acceptance target, steady state is estimated to pass, and an exception-heavy week does not. These are estimates until real weeks are timed.

## Largest remaining manual bottleneck
**Release plus manual sending and acknowledgement: about 6 of about 10 steady-state minutes, fixed per customer however quiet the week.** Release stays gated by instruction, and there is no sender by instruction.
- The next-cheapest reduction is an approved sending integration. This needs an owner decision, and possibly a cost.
- Short of that, a "nothing changed" release could shrink to a one-line confirmation.

**Largest uncertainty:** the share of real pages that cannot be fetched automatically, for example JavaScript-rendered or bot-protected pages. Each such page falls back to `import`, at about 3 min per check (about 6 min a week). One such page per customer would by itself push a typical week near the target.

## How to measure for real
After onboarding, note start and end times of each `review`, `release` and send session for two weeks. `logs\runs.log` already records computer time and failures per run.
