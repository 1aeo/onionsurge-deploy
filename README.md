# OnionSurge deployment

Hourly collection and publication (windows close on completed UTC hours `end % 3600 == 0`; scheduled run defaults
to UTC minute `5`, using native UTC cron or the opt-in portable UTC gate) for
[OnionSurge](https://github.com/1aeo/onionsurge).

The runner invokes the pinned [OnionSurge](https://github.com/1aeo/onionsurge)
checkout to collect hourly and daily aggregates and build live outputs, verifies
immutable releases in local storage, DigitalOcean Spaces, or Cloudflare R2, and
promotes a small current/previous manifest for [private local/VPN hosting](docs/local-vpn.md)
or an optional Cloudflare Worker. Cloning or testing this repository performs no deployment.

## Production setup

1. Install clean, reviewed `main` commits of both repositories (`1aeo/onionsurge`
   must be at least `cd9ba793b5c7b7ad3aae102368a2fa82895a9bc3`). When upgrading
   an existing host, pause the managed schedule, wait until active runs finish
   (`run_busy == false`), and update `APPLICATION_DIR` and `APPLICATION_COMMIT`
   before resuming. All configuration, state, logs, source credentials and
   provider credentials remain private.
2. Copy [config.env.example](config.env.example) to `config.env`, set mode `0600`,
   and fill in the exact commit pins, `SOURCE_CONFIG`, optional
   `TIME_RANGES_CONFIG`, publication target, and deployment settings
   (`LOCAL_ENABLED="true"`, `DO_ENABLED="false"`, `R2_ENABLED="false"` by
   default for [private local/VPN hosting](docs/local-vpn.md) with
   `LOCAL_SITE_DIR`, or set `DO_ENABLED="true"` / `R2_ENABLED="true"` for
   Spaces/Cloudflare).
   The real file is ignored by Git; the parser accepts assignments without
   executing a shell.
3. Install the restricted source gateway and create the mode-`0600` `SOURCE_CONFIG`
   file (for example `${HOME}/.config/onionsurge/source.env`) following
   [1aeo/onionsurge source install](https://github.com/1aeo/onionsurge/blob/main/docs/source-install.md)
   and [collector docs](https://github.com/1aeo/onionsurge/blob/main/docs/collector.md).
4. Run and verify one [hourly cycle](docs/operations.md), stage its four live
   frontend assets with `--live`, and complete either [deploy Cloudflare](docs/cloudflare.md)
   or [private local/VPN hosting](docs/local-vpn.md).
5. Verify public manifests, downloads and observation times, then install and
   verify the managed hourly schedule. On a non-UTC cron host without native
   per-entry timezone support (or non-hour timezone offsets), opt in with `CRON_UTC_GATE=true`; only the due UTC
   minute invokes the runner, and other jobs and timezone settings stay intact.

When temporarily sharing an existing bucket, protect the `onionsurge/` prefix
from every other writer before uploading. The publisher never runs a bucket-root
sync or deletes remote objects. Follow the [dedicated Spaces migration guide](docs/spaces-migration.md)
when the new bucket is ready.

## Validate

Validation supports Linux and macOS with Python 3.11+, Node.js 22+ and `make`. Tests need no private data, provider
credentials or application checkout.

```sh
make check
# If the local default Python is older:
make check PYTHON=python3.12
```

Checks cover encoded routes, storage timeout/fallback, caching, immutable asset
staging, release manifest and `query_contract_sha256` verification, subprocess
runner isolation, and hourly UTC schedule management.

## Prepare public assets

Build an exact reviewed commit of `1aeo/onionsurge`, then pass its public output
directory explicitly:

```sh
python3 scripts/prepare_assets.py \
  --source /path/to/live-build --live \
  --application-commit FULL_40_CHARACTER_COMMIT_SHA
```

`--output` names a **new release root**, defaulting to `build/`, not its public
subdirectory. The completed layout is:

```text
build/
  public/                 # Only these assets enter the Worker binding
  asset-release.json      # Application commit and verified file hashes
```

All files are validated in private staging (schema 7 `data.json`,
`build-manifest.json` `schema_version == 7`, `release_objects` / `exports/*.csv`
consistency allowing `-24h.csv` while strictly sub-24h export suffixes remain
blocked). One atomic no-replace directory
rename publishes the public assets and their receipt together. An existing
release root is never overwritten, and a published release is never deleted as
rollback. To stage another release, choose a fresh root with `--output`; point
the asset binding at that root's `public/` directory. The example binding remains
`../build/public` for the default root. The receipt does not record the input
machine's path.

A post-publication staging-cleanup error returns a completed release with a
warning; inspect the named temporary directory separately. Interrupts retain any
published release. If a command is killed or its result is uncertain:

1. Inspect the requested release root before retrying. If it exists, require both
   `public/` and `asset-release.json`. Compare its application commit and
   `build_manifest_sha256` with the retained `build-manifest.json` from the
   **intended build**, then match every receipt object name/hash to that manifest.
   The same code commit can produce different hourly data, so the commit alone
   is insufficient. Verify the published file set, byte counts and SHA-256 values
   against those intended objects. A verified complete root is an
   already completed release, even if the interrupted command reported no result.
2. Inspect the parent's `.onionsurge-stage-*` directories, including unreported
   stages left by a hard kill, even if a complete release is visible. Use an
   exclusive staging workspace, or stop and confirm completion of
   **all staging jobs sharing that parent** before deciding which stages are
   orphaned. After all jobs stop, recheck the original requested release root: a
   job may have published it during the wait. If it exists, verify it against the
   intended build using step 1. Inspect each candidate and remove only confirmed
   orphans regardless of whether the root exists; never remove by prefix while
   another job might own one. Retry with a fresh root only if there is no verified
   release to use.
3. If the root is incomplete or inconsistent, preserve it for investigation and
   use a different root. Never delete an uncertain destination or another run's
   receipt as automatic recovery.

Native publication requires Linux `renameat2(RENAME_NOREPLACE)` or macOS
`renamex_np(RENAME_EXCL)` and supporting filesystem semantics. Unsupported systems
fail closed; there is no check-then-overwrite fallback. Node.js 22's supported
GNU/Linux baseline already includes glibc 2.28 and kernel 4.18, above the API's
introduction floor; actual filesystem support still matters. Native-primitive
tests skip only when that operation is unavailable; validation and fail-closed
unit tests still run. [Node.js platform baseline](https://github.com/nodejs/node/blob/v22.x/BUILDING.md#platform-list),
[Linux rename API](https://man7.org/linux/man-pages/man2/rename.2.html).

Review `edge/wrangler.example.jsonc` for the intended account and release before
using it as a deployment configuration. Assets come from the release's `public/`
directory, not the repository or release root. No provider account/token, private
monitoring endpoint or host inventory is included. See [routing](docs/routing.md).

## Telemetry ownership boundary

Query definitions, PromQL/catalogue validation, the restricted source gateway,
and hourly/daily aggregate collection live in
[1aeo/onionsurge](https://github.com/1aeo/onionsurge):

- [Source gateway](https://github.com/1aeo/onionsurge/blob/main/docs/query-gateway.md)
- [Source installation](https://github.com/1aeo/onionsurge/blob/main/docs/source-install.md)
- [Hourly collector](https://github.com/1aeo/onionsurge/blob/main/docs/collector.md)

This repository retains Layer-3 operations: invoking the pinned `collect.py`
and `build.py --live` scripts using a private mode-`0600` `SOURCE_CONFIG`,
staging static assets, verifying `query_contract_sha256` from
`build-manifest.json` against `data.json` provenance, publishing immutable
releases, scheduling UTC runs, and edge routing. See
[publication](docs/publication.md) for schema-7/multi-period release
verification (up to 512 objects), promotion and recovery.

| Path | Purpose |
| --- | --- |
| `edge/` | Canonical redirects, static binding and public object delivery |
| `scripts/` | Runner orchestration, build staging, publication, cloud deployment and scheduling |
| `tests/` | Offline tests with synthetic data and mocked providers |
| `docs/` | Operations, routing, publication, Cloudflare, local/VPN hosting and migration contracts |

Private discovery receipts, host addresses, SSH fingerprints, raw observations,
Grafana exports and source settings stay outside this repository.

Licensed under [Apache License 2.0](LICENSE).
