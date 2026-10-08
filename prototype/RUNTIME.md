# $0 runtime specification

## What is actually known about Ray's resources
- A GitHub account. Repository `galassoray/OfferWatch` is **public**.
- A claude.ai account.
- No list of Ray's computers, operating systems or uptime was provided. **I have not assumed any.**

## Recommendation: human-triggered runs on Ray's own computer
**Hardware:** any computer Ray already uses that runs Python 3.10+ (macOS, Windows or Linux).

Every successful automated capture must be human-reviewed before it can appear in a report. The person doing the review is the real cadence limit, so an unattended scheduler adds no delivered reliability. The plan is to run `fetch` at the start of each review session:

| Item | Spec |
|---|---|
| Schedule | Two check sessions/week (e.g. Tue and Fri mornings), plus report assembly on Fri. Reminders come from Ray's existing calendar. |
| Per session | `fetch` (≤5 pages, ≤10 requests, ~15 s), then `review` per page with the live page open. Use `import` when a page fails or needs JavaScript. |
| Downtime | A missed session means no capture. compare.py then reports `NOT RECHECKED`/`STALE OBSERVATION`/`NOT CHECKED`, never "removed". Missing two sessions breaks the "two checks/week" promise for that week. That is a service failure to disclose, not a silent gap. |
| Rate | ≤1 capture per page per 24 h is enforced. Robots.txt is fetched once per host per run, with a 2 s pause between pages. |
| Network | Direct HTTPS from Ray's own connection. Proxies/VPN are not supported, because the IP guard needs direct connections. |
| Data retention | Local disk only, in `captures/<tenant>/` (git-ignored). `prune --keep-days 35 --apply` removes records older than 35 days. Untrusted page text exists only in unreviewed capture files and is dropped from reviewed copies. There is no backup, so a disk loss loses history. That is acceptable for validation, but not for paying customers. |
| LLM use | None. Raw web text is never sent to an LLM or any other service. |
| Cost | $0 incremental. |

## Options considered and rejected for now
- **Unattended OS scheduler** (cron / launchd / Task Scheduler) on Ray's computer. This only works if the machine is on at the scheduled time. A laptop that is asleep or off misses runs: launchd catches up after sleep but not after power-off, and Task Scheduler needs "run as soon as possible after a missed start". It also produces unreviewed captures, so it adds no value until review happens. Revisit only if Ray has an always-on machine.
- **GitHub Actions scheduled workflow in `galassoray/OfferWatch`.** The repo is public, so workflow logs and artifacts would expose client names and watched pages: tenant leakage. Scheduled runs are best-effort and can be delayed or dropped. Scheduled workflows in public repos are disabled after 60 days without repository activity. Cloud-runner IPs are also more likely to be bot-challenged. A *private* repo has a limited free Actions-minutes allowance. Check the current figure on Ray's GitHub billing page; I have not assumed one. Not recommended until there is a private storage plan.
- **Claude Code routines / cloud sessions.** These would place raw web text inside an LLM session, which conflicts with the "no raw web text to an LLM by default" rule. Rejected.

## Honest bottom line
There is no unattended $0 option I can verify that reliably meets "two checks/week" **and** keeps tenant data private. The human-triggered plan meets the cadence exactly as reliably as Ray keeps two ~15-minute calendar slots. Passing tests are **not** evidence of live continuous monitoring. That evidence would be a dated log of real sessions over several consecutive weeks.
