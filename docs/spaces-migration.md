# Move the public data prefix to a new Spaces bucket

The browser URLs and release IDs stay unchanged. This procedure copies immutable
data, switches the private storage destination and Worker origin, and retains the
old prefix for rollback. It does not delete a bucket, release or other project's
objects. A bucket's region cannot be changed in place; moving regions requires
a new bucket and object transfer.
[DigitalOcean bucket creation](https://docs.digitalocean.com/products/spaces/how-to/create/).

## Create the destination

The operator creates a **Standard Storage** bucket in the DigitalOcean control
panel. Choose a region appropriate for the publisher and a globally available
name containing no personal name, private host name, account identifier or other
sensitive infrastructure detail. Standard Storage supports the public access
needed by the Worker; Cold Storage is not the target for this active dataset.
Leave the Spaces CDN disabled so mutable roots use the direct origin.
[Storage types and regions](https://docs.digitalocean.com/products/spaces/how-to/create/).

Keep **Restrict File Listing** enabled. Private listing and public object reads
are separate settings: the publisher gives only its verified public objects a
public-read ACL. Never upload private inputs, logs, configuration, SQLite files
or credentials to this prefix.
[Listing permissions](https://docs.digitalocean.com/products/spaces/how-to/set-file-listing-permissions/).

Review the destination's lifecycle settings as the bucket owner. No expiration,
destructive cleanup or cold-storage policy may apply to `onionsurge/` or its
retained releases. A limited key's inability to read lifecycle or policy settings
is not evidence that no rule exists; inspect them through authorized owner
access. Also inventory other jobs or tools with write access to the bucket.
[Lifecycle rules](https://docs.digitalocean.com/products/spaces/how-to/configure-lifecycle-rules/).

Create a **Limited** Spaces access key for this bucket. DigitalOcean's write
permission option is named **Read/Write/Delete**; the OnionSurge publisher uses
read and copy operations and has no delete/sync path. Full account access is not
needed for ordinary publication. The bucket must already exist. Keep both key
values only in the private configuration; never place them in command arguments,
public source, Worker variables or reports.
[Access-key scopes](https://docs.digitalocean.com/products/spaces/how-to/manage-access/).

The ACL-based configuration above is compatible with limited keys. DigitalOcean
does not allow combining bucket policies with limited-access keys; do not add a
bucket-wide public policy to work around an object-read failure.
[Bucket-policy restrictions](https://docs.digitalocean.com/products/spaces/how-to/configure-bucket-policies/).

## Pause and preserve state

1. Keep a mode-0600 private copy of the old `config.env`, the current Worker
   deployment identity/cache generation, `published.json` and relevant receipts.
   Keep the existing `STATE_DIR`; do not reset publication history.
2. Pause future scheduled runs:

   ```sh
   python3 scripts/schedule.py remove --config /path/to/private/config.env
   python3 scripts/schedule.py verify --config /path/to/private/config.env
   ```

   Confirm `schedule: paused` and `run_busy: false`. Removing cron does not stop
   a running process. Join active manual publishers too, and fence any pending
   provider request so it cannot arrive after cutover.
3. Confirm `pending_promotion: false`. If a promotion is uncertain, complete the
   [fenced recovery procedure](publication.md#uncertain-promotion-and-recovery)
   before migrating. Do not remove the pending marker or infer completion from
   a delay or a single GET.
4. Read each current provider root and compare it with the private committed
   receipt. Preserve the exact root bytes and its current/previous descriptors.
   Keep all writers paused through public verification.

## Copy and verify the isolated prefix

Use a reviewed, prefix-scoped copy operation with private child credentials.
The source and destination are both restricted to `onionsurge/`; never run
bucket-root `sync`, delete operations or `--delete-excluded`.

1. Inventory and copy only immutable objects under `onionsurge/releases/`.
   Preserve every retained release required by current, previous and rollback
   receipts; copying the complete retained release namespace is preferable to
   dropping older reproducibility links. Do not copy another application's
   root files or backup prefixes.
2. For each release, verify its manifest's bytes and SHA-256 against its recorded
   descriptor, then fully download every listed object and compare exact length
   and SHA-256. Verify schema, observation endpoint and reference identity as
   well. A transfer summary, size-only comparison or multipart ETag is not the
   required content verification. Conflicting existing immutable keys stop the
   migration instead of being overwritten.
3. Preserve long immutable cache metadata and public-read ACLs for public
   release objects. Confirm anonymous direct-origin reads work for representative
   JSON, reference and CSV objects while bucket listing remains private.
4. Copy the exact frozen `onionsurge/manifest.json` **last**, with `no-store`
   cache metadata. Fully read it back. It must still select the same complete
   current and previous releases on every enabled provider, including R2 when
   configured. Do not publish a newer candidate during this step.

The destination-reconciliation command below performs verification only; it
does not copy objects or rewrite remote roots for these steps.

## Reconcile private settings and switch the Worker

Prepare a new mode-0600 private configuration owned by the publisher. Set the new
`DO_SPACES_BUCKET`, `DO_SPACES_REGION`, key and secret; retain the validated
`DO_SPACES_PREFIX="onionsurge"`. If R2 is enabled, retain or deliberately migrate
its configured mirror too. Keep the same private `STATE_DIR` and exact reviewed
application/deployment commit pins.

The publisher pins storage destination identities in `published.json`. Changing
configuration alone therefore fails closed. After all old/new writers have been
fenced and the copied roots agree, reconcile those private pins explicitly:

```sh
python3 scripts/publish.py --config /path/to/private/new-config.env \
  --migrate-destination --confirm-writers-fenced
```

This checks each new configured provider's root against the existing committed
receipt, fully verifies its current and previous releases, and rechecks all
roots. Only then does it preserve a durable `destination-migration-*.json` record
containing the old/new receipts and update the private destination pins. It
performs **no remote writes or deletes**. Missing objects, differing roots,
pending promotion or failed verification leave the old receipt in place. A
local I/O failure requires inspection of the retained migration record before
retrying; never delete `published.json` to bypass the check.

Choose a fresh, never-used `ROOT_CACHE_GENERATION` in the new private config.
The reviewed Worker CLI derives its direct public origin from the new bucket,
region and prefix. Prepare/check it first, then deploy the reviewed frontend
release:

```sh
python3 scripts/cloudflare.py --config /path/to/private/new-config.env \
  --frontend-release /path/to/reviewed-frontend-release --dry-run
python3 scripts/cloudflare.py --config /path/to/private/new-config.env \
  --frontend-release /path/to/reviewed-frontend-release
```

Complete the new Worker rollout before declaring cutover. Root cache-generation
changes do not repair a stale provider CDN root; use the direct origin and
`no-store` root metadata. Keep the canonical application and alias redirect
behavior unchanged.

## Verify, resume and retain rollback

Through the canonical public site, verify the current root and both named
release manifests, data JSON, frozen reference and every CSV against their
recorded hashes. Check a cold browser load, a refresh and downloads; all must use
one complete selected release. Confirm the Worker reads the intended origin,
the actual observation/reference dates remain accurate, and private bucket
listing is still unavailable.

With writers still paused, re-read every provider root and confirm it is
unchanged. Make the new private configuration the runner's reviewed active
configuration, run one manual hourly cycle, then install and verify its schedule:

```sh
python3 scripts/run_hourly.py --config /path/to/private/new-config.env
python3 scripts/schedule.py install --config /path/to/private/new-config.env
python3 scripts/schedule.py verify --config /path/to/private/new-config.env
```

Verify two scheduled cycles and public observation freshness. Keep the old
prefix, old private configuration and AROI reserved-prefix exclusion in place
through the verified rollback window. After new releases are published, a
rollback must first copy/verify those required releases and reconcile roots and
private destination pins under the same writer fence; simply restoring old
credentials is insufficient. Every Worker rollback/cutover uses another fresh
cache generation.

For a storage rollback, first copy and verify the currently committed release
and its fallback into the old prefix and set its root to the committed receipt's
exact root. Then run `--migrate-destination --confirm-writers-fenced` with the old
destination configuration. If the content root also needs to move to an older
release, restore that explicitly selected root afterward and run
`--reconcile-root RESTORED_ROOT_JSON --confirm-writers-fenced` on the now-matching
destinations. Follow the [schema-compatible rollback procedure](publication.md#rollback-across-schema-versions)
before resuming. Neither reconciliation action writes remote roots or bypasses
the required immutable-object verification.

This guide authorizes no automatic cleanup. Retiring credentials, removing the
old prefix or removing its owner's exclusion is a separate reviewed operation
after rollback/reproducibility requirements are met. Never delete another
project's prefix or the shared source bucket.
