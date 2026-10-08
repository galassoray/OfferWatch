# Offline rehearsal (synthetic; no network)

| Run | Simulated time (UTC) | Outcome | Fake requests | Computer seconds |
|---|---|---|---|---|
| w41 Mon baseline collection | 2026-10-05T09:15:00Z | OK | 18 | 0.055 |
| w41 Thu routine collection | 2026-10-08T09:15:00Z | OK | 18 | 0.034 |
| w41 Thu repeat execution | 2026-10-08T09:40:00Z | OK | 0 | 0.012 |
| w42 Mon collection (killed mid-run) | 2026-10-12T07:00:00Z | INTERRUPTED (simulated kill) | 14 | 0.025 |
| w42 Mon retry 30 min later | 2026-10-12T07:30:00Z | LOCKED (overlap prevented) | 0 | 0.0 |
| w42 Mon after lock goes stale | 2026-10-12T09:31:00Z | COMPLETED WITH FAILURES (recovered stale lock) | 10 | 0.03 |
| w42 Thu collection | 2026-10-15T09:15:00Z | COMPLETED WITH FAILURES | 20 | 0.038 |

Total computer time (processing only; network latency excluded): 0.194 s

## Checks

- [x] unattested page never requested
- [x] repeat execution makes no requests
- [x] alpha-agency: Thursday unchanged pages auto-accepted with empty queue
- [x] beta-agency: Thursday unchanged pages auto-accepted with empty queue
- [x] overlap prevented while lock fresh
- [x] stale lock recovered
- [x] alpha-agency: each due page captured exactly once after recovery
- [x] beta-agency: each due page captured exactly once after recovery
- [x] note edits create no new business event
- [x] two customers on the same URL share no event identities
- [x] beta keeps its own pending change after alpha accepted it
- [x] alpha-agency: re-preparing identical draft is a no-op
- [x] beta-agency: re-preparing identical draft is a no-op
- [x] A->B->A gives two distinct alpha events
- [x] beta records its own A->B->A after its own review
- [x] same URL, different customers: no shared event ids
- [x] week-41 baselines not repeated after acknowledged delivery
- [x] every expected page has an explicit state
- [x] alpha unattested page reported NOT CHECKED
- [x] incomplete capture reported, not as removal
- [x] repeated failures reported STALE with reason

## Final week-42 page states

- **alpha-agency**: Alpha Duct Doctors: NOT CHECKED; Alpha Furnace Fixers: UNCHANGED; Shared Air Partners: STALE; Shared Cooling Co: CHANGED; Shared Heating Pros: INCOMPLETE
- **beta-agency**: Beta Coil Care: UNCHANGED; Beta Vent Velocity: PENDING REVIEW; Shared Air Partners: STALE; Shared Cooling Co: CHANGED; Shared Heating Pros: CHANGED

## Estimated operator minutes, week 42 (estimate, not measured)

- alpha-agency: onboarding 4 baseline items ~12.0 min (once); week 42: 4 exception captures ~8.0 min + 7.0 min fixed (failure triage, release, manual send + ack) = ~15.0 min
- beta-agency: onboarding 5 baseline items ~15.0 min (once); week 42: 5 exception captures ~10.0 min + 7.0 min fixed (failure triage, release, manual send + ack) = ~17.0 min
