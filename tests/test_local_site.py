from contextlib import redirect_stderr
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import io
import json
import os
from pathlib import Path
import re
import subprocess
import sys
import tempfile
import threading
import unittest
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent))
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'scripts'))
import local_site
import publication as pub
from test_publication import END, CONTRACT_SHA256, fixture

LIVE_HTML = (
    '<!doctype html><html lang="en" data-mode="live"><head><title>OnionSurge</title></head>'
    '<body><script src="app.js"></script><script src="live.js"></script></body></html>\n'
)


def make_live_frontend(root, build_dir, commit='a' * 40, name='frontend-rel', suffix=''):
    rel_dir = root / name
    public = rel_dir / 'public'
    public.mkdir(parents=True, exist_ok=True)
    objects = {}
    for fname in sorted(local_site.FRONTEND):
        raw = (LIVE_HTML + suffix).encode() if fname == 'index.html' else f'/* {name} {fname} */\n'.encode()
        (public / fname).write_bytes(raw)
        objects[fname] = {'bytes': len(raw), 'sha256': pub.sha256(raw)}
    build_manifest = build_dir / 'build-manifest.json'
    receipt = {
        'schema_version': 1,
        'mode': 'live',
        'application_commit': commit,
        'build_manifest_sha256': pub.sha256(build_manifest.read_bytes()),
        'objects': objects,
    }
    (rel_dir / 'asset-release.json').write_bytes(pub.json_bytes(receipt))
    return rel_dir


class LocalSiteTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name).resolve()
        self.app = self.root / 'app'
        self.app.mkdir()
        self.deploy = self.root / 'deploy'
        self.deploy.mkdir()
        self.state = self.root / 'state'
        self.state.mkdir(mode=0o700)
        self.site_dir = self.root / 'site-out'
        self.build = self.root / 'build'
        fixture(self.build)
        self.frontend = make_live_frontend(self.root, self.build)
        self.config = {
            'APPLICATION_DIR': str(self.app),
            'APPLICATION_COMMIT': 'a' * 40,
            'DEPLOYMENT_COMMIT': 'b' * 40,
            'STATE_DIR': str(self.state),
            'DO_ENABLED': 'false',
            'R2_ENABLED': 'false',
            'LOCAL_ENABLED': 'true',
            'LOCAL_SITE_DIR': str(self.site_dir),
            'LOCAL_BIND_ADDR': '127.0.0.1:8781',
            'LOCAL_URL_PREFIX': '/',
            'LOCAL_RETAIN_RELEASES': '2',
        }

    def test_bind_address_and_prefix_validation(self):
        for good in ('127.0.0.1:8781', '[::1]:8781', '100.64.0.12:8781', '10.1.2.3:8080',
                     '192.168.10.20:8781', '[fd7a:115c:a1e0::1]:443'):
            _ip, _port, formatted = local_site.parse_private_bind(good)
            self.assertEqual(formatted, good)
        for bad in ('0.0.0.0:8781', '[::]:8781', '8.8.8.8:8781', '192.0.2.1:8781',
                    '169.254.1.1:80', 'localhost:8781', '127.0.0.1:0', '127.0.0.1:99999', 'bad'):
            with self.assertRaises(local_site.LocalSiteError):
                local_site.parse_private_bind(bad)
        self.assertEqual(local_site.normalize_url_prefix('/'), '')
        self.assertEqual(local_site.normalize_url_prefix('/onionsurge'), '/onionsurge')
        for bad_prefix in ('onionsurge', '/onionsurge/', '/a/b', '/..'):
            with self.assertRaises(local_site.LocalSiteError):
                local_site.normalize_url_prefix(bad_prefix)
        for bad_retain in ('', '0', '366'):
            with self.assertRaisesRegex(local_site.LocalSiteError, 'invalid_local_retain_releases'):
                local_site.settings(dict(self.config, LOCAL_RETAIN_RELEASES=bad_retain),
                                   self.deploy, require_checkouts=False)
        for bad_char in ('bad"quote', 'bad{brace', 'bad$var', 'bad\nline'):
            with self.assertRaisesRegex(local_site.LocalSiteError, 'invalid_local_site_directory'):
                local_site.settings(dict(self.config, LOCAL_SITE_DIR=str(self.root / bad_char)),
                                   self.deploy, require_checkouts=False)
        tr_overlap = self.site_dir / 'ranges.json'
        with self.assertRaisesRegex(local_site.LocalSiteError, 'local_site_overlaps_state_or_checkout'):
            local_site.settings(dict(self.config, TIME_RANGES_CONFIG=str(tr_overlap)),
                               self.deploy, require_checkouts=False)
        tr_real = self.root / 'ranges.json'
        tr_real.write_text('{}\n')
        tr_link = self.root / 'ranges-link.json'
        tr_link.symlink_to(tr_real)
        with self.assertRaisesRegex(local_site.LocalSiteError, 'invalid_time_ranges_config'):
            local_site.settings(dict(self.config, TIME_RANGES_CONFIG=str(tr_link)),
                               self.deploy, require_checkouts=False)

    def test_render_caddyfile_supports_loopback_tls_and_subpath_without_lookahead(self):
        spaced_cfg = dict(self.config, LOCAL_SITE_DIR=str(self.root / 'Onion Surge'))
        vals = local_site.settings(spaced_cfg, self.deploy, require_checkouts=False)
        caddy = local_site.render_caddyfile(vals)
        self.assertIn('http://:8781 {', caddy)
        self.assertIn('bind 127.0.0.1', caddy)
        self.assertIn(f'root * "{self.root / "Onion Surge" / "current"}"', caddy)
        self.assertIn('X-Robots-Tag "noindex, nofollow"', caddy)
        self.assertIn('Cache-Control "public, max-age=31536000, immutable"', caddy)
        self.assertRegex(
            caddy,
            r'@onionsurge_assets path [^\n]+\n\s*handle @onionsurge_assets \{[^}]*'
            r'header Content-Security-Policy "default-src \'none\'; sandbox; frame-ancestors \'none\'"',
        )
        self.assertNotIn('(?!', caddy)  # Go RE2 compatibility
        self.assertNotIn('auto_https disable_redirects', caddy)
        legacy_pat = re.search(r'@onionsurge_legacy path_regexp legacy_csv (\S+)', caddy).group(1)
        release_pat = re.search(r'@onionsurge_release path_regexp release_obj (\S+)', caddy).group(1)
        legacy_re = re.compile(legacy_pat)
        release_re = re.compile(release_pat)
        self.assertIsNone(legacy_re.match('/data/releases/rel-1/exports/connection-rate-24h.csv'))
        self.assertIsNotNone(release_re.match('/data/releases/rel-1/exports/connection-rate-24h.csv'))
        for blocked in ('1d', '0d', '0h', '1h', '23h', '024h', '30m', '1m', '60s', 'foo-1h'):
            self.assertIsNotNone(legacy_re.match(f'/data/releases/rel-1/exports/connection-rate-{blocked}.csv'), blocked)

        tls_cfg = dict(self.config, LOCAL_TLS_BIND_ADDR='100.64.10.2:443', LOCAL_URL_PREFIX='/private')
        vals_tls = local_site.settings(tls_cfg, self.deploy, require_checkouts=False)
        caddy_tls = local_site.render_caddyfile(vals_tls, include_global=True)
        self.assertIn('auto_https disable_redirects', caddy_tls)
        self.assertIn('http://:8781 {', caddy_tls)
        self.assertIn('https://100.64.10.2:443 {', caddy_tls)
        self.assertIn('tls internal', caddy_tls)
        self.assertIn('@onionsurge_bare path /private\n    handle @onionsurge_bare {\n        redir * /private/ 308', caddy_tls)
        self.assertIn('@onionsurge_root path /private/', caddy_tls)
        self.assertIn('uri strip_prefix /private', caddy_tls)
        caddy_frag = local_site.render_caddyfile(vals_tls, include_global=False)
        self.assertNotIn('auto_https disable_redirects', caddy_frag)

    def test_rollout_dry_run_activation_idempotency_pruning_and_live_http_verify(self):
        commits = {'application_commit': 'a' * 40, 'deployment_commit': 'b' * 40}
        providers = pub.providers_from_config(self.config)
        # Publish a schema-6 release first so rollover leaves it as root['previous'] without -24h.csv.
        v6_id = '20261118T002000Z-' + 'd' * 12
        v6_dir = self.site_dir / 'data/releases' / v6_id
        v6_dir.mkdir(parents=True)
        (v6_dir / 'exports').mkdir()
        v6_ref_dir = v6_dir / 'reference'
        v6_ref_dir.mkdir()
        raw_data = json.loads((self.build / 'data.json').read_bytes())
        v6_ranges = dict(raw_data['time_ranges'])
        v6_ranges['periods'] = v6_ranges['periods'][1:]
        v6_ranges['default_range'] = '1mo'
        v6_ranges['card_slots'] = {'fallback_dynamic': '1mo', 'fixed': ['6mo', 'all']}
        v6_data = dict(raw_data, schema_version=6, window_seconds=86400, time_ranges=v6_ranges)
        v6_ref_bytes = (self.build / v6_data['reference_document']).read_bytes()
        (v6_dir / v6_data['reference_document']).write_bytes(v6_ref_bytes)
        (v6_dir / 'data.json').write_bytes(pub.json_bytes(v6_data))
        v6_objects = {'data.json': (v6_dir / 'data.json').read_bytes(), v6_data['reference_document']: v6_ref_bytes}
        for chart in v6_data['charts']:
            for period in v6_ranges['periods']:
                csv_rel = f"exports/{chart['id']}-{period['export_suffix']}.csv"
                raw_csv = b'timestamp,value\n1,2\n'
                (v6_dir / csv_rel).write_bytes(raw_csv)
                v6_objects[csv_rel] = raw_csv
        v6_manifest = {'format': 'onionsurge-release-v1', 'id': v6_id, 'data_schema_version': 6,
                       'observation_end': END, 'generated_at': END + 5,
                       'application_commit': 'a' * 40, 'deployment_commit': 'b' * 40,
                       'query_contract_sha256': CONTRACT_SHA256,
                       'objects': {k: {'sha256': pub.sha256(v), 'bytes': len(v)} for k, v in sorted(v6_objects.items())}}
        v6_manifest_raw = pub.json_bytes(v6_manifest)
        (v6_dir / 'manifest.json').write_bytes(v6_manifest_raw)
        v6_desc = pub.descriptor(v6_manifest_raw, v6_id)
        v6_root = {'format': 'onionsurge-root-v1', 'updated_at': END + 5, 'current': v6_desc, 'previous': None}
        (self.site_dir / 'data/manifest.json').write_bytes(pub.json_bytes(v6_root))
        pub.atomic_private(self.state / 'published.json', pub.json_bytes({
            'destinations': sorted(p.identity for p in providers), 'root': v6_root}))

        bad_frontend = self.root / 'bad-frontend-link'
        bad_frontend.symlink_to(self.frontend)
        with mock.patch.object(local_site, 'verify_checkout', side_effect=lambda p, n, pin: pin):
            with self.assertRaisesRegex(local_site.LocalSiteError, 'invalid_frontend'):
                local_site.rollout(self.config, bad_frontend, dry_run=True, deploy_root=self.deploy)
            dry = local_site.rollout(self.config, self.frontend, dry_run=True, deploy_root=self.deploy)
            self.assertEqual(dry['status'], 'dry-run-complete')
            self.assertFalse((self.site_dir / 'current').exists())

            with pub.publication_lock(self.state):
                cand = pub.prepare_candidate(self.build, self.state, **commits, now=END + 10)
                pub_res = pub.publish_candidate(cand, self.state, providers)

            caddy_out = self.root / 'private.caddy'
            res1 = local_site.rollout(self.config, self.frontend, deploy_root=self.deploy, write_caddy=caddy_out)
            self.assertEqual(res1['status'], 'deployed')
            self.assertTrue((self.site_dir / 'current/index.html').is_file())
            self.assertTrue(caddy_out.is_file())

            # Idempotent rollout reuses same frontend dir cleanly.
            res_again = local_site.rollout(self.config, self.frontend, deploy_root=self.deploy)
            self.assertEqual(res_again['frontend_id'], res1['frontend_id'])

            # Subsequent distinct frontends prune older frontend dirs while retaining RETAIN=2.
            frontend_root = self.site_dir / 'frontend'
            for i in range(2):
                b_next = self.root / f'b{i}'
                fixture(b_next, end=END + (i + 1) * 86400)
                f_next = make_live_frontend(self.root, b_next, name=f'f{i}', suffix=f'<!-- {i} -->')
                local_site.rollout(self.config, f_next, deploy_root=self.deploy)
            self.assertEqual(len(list(frontend_root.iterdir())), 2)

        # Serve staged site_dir over localhost HTTP and verify local_site.verify_http end-to-end.
        release_id = pub_res['release_id']
        site_dir = self.site_dir
        corrupt_data = [False]

        class TestHandler(BaseHTTPRequestHandler):
            def log_message(self, *args):
                pass

            def do_GET(self):
                self.send_header_common = [
                    ('X-Robots-Tag', 'noindex, nofollow'),
                    ('X-Content-Type-Options', 'nosniff'),
                    ('X-Frame-Options', 'DENY'),
                ]
                if self.path == '/':
                    body = (site_dir / 'current/index.html').read_bytes()
                    self.send_response(200)
                    for k, v in self.send_header_common:
                        self.send_header(k, v)
                    self.send_header('Content-Security-Policy', local_site.INDEX_CSP)
                    self.send_header('Cache-Control', 'no-store')
                    self.end_headers()
                    self.wfile.write(body)
                elif self.path == '/data/manifest.json':
                    body = (site_dir / 'data/manifest.json').read_bytes()
                    self.send_response(200)
                    for k, v in self.send_header_common:
                        self.send_header(k, v)
                    self.send_header('Content-Security-Policy', local_site.SANDBOX_CSP)
                    self.send_header('Cache-Control', 'no-store')
                    self.end_headers()
                    self.wfile.write(body)
                elif self.path.startswith('/data/releases/') and not self.path.endswith('-1h.csv'):
                    rel_path = self.path.removeprefix('/data/')
                    target = site_dir / 'data' / rel_path
                    if target.is_file():
                        body = b'not json' if corrupt_data[0] and rel_path.endswith('/data.json') else target.read_bytes()
                        self.send_response(200)
                        for k, v in self.send_header_common:
                            self.send_header(k, v)
                        self.send_header('Content-Security-Policy', local_site.SANDBOX_CSP)
                        self.send_header('Cache-Control', local_site.IMMUTABLE_CACHE_CONTROL)
                        self.end_headers()
                        self.wfile.write(body)
                        return
                    self.send_response(404)
                    self.end_headers()
                else:
                    self.send_response(404)
                    self.end_headers()

        server = ThreadingHTTPServer(('127.0.0.1', 0), TestHandler)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        self.addCleanup(server.shutdown)
        self.addCleanup(server.server_close)
        url = f'http://127.0.0.1:{server.server_port}/'
        verified = local_site.verify_http(url, state=self.state)
        self.assertEqual(verified['release_id'], release_id)
        verified_prev = local_site.verify_http(url, state=self.state, expected_release=v6_id)
        self.assertEqual(verified_prev['release_id'], v6_id)

        corrupt_data[0] = True
        with self.assertRaisesRegex(local_site.LocalSiteError, 'verify_data_failed'):
            local_site.verify_http(url, state=self.state)
        corrupt_data[0] = False

        cfg_file = self.root / 'config.env'
        cfg_file.write_text(''.join(f'{k}="{v}"\n' for k, v in self.config.items()))
        cfg_file.chmod(0o600)
        cli = subprocess.run([sys.executable, str(Path(__file__).resolve().parents[1] / 'scripts/local_site.py'),
                              '--config', str(cfg_file), '--verify-url', url],
                             capture_output=True, text=True, check=True)
        self.assertEqual(json.loads(cli.stdout)['release_id'], release_id)

        # Failed post-activation check with retain=1 retains previous frontend for recovery.
        retain_one_cfg = dict(self.config, LOCAL_RETAIN_RELEASES='1')
        previous_id = (self.site_dir / 'current').resolve().name
        b_unverified = self.root / 'b-unverified'
        fixture(b_unverified, end=END + 3 * 86400)
        new_frontend = make_live_frontend(self.root, b_unverified, name='f-unverified', suffix='<!-- u1 -->')
        b_cli = self.root / 'b-cli'
        fixture(b_cli, end=END + 4 * 86400)
        cli_frontend = make_live_frontend(self.root, b_cli, name='f-cli', suffix='<!-- u2 -->')
        corrupt_data[0] = True
        with mock.patch.object(local_site, 'verify_checkout', side_effect=lambda p, n, pin: pin):
            with self.assertRaisesRegex(local_site.LocalSiteError, 'activated_unverified_frontend'):
                local_site.rollout(retain_one_cfg, new_frontend, deploy_root=self.deploy, verify_url=url)
            self.assertTrue((self.site_dir / 'frontend' / previous_id).is_dir())
            # Sync failure immediately after os.replace(current) also raises activated_unverified_frontend.
            b_sync_fail = self.root / 'b-sync-fail'
            fixture(b_sync_fail, end=END + 5 * 86400)
            sync_fail_frontend = make_live_frontend(self.root, b_sync_fail, name='f-sync-fail', suffix='<!-- u3 -->')
            real_sync = local_site.sync_directory
            def fail_site_dir_sync(path):
                if path == self.site_dir:
                    raise OSError('simulated post-replace sync failure')
                return real_sync(path)
            corrupt_data[0] = False
            with mock.patch.object(local_site, 'sync_directory', side_effect=fail_site_dir_sync):
                with self.assertRaisesRegex(local_site.LocalSiteError, 'activated_unverified_frontend'):
                    local_site.rollout(retain_one_cfg, sync_fail_frontend, deploy_root=self.deploy, verify_url=url)
            self.assertTrue((self.site_dir / 'frontend' / previous_id).is_dir())

            corrupt_data[0] = True
            retain_cfg_file = self.root / 'retain-config.env'
            retain_cfg_file.write_text(''.join(f'{k}="{v}"\n' for k, v in retain_one_cfg.items()))
            retain_cfg_file.chmod(0o600)
            stderr_buf = io.StringIO()
            with redirect_stderr(stderr_buf), self.assertRaises(SystemExit) as exit_ctx:
                local_site.main(['--config', str(retain_cfg_file), '--frontend-release', str(cli_frontend),
                                  '--verify-url', url])
        self.assertEqual(exit_ctx.exception.code, 1)
        self.assertIn('previous frontend retained', stderr_buf.getvalue().lower())
        self.assertNotIn(str(self.root), stderr_buf.getvalue())


if __name__ == '__main__':
    unittest.main()
