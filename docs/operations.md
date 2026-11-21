# Hourly operation

The collector, live builder and immutable publisher run as one ordinary user.
No runtime Git pull, branch change, sudo operation or service installation is
performed by the runner. Scheduling uses that user's crontab; it does not depend
on a systemd user manager or login persistence.

## Configure and pin reviewed code

Keep `config.env` private, owned by the run user and mode `0600`. Configure:

- `APPLICATION_DIR`: the application checkout.
- `APPLICATION_COMMIT` and `DEPLOYMENT_COMMIT`: exact reviewed 40-character
  lowercase commit hashes. This deployment runner requires an `1aeo/onionsurge`
  `APPLICATION_COMMIT` of at least `cd9ba793b5c7b7ad3aae102368a2fa82895a9bc3`
  (providing `collect.py --plan`, `--forbid-overlap`, `--record-checkpoint`,
  lossless compressed `hourly_archive` storage with 35-day raw `1h` pruning,
  daily `86400s` `schema_version: 7` build support alongside hourly `3600s`,
  and lean `--live` builds omitting offline-only `data.js`/`catalogue.json`
  alongside required `schema_version: 7` & `release_objects` in
  `build-manifest.json`).
- `STATE_DIR`: private state outside both source checkouts.
- `LOG_DIR`: private logs outside both source checkouts; defaults to
  `STATE_DIR/logs`.
- `STATE_RETAIN_RUNS`: optional integer (`2..168`, default `3`) controlling how
  many completed `runs/`, `candidates/` (always keeping active `current` and
  `previous`), and `cloudflare-*` directories are retained locally.
- `SOURCE_CONFIG`: path to the private mode-`0600` collector source
  configuration outside both checkouts (passed via `--config` to
  `onionsurge/scripts/collect.py`).
- `TIME_RANGES_CONFIG`: optional path to a non-symlink, non-group/world-writable
  JSON time-range policy file outside checkouts, `STATE_DIR`, `LOG_DIR`,
  `SOURCE_CONFIG`, and `LOCAL_SITE_DIR` (passed via `--time-ranges` to
  `onionsurge/scripts/build.py --live` and included in `--forbid-overlap`).
- `CRON_MINUTE`: optional UTC schedule integer (`0..59`); defaults to `5`
  (`5 * * * *`, running at minute `05` past each UTC hour). Legacy `CRON_HOUR`
  entries in existing configs are silently ignored.
- `CRON_UTC_GATE`: optional `true` or `false`, default `false`. The default uses a
  normal hourly cron entry and requires a UTC cron daemon or verified native
  `CRON_TZ=UTC` support. Use `true` on a non-UTC host whose cron implementation
  cannot schedule entries in UTC or uses a non-hour offset. Setting `TZ=UTC` for
  the command alone does not change when cron dispatches it.
- Optional `CLOUDFLARE_CANONICAL_HOST` / `CLOUDFLARE_REDIRECT_HOST` for
  configuring live Cloudflare domains in private `config.env` without editing
  tracked checkout files (see [cloudflare](cloudflare.md#ownership-and-scope)).
- The source collector and publication settings described in `1aeo/onionsurge`'s
  [query gateway](https://github.com/1aeo/onionsurge/blob/main/docs/query-gateway.md),
  [source install](https://github.com/1aeo/onionsurge/blob/main/docs/source-install.md),
  and [collector guide](https://github.com/1aeo/onionsurge/blob/main/docs/collector.md),
  plus [publication](publication.md) (and [local/VPN hosting](local-vpn.md) when `LOCAL_ENABLED="true"`).

Both checkouts must have the canonical `1aeo/onionsurge` or
`1aeo/onionsurge-deploy` GitHub origin and match their configured pins. Tracked
changes and untracked files are rejected; ignored private configuration and
generated files are permitted. When `origin/main` exists locally, the pin must
be an ancestor of that cached main ref. Refresh that ref during reviewed code
installation; the runner performs no network Git operation and cannot establish
remote review status by itself.

Run the complete pipeline once before scheduling:

```sh
python3 scripts/run_hourly.py --config /path/to/private/config.env
```

An optional `--end UNIX_SECONDS` selects a completed UTC hour accepted by the
collector (`end % 3600 == 0`). Omit it for the latest complete UTC hour (`(int(time.time()) // 3600) * 3600`).
If `published.json` and a current schema-7 candidate manifest already match that UTC hour, the runner exits cleanly
with `status: up_to_date` without re-collecting or re-publishing. Otherwise (including upgrading an existing schema-6 `00:00 UTC` release), the runner checks both source
pins before collection and again after the build, before publication. Pause the
managed schedule (`python3 scripts/schedule.py remove --config ...`) and wait
until active runs finish (`run_busy == false` and `last-run.json` is not
`running`; if an interrupted run left a stale `running` receipt, follow
[Handling a stale `running` receipt](#handling-a-stale-running-receipt) first)
before installing new code or updating `APPLICATION_DIR` /
`APPLICATION_COMMIT` (including upgrading to at least
`cd9ba793b5c7b7ad3aae102368a2fa82895a9bc3`) and resuming; these checks are not
protection against uncoordinated writers changing and reverting files while they
are being read.

Each run holds a nonblocking private `run.lock`. A second run returns `busy`
without collecting or replacing health receipts. The active run creates a
unique private `runs/<id>/` directory, invokes the pinned application's
`scripts/collect.py` subprocess with `--config SOURCE_CONFIG` and `--state-dir
STATE_DIR/collector` (validating only its bounded JSON summary on stdout without
logging child stdout/stderr), invokes `scripts/build.py --live` (passing
`--time-ranges TIME_RANGES_CONFIG` when configured) with
`reference-cache/` for daily reference reuse, and then runs publication under
the publisher's separate lock.

Automated catch-up is bounded to `MAX_AUTO_CATCHUP_DAYS = 31` UTC days. `run_hourly.py`
verifies `SOURCE_CONFIG` as an opaque mode-`0600` non-symlink regular file
outside checkouts and operational state paths without parsing its contents, and
verifies any optional `TIME_RANGES_CONFIG` as a regular non-symlink file that is
not group- or world-writable.
Before starting collection, the runner invokes `collect.py --plan` with
`--config SOURCE_CONFIG`, `--application-dir`, `--state-dir STATE_DIR/collector`,
`--end target_end`, `--previous-end prev_end` (when a prior release exists),
`--max-catchup-days 31`, and repeatable `--forbid-overlap` arguments covering the
source checkouts, log directory, runtime state paths, any `TIME_RANGES_CONFIG`, and any `LOCAL_SITE_DIR`.
`collect.py --plan` enforces collector checkpoint/seed continuity inside
`STATE_DIR/collector`, rejects gaps exceeding `31` days, and returns the bounded
`recommended_timeout_seconds` used as the collection subprocess timeout. Target
endpoints earlier than `prev_end` also fail closed before calling `--plan` or
`collect.py`. To resolve a gap exceeding `31` days safely:

1. Pause the managed schedule (`python3 scripts/schedule.py remove --config /path/to/private/config.env`)
   and wait until no active run holds `run.lock` (`run_busy == false` and
   `last-run.json` status is not `running`; clear any interrupted `running`
   receipt first via
   [Handling a stale `running` receipt](#handling-a-stale-running-receipt)).
2. Choose one explicit completed UTC hour `T` (`T % 3600 == 0`), use a fresh
   private manual output directory (matching your configured `SOURCE_CONFIG`,
   optional `TIME_RANGES_CONFIG`, `STATE_DIR/collector`, and
   `STATE_DIR/reference-cache`), verify
   `"cache_committed": true` from `collect.py`'s JSON summary before building
   or publishing, and stop on the first failed step (`set -e`) so the schedule
   stays paused unless collection, build, publication, and checkpoint
   recording all succeed (add `--time-ranges /path/to/private/time-ranges.json`
   to the `build.py` command when `TIME_RANGES_CONFIG` is set, and omit it when
   unset):

```sh
set -e
collect_summary="$(python3 /path/to/onionsurge/scripts/collect.py \
  --config /path/to/private/source.env \
  --state-dir /path/to/private/state/collector \
  --output /path/to/private/manual-run/snapshot.json \
  --end T)"

python3 -c 'import json, sys; sys.exit(0 if json.loads(sys.argv[1]).get("cache_committed") is True else 1)' \
  "$collect_summary"

# Add --time-ranges /path/to/private/time-ranges.json when TIME_RANGES_CONFIG is set; omit when unset.
python3 /path/to/onionsurge/scripts/build.py \
  --input /path/to/private/manual-run/snapshot.json \
  --output /path/to/private/manual-run/site \
  --cache /path/to/private/state/reference-cache \
  --live

python3 scripts/publish.py \
  --config /path/to/private/config.env \
  --build-dir /path/to/private/manual-run/site \
  --application-commit FULL_40_CHAR_APPLICATION_COMMIT \
  --deployment-commit FULL_40_CHAR_DEPLOYMENT_COMMIT

python3 scripts/run_hourly.py \
  --config /path/to/private/config.env \
  --record-checkpoint \
  --end T

python3 scripts/schedule.py install --config /path/to/private/config.env
python3 scripts/schedule.py verify --config /path/to/private/config.env
```

If `collect.py` exits with `"cache_committed": false`, `collect.py` rolled back
its collector observation and completion records for `T` (so `--record-checkpoint`
would reject it): do **not** proceed to `build.py`, `publish.py`, or
`--record-checkpoint`. Fix the collector cache failure, leave the schedule
paused, and rerun `collect.py` for the same `--end T` into a **new** private
`--output` path.

`--record-checkpoint` acquires `run.lock`, verifies the pinned checkouts, confirms
no `pending-promotion.json` exists, confirms a published schema-7 manifest
(`observation_end % 3600 == 0`), and delegates checkpoint persistence to
`collect.py --record-checkpoint` (optionally checked against `--end` and bounded
with `--forbid-overlap`).

Standard output contains a fixed status and, after publication, a release ID
and an optional fixed warning code. Detailed build output goes to mode-0600
`hourly.log`. Run receipts record
stages and exception types without copying raw exception messages or settings.
Failures before promotion leave the existing root unchanged; uncertain root
writes follow the publisher's pending-marker recovery procedure. A local
receipt/log failure after confirmed publication is reported as
`published_receipt_warning`, not as a rollback of the completed release.

## Install, verify and pause the user schedule

```sh
python3 scripts/schedule.py install --config /path/to/private/config.env
python3 scripts/schedule.py verify --config /path/to/private/config.env
python3 scripts/schedule.py remove --config /path/to/private/config.env
```

Install and remove replace only the block between
`# BEGIN ONIONSURGE MANAGED HOURLY` and
`# END ONIONSURGE MANAGED HOURLY` (also recognizing a legacy
`# BEGIN ONIONSURGE MANAGED DAILY` / `# END ONIONSURGE MANAGED DAILY` block so `verify` reports `drifted` and `install` upgrades it in-place, while rejecting mixed or duplicate markers). Unrelated entries and their order remain
unchanged. Duplicate, missing or reversed markers fail instead of guessing which
lines to remove. Reinstalling an unchanged block does not rewrite the crontab.
The previous complete crontab is saved privately as `crontab-before.txt` before
an update. Do not blindly restore it over later unrelated administrator edits.

The manager reads the crontab again before replacement and verifies it after
writing. There is no atomic compare-and-swap crontab API: serialize other
administrator/crontab tools during this short operation. An unexpected change
or failed verification requires inspection of the current table, not repeated
blind replacement.

The generated runner command prefixes `TZ=UTC `, pins the Python executable and runner/config paths, sets
`umask 077` for the command, and appends output to mode-0600 `cron.log` in the
private log directory. Shell arguments are quoted and percent signs are escaped
for cron before shell parsing. Backslashes and control characters in cron paths
are unsupported. The installer checks the effective `SHELL` at the managed
block's position. It accepts `sh`, `bash`, `dash`, `ksh` or `zsh` under `/bin/` or
`/usr/bin/` when that executable is installed; without an override, cron uses
`/bin/sh`. Other paths or incompatible shells are rejected before the crontab is
written. An incompatible override inserted before the block makes verification
report drift. Assignments after that block apply only to later entries. The manager leaves every shell,
environment and timezone assignment intact.
[Cron command parsing](https://man7.org/linux/man-pages/man5/crontab.5.html).

With `CRON_UTC_GATE=true`, the managed entry runs a small POSIX-shell clock check
every minute (`* * * * *`). It uses the resolved absolute system `date` path with
`-u +%M`, then invokes the same runner only when that UTC minute matches
`CRON_MINUTE`. Installation and verification require a root-owned,
non-writable-by-others executable and parent path, and prove its UTC output even
under a non-UTC `TZ` environment. Paths are never selected through `PATH` or a
configurable shell command. Cron percent escaping still applies to the date
format and all paths.

Other minutes start no Python runner, read no application configuration and make
no telemetry or provider requests. Clock errors fail closed and go to the same
private cron log; the log is also opened for otherwise silent clock checks.
The due runner retains its checkout-pin checks, nonblocking run lock, publication
lock and private receipts. A missed due minute is not caught up automatically:
run the normal runner manually or wait for the next scheduled hour. The manager
does not alter the host timezone or any crontab environment assignment. This mode
therefore does not depend on the daemon supporting `CRON_TZ`, and follows the
same UTC time through local daylight-saving changes.

`remove` means **pause future starts**. It does not kill a running collector,
builder or provider write. `verify` reports `paused`, `installed` or `drifted`,
whether the log is private, whether `run.lock` is held, whether publication is
pending recovery, and sanitized last-run/release status. It performs no
installation or repair. A drifted block exits nonzero.

## Health and recovery

Private state includes:

| Path | Meaning |
| --- | --- |
| `last-run.json` | Most recent attempted cycle and stage |
| `last-success.json` | Last cycle with confirmed publication and saved receipt |
| `published.json` | Publisher's committed root and provider identity hashes |
| `pending-promotion.json` | Automatic promotion is blocked pending operator recovery |
| `runs/` | Retained per-run aggregate inputs and live builds |
| `candidates/` | Retained immutable publication candidates |
| `collector/` | Collector-owned private bounded-query cache and checkpoint state |
| `reference-cache/` | Daily frozen reference cache |

An idle lock with `last_run_status: running` is inconclusive: a run may have been
interrupted, or a new run may have started between the lock and receipt checks.
Inspect publication state before retrying. A `published_receipt_warning` can
mean the public release advanced even though the latest success receipt was not
saved; use the publisher's receipt and verified public manifest to determine
what is live.

### Handling a stale `running` receipt

Because `run.lock` is released automatically when `run_hourly.py` is killed, a
crash after `last-run.json` records `status: "running"` leaves `run_busy: false`
while `last_run_status` remains `running`. Before upgrading pins or running
manual catch-up:

1. Confirm `schedule.py verify --config /path/to/private/config.env` reports
   `schedule: paused` (if not, run `schedule.py remove --config ...` first) and
   no active cron or manual invocation can start.
2. Check host processes (`ps -u "$(id -un)" -o pid=,ppid=,args=`) to confirm zero
   `run_hourly.py`, `collect.py`, `build.py`, `publish.py`, or `rclone`
   processes remain active for the checkouts or `STATE_DIR` (a terminated
   runner can leave child processes running briefly).
3. Recheck `schedule.py verify --config /path/to/private/config.env` to confirm
   `run_busy` stays `false`, and inspect `STATE_DIR/pending-promotion.json`; if a
   promotion marker exists, complete
   [publication recovery](publication.md#uncertain-promotion-and-recovery) first.
4. Once zero processes and zero pending promotions remain, clear the stale
   receipt by removing `STATE_DIR/last-run.json` (`runner_health` resets
   `last_run_status` to `none`), or—only when no code upgrade or manual
   catch-up is needed—run a normal `run_hourly.py` cycle to overwrite
   `last-run.json` with a completed receipt.

For normal code updates, remove the managed cron entry, wait for `run_busy` to
be false, install reviewed clean checkouts and update both private commit pins.
Run one cycle manually, verify public data and downloads, reinstall the schedule,
then verify two scheduled cycles and observation freshness. Other writers must
also be stopped or fenced before any root recovery or storage cutover; removing
this cron entry alone does not establish that fence. Follow the
[publication recovery procedure](publication.md#uncertain-promotion-and-recovery).

On clean runs (without `pending-promotion.json` or a `publication_warning`),
`run_hourly.py` prunes older completed `runs/` and `candidates/` directories
beyond `STATE_RETAIN_RUNS` (`2..168`, default `3`, always preserving `current`
and `previous` candidates plus any active or incomplete run/candidate
directories) and removes `reference-cache/YYYY-MM-DD/` directories older than 3
UTC days, while `cloudflare.py` prunes older completed `cloudflare-*` staging
directories beyond `STATE_RETAIN_RUNS`. Identical unchanged objects reuse
hardlinks across `candidates/` and `LOCAL_SITE_DIR/data/releases/`, `hourly.log`
and `cron.log` rotate automatically at `10 MiB` (retaining `.1`–`.3` in mode
`0600`), remote Spaces/R2 releases are never automatically deleted, and
`collector/observations.sqlite3` retains complete `daily`/`sampled_hour` history
back to ~May 2025 alongside permanent `hourly_archive` deltas (pruning only raw
`1h` scrape cache rows older than 35 days). Private state and logs never belong
in the frontend bundle or public storage prefix.
