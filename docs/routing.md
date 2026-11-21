# Edge routing

The Worker serves an example application build at `https://example.com/onionsurge/`
by default using placeholder fallback hostnames (`DEFAULT_CANONICAL_HOST` =
`example.com` and `DEFAULT_REDIRECT_HOST` = `onionsurge.example.com` in
`edge/worker.mjs`). When deployed to a live Cloudflare zone, set
`CLOUDFLARE_CANONICAL_HOST` and `CLOUDFLARE_REDIRECT_HOST` in your private
`config.env`; `scripts/cloudflare.py` injects them as plain-text Worker
bindings (`CANONICAL_HOST` and `REDIRECT_HOST`) without editing repository
files (failing closed with HTTP `503` if invalid host bindings are present).
See [cloudflare](cloudflare.md#ownership-and-scope). The standalone
`edge/wrangler.example.jsonc` remains prepared example configuration, not an
installed route.

| Request | Result |
| --- | --- |
| `example.com/onionsurge?x=1` | 308 to `https://example.com/onionsurge/?x=1` |
| `onionsurge.example.com/` | 308 to the canonical root |
| `onionsurge.example.com/a/b?x=1` | 308 to canonical `/onionsurge/a/b?x=1` |
| Already-prefixed alias path | Prefix retained once |
| Canonical `/onionsurge/*` | Public asset or allowlisted object |
| Other apex paths | Original request passed to origin |

Encoded components, query order, duplicate keys and percent escapes are preserved
as delivered by the platform URL parser. Browser fragments never reach the
server; standard parsing can normalize dot segments before the Worker sees them.

The root maps internally to `/index.html` through `ASSETS.fetch`, not a network
fetch back through its own route. Relative `data.js`, `style.css`, `reference/`
and `exports/` links stay under the visible `/onionsurge/` prefix. Unknown assets
return 404. Automatic asset redirects are disabled and unexpected redirects fail
closed rather than sending visitors to the apex.

The route pattern ends with `*` because Cloudflare matches query strings too.
The Worker checks the exact path boundary, including lookalike prefixes. Inspect
existing route precedence before deployment; no apex-wide route is declared.
`run_worker_first=true` applies redirects before static assets.
[Cloudflare routes](https://developers.cloudflare.com/workers/configuration/routing/routes/),
[Worker-first assets](https://developers.cloudflare.com/workers/static-assets/routing/worker-script/).

## Object delivery

Bundled prototype data/reference/export files remain assets. The future object
interface uses `/onionsurge/data/` and a fixed grammar for mutable aliases and
immutable release/history/reference/issue objects. Configuration files, databases,
encoded separators and arbitrary bucket paths are rejected there.

`PUBLIC_DATA_ORIGIN` accepts a public HTTPS DigitalOcean Spaces origin. Optional
`ONIONSURGE_DATA` is the R2 fallback. Provider secrets and private telemetry
settings do not belong in Worker vars or assets.

Each provider has an eight-second deadline, including its body. Data objects are
limited to 8 MiB and buffered before returning success. A stalled or oversized
primary can therefore fall back instead of producing a successful partial reply.
HEAD uses provider metadata. Visitor cookies/Authorization are not forwarded;
storage redirects are not followed and Set-Cookie is not returned.
An oversized R2 GET body is cancelled before rejection; this cleanup remains
inside the provider deadline. HEAD has no body to cancel.

Successful GETs use Cloudflare's edge Cache API. Mutable manifest/data/export
aliases cache for 60 seconds; immutable objects cache for one year. Errors use
no-store. Query strings do not change an object cache key. HEAD can reuse a cached
GET. Partition larger datasets before publication.
Cache lookup has a one-second deadline; a stalled cache falls through to storage.
A cache response arriving after that deadline is discarded and its body cancelled.

Upload and verify immutable objects first, then move the root manifest. Bind all
client data/reference/export requests to that release ID. A fallback manifest may
be older, so display actual observation/reference dates. Mutable aliases alone
are not a multi-file transaction. Store explicit `.gz` artifacts as compressed
bytes without `Content-Encoding`. The Worker rejects encoded Spaces `.gz`
responses before reading their potentially decoded body and tries R2 instead.
Both providers must return the same compressed fixture bytes/checksum; the Worker
serves those bytes as `application/gzip` without a content-encoding transform.
See [publication](publication.md).
