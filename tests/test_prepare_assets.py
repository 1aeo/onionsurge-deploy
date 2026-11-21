import codecs
import copy
import ctypes
import errno
import hashlib
import importlib.util
import io
import json
import os
from pathlib import Path
import subprocess
import signal
import sys
import tempfile
import unittest
from types import SimpleNamespace
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]


def load(name):
    spec = importlib.util.spec_from_file_location(name, ROOT / 'scripts' / (name + '.py'))
    result = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(result)
    return result


assets = load('prepare_assets')

DEFAULT_TIME_RANGES = {
    'version': 1,
    'default_range': '24h',
    'legacy_aliases': {'30d': '1mo', '1m': '1mo', '3m': '3mo', '6m': '6mo', '12m': '12mo'},
    'card_slots': {'fallback_dynamic': '6mo', 'fixed': ['24h', 'all']},
    'periods': [
        {
            'id': '24h',
            'summary_key': 'twenty_four_hours',
            'export_suffix': '24h',
            'label': '24 hours',
            'average_label': 'Last 24 hours',
            'plot_label': '24-hour snapshot',
            'window': {'unit': 'hours', 'count': 24, 'comparison_lag_days': 0},
            'minimum_coverage': 0,
            'minimum_comparison_windows': 30,
        },
        {
            'id': '7d',
            'summary_key': 'seven_days',
            'export_suffix': '7d',
            'label': '7 days',
            'average_label': 'Last 7 days average',
            'plot_label': 'Last 7 days',
            'window': {'unit': 'days', 'count': 7, 'comparison_lag_days': 1},
            'minimum_coverage': 0,
            'minimum_comparison_windows': 6,
        },
        {
            'id': '1mo',
            'summary_key': 'one_month',
            'export_suffix': '1mo',
            'label': '1 month',
            'average_label': 'Last 1 month average',
            'plot_label': 'Last 1 month',
            'window': {'unit': 'months', 'count': 1, 'comparison_lag_days': 0},
            'minimum_coverage': 0,
            'minimum_comparison_windows': 25,
        },
        {
            'id': '3mo',
            'summary_key': 'three_months',
            'export_suffix': '3mo',
            'label': '3 months',
            'average_label': 'Last 3 months average',
            'plot_label': 'Last 3 months',
            'window': {'unit': 'months', 'count': 3, 'comparison_lag_days': 0},
            'minimum_coverage': 0,
            'minimum_comparison_windows': 30,
        },
        {
            'id': '6mo',
            'summary_key': 'six_months',
            'export_suffix': '6mo',
            'label': '6 months',
            'average_label': 'Last 6 months average',
            'plot_label': 'Last 6 months',
            'window': {'unit': 'months', 'count': 6, 'comparison_lag_days': 0},
            'minimum_coverage': 0,
            'minimum_comparison_windows': 30,
        },
        {
            'id': '12mo',
            'summary_key': 'twelve_months',
            'export_suffix': '12mo',
            'label': '12 months',
            'average_label': 'Last 12 months average',
            'plot_label': 'Last 12 months',
            'window': {'unit': 'months', 'count': 12, 'comparison_lag_days': 0},
            'minimum_coverage': 0,
            'minimum_comparison_windows': 30,
        },
        {
            'id': 'all',
            'summary_key': 'all_time',
            'export_suffix': 'all-time',
            'label': 'All time',
            'average_label': 'All-time average',
            'plot_label': 'All time',
            'window': {'unit': 'all_time', 'count': None, 'comparison_lag_days': 0},
            'minimum_coverage': 0,
            'minimum_comparison_windows': 30,
        },
    ],
}


FIXTURE_REF = 'reference/reference-2026-11-18-' + '0' * 12 + '.json'


def public_fixture(source, *, live=False, with_ref=True):
    source.mkdir()
    data = {
        'schema_version': 7,
        'window_seconds': 3600,
        'start': 1794873600,
        'end': 1794960000,
        'time_ranges': DEFAULT_TIME_RANGES,
        'charts': [],
    }
    if with_ref:
        data['reference_document'] = FIXTURE_REF
        (source / 'reference').mkdir()
        (source / FIXTURE_REF).write_text('{}\n')
    core = assets.LIVE_REQUIRED if live else assets.CORE
    for name in core:
        content = json.dumps(data) + '\n' if name == 'data.json' else 'public fixture ' + name
        (source / name).write_text(content)
    files = {
        p.relative_to(source).as_posix(): hashlib.sha256(p.read_bytes()).hexdigest()
        for p in source.rglob('*')
        if p.is_file()
    }
    release_objects = ['data.json', *([FIXTURE_REF] if with_ref else [])]
    manifest = {
        'format': 'onionsurge-build-v1',
        'schema_version': 7,
        'release_objects': sorted(release_objects),
        'files': files,
    }
    if live:
        manifest['mode'] = 'live'
    (source / 'build-manifest.json').write_text(json.dumps(manifest))

REAL_TEMPORARY_DIRECTORY = tempfile.TemporaryDirectory


class CleanupFailure:
    def __init__(self, *args, **kwargs):
        self.inner = REAL_TEMPORARY_DIRECTORY(*args, **kwargs)

    def __enter__(self):
        return self.inner.__enter__()

    def __exit__(self, *args):
        self.inner.__exit__(*args)
        raise OSError('temporary cleanup failed')


class NativePublicationTests(unittest.TestCase):
    def setUp(self):
        # Only tests that exercise the native publication primitive may skip.
        # The implementation itself always fails closed when it is unavailable.
        with tempfile.TemporaryDirectory() as folder:
            source, target = Path(folder) / 'source', Path(folder) / 'target'
            source.mkdir()
            try:
                assets.rename_noreplace(source, target)
            except OSError as error:
                unavailable = {errno.ENOSYS, errno.ENOTSUP, errno.EOPNOTSUPP}
                if sys.platform.startswith('linux'):
                    unavailable.add(errno.EINVAL)
                if error.errno in unavailable:
                    self.skipTest('Native no-replace publication unavailable: ' + str(error))
                raise

    def assert_complete(self, output, commit='a' * 40):
        self.assertEqual({p.name for p in output.iterdir()}, {'public', 'asset-release.json'})
        receipt = json.loads((output / 'asset-release.json').read_text())
        self.assertEqual(receipt['application_commit'], commit)
        self.assertEqual(set(receipt['objects']), assets.CORE | {FIXTURE_REF})
        self.assertFalse((output / 'public/asset-release.json').exists())
        self.assertFalse((output / 'public/build-manifest.json').exists())
        for name, info in receipt['objects'].items():
            raw = (output / 'public' / name).read_bytes()
            self.assertEqual(info, {'bytes': len(raw), 'sha256': hashlib.sha256(raw).hexdigest()})
        return receipt

    def test_live_binding_contains_only_frontend_files(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            source = root / 'source'
            public_fixture(source, live=True)
            with self.assertRaisesRegex(ValueError, '--live'):
                assets.prepare(source, root / 'wrong-mode', 'a' * 40)
            result = assets.prepare(source, root / 'release', 'a' * 40, live=True)
            self.assertEqual(set(result['receipt']['objects']), assets.FRONTEND)
            self.assertEqual(result['receipt']['mode'], 'live')
            self.assertFalse((root / 'release/public/data.js').exists())
            self.assertFalse((root / 'release/public/data.json').exists())

            offline_missing_data_js = root / 'offline-no-data-js'
            public_fixture(offline_missing_data_js, live=True)
            manifest = json.loads((offline_missing_data_js / 'build-manifest.json').read_text())
            del manifest['mode']
            (offline_missing_data_js / 'build-manifest.json').write_text(json.dumps(manifest))
            with self.assertRaisesRegex(ValueError, 'required assets'):
                assets.prepare(offline_missing_data_js, root / 'offline-bad-out', 'a' * 40)

    def test_live_staging_still_checks_omitted_data_hashes(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            source = root / 'source'
            public_fixture(source, live=True)
            (source / 'data.json').write_text('changed')
            with self.assertRaisesRegex(ValueError, 'digest'):
                assets.prepare(source, root / 'release', 'a' * 40, live=True)
            self.assertFalse((root / 'release').exists())

    def test_final_symlink_created_after_checks_is_not_followed(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder).resolve(); source = root / 'source'; public_fixture(source)
            output, unintended = root / 'release', source / 'unintended'
            scan = Path.rglob
            def insert_link_after_scan(path, pattern):
                yield from scan(path, pattern)
                if path == source:
                    output.symlink_to(unintended, target_is_directory=True)
            with mock.patch.object(Path, 'rglob', insert_link_after_scan):
                with self.assertRaises(FileExistsError):
                    assets.prepare(source, output, 'a' * 40)
            self.assertTrue(output.is_symlink())
            self.assertFalse(unintended.exists())

    def test_assets_and_receipt_publish_in_one_no_replace_rename(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder); source = root / 'source'; public_fixture(source)
            output = root / 'release'
            rename = assets.rename_noreplace
            def publish(stage, destination):
                self.assert_complete(stage)
                self.assertFalse(destination.exists())
                rename(stage, destination)
            with mock.patch.object(assets, 'rename_noreplace', side_effect=publish) as operation:
                result = assets.prepare(source, output, 'a' * 40)
            self.assertEqual(operation.call_count, 1)
            self.assertNotIn(str(source), json.dumps(result))
            with self.assertRaises(ValueError):
                assets.prepare(source, output, 'a' * 40)
            self.assertEqual(result['status'], 'complete')
            self.assertEqual(result['release_directory'], str(output.resolve()))
            self.assertEqual(result['public_directory'], str((output / 'public').resolve()))
            self.assertEqual(result['receipt'], self.assert_complete(output))
            self.assertEqual(result['warnings'], [])
            self.assertFalse(list(root.glob('.onionsurge-stage-*')))

    def test_existing_empty_directory_at_commit_is_preserved(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder); source = root / 'source'; public_fixture(source)
            output = root / 'release'; rename = assets.rename_noreplace
            foreign = None
            def conflict(stage, destination):
                nonlocal foreign
                destination.mkdir()
                foreign = destination.stat().st_ino
                rename(stage, destination)
            with mock.patch.object(assets, 'rename_noreplace', side_effect=conflict):
                with self.assertRaises(FileExistsError):
                    assets.prepare(source, output, 'a' * 40)
            self.assertEqual(output.stat().st_ino, foreign)
            self.assertEqual(list(output.iterdir()), [])
            self.assertFalse(list(root.glob('.onionsurge-stage-*')))

    def test_interrupt_after_commit_retains_complete_release(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder); source = root / 'source'; public_fixture(source)
            output = root / 'release'; rename = assets.rename_noreplace
            def interrupt(stage, destination):
                rename(stage, destination)
                raise KeyboardInterrupt('after atomic publication')
            with mock.patch.object(assets, 'rename_noreplace', side_effect=interrupt):
                with self.assertRaises(KeyboardInterrupt) as raised:
                    assets.prepare(source, output, 'a' * 40)
            self.assert_complete(output)
            self.assertIn('Inspect that release root', '\n'.join(raised.exception.__notes__))
            self.assertFalse(list(root.glob('.onionsurge-stage-*')))

    @unittest.skipUnless(hasattr(signal, 'SIGKILL'), 'SIGKILL is unavailable')
    def test_sigkill_at_commit_cannot_separate_receipt_from_assets(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder); source = root / 'source'; public_fixture(source)
            output = root / 'release'
            code = '''import os,signal,sys
from pathlib import Path
sys.path.insert(0, sys.argv[1])
import prepare_assets as assets
native=assets.rename_noreplace
def killed(stage,destination):
    native(stage,destination)
    os.kill(os.getpid(),signal.SIGKILL)
assets.rename_noreplace=killed
assets.prepare(Path(sys.argv[2]),Path(sys.argv[3]),'a'*40)
'''
            result = subprocess.run([sys.executable, '-B', '-c', code,
                str(Path(assets.__file__).parent), str(source), str(output)], capture_output=True)
            self.assertEqual(result.returncode, -signal.SIGKILL, result.stderr)
            self.assert_complete(output)

    def test_post_commit_cleanup_failure_returns_completed_release_with_warning(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder); source = root / 'source'; public_fixture(source)
            output = root / 'release'
            with mock.patch.object(assets.tempfile, 'TemporaryDirectory', CleanupFailure):
                result = assets.prepare(source, output, 'a' * 40)
            self.assertEqual(result['status'], 'complete')
            self.assertEqual(result['receipt'], self.assert_complete(output))
            self.assertEqual(len(result['warnings']), 1)
            self.assertIn('Release completed at ' + str(output.resolve()), result['warnings'][0])
            self.assertIn('temporary cleanup failed', result['warnings'][0])

    def test_distinct_release_roots_do_not_share_a_receipt_path(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder); source = root / 'source'; public_fixture(source)
            for name, commit in [('one', 'a' * 40), ('two', 'b' * 40)]:
                result = assets.prepare(source, root / name, commit)
                self.assertEqual(result['receipt'], self.assert_complete(root / name, commit))
            self.assertFalse((root / 'asset-release.json').exists())

    def test_configurable_time_range_csvs_and_staged_data_json_validation_succeed_under_native_rename(self):
        def make_custom_time_ranges():
            return {
                'version': 1,
                'default_range': '14d',
                'legacy_aliases': {'24h': '14d', '30d': '14d'},
                'card_slots': {'fallback_dynamic': '14d', 'fixed': ['24mo', 'all']},
                'periods': [
                    {
                        'id': '14d',
                        'summary_key': 'fourteen_days',
                        'export_suffix': '14d',
                        'label': '14 days',
                        'average_label': 'Last 14 days average',
                        'plot_label': 'Last 14 days',
                        'window': {'unit': 'days', 'count': 14, 'comparison_lag_days': 1},
                        'minimum_coverage': 0.9,
                        'minimum_comparison_windows': 10,
                    },
                    {
                        'id': '24mo',
                        'summary_key': 'twenty_four_months',
                        'export_suffix': '24mo',
                        'label': '24 months',
                        'average_label': 'Last 24 months average',
                        'plot_label': 'Last 24 months',
                        'window': {'unit': 'months', 'count': 24, 'comparison_lag_days': 0},
                        'minimum_coverage': 0.9,
                        'minimum_comparison_windows': 30,
                    },
                    {
                        'id': 'all',
                        'summary_key': 'all_time',
                        'export_suffix': 'all-time',
                        'label': 'All time',
                        'average_label': 'All-time average',
                        'plot_label': 'All time',
                        'window': {'unit': 'all_time', 'count': None, 'comparison_lag_days': 0},
                        'minimum_coverage': 0,
                        'minimum_comparison_windows': 30,
                    },
                ],
            }

        for label, time_ranges, window_seconds, end in [
            ('default', copy.deepcopy(DEFAULT_TIME_RANGES), 3600, 1794963600),
            ('custom', make_custom_time_ranges(), 86400, 1794960000),
        ]:
            with self.subTest(label=label), tempfile.TemporaryDirectory() as folder:
                series_entry = (
                    {'key': 'circuit_rejections', 'values': [], 'current': {'hour': {}, 'day': {}},
                     'history': {'daily': [], 'sampled_hour': []}}
                    if window_seconds == 3600
                    else {'key': 'circuit_rejections', 'current': {'day': {}}, 'history': {'daily': []}}
                )
                base_data = {
                    'schema_version': 7,
                    'window_seconds': window_seconds,
                    'start': end - 86400,
                    'end': end,
                    'time_ranges': time_ranges,
                    'reference_document': FIXTURE_REF,
                    'charts': [{'id': 'circuit_rejections', 'series': [series_entry]}],
                }
                root = Path(folder); source = root / 'good'
                public_fixture(source)
                (source / 'data.json').write_text(json.dumps(base_data))
                expected_csvs = [f"exports/circuit_rejections-{p['export_suffix']}.csv" for p in time_ranges['periods']]
                for rel in expected_csvs:
                    csv_path = source / rel
                    csv_path.parent.mkdir(parents=True, exist_ok=True)
                    csv_path.write_text('t,v\n')
                files = {str(p.relative_to(source)): hashlib.sha256(p.read_bytes()).hexdigest()
                         for p in source.rglob('*') if p.is_file() and p.name != 'build-manifest.json'}
                (source / 'build-manifest.json').write_text(json.dumps({
                    'format': 'onionsurge-build-v1', 'schema_version': 7,
                    'release_objects': sorted(['data.json', FIXTURE_REF, *expected_csvs]), 'files': files,
                }))
                res = assets.prepare(source, root / 'good-out', 'a' * 40)
                self.assertEqual(res['status'], 'complete')
                for rel in expected_csvs:
                    self.assertIn(rel, res['receipt']['objects'])

                if window_seconds == 86400:
                    live_src = root / 'good-live'
                    public_fixture(live_src, live=True)
                    (live_src / 'data.json').write_text(json.dumps(base_data))
                    for rel in expected_csvs:
                        csv_path = live_src / rel
                        csv_path.parent.mkdir(parents=True, exist_ok=True)
                        csv_path.write_text('t,v\n')
                    live_files = {str(p.relative_to(live_src)): hashlib.sha256(p.read_bytes()).hexdigest()
                                  for p in live_src.rglob('*') if p.is_file() and p.name != 'build-manifest.json'}
                    (live_src / 'build-manifest.json').write_text(json.dumps({
                        'format': 'onionsurge-build-v1', 'mode': 'live', 'schema_version': 7,
                        'release_objects': sorted(['data.json', FIXTURE_REF, *expected_csvs]), 'files': live_files,
                    }))
                    live_res = assets.prepare(live_src, root / 'good-live-out', 'a' * 40, live=True)
                    self.assertEqual(live_res['status'], 'complete')
                    self.assertEqual(set(live_res['receipt']['objects']), set(assets.FRONTEND))

                    bad_24h_live = root / 'bad-24h-live'
                    public_fixture(bad_24h_live, live=True)
                    (bad_24h_live / 'data.json').write_text(json.dumps(base_data))
                    extra_24h = [*expected_csvs, 'exports/circuit_rejections-24h.csv']
                    for rel in extra_24h:
                        csv_path = bad_24h_live / rel
                        csv_path.parent.mkdir(parents=True, exist_ok=True)
                        csv_path.write_text('t,v\n')
                    bad_files = {str(p.relative_to(bad_24h_live)): hashlib.sha256(p.read_bytes()).hexdigest()
                                 for p in bad_24h_live.rglob('*') if p.is_file() and p.name != 'build-manifest.json'}
                    (bad_24h_live / 'build-manifest.json').write_text(json.dumps({
                        'format': 'onionsurge-build-v1', 'mode': 'live', 'schema_version': 7,
                        'release_objects': sorted(['data.json', FIXTURE_REF, *extra_24h]), 'files': bad_files,
                    }))
                    with self.assertRaisesRegex(ValueError, '-24h\\.csv'):
                        assets.prepare(bad_24h_live, root / 'bad-24h-live-out', 'a' * 40, live=True)

                # Missing or unexpected export CSVs are rejected in non-live staging.
                missing_src = root / 'missing'
                public_fixture(missing_src)
                (missing_src / 'data.json').write_text(json.dumps(base_data))
                for rel in expected_csvs[:-1]:
                    csv_path = missing_src / rel
                    csv_path.parent.mkdir(parents=True, exist_ok=True)
                    csv_path.write_text('t,v\n')
                missing_files = {str(p.relative_to(missing_src)): hashlib.sha256(p.read_bytes()).hexdigest()
                                 for p in missing_src.rglob('*') if p.is_file() and p.name != 'build-manifest.json'}
                (missing_src / 'build-manifest.json').write_text(json.dumps({
                    'format': 'onionsurge-build-v1', 'schema_version': 7,
                    'release_objects': sorted(['data.json', FIXTURE_REF, *expected_csvs[:-1]]), 'files': missing_files,
                }))
                with self.assertRaisesRegex(ValueError, 'exports/\\*\\.csv'):
                    assets.prepare(missing_src, root / 'missing-out', 'a' * 40)
                self.assertFalse((root / 'missing-out').exists())

                extra_src = root / 'extra'
                public_fixture(extra_src)
                (extra_src / 'data.json').write_text(json.dumps(base_data))
                for rel in [*expected_csvs, 'exports/circuit_rejections-99d.csv']:
                    csv_path = extra_src / rel
                    csv_path.parent.mkdir(parents=True, exist_ok=True)
                    csv_path.write_text('t,v\n')
                extra_files = {str(p.relative_to(extra_src)): hashlib.sha256(p.read_bytes()).hexdigest()
                               for p in extra_src.rglob('*') if p.is_file() and p.name != 'build-manifest.json'}
                (extra_src / 'build-manifest.json').write_text(json.dumps({
                    'format': 'onionsurge-build-v1', 'schema_version': 7,
                    'release_objects': sorted(['data.json', FIXTURE_REF, *expected_csvs, 'exports/circuit_rejections-99d.csv']),
                    'files': extra_files,
                }))
                with self.assertRaisesRegex(ValueError, 'exports/\\*\\.csv'):
                    assets.prepare(extra_src, root / 'extra-out', 'a' * 40)
                self.assertFalse((root / 'extra-out').exists())

        # Cross-chart/period CSV filename collisions (connection + rate-7d vs connection-rate + 7d) fail closed.
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder); collide_src = root / 'collide'
            public_fixture(collide_src)
            collide_ranges = copy.deepcopy(DEFAULT_TIME_RANGES)
            collide_ranges['periods'].append(
                dict(collide_ranges['periods'][1], id='rate_7d', summary_key='rate_7d', export_suffix='rate-7d'))
            collide_data = {
                'schema_version': 7,
                'window_seconds': 3600,
                'start': 1794873600,
                'end': 1794960000,
                'time_ranges': collide_ranges,
                'reference_document': FIXTURE_REF,
                'charts': [{'id': 'connection'}, {'id': 'connection-rate'}],
            }
            (collide_src / 'data.json').write_text(json.dumps(collide_data))
            for cid in ('connection', 'connection-rate'):
                for period in collide_ranges['periods']:
                    csv_path = collide_src / f"exports/{cid}-{period['export_suffix']}.csv"
                    csv_path.parent.mkdir(parents=True, exist_ok=True)
                    csv_path.write_text('t,v\n')
            c_files = {str(p.relative_to(collide_src)): hashlib.sha256(p.read_bytes()).hexdigest()
                       for p in collide_src.rglob('*') if p.is_file() and p.name != 'build-manifest.json'}
            (collide_src / 'build-manifest.json').write_text(json.dumps({
                'format': 'onionsurge-build-v1', 'schema_version': 7,
                'release_objects': sorted(name for name in c_files if name == 'data.json' or name.startswith(('reference/', 'exports/'))),
                'files': c_files,
            }))
            with self.assertRaisesRegex(ValueError, 'Duplicate export CSV path: exports/connection-rate-7d\\.csv'):
                assets.prepare(collide_src, root / 'collide-out', 'a' * 40)
            self.assertFalse((root / 'collide-out').exists())


class StagingBoundaryTests(unittest.TestCase):
    def test_native_probe_skips_linux_einval_but_not_unexpected_errors(self):
        for platform, code, outcome in [('linux', errno.EINVAL, unittest.SkipTest),
                                         ('linux', errno.EPERM, OSError),
                                         ('darwin', errno.EINVAL, OSError)]:
            probe = NativePublicationTests('test_assets_and_receipt_publish_in_one_no_replace_rename')
            with self.subTest(platform=platform, code=code), mock.patch.object(sys, 'platform', platform), \
                    mock.patch.object(assets, 'rename_noreplace', side_effect=OSError(code, 'probe failure')):
                with self.assertRaises(outcome):
                    probe.setUp()

    def test_interrupt_before_commit_cleans_only_private_stage(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder); source = root / 'source'; public_fixture(source)
            output = root / 'release'
            with mock.patch.object(assets, 'rename_noreplace', side_effect=KeyboardInterrupt('before commit')):
                with self.assertRaises(KeyboardInterrupt):
                    assets.prepare(source, output, 'a' * 40)
            self.assertFalse(output.exists())
            self.assertFalse(list(root.glob('.onionsurge-stage-*')))
            self.assertTrue((source / 'index.html').exists())

    def test_pre_commit_cleanup_error_preserves_primary_error(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder); source = root / 'source'; public_fixture(source)
            output = root / 'release'
            with mock.patch.object(assets.tempfile, 'TemporaryDirectory', CleanupFailure), \
                    mock.patch.object(assets, 'stage_asset_file', side_effect=ValueError('original copy failure')):
                with self.assertRaisesRegex(ValueError, 'original copy failure') as raised:
                    assets.prepare(source, output, 'a' * 40)
            self.assertIn('temporary cleanup failed', '\n'.join(raised.exception.__notes__))
            self.assertFalse(output.exists())

    def test_manifest_json_and_asset_file_safety_limits(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder); source = root / 'source'; public_fixture(source)
            (source / 'build-manifest.json').write_text('{"format":"onionsurge-build-v1","format":"onionsurge-build-v1"}')
            with self.assertRaisesRegex(ValueError, 'Duplicate JSON'):
                assets.prepare(source, root / 'dup', 'a' * 40)
            (source / 'build-manifest.json').write_text('[' * 100 + '1' + ']' * 100)
            with self.assertRaisesRegex(ValueError, 'nesting'):
                assets.prepare(source, root / 'deep', 'a' * 40)
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder); source = root / 'source'; public_fixture(source)
            (source / 'app.js').unlink()
            os.mkfifo(source / 'app.js')
            with self.assertRaisesRegex(ValueError, 'regular file'):
                assets.read_regular_bytes(source / 'app.js')
            dir_target = Path(folder) / 'dir-asset'
            dir_target.mkdir()
            probe_before = os.open(os.devnull, os.O_RDONLY)
            os.close(probe_before)
            with self.assertRaisesRegex(ValueError, 'regular file'):
                assets.read_regular_bytes(dir_target)
            probe_after = os.open(os.devnull, os.O_RDONLY)
            os.close(probe_after)
            self.assertEqual(probe_after, probe_before)
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder); source = root / 'source'; public_fixture(source)
            with mock.patch.object(assets, 'MAX_ASSET_BYTES', 8):
                with self.assertRaisesRegex(ValueError, 'size limit'):
                    assets.prepare(source, root / 'oversized', 'a' * 40)

    def test_atomic_rename_flags_and_unsupported_platform_fail_closed(self):
        for platform, symbol, expected in [
                ('linux', 'renameat2', (-100, b'source', -100, b'target', 1)),
                ('darwin', 'renamex_np', (b'source', b'target', 4))]:
            operation = mock.Mock(return_value=0)
            with self.subTest(platform=platform), mock.patch.object(sys, 'platform', platform), \
                    mock.patch.object(ctypes, 'CDLL', return_value=SimpleNamespace(**{symbol: operation})):
                assets.rename_noreplace(Path('source'), Path('target'))
                operation.assert_called_once_with(*expected)
        for platform, library in [('unsupported', SimpleNamespace()), ('linux', SimpleNamespace())]:
            with mock.patch.object(sys, 'platform', platform), mock.patch.object(ctypes, 'CDLL', return_value=library):
                with self.assertRaises(OSError) as raised:
                    assets.rename_noreplace(Path('source'), Path('target'))
                self.assertEqual(raised.exception.errno, errno.ENOTSUP)

    def test_non_object_manifest_is_rejected_before_staging(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder); source = root / 'source'; public_fixture(source)
            (source / 'build-manifest.json').write_text('null')
            with self.assertRaisesRegex(ValueError, 'manifest'):
                assets.prepare(source, root / 'release', 'a' * 40)
            self.assertFalse((root / 'release').exists())

    def test_sub_24h_data_and_legacy_csv_rejected_and_utf16le_depth_linear(self):
        base_data = {
            'schema_version': 7,
            'window_seconds': 3600,
            'start': 1794873600,
            'end': 1794960000,
            'time_ranges': copy.deepcopy(DEFAULT_TIME_RANGES),
            'reference_document': FIXTURE_REF,
            'charts': [{'id': 'circuit_rejections', 'series': [
                {'key': 'circuit_rejections', 'values': [], 'current': {'hour': {}, 'day': {}},
                 'history': {'daily': [], 'sampled_hour': []}}]}],
        }
        def write_source(source, payload, extra_files=()):
            public_fixture(source)
            (source / 'data.json').write_text(json.dumps(payload))
            for chart in base_data['charts']:
                for period in DEFAULT_TIME_RANGES['periods']:
                    target = source / f"exports/{chart['id']}-{period['export_suffix']}.csv"
                    target.parent.mkdir(parents=True, exist_ok=True)
                    target.write_text('t,v\n')
            for name, content in extra_files:
                target = source / name
                target.parent.mkdir(parents=True, exist_ok=True)
                target.write_text(content)
            files = {str(p.relative_to(source)): hashlib.sha256(p.read_bytes()).hexdigest()
                     for p in source.rglob('*') if p.is_file() and p.name != 'build-manifest.json'}
            (source / 'build-manifest.json').write_text(json.dumps({
                'format': 'onionsurge-build-v1', 'schema_version': 7,
                'release_objects': sorted(name for name in files if name == 'data.json' or name.startswith(('reference/', 'exports/'))),
                'files': files,
            }))

        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            hyphen_src = root / 'src-hyphen-24h'
            hyphen_data = dict(base_data, charts=[{'id': 'connection-rate'}])
            public_fixture(hyphen_src)
            (hyphen_src / 'data.json').write_text(json.dumps(hyphen_data))
            for period in DEFAULT_TIME_RANGES['periods']:
                target = hyphen_src / f"exports/connection-rate-{period['export_suffix']}.csv"
                target.parent.mkdir(parents=True, exist_ok=True)
                target.write_text('t,v\n')
            h_files = {str(p.relative_to(hyphen_src)): hashlib.sha256(p.read_bytes()).hexdigest()
                       for p in hyphen_src.rglob('*') if p.is_file() and p.name != 'build-manifest.json'}
            (hyphen_src / 'build-manifest.json').write_text(json.dumps({
                'format': 'onionsurge-build-v1', 'schema_version': 7,
                'release_objects': sorted(name for name in h_files if name == 'data.json' or name.startswith(('reference/', 'exports/'))),
                'files': h_files,
            }))
            self.assertEqual(assets.prepare(hyphen_src, root / 'out-hyphen-24h', 'a' * 40)['status'], 'complete')

            bad_cases = [
                ('sub24h-0h-csv', base_data, [('exports/circuit_rejections-0h.csv', 't,v\n')], r'unapproved filename'),
                ('sub24h-1h-csv', base_data, [('exports/circuit_rejections-1h.csv', 't,v\n')], r'unapproved filename'),
                ('sub24h-23h-csv', base_data, [('exports/circuit_rejections-23h.csv', 't,v\n')], r'unapproved filename'),
                ('sub24h-024h-csv', base_data, [('exports/circuit_rejections-024h.csv', 't,v\n')], r'unapproved filename'),
                ('sub24h-30m-csv', base_data, [('exports/circuit_rejections-30m.csv', 't,v\n')], r'unapproved filename'),
                ('sub24h-1m-csv', base_data, [('exports/circuit_rejections-1m.csv', 't,v\n')], r'unapproved filename'),
                ('sub24h-60s-csv', base_data, [('exports/circuit_rejections-60s.csv', 't,v\n')], r'unapproved filename'),
                ('sub24h-1d-csv', base_data, [('exports/circuit_rejections-1d.csv', 't,v\n')], r'unapproved filename'),
                ('sub24h-0d-csv', base_data, [('exports/circuit_rejections-0d.csv', 't,v\n')], r'unapproved filename'),
                ('sub24h-foo-1h-csv', base_data, [('exports/connection-rate-foo-1h.csv', 't,v\n')], r'unapproved filename'),
                ('non-object-array', [{'schema_version': 4, 'window_seconds': 3600, 'hour': {}}], (), r'Expected JSON object'),
                ('empty-object', {}, (), r'schema_version'),
                ('missing-schema', {k: v for k, v in base_data.items() if k != 'schema_version'}, (), r'schema_version'),
                ('float-schema', dict(base_data, schema_version=7.0), (), r'schema_version'),
                ('bool-schema', dict(base_data, schema_version=True), (), r'schema_version'),
                ('schema4', dict(base_data, schema_version=4), (), r'schema_version'),
                ('schema5', dict(base_data, schema_version=5), (), r'schema_version'),
                ('schema6', dict(base_data, schema_version=6), (), r'schema_version'),
                ('missing-time-ranges', {k: v for k, v in base_data.items() if k != 'time_ranges'}, (), r'time_ranges'),
                ('missing-window', {k: v for k, v in base_data.items() if k != 'window_seconds'}, (), r'window_seconds'),
                ('float-window', dict(base_data, window_seconds=3600.0), (), r'window_seconds'),
                ('window7200', dict(base_data, window_seconds=7200), (), r'window_seconds'),
                ('daily-window-non-midnight', dict(base_data, window_seconds=86400, end=1794963600, start=1794877200), (), r'UTC day'),
                ('daily-window-24h-period', dict(base_data, window_seconds=86400, charts=[{'id': 'circuit_rejections', 'series': [{'key': 'circuit_rejections', 'current': {'day': {}}, 'history': {'daily': []}}]}]), (), r'daily time_ranges'),
                ('daily-window-values', dict(base_data, window_seconds=86400, time_ranges=dict(DEFAULT_TIME_RANGES, default_range='1mo', periods=DEFAULT_TIME_RANGES['periods'][1:]), charts=[{'id': 'circuit_rejections', 'series': [{'values': []}]}]), (), r'Forbidden sub-24h'),
                ('daily-window-hour-key', dict(base_data, window_seconds=86400, time_ranges=dict(DEFAULT_TIME_RANGES, default_range='1mo', periods=DEFAULT_TIME_RANGES['periods'][1:]), charts=[{'id': 'circuit_rejections', 'series': [{'current': {'hour': {}}}]}]), (), r'Forbidden sub-24h'),
                ('non-hourly', dict(base_data, end=1794961800, start=1794875400), (), r'UTC hour'),
                ('private-hostname', dict(base_data, charts=[{'id': 'circuit_rejections', 'hostname': 'internal'}]), (), r'private metadata'),
                ('private-token', dict(base_data, token='secret'), (), r'private metadata'),
                ('private-proto', dict(base_data, charts=[{'id': 'circuit_rejections', '__proto__': {}}]), (), r'private metadata'),
            ]
            for label, payload, extras, pattern in bad_cases:
                with self.subTest(label=label):
                    bad_src = root / f'src-{label}'
                    write_source(bad_src, payload, extras)
                    with self.assertRaisesRegex(ValueError, pattern):
                        assets.prepare(bad_src, root / f'out-{label}', 'a' * 40)
                    self.assertFalse((root / f'out-{label}').exists())

            # Staged bytes validation reads the private staged copy rather than re-reading source path.
            tampered_src = root / 'src-tamper'
            write_source(tampered_src, base_data)
            real_stage = assets.stage_asset_file
            def swap_staged_data(src, dst):
                if src.name == 'data.json':
                    payload = json.dumps({'schema_version': 4, 'window_seconds': 3600}).encode()
                    dst.write_bytes(payload)
                    return len(payload), hashlib.sha256((tampered_src / 'data.json').read_bytes()).hexdigest()
                return real_stage(src, dst)
            with mock.patch.object(assets, 'stage_asset_file', side_effect=swap_staged_data):
                with self.assertRaisesRegex(ValueError, 'schema_version'):
                    assets.prepare(tampered_src, root / 'out-tamper', 'a' * 40)

        self.assertEqual(assets.strict_json_bytes(codecs.BOM_UTF16_LE + '{"ok":true}'.encode('utf-16-le')), {'ok': True})
        self.assertEqual(
            assets.strict_json_bytes(codecs.BOM_UTF16_LE + '{"emoji":"\\ud83d\\ude00","raw":"\U0001f600"}'.encode('utf-16-le')),
            {'emoji': '\U0001f600', 'raw': '\U0001f600'},
        )
        with self.assertRaisesRegex(ValueError, 'Invalid JSON text encoding'):
            assets.strict_json_bytes(b'{"a":"\xed\xa0\x80"}')
        for payload, expected in (
            ('{"a": "' + '[' * 70 + '"}', {'a': '[' * 70}),
            ('[1, "' + '{' * 70 + '"]', [1, '{' * 70]),
        ):
            for encoded in (payload.encode('utf-8'), codecs.BOM_UTF16_LE + payload.encode('utf-16-le')):
                self.assertEqual(assets.strict_json_bytes(encoded), expected)
        deep_bypass = ('{"a": "' + ']' * 100 + '", "b": ' + '[' * 120 + '1' + ']' * 120 + '}').encode('utf-8')
        with self.assertRaisesRegex(ValueError, 'nesting'):
            assets.strict_json_bytes(deep_bypass)
        for raw_nonfinite in (b'{"ok":1e400}', b'{"ok":-1e400}'):
            with self.subTest(raw_nonfinite=raw_nonfinite), self.assertRaisesRegex(ValueError, 'Non-finite'):
                assets.strict_json_bytes(raw_nonfinite)
        with self.assertRaises(ValueError):
            assets.strict_json_bytes(b'"' + b'\\"' * 500_000)
        large_flat_payload = '"' + '[' * 200_000 + '\\"' * 1000 + '"'
        self.assertEqual(assets.strict_json_bytes(large_flat_payload.encode('utf-8')), '[' * 200_000 + '"' * 1000)

        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            bigint_data = copy.deepcopy(base_data)
            bigint_data['time_ranges']['periods'][0]['minimum_coverage'] = 10**400
            bigint_src = root / 'src-bigint'
            write_source(bigint_src, bigint_data)
            with self.assertRaisesRegex(ValueError, 'Non-finite'):
                assets.prepare(bigint_src, root / 'out-bigint', 'a' * 40)
            stderr_buf = io.StringIO()
            with mock.patch('sys.stderr', stderr_buf), self.assertRaises(SystemExit) as raised:
                assets.main(['--source', str(bigint_src), '--application-commit', 'a' * 40, '--output', str(root / 'out-cli')])
            self.assertEqual(raised.exception.code, 1)
            self.assertIn('Asset staging failed:', stderr_buf.getvalue())

    def test_build_manifest_schema_version_and_release_objects_verification(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            ref_path = 'reference/reference-2026-11-18-' + 'a' * 12 + '.json'
            export_paths = [f"exports/circuit_rejections-{p['export_suffix']}.csv" for p in DEFAULT_TIME_RANGES['periods']]
            base_data = {
                'schema_version': 7,
                'window_seconds': 3600,
                'start': 1794873600,
                'end': 1794960000,
                'time_ranges': copy.deepcopy(DEFAULT_TIME_RANGES),
                'charts': [{'id': 'circuit_rejections'}],
                'reference_document': ref_path,
            }

            def build_with_manifest(name, manifest_patch, data_override=None, omit_keys=()):
                src = root / name
                public_fixture(src, with_ref=False)
                (src / 'data.json').write_text(json.dumps(data_override if data_override is not None else base_data))
                (src / 'reference').mkdir(exist_ok=True)
                (src / ref_path).write_text('{}')
                (src / 'exports').mkdir()
                for rel in export_paths:
                    (src / rel).write_text('t,v\n')
                files = {str(p.relative_to(src)): hashlib.sha256(p.read_bytes()).hexdigest()
                         for p in src.rglob('*') if p.is_file() and p.name != 'build-manifest.json'}
                manifest = {'format': 'onionsurge-build-v1', 'schema_version': 7,
                            'release_objects': sorted(['data.json', ref_path, *export_paths]), 'files': files}
                manifest.update(manifest_patch)
                for key in omit_keys:
                    manifest.pop(key, None)
                (src / 'build-manifest.json').write_text(json.dumps(manifest))
                return src

            good_src = build_with_manifest('good', {})
            res = assets.prepare(good_src, root / 'good-out', 'a' * 40)
            self.assertIn(ref_path, res['receipt']['objects'])

            # Non-live staging still cross-checks data.json exports and reference_document when release_objects is present.
            wrong_charts = dict(base_data, charts=[{'id': 'other_chart'}])
            with self.assertRaisesRegex(ValueError, 'exports/\\*\\.csv'):
                assets.prepare(build_with_manifest('bad-data-charts', {}, wrong_charts), root / 'out-charts', 'a' * 40)
            missing_ref_field = {k: v for k, v in base_data.items() if k != 'reference_document'}
            with self.assertRaisesRegex(ValueError, 'reference_document'):
                assets.prepare(build_with_manifest('bad-data-ref', {}, missing_ref_field), root / 'out-ref', 'a' * 40)

            for label, omit_key in [('missing-schema', 'schema_version'), ('missing-release-objects', 'release_objects')]:
                with self.subTest(missing_manifest_key=label):
                    bad_src = build_with_manifest(f'bad-{label}', {}, omit_keys=(omit_key,))
                    with self.assertRaisesRegex(ValueError, 'manifest'):
                        assets.prepare(bad_src, root / f'out-{label}', 'a' * 40)

            for label, bad_schema in [('v6', 6), ('v5', 5), ('v4', 4), ('float', 7.0), ('bool', True)]:
                with self.subTest(bad_schema=label):
                    bad_src = build_with_manifest(f'bad-schema-{label}', {'schema_version': bad_schema})
                    with self.assertRaisesRegex(ValueError, 'manifest'):
                        assets.prepare(bad_src, root / f'out-schema-{label}', 'a' * 40)

            too_many_periods = copy.deepcopy(base_data)
            too_many_periods['time_ranges']['periods'] = [
                dict(base_data['time_ranges']['periods'][0]) for _ in range(17)
            ]
            with self.assertRaisesRegex(ValueError, 'time_ranges'):
                assets.prepare(build_with_manifest('bad-periods-17', {}, too_many_periods),
                               root / 'out-periods-17', 'a' * 40)

            bad_rel_cases = [
                ('not-list', {'release_objects': 'data.json'}),
                ('duplicate', {'release_objects': ['data.json', 'data.json', ref_path, *export_paths]}),
                ('design-html', {'release_objects': ['data.json', 'design-1.html', ref_path, *export_paths]}),
                ('missing-data', {'release_objects': [ref_path, *export_paths]}),
                ('sub-24h', {'release_objects': ['data.json', ref_path, *export_paths, 'exports/circuit_rejections-1h.csv']}),
                ('mismatch-export', {'release_objects': sorted(['data.json', ref_path, *export_paths[:-1]])}),
                ('mismatch-ref', {'release_objects': sorted(['data.json', *export_paths])}),
            ]
            for label, patch in bad_rel_cases:
                with self.subTest(bad_release_objects=label):
                    bad_src = build_with_manifest(f'bad-rel-{label}', patch)
                    with self.assertRaises(ValueError):
                        assets.prepare(bad_src, root / f'out-rel-{label}', 'a' * 40)

    def test_asset_staging_rejects_private_hidden_file(self):
        with tempfile.TemporaryDirectory() as tmp:
            source = Path(tmp) / 'input'; public_fixture(source)
            (source / '.env').write_text('private')
            with self.assertRaises(ValueError):
                assets.prepare(source, Path(tmp) / 'build', 'a' * 40)

    def test_asset_staging_checks_manifest_hashes_and_file_allowlist(self):
        with tempfile.TemporaryDirectory() as tmp:
            source = Path(tmp) / 'input'; public_fixture(source)
            (source / 'app.js').write_text('changed after build')
            with self.assertRaisesRegex(ValueError, 'digest'):
                assets.prepare(source, Path(tmp) / 'build', 'a' * 40)
            self.assertFalse((Path(tmp) / 'build').exists())
        self.assertTrue(assets.allowed_public_path('design-3.html'))
        for suffix in ('24h', '7d', '1mo', '3mo', '6mo', '12mo', 'all-time', '14d', '24mo', '30d'):
            self.assertTrue(assets.allowed_public_path(f'exports/connection-rate-{suffix}.csv'), suffix)
        for bad_export in ('exports/connection-rate-0d.csv', 'exports/connection-rate-1d.csv',
                           'exports/connection-rate-0h.csv', 'exports/connection-rate-1h.csv',
                           'exports/connection-rate-23h.csv', 'exports/connection-rate-024h.csv',
                           'exports/connection-rate-foo-1h.csv', 'exports/connection-rate-30m.csv',
                           'exports/connection-rate-1m.csv', 'exports/connection-rate-60s.csv',
                           'exports/1connection-rate-7d.csv'):
            self.assertFalse(assets.allowed_public_path(bad_export), bad_export)
        for name in ('private.json', 'commands.txt', '../secret', 'exports/database.db', 'design-4.html'):
            self.assertFalse(assets.allowed_public_path(name))
