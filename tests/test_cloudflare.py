import hashlib
import http.client
import io
import json
import os
from pathlib import Path
import stat
import subprocess
import sys
import tempfile
import unittest
from unittest import mock
from urllib.parse import parse_qs, urlsplit

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'scripts'))
import cloudflare as rollout
from publication import providers_from_config


class FakeAPI:
    def __init__(self, config):
        self.config = config
        self.routes = []
        self.alias = []
        self.bindings = None
        self.writes = []
        self.calls = []
        self.zone_account = config['CLOUDFLARE_ACCOUNT_ID']
        self.apex_proxied = True
        self.previews = False
        self.workers_dev = False
        self.account_routes = None
        self.route_info = None
        self.domains = []
        self.domain_info = {'page': 1, 'per_page': 0, 'count': 0, 'total_count': 0}
        self.inventory_error = None

    def deployed(self, config_path):
        config = json.loads(Path(config_path).read_text())
        self.bindings = [{'name': key, 'type': 'plain_text', 'text': value} for key, value in config['vars'].items()]
        self.bindings.append({'name': 'ASSETS', 'type': 'assets'})
        for binding in config.get('r2_buckets', []):
            self.bindings.append({'name': binding['binding'], 'type': 'r2_bucket', 'bucket_name': binding['bucket_name']})
        self.routes = [{'pattern': item['pattern'], 'script': config['name']} for item in config['routes']]

    def request(self, method, path, payload=None, missing=False):
        self.calls.append((method, path))
        if method != 'GET':
            self.writes.append((method, path, payload))
            assert method == 'POST' and path.endswith('/dns_records')
            self.alias = [{**payload, 'id': 'd' * 32}]
            return {'success': True, 'result': self.alias[0]}
        if path == '/zones/' + self.config['CLOUDFLARE_ZONE_ID']:
            canonical = self.config.get('CLOUDFLARE_CANONICAL_HOST') or rollout.CANONICAL
            result = {'name': canonical, 'status': 'active', 'account': {'id': self.zone_account}}
        elif path.endswith('/settings'):
            if self.bindings is None:
                assert missing
                return None
            result = {'bindings': self.bindings}
        elif path.endswith('/workers/routes'):
            result = self.routes
        elif path.endswith('/environments/production/routes?show_zonename=true'):
            canonical = self.config.get('CLOUDFLARE_CANONICAL_HOST') or rollout.CANONICAL
            if self.inventory_error:
                raise rollout.RolloutError(self.inventory_error)
            result = self.account_routes if self.account_routes is not None else [
                {**route, 'id': str(index), 'zone_name': canonical}
                for index, route in enumerate(self.routes) if route.get('script') == self.config['WORKER_NAME']]
            return {'success': True, 'result': result, **({'result_info': self.route_info} if self.route_info is not None else {})}
        elif path.endswith('/workers/domains'):
            return {'success': True, 'result': self.domains, 'result_info': self.domain_info}
        elif '/dns_records?' in path:
            canonical = self.config.get('CLOUDFLARE_CANONICAL_HOST') or rollout.CANONICAL
            alias = self.config.get('CLOUDFLARE_REDIRECT_HOST') or f'onionsurge.{canonical}'
            name = parse_qs(urlsplit(path).query)['name'][0]
            result = self.alias if name == alias else [{'name': canonical, 'type': 'A', 'proxied': self.apex_proxied, 'content': '192.0.2.1'}]
        elif '/r2/buckets/' in path:
            result = {'name': self.config['R2_BUCKET']}
        elif path.endswith('/subdomain'):
            result = {'enabled': self.workers_dev, 'previews_enabled': self.previews}
        elif path.endswith('/deployments'):
            result = {'deployments': [{'versions': [{'version_id': '11111111-1111-4111-8111-111111111111', 'percentage': 100}]}]}
        else:
            raise AssertionError(path)
        return {'success': True, 'result': result}


class CloudflareTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name).resolve()
        self.app, self.deploy, self.state = [self.root / name for name in ('app', 'deploy', 'state')]
        self.app.mkdir(); self.deploy.mkdir()
        self.config = {'APPLICATION_DIR': str(self.app), 'APPLICATION_COMMIT': 'a' * 40,
            'DEPLOYMENT_COMMIT': 'b' * 40, 'STATE_DIR': str(self.state),
            'CLOUDFLARE_ACCOUNT_ID': 'a' * 32, 'CLOUDFLARE_ZONE_ID': 'b' * 32,
            'CLOUDFLARE_API_TOKEN': 'private-fixture-token-DO-NOT-PUBLISH',
            'WORKER_NAME': 'onionsurge', 'ROOT_CACHE_GENERATION': 'generation-one',
            'DO_ENABLED': 'true', 'DO_SPACES_BUCKET': 'public-fixture', 'DO_SPACES_REGION': 'nyc3',
            'DO_SPACES_PREFIX': 'metrics', 'DO_SPACES_KEY': 'fixture-key', 'DO_SPACES_SECRET': 'fixture-secret',
            'RCLONE_PATH': str(self.root / 'rclone'), 'R2_ENABLED': 'false'}
        self.release = self.root / 'release'; public = self.release / 'public'; public.mkdir(parents=True)
        objects = {}
        for name in rollout.FRONTEND:
            raw = (b'<html data-mode="live"><script src="app.js"></script><script src="live.js"></script></html>'
                   if name == 'index.html' else b'public fixture\n')
            (public / name).write_bytes(raw)
            objects[name] = {'bytes': len(raw), 'sha256': hashlib.sha256(raw).hexdigest()}
        self.receipt = {'schema_version': 1, 'mode': 'live', 'application_commit': 'a' * 40,
                        'build_manifest_sha256': 'c' * 64, 'objects': objects}
        (self.release / 'asset-release.json').write_text(json.dumps(self.receipt))
        for package, version in [('node', rollout.NODE_VERSION), ('wrangler', rollout.WRANGLER_VERSION)]:
            directory = self.deploy / 'node_modules' / package; directory.mkdir(parents=True)
            (directory / 'package.json').write_text(json.dumps({'version': version}))
        for name in ['node_modules/node/bin/node', 'node_modules/wrangler/bin/wrangler.js', 'edge/worker.mjs']:
            path = self.deploy / name; path.parent.mkdir(exist_ok=True, parents=True); path.write_text('fixture')
        self.api = FakeAPI(self.config)
        self.commands = []
        self.verify = mock.patch.object(rollout, 'verify_checkout', return_value='a' * 40).start()
        self.addCleanup(mock.patch.stopall)

    def run_command(self, command, **options):
        self.commands.append((command, options))
        if command[-1] == '--version':
            return subprocess.CompletedProcess(command, 0, 'v' + rollout.NODE_VERSION + '\n', '')
        self.assertEqual(command[:2], [str(self.deploy / 'node_modules/node/bin/node'), str(self.deploy / 'node_modules/wrangler/bin/wrangler.js')])
        self.assertNotIn(self.config['CLOUDFLARE_API_TOKEN'], ' '.join(command))
        self.assertEqual(options['env']['CLOUDFLARE_API_TOKEN'], self.config['CLOUDFLARE_API_TOKEN'])
        self.assertNotIn('DO_SPACES_SECRET', options['env'])
        self.assertNotIn('NODE_OPTIONS', options['env'])
        if '--dry-run' not in command:
            self.api.deployed(command[command.index('--config') + 1])
        return subprocess.CompletedProcess(command, 0, 'tool-private-detail ' + self.config['CLOUDFLARE_API_TOKEN'], '')

    def published(self, config=None):
        self.state.mkdir(mode=0o700, exist_ok=True)
        value = {'destinations': sorted(provider.identity for provider in providers_from_config(config or self.config)),
                 'root': {'format': 'onionsurge-root-v1', 'updated_at': 1,
                          'current': {'id': 'fixture', 'manifest_sha256': 'a' * 64, 'manifest_bytes': 2}, 'previous': None}}
        (self.state / 'published.json').write_text(json.dumps(value))

    def call(self, dry=True):
        return rollout.rollout(self.config, self.release, dry, deploy_root=self.deploy, api=self.api, run=self.run_command)

    def owned_worker(self, **changes):
        values = rollout.settings(self.config, self.deploy)
        cfg = rollout.wrangler_config(values, self.release / 'public', self.deploy)
        cfg['vars'].update(changes)
        path = self.root / 'fixture-wrangler.json'; path.write_text(json.dumps(cfg)); self.api.deployed(path)

    def test_dry_run_has_no_api_writes_and_keeps_settings_and_logs_private(self):
        with mock.patch.dict(os.environ, {'NODE_OPTIONS': '--require untrusted', 'DO_SPACES_SECRET': 'ambient-secret'}):
            self.assertEqual(self.call(), {'status': 'dry-run-complete'})
        self.assertEqual(self.api.writes, [])
        self.assertTrue(all('--dry-run' in command for command, _ in self.commands if command[-1] != '--version'))
        work = next(self.state.glob('cloudflare-*'))
        config = json.loads((work / 'wrangler.json').read_text())
        self.assertFalse(config['workers_dev']); self.assertFalse(config['preview_urls'])
        self.assertEqual({row['pattern'] for row in config['routes']}, set(rollout.ROUTES))
        self.assertTrue(config['assets']['run_worker_first'])
        self.assertEqual(config['assets']['directory'], str(self.release / 'public'))
        self.assertEqual(config['vars']['PUBLIC_DATA_ORIGIN'], 'https://public-fixture.nyc3.digitaloceanspaces.com/metrics')
        self.assertNotIn(self.config['CLOUDFLARE_API_TOKEN'], (work / 'wrangler.json').read_text())
        self.assertNotIn(self.config['CLOUDFLARE_API_TOKEN'], (work / 'tool-output.log').read_text())
        self.assertIn('tool-private-detail', (work / 'tool-output.log').read_text())
        for path in [work, work / 'wrangler.json', work / 'tool-output.log']:
            self.assertEqual(stat.S_IMODE(path.stat().st_mode) & 0o077, 0)
        self.verify.assert_any_call(self.app, 'onionsurge', 'a' * 40)
        self.verify.assert_any_call(self.deploy, 'onionsurge-deploy', 'b' * 40)

    def test_apply_checks_publication_and_adds_only_the_exact_alias(self):
        with self.assertRaisesRegex(rollout.RolloutError, 'published_release_required'):
            self.call(False)
        self.assertEqual(self.api.writes, [])
        self.published(); self.assertEqual(self.call(False), {'status': 'deployed'})
        self.assertEqual(len(self.api.writes), 1)
        self.assertEqual(self.api.writes[0][2], {'type': 'CNAME', 'name': rollout.ALIAS,
                         'content': rollout.CANONICAL, 'proxied': True, 'ttl': 1})
        self.assertTrue(list(self.state.glob('cloudflare-*/completed.json')))
        self.api.writes.clear(); self.call(False); self.assertEqual(self.api.writes, [])

    def test_worker_and_route_conflicts_fail_before_wrangler_deploy(self):
        for routes, bindings in [
            ([{'pattern': rollout.ROUTES[0], 'script': 'another-worker'}], None),
            ([{'pattern': 'example.com/*', 'script': 'another-worker'}], None),
            ([{'pattern': '*.example.com/*', 'script': None}], None),
            ([{'pattern': 'example.com/blog*', 'script': 'onionsurge'}], None),
            ([], []),
        ]:
            with self.subTest(routes=routes, bindings=bindings):
                self.api.routes, self.api.bindings = routes, bindings
                self.commands.clear()
                with self.assertRaises(rollout.RolloutError): self.call()
                self.assertTrue(all(command[-1] == '--version' for command, _ in self.commands))
                self.assertEqual(self.api.writes, [])

    def test_overlapping_route_patterns_are_conservative_but_other_apps_are_allowed(self):
        for pattern in ['example.com/onionsurge', 'https://example.com/onionsurge/data/*', '*://example.com/onionsurge/*', 'example.com/onion*',
                        'example.com/onionsurge-other/*', '*.example.com/*', '*/*', 'example.com/*/manifest.json']:
            self.assertTrue(rollout.route_overlaps(pattern), pattern)
        for pattern in ['example.com/blog*', '*://example.com/blog*', 'example.com/onionflash*', 'example.com/onion',
                        'other.example.com/*', 'onionsurge.example.com.invalid/*']:
            self.assertFalse(rollout.route_overlaps(pattern), pattern)
        rollout.check_routes([{'pattern': 'example.com/blog*', 'script': 'blog-worker'}], 'onionsurge')
        with self.assertRaises(rollout.RolloutError):
            rollout.check_routes([{'pattern': rollout.ROUTES[0], 'script': 'onionsurge'}] * 2, 'onionsurge')

    def test_other_zone_attachment_blocks_code_upload_even_when_local_routes_are_safe(self):
        self.owned_worker()
        self.api.account_routes = [
            {'id': 'foreign', 'pattern': 'other.example.test/*', 'zone_name': 'example.test',
             'script': self.config['WORKER_NAME']},
        ]
        with self.assertRaisesRegex(rollout.RolloutError, 'worker_has_unrelated_routes'):
            self.call()
        self.assertTrue(all(command[-1] == '--version' for command, _ in self.commands))
        self.assertEqual(self.api.writes, [])
        self.assertFalse(any(path == '/zones' for _, path in self.api.calls))

    def test_account_inventory_must_be_complete_and_agree_with_configured_zone(self):
        self.owned_worker()
        for error in ['account_inventory_forbidden', 'account_inventory_missing']:
            self.api.inventory_error = error
            with self.subTest(error=error), self.assertRaisesRegex(rollout.RolloutError, error): self.call()
        self.api.inventory_error = None
        for info in [
            {'page': 1, 'per_page': 2, 'count': 2, 'total_count': 3, 'total_pages': 2},
            {'page': 2, 'count': 2, 'total_count': 2},
            {'count': 2}, {'total_count': True}, {'total_count': 2, 'cursor': 'more'},
        ]:
            self.api.route_info = info
            with self.subTest(info=info), self.assertRaisesRegex(rollout.RolloutError, 'incomplete_worker_route_inventory'):
                self.call()
        self.api.route_info = None
        self.api.account_routes = []
        with self.assertRaisesRegex(rollout.RolloutError, 'inventories_disagree'): self.call()
        self.assertTrue(all(command[-1] == '--version' for command, _ in self.commands))
        self.assertEqual(self.api.writes, [])

        # Explicit "result_info": null is valid on unpaginated Cloudflare responses, while non-dict values fail closed.
        self.assertEqual(
            rollout.complete_account_inventory({'success': True, 'result': [{'id': '1'}], 'result_info': None}, 'invalid'),
            [{'id': '1'}],
        )
        for bad_info in ([], False, ''):
            with self.subTest(bad_info=bad_info), self.assertRaisesRegex(rollout.RolloutError, 'incomplete'):
                rollout.complete_account_inventory({'success': True, 'result': [], 'result_info': bad_info}, 'incomplete')
        null_api = mock.Mock()
        null_api.request.side_effect = [
            {'success': True, 'result': [{'pattern': p, 'script': self.config['WORKER_NAME'], 'zone_id': self.config['CLOUDFLARE_ZONE_ID']} for p in rollout.ROUTES], 'result_info': None},
            {'success': True, 'result': [], 'result_info': None},
            {'success': True, 'result': {'enabled': False, 'previews_enabled': False}},
        ]
        self.assertEqual(
            rollout.check_worker_attachments(null_api, self.config['CLOUDFLARE_ACCOUNT_ID'], self.config['CLOUDFLARE_ZONE_ID'], self.config['WORKER_NAME']),
            set(rollout.ROUTES),
        )
        dns_null_api = mock.Mock()
        dns_null_api.request.return_value = {'success': True, 'result': [{'name': rollout.CANONICAL, 'type': 'A', 'proxied': True}], 'result_info': None}
        self.assertEqual(len(rollout.dns_records(dns_null_api, self.config['CLOUDFLARE_ZONE_ID'], rollout.CANONICAL)), 1)

    def test_account_routes_require_selected_worker_and_zone_identity(self):
        self.owned_worker()
        base = {'pattern': rollout.ROUTES[0], 'script': self.config['WORKER_NAME'], 'zone_name': rollout.CANONICAL}
        for row in [dict(base, script='other'), dict(base, zone_id='f' * 32),
                    {key: value for key, value in base.items() if key != 'zone_name'}]:
            self.api.account_routes = [row]
            with self.subTest(row=row), self.assertRaisesRegex(rollout.RolloutError, 'worker_has_unrelated_routes'):
                self.call()
        self.api.account_routes = [base, base]
        with self.assertRaisesRegex(rollout.RolloutError, 'worker_has_unrelated_routes'): self.call()
        self.api.account_routes = [{'pattern': pattern, 'script': self.config['WORKER_NAME'],
                                    'zone_id': self.config['CLOUDFLARE_ZONE_ID']} for pattern in rollout.ROUTES]
        self.assertEqual(self.call(), {'status': 'dry-run-complete'})

    def test_custom_domains_and_unscoped_urls_are_checked_before_upload(self):
        self.owned_worker()
        self.api.domains = [{'service': self.config['WORKER_NAME'], 'hostname': 'other.example.test',
                             'environment': 'production'}]
        self.api.domain_info = {'page': 1, 'per_page': 1, 'count': 1, 'total_count': 1}
        with self.assertRaisesRegex(rollout.RolloutError, 'worker_has_custom_domains'): self.call()
        self.api.domains[0]['service'] = 'other-worker'
        self.api.domain_info['total_count'] = 2
        with self.assertRaisesRegex(rollout.RolloutError, 'incomplete_worker_domain_inventory'): self.call()
        self.api.domain_info['total_count'] = 1
        self.api.workers_dev = True
        with self.assertRaisesRegex(rollout.RolloutError, 'unscoped_worker_urls_enabled'): self.call()
        self.api.workers_dev = False; self.api.previews = True
        with self.assertRaisesRegex(rollout.RolloutError, 'unscoped_worker_urls_enabled'): self.call()
        self.assertTrue(all(command[-1] == '--version' for command, _ in self.commands))
        self.api.previews = False
        self.api.domain_info['per_page'] = 0  # The account endpoint can report an unpaginated list.
        self.assertEqual(self.call(), {'status': 'dry-run-complete'}, 'another Worker custom domain remains untouched')
        self.assertEqual(self.api.writes, [])

    def test_dns_conflicts_and_wrong_zone_never_change_records(self):
        for record in [
            {'name': rollout.ALIAS, 'type': 'CNAME', 'content': rollout.CANONICAL, 'proxied': False},
            {'name': rollout.ALIAS, 'type': 'CNAME', 'content': 'other.example', 'proxied': True},
            {'name': rollout.ALIAS, 'type': 'A', 'content': '192.0.2.2', 'proxied': True},
        ]:
            self.api.alias = [record]
            with self.assertRaisesRegex(rollout.RolloutError, 'alias_dns_conflict'): self.call()
            self.assertEqual(self.api.writes, [])
        self.api.alias = []; self.api.zone_account = 'f' * 32
        with self.assertRaisesRegex(rollout.RolloutError, 'zone_account_mismatch'): self.call()
        self.api.zone_account = self.config['CLOUDFLARE_ACCOUNT_ID']; self.api.apex_proxied = False
        with self.assertRaisesRegex(rollout.RolloutError, 'canonical_dns'): self.call()

    def test_live_receipt_and_every_asset_are_checked(self):
        public = self.release / 'public'
        for change in ['hash', 'extra', 'mode', 'pin', 'symlink']:
            with self.subTest(change=change), tempfile.TemporaryDirectory() as folder:
                from shutil import copytree
                target = Path(folder) / 'release'; copytree(self.release, target)
                if change == 'hash': (target / 'public/app.js').write_text('changed')
                elif change == 'extra': (target / 'public/data.js').write_text('sample')
                elif change == 'symlink':
                    (target / 'public/app.js').unlink(); (target / 'public/app.js').symlink_to(public / 'app.js')
                else:
                    receipt = dict(self.receipt); receipt['mode' if change == 'mode' else 'application_commit'] = 'wrong'
                    (target / 'asset-release.json').write_text(json.dumps(receipt))
                with self.assertRaises((OSError, ValueError)): rollout.verify_frontend(target, 'a' * 40)

    def test_storage_change_requires_verified_migration_and_new_cache_generation(self):
        self.owned_worker(PUBLIC_DATA_ORIGIN='https://public-fixture.nyc3.digitaloceanspaces.com/old')
        with self.assertRaisesRegex(rollout.RolloutError, 'fresh_cache_generation_required'): self.call()
        self.config['ROOT_CACHE_GENERATION'] = 'generation-two'
        with self.assertRaisesRegex(rollout.RolloutError, 'published_release_required'): self.call()
        old = dict(self.config, DO_SPACES_PREFIX='old'); self.published(old)
        with self.assertRaisesRegex(ValueError, 'migration_required'): self.call()
        self.published(); self.assertEqual(self.call(), {'status': 'dry-run-complete'})

    def test_optional_r2_uses_same_account_binding_and_prefix(self):
        self.config.update(R2_ENABLED='true', R2_BUCKET='r2-fixture', R2_PREFIX='mirror',
                           R2_ACCESS_KEY_ID='fixture-r2-key', R2_SECRET_ACCESS_KEY='fixture-r2-secret')
        self.call(); work = next(self.state.glob('cloudflare-*')); config = json.loads((work / 'wrangler.json').read_text())
        self.assertEqual(config['r2_buckets'], [{'binding': 'ONIONSURGE_DATA', 'bucket_name': 'r2-fixture'}])
        self.assertEqual(config['vars']['R2_PREFIX'], 'mirror')
        self.assertIn(('GET', '/accounts/' + self.config['CLOUDFLARE_ACCOUNT_ID'] + '/r2/buckets/r2-fixture'), self.api.calls)

    def test_wrong_toolchain_dirty_checkouts_or_pending_publication_abort(self):
        metadata = self.deploy / 'node_modules/wrangler/package.json'
        metadata.write_text('{"version":"0.0.0"}')
        with self.assertRaisesRegex(rollout.RolloutError, 'pinned_toolchain'): self.call()
        self.assertEqual(self.api.calls, [])
        metadata.write_text(json.dumps({'version': rollout.WRANGLER_VERSION}))
        self.verify.side_effect = ValueError('uncommitted')
        with self.assertRaises(ValueError): self.call()
        self.assertEqual(self.api.calls, [])
        self.verify.side_effect = None
        self.state.mkdir(mode=0o700, exist_ok=True); (self.state / 'pending-promotion.json').write_text('{}')
        with self.assertRaisesRegex(rollout.RolloutError, 'pending_publication'): self.call()
        self.assertEqual(self.api.writes, [])

    def test_command_failure_logs_are_redacted_and_cli_output_is_fixed(self):
        def fail(command, **options):
            if command[-1] == '--version': return self.run_command(command, **options)
            return subprocess.CompletedProcess(command, 1, '', self.config['CLOUDFLARE_API_TOKEN'])
        with self.assertRaisesRegex(rollout.RolloutError, 'wrangler_failed'):
            rollout.rollout(self.config, self.release, True, deploy_root=self.deploy, api=self.api, run=fail)
        self.assertEqual(self.api.writes, [])
        for log in self.state.glob('cloudflare-*/tool-output.log'):
            self.assertNotIn(self.config['CLOUDFLARE_API_TOKEN'], log.read_text())
        with mock.patch.object(rollout, 'load_config', return_value=self.config), \
                mock.patch.object(rollout, 'rollout', side_effect=ValueError(self.config['CLOUDFLARE_API_TOKEN'])), \
                mock.patch('sys.stderr', new_callable=io.StringIO) as output:
            with self.assertRaises(SystemExit): rollout.main(['--config', 'private.env', '--frontend-release', 'release'])
            self.assertEqual(output.getvalue(), 'Cloudflare rollout failed. Inspect the private rollout log.\n')

    def test_zone_route_pagination_binding_types_dangling_receipt_and_umask(self):
        # 1) Paginated /zones/{zone}/workers/routes fails closed with invalid_routes_response.
        orig_request = self.api.request
        def paginated_zone_routes(method, path, payload=None, missing=False):
            res = orig_request(method, path, payload=payload, missing=missing)
            if method == 'GET' and path.endswith('/workers/routes'):
                res['result_info'] = {'page': 1, 'per_page': 1, 'count': len(res['result']), 'total_count': 2, 'total_pages': 2}
            return res
        with mock.patch.object(self.api, 'request', side_effect=paginated_zone_routes):
            with self.assertRaisesRegex(rollout.RolloutError, 'invalid_routes_response'):
                self.call()

        # 2) Dangling published.json fails closed (both dry-run and apply) via O_NOFOLLOW instead of appearing absent.
        self.state.mkdir(mode=0o700, exist_ok=True)
        (self.state / 'published.json').symlink_to(self.root / 'missing-published.json')
        for dry in (True, False):
            with self.subTest(dry=dry), self.assertRaises(OSError):
                self.call(dry)
        (self.state / 'published.json').unlink()

        # 3) rollout() sets umask 0o077 under publication_lock and restores previous umask on return.
        seen_umasks = []
        def tracking_run(command, **options):
            current = os.umask(0o077)
            os.umask(current)
            seen_umasks.append(current)
            return self.run_command(command, **options)
        prior_umask = os.umask(0o022)
        try:
            self.assertEqual(rollout.rollout(self.config, self.release, True, deploy_root=self.deploy, api=self.api, run=tracking_run),
                             {'status': 'dry-run-complete'})
            self.assertTrue(all(mask == 0o077 for mask in seen_umasks))
            after = os.umask(0o022)
            self.assertEqual(after, 0o022)
        finally:
            os.umask(prior_umask)

        # 4) preflight(..., complete=True) rejects wrong binding types and leftover R2 bindings when disabled.
        self.published()
        self.assertEqual(self.call(False), {'status': 'deployed'})
        values = rollout.settings(self.config, self.deploy)
        good_bindings = list(self.api.bindings)
        bad_sets = [
            [b for b in good_bindings if b['name'] != 'ASSETS'] + [{'name': 'ASSETS', 'type': 'plain_text', 'text': 'wrong'}],
            [b for b in good_bindings if b['name'] != 'PUBLIC_DATA_ORIGIN'] + [{'name': 'PUBLIC_DATA_ORIGIN', 'type': 'secret_text', 'text': values['origin']}],
            [b for b in good_bindings if b['name'] != 'ROOT_CACHE_GENERATION'] + [{'name': 'ROOT_CACHE_GENERATION', 'type': 'secret_text', 'text': values['generation']}],
            [*good_bindings, {'name': 'ONIONSURGE_DATA', 'type': 'r2_bucket', 'bucket_name': 'stale'}],
            [*good_bindings, {'name': 'R2_PREFIX', 'type': 'plain_text', 'text': 'stale'}],
        ]
        for bad_bindings in bad_sets:
            self.api.bindings = bad_bindings
            with self.subTest(bad_bindings=bad_bindings), self.assertRaisesRegex(rollout.RolloutError, 'worker_binding_mismatch'):
                rollout.preflight(self.api, values, complete=True)

        # When R2 is enabled, wrong ONIONSURGE_DATA / R2_PREFIX types are also rejected at complete=True.
        r2_cfg = dict(self.config, R2_ENABLED='true', R2_BUCKET='r2-fixture', R2_PREFIX='mirror',
                      R2_ACCESS_KEY_ID='fixture-r2-key', R2_SECRET_ACCESS_KEY='fixture-r2-secret')
        r2_values = rollout.settings(r2_cfg, self.deploy)
        r2_api = FakeAPI(r2_cfg)
        r2_api.routes, r2_api.alias = list(self.api.routes), list(self.api.alias)
        r2_base = [*good_bindings, {'name': 'ONIONSURGE_DATA', 'type': 'r2_bucket', 'bucket_name': 'r2-fixture'},
                   {'name': 'R2_PREFIX', 'type': 'plain_text', 'text': 'mirror'}]
        r2_api.bindings = r2_base
        rollout.preflight(r2_api, r2_values, complete=True)
        for bad_r2 in (
            [*good_bindings, {'name': 'ONIONSURGE_DATA', 'type': 'plain_text', 'bucket_name': 'r2-fixture'},
             {'name': 'R2_PREFIX', 'type': 'plain_text', 'text': 'mirror'}],
            [*good_bindings, {'name': 'ONIONSURGE_DATA', 'type': 'r2_bucket', 'bucket_name': 'r2-fixture'},
             {'name': 'R2_PREFIX', 'type': 'secret_text', 'text': 'mirror'}],
        ):
            r2_api.bindings = bad_r2
            with self.subTest(bad_r2=bad_r2), self.assertRaisesRegex(rollout.RolloutError, 'worker_binding_mismatch'):
                rollout.preflight(r2_api, r2_values, complete=True)

    def test_http_exception_dns_pagination_redos_and_fresh_state_umask(self):
        # 1) http.client.HTTPException (e.g. IncompleteRead) becomes RolloutError('cloudflare_api_failed').
        client = rollout.Cloudflare('token-fixture')
        with mock.patch.object(client.opener, 'open', side_effect=http.client.IncompleteRead(b'part')):
            with self.assertRaisesRegex(rollout.RolloutError, 'cloudflare_api_failed'):
                client.request('GET', '/zones/b')

        # 2) dns_records rejects string total_pages or page 2 without total_pages via complete_account_inventory.
        for bad_info in (
            {'total_pages': '2'},
            {'total_pages': True},
            {'page': 2, 'per_page': 100, 'count': 1, 'total_count': 1},
        ):
            bad_dns_api = mock.Mock()
            bad_dns_api.request.return_value = {
                'success': True, 'result': [{'name': rollout.CANONICAL, 'type': 'A', 'proxied': True}],
                'result_info': bad_info,
            }
            with self.subTest(bad_info=bad_info), self.assertRaisesRegex(rollout.RolloutError, 'incomplete_dns_response'):
                rollout.dns_records(bad_dns_api, 'b' * 32, rollout.CANONICAL)

        # 3) route_overlaps collapses consecutive wildcards without exponential backtracking.
        self.assertFalse(rollout.route_overlaps('*' * 40 + 'x/foo'))
        self.assertTrue(rollout.route_overlaps('*' * 40 + '/foo*'))

        # 4) rollout() sets umask(0o077) BEFORE publication_lock so fresh state dir & lock succeed under 0o277 umask.
        fresh_state = self.root / 'fresh-parent' / 'state'
        fresh_cfg = dict(self.config, STATE_DIR=str(fresh_state))
        prior_umask = os.umask(0o277)
        try:
            self.assertEqual(rollout.rollout(fresh_cfg, self.release, True, deploy_root=self.deploy, api=self.api, run=self.run_command),
                             {'status': 'dry-run-complete'})
            restored = os.umask(0o277)
            self.assertEqual(restored, 0o277)
        finally:
            os.umask(prior_umask)
        self.assertEqual(stat.S_IMODE(fresh_state.stat().st_mode), 0o700)
        self.assertEqual(stat.S_IMODE((fresh_state / 'publication.lock').stat().st_mode), 0o600)

    def test_filtered_dns_pagination_allows_zone_total_count_while_rejecting_truncation(self):
        record = {'name': rollout.CANONICAL, 'type': 'A', 'proxied': True}
        record2 = {'name': rollout.CANONICAL, 'type': 'AAAA', 'proxied': True}
        for good_info in (
            {'count': 1, 'page': 1, 'per_page': 100, 'total_count': 42, 'total_pages': 1},
            {'count': 1, 'page': 1, 'per_page': 100, 'total_count': 42},
        ):
            good_api = mock.Mock()
            good_api.request.return_value = {'success': True, 'result': [record], 'result_info': good_info}
            with self.subTest(good_info=good_info):
                self.assertEqual(rollout.dns_records(good_api, 'b' * 32, rollout.CANONICAL), [record])

        # Multi-page filtered responses succeed when a zone has >100 records, accumulating all matches cleanly.
        paged_empty_second = mock.Mock()
        paged_empty_second.request.side_effect = [
            {'success': True, 'result': [record], 'result_info': {'count': 1, 'page': 1, 'per_page': 100, 'total_count': 150, 'total_pages': 2}},
            {'success': True, 'result': [], 'result_info': {'count': 0, 'page': 2, 'per_page': 100, 'total_count': 150, 'total_pages': 2}},
        ]
        self.assertEqual(rollout.dns_records(paged_empty_second, 'b' * 32, rollout.CANONICAL), [record])
        self.assertEqual(paged_empty_second.request.call_count, 2)
        self.assertIn('page=2', paged_empty_second.request.call_args_list[1].args[1])

        paged_both = mock.Mock()
        paged_both.request.side_effect = [
            {'success': True, 'result': [record], 'result_info': {'count': 1, 'page': 1, 'per_page': 1, 'total_count': 150, 'total_pages': 2}},
            {'success': True, 'result': [record2], 'result_info': {'count': 1, 'page': 2, 'per_page': 1, 'total_count': 150, 'total_pages': 2}},
        ]
        self.assertEqual(rollout.dns_records(paged_both, 'b' * 32, rollout.CANONICAL), [record, record2])

        # Zones with >20 pages (e.g. total_pages=25) work: empty page 2 stops early, and 25 matching pages all accumulate.
        paged_25_early_stop = mock.Mock()
        paged_25_early_stop.request.side_effect = [
            {'success': True, 'result': [record], 'result_info': {'count': 1, 'page': 1, 'per_page': 100, 'total_count': 2500, 'total_pages': 25}},
            {'success': True, 'result': [], 'result_info': {'count': 0, 'page': 2, 'per_page': 100, 'total_count': 2500, 'total_pages': 25}},
        ]
        self.assertEqual(rollout.dns_records(paged_25_early_stop, 'b' * 32, rollout.CANONICAL), [record])
        self.assertEqual(paged_25_early_stop.request.call_count, 2)

        paged_25_all = mock.Mock()
        paged_25_all.request.side_effect = [
            {'success': True, 'result': [record], 'result_info': {'count': 1, 'page': p, 'per_page': 1, 'total_count': 25, 'total_pages': 25}}
            for p in range(1, 26)
        ]
        self.assertEqual(len(rollout.dns_records(paged_25_all, 'b' * 32, rollout.CANONICAL)), 25)
        self.assertEqual(paged_25_all.request.call_count, 25)

        for bad_info in (
            {'count': 1, 'page': 1, 'per_page': 1, 'total_count': 42},
            {'count': 1, 'total_count': 0},
            {'count': 1, 'page': 1, 'per_page': 100, 'total_count': 42, 'total_pages': 2},
            {'count': 1, 'page': 1, 'per_page': 100, 'total_count': 10001, 'total_pages': rollout.MAX_DNS_PAGES + 1},
        ):
            bad_api = mock.Mock()
            bad_api.request.return_value = {'success': True, 'result': [record], 'result_info': bad_info}
            with self.subTest(bad_info=bad_info), self.assertRaisesRegex(rollout.RolloutError, 'incomplete_dns_response'):
                rollout.dns_records(bad_api, 'b' * 32, rollout.CANONICAL)

        for bad_page2_info in (
            {'count': 0, 'page': 2, 'per_page': 100, 'total_count': 150, 'total_pages': 3},
            {'count': 0, 'page': 1, 'per_page': 100, 'total_count': 150, 'total_pages': 2},
            {'page': 2, 'total_pages': 2},
            None,
        ):
            drift_api = mock.Mock()
            drift_api.request.side_effect = [
                {'success': True, 'result': [record], 'result_info': {'count': 1, 'page': 1, 'per_page': 100, 'total_count': 150, 'total_pages': 2}},
                {'success': True, 'result': [], **({} if bad_page2_info is None else {'result_info': bad_page2_info})},
            ]
            with self.subTest(bad_page2_info=bad_page2_info), self.assertRaisesRegex(rollout.RolloutError, 'incomplete_dns_response'):
                rollout.dns_records(drift_api, 'b' * 32, rollout.CANONICAL)

        with self.assertRaisesRegex(rollout.RolloutError, 'incomplete_account_inventory'):
            rollout.complete_account_inventory(
                {'success': True, 'result': [record], 'result_info': {'count': 1, 'total_count': 42, 'total_pages': 1}},
                'incomplete_account_inventory',
            )

    def test_rollout_detects_frontend_release_swap_post_preflight_alias_drift_and_tolerates_log_oserror(self):
        self.published()
        public = (self.release / 'public').resolve()
        swapped = dict(self.receipt, build_manifest_sha256='d' * 64)
        with mock.patch.object(rollout, 'verify_frontend', side_effect=[(public, self.receipt), (public, swapped)]):
            with self.assertRaisesRegex(rollout.RolloutError, 'frontend_release_changed'):
                self.call(False)

        self.api.alias = [{'type': 'CNAME', 'name': rollout.ALIAS, 'content': rollout.CANONICAL, 'proxied': True, 'id': 'd' * 32}]
        self.api.writes.clear()

        def drop_alias_on_deploy(command, **options):
            result = self.run_command(command, **options)
            if command[-1] != '--version' and '--dry-run' not in command:
                self.api.alias = []
            return result

        self.assertEqual(
            rollout.rollout(self.config, self.release, False, deploy_root=self.deploy, api=self.api, run=drop_alias_on_deploy),
            {'status': 'deployed'},
        )
        self.assertEqual(len(self.api.writes), 1)
        self.assertEqual(self.api.writes[0][1], f"/zones/{self.config['CLOUDFLARE_ZONE_ID']}/dns_records")

        with mock.patch.object(rollout, 'preflight', side_effect=rollout.RolloutError('preflight_boom')), \
                mock.patch.object(rollout, 'write_log', side_effect=OSError('disk full')):
            with self.assertRaisesRegex(rollout.RolloutError, 'preflight_boom'):
                self.call(False)

    def test_configurable_hosts_binding_verification_and_work_dir_pruning(self):
        # 1) Custom canonical/redirect hosts propagate through settings, wrangler.json, DNS, and complete=True preflight.
        custom_cfg = dict(
            self.config,
            CLOUDFLARE_CANONICAL_HOST='metrics.tor.example',
            CLOUDFLARE_REDIRECT_HOST='dos.metrics.tor.example',
            STATE_RETAIN_RUNS='2',
        )
        self.published(custom_cfg)
        custom_api = FakeAPI(custom_cfg)
        self.api = custom_api
        self.assertEqual(
            rollout.rollout(custom_cfg, self.release, False, deploy_root=self.deploy, api=custom_api, run=self.run_command),
            {'status': 'deployed'},
        )
        work = next(self.state.glob('cloudflare-*'))
        cfg = json.loads((work / 'wrangler.json').read_text())
        self.assertEqual(
            [r['pattern'] for r in cfg['routes']],
            ['metrics.tor.example/onionsurge*', 'dos.metrics.tor.example/*'],
        )
        self.assertEqual(cfg['vars']['CANONICAL_HOST'], 'metrics.tor.example')
        self.assertEqual(cfg['vars']['REDIRECT_HOST'], 'dos.metrics.tor.example')
        self.assertEqual(
            custom_api.writes[0][2],
            {'type': 'CNAME', 'name': 'dos.metrics.tor.example', 'content': 'metrics.tor.example', 'proxied': True, 'ttl': 1},
        )
        # Blank CLOUDFLARE_REDIRECT_HOST derives onionsurge.<canonical_host>.
        derived = rollout.settings(dict(self.config, CLOUDFLARE_CANONICAL_HOST='tor.example', CLOUDFLARE_REDIRECT_HOST=''), self.deploy)
        self.assertEqual((derived['canonical'], derived['alias']), ('tor.example', 'onionsurge.tor.example'))

        # Wrong CANONICAL_HOST / REDIRECT_HOST Worker bindings fail complete=True preflight.
        values = rollout.settings(custom_cfg, self.deploy)
        for bad_var in ('CANONICAL_HOST', 'REDIRECT_HOST'):
            mutated = [
                {**b, 'text': 'wrong.example'} if b.get('name') == bad_var else b
                for b in custom_api.bindings
            ]
            saved = custom_api.bindings
            custom_api.bindings = mutated
            with self.subTest(bad_var=bad_var), self.assertRaisesRegex(rollout.RolloutError, 'worker_binding_mismatch'):
                rollout.preflight(custom_api, values, complete=True)
            custom_api.bindings = saved

        # Invalid hostnames or cross-zone redirect aliases fail closed.
        for patch in (
            {'CLOUDFLARE_CANONICAL_HOST': 'localhost'},
            {'CLOUDFLARE_CANONICAL_HOST': 'Bad.Example.com'},
            {'CLOUDFLARE_CANONICAL_HOST': '-bad.example.com'},
            {'CLOUDFLARE_CANONICAL_HOST': 'metrics.tor.example', 'CLOUDFLARE_REDIRECT_HOST': 'metrics.tor.example'},
            {'CLOUDFLARE_CANONICAL_HOST': 'metrics.tor.example', 'CLOUDFLARE_REDIRECT_HOST': 'other.example.net'},
        ):
            with self.subTest(patch=patch), self.assertRaisesRegex(rollout.RolloutError, 'invalid_cloudflare_host'):
                rollout.settings({**self.config, **patch}, self.deploy)

        for bad_cfg in ({k: v for k, v in self.config.items() if k != 'DO_ENABLED'},
                        dict(self.config, DO_ENABLED='false')):
            with self.subTest(do_enabled=bad_cfg.get('DO_ENABLED')), \
                    self.assertRaisesRegex(rollout.RolloutError, 'spaces_primary_required'):
                rollout.settings(bad_cfg, self.deploy)

        # 2) Completed cloudflare-* directories beyond STATE_RETAIN_RUNS=2 are pruned; incomplete dirs remain untouched.
        incomplete_dir = self.state / 'cloudflare-incomplete'
        incomplete_dir.mkdir(mode=0o700)
        for idx in range(3):
            self.assertEqual(
                rollout.rollout(custom_cfg, self.release, False, deploy_root=self.deploy, api=custom_api, run=self.run_command),
                {'status': 'deployed'},
            )
        completed_dirs = [p for p in self.state.glob('cloudflare-*') if (p / 'completed.json').exists()]
        self.assertEqual(len(completed_dirs), 2)
        self.assertTrue(incomplete_dir.is_dir())


if __name__ == '__main__':
    unittest.main()
