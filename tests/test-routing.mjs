import test from 'node:test';
import assert from 'node:assert/strict';
import { AsyncLocalStorage } from 'node:async_hooks';
import { readFile } from 'node:fs/promises';
import { createHash } from 'node:crypto';
import { gzipSync } from 'node:zlib';
import { routeRequest, handleRequest as workerRequest, MAX_OBJECT_BYTES } from '../edge/worker.mjs';

const req = (url, init) => new Request(url, init);
const TEST_GENERATION = 'test-generation-1';
// Existing provider fixtures represent a configured Worker. Passing an explicit
// undefined/invalid binding still exercises the fail-closed boundary below.
const handleRequest = (request, env, ctx, dependencies) => workerRequest(request,
  { ROOT_CACHE_GENERATION: TEST_GENERATION, ...env }, ctx, dependencies);
const stubAssets = () => {
  const calls = [];
  return { calls, fetch: async request => {
    calls.push(request);
    return new Response('public asset', { headers: { 'Content-Type': 'text/plain' } });
  } };
};

const redirects = [
  ['https://example.com/onionsurge', 'https://example.com/onionsurge/'],
  ['https://example.com/onionsurge?view=explorer&all=directory%2Cqueues', 'https://example.com/onionsurge/?view=explorer&all=directory%2Cqueues'],
  ['http://example.com/onionsurge/app.js?x=1', 'https://example.com/onionsurge/app.js?x=1'],
  ['https://onionsurge.example.com/', 'https://example.com/onionsurge/'],
  ['https://onionsurge.example.com/exports/chart.csv?download=1', 'https://example.com/onionsurge/exports/chart.csv?download=1'],
  ['https://onionsurge.example.com/onionsurge?x=1', 'https://example.com/onionsurge/?x=1'],
  ['https://onionsurge.example.com/onionsurge/app.js?x=1', 'https://example.com/onionsurge/app.js?x=1'],
  ['http://onionsurge.example.com:8080/a%2Fb/%E2%98%83?q=a%2Fb+%26&x=%252F',
   'https://example.com/onionsurge/a%2Fb/%E2%98%83?q=a%2Fb+%26&x=%252F'],
  ['https://onionsurge.example.com/a%3Fb%23c?x=%2f&x=%2F', 'https://example.com/onionsurge/a%3Fb%23c?x=%2f&x=%2F'],
  ['https://onionsurge.example.com/?', 'https://example.com/onionsurge/?'],
];
for (const [input, target] of redirects) test(`308: ${input}`, async () => {
  assert.deepEqual(routeRequest(req(input)), { kind: 'redirect', location: target });
  const response = await handleRequest(req(input), {}, {});
  assert.equal(response.status, 308);
  assert.equal(response.headers.get('Location'), target);
  assert.notEqual(routeRequest(req(target)).kind, 'redirect', 'destination must not redirect again');
});

test('unrelated apex paths and lookalike hosts pass unchanged to origin', async () => {
  for (const path of ['/', '/blog/', '/dos', '/dos/chart?x=%2F', '/onionsurge-other', '/onionsurge%2Fapp.js']) {
    const request = req('https://example.com' + path);
    let passed;
    const response = await handleRequest(request, {}, {}, { originFetch: async r => {
      passed = r; return new Response('original origin', { status: 202 });
    } });
    assert.equal(passed, request);
    assert.equal(response.status, 202);
  }
  assert.equal(routeRequest(req('https://onionsurge.example.com.evil.test/')).kind, 'origin');
});

test('canonical root uses internal index binding; relative asset stays under public prefix', async () => {
  const ASSETS = stubAssets();
  const browserUrl = 'https://example.com/onionsurge/?view=overview';
  const response = await handleRequest(req(browserUrl), { ASSETS }, {});
  assert.equal(response.status, 200);
  assert.equal(new URL(ASSETS.calls[0].url).pathname, '/index.html');
  assert.equal(new URL('app.js', browserUrl).href, 'https://example.com/onionsurge/app.js');
  await handleRequest(req(new URL('app.js', browserUrl)), { ASSETS }, {});
  assert.equal(new URL(ASSETS.calls[1].url).pathname, '/app.js');
});

test('asset path encoding and query survive prefix stripping', async () => {
  const ASSETS = stubAssets();
  await handleRequest(req('https://example.com/onionsurge/a%2Fb/%E2%98%83.js?q=%26+%2F'), { ASSETS }, {});
  assert.equal(ASSETS.calls[0].url, 'https://example.com/a%2Fb/%E2%98%83.js?q=%26+%2F');
});

test('approved frontend data and export filenames resolve through the public asset binding', async () => {
  const ASSETS = stubAssets();
  for (const path of ['data.json', 'data.js', 'reference/reference-2026-11-18-a1b2c3d4e5f6.json',
    'exports/circuit_rejections-7d.csv', 'exports/circuit_rejections-1mo.csv',
    'exports/circuit_rejections-3mo.csv', 'exports/circuit_rejections-6mo.csv',
    'exports/circuit_rejections-12mo.csv', 'exports/circuit_rejections-all-time.csv']) {
    await handleRequest(req('https://example.com/onionsurge/' + path), { ASSETS }, {});
    assert.equal(new URL(ASSETS.calls.at(-1).url).pathname, '/' + path);
  }
});

test('unexpected static asset redirects fail closed rather than escaping to apex', async () => {
  const ASSETS = { fetch: async () => new Response(null, { status: 307, headers: { Location: '/' } }) };
  const response = await handleRequest(req('https://example.com/onionsurge/index.html'), { ASSETS }, {});
  assert.equal(response.status, 502);
  assert.equal(response.headers.get('Location'), null);
});

test('non-GET app requests rejected, origin methods unchanged, redirect preserves method semantics', async () => {
  assert.equal((await handleRequest(req('https://example.com/onionsurge/', { method: 'POST' }), {}, {})).status, 405);
  assert.equal((await handleRequest(req('https://onionsurge.example.com/', { method: 'POST' }), {}, {})).status, 308);
  let method;
  await handleRequest(req('https://example.com/contact', { method: 'POST' }), {}, {}, {
    originFetch: async r => { method = r.method; return new Response('origin'); },
  });
  assert.equal(method, 'POST');
});

test('data route only accepts fixed public object grammar', () => {
  const hash = 'a'.repeat(64);
  const validSuffixes = ['24h', '7d', '1mo', '3mo', '6mo', '12mo', 'all-time', '14d', '24mo'];
  const validExports = validSuffixes.flatMap(suffix => [
    `exports/circuit_rejections-${suffix}.csv`,
    `exports/connection-rate-${suffix}.csv`,
    `releases/20261118T040000Z/exports/circuit_rejections-${suffix}.csv`,
    `releases/20261118T040000Z/exports/connection-rate-${suffix}.csv`,
  ]);
  for (const key of ['manifest.json', 'data.json', 'data.js', ...validExports,
    'releases/20261118T040000Z/manifest.json',
    'releases/20261118T040000Z/data.json', 'releases/20261118T040000Z/data.js',
    'releases/20261118T040000Z/reference/2026-11-18-v1.json',
    'releases/20261118T040000Z/current/circuit_rejections.json',
    `history/query-v1/2026-11/${hash}.json.gz`, `history/query-v1/index-${hash}.json`,
    'reference/2026-11-18-v1.json', 'issues/20261118T040000Z.json']) {
    assert.deepEqual(routeRequest(req('https://example.com/onionsurge/data/' + key)), { kind: 'data', key });
  }
  const rejectedExports = ['1d', '0d', '0h', '1h', '23h', '024h', '30m', '1m', '60s', 'foo-1h'].flatMap(suffix => [
    `exports/circuit_rejections-${suffix}.csv`,
    `exports/connection-rate-${suffix}.csv`,
    `releases/20261118T040000Z/exports/circuit_rejections-${suffix}.csv`,
    `releases/20261118T040000Z/exports/connection-rate-${suffix}.csv`,
  ]);
  for (const key of ['config.env', 'observations.db', 'private/secret.json', ...rejectedExports,
    'releases%2Fabc/current/a.json', 'releases/abc/current/a%00.json', 'manifest.json/extra']) {
    assert.equal(routeRequest(req('https://example.com/onionsurge/data/' + key)).kind, 'not-found', key);
  }
});

test('Spaces fetch drops visitor credentials/query and returns only safe headers', async () => {
  let captured;
  const response = await handleRequest(req('https://example.com/onionsurge/data/manifest.json?token=not-forwarded', {
    headers: { Authorization: 'Bearer visitor', Cookie: 'session=visitor' },
  }), { PUBLIC_DATA_ORIGIN: 'https://onionsurge-data.nyc3.digitaloceanspaces.com/public' }, {}, {
    storageFetch: async (url, init) => {
      captured = { url, init };
      return new Response('{}', { headers: { ETag: '"abc"', 'Set-Cookie': 'bad=1' } });
    },
  });
  assert.equal(captured.url, 'https://onionsurge-data.nyc3.digitaloceanspaces.com/public/manifest.json');
  assert.equal(captured.init.redirect, 'manual');
  assert.deepEqual(Object.keys(captured.init.headers), ['Accept']);
  assert.equal(response.headers.get('Set-Cookie'), null);
  assert.equal(response.headers.get('Cache-Control'), 'no-store');
  assert.equal(response.headers.get('ETag'), '"abc"');
});

test('immutable data caches long and R2 fallback does not redirect to storage', async () => {
  const key = 'reference/2026-11-18-v1.json';
  const ONIONSURGE_DATA = { get: async k => {
    assert.equal(k, key); return { body: new Response('{}').body, httpEtag: '"r2"' };
  } };
  const response = await handleRequest(req('https://example.com/onionsurge/data/' + key), {
    PUBLIC_DATA_ORIGIN: 'https://onionsurge-data.nyc3.digitaloceanspaces.com', ONIONSURGE_DATA,
  }, {}, { storageFetch: async () => new Response(null, { status: 503 }) });
  assert.equal(response.status, 200);
  assert.equal(response.headers.get('X-OnionSurge-Storage'), 'r2');
  assert.match(response.headers.get('Cache-Control'), /immutable/);
  assert.equal(response.headers.get('Location'), null);
});

test('mutable object aliases use short caching and data.js has JavaScript MIME type', async () => {
  for (const key of ['data.js', 'data.json', 'exports/circuit_rejections-1mo.csv', 'exports/circuit_rejections-7d.csv']) {
    const response = await handleRequest(req('https://example.com/onionsurge/data/' + key), {
      ONIONSURGE_DATA: { get: async () => ({ body: new Response(key === 'data.js' ? 'window.ONIONSURGE_DATA={};' : '{}').body }) },
    }, {});
    assert.equal(response.headers.get('Cache-Control'), 'public, max-age=60');
    if (key === 'data.js') assert.match(response.headers.get('Content-Type'), /^text\/javascript/);
  }
});

test('HEAD uses R2 metadata without reading object body', async () => {
  let heads = 0;
  const response = await handleRequest(req('https://example.com/onionsurge/data/manifest.json', { method: 'HEAD' }), {
    ONIONSURGE_DATA: { head: async () => { heads++; return { httpEtag: '"r2"' }; } },
  }, {});
  assert.equal(heads, 1);
  assert.equal(await response.text(), '');
});

test('R2 project prefixes apply to GET and HEAD without changing public object names', async () => {
  const calls = [];
  const env = { R2_PREFIX: 'public-metrics', ONIONSURGE_DATA: {
    get: async key => { calls.push(['get', key]); return { body: new Response('{}').body, size: 2 }; },
    head: async key => { calls.push(['head', key]); return { size: 2 }; },
  } };
  for (const method of ['GET', 'HEAD']) {
    const response = await handleRequest(req('https://example.com/onionsurge/data/manifest.json', { method }), env, {});
    assert.equal(response.status, 200);
  }
  assert.deepEqual(calls, [['get', 'public-metrics/manifest.json'], ['head', 'public-metrics/manifest.json']]);
});

test('invalid R2 prefixes fail closed before accessing another key namespace', async () => {
  for (const prefix of ['../private', 'prefix/', '/prefix', 4, null]) {
    let called = false;
    const response = await handleRequest(req('https://example.com/onionsurge/data/manifest.json'), {
      R2_PREFIX: prefix, ONIONSURGE_DATA: { get: async () => { called = true; } },
    }, {});
    assert.equal(response.status, 503);
    assert.equal(called, false);
  }
});

test('missing storage and unsafe configured origins fail without a network request', async () => {
  const request = req('https://example.com/onionsurge/data/manifest.json');
  assert.equal((await handleRequest(request, {}, {})).status, 503);
  let calls = 0;
  const response = await handleRequest(request, { PUBLIC_DATA_ORIGIN: 'http://127.0.0.1:9090' }, {}, {
    storageFetch: async () => { calls++; throw new Error('must not execute'); },
  });
  assert.equal(calls, 0);
  assert.equal(response.status, 503);
});

test('example config runs Worker first and disables automatic asset redirects', async () => {
  const text = await readFile(new URL('../edge/wrangler.example.jsonc', import.meta.url), 'utf8');
  assert.match(text, /"run_worker_first": true/);
  assert.match(text, /"html_handling": "none"/);
  assert.match(text, /"directory": "\.\.\/build\/public"/);
  assert.match(text, /"ROOT_CACHE_GENERATION": ""/);
  assert.doesNotMatch(text, /"pattern": "example\.com\/\*"/);
});

test('primary header timeout reaches R2 fallback within the request budget', async () => {
  let aborted = false;
  const response = await handleRequest(req('https://example.com/onionsurge/data/manifest.json'), {
    PUBLIC_DATA_ORIGIN: 'https://public-data.nyc3.digitaloceanspaces.com',
    ONIONSURGE_DATA: { get: async () => ({ body: new Response('{}').body }) },
  }, {}, { timeoutMs: 5, storageFetch: (_url, options) => {
    options.signal.addEventListener('abort', () => { aborted = true; });
    return new Promise(() => {});
  } });
  assert.equal(aborted, true);
  assert.equal(response.headers.get('X-OnionSurge-Storage'), 'r2');
});

test('stalled primary body is cancelled before fallback', async () => {
  let cancelled = false;
  const response = await handleRequest(req('https://example.com/onionsurge/data/manifest.json'), {
    PUBLIC_DATA_ORIGIN: 'https://public-data.nyc3.digitaloceanspaces.com',
    ONIONSURGE_DATA: { get: async () => ({ body: new Response('{}').body }) },
  }, {}, { timeoutMs: 5, storageFetch: async () => new Response(new ReadableStream({ cancel() { cancelled = true; } })) });
  assert.equal(cancelled, true);
  assert.equal(response.headers.get('X-OnionSurge-Storage'), 'r2');
});

test('oversized and unexpected successful HTTP statuses are not published as data', async () => {
  for (const upstream of [new Response(null, { status: 204 }),
    new Response('{}', { headers: { 'Content-Length': String(MAX_OBJECT_BYTES + 1) } }),
    new Response(new Uint8Array(MAX_OBJECT_BYTES + 1))]) {
    const response = await handleRequest(req('https://example.com/onionsurge/data/manifest.json'), {
      PUBLIC_DATA_ORIGIN: 'https://public-data.nyc3.digitaloceanspaces.com',
    }, {}, { storageFetch: async () => upstream });
    assert.equal(response.status, 503);
    assert.equal(response.headers.get('Cache-Control'), 'no-store');
  }
});

test('edge cache serves GET and HEAD without repeating provider fetches', async () => {
  const entries = new Map(), pending = [];
  const edgeCache = { match: async key => entries.get(key.url)?.clone(),
    put: async (key, response) => { entries.set(key.url, response); } };
  let calls = 0;
  const env = { PUBLIC_DATA_ORIGIN: 'https://public-data.nyc3.digitaloceanspaces.com' };
  const dependencies = { edgeCache, storageFetch: async () => { calls++; return new Response('{}'); } };
  const ctx = { waitUntil: value => pending.push(value) };
  const url = 'https://example.com/onionsurge/data/manifest.json';
  assert.equal(await (await handleRequest(req(url + '?a=1'), env, ctx, dependencies)).text(), '{}');
  await Promise.all(pending);
  assert.equal(await (await handleRequest(req(url + '?a=2'), env, ctx, dependencies)).text(), '{}');
  assert.equal(await (await handleRequest(req(url, { method: 'HEAD' }), env, ctx, dependencies)).text(), '');
  assert.equal(calls, 1);
});

test('a stalled cache lookup cannot prevent healthy storage delivery', async () => {
  let providerReads = 0;
  const result = handleRequest(req('https://example.com/onionsurge/data/manifest.json'), {
    ONIONSURGE_DATA: { get: async () => {
      providerReads++;
      return { body: new Response('{"release_id":"healthy"}').body };
    } },
  }, {}, { edgeCache: { match: () => new Promise(() => {}) }, cacheTimeoutMs: 5 });
  let watchdog;
  try {
    const response = await Promise.race([result, new Promise((_, reject) => {
      watchdog = setTimeout(() => reject(new Error('cache lookup blocked storage delivery')), 250);
    })]);
    assert.equal(response.status, 200);
    assert.equal((await response.json()).release_id, 'healthy');
    assert.equal(providerReads, 1);
  } finally {
    clearTimeout(watchdog);
  }
});

test('a cache response arriving after its deadline is discarded and cancelled', { timeout: 1000 }, async () => {
  let resolveCache, cancelled = false;
  const delayedCache = new Promise(resolve => { resolveCache = resolve; });
  const response = await handleRequest(req('https://example.com/onionsurge/data/manifest.json'), {
    ONIONSURGE_DATA: { get: async () => ({ body: new Response('fresh').body }) },
  }, {}, { edgeCache: { match: () => delayedCache }, cacheTimeoutMs: 5 });
  assert.equal(await response.text(), 'fresh');
  resolveCache(new Response(new ReadableStream({ cancel() { cancelled = true; } })));
  await new Promise(resolve => setTimeout(resolve, 0));
  assert.equal(cancelled, true);
});

test('opaque gzip fixture has identical downloaded checksum from Spaces and R2', async () => {
  const fixture = gzipSync(Buffer.from('{"fixture":"compressed-public-data"}'));
  const hash = createHash('sha256').update(fixture).digest('hex');
  const url = 'https://example.com/onionsurge/data/history/v1/2026-11/' + hash + '.json.gz';
  const providers = [
    [{ PUBLIC_DATA_ORIGIN: 'https://public-data.nyc3.digitaloceanspaces.com' },
      { storageFetch: async () => new Response(fixture) }],
    [{ ONIONSURGE_DATA: { get: async () => ({ body: new Response(fixture).body, size: fixture.length }) } }, {}],
  ];
  for (const [env, dependencies] of providers) {
    const response = await handleRequest(req(url), env, {}, dependencies);
    assert.equal(response.status, 200);
    assert.equal(response.headers.get('Content-Type'), 'application/gzip');
    assert.equal(response.headers.get('Content-Encoding'), null);
    assert.equal(createHash('sha256').update(Buffer.from(await response.arrayBuffer())).digest('hex'), hash);
  }
});

test('encoded Spaces gzip response is cancelled and never cached as decoded gzip bytes', async () => {
  let cancelled = false;
  const fixture = gzipSync(Buffer.from('{"fixture":"compressed-public-data"}'));
  const hash = createHash('sha256').update(fixture).digest('hex');
  const encoded = new Response(new ReadableStream({ cancel() { cancelled = true; } }),
    { headers: { 'Content-Encoding': 'gzip' } });
  const getReader = encoded.body.getReader.bind(encoded.body);
  let readerCalls = 0;
  encoded.body.getReader = (...args) => { readerCalls++; return getReader(...args); };
  const response = await handleRequest(req('https://example.com/onionsurge/data/history/v1/2026-11/' + hash + '.json.gz'), {
    PUBLIC_DATA_ORIGIN: 'https://public-data.nyc3.digitaloceanspaces.com',
    ONIONSURGE_DATA: { get: async () => ({ body: new Response(fixture).body, size: fixture.length }) },
  }, {}, { timeoutMs: 5, storageFetch: async () => encoded });
  assert.equal(readerCalls, 0, 'reject Content-Encoding before acquiring a body reader');
  assert.equal(cancelled, true);
  assert.equal(response.headers.get('X-OnionSurge-Storage'), 'r2');
  assert.equal(createHash('sha256').update(Buffer.from(await response.arrayBuffer())).digest('hex'), hash);
});

test('oversized R2 GET body is cancelled; HEAD does not require a body', async () => {
  let cancelled = false;
  const env = { ONIONSURGE_DATA: {
    get: async () => ({ size: MAX_OBJECT_BYTES + 1,
      body: new ReadableStream({ cancel() { cancelled = true; } }) }),
    head: async () => ({ size: MAX_OBJECT_BYTES + 1 }),
  } };
  const url = 'https://example.com/onionsurge/data/manifest.json';
  assert.equal((await handleRequest(req(url), env, {})).status, 503);
  assert.equal(cancelled, true);
  assert.equal((await handleRequest(req(url, { method: 'HEAD' }), env, {})).status, 503);
});

const generationValues = request => [...new URL(request.url).searchParams.values()];
const deferred = () => { let resolve; const promise = new Promise(done => { resolve = done; }); return { promise, resolve }; };
const withTimeout = (promise, label = 'operation', ms = 5000) => {
  let timer;
  return Promise.race([
    promise,
    new Promise((_, reject) => {
      timer = setTimeout(() => reject(new Error(`Timed out waiting for ${label}`)), ms);
    }),
  ]).finally(() => clearTimeout(timer));
};

test('mutable cache keys require a generation and ignore visitor namespace selectors', async () => {
  const aliases = ['manifest.json', 'data.json', 'data.js', 'exports/circuit_rejections-1mo.csv', 'exports/circuit_rejections-7d.csv'];
  for (const alias of aliases) {
    const keys = [];
    const edgeCache = { match: async key => { keys.push(key); return new Response('{}'); } };
    for (const generation of ['generation-A', 'generation-B']) {
      await workerRequest(req('https://example.com/onionsurge/data/' + alias + '?__onionsurge_root_generation=visitor&x=1'),
        { ROOT_CACHE_GENERATION: generation }, {}, { edgeCache });
    }
    assert.deepEqual(generationValues(keys[0]), ['generation-A']);
    assert.deepEqual(generationValues(keys[1]), ['generation-B']);
    assert.notEqual(keys[0].url, keys[1].url);
  }
  for (const generation of [undefined, '', null, 1, 'invalid value', 'x'.repeat(129)]) {
    let touched = false;
    const response = await workerRequest(req('https://example.com/onionsurge/data/manifest.json', { method: 'HEAD' }),
      { ROOT_CACHE_GENERATION: generation }, {}, {
        edgeCache: { match: async () => { touched = true; } },
        storageFetch: async () => { touched = true; throw new Error('must not fetch'); },
      });
    assert.equal(response.status, 503);
    assert.equal(response.headers.get('Cache-Control'), 'no-store');
    assert.equal(await response.text(), '');
    assert.equal(touched, false);
  }
});

test('immutable object keys and cache policy do not change with root generations', async () => {
  for (const key of ['reference/reference-2026-11-18-v1.json',
    'releases/release-A/manifest.json', 'releases/release-A/data.json']) {
    const keys = [], entries = new Map(), writes = [];
    let providerCalls = 0, cachePuts = 0;
    const edgeCache = {
      match: async request => {
        keys.push(request.url);
        return entries.get(request.url)?.clone();
      },
      put: async (request, response) => {
        cachePuts++;
        entries.set(request.url, response.clone());
      },
    };
    const storageFetch = async () => { providerCalls++; return new Response('immutable'); };
    for (const generation of ['old-generation', 'new-generation', undefined]) {
      const response = await workerRequest(req('https://example.com/onionsurge/data/' + key + '?x=1'),
        { ROOT_CACHE_GENERATION: generation, PUBLIC_DATA_ORIGIN: 'https://public-data.nyc3.digitaloceanspaces.com' },
        { waitUntil: write => writes.push(write) }, { edgeCache, storageFetch });
      await Promise.all(writes);
      assert.equal(await response.text(), 'immutable');
      assert.equal(response.headers.get('Cache-Control'), 'public, max-age=31536000, immutable');
      assert.equal(entries.get(keys[0]).headers.get('Cache-Control'), 'public, max-age=31536000, immutable');
      assert.equal(providerCalls, 1, 'later generations reuse the entry populated by the provider miss');
      assert.equal(cachePuts, 1);
    }
    assert.equal(keys[0], keys[1]);
    assert.equal(keys[1], keys[2]);
    assert.equal(new URL(keys[0]).search, '');
  }
});

test('late old-generation cache writes cannot poison the restored generation', async () => {
  const oldMatchStarted = deferred(), releaseOldMatch = deferred(), releaseOldPut = deferred();
  const entries = new Map(), matches = [], puts = [], oldWrites = [], newWrites = [];
  let generation = 'old-generation', bindingReads = 0, providerCalls = 0;
  const env = {
    get ROOT_CACHE_GENERATION() { bindingReads++; return generation; },
    PUBLIC_DATA_ORIGIN: 'https://public-data.nyc3.digitaloceanspaces.com',
  };
  const edgeCache = {
    match: async key => {
      matches.push(key.url);
      if (generationValues(key)[0] === 'old-generation') {
        oldMatchStarted.resolve();
        await releaseOldMatch.promise;
      }
      return entries.get(key.url)?.clone();
    },
    put: async (key, response) => {
      puts.push(key.url);
      if (generationValues(key)[0] === 'old-generation') await releaseOldPut.promise;
      entries.set(key.url, response);
    },
  };
  const dependencies = { edgeCache, storageFetch: async () => {
    providerCalls++;
    return new Response(JSON.stringify({ release_id: providerCalls === 1 ? 'superseded' : 'restored' }),
      { headers: { ETag: '"root"' } });
  } };
  const url = 'https://example.com/onionsurge/data/manifest.json';
  const oldRequest = workerRequest(req(url), env, { waitUntil: task => oldWrites.push(task) }, dependencies);
  await oldMatchStarted.promise;
  generation = 'restored-generation';
  releaseOldMatch.resolve();
  const oldResponse = await oldRequest;
  assert.equal(oldResponse.headers.get('Cache-Control'), 'no-store');
  assert.equal((await oldResponse.json()).release_id, 'superseded');

  const restored = await workerRequest(req(url), env, { waitUntil: task => newWrites.push(task) }, dependencies);
  assert.equal((await restored.json()).release_id, 'restored');
  assert.equal(restored.headers.get('Cache-Control'), 'no-store');
  await Promise.all(newWrites);
  releaseOldPut.resolve();
  await Promise.all(oldWrites);

  const current = await workerRequest(req(url + '?cachebuster=ignored'), env, {}, dependencies);
  assert.equal((await current.json()).release_id, 'restored');
  assert.equal(providerCalls, 2, 'new requests use the restored cache entry');
  assert.equal(bindingReads, 3, 'one admission-time binding read per request');
  assert.equal(puts[0], matches[0], 'old request writes the exact key it read before suspension');
  assert.notEqual(puts[0], puts[1]);
  assert.deepEqual([...new URL(puts[0]).searchParams.values()], ['old-generation']);
  assert.deepEqual([...new URL(puts[1]).searchParams.values()], ['restored-generation']);
  for (const response of entries.values()) assert.equal(response.headers.get('Cache-Control'), 'public, max-age=60');
});

test('root GET and HEAD expose no-store while the internal GET remains cacheable', async () => {
  const entries = new Map(), pending = [];
  const edgeCache = {
    match: async key => entries.get(key.url)?.clone(),
    put: async (key, response) => { entries.set(key.url, response); },
  };
  const env = { ROOT_CACHE_GENERATION: 'headers-generation', PUBLIC_DATA_ORIGIN: 'https://public-data.nyc3.digitaloceanspaces.com' };
  let calls = 0;
  const dependencies = { edgeCache, storageFetch: async () => {
    calls++; return new Response('{"release_id":"A"}', { headers: { ETag: '"release-A"' } });
  } };
  const url = 'https://example.com/onionsurge/data/manifest.json';
  const first = await workerRequest(req(url), env, { waitUntil: task => pending.push(task) }, dependencies);
  assert.equal(await first.text(), '{"release_id":"A"}');
  assert.equal(first.headers.get('Cache-Control'), 'no-store');
  await Promise.all(pending);
  assert.equal([...entries.values()][0].headers.get('Cache-Control'), 'public, max-age=60');
  for (const method of ['GET', 'HEAD']) {
    const response = await workerRequest(req(url, { method }), env, {}, dependencies);
    assert.equal(response.status, 200);
    assert.equal(response.headers.get('Cache-Control'), 'no-store');
    assert.equal(response.headers.get('ETag'), '"release-A"');
    assert.match(response.headers.get('Content-Type'), /^application\/json/);
    assert.equal(await response.text(), method === 'HEAD' ? '' : '{"release_id":"A"}');
  }
  assert.equal(calls, 1);
  const head = await workerRequest(req(url, { method: 'HEAD' }), {
    ROOT_CACHE_GENERATION: 'fresh-head', ONIONSURGE_DATA: { head: async () => ({ httpEtag: '"head"' }) },
  }, {});
  assert.equal(head.headers.get('Cache-Control'), 'no-store');
  assert.equal(await head.text(), '');
});

test('security and privacy headers protect app, preview, data, redirect and error responses', async () => {
  const immutableHeaders = body => {
    const response = new Response(body, { headers: { 'Content-Type': 'text/html; charset=utf-8' } });
    Object.defineProperty(response.headers, 'set', { value() { throw new TypeError("Can't modify immutable headers"); } });
    return response;
  };
  const ASSETS = {
    fetch: async request => immutableHeaders(new URL(request.url).pathname === '/index.html' ? '<html></html>' : 'asset'),
  };
  for (const [target, expectedCsp] of [
    ['https://example.com/onionsurge/', /script-src 'self'.*style-src-attr 'unsafe-inline'.*frame-ancestors 'none'/],
    ['https://example.com/onionsurge/design-1.html', /script-src 'unsafe-inline'.*style-src 'unsafe-inline'.*frame-ancestors 'none'/],
    ['https://example.com/onionsurge/app.js', /^default-src 'none'; sandbox; frame-ancestors 'none'$/],
  ]) {
    const res = await handleRequest(req(target), { ASSETS }, {});
    assert.equal(res.status, 200);
    assert.match(res.headers.get('Content-Security-Policy'), expectedCsp);
    assert.equal(res.headers.get('X-Content-Type-Options'), 'nosniff');
    assert.equal(res.headers.get('Referrer-Policy'), 'no-referrer');
    assert.equal(res.headers.get('X-Frame-Options'), 'DENY');
    assert.equal(res.headers.get('Cross-Origin-Opener-Policy'), 'same-origin');
    assert.equal(res.headers.get('Cross-Origin-Resource-Policy'), 'same-origin');
    assert.ok(res.headers.get('Permissions-Policy').includes('geolocation=()'));
  }
  const head404 = await handleRequest(req('https://example.com/onionsurge/data/private.json', { method: 'HEAD' }), {}, {});
  assert.equal(head404.status, 404);
  assert.equal(await head404.text(), '');
  assert.equal(head404.headers.get('X-Frame-Options'), 'DENY');
  const originRes = await handleRequest(req('https://example.com/blog/'), {}, {}, {
    originFetch: async () => new Response('blog'),
  });
  assert.equal(originRes.headers.get('Content-Security-Policy'), null);
});

test('Spaces CDN endpoints, bad ETag/Content-Length and truncated streams are rejected safely', async () => {
  let fetches = 0;
  const cdn = await handleRequest(req('https://example.com/onionsurge/data/manifest.json'), {
    ROOT_CACHE_GENERATION: 'cdn-check',
    PUBLIC_DATA_ORIGIN: 'https://onionsurge-data.nyc3.cdn.digitaloceanspaces.com',
  }, {}, { storageFetch: async () => { fetches++; return new Response('{}'); } });
  assert.equal(cdn.status, 503);
  assert.equal(fetches, 0);

  for (const badLength of ['abc', '-1', '10, 10']) {
    const res = await handleRequest(req('https://example.com/onionsurge/data/manifest.json', { method: 'HEAD' }), {
      ROOT_CACHE_GENERATION: 'len-check',
      PUBLIC_DATA_ORIGIN: 'https://onionsurge-data.nyc3.digitaloceanspaces.com',
    }, {}, { storageFetch: async () => new Response(null, { headers: { 'Content-Length': badLength } }) });
    assert.equal(res.status, 503);
  }

  const puts = [];
  const truncated = await handleRequest(req('https://example.com/onionsurge/data/reference/2026-11-18-v1.json'), {
    PUBLIC_DATA_ORIGIN: 'https://onionsurge-data.nyc3.digitaloceanspaces.com',
    ONIONSURGE_DATA: { get: async () => ({ body: new Response('{"ok":true}').body, size: 11, httpEtag: '"r2"' }) },
  }, { waitUntil: p => puts.push(p) }, {
    edgeCache: { match: async () => null, put: async () => { throw new Error('truncated must not cache spaces'); } },
    storageFetch: async () => new Response('short', { headers: { 'Content-Length': '100', ETag: 'invalid-etag' } }),
  });
  assert.equal(truncated.status, 200);
  assert.equal(truncated.headers.get('X-OnionSurge-Storage'), 'r2');
  assert.equal(truncated.headers.get('Content-Length'), '11');

  const truncatedOnly = await handleRequest(req('https://example.com/onionsurge/data/reference/2026-11-18-v1.json'), {
    PUBLIC_DATA_ORIGIN: 'https://onionsurge-data.nyc3.digitaloceanspaces.com',
  }, {}, { storageFetch: async () => new Response('short', { headers: { 'Content-Length': '100' } }) });
  assert.equal(truncatedOnly.status, 503);

  const etagFiltered = await handleRequest(req('https://example.com/onionsurge/data/manifest.json'), {
    ROOT_CACHE_GENERATION: 'etag-check',
    PUBLIC_DATA_ORIGIN: 'https://onionsurge-data.nyc3.digitaloceanspaces.com',
  }, {}, { storageFetch: async () => new Response('{}', { headers: { ETag: 'unquoted-etag' } }) });
  assert.equal(etagFiltered.headers.get('ETag'), null);
  assert.equal(etagFiltered.headers.get('Content-Length'), '2');
});

test('HEAD cache hit cancels unused stream, preserves Content-Length, and cold GETs coalesce', async () => {
  let cancelled = false;
  const cachedStream = new ReadableStream({ cancel() { cancelled = true; } });
  const hit = await handleRequest(req('https://example.com/onionsurge/data/data.json', { method: 'HEAD' }), {
    ROOT_CACHE_GENERATION: 'head-hit',
  }, {}, {
    edgeCache: {
      match: async () => new Response(cachedStream, { headers: { 'Content-Type': 'application/json', 'Content-Length': '42' } }),
    },
  });
  assert.equal(hit.status, 200);
  assert.equal(hit.headers.get('Content-Length'), '42');
  assert.equal(await hit.text(), '');
  assert.equal(cancelled, true);

  let upstreamCalls = 0;
  const inflightGets = new Map();
  const dep = {
    inflightGets,
    storageFetch: async () => {
      upstreamCalls++;
      await new Promise(resolve => setTimeout(resolve, 10));
      return new Response('{"shared":true}', { headers: { ETag: 'W/"coalesced"' } });
    },
  };
  const env = { ROOT_CACHE_GENERATION: 'coalesce-gen', PUBLIC_DATA_ORIGIN: 'https://onionsurge-data.nyc3.digitaloceanspaces.com' };
  const [a, b] = await Promise.all([
    handleRequest(req('https://example.com/onionsurge/data/data.json'), env, {}, dep),
    handleRequest(req('https://example.com/onionsurge/data/data.json'), env, {}, dep),
  ]);
  assert.equal(upstreamCalls, 1);
  assert.equal(await a.text(), '{"shared":true}');
  assert.equal(await b.text(), '{"shared":true}');
  assert.equal(a.headers.get('ETag'), 'W/"coalesced"');
  assert.equal(inflightGets.size, 0);
});

test('coalesced waitUntil tracks leader disconnect, late R2 objects discard on abort, and invalid Spaces prefixes reject', async () => {
  const inflightGets = new Map();
  const waitUntilPromises = [];
  const ctx = { waitUntil: p => waitUntilPromises.push(p) };
  let fetchCalls = 0;
  const finishFetch = deferred();
  const env = { ROOT_CACHE_GENERATION: 'disconnect-gen', PUBLIC_DATA_ORIGIN: 'https://public-data.nyc3.digitaloceanspaces.com' };
  const dep = {
    inflightGets,
    storageFetch: async () => {
      fetchCalls++;
      await finishFetch.promise;
      return new Response('{"alive":true}');
    },
  };
  const leaderPromise = handleRequest(req('https://example.com/onionsurge/data/data.json'), env, ctx, dep);
  const followerPromise = handleRequest(req('https://example.com/onionsurge/data/data.json'), env, {}, dep);
  assert.equal(waitUntilPromises.length, 1);
  assert.equal(inflightGets.size, 1);
  finishFetch.resolve();
  await Promise.all(waitUntilPromises);
  const followerRes = await followerPromise;
  const leaderRes = await leaderPromise;
  assert.equal(fetchCalls, 1);
  assert.equal(await followerRes.text(), '{"alive":true}');
  assert.equal(await leaderRes.text(), '{"alive":true}');
  assert.equal(inflightGets.size, 0);

  // Rejecting storage promise still deletes its entry from inflightGets via .finally().
  const rejectDep = { inflightGets, storageFetch: async () => { throw new Error('boom'); } };
  const badRes = await handleRequest(req('https://example.com/onionsurge/data/data.json'), env, ctx, rejectDep);
  assert.equal(badRes.status, 503);
  assert.equal(inflightGets.size, 0);

  // Late R2 get() resolving after deadline aborts discards the unread body stream and returns 503.
  let cancelledLateR2 = false;
  let resolveLateR2;
  const lateR2 = await handleRequest(req('https://example.com/onionsurge/data/manifest.json'), {
    ONIONSURGE_DATA: { get: () => new Promise(resolve => { resolveLateR2 = resolve; }) },
  }, {}, { timeoutMs: 5 });
  assert.equal(lateR2.status, 503);
  resolveLateR2({ body: new ReadableStream({ cancel() { cancelledLateR2 = true; } }), size: 2 });
  await new Promise(resolve => setTimeout(resolve, 0));
  assert.equal(cancelledLateR2, true);

  // spacesObjectUrl validates single-segment lowercase pathname prefixes matching R2_PREFIX_RE.
  for (const badOrigin of [
    'https://public-data.nyc3.digitaloceanspaces.com/a/b',
    'https://public-data.nyc3.digitaloceanspaces.com//public',
    'https://public-data.nyc3.digitaloceanspaces.com/UPPER',
    'https://public-data.nyc3.digitaloceanspaces.com/..%2Fprivate',
    'https://public-data.nyc3.digitaloceanspaces.com/' + 'a'.repeat(64),
  ]) {
    let calls = 0;
    const res = await handleRequest(req('https://example.com/onionsurge/data/manifest.json'), {
      PUBLIC_DATA_ORIGIN: badOrigin,
    }, {}, { storageFetch: async () => { calls++; return new Response('{}'); } });
    assert.equal(res.status, 503, badOrigin);
    assert.equal(calls, 0, badOrigin);
  }
  const seenUrls = [];
  for (const okOrigin of [
    'https://public-data.nyc3.digitaloceanspaces.com/public',
    'https://public-data.nyc3.digitaloceanspaces.com/public/',
    'https://public-data.nyc3.digitaloceanspaces.com/public?',
    'https://public-data.nyc3.digitaloceanspaces.com/public#',
    'https://public-data.nyc3.digitaloceanspaces.com/public/?#',
  ]) {
    const res = await handleRequest(req('https://example.com/onionsurge/data/manifest.json'), {
      PUBLIC_DATA_ORIGIN: okOrigin,
    }, {}, { storageFetch: async url => { seenUrls.push(url); return new Response('{}'); } });
    assert.equal(res.status, 200);
  }
  assert.deepEqual(seenUrls, Array(5).fill('https://public-data.nyc3.digitaloceanspaces.com/public/manifest.json'));
});

test('coalesced cold GET populates edgeCache via synchronous waitUntil even on leader disconnect or follower takeover', async () => {
  const entries = new Map(), waitUntilPromises = [];
  let putCalls = 0, originCalls = 0;
  const edgeCache = {
    match: async key => entries.get(key.url)?.clone(),
    put: async (key, response) => { putCalls++; entries.set(key.url, response); },
  };
  const inflightGets = new Map();
  const finish = deferred(), storageEntered = deferred();
  const dep = {
    edgeCache,
    inflightGets,
    storageFetch: async () => {
      originCalls++;
      storageEntered.resolve();
      await finish.promise;
      return new Response('{"cached_after_disconnect":true}', { headers: { ETag: '"v1"' } });
    },
  };
  const env = { ROOT_CACHE_GENERATION: 'cache-put-gen', PUBLIC_DATA_ORIGIN: 'https://public-data.nyc3.digitaloceanspaces.com' };
  const targetUrl = 'https://example.com/onionsurge/data/data.json';

  // Case 1: leader passes ctx.waitUntil; even before leader await settles, background cache.put runs via waitUntil.
  const leaderCtx = { waitUntil: p => waitUntilPromises.push(p) };
  const abandonedLeader = handleRequest(req(targetUrl), env, leaderCtx, dep);
  try {
    await withTimeout(storageEntered.promise, 'storageEntered');
    assert.equal(waitUntilPromises.length, 1);
  } finally {
    finish.resolve();
  }
  await Promise.all(waitUntilPromises);
  assert.equal(putCalls, 1);
  assert.equal(inflightGets.size, 0);
  // Cached response serves subsequent requests cleanly without repeating upstream fetch (even verifying abandonedLeader still reads cleanly).
  const cachedRes = await handleRequest(req(targetUrl), env, {}, dep);
  assert.equal(originCalls, 1);
  assert.equal(await cachedRes.text(), '{"cached_after_disconnect":true}');
  assert.equal(await (await abandonedLeader).text(), '{"cached_after_disconnect":true}');

  // Case 2: leader lacks ctx.waitUntil, cacheable follower takes over synchronously and populates edgeCache once.
  entries.clear();
  waitUntilPromises.length = 0;
  putCalls = 0;
  originCalls = 0;
  const finish2 = deferred(), storageEntered2 = deferred(), cachePutJoined = deferred();
  const dep2 = {
    edgeCache,
    inflightGets,
    storageFetch: async () => {
      originCalls++;
      storageEntered2.resolve();
      await finish2.promise;
      return new Response('{"follower_cached":true}');
    },
  };
  const leaderNoCtx = handleRequest(req(targetUrl), env, {}, dep2);
  let followerWithCtx, secondFollowerWithCtx;
  try {
    await withTimeout(storageEntered2.promise, 'storageEntered2');
    followerWithCtx = handleRequest(req(targetUrl), env, {
      waitUntil: p => { waitUntilPromises.push(p); cachePutJoined.resolve(); },
    }, dep2);
    await withTimeout(cachePutJoined.promise, 'cachePutJoined');
    secondFollowerWithCtx = handleRequest(req(targetUrl), env, { waitUntil: p => waitUntilPromises.push(p) }, dep2);
    await new Promise(resolve => setTimeout(resolve, 0));
    assert.equal(waitUntilPromises.length, 1);
  } finally {
    finish2.resolve();
  }
  await Promise.all(waitUntilPromises);
  assert.equal(putCalls, 1);
  assert.equal(originCalls, 1);
  assert.equal(await (await leaderNoCtx).text(), '{"follower_cached":true}');
  assert.equal(await (await followerWithCtx).text(), '{"follower_cached":true}');
  assert.equal(await (await secondFollowerWithCtx).text(), '{"follower_cached":true}');
  assert.equal(inflightGets.size, 0);
});

test('singleflight before matchCache deduplicates concurrent cache lookups, secures cache hits, and skips duplicate puts', async () => {
  const entries = new Map(), waitUntilPromises = [];
  let originCalls = 0, matchCalls = 0, putCalls = 0;
  let matchEntered = deferred(), matchGate = deferred(), holdMatch = true;
  const edgeCache = {
    match: async key => {
      matchCalls++;
      if (holdMatch) {
        matchEntered.resolve();
        await matchGate.promise;
      }
      return entries.get(key.url)?.clone();
    },
    put: async (key, response) => {
      putCalls++;
      entries.set(key.url, response);
    },
  };
  const inflightGets = new Map();
  const env = { ROOT_CACHE_GENERATION: 'singleflight-cache-gen', PUBLIC_DATA_ORIGIN: 'https://public-data.nyc3.digitaloceanspaces.com' };
  const dep = {
    edgeCache,
    inflightGets,
    cacheTimeoutMs: 5000,
    storageFetch: async () => {
      originCalls++;
      return new Response('{"coalesced_lookup":true}', { headers: { ETag: '"m1"' } });
    },
  };
  const manifestUrl = 'https://example.com/onionsurge/data/manifest.json';
  const ctx = { waitUntil: p => waitUntilPromises.push(p) };

  // Cold lookup: requests B and C join request A synchronously while A is awaiting cache.match().
  const reqA = handleRequest(req(manifestUrl), env, ctx, dep);
  let reqB, reqC;
  try {
    await withTimeout(matchEntered.promise, 'coldMatchEntered');
    assert.equal(inflightGets.size, 1);
    reqB = handleRequest(req(manifestUrl), env, ctx, dep);
    reqC = handleRequest(req(manifestUrl), env, {}, dep);
    assert.equal(matchCalls, 1);
    assert.equal(originCalls, 0);
  } finally {
    matchGate.resolve();
  }
  const [resA, resB, resC] = await withTimeout(Promise.all([reqA, reqB, reqC]), 'coldCoalesced');
  await withTimeout(Promise.all(waitUntilPromises.splice(0)), 'coldPut');
  assert.equal(matchCalls, 1);
  assert.equal(originCalls, 1);
  assert.equal(putCalls, 1);
  assert.equal(inflightGets.size, 0);
  for (const res of [resA, resB, resC]) {
    assert.equal(await res.text(), '{"coalesced_lookup":true}');
    assert.equal(res.headers.get('Cache-Control'), 'no-store');
  }

  // Warm cache hit through inflight: followers join while leader's matchCache is pending,
  // then read their own request-local cache entry without touching leader's stream or origin.
  matchEntered = deferred();
  matchGate = deferred();
  const hitA = handleRequest(req(manifestUrl), env, ctx, dep);
  let hitB;
  try {
    await withTimeout(matchEntered.promise, 'warmMatchEntered');
    hitB = handleRequest(req(manifestUrl), env, ctx, dep);
    assert.equal(matchCalls, 2);
  } finally {
    holdMatch = false;
    matchGate.resolve();
  }
  const [warmA, warmB] = await withTimeout(Promise.all([hitA, hitB]), 'warmCoalesced');
  await withTimeout(Promise.all(waitUntilPromises.splice(0)), 'warmWaitUntil');
  assert.equal(matchCalls, 3);
  assert.equal(originCalls, 1);
  assert.equal(putCalls, 1);
  assert.equal(inflightGets.size, 0);
  assert.equal(await warmA.text(), '{"coalesced_lookup":true}');
  assert.equal(await warmB.text(), '{"coalesced_lookup":true}');
  for (const warm of [warmA, warmB]) {
    assert.equal(warm.headers.get('Cache-Control'), 'no-store');
    assert.match(warm.headers.get('Content-Security-Policy'), /sandbox/);
  }
});

test('coalesced warm hits and cold storage hits isolate I/O streams strictly to each request context', async () => {
  const als = new AsyncLocalStorage();
  const inflightGets = new Map(), waitUntilPromises = [];
  let putCalls = 0, storageCalls = 0, warmMode = false;
  let warmMatchCalls = 0;
  const matchEntered = deferred(), matchGate = deferred();
  const encoder = new TextEncoder();

  const makeScopedResponse = (owner, payload, headers = {}) => {
    let sent = false;
    const stream = new ReadableStream({
      pull(controller) {
        if (als.getStore() !== owner) {
          throw new Error('Cannot perform I/O on behalf of a different request');
        }
        if (sent) {
          controller.close();
          return;
        }
        sent = true;
        controller.enqueue(encoder.encode(payload));
      },
      cancel() {
        if (als.getStore() !== owner) {
          throw new Error('Cannot perform I/O on behalf of a different request');
        }
      },
    });
    const res = new Response(stream, { status: 200, headers });
    res.clone = () => {
      if (als.getStore() !== owner) {
        throw new Error('Cannot perform I/O on behalf of a different request');
      }
      throw new Error('Cross-request response cloning should never occur');
    };
    return res;
  };

  const edgeCache = {
    match: async () => {
      const owner = als.getStore();
      if (!warmMode) return undefined;
      warmMatchCalls++;
      if (warmMatchCalls === 1) {
        matchEntered.resolve();
        await matchGate.promise;
      }
      return makeScopedResponse(owner, '{"warm_isolated":true}', { ETag: '"iso-warm"' });
    },
    put: async () => { putCalls++; },
  };
  const env = { ROOT_CACHE_GENERATION: 'als-isolation-gen', PUBLIC_DATA_ORIGIN: 'https://public-data.nyc3.digitaloceanspaces.com' };
  const dep = {
    edgeCache,
    inflightGets,
    cacheTimeoutMs: 5000,
    storageFetch: async () => {
      storageCalls++;
      return makeScopedResponse(als.getStore(), '{"cold_shared_bytes":true}', { ETag: '"iso-cold"' });
    },
  };
  const url = 'https://example.com/onionsurge/data/exports/als-iso-7d.csv';

  // 1) Cold storage coalescing: heap Uint8Array bytes are shared cleanly across request contexts.
  const [cold1, cold2] = await withTimeout(Promise.all([
    als.run('cold-req-1', async () => await (await handleRequest(req(url), env, { waitUntil: p => waitUntilPromises.push(p) }, dep)).text()),
    als.run('cold-req-2', async () => await (await handleRequest(req(url), env, {}, dep)).text()),
  ]), 'coldAls');
  await withTimeout(Promise.all(waitUntilPromises.splice(0)), 'coldPutWaitUntil');
  assert.equal(cold1, '{"cold_shared_bytes":true}');
  assert.equal(cold2, '{"cold_shared_bytes":true}');
  assert.equal(storageCalls, 1);
  assert.equal(putCalls, 1);

  // 2) Concurrent warm cache hits: follower opens its own request-local matchCache stream under ALS.
  warmMode = true;
  const warmReq1 = als.run('req-1', async () => {
    const res = await handleRequest(req(url), env, { waitUntil: p => waitUntilPromises.push(p) }, dep);
    return res.text();
  });
  let warmReq2;
  try {
    await withTimeout(matchEntered.promise, 'alsMatchEntered');
    warmReq2 = als.run('req-2', async () => {
      const res = await handleRequest(req(url), env, { waitUntil: p => waitUntilPromises.push(p) }, dep);
      return res.text();
    });
  } finally {
    matchGate.resolve();
  }
  const [text1, text2] = await withTimeout(Promise.all([warmReq1, warmReq2]), 'warmAls');
  await withTimeout(Promise.all(waitUntilPromises.splice(0)), 'warmAlsWaitUntil');
  assert.equal(text1, '{"warm_isolated":true}');
  assert.equal(text2, '{"warm_isolated":true}');
  assert.equal(storageCalls, 1);
  assert.equal(putCalls, 1);
  assert.equal(warmMatchCalls, 2);
  assert.equal(inflightGets.size, 0);
});

test('late cache.put finishing after cacheWriteTimeoutMs only retries the matching cache key', async () => {
  const entries = new Map(), waitUntil1 = [], waitUntil2 = [];
  const latePutGate = deferred();
  const req2MatchEntered = deferred(), req2MatchRelease = deferred();
  let putCalls = 0, matchCalls = 0, storageFails = false;
  const edgeCache = {
    match: async key => {
      matchCalls++;
      if (matchCalls === 2) {
        req2MatchEntered.resolve();
        await req2MatchRelease.promise;
        return undefined; // stale miss captured before late cache.put landed
      }
      return entries.get(key.url)?.clone();
    },
    put: async (key, response) => {
      putCalls++;
      if (putCalls === 1) await latePutGate.promise;
      entries.set(key.url, response);
    },
  };
  const inflightGets = new Map();
  const env = { ROOT_CACHE_GENERATION: 'late-put-retry-gen', PUBLIC_DATA_ORIGIN: 'https://public-data.nyc3.digitaloceanspaces.com' };
  const targetUrl = 'https://example.com/onionsurge/data/exports/late-write-retry-7d.csv';

  // Request 1 succeeds from storage, starts slow cache.put, and times out releasing inflight.
  const res1 = await withTimeout(handleRequest(req(targetUrl), env, { waitUntil: p => waitUntil1.push(p) }, {
    edgeCache,
    inflightGets,
    cacheWriteTimeoutMs: 15,
    storageFetch: async () => new Response('{"late_cached":true}'),
  }), 'lateReq1');
  assert.equal(res1.status, 200);
  await withTimeout(Promise.all(waitUntil1), 'lateWaitUntil1');
  assert.equal(inflightGets.size, 0);

  // Request 2 begins cache.match(); while suspended, Request 1's late cache.put lands for the same key.
  storageFails = true;
  let req2;
  try {
    req2 = handleRequest(req(targetUrl), env, { waitUntil: p => waitUntil2.push(p) }, {
      edgeCache,
      inflightGets,
      cacheTimeoutMs: 5000,
      storageFetch: async () => {
        if (storageFails) return new Response('unavailable', { status: 503 });
        return new Response('unexpected-origin');
      },
    });
    await withTimeout(req2MatchEntered.promise, 'req2MatchEntered');
    latePutGate.resolve();
    await new Promise(resolve => setTimeout(resolve, 0));
  } finally {
    latePutGate.resolve();
    req2MatchRelease.resolve();
  }
  const res2 = await withTimeout(req2, 'lateReq2');
  await withTimeout(Promise.all(waitUntil2), 'lateWaitUntil2');
  assert.equal(res2.status, 200);
  assert.equal(await res2.text(), '{"late_cached":true}');
  assert.equal(matchCalls, 3);
  assert.equal(inflightGets.size, 0);

  // Unrelated key: timed-out manifest.json late write must NOT trigger a second cache.match() on data.json.
  const otherManifestPutGate = deferred(), dataMatchEntered = deferred(), dataMatchRelease = deferred();
  const unrelatedWaitUntil = [];
  let manifestPutCalls = 0, dataMatchCalls = 0, dataStorageCalls = 0;
  const manifestUrl = 'https://example.com/onionsurge/data/manifest.json';
  const dataUrl = 'https://example.com/onionsurge/data/data.json';
  const unrelatedCache = {
    match: async key => {
      if (key.url.includes('/data.json')) {
        dataMatchCalls++;
        dataMatchEntered.resolve();
        await dataMatchRelease.promise;
      }
      return undefined;
    },
    put: async key => {
      if (key.url.includes('/manifest.json')) {
        manifestPutCalls++;
        await otherManifestPutGate.promise;
      }
    },
  };
  const unrelatedDep = {
    edgeCache: unrelatedCache,
    inflightGets,
    cacheTimeoutMs: 5000,
    cacheWriteTimeoutMs: 15,
    storageFetch: async url => {
      if (url.endsWith('/data.json')) dataStorageCalls++;
      return new Response('{"from_storage":true}');
    },
  };
  const manifestRes = await withTimeout(
    handleRequest(req(manifestUrl), env, { waitUntil: p => unrelatedWaitUntil.push(p) }, unrelatedDep),
    'manifestRes',
  );
  assert.equal(manifestRes.status, 200);
  await withTimeout(Promise.all(unrelatedWaitUntil.splice(0)), 'manifestTimeoutWaitUntil');

  let dataReq;
  try {
    dataReq = handleRequest(req(dataUrl), env, { waitUntil: p => unrelatedWaitUntil.push(p) }, unrelatedDep);
    await withTimeout(dataMatchEntered.promise, 'dataMatchEntered');
    otherManifestPutGate.resolve();
    await new Promise(resolve => setTimeout(resolve, 0));
  } finally {
    otherManifestPutGate.resolve();
    dataMatchRelease.resolve();
  }
  const dataRes = await withTimeout(dataReq, 'dataRes');
  await withTimeout(Promise.all(unrelatedWaitUntil), 'dataWaitUntil');
  assert.equal(manifestPutCalls, 1);
  assert.equal(dataMatchCalls, 1);
  assert.equal(dataStorageCalls, 1);
  assert.equal(await dataRes.text(), '{"from_storage":true}');
  assert.equal(inflightGets.size, 0);
});

test('warm cache hits preserve zero-copy streaming and null-body responses without buffering', async () => {
  const inflightGets = new Map(), waitUntil = [];
  let putCalls = 0, storageCalls = 0, controller;
  const encoder = new TextEncoder(), decoder = new TextDecoder();
  const stream = new ReadableStream({ start(c) { controller = c; } });
  controller.enqueue(encoder.encode('chunk-1-'));
  let nextMatch = new Response(stream, {
    status: 200,
    headers: { 'Content-Type': 'text/csv; charset=utf-8', ETag: '"stream1"' },
  });
  const edgeCache = {
    match: async () => nextMatch,
    put: async () => { putCalls++; },
  };
  const env = { ROOT_CACHE_GENERATION: 'stream-warm-gen', PUBLIC_DATA_ORIGIN: 'https://public-data.nyc3.digitaloceanspaces.com' };
  const dep = {
    edgeCache,
    inflightGets,
    cacheTimeoutMs: 5000,
    storageFetch: async () => { storageCalls++; return new Response('unexpected'); },
  };
  const csvUrl = 'https://example.com/onionsurge/data/exports/streaming-warm-7d.csv';

  let closed = false;
  try {
    const streamRes = await withTimeout(handleRequest(req(csvUrl), env, { waitUntil: p => waitUntil.push(p) }, dep), 'warmStreamRes');
    assert.equal(streamRes.status, 200);
    assert.equal(streamRes.headers.get('ETag'), '"stream1"');
    assert.match(streamRes.headers.get('Content-Security-Policy'), /sandbox/);
    const reader = streamRes.body.getReader();
    const first = await withTimeout(reader.read(), 'chunk1');
    assert.equal(first.done, false);
    assert.equal(decoder.decode(first.value), 'chunk-1-');

    controller.enqueue(encoder.encode('chunk-2'));
    controller.close();
    closed = true;
    const second = await withTimeout(reader.read(), 'chunk2');
    assert.equal(second.done, false);
    assert.equal(decoder.decode(second.value), 'chunk-2');
    const done = await withTimeout(reader.read(), 'streamEof');
    assert.equal(done.done, true);
  } finally {
    if (!closed) controller.close();
  }

  // Null-body warm cache entry passes through untouched.
  nextMatch = new Response(null, { status: 200, headers: { ETag: '"null-warm"' } });
  const nullRes = await withTimeout(handleRequest(req(csvUrl), env, { waitUntil: p => waitUntil.push(p) }, dep), 'warmNullRes');
  assert.equal(nullRes.status, 200);
  assert.equal(nullRes.body, null);
  assert.equal(nullRes.headers.get('ETag'), '"null-warm"');
  assert.equal(await nullRes.text(), '');

  await withTimeout(Promise.all(waitUntil), 'waitUntil');
  assert.equal(putCalls, 0);
  assert.equal(storageCalls, 0);
  assert.equal(inflightGets.size, 0);
});

test('inflight stays occupied while async cache.put finishes without blocking GET and cleans up on rejection', async () => {
  const entries = new Map(), waitUntilPromises = [];
  const putEntered = deferred(), putGate = deferred();
  let originCalls = 0;
  const edgeCache = {
    match: async key => entries.get(key.url)?.clone(),
    put: async (key, response) => {
      putEntered.resolve();
      await putGate.promise;
      entries.set(key.url, response);
    },
  };
  const inflightGets = new Map();
  const env = { ROOT_CACHE_GENERATION: 'pending-put-gen', PUBLIC_DATA_ORIGIN: 'https://public-data.nyc3.digitaloceanspaces.com' };
  const dep = {
    edgeCache,
    inflightGets,
    storageFetch: async () => {
      originCalls++;
      return new Response('{"async_put":true}');
    },
  };
  const targetUrl = 'https://example.com/onionsurge/data/data.json';
  try {
    const leaderRes = await withTimeout(
      handleRequest(req(targetUrl), env, { waitUntil: p => waitUntilPromises.push(p) }, dep),
      'leaderRes',
    );
    assert.equal(leaderRes.status, 200);
    assert.equal(await leaderRes.text(), '{"async_put":true}');
    await withTimeout(putEntered.promise, 'putEntered');
    assert.equal(inflightGets.size, 1);
    const followerRes = await withTimeout(
      handleRequest(req(targetUrl), env, { waitUntil: p => waitUntilPromises.push(p) }, dep),
      'followerRes',
    );
    assert.equal(await followerRes.text(), '{"async_put":true}');
    assert.equal(originCalls, 1);
  } finally {
    putGate.resolve();
  }
  await Promise.all(waitUntilPromises);
  assert.equal(inflightGets.size, 0);

  // Rejected cache.put still releases inflightGets via finally().
  const failWaitUntil = [];
  const failRes = await handleRequest(req(targetUrl), env, { waitUntil: p => failWaitUntil.push(p) }, {
    edgeCache: {
      match: async () => undefined,
      put: async () => { throw new Error('cache write failed'); },
    },
    inflightGets,
    storageFetch: async () => new Response('{"put_failed":true}'),
  });
  assert.equal(failRes.status, 200);
  await Promise.all(failWaitUntil);
  assert.equal(inflightGets.size, 0);
});

test('repeated concurrent fills across 6 cycles avoid recheck limits and duplicate storage fetches', async () => {
  const entries = new Map(), waitUntilPromises = [];
  let originCalls = 0, matchCalls = 0, putCalls = 0;
  let putEntered = null, putGate = null;
  const edgeCache = {
    match: async key => { matchCalls++; return entries.get(key.url)?.clone(); },
    put: async (key, response) => {
      putCalls++;
      putEntered.resolve();
      await putGate.promise;
      entries.set(key.url, response);
    },
  };
  const inflightGets = new Map();
  const env = { ROOT_CACHE_GENERATION: 'multi-round-singleflight', PUBLIC_DATA_ORIGIN: 'https://public-data.nyc3.digitaloceanspaces.com' };
  const dep = {
    edgeCache,
    inflightGets,
    cacheTimeoutMs: 5000,
    cacheWriteTimeoutMs: 5000,
    storageFetch: async url => {
      originCalls++;
      return new Response(`round-body-${originCalls}:${url}`);
    },
  };
  const targetUrl = 'https://example.com/onionsurge/data/exports/six-rounds-7d.csv';
  const expectedUpstream = 'https://public-data.nyc3.digitaloceanspaces.com/exports/six-rounds-7d.csv';
  const ctx = { waitUntil: p => waitUntilPromises.push(p) };

  for (let round = 1; round <= 6; round++) {
    entries.clear();
    putEntered = deferred();
    putGate = deferred();
    let followerDuringPut, thirdDuringPut;
    try {
      const leaderRes = await withTimeout(handleRequest(req(targetUrl), env, ctx, dep), `leader-${round}`);
      assert.equal(await leaderRes.text(), `round-body-${round}:${expectedUpstream}`);
      await withTimeout(putEntered.promise, `putEntered-${round}`);
      assert.equal(inflightGets.size, 1);
      followerDuringPut = handleRequest(req(targetUrl), env, ctx, dep);
      thirdDuringPut = handleRequest(req(targetUrl), env, {}, dep);
    } finally {
      putGate.resolve();
    }
    const [res2, res3] = await withTimeout(Promise.all([followerDuringPut, thirdDuringPut]), `followers-${round}`);
    await withTimeout(Promise.all(waitUntilPromises.splice(0)), `waitUntil-${round}`);
    assert.equal(await res2.text(), `round-body-${round}:${expectedUpstream}`);
    assert.equal(await res3.text(), `round-body-${round}:${expectedUpstream}`);
    assert.equal(originCalls, round);
    assert.equal(matchCalls, round);
    assert.equal(putCalls, round);
    assert.equal(inflightGets.size, 0);
  }
});

test('bounded cache.put timeout releases inflight cleanly without deleting a replacement flight', async () => {
  const entries = new Map(), waitUntil1 = [], waitUntil2 = [];
  let putCalls = 0, originCalls = 0, rejectPut1;
  const fetch2Entered = deferred(), fetch2Gate = deferred();
  const put1Promise = new Promise((_, reject) => { rejectPut1 = reject; });
  const edgeCache = {
    match: async key => entries.get(key.url)?.clone(),
    put: async (key, response) => {
      putCalls++;
      if (putCalls === 1) return put1Promise;
      entries.set(key.url, response);
    },
  };
  const inflightGets = new Map();
  const env = { ROOT_CACHE_GENERATION: 'bounded-put-gen', PUBLIC_DATA_ORIGIN: 'https://public-data.nyc3.digitaloceanspaces.com' };
  const targetUrl = 'https://example.com/onionsurge/data/exports/bounded-put-7d.csv';

  const res1 = await withTimeout(handleRequest(req(targetUrl), env, { waitUntil: p => waitUntil1.push(p) }, {
    edgeCache,
    inflightGets,
    cacheWriteTimeoutMs: 20,
    storageFetch: async () => { originCalls++; return new Response('first-put-hangs'); },
  }), 'res1');
  assert.equal(res1.status, 200);
  await withTimeout(Promise.all(waitUntil1), 'waitUntil1');
  assert.equal(inflightGets.size, 0);

  let req2;
  try {
    req2 = handleRequest(req(targetUrl), env, { waitUntil: p => waitUntil2.push(p) }, {
      edgeCache,
      inflightGets,
      cacheWriteTimeoutMs: 500,
      storageFetch: async () => {
        originCalls++;
        fetch2Entered.resolve();
        await fetch2Gate.promise;
        return new Response('replacement-flight');
      },
    });
    await withTimeout(fetch2Entered.promise, 'fetch2Entered');
    assert.equal(inflightGets.size, 1);

    // Late rejection of flight 1's stalled cache.put must not cause an unhandled rejection or evict flight 2.
    rejectPut1(new Error('late put aborted'));
    await new Promise(resolve => setTimeout(resolve, 0));
    assert.equal(inflightGets.size, 1);
  } finally {
    fetch2Gate.resolve();
  }
  const res2 = await withTimeout(req2, 'req2');
  assert.equal(await res2.text(), 'replacement-flight');
  await withTimeout(Promise.all(waitUntil2), 'waitUntil2');
  assert.equal(originCalls, 2);
  assert.equal(inflightGets.size, 0);
});

test('configurable CANONICAL_HOST and REDIRECT_HOST bindings route safely and invalid hosts fail closed', async () => {
  const customEnv = {
    CANONICAL_HOST: 'metrics.tor.example',
    REDIRECT_HOST: 'dos.metrics.tor.example',
    ASSETS: stubAssets(),
  };
  assert.deepEqual(
    routeRequest(req('https://dos.metrics.tor.example/sub?x=1'), customEnv),
    { kind: 'redirect', location: 'https://metrics.tor.example/onionsurge/sub?x=1' },
  );
  assert.deepEqual(
    routeRequest(req('https://metrics.tor.example/onionsurge/data/manifest.json'), customEnv),
    { kind: 'data', key: 'manifest.json' },
  );
  const redirRes = await handleRequest(req('https://dos.metrics.tor.example/'), customEnv, {});
  assert.equal(redirRes.status, 308);
  assert.equal(redirRes.headers.get('Location'), 'https://metrics.tor.example/onionsurge/');

  // Omitting REDIRECT_HOST derives onionsurge.<CANONICAL_HOST>.
  assert.deepEqual(
    routeRequest(req('https://onionsurge.tor.example/'), { CANONICAL_HOST: 'tor.example' }),
    { kind: 'redirect', location: 'https://tor.example/onionsurge/' },
  );

  // Invalid host bindings or identical canonical/redirect hosts fail closed (503 in handleRequest, throw in routeRequest).
  for (const badEnv of [
    { CANONICAL_HOST: '' },
    { CANONICAL_HOST: 'localhost' },
    { CANONICAL_HOST: 'Bad.Example.com' },
    { CANONICAL_HOST: 'tor.example', REDIRECT_HOST: 'tor.example' },
    { CANONICAL_HOST: 'tor.example', REDIRECT_HOST: 'bad host' },
  ]) {
    assert.throws(() => routeRequest(req('https://example.com/onionsurge/'), badEnv), /Invalid worker host/);
    const res = await handleRequest(req('https://example.com/onionsurge/'), badEnv, {});
    assert.equal(res.status, 503);
  }
});
