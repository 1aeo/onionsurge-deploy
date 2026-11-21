from concurrent.futures import ThreadPoolExecutor
from contextlib import ExitStack, contextmanager, redirect_stdout
import fcntl
import io
import json
import os
from pathlib import Path
import stat
import subprocess
import sys
import tempfile
import time
from types import SimpleNamespace
import unittest
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent))
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'scripts'))
import run_hourly as hourly
from test_publication import END, MemoryProvider, fixture

RELEASE = '20261118T040000Z-' + 'a' * 12


class RunnerTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name).resolve()
        self.app = self.root / 'application'; self.app.mkdir()
        self.deploy = self.root / 'deployment'; self.deploy.mkdir()
        self.state = self.root / 'state'
        self.source_config = self.root / 'source.env'
        self.source_config.write_text('COLLECTOR_MODE="private"\n')
        self.source_config.chmod(0o600)
        self.config = {'APPLICATION_DIR': str(self.app), 'STATE_DIR': str(self.state),
                       'LOG_DIR': str(self.root / 'logs'), 'APPLICATION_COMMIT': 'a' * 40,
                       'DEPLOYMENT_COMMIT': 'b' * 40, 'SOURCE_CONFIG': str(self.source_config)}

    @staticmethod
    def _overlap_paths(command):
        return [command[i + 1] for i, token in enumerate(command) if token == '--forbid-overlap']

    def pipeline(self):
        stack = ExitStack()
        self.addCleanup(stack.close)
        self.checkout = stack.enter_context(mock.patch.object(hourly, 'verify_checkout', side_effect=lambda path, name, pin: pin))
        self.plan_calls = []
        self.collect_calls = []
        self.checkpoint_calls = []
        self.build_calls = []
        self.subprocess_order = []
        self.plan_response = None
        self.plan_side_effect = None
        self.collect_response = SimpleNamespace(
            returncode=0,
            stdout=json.dumps({'snapshot_end': 1794960000, 'charts': 13, 'series': 42, 'source_requests': 61,
                               'cache_committed': True, 'checkpoint_updated': True}) + '\n',
            stderr='',
        )
        self.collect_side_effect = None
        self.checkpoint_response = None
        self.build_response = SimpleNamespace(returncode=0, stdout='', stderr='')

        def run_subprocess(command, **kwargs):
            name = Path(command[2]).name
            if name == 'collect.py':
                if '--plan' in command:
                    self.subprocess_order.append('plan')
                    self.plan_calls.append((command, kwargs))
                    if self.plan_side_effect is not None:
                        raise self.plan_side_effect
                    if self.plan_response is not None:
                        return self.plan_response
                    end_val = int(command[command.index('--end') + 1])
                    return SimpleNamespace(
                        returncode=0,
                        stdout=json.dumps({'snapshot_end': end_val, 'catchup_days': 1,
                                           'recommended_timeout_seconds': 10290}) + '\n',
                        stderr='',
                    )
                if '--record-checkpoint' in command:
                    self.subprocess_order.append('record-checkpoint')
                    self.checkpoint_calls.append((command, kwargs))
                    if self.checkpoint_response is not None:
                        return self.checkpoint_response
                    end_val = int(command[command.index('--end') + 1])
                    return SimpleNamespace(
                        returncode=0,
                        stdout=json.dumps({'status': 'checkpoint_recorded', 'observation_end': end_val}) + '\n',
                        stderr='',
                    )
                self.subprocess_order.append('collect')
                self.collect_calls.append((command, kwargs))
                if self.collect_side_effect is not None:
                    raise self.collect_side_effect
                return self.collect_response
            if name == 'build.py':
                self.subprocess_order.append('build')
                self.build_calls.append((command, kwargs))
                return self.build_response
            raise AssertionError('Unexpected subprocess command: ' + repr(command))

        def run_bounded(command, **kwargs):
            result = run_subprocess(command, **kwargs)
            stdout = result.stdout
            hourly.require(
                result.returncode == 0 and isinstance(stdout, str)
                and len(stdout.encode('utf-8')) <= hourly.MAX_CHILD_STDOUT,
                kwargs['error_message'],
            )
            return stdout

        self.subprocess_run = stack.enter_context(mock.patch.object(hourly.subprocess, 'run', side_effect=run_subprocess))
        self.bounded_stdout = stack.enter_context(mock.patch.object(hourly, '_run_bounded_stdout', side_effect=run_bounded))
        self.prepare = stack.enter_context(mock.patch.object(hourly.publication, 'prepare_candidate', return_value=object()))
        self.providers = stack.enter_context(mock.patch.object(hourly.publication, 'providers_from_config', return_value=[object()]))
        self.publish = stack.enter_context(mock.patch.object(hourly.publication, 'publish_candidate',
            return_value={'status': 'published', 'release_id': RELEASE}))

    def test_success_uses_unique_private_run_pinned_live_build_and_public_safe_result(self):
        self.pipeline()
        result = hourly.run(self.config, 1794960000, self.deploy)
        self.assertEqual(result, {'status': 'published', 'release_id': RELEASE})
        self.assertNotIn(str(self.root), json.dumps(result))
        self.assertEqual(self.checkout.call_count, 4)
        self.assertEqual(self.subprocess_order, ['plan', 'collect', 'build'])

        plan_cmd, plan_kwargs = self.plan_calls[0]
        self.assertEqual(plan_cmd[:5], [sys.executable, '-B', str(self.app / 'scripts/collect.py'), '--plan', '--config'])
        self.assertEqual(plan_cmd[plan_cmd.index('--application-dir') + 1], str(self.app))
        self.assertEqual(plan_cmd[plan_cmd.index('--state-dir') + 1], str(self.state / 'collector'))
        self.assertEqual(plan_cmd[plan_cmd.index('--end') + 1], '1794960000')
        self.assertEqual(plan_cmd[plan_cmd.index('--max-catchup-days') + 1], '31')
        self.assertNotIn('--previous-end', plan_cmd)
        self.assertEqual(plan_kwargs['cwd'], self.deploy)
        overlaps = self._overlap_paths(plan_cmd)
        self.assertIn(str(self.app), overlaps)
        self.assertIn(str(self.deploy), overlaps)
        self.assertIn(str(self.root / 'logs'), overlaps)
        self.assertIn(str(self.state / 'runs'), overlaps)
        self.assertIn(str(self.state / 'publication.lock'), overlaps)
        self.assertIn(str(self.state / 'schedule.lock'), overlaps)
        self.assertIn(str(self.state / 'crontab-before.txt'), overlaps)
        self.assertIn(str(self.state / 'collector-checkpoint.json'), overlaps)
        self.assertNotIn(str(self.source_config), overlaps)
        self.assertNotIn(str(self.state / 'collector'), overlaps)

        collect_cmd, collect_kwargs = self.collect_calls[0]
        self.assertEqual(collect_cmd[:4], [sys.executable, '-B', str(self.app / 'scripts/collect.py'), '--config'])
        self.assertEqual(collect_cmd[4], str(self.source_config))
        self.assertEqual(collect_cmd[collect_cmd.index('--state-dir') + 1], str(self.state / 'collector'))
        run_input = Path(collect_cmd[collect_cmd.index('--output') + 1])
        self.assertEqual(run_input.name, 'input.json')
        self.assertEqual(run_input.parent.parent, self.state / 'runs')
        self.assertEqual(collect_cmd[collect_cmd.index('--end') + 1], '1794960000')
        self.assertEqual(self._overlap_paths(collect_cmd), overlaps)
        self.assertEqual(collect_kwargs['timeout'], 10290)
        self.assertEqual(collect_kwargs['cwd'], self.deploy)

        build_cmd, build_kwargs = self.build_calls[0]
        self.assertIn('--live', build_cmd)
        self.assertEqual(build_cmd[build_cmd.index('--input') + 1], str(run_input))
        self.assertEqual(build_cmd[build_cmd.index('--cache') + 1], str(self.state / 'reference-cache'))
        self.assertEqual(build_kwargs['cwd'], self.deploy)

        self.assertEqual(self.prepare.call_args.kwargs,
            {'application_commit': 'a' * 40, 'deployment_commit': 'b' * 40})
        receipt = json.loads((self.state / 'last-success.json').read_text())
        self.assertEqual(receipt['release_id'], RELEASE)
        self.assertEqual(receipt['observation_end'], 1794960000)
        for path in [self.state, run_input.parent]:
            self.assertEqual(stat.S_IMODE(path.stat().st_mode), 0o700)
        for path in [self.state / 'last-run.json', self.state / 'last-success.json', self.root / 'logs/hourly.log']:
            self.assertEqual(stat.S_IMODE(path.stat().st_mode), 0o600)
        self.assertFalse((self.state / 'collector-checkpoint.json').exists())
        first = run_input.parent
        hourly.run(self.config, 1794960000, self.deploy)
        second_input = Path(self.collect_calls[1][0][self.collect_calls[1][0].index('--output') + 1])
        self.assertNotEqual(first, second_input.parent)

    def published_at_midnight(self, schema=7, observation_end=1794960000, include_24h=True):
        hourly.private_directory(self.state)
        objects = {'data.json': {'sha256': 'd' * 64, 'bytes': 10}}
        if include_24h:
            objects['exports/metric0-24h.csv'] = {'sha256': 'e' * 64, 'bytes': 10}
        manifest = {
            'format': 'onionsurge-release-v1',
            'id': RELEASE,
            'data_schema_version': schema,
            'observation_end': observation_end,
            'generated_at': observation_end + 100,
            'application_commit': 'a' * 40,
            'deployment_commit': 'b' * 40,
            'query_contract_sha256': 'c' * 64,
            'objects': objects,
        }
        raw_manifest = hourly.json_bytes(manifest)
        cand_dir = self.state / 'candidates' / RELEASE
        cand_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
        hourly.atomic_private(cand_dir / 'manifest.json', raw_manifest)
        desc = hourly.publication.descriptor(raw_manifest, RELEASE)
        published = {'destinations': [hourly.publication.sha256(b'do')], 'root': {
            'format': 'onionsurge-root-v1', 'updated_at': 1794960100, 'current': desc, 'previous': None}}
        hourly.atomic_private(self.state / 'published.json', hourly.json_bytes(published))

    def test_non_hourly_end_rejected_and_same_utc_hour_skips_cleanly(self):
        self.pipeline()
        with self.assertRaises(hourly.HourlyError):
            hourly.run(self.config, 1794961800, self.deploy)
        self.assertEqual(self.plan_calls, [])
        self.assertEqual(self.collect_calls, [])
        non_midnight_hour = 1794970800
        self.published_at_midnight(schema=7, observation_end=non_midnight_hour)
        result = hourly.run(self.config, non_midnight_hour, self.deploy)
        self.assertEqual(result, {'status': 'up_to_date', 'release_id': RELEASE, 'observation_end': non_midnight_hour})
        self.assertEqual(self.plan_calls, [])
        self.assertEqual(self.collect_calls, [])
        self.assertEqual(hourly.health(self.config)['last_run_status'], 'up_to_date')

        # Schema-7 release without -24h.csv (custom daily-only time_ranges) also short-circuits up_to_date cleanly.
        self.published_at_midnight(schema=7, observation_end=non_midnight_hour, include_24h=False)
        result_daily_v7 = hourly.run(self.config, non_midnight_hour, self.deploy)
        self.assertEqual(result_daily_v7, {'status': 'up_to_date', 'release_id': RELEASE, 'observation_end': non_midnight_hour})
        self.assertEqual(self.plan_calls, [])
        self.assertEqual(self.collect_calls, [])

    def test_previous_v6_midnight_release_does_not_skip_schema_migration(self):
        self.pipeline()
        self.published_at_midnight(schema=6, include_24h=False)
        result = hourly.run(self.config, 1794960000, self.deploy)
        self.assertEqual(result, {'status': 'published', 'release_id': RELEASE})
        self.assertEqual(len(self.plan_calls), 1)
        self.assertEqual(len(self.collect_calls), 1)
        plan_cmd = self.plan_calls[0][0]
        self.assertEqual(plan_cmd[plan_cmd.index('--previous-end') + 1], '1794960000')
        self.assertEqual(hourly.health(self.config)['last_run_status'], 'published')

    def test_busy_run_does_not_collect_or_replace_health(self):
        self.pipeline()
        with hourly.private_lock(self.state, 'run.lock'):
            self.assertEqual(hourly.run(self.config, deploy_dir=self.deploy), {'status': 'busy'})
            self.assertTrue(hourly.health(self.config)['run_busy'])
        self.assertEqual(self.plan_calls, [])
        self.assertEqual(self.collect_calls, [])
        self.assertFalse((self.state / 'last-run.json').exists())

    def test_plan_collection_or_build_failure_never_publishes_and_logs_no_raw_error(self):
        self.pipeline()
        bad_plans = [
            SimpleNamespace(returncode=1, stdout='PLAN_SECRET_STDOUT\n', stderr='PLAN_SECRET_STDERR\n'),
            SimpleNamespace(returncode=0, stdout='not-json', stderr=''),
            SimpleNamespace(returncode=0, stdout=json.dumps({'snapshot_end': 1794873600, 'catchup_days': 1,
                                                             'recommended_timeout_seconds': 100}), stderr=''),
            SimpleNamespace(returncode=0, stdout=json.dumps({'snapshot_end': 1794960000, 'catchup_days': 32,
                                                             'recommended_timeout_seconds': 100}), stderr=''),
            SimpleNamespace(returncode=0, stdout=json.dumps({'snapshot_end': 1794960000, 'catchup_days': True,
                                                             'recommended_timeout_seconds': 100}), stderr=''),
            SimpleNamespace(returncode=0, stdout=json.dumps({'snapshot_end': 1794960000, 'catchup_days': 1,
                                                             'recommended_timeout_seconds': 0}), stderr=''),
        ]
        for bad_plan in bad_plans:
            self.plan_response = bad_plan
            self.collect_calls.clear()
            with self.subTest(bad_plan=bad_plan), self.assertRaisesRegex(hourly.HourlyError, '^Hourly cycle failed; inspect private state$'):
                hourly.run(self.config, 1794960000, self.deploy)
            self.assertEqual(self.collect_calls, [])
            self.assertEqual(json.loads((self.state / 'last-run.json').read_text())['stage'], 'plan')
            log_text = (self.root / 'logs/hourly.log').read_text()
            self.assertNotIn('PLAN_SECRET_STDOUT', log_text)
            self.assertNotIn('PLAN_SECRET_STDERR', log_text)
        self.plan_response = None

        self.collect_side_effect = ValueError('PRIVATE_ERROR_MARKER')
        with self.assertRaisesRegex(hourly.HourlyError, '^Hourly cycle failed; inspect private state$'):
            hourly.run(self.config, 1794960000, self.deploy)
        self.publish.assert_not_called()
        self.assertEqual(json.loads((self.state / 'last-run.json').read_text())['stage'], 'collect')
        self.assertNotIn('PRIVATE_ERROR_MARKER', (self.root / 'logs/hourly.log').read_text())
        self.collect_side_effect = None

        bad_summaries = [
            SimpleNamespace(returncode=1, stdout='PRIVATE_STDOUT_SECRET\n', stderr='PRIVATE_STDERR_SECRET\n'),
            SimpleNamespace(returncode=0, stdout='{' + ' ' * 65536 + '}', stderr=''),
            SimpleNamespace(returncode=0, stdout='not-json', stderr='PRIVATE_STDERR_SECRET'),
            SimpleNamespace(returncode=0, stdout=json.dumps({'snapshot_end': 1794960000, 'extra': 1}), stderr=''),
            SimpleNamespace(returncode=0, stdout=json.dumps({'snapshot_end': 1794873600}), stderr=''),
            SimpleNamespace(returncode=0, stdout=json.dumps({'snapshot_end': True}), stderr=''),
            SimpleNamespace(returncode=0, stdout=json.dumps({'snapshot_end': 1794960000, 'cache_committed': 1}), stderr=''),
            SimpleNamespace(returncode=0, stdout=json.dumps({'snapshot_end': 1794960000, 'checkpoint_updated': 1}), stderr=''),
        ]
        for bad_response in bad_summaries:
            self.build_calls.clear()
            self.collect_response = bad_response
            with self.subTest(bad_response=bad_response):
                with self.assertRaisesRegex(hourly.HourlyError, '^Hourly cycle failed; inspect private state$') as raised:
                    hourly.run(self.config, 1794960000, self.deploy)
                self.assertEqual(self.build_calls, [])
                self.publish.assert_not_called()
                self.assertEqual(json.loads((self.state / 'last-run.json').read_text())['stage'], 'collect')
                log_text = (self.root / 'logs/hourly.log').read_text()
                self.assertNotIn('PRIVATE_STDOUT_SECRET', log_text)
                self.assertNotIn('PRIVATE_STDERR_SECRET', log_text)
                self.assertNotIn('PRIVATE_STDOUT_SECRET', str(raised.exception))
                self.assertNotIn('PRIVATE_STDERR_SECRET', str(raised.exception))

        # Stderr from collect.py is ignored cleanly and valid cache_committed / checkpoint_updated booleans succeed.
        self.collect_response = SimpleNamespace(
            returncode=0, stdout=json.dumps({'snapshot_end': 1794960000, 'cache_committed': True,
                                             'checkpoint_updated': False}),
            stderr='Collection warning: collector checkpoint update failed after snapshot publication.\n')
        self.build_response = SimpleNamespace(returncode=1, stdout='', stderr='')
        with self.assertRaises(hourly.HourlyError):
            hourly.run(self.config, 1794960000, self.deploy)
        self.publish.assert_not_called()
        self.assertEqual(json.loads((self.state / 'last-run.json').read_text())['stage'], 'build')

    def test_pending_promotion_and_changed_checkout_stop_the_pipeline(self):
        self.pipeline()
        self.state.mkdir(mode=0o700)
        (self.state / 'pending-promotion.json').write_text('{}')
        with self.assertRaises(hourly.HourlyError):
            hourly.run(self.config, 1794960000, self.deploy)
        self.assertEqual(self.plan_calls, [])
        self.assertEqual(self.collect_calls, [])
        (self.state / 'pending-promotion.json').unlink()
        self.checkout.side_effect = ['a' * 40, 'b' * 40, ValueError('changed')]
        with self.assertRaises(hourly.HourlyError):
            hourly.run(self.config, 1794960000, self.deploy)
        self.publish.assert_not_called()

    def test_post_publication_local_failure_reports_published_warning(self):
        self.pipeline()
        actual = hourly.atomic_private
        def fail_success(path, raw):
            if path.name == 'last-success.json':
                raise OSError('fixture disk failure')
            actual(path, raw)
        with mock.patch.object(hourly, 'atomic_private', side_effect=fail_success):
            result = hourly.run(self.config, 1794960000, self.deploy)
        self.assertEqual(result, {'status': 'published_receipt_warning', 'release_id': RELEASE})
        self.assertEqual(hourly.health(self.config)['last_run_status'], 'published_receipt_warning')

    def test_fixed_publisher_warning_survives_receipt_cli_and_health(self):
        self.pipeline()
        code = 'pending_marker_removal_not_durable'
        self.publish.return_value.update(warning=code)
        result = hourly.run(self.config, 1794960000, self.deploy)
        self.assertEqual(result, {'status': 'published_receipt_warning', 'release_id': RELEASE, 'warning': code})
        for name in ('last-run.json', 'last-success.json'):
            receipt = json.loads((self.state / name).read_bytes())
            self.assertEqual(receipt['publication_warning'], code)
        self.assertEqual(hourly.health(self.config)['publication_warning'], code)
        self.assertIn(code, (self.root / 'logs/hourly.log').read_text())

    def test_log_close_failure_does_not_relabel_confirmed_publication_as_failure(self):
        self.pipeline()
        original = hourly.private_log
        @contextmanager
        def failing_close(path):
            with original(path) as stream:
                yield stream
            raise OSError('fixture close failure')
        with mock.patch.object(hourly, 'private_log', side_effect=failing_close):
            result = hourly.run(self.config, 1794960000, self.deploy)
        self.assertEqual(result, {'status': 'published_receipt_warning', 'release_id': RELEASE})

    def test_runtime_paths_do_not_write_into_source_and_health_does_not_echo_arbitrary_values(self):
        for field in ['STATE_DIR', 'LOG_DIR']:
            config = dict(self.config, **{field: str(self.app / 'private')})
            with self.subTest(field=field), self.assertRaises(hourly.HourlyError):
                hourly.run(config, 1794960000, self.deploy)
        self.assertFalse((self.app / 'private').exists())
        self.state.mkdir(mode=0o700)
        (self.state / 'last-run.json').write_text('{"status":"PRIVATE_MARKER","publication_warning":"PRIVATE_MARKER"}')
        (self.state / 'last-success.json').write_text('{"release_id":"PRIVATE_MARKER"}')
        self.assertNotIn('PRIVATE_MARKER', json.dumps(hourly.health(self.config)))

    def test_source_config_permissions_symlink_overlap_and_opaque_contents(self):
        self.pipeline()
        # Opaque non-env content in SOURCE_CONFIG is never parsed by Layer 3.
        self.source_config.write_text('not parsed = by deploy {{{}\n# arbitrary operator data\n')
        self.source_config.chmod(0o600)
        self.assertEqual(hourly.run(self.config, 1794960000, self.deploy)['status'], 'published')

        self.source_config.chmod(0o640)
        with self.assertRaisesRegex(hourly.HourlyError, 'mode 0600'):
            hourly.run(self.config, 1794960000, self.deploy)
        self.source_config.chmod(0o600)

        link = self.root / 'source-link.env'
        link.symlink_to(self.source_config)
        with self.assertRaises(OSError):
            hourly.run(dict(self.config, SOURCE_CONFIG=str(link)), 1794960000, self.deploy)

        for checkout_dir in (self.app, self.deploy):
            inside = checkout_dir / 'source.env'
            inside.write_text('COLLECTOR_MODE="private"\n')
            inside.chmod(0o600)
            with self.subTest(checkout=checkout_dir.name), self.assertRaisesRegex(hourly.HourlyError, 'outside'):
                hourly.run(dict(self.config, SOURCE_CONFIG=str(inside)), 1794960000, self.deploy)

        bad_state_src = self.state / 'runs/source.env'
        bad_state_src.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        bad_state_src.write_text('COLLECTOR_MODE="private"\n')
        bad_state_src.chmod(0o600)
        with self.assertRaisesRegex(hourly.HourlyError, 'overlap'):
            hourly.run(dict(self.config, SOURCE_CONFIG=str(bad_state_src)), 1794960000, self.deploy)

        config_file = self.root / 'config.env'
        config_file.write_text(
            f'APPLICATION_DIR="{self.app}"\nSTATE_DIR="{self.state}"\n'
            f'APPLICATION_COMMIT="{"a" * 40}"\nDEPLOYMENT_COMMIT="{"b" * 40}"\n')
        config_file.chmod(0o600)
        with self.assertRaises(SystemExit) as raised:
            hourly.main(['--config', str(config_file)])
        self.assertNotEqual(raised.exception.code, 0)
        self.assertEqual(len(self.collect_calls), 1)

    def test_plan_forwards_previous_end_and_timeout_to_collection_and_rejects_backwards_end(self):
        self.pipeline()
        target_end = 1794960000
        self.published_at_midnight(schema=7, observation_end=target_end - 11 * 86400)
        self.plan_response = SimpleNamespace(
            returncode=0,
            stdout=json.dumps({'snapshot_end': target_end, 'catchup_days': 11,
                               'recommended_timeout_seconds': 24680}) + '\n',
            stderr='',
        )
        result = hourly.run(self.config, target_end, self.deploy)
        self.assertEqual(result, {'status': 'published', 'release_id': RELEASE})
        plan_cmd = self.plan_calls[0][0]
        self.assertEqual(plan_cmd[plan_cmd.index('--previous-end') + 1], str(target_end - 11 * 86400))
        self.assertEqual(self.collect_calls[0][1]['timeout'], 24680)

        # target_end earlier than previous observation_end fails closed before --plan or collect.py.
        self.plan_calls.clear()
        self.collect_calls.clear()
        self.published_at_midnight(schema=6, observation_end=target_end + 86400, include_24h=False)
        with self.assertRaisesRegex(hourly.HourlyError, '^Hourly cycle failed; inspect private state$'):
            hourly.run(self.config, target_end, self.deploy)
        self.assertEqual(self.plan_calls, [])
        self.assertEqual(self.collect_calls, [])

    def test_record_checkpoint_delegates_to_collector_and_enforces_prerequisites(self):
        self.pipeline()
        target_end = 1794960000
        manual_end = target_end - 86400
        hourly.private_directory(self.state)

        with self.assertRaisesRegex(hourly.HourlyError, 'schema-7'):
            hourly.record_checkpoint(self.config, deploy_dir=self.deploy)
        self.assertEqual(self.checkpoint_calls, [])

        self.published_at_midnight(schema=6, observation_end=manual_end, include_24h=False)
        with self.assertRaisesRegex(hourly.HourlyError, 'schema-7'):
            hourly.record_checkpoint(self.config, deploy_dir=self.deploy)
        self.assertEqual(self.checkpoint_calls, [])

        published_link = self.state / 'published.json'
        published_link.unlink()
        published_link.symlink_to(self.root / 'missing-published.json')
        with self.assertRaises(OSError):
            hourly.record_checkpoint(self.config, deploy_dir=self.deploy)
        published_link.unlink()

        self.published_at_midnight(schema=7, observation_end=manual_end)
        (self.state / 'pending-promotion.json').write_text('{}')
        with self.assertRaisesRegex(hourly.HourlyError, 'Pending publication'):
            hourly.record_checkpoint(self.config, deploy_dir=self.deploy)
        (self.state / 'pending-promotion.json').unlink()

        with mock.patch.object(hourly, 'verify_checkout', side_effect=ValueError('changed')):
            with self.assertRaises(ValueError):
                hourly.record_checkpoint(self.config, deploy_dir=self.deploy)

        with self.assertRaisesRegex(hourly.HourlyError, 'Invalid collection end'):
            hourly.record_checkpoint(self.config, manual_end - 86400, self.deploy)
        self.assertEqual(self.checkpoint_calls, [])

        with hourly.private_lock(self.state, 'run.lock'):
            self.assertEqual(hourly.record_checkpoint(self.config, deploy_dir=self.deploy), {'status': 'busy'})
        self.assertEqual(self.checkpoint_calls, [])

        config_file = self.root / 'config.env'
        config_file.write_text(''.join(f'{key}="{value}"\n' for key, value in self.config.items()))
        config_file.chmod(0o600)
        stdout = io.StringIO()
        with mock.patch.object(hourly, 'ROOT', self.deploy), redirect_stdout(stdout):
            hourly.main(['--config', str(config_file), '--record-checkpoint'])
        self.assertEqual(json.loads(stdout.getvalue()), {'status': 'checkpoint_recorded', 'observation_end': manual_end})
        self.assertEqual(self.collect_calls, [])
        ckpt_cmd = self.checkpoint_calls[0][0]
        self.assertIn('--record-checkpoint', ckpt_cmd)
        self.assertEqual(ckpt_cmd[ckpt_cmd.index('--application-dir') + 1], str(self.app))
        self.assertEqual(ckpt_cmd[ckpt_cmd.index('--state-dir') + 1], str(self.state / 'collector'))
        self.assertEqual(ckpt_cmd[ckpt_cmd.index('--end') + 1], str(manual_end))
        self.assertIn(str(self.app), self._overlap_paths(ckpt_cmd))

        for bad_ckpt in (
            SimpleNamespace(returncode=1, stdout='', stderr='err'),
            SimpleNamespace(returncode=0, stdout='not-json', stderr=''),
            SimpleNamespace(returncode=0, stdout=json.dumps({'status': 'other', 'observation_end': manual_end}), stderr=''),
            SimpleNamespace(returncode=0, stdout=json.dumps({'status': 'checkpoint_recorded', 'observation_end': target_end}), stderr=''),
        ):
            self.checkpoint_response = bad_ckpt
            with self.subTest(bad_ckpt=bad_ckpt), self.assertRaisesRegex(hourly.HourlyError, 'Checkpoint recording failed'):
                hourly.record_checkpoint(self.config, manual_end, self.deploy)

    def test_health_uses_shared_lock_and_dangling_symlinks_fail_closed(self):
        self.pipeline()
        hourly.private_directory(self.state)
        lock_path = self.state / 'run.lock'
        lock_path.touch(mode=0o600)

        calls = []
        real_flock = fcntl.flock
        def record_flock(fd, op):
            calls.append(op)
            return real_flock(fd, op)

        holder = os.open(lock_path, os.O_RDONLY)
        try:
            real_flock(holder, fcntl.LOCK_SH | fcntl.LOCK_NB)
            with mock.patch.object(fcntl, 'flock', side_effect=record_flock):
                with ThreadPoolExecutor(max_workers=4) as pool:
                    results = list(pool.map(lambda _: hourly.health(self.config), range(4)))
            self.assertTrue(all(item['run_busy'] is False for item in results))
            self.assertIn(fcntl.LOCK_SH | fcntl.LOCK_NB, calls)
            self.assertIn(fcntl.LOCK_UN, calls)
            self.assertNotIn(fcntl.LOCK_EX | fcntl.LOCK_NB, calls)
        finally:
            real_flock(holder, fcntl.LOCK_UN)
            os.close(holder)
        self.assertEqual(hourly.run(self.config, 1794960000, self.deploy)['status'], 'published')

        for filename in ('run.lock', 'last-run.json', 'last-success.json'):
            path = self.state / filename
            path.unlink(missing_ok=True)
            path.symlink_to(self.root / ('missing-' + filename))
            with self.subTest(dangling=filename), self.assertRaises(OSError):
                hourly.health(self.config)
            path.unlink()

        pending = self.state / 'pending-promotion.json'
        pending.symlink_to(self.root / 'missing-pending.json')
        self.assertTrue(hourly.health(self.config)['pending_promotion'])
        with self.assertRaises(hourly.HourlyError):
            hourly.run(self.config, 1794960000, self.deploy)
        with self.assertRaisesRegex(hourly.HourlyError, 'Pending publication'):
            hourly.record_checkpoint(self.config, deploy_dir=self.deploy)
        pending.unlink()

    def test_directory_run_lock_and_restrictive_umask_before_lock(self):
        self.pipeline()
        hourly.private_directory(self.state)
        (self.state / 'run.lock').mkdir(mode=0o700)
        probe_before = os.open(os.devnull, os.O_RDONLY)
        os.close(probe_before)
        with self.assertRaisesRegex(hourly.HourlyError, 'Invalid run lock'):
            hourly.health(self.config)
        with self.assertRaisesRegex(hourly.HourlyError, 'Invalid private lock file'):
            hourly.run(self.config, 1794960000, self.deploy)
        probe_after = os.open(os.devnull, os.O_RDONLY)
        os.close(probe_after)
        self.assertEqual(probe_after, probe_before)
        (self.state / 'run.lock').rmdir()

        fresh_state = self.root / 'fresh-parent' / 'state'
        fresh_cfg = dict(self.config, STATE_DIR=str(fresh_state))
        prior_umask = os.umask(0o277)
        try:
            result = hourly.run(fresh_cfg, 1794960000, self.deploy)
            self.assertEqual(result['status'], 'published')
            restored = os.umask(0o277)
            self.assertEqual(restored, 0o277)
        finally:
            os.umask(prior_umask)
        self.assertEqual(stat.S_IMODE(fresh_state.stat().st_mode), 0o700)
        self.assertEqual(stat.S_IMODE((fresh_state / 'run.lock').stat().st_mode), 0o600)

    def test_local_only_provider_validates_site_dir_isolation_and_publishes(self):
        local_site = self.root / 'local-site'
        cfg = {
            **self.config,
            'DO_ENABLED': 'false',
            'R2_ENABLED': 'false',
            'LOCAL_ENABLED': 'true',
            'LOCAL_SITE_DIR': str(local_site),
        }
        plan_commands = []

        def fake_subprocess(command, **_kwargs):
            name = Path(command[2]).name
            if name == 'collect.py':
                if '--plan' in command:
                    plan_commands.append(command)
                    return SimpleNamespace(
                        returncode=0,
                        stdout=json.dumps({'snapshot_end': END, 'catchup_days': 1,
                                           'recommended_timeout_seconds': 10290}) + '\n',
                        stderr='',
                    )
                return SimpleNamespace(
                    returncode=0,
                    stdout=json.dumps({'snapshot_end': END, 'charts': 13, 'series': 42, 'source_requests': 61,
                                       'cache_committed': True}) + '\n',
                    stderr='',
                )
            if name == 'build.py':
                out_dir = Path(command[command.index('--output') + 1])
                fixture(out_dir, end=END)
                return SimpleNamespace(returncode=0, stdout='', stderr='')
            raise AssertionError('Unexpected subprocess command: ' + repr(command))

        with mock.patch.object(hourly, 'verify_checkout', side_effect=lambda path, name, pin: pin):
            with mock.patch.object(hourly.subprocess, 'run', side_effect=fake_subprocess), \
                    mock.patch.object(hourly, '_run_bounded_stdout',
                                      side_effect=lambda command, **kwargs: fake_subprocess(command, **kwargs).stdout):
                result = hourly.run(cfg, END, self.deploy)

        self.assertEqual(result['status'], 'published')
        self.assertIn(str(local_site), self._overlap_paths(plan_commands[0]))
        manifest_path = local_site / 'data/manifest.json'
        self.assertTrue(manifest_path.is_file())
        root_manifest = json.loads(manifest_path.read_text())
        self.assertEqual(root_manifest['current']['id'], result['release_id'])

        inside_source = local_site / 'source.env'
        inside_source.write_text('COLLECTOR_MODE="private"\n')
        inside_source.chmod(0o600)
        with self.assertRaises(ValueError):
            hourly.run(dict(cfg, SOURCE_CONFIG=str(inside_source)), END, self.deploy)

        for overlap in (self.app / 'sub', self.deploy / 'sub', self.state / 'sub'):
            bad = dict(cfg, LOCAL_SITE_DIR=str(overlap))
            with self.assertRaises(ValueError):
                hourly.run(bad, END, self.deploy)

    def test_time_ranges_config_validation_and_passthrough(self):
        self.pipeline()
        res_without = hourly.run(self.config, END, self.deploy)
        self.assertEqual(res_without['status'], 'published')
        self.assertNotIn('--time-ranges', self.build_calls[0][0])

        res_blank = hourly.run(dict(self.config, TIME_RANGES_CONFIG='   '), END, self.deploy)
        self.assertEqual(res_blank['status'], 'published')
        self.assertNotIn('--time-ranges', self.build_calls[1][0])

        tr_file = self.root / 'time-ranges.json'
        tr_file.write_text('{"version":1}\n')
        tr_file.chmod(0o644)
        res_with = hourly.run(dict(self.config, TIME_RANGES_CONFIG=str(tr_file)), END, self.deploy)
        self.assertEqual(res_with['status'], 'published')
        build_cmd = self.build_calls[2][0]
        self.assertIn('--time-ranges', build_cmd)
        self.assertEqual(build_cmd[build_cmd.index('--time-ranges') + 1], str(tr_file.resolve()))
        self.assertIn(str(tr_file.resolve()), self._overlap_paths(self.plan_calls[2][0]))
        self.assertIn(str(tr_file.resolve()), self._overlap_paths(self.collect_calls[2][0]))

        tr_file.chmod(0o664)
        with self.assertRaisesRegex(hourly.HourlyError, 'writable'):
            hourly.run(dict(self.config, TIME_RANGES_CONFIG=str(tr_file)), END + 10800, self.deploy)
        tr_file.chmod(0o644)

        tr_link = self.root / 'time-ranges-link.json'
        tr_link.symlink_to(tr_file)
        with self.assertRaises(OSError):
            hourly.run(dict(self.config, TIME_RANGES_CONFIG=str(tr_link)), END + 10800, self.deploy)

        with self.assertRaises(FileNotFoundError):
            hourly.run(dict(self.config, TIME_RANGES_CONFIG=str(self.root / 'missing-tr.json')), END + 10800, self.deploy)

        with self.assertRaisesRegex(hourly.HourlyError, 'TIME_RANGES_CONFIG'):
            hourly.run(dict(self.config, TIME_RANGES_CONFIG=123), END + 10800, self.deploy)

        with self.assertRaisesRegex(hourly.HourlyError, 'TIME_RANGES_CONFIG'):
            hourly.run(dict(self.config, TIME_RANGES_CONFIG='relative/time-ranges.json'), END + 10800, self.deploy)

        for inside in (self.app / 'tr.json', self.deploy / 'tr.json', self.state / 'runs/tr.json'):
            inside.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
            inside.write_text('{}\n')
            inside.chmod(0o600)
            with self.subTest(inside=inside), self.assertRaises(hourly.HourlyError):
                hourly.run(dict(self.config, TIME_RANGES_CONFIG=str(inside)), END + 10800, self.deploy)

        local_dir = self.root / 'local-tr-overlap'
        local_dir.mkdir()
        tr_in_local = local_dir / 'tr.json'
        tr_in_local.write_text('{}\n')
        tr_in_local.chmod(0o600)
        with self.assertRaisesRegex(hourly.HourlyError, 'LOCAL_SITE_DIR'):
            hourly.run(dict(self.config, DO_ENABLED='false', LOCAL_ENABLED='true',
                            LOCAL_SITE_DIR=str(local_dir), TIME_RANGES_CONFIG=str(tr_in_local)),
                       END + 10800, self.deploy)

    def test_run_bounded_stdout_enforces_limit_timeout_and_discards_stderr(self):
        exact = 'a' * hourly.MAX_CHILD_STDOUT
        ok_code = (
            "import sys; "
            f"sys.stderr.write('x' * 100000); sys.stderr.flush(); "
            f"sys.stdout.write('{exact}'); sys.stdout.flush()"
        )
        out = hourly._run_bounded_stdout(
            [sys.executable, '-c', ok_code], timeout=5, cwd=self.deploy, error_message='Collection failed',
        )
        self.assertEqual(out, exact)

        start = time.monotonic()
        with self.assertRaisesRegex(hourly.HourlyError, 'Collection failed'):
            hourly._run_bounded_stdout(
                [sys.executable, '-c',
                 f"import sys, time; sys.stdout.write('b' * ({hourly.MAX_CHILD_STDOUT} + 10)); sys.stdout.flush(); time.sleep(30)"],
                timeout=10, cwd=self.deploy, error_message='Collection failed',
            )
        self.assertLess(time.monotonic() - start, 5.0)

        start = time.monotonic()
        with self.assertRaises(subprocess.TimeoutExpired):
            hourly._run_bounded_stdout(
                [sys.executable, '-c', 'import time; time.sleep(30)'],
                timeout=0.1, cwd=self.deploy, error_message='Collection failed',
            )
        self.assertLess(time.monotonic() - start, 5.0)

    def test_retain_runs_validation_state_pruning_warning_skip_and_log_rotation(self):
        state = self.state
        state.mkdir(mode=0o700, exist_ok=True)
        logs = self.root / 'logs'
        logs.mkdir(mode=0o700, exist_ok=True)

        # Publish two real prior releases via MemoryProvider before mocking prepare/publish in self.pipeline().
        provider = MemoryProvider('do', [])
        commits = {'application_commit': 'a' * 40, 'deployment_commit': 'b' * 40}
        with hourly.publication.publication_lock(state):
            b_prev = self.root / 'build-prev'
            fixture(b_prev, end=END - 7200)
            c_prev = hourly.publication.prepare_candidate(b_prev, state, **commits, now=END - 7190)
            hourly.publication.publish_candidate(c_prev, state, [provider])
            b_curr = self.root / 'build-curr'
            fixture(b_curr, end=END - 3600)
            c_curr = hourly.publication.prepare_candidate(b_curr, state, **commits, now=END - 3590)
            hourly.publication.publish_candidate(c_curr, state, [provider])
        curr_id, prev_id = c_curr.manifest['id'], c_prev.manifest['id']

        self.pipeline()
        self.providers.return_value = [provider]
        for bad_retain in ('0', '1', '169', 'abc'):
            with self.subTest(bad_retain=bad_retain), self.assertRaisesRegex(hourly.HourlyError, 'invalid_state_retain_runs'):
                hourly.run(dict(self.config, STATE_RETAIN_RUNS=bad_retain), END, self.deploy)

        # Pre-populate logs at >= 10 MiB threshold with existing .1/.2/.3 archives.
        for base_name in ('hourly.log', 'cron.log'):
            for idx in (1, 2, 3):
                archive = logs / f'{base_name}.{idx}'
                archive.write_text(f'archive-{idx}\n', encoding='utf-8')
                archive.chmod(0o600)
            active = logs / base_name
            with open(active, 'wb') as handle:
                handle.truncate(10 * 1024 * 1024)
            active.chmod(0o600)

        # Pre-populate runs (2 old completed + 1 post-build failed run with build/build-manifest.json but no completed.json).
        runs_dir = state / 'runs'
        runs_dir.mkdir(mode=0o700)
        for old_run in ('20261116T000000Z-aaaaaaaa1111', '20261116T010000Z-bbbbbbbb2222'):
            d = runs_dir / old_run
            d.mkdir(mode=0o700)
            (d / 'completed.json').write_text('{"run_id":"' + old_run + '"}\n')
        incomplete_run = runs_dir / '20261116T020000Z-cccccccc3333'
        incomplete_run.mkdir(mode=0o700)
        (incomplete_run / 'input.json').write_text('{}')
        (incomplete_run / 'build').mkdir(mode=0o700)
        (incomplete_run / 'build/build-manifest.json').write_text('{"format":"onionsurge-build-v1"}\n')

        candidates_dir = state / 'candidates'
        old1_id = '20261116T000000Z-111111111111'
        old2_id = '20261116T010000Z-222222222222'
        for cid in (old1_id, old2_id):
            cdir = candidates_dir / cid
            cdir.mkdir(mode=0o700)
            (cdir / 'manifest.json').write_text(json.dumps({'format': 'onionsurge-release-v1', 'id': cid}))

        ref_cache = state / 'reference-cache'
        ref_cache.mkdir(mode=0o700)
        for day_name in ('2026-11-13', '2026-11-14', '2026-11-15', '2026-11-18', 'keep-non-date'):
            d = ref_cache / day_name
            d.mkdir(mode=0o700)
            (d / 'item.json').write_text('{}')

        # 1) Warning in result skips _prune_retained_state completely.
        self.publish.return_value = {
            'status': 'published',
            'release_id': RELEASE,
            'warning': 'pending_marker_removal_not_durable',
        }
        warn_res = hourly.run(dict(self.config, STATE_RETAIN_RUNS='2'), END, self.deploy)
        self.assertEqual(warn_res['warning'], 'pending_marker_removal_not_durable')
        self.assertTrue((candidates_dir / old1_id).exists())
        self.assertTrue((runs_dir / '20261116T000000Z-aaaaaaaa1111').exists())
        self.assertTrue((ref_cache / '2026-11-13').exists())

        # Logs did rotate on start and preserved max 3 archives in mode 0600.
        for base_name in ('hourly.log', 'cron.log'):
            self.assertFalse((logs / f'{base_name}.4').exists())
            for idx in (1, 2, 3):
                p = logs / f'{base_name}.{idx}'
                self.assertTrue(p.is_file())
                self.assertEqual(stat.S_IMODE(p.stat().st_mode), 0o600)
            self.assertEqual((logs / f'{base_name}.2').read_text(), 'archive-1\n')
            self.assertEqual((logs / f'{base_name}.1').stat().st_size, 10 * 1024 * 1024)
            self.assertEqual(stat.S_IMODE((logs / base_name).stat().st_mode), 0o600)
        self.assertEqual((logs / 'cron.log').stat().st_size, 0)
        self.assertLess((logs / 'hourly.log').stat().st_size, 4096)

        # Remove the run created during warn_res? Keep it so now runs has 4 completed + 1 incomplete -> prune keeps 2 newest completed!
        # 2) Clean run without warning prunes runs, candidates, and reference-cache, writes completed.json, and allows retain=168.
        self.publish.return_value = {'status': 'published', 'release_id': RELEASE}
        ok_res = hourly.run(dict(self.config, STATE_RETAIN_RUNS='2'), END, self.deploy)
        self.assertEqual(ok_res, {'status': 'published', 'release_id': RELEASE})

        # candidates: old1 & old2 pruned; curr_id & prev_id kept.
        self.assertEqual({p.name for p in candidates_dir.iterdir()}, {curr_id, prev_id})
        # reference-cache: < 2026-11-15 pruned; 2026-11-15, 2026-11-18, and keep-non-date kept.
        self.assertEqual({p.name for p in ref_cache.iterdir()}, {'2026-11-15', '2026-11-18', 'keep-non-date'})
        # runs: incomplete preserved + exactly 2 completed runs (newest active run + 1 newest completed) with completed.json.
        run_entries = {p.name: p for p in runs_dir.iterdir()}
        self.assertIn('20261116T020000Z-cccccccc3333', run_entries)
        completed_runs = [name for name, path in run_entries.items() if (path / 'completed.json').is_file()]
        self.assertEqual(len(completed_runs), 2)
        newest = max(completed_runs)
        self.assertEqual(json.loads((runs_dir / newest / 'completed.json').read_text())['observation_end'], END)

        # 3) pending-promotion.json appearing before prune check skips candidates/, runs/, and reference-cache/ under lock.
        (candidates_dir / old1_id).mkdir(mode=0o700)
        (candidates_dir / old1_id / 'manifest.json').write_text(
            json.dumps({'format': 'onionsurge-release-v1', 'id': old1_id}))
        stale_ref = ref_cache / '2026-11-12'
        stale_ref.mkdir(mode=0o700)
        def publish_creating_pending(*_args, **_kwargs):
            (state / 'pending-promotion.json').write_text('{}')
            return {'status': 'published', 'release_id': RELEASE}
        self.publish.side_effect = publish_creating_pending
        hourly.run(dict(self.config, STATE_RETAIN_RUNS='2'), END, self.deploy)
        self.assertTrue((candidates_dir / old1_id).exists())
        self.assertTrue(stale_ref.exists())
        self.assertEqual(len([p for p in runs_dir.iterdir() if (p / 'completed.json').is_file()]), 3)
        hourly._prune_retained_state(state, newest, END, 2)
        self.assertTrue((candidates_dir / old1_id).exists())
        self.assertTrue(stale_ref.exists())
        self.assertEqual(len([p for p in runs_dir.iterdir() if (p / 'completed.json').is_file()]), 3)
        (state / 'pending-promotion.json').unlink()


if __name__ == '__main__':
    unittest.main()
