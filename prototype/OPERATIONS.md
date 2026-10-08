# Operating OfferWatch on Ray's Windows laptop ($0)

Status: **prepared, not installed.** Nothing in this repository has been run on Ray's laptop. Every step marked *(unverified)* depends on that machine.

## What runs where
| Piece | Where | Notes |
|---|---|---|
| Code | `C:\Users\<you>\OfferWatch\prototype` (a copy of this public repo) | No customer data is ever written here. `DataDir` refuses any folder inside a git working tree. |
| Operational data | `%LOCALAPPDATA%\OfferWatch` | Holds `tenants\<customer>\` (allowlist, `ledger.sqlite3`, raw text, draft reports), `STATUS.txt`, `status.json` and `logs\`. Not synced. Do not move it into OneDrive: SQLite plus sync clients risk lock and corruption issues. |
| Scheduler | Windows Task Scheduler, current user, least privilege | Imported **disabled**. Enabling it is a separate step. |
| Sending | **Nothing.** | Ray sends released reports from his own mailbox, then acknowledges delivery. |

## One-time setup (exact commands, PowerShell)
1. **Python** *(unverified: version installed on the laptop)*. `py -3 --version` must print 3.10 or newer. If `py` is missing, install Python 3.12 from python.org with "py launcher" ticked. Microsoft Store Python also works for manual runs, but its `pythonw.exe` alias is *unverified* under Task Scheduler.
2. **Code:** download `https://github.com/galassoray/OfferWatch/archive/refs/heads/claude/offerwatch-validation-prototype-vdyly7.zip`, unzip to `C:\Users\<you>\OfferWatch`, then:
   `cd C:\Users\<you>\OfferWatch\prototype`
3. **Check (offline, about 1 minute):** `py -3 offerwatch.py doctor`. It prints the Python, SQLite and TLS details, checks the data folder and the `pythonw.exe` path, and runs the full synthetic rehearsal. **It makes no network requests.**

## Onboarding a customer (no JSON editing)
```
py -3 offerwatch.py add-source --tenant acme-agency --market "Metro X" --contact mailto:you@yourbusiness.example `
   --id "Rival HVAC" --url https://rival.example.com/specials --approved-by "Jane at Acme" --approved-at 2026-10-20T15:00:00Z `
   --field "advertised_offer=Fall special:\s*(.{1,120}?)\s*\|" --field "financing=Financing:\s*(.{1,120}?)\s*\|" --case-insensitive financing
py -3 offerwatch.py attest-access --tenant acme-agency --id "Rival HVAC"     # interactive: you read the terms
py -3 offerwatch.py run --tenant acme-agency                                  # first real collection
py -3 offerwatch.py review                                                    # approve 5 baselines once
```
Extraction patterns are per page. Writing them is the one onboarding step that needs someone to look at each real page.

## The one routine command
`py -3 offerwatch.py run` collects every due page for every customer, validates, compares, updates the ledger and prepares a **DRAFT** report per customer. It never sends.

Exit codes:

| Code | Meaning |
|---|---|
| 0 | OK |
| 2 | Completed with page failures |
| 1 | Error |
| 3 | Another run holds the lock |

It writes `%LOCALAPPDATA%\OfferWatch\STATUS.txt` (human summary, including last fully successful run and failures needing attention), `status.json`, and a one-line-per-run log without page text.

Due logic per page:
- **Slots:** two per ISO week, Mon–Wed and Thu–Sun (UTC). At most one successful capture per slot, and successes at least 24 h apart.
- **Retries:** up to 2 attempts per run, only for network errors, timeouts, DNS failures and HTTP 5xx. A 2 h backoff applies after a failure, with at most 4 failed attempts per slot. Robots denials, redirects, 4xx, challenges and non-HTML responses are not retried.
- **Repeats:** running the command again in the same slot is safe and makes no requests.

## Weekly operator routine
| When | Command | Notes |
|---|---|---|
| After each slot's collection | `py -3 offerwatch.py review` | One batch for all customers; exceptions only. |
| Weekly | `py -3 offerwatch.py release --tenant X --report 2026-W43-rN` | Then send the HTML yourself. |
| After sending | `py -3 offerwatch.py ack-delivery --tenant X --report ... --channel "email from my mailbox"` | |
| If unsure whether a report went out | `py -3 offerwatch.py delivery-unknown --tenant X --report ...` | Later drafts flag those items "possibly already sent in …". Check Sent items: if found, `ack-delivery` the same report; if not, send the next draft, which still contains the items. Nothing is lost and nothing is marked sent automatically. |

## Scheduled collection (prepared, not installed)
```
py -3 offerwatch.py task-xml --out "$env:USERPROFILE\OfferWatch-task.xml"          # writes a file only
Register-ScheduledTask -TaskName "OfferWatch Collect" -Xml (Get-Content "$env:USERPROFILE\OfferWatch-task.xml" -Raw)   # installs DISABLED
Enable-ScheduledTask   -TaskName "OfferWatch Collect"                                # turns it on
Get-ScheduledTaskInfo  -TaskName "OfferWatch Collect"                                # LastRunTime / LastTaskResult (0,1,2,3 above)
Disable-ScheduledTask  -TaskName "OfferWatch Collect"; Unregister-ScheduledTask -TaskName "OfferWatch Collect" -Confirm:$false   # undo
```
`samples/OfferWatch-task.sample.xml` shows the definition with placeholder paths.

| Setting | Value | Why |
|---|---|---|
| Triggers | Daily 09:15, and at logon + 15 min | The app's slot logic decides what is actually due, so extra triggers are cheap. |
| `StartWhenAvailable` | true | A missed 09:15 runs at the next opportunity. |
| `MultipleInstancesPolicy` | IgnoreNew | Plus the app's own `run.lock`. A lock older than 2 h is set aside as left by a killed run. |
| `RunOnlyIfNetworkAvailable` | true | |
| `ExecutionTimeLimit` | PT1H | Hard stop. Each page is committed as it completes, so a stop loses at most the page in flight. |
| `RestartOnFailure` | 2 × 15 min | *(unverified)* whether Windows applies this to non-zero exit codes. The app's own retries and the next day's trigger are the mechanism relied on. |
| `WakeToRun` | **false** | Deliberate: the laptop may be in a bag. |
| Battery | Allowed on battery | |
| Logon type | Interactive, least privilege | Runs only while Ray is logged on, and no password is stored. |

Other things that need checking on the laptop *(unverified)*:
- `Register-ScheduledTask -Xml` without a `UserId` registers the task for the current user.
- `pythonw.exe` exists beside `python.exe`. `doctor` checks this.
- Antivirus or Defender does not lock `ledger.sqlite3` during runs.

**Sleep and offline limits.** The task never wakes the laptop. While it is asleep, off, logged out or offline, nothing runs. If the laptop is unavailable for a whole slot (Mon–Wed or Thu–Sun), that slot has no capture. The report then shows `STALE` or `NOT CHECKED` with the age of the last accepted value; it never shows "removed". Catch-up is automatic once the laptop is back within the same slot.

**Preparing is not installing.** This repository only writes an XML file. Nothing changes on Ray's computer until he runs `Register-ScheduledTask`, and the task stays disabled until he runs `Enable-ScheduledTask`.

## Backup and restore
- **Backup (weekly; suggested destination: a USB drive):** `py -3 offerwatch.py backup --dest E:\OfferWatchBackups`. It writes a timestamped folder with consistent SQLite copies (online backup API), allowlists, raw text and reports, plus a SHA-256 manifest. Backups are **not encrypted**, so store them as you would client files.
- **Restore:** `py -3 offerwatch.py restore --from E:\OfferWatchBackups\offerwatch-backup-<time>`. It verifies every file hash and runs SQLite `integrity_check`, and confirms each ledger belongs to its folder's customer. The current `tenants` folder is **moved aside** to `pre-restore-<time>`, never deleted. Tested in `test_offerwatch.BackupTests` on Linux; *(unverified)* on Windows.
- **What a restore can lose:** captures and decisions made after the backup, and the ledger's memory of deliveries acknowledged after it. If you restore, re-run `ack-delivery` for any report you sent after the backup date.

## Retention: reconciling 35 vs 90 days
Neither number was adopted silently.
- 35 days was my earlier proposal for local capture files.
- 90 days appeared earlier only as GitHub's artifact default and as the proposal you mention.

This sprint splits retention by data type:

| Data | Proposed default | Why |
|---|---|---|
| Full raw page text (`raw\*.txt`) | 35 days, **but never while its capture awaits review** | Only needed to review or dispute a capture; it is the bulkiest third-party text. |
| Draft / void report files | 90 days | Re-prepared drafts accumulate. |
| Released / delivered report files | Kept until the customer offboards | It is the customer record. |
| Ledger rows (observations with short excerpts, reviews, transitions, reports, deliveries) | **Never pruned automatically** | Needed for pending work, duplicate prevention and A→B→A detection. |
| Logs | Rotate at 1 MB, keep one old file | |

`prune` is a dry run unless `--apply` is given, and it prints the policy it used. Override it with `%LOCALAPPDATA%\OfferWatch\retention.json`. **Owner decision needed:** confirm these defaults, and the offboarding rule (export, then delete the customer's folder; that is manual today).

## Worst-case timing (measured and analysed)
| Bound | Value | Basis |
|---|---|---|
| DNS | Wait ≤ 10 s | A daemon resolver thread; it can linger in the background until the OS gives up. |
| Response body / slow server | Cut at the 25 s deadline by a watchdog that shuts the socket down | Measured on loopback (Linux): a silent server and a byte-trickling server were both cut at about 1 s with a 1 s deadline. |
| Connect + TLS handshake | ≤ 10 s each, by socket timeout | The watchdog cannot interrupt them. |
| One request, guaranteed | ≈ 45 s | Not 25 s. |
| One page, worst case | ≈ 2.8 min | robots + 2 attempts + 30 s retry pause. |
| One customer (5 pages), worst case | ≈ 14 min | |
| One customer, typical (estimate) | 10–30 s | |
| Offline processing (measured) | 0.19 s | The whole two-week, two-customer rehearsal; 0.36 s wall-clock including interpreter start. |
