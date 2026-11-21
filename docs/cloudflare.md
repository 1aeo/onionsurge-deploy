# Cloudflare rollout

The rollout command uses a reviewed live frontend release and pinned application
and deployment checkouts. It owns only the canonical application namespace and
its redirect alias. It does not collect measurements, create storage buckets or
edit unrelated DNS records.

After the source changes have been reviewed and merged, install the pinned
toolchain in the deployment checkout without provider credentials in the install
environment:

```sh
npm ci
npm run check:worker
npm run test:worker
```

The package and lockfile pin Node **24.21.0** and Wrangler **4.158.0**. There is no
project install hook. The trusted Node dependency's installation supplies the
platform binary, so blindly disabling every dependency install script would leave
that toolchain incomplete. `node_modules/` and Wrangler working files are ignored.
The command runs `node_modules/node/bin/node` directly with the installed Wrangler
entrypoint; it does not use a global Wrangler or an arbitrary shell command.

## Private settings and prepared assets

Use the assignment-only private configuration described in
[operations](operations.md). Set `APPLICATION_DIR`, `APPLICATION_COMMIT`,
`DEPLOYMENT_COMMIT` and `STATE_DIR`, plus:

- `CLOUDFLARE_ACCOUNT_ID`, `CLOUDFLARE_ZONE_ID`, `CLOUDFLARE_API_TOKEN`;
- `WORKER_NAME`, `ROOT_CACHE_GENERATION` (rotate to a fresh value on every Worker deployment);
- optional `CLOUDFLARE_CANONICAL_HOST` (defaults to the non-routable placeholder
  `example.com`) and `CLOUDFLARE_REDIRECT_HOST` (defaults to
  `onionsurge.<CLOUDFLARE_CANONICAL_HOST>`, i.e. `onionsurge.example.com` in the
  placeholder configuration);
- optional `STATE_RETAIN_RUNS` (`2..168`, default `3`), which also bounds completed
  `cloudflare-*` staging directories retained under `STATE_DIR`;
- enabled Spaces settings with a DNS-label bucket, region and nonempty project
  prefix; the public origin is derived rather than accepted as an arbitrary URL;
- optional `R2_ENABLED`, `R2_BUCKET` and `R2_PREFIX` for a global bucket in the
  same account. R2 is disabled by default.

The token needs the account's Worker script permissions and the zone's read,
route and DNS permissions; optional R2 validation also needs bucket read access.
It must also permit the selected Worker's account-scoped production-route
inventory and the account-wide Worker Domains list. Configured-zone visibility
alone is insufficient. Denied or incomplete account reads stop rollout; there is
no fallback to enumerating only zones visible to the token. This read requirement
does not require granting route or DNS writes to unrelated zones.
Use a token scoped to the intended account and zone. Credentials go only to the
API authorization header or the child environment, never to Wrangler arguments,
generated configuration or source. Other ambient credentials and Node injection
options are not inherited. CLI output is a fixed status summary; detailed tool
output stays in a private per-run directory under `STATE_DIR`, with the token
redacted and Wrangler's sanitization enabled.

First build and stage the frontend through the documented application and
[asset-staging interface](../README.md#prepare-public-assets). The release root
must contain `asset-release.json` with `mode: "live"`, the configured application
commit and exactly four public files: `index.html`, `app.js`, `live.js` and
`style.css`. Every byte count and SHA-256 is checked again. A live entrypoint must
not execute the saved `data.js`.

```sh
python3 scripts/cloudflare.py --config /private/config.env \
  --frontend-release /private/frontend-release --dry-run
```

Dry-run checks the local pins/assets and existing Cloudflare state, then runs a
real Wrangler dry-run. It makes no Cloudflare writes. A fresh deployment can be
planned before the first publication receipt exists. Actual rollout requires a
committed `published.json` receipt matching all configured storage destinations
and refuses a pending publication recovery.

```sh
python3 scripts/cloudflare.py --config /private/config.env \
  --frontend-release /private/frontend-release
```

## Ownership and scope

The repository ships with non-routable `example.com` / `onionsurge.example.com`
placeholder defaults in `edge/worker.mjs` and `edge/wrangler.example.jsonc`.
Before running Cloudflare rollout on a live domain, set `CLOUDFLARE_CANONICAL_HOST`
and (optionally) `CLOUDFLARE_REDIRECT_HOST` in your private mode-`0600`
`config.env` instead of modifying tracked repository files — keeping your
deployment checkout cleanly pinned to `origin/main` so `DEPLOYMENT_COMMIT`
verification succeeds without local edits:

- `CLOUDFLARE_CANONICAL_HOST` sets the canonical apex domain (pinned against the
  Cloudflare zone's `name`, failing closed with `zone_account_mismatch` if they
  differ).
- `CLOUDFLARE_REDIRECT_HOST` sets the redirect alias hostname (derived as
  `onionsurge.<CLOUDFLARE_CANONICAL_HOST>` when left blank).
- `scripts/cloudflare.py` injects these values as `CANONICAL_HOST` and
  `REDIRECT_HOST` plain-text bindings and derives the two route patterns
  `<canonical_host>/onionsurge*` and `<redirect_host>/*`. (Update
  `edge/wrangler.example.jsonc` only if adapting the standalone example outside
  this runner.)

The private generated Wrangler configuration binds the exact Worker source and
the release's `public/` directory. `workers_dev` and `preview_urls` are false,
asset routing runs through the Worker first, and only those two derived routes
are configured (for example `example.com/onionsurge*` and
`onionsurge.example.com/*` under placeholder defaults).

The zone must be active, match `CLOUDFLARE_CANONICAL_HOST` (`example.com` in the
placeholder configuration) and be owned by the configured account. The
canonical DNS record must already be proxied; the command never changes it. An
existing Worker must carry the fixed plain-text ownership binding
`ONIONSURGE_OWNER=1aeo/onionsurge-deploy` plus the configured `CANONICAL_HOST`
and `REDIRECT_HOST` plain-text bindings (plus `PUBLIC_DATA_ORIGIN` /
`ROOT_CACHE_GENERATION`), with no unrelated bindings or routes.
An unmarked Worker is not silently adopted. Conflicting, excluded or overlapping
namespace routes stop the command, including broader wildcard routes.

Before uploading code to an existing Worker, the command reads the same
account-scoped production-route endpoint used by the pinned
[Wrangler remote-configuration reader](https://github.com/cloudflare/workers-sdk/blob/wrangler%404.158.0/packages/deploy-helpers/src/deploy/helpers/download-worker-config.ts).
Every attachment must be one of the two derived route patterns above, attached
to the selected Worker in the configured zone. The account and configured-zone
inventories must agree. The account's [Worker Domains inventory](https://developers.cloudflare.com/api/resources/workers/subresources/domains/methods/list/)
must contain no custom-domain attachment for that Worker, and its workers.dev and
preview endpoints must already be disabled. Other Workers' domain attachments are
left alone. No API error, malformed record, mismatched count or partial page is
treated as an empty inventory. These account endpoints return complete lists;
unexpected pagination fails closed rather than silently accepting a first page.

The alias is added only when absent, as a proxied CNAME to the configured
canonical host (`example.com` in placeholder defaults). An existing alias is
reused only when it has exactly that target/type/proxy state. Other records,
targets and non-proxied aliases are refused. No DNS update/delete request is
issued to resolve a conflict.

Run rollout under the approved single-operator maintenance procedure, without
concurrent Cloudflare configuration changes or edits to pinned checkouts/staged
assets. The local publication lock coordinates this command with this host's
publisher; it is not a distributed Cloudflare transaction. The command rechecks
pins, assets and namespace state before deployment. After deployment it checks
the configured bindings, disabled unscoped URLs and a single 100% deployment.
Completed remote changes are retained if a later check fails; inspect the private
run log and retry or recover explicitly rather than deleting shared resources.

For destination changes, follow [publication migration](publication.md): fence
writers, copy and verify complete releases, move roots last, and run the explicit
publisher destination-migration action. The Cloudflare command refuses a changed
destination receipt and requires a different cache generation from the currently
bound one. Generate a fresh value; never recycle a retired generation. Keep old
objects and private settings for reviewed rollback.

`R2_PREFIX` is applied only to the server-side storage key. Public URLs and cache
keys stay unchanged. The Worker retains its legacy empty-prefix behavior when
the binding is absent; generated production R2 configurations require a nonempty
project prefix matching the publisher.

API references: [Worker settings](https://developers.cloudflare.com/api/resources/workers/subresources/scripts/subresources/script_and_version_settings/methods/get/),
[Worker routes](https://developers.cloudflare.com/api/resources/workers/subresources/routes/),
and [DNS record creation](https://developers.cloudflare.com/api/resources/dns/subresources/records/methods/create/).
