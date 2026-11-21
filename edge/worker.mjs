/** Scoped OnionSurge routing and verified-release object delivery.
 *
 * The asset binding contains ONLY the public build. Private collector settings,
 * keys, database snapshots and operations packets must never enter that build.
 */
export const CANONICAL_HOST = 'example.com';
export const REDIRECT_HOST = 'onionsurge.example.com';
export const PREFIX = '/onionsurge';
const PREFIX_SLASH = PREFIX + '/';
const HOSTNAME_RE = /^(?=.{1,253}$)[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?(?:\.[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?)+$/;
export const MAX_OBJECT_BYTES = 8 * 1024 * 1024;

// Object names, not arbitrary bucket paths. Mutable manifest/data/export aliases
// get short caches. Release/history/reference objects are immutable at publish.
const ID = '[A-Za-z0-9][A-Za-z0-9_.-]{0,95}';
const SLUG = '[a-z0-9][a-z0-9_-]{0,95}';
const HASH = '[a-f0-9]{64}';
const SUB_24H_SUFFIX = '(?:0*[01]d|0+[0-9]*h|(?:[0-9]|1[0-9]|2[0-3])h|[0-9]+[sm])';
const EXPORT_CSV = `exports/(?!.*-${SUB_24H_SUFFIX}\\.csv$)${SLUG}-[a-z0-9]+(?:-[a-z0-9]+){0,7}\\.csv`;
const PUBLIC_OBJECT = new RegExp(`^(?:manifest\\.json|data\\.(?:json|js)|${EXPORT_CSV}|` +
  `releases/${ID}/(?:manifest\\.json|data\\.(?:json|js)|${EXPORT_CSV}|reference/${ID}\\.json|(?:current|coverage)/${SLUG}\\.(?:json|csv)(?:\\.gz)?)|` +
  `history/${ID}/(?:index-${HASH}\\.json|[0-9]{4}-[0-9]{2}/${HASH}\\.json(?:\\.gz)?)|` +
  `(?:reference|issues)/${ID}\\.json)$`);
const mutableObject = key => key === 'manifest.json' || key === 'data.json' || key === 'data.js' || key.startsWith('exports/');
const CACHE_GENERATION_PARAMETER = '__onionsurge_root_generation';
const GENERATION_RE = /^[A-Za-z0-9_-]{1,128}$/;

const SECURITY_HEADERS = [
  ['X-Content-Type-Options', 'nosniff'],
  ['Referrer-Policy', 'no-referrer'],
  ['X-Frame-Options', 'DENY'],
  ['Cross-Origin-Opener-Policy', 'same-origin'],
  ['Cross-Origin-Resource-Policy', 'same-origin'],
  ['Permissions-Policy', 'accelerometer=(), browsing-topics=(), camera=(), geolocation=(), gyroscope=(), magnetometer=(), microphone=(), payment=(), usb=()'],
];
const INDEX_CSP = "default-src 'none'; script-src 'self'; style-src 'self' 'unsafe-inline'; style-src-elem 'self'; style-src-attr 'unsafe-inline'; img-src 'self'; connect-src 'self'; base-uri 'none'; form-action 'none'; frame-ancestors 'none'; object-src 'none'";
const PREVIEW_CSP = "default-src 'none'; script-src 'unsafe-inline'; style-src 'unsafe-inline'; img-src 'self'; connect-src 'none'; base-uri 'none'; form-action 'none'; frame-ancestors 'none'; object-src 'none'";
const SANDBOX_CSP = "default-src 'none'; sandbox; frame-ancestors 'none'";
const VALID_ETAG = /^(?:W\/)?"[\x21\x23-\x5b\x5d-\x7e]{1,256}"$/;
const CONTENT_LENGTH_RE = /^\d{1,16}$/;
const R2_PREFIX_RE = /^[a-z][a-z0-9_-]{0,62}$/;
const DESIGN_PREVIEW_RE = /^\/design-[123]\.html$/;

const defaultInflightGets = new Map();
const flightMeta = new WeakMap();
const aliveFlights = new WeakSet();
const lateWriteEpochs = new Map();
const MAX_LATE_WRITE_EPOCHS = 256;
const validatedSpacesBases = new Map();
const discard = r => r?.body?.cancel().catch(() => {});
const bumpLateWriteEpoch = k => {
  if (lateWriteEpochs.has(k)) {
    const next = lateWriteEpochs.get(k) + 1;
    lateWriteEpochs.delete(k);
    lateWriteEpochs.set(k, next);
    return;
  }
  if (lateWriteEpochs.size >= MAX_LATE_WRITE_EPOCHS) {
    lateWriteEpochs.delete(lateWriteEpochs.keys().next().value);
  }
  lateWriteEpochs.set(k, 1);
};

function applySecurityHeaders(headers, csp) {
  for (const [name, value] of SECURITY_HEADERS) headers.set(name, value);
  headers.set('Content-Security-Policy', csp);
  return headers;
}

function withHeaders(response, csp = null, method = 'GET', cacheControl = null) {
  const headers = new Headers(response.headers);
  if (csp) applySecurityHeaders(headers, csp);
  if (cacheControl) headers.set('Cache-Control', cacheControl);
  let body = response.body;
  if (method === 'HEAD') {
    discard(response);
    body = null;
  }
  return new Response(body, { status: response.status, statusText: response.statusText, headers });
}

function dataCacheKey(url, key, generation) {
  // Safe to mutate url in place: data requests return before url is read again.
  url.search = mutableObject(key) ? '?' + CACHE_GENERATION_PARAMETER + '=' + generation : '';
  return new Request(url.href);
}

function clientDataResponse(response, request, key, alreadySecured = false) {
  const cacheControl = key === 'manifest.json' ? 'no-store' : null;
  if (alreadySecured && (!cacheControl || response.headers.get('Cache-Control') === cacheControl)) return response;
  return withHeaders(response, alreadySecured ? null : SANDBOX_CSP, request.method, cacheControl);
}

async function deadline(task, milliseconds) {
  const controller = new AbortController();
  let timer;
  try {
    return await Promise.race([
      task(controller.signal),
      new Promise((_, reject) => {
        timer = setTimeout(() => { controller.abort(); reject(new Error('Storage timeout')); }, milliseconds);
      }),
    ]);
  } finally {
    clearTimeout(timer);
  }
}

async function boundedBody(body, signal, expectedBytes = null) {
  if (!body) {
    if (expectedBytes !== null && expectedBytes !== 0) throw new Error('Truncated object body');
    return new Uint8Array();
  }
  const reader = body.getReader();
  const chunks = [];
  let bytes = 0;
  let complete = false;
  const abort = () => { reader.cancel().catch(() => {}); };
  signal.addEventListener('abort', abort, { once: true });
  try {
    while (true) {
      if (signal.aborted) throw new Error('Storage timeout');
      const { done, value } = await reader.read();
      if (signal.aborted) throw new Error('Storage timeout');
      if (done) { complete = true; break; }
      if (!value?.byteLength) continue;
      bytes += value.byteLength;
      if (bytes > MAX_OBJECT_BYTES || (expectedBytes !== null && bytes > expectedBytes)) {
        throw new Error('Object exceeds delivery limit');
      }
      chunks.push(value);
    }
    if (expectedBytes !== null && bytes !== expectedBytes) throw new Error('Truncated object body');
    if (chunks.length === 0) return new Uint8Array();
    if (chunks.length === 1) return chunks[0];
    const result = new Uint8Array(bytes);
    let offset = 0;
    for (const chunk of chunks) {
      result.set(chunk, offset);
      offset += chunk.byteLength;
    }
    return result;
  } finally {
    signal.removeEventListener('abort', abort);
    if (!complete) await reader.cancel().catch(() => {});
  }
}

function resolveHosts(env = {}) {
  const rawCanonical = env?.CANONICAL_HOST;
  const rawRedirect = env?.REDIRECT_HOST;
  const canonicalHost = rawCanonical === undefined ? CANONICAL_HOST : rawCanonical;
  if (typeof canonicalHost !== 'string' || !HOSTNAME_RE.test(canonicalHost)) return null;
  const redirectHost = rawRedirect === undefined
    ? (rawCanonical === undefined ? REDIRECT_HOST : 'onionsurge.' + canonicalHost)
    : rawRedirect;
  if (typeof redirectHost !== 'string' || !HOSTNAME_RE.test(redirectHost) || redirectHost === canonicalHost) {
    return null;
  }
  return { canonicalHost, redirectHost };
}

function redirectTarget(url, pathname, canonicalHost = CANONICAL_HOST) {
  const target = new URL(url.href);
  target.protocol = 'https:';
  target.hostname = canonicalHost;
  target.port = '';
  target.username = '';
  target.password = '';
  // Assigning pathname preserves percent escapes. Never decode/re-encode query
  // parameters, concatenate a user-provided host, or resolve a //path as a host.
  target.pathname = pathname;
  return target.href;
}

function routeUrl(url, { canonicalHost = CANONICAL_HOST, redirectHost = REDIRECT_HOST } = {}) {
  const path = url.pathname;
  if (url.hostname === redirectHost) {
    // Alias paths are relative to the application. Already-prefixed links are
    // idempotent, so a copied canonical path never gains a duplicate prefix.
    const canonicalPath = path === PREFIX ? PREFIX_SLASH :
      path.startsWith(PREFIX_SLASH) ? path : PREFIX + path;
    return { kind: 'redirect', location: redirectTarget(url, canonicalPath, canonicalHost) };
  }
  if (url.hostname !== canonicalHost || (path !== PREFIX && !path.startsWith(PREFIX_SLASH))) {
    return { kind: 'origin' };
  }
  if (path === PREFIX || url.protocol !== 'https:') {
    return { kind: 'redirect', location: redirectTarget(url, path === PREFIX ? PREFIX_SLASH : path, canonicalHost) };
  }
  const relative = path.slice(PREFIX_SLASH.length);
  if (relative.startsWith('data/')) {
    const key = relative.slice(5);
    return PUBLIC_OBJECT.test(key) ? { kind: 'data', key } : { kind: 'not-found' };
  }
  // Explicit index mapping plus html_handling=none prevents asset middleware
  // from redirecting /index.html to the apex or repeatedly normalizing the URL.
  return { kind: 'asset', pathname: relative === '' ? '/index.html' : '/' + relative };
}

export function routeRequest(request, env = {}) {
  const hosts = resolveHosts(env);
  if (!hosts) throw new Error('Invalid worker host configuration');
  return routeUrl(new URL(request.url), hosts);
}

const errorHeaders = () => applySecurityHeaders(
  new Headers({ 'Content-Type': 'text/plain; charset=utf-8', 'Cache-Control': 'no-store' }),
  SANDBOX_CSP,
);

function error(status, text, method = 'GET') {
  return new Response(method === 'HEAD' ? null : text, { status, headers: errorHeaders() });
}

function objectHeaders(key, source, etag = null, size = null) {
  const headers = applySecurityHeaders(new Headers({
    'Content-Type': key.endsWith('.gz') ? 'application/gzip' :
      key.endsWith('.js') ? 'text/javascript; charset=utf-8' :
      key.endsWith('.csv') ? 'text/csv; charset=utf-8' : 'application/json; charset=utf-8',
    'Cache-Control': mutableObject(key) ? 'public, max-age=60' : 'public, max-age=31536000, immutable',
    'X-OnionSurge-Storage': source,
  }), SANDBOX_CSP);
  if (Number.isInteger(size) && size >= 0) headers.set('Content-Length', String(size));
  if (typeof etag === 'string' && VALID_ETAG.test(etag)) headers.set('ETag', etag);
  return headers;
}

function spacesObjectUrl(origin, key) {
  if (!origin) return null;
  let base = validatedSpacesBases.get(origin);
  if (base === undefined) {
    const url = new URL(origin);
    const host = url.hostname.toLowerCase();
    if (url.protocol !== 'https:' ||
        host.endsWith('.cdn.digitaloceanspaces.com') ||
        !/^(?:[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?\.)?[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])\.digitaloceanspaces\.com$/.test(host) ||
        url.username || url.password || url.port || url.search || url.hash ||
        !/^(\/[a-z][a-z0-9_-]{0,62})?\/?$/.test(url.pathname)) {
      throw new Error('Invalid public Spaces origin');
    }
    url.pathname = url.pathname.replace(/\/?$/, '/');
    url.search = '';
    url.hash = '';
    base = url.href;
    if (validatedSpacesBases.size >= 64) validatedSpacesBases.clear();
    validatedSpacesBases.set(origin, base);
  }
  return base + key;
}

function r2ObjectKey(prefix, key) {
  if (prefix === undefined || prefix === '') return key;
  if (typeof prefix !== 'string' || !R2_PREFIX_RE.test(prefix)) throw new Error('Invalid R2 project prefix');
  return prefix + '/' + key;
}

async function readRecord(key, source, method, obj, signal, expectedBytes, etag) {
  if (method === 'HEAD') {
    await discard(obj);
    return { status: 200, body: null, headers: objectHeaders(key, source, etag, expectedBytes) };
  }
  const body = await boundedBody(obj.body, signal, expectedBytes);
  return { status: 200, body, headers: objectHeaders(key, source, etag, body.byteLength) };
}

async function fetchStorageRecord(method, env, key, storageFetch, timeoutMs = 8000) {
  let configured = false;
  let failure = false;
  if (env.PUBLIC_DATA_ORIGIN) {
    configured = true;
    try {
      const upstream = await deadline(async signal => {
        const response = await storageFetch(spacesObjectUrl(env.PUBLIC_DATA_ORIGIN, key), {
          method, redirect: 'manual', signal,
          // Never forward browser cookies, Authorization or arbitrary request headers.
          headers: { Accept: 'application/json, text/csv, application/gzip' },
        });
        if (signal.aborted) { await discard(response); throw new Error('Storage timeout'); }
        if (response.status !== 200) { await discard(response); return { status: response.status }; }
        if (key.endsWith('.gz') && response.headers.has('Content-Encoding')) {
          // .gz keys are opaque compressed artifacts; reject decoded encodings before reading.
          await discard(response);
          throw new Error('Compressed artifact must not carry Content-Encoding');
        }
        const rawLength = response.headers.get('Content-Length');
        let expectedBytes = null;
        if (rawLength !== null) {
          const parsed = CONTENT_LENGTH_RE.test(rawLength) ? Number(rawLength) : NaN;
          if (!Number.isSafeInteger(parsed) || parsed < 0 || parsed > MAX_OBJECT_BYTES) {
            await discard(response);
            throw new Error('Object exceeds delivery limit');
          }
          expectedBytes = parsed;
        }
        return readRecord(key, 'spaces', method, response, signal, expectedBytes, response.headers.get('ETag'));
      }, timeoutMs);
      if (upstream.status === 200) return upstream;
      failure = upstream.status !== 404;
    } catch { failure = true; }
  }
  if (env.ONIONSURGE_DATA) {
    configured = true;
    try {
      const storageKey = r2ObjectKey(env.R2_PREFIX, key);
      const result = await deadline(async signal => {
        const object = method === 'HEAD' ? await env.ONIONSURGE_DATA.head(storageKey) : await env.ONIONSURGE_DATA.get(storageKey);
        if (signal.aborted) {
          await discard(object);
          throw new Error('Storage timeout');
        }
        if (!object) return null;
        let expectedBytes = null;
        if (object.size !== undefined) {
          if (!Number.isInteger(object.size) || object.size < 0 || object.size > MAX_OBJECT_BYTES) {
            await discard(object);
            throw new Error('Object exceeds delivery limit');
          }
          expectedBytes = object.size;
        }
        return readRecord(key, 'r2', method, object, signal, expectedBytes, object.httpEtag);
      }, timeoutMs);
      if (result) return result;
    } catch { failure = true; }
  }
  const status = (!configured || failure) ? 503 : 404;
  const body = method === 'HEAD' ? null : (!configured ? 'Public dataset storage is not configured.' :
    failure ? 'Dataset temporarily unavailable.' : 'Dataset not found.');
  return { status, body, headers: errorHeaders() };
}

export async function handleRequest(request, env, ctx, dependencies = {}) {
  // Capture the admitted Worker version's binding before any asynchronous work.
  // Reuse this value even if an older request finishes after a new rollout.
  const rootCacheGeneration = env?.ROOT_CACHE_GENERATION;
  const hosts = resolveHosts(env);
  if (!hosts) return error(503, 'Worker host configuration is invalid.', request.method);
  const url = new URL(request.url);
  const route = routeUrl(url, hosts);
  if (route.kind === 'origin') return (dependencies.originFetch || fetch)(request);
  if (route.kind === 'redirect') return new Response(null, {
    status: 308,
    headers: applySecurityHeaders(new Headers({ Location: route.location, 'Cache-Control': 'public, max-age=86400' }), SANDBOX_CSP),
  });
  if (request.method !== 'GET' && request.method !== 'HEAD') {
    const response = error(405, 'Method not allowed.', request.method);
    response.headers.set('Allow', 'GET, HEAD');
    return response;
  }
  if (route.kind === 'not-found') return error(404, 'Not found.', request.method);
  if (route.kind === 'data') {
    if (mutableObject(route.key) && (typeof rootCacheGeneration !== 'string' || !GENERATION_RE.test(rootCacheGeneration))) {
      return error(503, 'Mutable dataset cache generation is not configured.', request.method);
    }
    const cache = dependencies.edgeCache || (typeof caches !== 'undefined' ? caches.default : null);
    const cacheKey = dataCacheKey(url, route.key, rootCacheGeneration);
    const inflight = dependencies.inflightGets ?? (dependencies.storageFetch || dependencies.edgeCache ? null : defaultInflightGets);
    const flightKey = `${cacheKey.url}\0${env.PUBLIC_DATA_ORIGIN || ''}\0${env.R2_PREFIX || ''}\0${Boolean(env.ONIONSURGE_DATA)}`;
    const matchCache = async () => {
      if (!cache) return null;
      try {
        return await deadline(async signal => {
          const match = await cache.match(cacheKey);
          // Cache.match has no cancellation option; discard late results safely.
          if (signal.aborted) { discard(match); return null; }
          return match;
        }, dependencies.cacheTimeoutMs ?? 1000);
      } catch {
        return null; /* A cache outage must not prevent origin delivery. */
      }
    };
    const load = () => fetchStorageRecord(request.method, env, route.key, dependencies.storageFetch || fetch, dependencies.timeoutMs);
    let record;
    if (request.method === 'GET' && inflight) {
      for (let attempt = 0; attempt < 2; attempt++) {
        let tracked = inflight.get(flightKey);
        const isLeader = !tracked;
        if (!tracked) {
          let putReleaseTimer = null;
          let release;
          const cacheLookupOrLoad = async () => {
            if (cache) {
              const epochBefore = lateWriteEpochs.get(flightKey) ?? 0;
              let cached = await matchCache();
              if (!cached && (lateWriteEpochs.get(flightKey) ?? 0) !== epochBefore) {
                cached = await matchCache();
              }
              if (cached) {
                release();
                return { fromCache: true, response: cached };
              }
            }
            return load();
          };
          release = () => {
            if (putReleaseTimer !== null) {
              clearTimeout(putReleaseTimer);
              putReleaseTimer = null;
            }
            if (inflight.get(flightKey) === promise) inflight.delete(flightKey);
          };
          const promise = cacheLookupOrLoad();
          const meta = {
            writeStarted: false,
            schedulePut(targetCtx) {
              if (!cache || !targetCtx?.waitUntil || this.writeStarted) return false;
              this.writeStarted = true;
              aliveFlights.add(promise);
              const cacheWriteTimeoutMs = dependencies.cacheWriteTimeoutMs ?? 5000;
              const writeTask = promise.then(loaded => {
                if (!loaded.fromCache && loaded.status === 200) {
                  let timedOut = false;
                  const putTimeout = new Promise(resolve => {
                    putReleaseTimer = setTimeout(() => {
                      timedOut = true;
                      release();
                      resolve();
                    }, cacheWriteTimeoutMs);
                    putReleaseTimer.unref?.();
                  });
                  const putPromise = cache.put(cacheKey, new Response(loaded.body, { status: loaded.status, headers: loaded.headers }))
                    .then(() => { if (timedOut) bumpLateWriteEpoch(flightKey); })
                    .catch(() => {});
                  return Promise.race([putPromise, putTimeout]);
                }
              }).catch(() => {}).finally(release);
              targetCtx.waitUntil(writeTask);
              return true;
            },
          };
          promise.then(() => { if (!meta.writeStarted) release(); }, release);
          flightMeta.set(promise, meta);
          inflight.set(flightKey, promise);
          tracked = promise;
        }
        const meta = flightMeta.get(tracked);
        if (!meta?.schedulePut(ctx) && ctx?.waitUntil && !aliveFlights.has(tracked)) {
          aliveFlights.add(tracked);
          ctx.waitUntil(tracked.catch(() => {}));
        }
        record = await tracked;
        if (!record.fromCache) break;
        if (isLeader) return clientDataResponse(record.response, request, route.key);
        const ownCached = cache ? await matchCache() : null;
        if (ownCached) return clientDataResponse(ownCached, request, route.key);
      }
      if (record.fromCache) {
        record = await load();
      }
    } else {
      const cached = cache ? await matchCache() : null;
      if (cached) return clientDataResponse(cached, request, route.key);
      record = await load();
    }
    const response = new Response(record.body, { status: record.status, headers: record.headers });
    if (cache && ctx?.waitUntil && request.method === 'GET' && response.status === 200 && !inflight) {
      ctx.waitUntil(cache.put(cacheKey, response.clone()).catch(() => {}));
    }
    return clientDataResponse(response, request, route.key, true);
  }
  if (!env.ASSETS) return error(503, 'Public assets are not configured.', request.method);
  url.pathname = route.pathname;
  // ASSETS is an internal binding, not a network fetch back through the public
  // route. The visitor URL remains /onionsurge/, so relative assets resolve there.
  const response = await env.ASSETS.fetch(new Request(url.href, request));
  if (response.status >= 300 && response.status < 400 && response.headers.has('Location')) {
    // Configuration drift must not redirect visitors outside the app prefix.
    await discard(response);
    return error(502, 'Unexpected asset redirect; check html_handling configuration.', request.method);
  }
  const csp = route.pathname === '/index.html' ? INDEX_CSP
    : DESIGN_PREVIEW_RE.test(route.pathname) ? PREVIEW_CSP
    : SANDBOX_CSP;
  return withHeaders(response, csp, request.method);
}

export default { fetch: handleRequest };
