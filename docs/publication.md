# Collector and publication contract

Intended flow: private monitoring → scheduled collector → immutable DigitalOcean
Spaces releases → Cloudflare, with optional R2 replication. The object publisher
is `scripts/publish.py`; installation and scheduling are separate operations.
Merging this code does not enable a job or publish data.

## Prepare and publish data

Build with the application's explicit `--live` mode (which omits offline-only `data.js`
and `catalogue.json`). Its `onionsurge-build-v1` manifest must declare `mode: live`,
`schema_version: 7`, the observation endpoint, `query_contract_sha256`
(matching `data.json`'s `provenance.query_contract_sha256`), `release_objects`
matching the validated data objects, and the SHA-256 of every file. The publisher
verifies the complete build file inventory, hashes, and contract hash, then selects
only compact `data.json`, its frozen reference, and every configured period CSV
export for every chart (`exports/<chart_id>-<export_suffix>.csv`). It does not
upload `data.js`, `catalogue.json`, the frontend, configuration, logs or private
evidence. The default 13-chart, 7-period (`24h`, `7d`, `1mo`, `3mo`, `6mo`, `12mo`,
`all-time`) configuration produces 93 data objects (cap: 512 objects per release
manifest, 528 declared build files).

```sh
python3 scripts/publish.py --config /path/to/private/config.env \
  --build-dir /path/to/live-build \
  --application-commit APPLICATION_COMMIT_SHA \
  --deployment-commit DEPLOYMENT_COMMIT_SHA --prepare-only
```

`--prepare-only` creates a private candidate without opening storage connections.
Omit it to prepare a fresh candidate and publish. The two commit arguments must
be full 40-character lowercase hashes. They record the operator's pinned source
revisions; they do not independently prove which source built the input files.
Run the tested, pinned checkouts and preserve the build manifest alongside the
private publication state.

`STATE_DIR` must be private and owned by the publisher. One nonblocking lock
serializes candidate publication and recovery. Completed candidates are retained
under `candidates/<id>/`, with `manifest.json` written last, and identical
unchanged objects across releases reuse hardlinks (`link_or_atomic_write`) both in
`candidates/` and in local `LOCAL_SITE_DIR/data/releases/` mirrors. A pre-promotion
failure can leave an incomplete private candidate or harmless immutable remote
objects; the next normal run creates a new candidate. Remote Spaces/R2 objects
are never automatically deleted; successful scheduled runs prune older completed
candidates beyond `STATE_RETAIN_RUNS` (always retaining `current`, `previous`,
and incomplete candidate directories).

Every selected object is limited to 8 MiB, the release manifest to 128 KiB and the
complete candidate to 32 MiB. Oversized builds fail without truncating history or
moving a pointer. Data schema 7 (`window_seconds` in `{3600, 86400}` with
corresponding `3600 s` UTC-hour or `86400 s` UTC-midnight `end` alignment,
`end - start == 86400`, `time_ranges.periods` with 1..16 unique non-sub-24h
`export_suffix` slugs matching `charts` x `periods`, required
`build-manifest.json` `schema_version == 7` / `release_objects` agreement,
allowing `-24h.csv` while rejecting strictly sub-24h suffixes such as `-1d`/`-0h`..`-23h`/`-024h`/`-30m`/`-1m`/`-60s` and `-foo-1h`), observation endpoints, reference
ID/path/full hash/window/cutoff and method identity must agree. A frozen daily
reference may come from an older input snapshot (`lag_days=1`, ending on `<= data['start']`);
its source hash need not equal the current input hash. Once a release is published,
advancing `observation_end` requires `>= 3600 s` (same-hour re-publications keep
an equal `observation_end`).

The public release manifest contains only format/schema identifiers, release ID,
observation/generation timestamps, application/deployment/query-contract hashes
and object byte counts/hashes. The root is `onionsurge-root-v1`, with `updated_at`
and `current`/`previous` descriptors containing `id`, `manifest_sha256` and
`manifest_bytes`; `previous` is null on the first release. Generation time is the
actual preparation time, separate from the observation endpoint.

## Storage boundary

Configure `DO_ENABLED="true"`, `RCLONE_PATH`, `DO_SPACES_REGION`, `DO_SPACES_BUCKET`,
`DO_SPACES_PREFIX`, `DO_SPACES_KEY` and `DO_SPACES_SECRET` privately when publishing to Spaces (`DO_ENABLED` defaults to `"false"` in `config.env.example` alongside `LOCAL_ENABLED="true"` for private local/VPN hosting). Optional R2
uses `R2_ENABLED`, `R2_BUCKET`, `R2_PREFIX`, `R2_ACCESS_KEY_ID`,
`R2_SECRET_ACCESS_KEY` and `CLOUDFLARE_ACCOUNT_ID`. Each prefix is a required,
nonempty project component such as `onionsurge`, without slashes. Protect that
namespace from other writers and bucket lifecycle rules before publication.

The transport uses the installed rclone's `rcat` with the exact validated bytes
on stdin, plus full `cat` reads under that prefix;
it never uses sync or delete. An exact-key `lsjson --stat` probe distinguishes
an absent S3 virtual-directory result from an existing zero-byte file. Empty
`cat` output alone does not establish absence. Credentials exist only in the child's private
environment; inherited rclone/AWS options and proxies are not imported. Every
command uses `/dev/null` configuration and `--s3-no-check-bucket`. Immutable
objects receive long cache metadata; the root receives `no-store`. Only explicit
HTTP metadata is set: local ownership and extended attributes are not copied.
Uploads deliberately omit `--size`: the known-size rcat path in rclone 1.72.0
does not pass upload-header options to S3. The no-size path uses explicit
`--header-upload` values and a streaming cutoff one byte above the supported
8 MiB object limit, keeping each bounded input in memory without reopening a
file. The subprocess still has a 90-second deadline and one retry attempt.
See [rclone metadata](https://rclone.org/docs/#metadata) and
[S3 metadata fields](https://rclone.org/s3/#metadata).

An existing immutable object must match its expected bytes and SHA-256; a
conflict stops publication without replacing it. The upload does not reopen the
candidate pathname after validation. The existence check and upload rely on the
documented single cooperating writer; `rcat` is not a conditional-create API.
[Rclone stdin uploads](https://rclone.org/commands/rclone_rcat/).
Every configured backend must
pass full read-back verification of the complete release before either root is
advanced. Stored cache metadata is read with `lsjson --stat --metadata`: each
immutable object/manifest must retain its immutable cache policy, and every
mutable root must retain exactly `no-store`. Missing or different cache metadata
fails verification even when the bytes match. A root-header failure retains the
pending promotion marker. Fenced recovery, rollback and destination adoption
perform the same header checks before reconciling receipts.
Existing provider roots must match the private last-published receipt;
an unmanaged existing root, drift or destination change requires operator
reconciliation. Storage read-back uses authenticated provider access; rollout
checks must also exercise the public Worker URLs and their anonymous origin
access where applicable.

The optional real-rclone regression uses only a loopback S3 fixture and synthetic
credentials, including a full-size supported object. It never contacts cloud
storage:

```sh
ONIONSURGE_TEST_RCLONE=/path/to/rclone python3 -m unittest discover \
  -s tests -p test_publication_s3.py -v
```

## Uncertain promotion and recovery

Before the first mutable root write, the publisher durably records
`pending-promotion.json`. Root commands have one high-level and one low-level
rclone attempt. A failed command, timeout, interrupt or failed final bookkeeping
before roots and the durable receipt are confirmed leaves the pending state and
blocks later automatic promotions. A GET showing an
old value does not prove that a timed-out write cannot arrive later.

After every root is verified and `published.json` is durable, marker-cleanup
failures cannot undo publication. The publisher reports `status: published` with
`pending_marker_cleanup_failed` if the marker could not be removed, or
`pending_marker_removal_not_durable` if its removal could not be synced. The
runner preserves the fixed warning in its result, receipts and health output as
`published_receipt_warning`. In the latter case the marker is absent now but may
reappear after a crash. If it is present, use fenced recovery; if absent, inspect
the committed receipt and roots rather than invoking recovery on a missing file.

1. Stop all scheduled/manual root writers. Join every outstanding write or fence
   its authorization on every provider so it cannot arrive later. Keep the writers
   paused. A local lock or elapsed time alone is insufficient.
2. Inspect the private pending receipt and candidate. Compare every provider
   with the **pending receipt's exact root bytes**, not merely with the other
   providers. If any root differs or is absent, fully verify the pending
   candidate and its fallback objects, then use a separately reviewed operator
   root-write operation to converge all providers to that pending root after
   the write fence is established. This includes unanimous old roots when the
   first write never took effect, and a first publication where no root exists.
   Preserve all immutable releases. The recovery command performs no remote
   writes and never guesses a winner.
3. With all roots selecting the pending root, run:

   ```sh
   python3 scripts/publish.py --config /path/to/private/config.env \
     --recover --confirm-writers-fenced
   ```

   This acknowledgement asserts an operational fact; the program cannot prove
   that other machines or provider requests have been fenced. Recovery checks
   every root, the complete current and previous releases on every backend, then
   rechecks all roots. Only a complete match updates the private receipt and
   clears the pending marker. Mismatches leave the marker in place.
4. Verify the public release through Cloudflare before resuming the writer. Use
   the cache-generation procedure below for rollback or destination cutover.

Do not delete a pending marker to bypass an uncertain write. Lost local state
requires the same operator fence and a reviewed state/root reconciliation.
For a planned destination change, `--migrate-destination --confirm-writers-fenced`
verifies the copied current/previous releases and roots before updating private
destination pins; it preserves the old/new receipts and performs no remote
writes. Follow the [Spaces migration guide](spaces-migration.md).
For an already-restored rollback root on the same destinations,
`--reconcile-root RESTORED_ROOT_JSON --confirm-writers-fenced` verifies the
explicit canonical root and its complete current/previous releases everywhere,
rechecks every root, preserves old/new receipts in `root-reconciliation-*.json`,
and only then updates `published.json`. It neither writes remote roots nor clears
an uncertain promotion marker. Complete pending-promotion recovery first.
This is a single-writer publication protocol, not distributed
compare-and-swap or automatic recovery from conflicting operators.

## Collection and release semantics

Each hourly run queries through the last complete UTC hour using
`[1h]` and `[1d]` estimators plus coarsened eligible/available populations. Preserve counter
resets before aggregation, gauge semantics, null gaps and metric introduction
boundaries. Counters do not establish attacker or client counts.

The application checkout (`1aeo/onionsurge`) selects fixed IDs through its
query contract and records `query_contract_sha256` in `build-manifest.json` and
`data.json` provenance. Schema-7 releases support both hourly (`3,600 s`,
including `-24h.csv` and `hours` periods) and daily (`86,400 s`, requiring UTC
midnight bounds and omitting `hours` periods, `-24h.csv`, and sub-24h series
fields) builds across the configured `time_ranges` periods
(defaulting to `24 hours`, `7 days`, `1 month`, `3 months`, `6 months`, `12 months` and `All time` CSVs when hourly) with
`dir-spec` coarsening applied before persistence or publication.

Refresh 18-month references daily from cached daily aggregates with prior 31-day
population context, sample/coverage gates, `lag_days=1` cutoff and query/cohort-rule versions.
Calculate current `24-hour average` ranks against one immutable 18-month reference
version (and configured period percentile rulers, defaulting to `Last 1 month` / `Last 6 months` / `All-time`)
using the declared midpoint tie rule. Scale means by the current coarsened eligible relay count;
current windows must not enter their own reference distribution.

For a release:

1. Pin application, deployment, query-contract and reference identities.
2. Validate units, intervals, denominators, versions, schemas and no-data states;
   prepare only sanitized public objects.
3. Upload immutable objects, including the release manifest, to every configured
   backend and verify checksums. A release is complete only when all configured
   backends hold every referenced object; a slow or failed mirror delays promotion.
4. Only then advance either root manifest. Both root pointers name that release
   and its previous complete release. Pointer updates need not be simultaneous:
   either selected release already exists on every backend. Retain prior objects.
5. The browser binds data, reference and downloads to one immutable release ID.
   If any object is missing or fails verification, discard the entire candidate
   and retain the already loaded complete release. On a cold load, try the root
   manifest's previous complete release and validate it as a whole before use.
   Never substitute an older object into the selected release. The Worker only
   retries the same object key on another backend; release selection belongs to
   the publisher and client. If neither release loads completely, show unavailable.
6. Retain the last complete release on failure. Report observation age separately
   from generation time. Use the cache-aware rollback sequence below.

## Root cache generation

`ROOT_CACHE_GENERATION` is the Worker binding that fences mutable object-cache
entries. Configure a unique identifier before initial production deployment;
missing or invalid values make mutable data routes fail closed. UUID-style
letters, digits, underscores and hyphens are accepted, up to 128 characters.
Record the value with the Worker deployment. Never reactivate a retired value.

At request admission the Worker captures this binding once, before any cache
lookup. Its private keys for `manifest.json`, `data.json`, `data.js` and root
`exports/` aliases include that captured generation. The same key is used for
lookup and background writes. A late old-generation write cannot populate a key
read by new-generation requests. Immutable release/history/reference objects keep
their existing generation-independent keys and cache lifetimes.

Root-manifest responses to browsers use `Cache-Control: no-store`, including GET,
HEAD and edge-cache hits, from the **first production deployment**. The separate
internal cached response retains its 60-second lifetime. Thus a late root response
also cannot populate a browser cache used by the restored frontend. Keep this
policy when restoring older frontend artifacts. Provider root aliases must be
served from uncached/direct origins, with `no-store` metadata if a provider/CDN
layer is present; generation changes do not repair stale upstream root copies.

## Rollback across schema versions

1. Pause **all** scheduled and manual writers of provider root pointers. Before
   restoring roots, join every pending promotion and confirm its writes completed,
   or fence it so it cannot write afterward, on **every** configured provider.
   If any write's outcome remains uncertain, stay in maintenance and do not
   proceed. Keep these writers paused through frontend verification. Choose a
   prior complete release and its compatible frontend; verify its manifest,
   schema, data, references and downloads on every configured backend. Select a
   compatible previous-release fallback as well. Retain the target and superseded
   immutable objects throughout rollback.
   Complete any pending-marker recovery before starting root reconciliation;
   the rollback action does not abandon an uncertain promotion.
2. Restore and verify every configured provider root manifest to that target and
   fallback. Ensure the Worker’s configured provider endpoints return the restored
   root, including any provider/CDN layer. Keep a frontend that supports both
   schemas, or a maintenance response, during the incompatible transition.
3. Generate a fresh, never-used `ROOT_CACHE_GENERATION` and deploy a Worker version
   carrying that binding **after** the providers are restored. Keep the current or
   transition frontend in this deployment. Complete the Worker rollout to **100% of
   traffic across all serving locations** before switching to an incompatible
   frontend; a partial/gradual rollout is not the cutover. Do not roll back to a
   Worker version that lacks the generation fence or root `no-store` policy.
4. Verify the restored release ID and schema through
   `https://example.com/onionsurge/data/manifest.json`, then fetch and checksum its
   immutable manifest, data, reference and download objects through their canonical
   public URLs. Confirm the Worker deployment is the new generation and all roots
   select the same complete release. A direct bucket read alone is insufficient.
5. With all provider-root writers still paused, re-read every provider root and
   confirm it is unchanged from step 4. Stop in maintenance if any root changed or
   any promotion remains unaccounted for. Only then switch to the compatible
   frontend while retaining the **active** new generation. Cold-load
   `https://example.com/onionsurge/` and verify the root, selected release, schema and
   reference IDs as one coherent release, including its declared previous-release
   fallback.
6. Reconcile the private publisher receipt with the restored root before any
   hourly promotion resumes. Use the exact canonical bytes retained from the
   selected publication as the explicit input; do not reformat the root.

   For a **cross-schema rollback** (when the restored `root['current']` uses
   schema 6 rather than schema 7), install reviewed, pinned
   collector/application/publisher code compatible with the restored schema and
   the active application query contract **before** running reconciliation — this schema-7
   publisher rejects non-schema-7 `root['current']` targets (retaining
   schema-6 `allow_previous_v6` read support only for `root['previous']` during
   rollovers). Its reconciliation action must support
   that schema and preserve the same verification/fencing guarantees. Do not
   bypass schema checks or hand-edit receipt pins to resume; if compatible code
   is unavailable, keep promotions paused.

   For schema-7 targets (or once compatible code is installed for the restored
   schema), run:

   ```sh
   python3 scripts/publish.py --config /path/to/private/config.env \
     --reconcile-root /path/to/private/restored-root.json --confirm-writers-fenced
   ```

   This action verifies complete current/previous releases on all configured
   providers and preserves the superseded receipt. Without it, an old local
   receipt can correctly stop the next run with `provider_root_drift`.
7. Once compatible receipt reconciliation and public checks pass, run one manual
   cycle, verify its complete public release, then resume scheduled promotions.

Old-generation cache entries can expire or be removed as separate cleanup.
Neither cleanup, a purge, a sleep nor completion of old Worker requests/cache writes
is a prerequisite for serving the new generation. Provider-root writes must still
meet step 1's completion or fencing requirement. Keep prior immutable objects
available for clients already bound to them; rollback does not authorize deletion.
Visitor query strings cannot select a generation or bypass the private cache key.

Keep collection state separate from the publication lock. The publisher
serializes promotions, and a failed mirror delays promotion of that candidate.
Do not rescan the full database daily or hourly. Never publish raw database snapshots,
memory captures, host rosters or credentials.

Before deployment, test partial uploads, checksum failure, stale reference
refresh, fallback, schema compatibility and rollback. Verify two scheduled cycles
after installation. Merging this repository does not run deployment operations.

Local frontend artifacts use the same single-release principle: `prepare_assets.py`
publishes one new release root containing `public/` and `asset-release.json` in
one no-replace rename. A post-commit cleanup warning does not roll back that
release. For interrupted or uncertain staging, follow the receipt/checksum
[recovery procedure](../README.md#prepare-public-assets) before retrying; do not
automatically remove any published root.
