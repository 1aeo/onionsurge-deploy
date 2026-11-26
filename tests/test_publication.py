import copy
import json
import os
from pathlib import Path
import stat
import sys
import tempfile
import unittest
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent))
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'scripts'))
import publication as pub
import publish
from test_prepare_assets import DEFAULT_TIME_RANGES

END = 1794960000
CONTRACT_SHA256 = 'e' * 64


def fixture(directory, end=END, time_ranges=None, chart_count=13, contract_sha256=CONTRACT_SHA256):
    if not directory.exists():
        directory.mkdir()
    ranges = copy.deepcopy(time_ranges or DEFAULT_TIME_RANGES)
    window = {'date': '2026-11-18', 'end': end - 86400, 'start': end - 10 * 86400}
    reference = {'window': window, 'method_sha256': 'a' * 64, 'source_input_sha256': 'b' * 64,
                 'source_snapshot_end': end - 86400, 'distributions': {}}
    raw = pub.json_bytes(reference)
    reference_id = 'reference-' + window['date'] + '-' + pub.sha256(raw)[:12]
    name = 'reference/' + reference_id + '.json'
    data = {'schema_version': 7, 'window_seconds': 3600, 'start': end - 86400, 'end': end,
            'time_ranges': ranges,
            'charts': [{'id': 'metric' + str(n)} for n in range(chart_count)],
            'reference_window': window, 'reference_cutoff': window['end'],
            'reference_document': name, 'reference_id': reference_id, 'reference_sha256': pub.sha256(raw),
            'provenance': {'reference_method_sha256': 'a' * 64, 'aggregate_input_sha256': 'c' * 64,
                           'query_contract_sha256': contract_sha256}}
    contents = {key: b'public frontend\n' for key in pub.FRONTEND}
    contents.update({'data.json': pub.json_bytes(data), name: raw})
    for chart in data['charts']:
        for period in ranges['periods']:
            contents[f"exports/{chart['id']}-{period['export_suffix']}.csv"] = b'timestamp,value\n1,2\n'
    for name, content in contents.items():
        path = directory / name
        path.parent.mkdir(exist_ok=True)
        path.write_bytes(content)
    rewrite_build(directory, end=end, contract_sha256=contract_sha256)
    return data


_AUTO = object()


def rewrite_build(directory, mode='live', end=END, contract_sha256=CONTRACT_SHA256, schema_version=7, release_objects=_AUTO):
    files = {path.relative_to(directory).as_posix(): pub.sha256(path.read_bytes())
             for path in directory.rglob('*') if path.is_file() and path.name != 'build-manifest.json'}
    manifest = {'format': 'onionsurge-build-v1', 'mode': mode, 'snapshot_end': end, 'files': files}
    if schema_version is not None:
        manifest['schema_version'] = schema_version
    if release_objects is _AUTO:
        manifest['release_objects'] = sorted(name for name in files if pub.object_name(name))
    elif release_objects is not None:
        manifest['release_objects'] = release_objects
    if contract_sha256 is not None:
        manifest['query_contract_sha256'] = contract_sha256
    (directory / 'build-manifest.json').write_bytes(pub.json_bytes(manifest))


class MemoryProvider:
    def __init__(self, name, events):
        self.name = name
        self.identity = pub.sha256(name.encode())
        self.events = events
        self.objects = {}
        self.cache_control = {}
        self.fail = None

    def _check_key(self, key):
        parts = key.split('/')
        pub.require(
            key == 'manifest.json' or (
                len(parts) >= 3 and parts[0] == 'releases' and pub.ID.fullmatch(parts[1])
                and (parts[2:] == ['manifest.json']
                     or pub.object_name('/'.join(parts[2:])))),
            'unsafe_storage_key')

    def get(self, key, maximum, missing_ok=False):
        self._check_key(key)
        self.events.append(('get', self.name, key))
        raw = self.objects.get(key)
        if raw is None and missing_ok:
            return None
        if raw is None:
            raise pub.PublicationError('missing_fixture_object')
        if len(raw) > maximum:
            raise pub.PublicationError('storage_response_limit')
        return raw

    def put(self, key, raw, immutable):
        self.events.append(('put', self.name, key, immutable))
        if type(raw) is not bytes:
            raise AssertionError('Transport requires immutable validated bytes')
        if self.fail == key:
            raise pub.PublicationError('fixture_write_failed')
        self.objects[key] = raw
        self.cache_control[key] = pub.IMMUTABLE_CACHE_CONTROL if immutable else 'no-store'
        if self.fail == ('after', key):
            raise pub.PublicationError('fixture_write_outcome_unknown')

    def verify_cache_control(self, key, immutable):
        self._check_key(key)
        self.events.append(('cache-control', self.name, key, immutable))
        expected = pub.IMMUTABLE_CACHE_CONTROL if immutable else 'no-store'
        if self.cache_control.get(key) != expected:
            raise pub.PublicationError('cache_control_mismatch')


class PublicationTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.directory = Path(self.temporary.name)
        self.build = self.directory / 'build'
        self.data = fixture(self.build)
        self.state = self.directory / 'state'
        self.state.mkdir(mode=0o700)
        self.symlink_target = self.directory / 'outside.json'
        self.symlink_target.write_bytes(b'{"outside":true}\n')
        self.events = []
        self.providers = [MemoryProvider(name, self.events) for name in ('do', 'r2')]

    def prepare(self):
        return pub.prepare_candidate(self.build, self.state, 'a' * 40, 'b' * 40, now=END + 1000)

    def test_preparation_pins_actual_bytes_and_only_public_data_objects(self):
        candidate = self.prepare()
        self.assertEqual(len(candidate.manifest['objects']), 93)
        self.assertEqual(candidate.manifest['data_schema_version'], 7)
        self.assertEqual(candidate.manifest['query_contract_sha256'], CONTRACT_SHA256)
        self.assertEqual(candidate.manifest['observation_end'], END)
        self.assertEqual(candidate.manifest['generated_at'], END + 1000)
        self.assertEqual(set(candidate.manifest), {'format', 'id', 'data_schema_version', 'observation_end',
            'generated_at', 'application_commit', 'deployment_commit', 'query_contract_sha256', 'objects'})
        for name, info in candidate.manifest['objects'].items():
            self.assertNotIn(name, pub.FRONTEND)
            raw = (candidate.directory / name).read_bytes()
            self.assertEqual(info, {'sha256': pub.sha256(raw), 'bytes': len(raw)})
            self.assertEqual(raw, (self.build / name).read_bytes())
            self.assertEqual(stat.S_IMODE((candidate.directory / name).stat().st_mode), 0o600)
        self.assertEqual(stat.S_IMODE(candidate.directory.stat().st_mode), 0o700)
        self.assertNotIn(str(self.directory), json.dumps(candidate.manifest))
        # A daily-frozen reference legitimately has an older source endpoint and
        # a different source input hash from this hour's data provenance.
        self.assertEqual(pub.validate_data_objects({n: (candidate.directory / n).read_bytes()
                         for n in candidate.manifest['objects']})['end'], END)

    def test_custom_time_ranges_and_raised_object_caps_work_without_code_edits(self):
        custom_ranges = {
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
        custom_build = self.directory / 'custom-build'
        fixture(custom_build, time_ranges=custom_ranges)
        custom_candidate = pub.prepare_candidate(custom_build, self.state, 'a' * 40, 'b' * 40, now=END + 1000)
        self.assertEqual(len(custom_candidate.manifest['objects']), 41)
        self.assertIn('exports/metric0-14d.csv', custom_candidate.manifest['objects'])
        self.assertIn('exports/metric0-24mo.csv', custom_candidate.manifest['objects'])

        # 20 charts x 10 periods = 202 objects (above the old 130/140 limit, under 512/528).
        many_ranges = copy.deepcopy(DEFAULT_TIME_RANGES)
        many_ranges['periods'].extend(
            dict(DEFAULT_TIME_RANGES['periods'][1], id=f'w{i}', summary_key=f'w_{i}', export_suffix=f'w{i}')
            for i in range(3))
        large_build = self.directory / 'large-build'
        fixture(large_build, time_ranges=many_ranges, chart_count=20)
        large_candidate = pub.prepare_candidate(large_build, self.state, 'a' * 40, 'b' * 40, now=END + 1000)
        self.assertEqual(len(large_candidate.manifest['objects']), 202)
        with pub.publication_lock(self.state):
            self.assertEqual(pub.publish_candidate(large_candidate, self.state, self.providers)['status'], 'published')

        # Exceeding the 512 object cap in manifest validation and 528 file cap in build manifests are rejected.
        manifest_obj = {'sha256': 'a' * 64, 'bytes': 1}
        m_512 = dict(custom_candidate.manifest,
                     objects={'data.json': manifest_obj,
                              'reference/reference-2026-11-18-' + 'a' * 12 + '.json': manifest_obj,
                              **{f'exports/m{i}-7d.csv': manifest_obj for i in range(510)}})
        raw_512 = pub.json_bytes(m_512)
        self.assertEqual(len(pub.validate_manifest(raw_512, pub.descriptor(raw_512, m_512['id']))['objects']), 512)
        m_513 = dict(m_512, objects={**m_512['objects'], 'exports/m510-7d.csv': manifest_obj})
        raw_513 = pub.json_bytes(m_513)
        with self.assertRaisesRegex(pub.PublicationError, 'invalid_object_map'):
            pub.validate_manifest(raw_513, pub.descriptor(raw_513, m_513['id']))

        over_build = self.directory / 'over-build'
        full_ranges = copy.deepcopy(DEFAULT_TIME_RANGES)
        full_ranges['periods'].extend(
            dict(DEFAULT_TIME_RANGES['periods'][1], id=f'p{i}', summary_key=f'p_{i}', export_suffix=f'p{i}')
            for i in range(9))
        fixture(over_build, time_ranges=full_ranges, chart_count=33)  # 33*16 + 2 = 530 > 528
        with self.assertRaisesRegex(pub.PublicationError, 'invalid_build_manifest'):
            pub.prepare_candidate(over_build, self.state, 'a' * 40, 'b' * 40, now=END + 1000)

    def test_preparation_rejects_offline_untracked_hash_and_symlink_inputs(self):
        rewrite_build(self.build, mode='offline')
        with self.assertRaisesRegex(pub.PublicationError, 'live_build_required'):
            self.prepare()
        rewrite_build(self.build)
        (self.build / '.env').write_text('private fixture')
        with self.assertRaisesRegex(pub.PublicationError, 'file_mismatch'):
            self.prepare()
        (self.build / '.env').unlink()
        (self.build / 'data.json').write_bytes(b'changed')
        with self.assertRaisesRegex(pub.PublicationError, 'build_hash_mismatch'):
            self.prepare()
        (self.build / 'data.json').unlink()
        (self.build / 'data.json').symlink_to(self.symlink_target)
        with self.assertRaisesRegex(pub.PublicationError, 'nonregular_build_path'):
            self.prepare()

    def test_preparation_rejects_missing_invalid_or_mismatched_query_contract_sha256(self):
        for label, manifest_sha, data_sha in [
            ('missing-manifest-sha', None, CONTRACT_SHA256),
            ('nonhex-manifest-sha', 'not-a-sha256', CONTRACT_SHA256),
            ('mismatched-data-sha', 'f' * 64, CONTRACT_SHA256),
            ('missing-data-sha', CONTRACT_SHA256, None),
        ]:
            with self.subTest(label=label):
                bad_build = self.directory / ('contract-' + label)
                fixture(bad_build, contract_sha256=data_sha)
                if data_sha is None:
                    data = json.loads((bad_build / 'data.json').read_bytes())
                    data['provenance'].pop('query_contract_sha256', None)
                    (bad_build / 'data.json').write_bytes(pub.json_bytes(data))
                rewrite_build(bad_build, contract_sha256=manifest_sha)
                with self.assertRaisesRegex(pub.PublicationError, 'invalid_query_contract_hash'):
                    pub.prepare_candidate(bad_build, self.state, 'a' * 40, 'b' * 40, now=END + 1000)

    def test_preparation_validates_build_schema_version_and_release_objects(self):
        rel_objects = sorted(name for name in (self.build / 'build-manifest.json').read_text() and
                             json.loads((self.build / 'build-manifest.json').read_text())['files']
                             if pub.object_name(name))
        rewrite_build(self.build, release_objects=rel_objects)
        self.assertEqual(len(self.prepare().manifest['objects']), len(rel_objects))

        for bad_schema in (None, 4, 5, 6, 7.0, True):
            rewrite_build(self.build, schema_version=bad_schema, release_objects=rel_objects)
            with self.subTest(bad_schema=bad_schema), self.assertRaisesRegex(pub.PublicationError, 'unsupported_data_schema'):
                self.prepare()

        for label, bad_rel in [
            ('missing', None),
            ('not-list', 'data.json'),
            ('duplicate', [*rel_objects, 'data.json']),
            ('frontend-file', [*rel_objects, 'index.html']),
            ('missing-export', rel_objects[:-1]),
        ]:
            rewrite_build(self.build, release_objects=bad_rel)
            with self.subTest(bad_release_objects=label), self.assertRaises(pub.PublicationError):
                self.prepare()

        # Live builds omit offline-only data.js and catalogue.json and still validate cleanly.
        live_lean = self.directory / 'live-lean'
        fixture(live_lean, end=END)
        for offline_only in ('data.js', 'catalogue.json'):
            (live_lean / offline_only).unlink(missing_ok=True)
        rewrite_build(live_lean, end=END)
        lean_cand = pub.prepare_candidate(live_lean, self.state, 'a' * 40, 'b' * 40, now=END + 1000)
        self.assertNotIn('data.js', lean_cand.manifest['objects'])
        self.assertNotIn('catalogue.json', lean_cand.manifest['objects'])

    def test_candidate_hardlink_dedup_and_prune_candidates(self):
        import shutil
        commits = {'application_commit': 'a' * 40, 'deployment_commit': 'b' * 40}
        with pub.publication_lock(self.state):
            cand1 = pub.prepare_candidate(self.build, self.state, **commits, now=END + 10)
            pub.publish_candidate(cand1, self.state, self.providers)

            # Second candidate reusing identical exports & reference shares inodes while modified data.json gets a new inode.
            b2 = self.directory / 'build-2'
            fixture(b2, end=END)
            modified_data = json.loads((b2 / 'data.json').read_bytes())
            modified_data['charts'] = [{'id': 'metric' + str(n), 'series': []} for n in range(13)]
            (b2 / 'data.json').write_bytes(pub.json_bytes(modified_data))
            rewrite_build(b2, end=END)
            cand2 = pub.prepare_candidate(b2, self.state, **commits, now=END + 3610)
            csv_rel = 'exports/metric0-7d.csv'
            ref_rel = modified_data['reference_document']
            c1_csv = (cand1.directory / csv_rel).stat()
            c2_csv = (cand2.directory / csv_rel).stat()
            self.assertEqual((c1_csv.st_dev, c1_csv.st_ino), (c2_csv.st_dev, c2_csv.st_ino))
            self.assertGreaterEqual(c2_csv.st_nlink, 2)
            c1_ref = (cand1.directory / ref_rel).stat()
            c2_ref = (cand2.directory / ref_rel).stat()
            self.assertEqual((c1_ref.st_dev, c1_ref.st_ino), (c2_ref.st_dev, c2_ref.st_ino))
            self.assertNotEqual((cand1.directory / 'data.json').stat().st_ino,
                                (cand2.directory / 'data.json').stat().st_ino)
            pub.publish_candidate(cand2, self.state, self.providers)

            # Third and fourth candidates advance current & previous so cand1 & cand2 become prunable.
            b3 = self.directory / 'build-3'
            fixture(b3, end=END + 3600)
            cand3 = pub.prepare_candidate(b3, self.state, **commits, now=END + 7210)
            pub.publish_candidate(cand3, self.state, self.providers)

            b4 = self.directory / 'build-4'
            fixture(b4, end=END + 7200)
            cand4 = pub.prepare_candidate(b4, self.state, **commits, now=END + 10810)
            pub.publish_candidate(cand4, self.state, self.providers)

            # Incomplete candidate directory with RELEASE_DIR shape but without manifest.json is never pruned.
            incomplete_id = '20261118T000000Z-000000000000'
            incomplete = self.state / 'candidates' / incomplete_id
            incomplete.mkdir()
            (incomplete / 'partial.tmp').write_text('keep\n')

            removed = pub.prune_candidates(self.state, retain_runs=2)
            c1_id, c2_id = cand1.manifest['id'], cand2.manifest['id']
            c3_id, c4_id = cand3.manifest['id'], cand4.manifest['id']
            self.assertEqual(set(removed), {c1_id, c2_id})
            remaining = {p.name for p in (self.state / 'candidates').iterdir()}
            self.assertEqual(remaining, {c3_id, c4_id, incomplete_id})
            # Shared CSV file in cand3 remains readable after pruning older donors.
            self.assertEqual((cand3.directory / csv_rel).read_bytes(), b'timestamp,value\n1,2\n')
            self.assertGreaterEqual((cand3.directory / csv_rel).stat().st_nlink, 1)

            # Verify extra completed candidate beyond retain_runs=3 keeps 1 extra newest alongside current & previous.
            b5 = self.directory / 'build-5'
            fixture(b5, end=END + 10800)
            cand5 = pub.prepare_candidate(b5, self.state, **commits, now=END + 14410)
            pub.publish_candidate(cand5, self.state, self.providers)
            b6 = self.directory / 'build-6'
            fixture(b6, end=END + 14400)
            cand6 = pub.prepare_candidate(b6, self.state, **commits, now=END + 18010)
            pub.publish_candidate(cand6, self.state, self.providers)
            removed2 = pub.prune_candidates(self.state, retain_runs=3)
            self.assertEqual(removed2, [c3_id])
            self.assertEqual(
                {p.name for p in (self.state / 'candidates').iterdir()},
                {c4_id, cand5.manifest['id'], cand6.manifest['id'], incomplete_id},
            )
            shutil.rmtree(incomplete)

    def test_reference_identity_schema_and_object_set_are_checked(self):
        candidate = self.prepare()
        objects = {name: (candidate.directory / name).read_bytes() for name in candidate.manifest['objects']}
        for change in ({'schema_version': 4}, {'schema_version': 5}, {'schema_version': 6},
                       {'window_seconds': 7200},
                       {'window_seconds': 86400, 'end': END + 3600, 'start': END + 3600 - 86400},
                       {'end': END + 1800, 'start': END - 84600},
                       {'time_ranges': []},
                       {'time_ranges': {'periods': []}},
                       {'time_ranges': {'periods': [{'export_suffix': '1h'}]}},
                       {'time_ranges': {'periods': [{'export_suffix': '7d'}, {'export_suffix': '7d'}]}},
                       {'reference_sha256': '0' * 64},
                       {'reference_id': 'wrong'}, {'reference_cutoff': 1}, {'provenance': []},
                       {'provenance': {'reference_method_sha256': 'f' * 64}}, {'operator_notes': 'private'},
                       {'window_seconds': 86400},
                       {'window_seconds': 86400, 'charts': [{'id': 'metric0', 'series': [{'values': []}]}]},
                       {'window_seconds': 86400, 'charts': [{'id': 'metric0', 'series': [{'current': {'hour': {}}}]}]}):
            mutated = dict(objects)
            mutated['data.json'] = pub.json_bytes(dict(self.data, **change))
            with self.subTest(change=change), self.assertRaises(pub.PublicationError):
                pub.validate_data_objects(mutated)
        for valid_change in (
            {'charts': [{'id': 'metric' + str(n), 'series': [{'values': [], 'current': {'hour': {}}, 'history': {'sampled_hour': []}}]}
                        for n in range(13)]},
        ):
            self.assertEqual(pub.validate_data_objects(dict(objects, **{
                'data.json': pub.json_bytes(dict(self.data, **valid_change))}))['schema_version'], 7)

        # Daily schema_version 7 (window_seconds=86400) succeeds without 24h hours periods or -24h.csv.
        daily_ranges = copy.deepcopy(DEFAULT_TIME_RANGES)
        daily_ranges['periods'] = daily_ranges['periods'][1:]
        daily_ranges['default_range'] = '1mo'
        daily_ranges['card_slots'] = {'fallback_dynamic': '1mo', 'fixed': ['6mo', 'all']}
        daily_build = self.directory / 'daily-v7-build'
        daily_data = fixture(daily_build, time_ranges=daily_ranges)
        daily_data['window_seconds'] = 86400
        (daily_build / 'data.json').write_bytes(pub.json_bytes(daily_data))
        rewrite_build(daily_build, end=END)
        daily_cand = pub.prepare_candidate(daily_build, self.state, 'a' * 40, 'b' * 40, now=END + 1000)
        self.assertEqual(daily_cand.manifest['data_schema_version'], 7)
        self.assertFalse(any(name.endswith('-24h.csv') for name in daily_cand.manifest['objects']))
        missing_tr = dict(self.data)
        missing_tr.pop('time_ranges')
        with self.assertRaisesRegex(pub.PublicationError, 'invalid_time_ranges'):
            pub.validate_data_objects(dict(objects, **{'data.json': pub.json_bytes(missing_tr)}))
        self.assertTrue(pub.object_name('exports/metric0-24h.csv'))
        self.assertTrue(pub.object_name('exports/connection-rate-24h.csv'))
        for bad_suffix in ('0d', '1d', '0h', '1h', '23h', '024h', '30m', '1m', '60s', 'foo-1h'):
            mutated = dict(objects)
            csv_name = next(name for name in mutated if name.endswith('-1mo.csv'))
            mutated[f'exports/metric0-{bad_suffix}.csv'] = mutated.pop(csv_name)
            with self.subTest(bad_suffix=bad_suffix), self.assertRaisesRegex(pub.PublicationError, 'incomplete_release_objects'):
                pub.validate_data_objects(mutated)
            self.assertFalse(pub.object_name(f'exports/metric0-{bad_suffix}.csv'))
            self.assertFalse(pub.object_name(f'exports/connection-rate-{bad_suffix}.csv'))
        mutated = dict(objects, **{'data.js': b'not JSON data'})
        with self.assertRaisesRegex(pub.PublicationError, 'incomplete_release_objects'):
            pub.validate_data_objects(mutated)

        collide_ranges = copy.deepcopy(DEFAULT_TIME_RANGES)
        collide_ranges['periods'].append(
            dict(collide_ranges['periods'][1], id='rate_7d', summary_key='rate_7d', export_suffix='rate-7d'))
        collide_data = dict(self.data, time_ranges=collide_ranges,
                            charts=[{'id': 'connection'}, {'id': 'connection-rate'}])
        collide_objects = {'data.json': pub.json_bytes(collide_data),
                           self.data['reference_document']: objects[self.data['reference_document']]}
        for cid in ('connection', 'connection-rate'):
            for period in collide_ranges['periods']:
                collide_objects[f"exports/{cid}-{period['export_suffix']}.csv"] = b'timestamp,value\n1,2\n'
        with self.assertRaisesRegex(pub.PublicationError, 'duplicate_export_path'):
            pub.validate_data_objects(collide_objects)

    def test_observation_interval_progression_requires_full_utc_hour_when_advancing(self):
        # Reference window end must not exceed data['start'].
        candidate = self.prepare()
        objects = {name: (candidate.directory / name).read_bytes() for name in candidate.manifest['objects']}
        bad_window = {'date': '2026-11-18', 'end': END, 'start': END - 10 * 86400}
        bad_ref = {'window': bad_window, 'method_sha256': 'a' * 64, 'source_input_sha256': 'b' * 64,
                   'source_snapshot_end': END, 'distributions': {}}
        raw_ref = pub.json_bytes(bad_ref)
        bad_id = 'reference-' + bad_window['date'] + '-' + pub.sha256(raw_ref)[:12]
        bad_name = 'reference/' + bad_id + '.json'
        bad_data = dict(self.data, reference_window=bad_window, reference_cutoff=END,
                        reference_document=bad_name, reference_id=bad_id, reference_sha256=pub.sha256(raw_ref))
        mutated = dict(objects)
        mutated.pop(self.data['reference_document'])
        mutated[bad_name] = raw_ref
        mutated['data.json'] = pub.json_bytes(bad_data)
        with self.assertRaisesRegex(pub.PublicationError, 'invalid_reference_window'):
            pub.validate_data_objects(mutated)

        hour_two_build = self.directory / 'build-hour-two'
        fixture(hour_two_build, end=END + 3600)
        first = pub.prepare_candidate(hour_two_build, self.state, 'a' * 40, 'b' * 40, now=END + 7200)
        pub.publish_candidate(first, self.state, self.providers)
        older = self.prepare()
        with self.assertRaisesRegex(pub.PublicationError, 'observation_interval_too_short'):
            pub.publish_candidate(older, self.state, self.providers)
        hour_three_build = self.directory / 'build-hour-three'
        fixture(hour_three_build, end=END + 7200)
        third = pub.prepare_candidate(hour_three_build, self.state, 'a' * 40, 'b' * 40, now=END + 7300)
        self.assertEqual(pub.publish_candidate(third, self.state, self.providers)['status'], 'published')

    def test_forward_transition_from_schema6_release_and_recovery_preserves_previous_v6(self):
        v6_ranges = copy.deepcopy(DEFAULT_TIME_RANGES)
        v6_ranges['periods'] = v6_ranges['periods'][1:]
        v6_ranges['default_range'] = '1mo'
        v6_ranges['card_slots'] = {'fallback_dynamic': '1mo', 'fixed': ['6mo', 'all']}
        window = {'date': '2026-11-18', 'end': END - 86400, 'start': END - 10 * 86400}
        ref = {'window': window, 'method_sha256': 'a' * 64, 'source_input_sha256': 'b' * 64,
               'source_snapshot_end': END - 86400, 'distributions': {}}
        raw_ref = pub.json_bytes(ref)
        ref_id = 'reference-' + window['date'] + '-' + pub.sha256(raw_ref)[:12]
        ref_name = 'reference/' + ref_id + '.json'
        v6_data = {'schema_version': 6, 'window_seconds': 86400, 'start': END - 86400, 'end': END,
                   'time_ranges': v6_ranges, 'charts': [{'id': 'metric' + str(n)} for n in range(13)],
                   'reference_window': window, 'reference_cutoff': window['end'],
                   'reference_document': ref_name, 'reference_id': ref_id, 'reference_sha256': pub.sha256(raw_ref),
                   'provenance': {'reference_method_sha256': 'a' * 64, 'aggregate_input_sha256': 'c' * 64}}
        v6_objects = {'data.json': pub.json_bytes(v6_data), ref_name: raw_ref}
        for chart in v6_data['charts']:
            for period in v6_ranges['periods']:
                v6_objects[f"exports/{chart['id']}-{period['export_suffix']}.csv"] = b'timestamp,value\n1,2\n'
        v6_id = '20261118T002000Z-' + 'd' * 12
        v6_manifest = {'format': 'onionsurge-release-v1', 'id': v6_id, 'data_schema_version': 6,
                       'observation_end': END, 'generated_at': END + 60,
                       'application_commit': 'a' * 40, 'deployment_commit': 'b' * 40,
                       'query_contract_sha256': 'c' * 64,
                       'objects': {k: {'sha256': pub.sha256(v), 'bytes': len(v)} for k, v in sorted(v6_objects.items())}}
        raw_manifest = pub.json_bytes(v6_manifest)
        v6_desc = pub.descriptor(raw_manifest, v6_id)
        v6_root = {'format': 'onionsurge-root-v1', 'updated_at': END + 60, 'current': v6_desc, 'previous': None}
        raw_root = pub.json_bytes(v6_root)
        for provider in self.providers:
            for k, v in v6_objects.items():
                provider.put('releases/' + v6_id + '/' + k, v, immutable=True)
            provider.put('releases/' + v6_id + '/manifest.json', raw_manifest, immutable=True)
            provider.put('manifest.json', raw_root, immutable=False)
        pub.atomic_private(self.state / 'published.json', pub.json_bytes({
            'destinations': sorted(p.identity for p in self.providers), 'root': v6_root}))

        # Schema-7 rollover (without -24h.csv in previous) retains schema-6 fallback across publish and recovery.
        candidate = self.prepare()
        self.providers[1].fail = ('after', 'manifest.json')
        with self.assertRaisesRegex(pub.PublicationError, 'promotion_uncertain'):
            pub.publish_candidate(candidate, self.state, self.providers)
        self.providers[1].fail = None
        recovered = pub.recover_promotion(self.state, self.providers, writers_fenced=True)
        self.assertEqual(recovered['status'], 'recovered')
        self.assertEqual(pub.read_receipt(self.state / 'published.json')['root']['previous'], v6_desc)

    def test_limits_fail_without_truncating_or_creating_a_completed_candidate(self):
        with mock.patch.object(pub, 'MAX_OBJECT', 4):
            with self.assertRaises(pub.PublicationError):
                self.prepare()
        with mock.patch.object(pub, 'MAX_CANDIDATE', 100):
            with self.assertRaisesRegex(pub.PublicationError, 'candidate_limit'):
                self.prepare()
        self.assertFalse((self.state / 'candidates').exists())

    def test_all_provider_objects_verify_before_either_root_and_previous_is_retained(self):
        first = self.prepare()
        with pub.publication_lock(self.state):
            result = pub.publish_candidate(first, self.state, self.providers)
        self.assertEqual(result['status'], 'published')
        root_put = next(i for i, e in enumerate(self.events) if e[0] == 'put' and e[2] == 'manifest.json')
        for provider in self.providers:
            for name in [*first.manifest['objects'], 'manifest.json']:
                key = 'releases/' + first.manifest['id'] + '/' + name
                uploaded = self.events.index(('put', provider.name, key, True))
                self.assertLess(uploaded, root_put)
                self.assertIn(('get', provider.name, key), self.events[uploaded + 1:root_put],
                              'each uploaded object must be read back after its PUT and before root promotion')
        prior_objects = [dict(provider.objects) for provider in self.providers]
        second = self.prepare()
        with pub.publication_lock(self.state):
            pub.publish_candidate(second, self.state, self.providers)
        for provider, old in zip(self.providers, prior_objects):
            root = pub.validate_root(provider.objects['manifest.json'])
            self.assertEqual(root['current'], second.descriptor)
            self.assertEqual(root['previous'], first.descriptor)
            for key, raw in old.items():
                if key != 'manifest.json':
                    self.assertEqual(provider.objects[key], raw)
        self.assertFalse((self.state / 'pending-promotion.json').exists())

    def test_immutable_upload_failure_and_conflict_never_move_roots(self):
        candidate = self.prepare()
        key = 'releases/' + candidate.manifest['id'] + '/data.json'
        self.providers[1].fail = key
        with self.assertRaisesRegex(pub.PublicationError, 'fixture_write_failed'):
            pub.publish_candidate(candidate, self.state, self.providers)
        self.assertTrue(all('manifest.json' not in p.objects for p in self.providers))
        self.assertFalse((self.state / 'pending-promotion.json').exists())
        self.providers[1].fail = None
        self.providers[0].objects[key] = b'wrong'
        with self.assertRaisesRegex(pub.PublicationError, 'immutable_object_conflict'):
            pub.publish_candidate(candidate, self.state, self.providers)
        self.assertEqual(self.providers[0].objects[key], b'wrong')

    def test_readback_corruption_prevents_promotion(self):
        candidate = self.prepare()
        provider = self.providers[0]
        original = provider.put
        def corrupted(key, source, immutable):
            original(key, source, immutable)
            if key.endswith('/data.json'):
                provider.objects[key] = b'bad'
        with mock.patch.object(provider, 'put', side_effect=corrupted), self.assertRaises(pub.PublicationError):
            pub.publish_candidate(candidate, self.state, self.providers)
        self.assertTrue(all('manifest.json' not in p.objects for p in self.providers))

    def test_missing_root_cache_control_keeps_promotion_pending_despite_matching_bytes(self):
        candidate = self.prepare()
        provider = self.providers[0]
        actual = provider.put
        def omit_root_header(key, raw, immutable):
            actual(key, raw, immutable)
            if key == 'manifest.json':
                provider.cache_control.pop(key)
        with mock.patch.object(provider, 'put', side_effect=omit_root_header):
            with self.assertRaisesRegex(pub.PublicationError, 'promotion_uncertain'):
                pub.publish_candidate(candidate, self.state, self.providers)
        pending = pub.read_receipt(self.state / 'pending-promotion.json')
        self.assertEqual(provider.objects['manifest.json'], pub.json_bytes(pending['root']))
        self.assertFalse((self.state / 'published.json').exists())
        with self.assertRaisesRegex(pub.PublicationError, 'cache_control_mismatch'):
            pub.recover_promotion(self.state, self.providers, writers_fenced=True)

    def test_wrong_immutable_cache_control_stops_before_any_root_write(self):
        candidate = self.prepare()
        provider = self.providers[1]
        actual = provider.put
        def omit_cache(key, raw, immutable):
            actual(key, raw, immutable)
            if key.endswith('/data.json'):
                provider.cache_control[key] = 'no-store'
        with mock.patch.object(provider, 'put', side_effect=omit_cache):
            with self.assertRaisesRegex(pub.PublicationError, 'cache_control_mismatch'):
                pub.publish_candidate(candidate, self.state, self.providers)
        self.assertTrue(all('manifest.json' not in p.objects for p in self.providers))

    def test_candidate_replacement_during_lookup_cannot_publish_unvalidated_bytes(self):
        candidate = self.prepare()
        provider = self.providers[0]
        key = 'releases/' + candidate.manifest['id'] + '/data.json'
        expected = (candidate.directory / 'data.json').read_bytes()
        original = provider.get
        replaced = False
        def replace_candidate(name, maximum, missing_ok=False):
            nonlocal replaced
            if name == key and missing_ok and not replaced:
                replaced = True
                (candidate.directory / 'data.json').write_bytes(b'PRIVATE_UNVALIDATED_FIXTURE')
            return original(name, maximum, missing_ok)
        with mock.patch.object(provider, 'get', side_effect=replace_candidate):
            result = pub.publish_candidate(candidate, self.state, [provider])
        self.assertEqual(result['status'], 'published')
        self.assertEqual(provider.objects[key], expected)
        self.assertNotIn(b'PRIVATE_UNVALIDATED_FIXTURE', provider.objects.values())

    def test_root_upload_uses_encoded_bytes_even_if_saved_root_file_is_replaced(self):
        candidate = self.prepare()
        actual = pub.atomic_private
        def replace_root(path, raw):
            actual(path, raw)
            if path.name == 'pending-promotion.json':
                (candidate.directory / 'root.json').write_bytes(b'PRIVATE_UNVALIDATED_ROOT')
        with mock.patch.object(pub, 'atomic_private', side_effect=replace_root):
            pub.publish_candidate(candidate, self.state, self.providers)
        expected = pub.json_bytes(pub.read_receipt(self.state / 'published.json')['root'])
        for provider in self.providers:
            self.assertEqual(provider.objects['manifest.json'], expected)

    def test_confirmed_publication_reports_cleanup_warning_without_false_uncertainty(self):
        candidate = self.prepare()
        actual = pub.sync_directory
        def fail_final_sync(path):
            if path == self.state and (path / 'published.json').exists() and not (path / 'pending-promotion.json').exists():
                raise OSError('fixture final sync failure')
            actual(path)
        with mock.patch.object(pub, 'sync_directory', side_effect=fail_final_sync):
            result = pub.publish_candidate(candidate, self.state, self.providers)
        self.assertEqual(result['status'], 'published')
        self.assertEqual(result['warning'], 'pending_marker_removal_not_durable')
        self.assertFalse((self.state / 'pending-promotion.json').exists())
        self.assertEqual(pub.read_receipt(self.state / 'published.json')['root']['current'], candidate.descriptor)

    def test_confirmed_publication_with_retained_marker_still_reports_its_success(self):
        candidate = self.prepare()
        actual = Path.unlink
        def fail_marker(path, *args, **kwargs):
            if path.name == 'pending-promotion.json':
                raise PermissionError('fixture unlink failure')
            return actual(path, *args, **kwargs)
        with mock.patch.object(Path, 'unlink', fail_marker):
            result = pub.publish_candidate(candidate, self.state, self.providers)
        self.assertEqual(result['status'], 'published')
        self.assertEqual(result['warning'], 'pending_marker_cleanup_failed')
        self.assertTrue((self.state / 'pending-promotion.json').exists())

    def test_unmanaged_roots_and_missing_providers_fail_before_upload(self):
        candidate = self.prepare()
        with self.assertRaisesRegex(pub.PublicationError, 'no_enabled_storage'):
            pub.publish_candidate(candidate, self.state, [])
        self.providers[0].objects['manifest.json'] = b'unmanaged root'
        with self.assertRaisesRegex(pub.PublicationError, 'provider_root_drift'):
            pub.publish_candidate(candidate, self.state, self.providers)
        self.assertFalse(any(event[0] == 'put' for event in self.events))
        self.assertFalse((self.state / 'pending-promotion.json').exists())

    def test_uncertain_root_write_blocks_automatic_promotion_until_fenced_recovery(self):
        candidate = self.prepare()
        self.providers[1].fail = ('after', 'manifest.json')
        with self.assertRaisesRegex(pub.PublicationError, 'promotion_uncertain'):
            pub.publish_candidate(candidate, self.state, self.providers)
        marker = self.state / 'pending-promotion.json'
        self.assertTrue(marker.exists())
        self.assertEqual(stat.S_IMODE(marker.stat().st_mode), 0o600)
        with self.assertRaisesRegex(pub.PublicationError, 'blocked_pending_recovery'):
            pub.publish_candidate(self.prepare(), self.state, self.providers)
        with self.assertRaisesRegex(pub.PublicationError, 'writer_fence_required'):
            pub.recover_promotion(self.state, self.providers)
        with pub.publication_lock(self.state):
            recovered = pub.recover_promotion(self.state, self.providers, writers_fenced=True)
        self.assertEqual(recovered['status'], 'recovered')
        self.assertFalse(marker.exists())

    def test_mixed_root_recovery_and_changed_destination_remain_blocked(self):
        candidate = self.prepare()
        self.providers[1].fail = 'manifest.json'
        with self.assertRaisesRegex(pub.PublicationError, 'promotion_uncertain'):
            pub.publish_candidate(candidate, self.state, self.providers)
        with self.assertRaises(pub.PublicationError):
            pub.recover_promotion(self.state, self.providers, writers_fenced=True)
        self.assertTrue((self.state / 'pending-promotion.json').exists())
        self.providers[0].identity = 'changed'
        with self.assertRaisesRegex(pub.PublicationError, 'storage_destination_changed'):
            pub.recover_promotion(self.state, self.providers, writers_fenced=True)

    def test_crash_after_root_confirmation_retains_pending_fence(self):
        candidate = self.prepare()
        actual = pub.atomic_private
        def fail_receipt(path, raw):
            if path.name == 'published.json':
                raise OSError('fixture disk failure')
            actual(path, raw)
        with mock.patch.object(pub, 'atomic_private', side_effect=fail_receipt), self.assertRaisesRegex(pub.PublicationError, 'promotion_uncertain'):
            pub.publish_candidate(candidate, self.state, self.providers)
        self.assertTrue((self.state / 'pending-promotion.json').exists())
        self.assertTrue(all('manifest.json' in p.objects for p in self.providers))

    def test_pending_recovery_handles_old_or_absent_roots_only_after_operator_convergence(self):
        for had_previous in [False, True]:
            with self.subTest(had_previous=had_previous):
                if had_previous:
                    pub.publish_candidate(self.prepare(), self.state, self.providers)
                candidate = self.prepare()
                before = [provider.objects.get('manifest.json') for provider in self.providers]
                self.providers[0].fail = 'manifest.json'
                with self.assertRaisesRegex(pub.PublicationError, 'promotion_uncertain'):
                    pub.publish_candidate(candidate, self.state, self.providers)
                self.assertEqual([provider.objects.get('manifest.json') for provider in self.providers], before)
                with self.assertRaises(pub.PublicationError):
                    pub.recover_promotion(self.state, self.providers, writers_fenced=True)
                pending = pub.read_receipt(self.state / 'pending-promotion.json')
                # Simulate the separate, fenced operator root-write operation.
                for provider in self.providers:
                    provider.fail = None
                    provider.objects['manifest.json'] = pub.json_bytes(pending['root'])
                    provider.cache_control['manifest.json'] = 'no-store'
                result = pub.recover_promotion(self.state, self.providers, writers_fenced=True)
                self.assertEqual(result['release_id'], candidate.manifest['id'])

    def test_single_writer_and_private_state_boundary(self):
        with pub.publication_lock(self.state):
            with self.assertRaisesRegex(pub.PublicationError, 'publication_busy'), pub.publication_lock(self.state):
                pass
        self.state.chmod(0o755)
        with self.assertRaisesRegex(pub.PublicationError, 'unsafe_state_directory'), pub.publication_lock(self.state):
            pass

    def copied_destination(self):
        first = self.prepare()
        pub.publish_candidate(first, self.state, self.providers)
        second = self.prepare()
        pub.publish_candidate(second, self.state, self.providers)
        destination = MemoryProvider('new-storage', self.events)
        destination.objects = dict(self.providers[0].objects)
        destination.cache_control = dict(self.providers[0].cache_control)
        return first, second, [destination, self.providers[1]]

    def test_destination_migration_verifies_current_previous_and_preserves_receipt_without_writes(self):
        first, second, destinations = self.copied_destination()
        prior = (self.state / 'published.json').read_bytes()
        self.events.clear()
        with self.assertRaisesRegex(pub.PublicationError, 'operator_writer_fence_required'):
            pub.migrate_destinations(self.state, destinations)
        with pub.publication_lock(self.state):
            result = pub.migrate_destinations(self.state, destinations, writers_fenced=True)
        self.assertEqual(result, {'status': 'destination_migrated', 'release_id': second.manifest['id']})
        self.assertFalse(any(event[0] == 'put' for event in self.events))
        for provider in destinations:
            for candidate in [first, second]:
                for name in candidate.manifest['objects']:
                    self.assertIn(('get', provider.name, 'releases/' + candidate.manifest['id'] + '/' + name), self.events)
        records = list(self.state.glob('destination-migration-*.json'))
        self.assertEqual(len(records), 1)
        saved = json.loads(records[0].read_bytes())
        self.assertEqual(saved['previous_receipt'], json.loads(prior))
        self.assertEqual(saved['new_receipt'], pub.read_receipt(self.state / 'published.json'))
        self.assertEqual(stat.S_IMODE(records[0].stat().st_mode), 0o600)
        self.assertEqual(pub.migrate_destinations(self.state, destinations, True)['status'], 'destination_unchanged')
        self.assertEqual(len(list(self.state.glob('destination-migration-*.json'))), 1)

    def test_migration_pending_missing_previous_or_different_root_preserves_old_receipt(self):
        first, _, destinations = self.copied_destination()
        prior = (self.state / 'published.json').read_bytes()
        pending = self.state / 'pending-promotion.json'; pending.write_text('{}')
        with self.assertRaisesRegex(pub.PublicationError, 'promotion_blocked'):
            pub.migrate_destinations(self.state, destinations, True)
        pending.unlink()
        key = 'releases/' + first.manifest['id'] + '/data.json'
        old = destinations[0].objects.pop(key)
        with self.assertRaises(pub.PublicationError):
            pub.migrate_destinations(self.state, destinations, True)
        destinations[0].objects[key] = old
        destinations[0].objects['manifest.json'] = b'different root'
        with self.assertRaisesRegex(pub.PublicationError, 'migration_roots_disagree'):
            pub.migrate_destinations(self.state, destinations, True)
        self.assertEqual((self.state / 'published.json').read_bytes(), prior)
        self.assertEqual(list(self.state.glob('destination-migration-*.json')), [])

    def test_migration_keeps_durable_prior_receipt_if_final_state_update_fails(self):
        _, _, destinations = self.copied_destination()
        prior = (self.state / 'published.json').read_bytes()
        actual = pub.atomic_private
        def fail_receipt(path, raw):
            if path.name == 'published.json':
                raise OSError('fixture state failure')
            actual(path, raw)
        with mock.patch.object(pub, 'atomic_private', side_effect=fail_receipt), self.assertRaises(OSError):
            pub.migrate_destinations(self.state, destinations, True)
        self.assertEqual((self.state / 'published.json').read_bytes(), prior)
        record = next(self.state.glob('destination-migration-*.json'))
        self.assertEqual(json.loads(record.read_bytes())['previous_receipt'], json.loads(prior))

    def test_fenced_rollback_reconciles_receipt_and_allows_the_next_normal_publication(self):
        first = self.prepare(); pub.publish_candidate(first, self.state, self.providers)
        second = self.prepare(); pub.publish_candidate(second, self.state, self.providers)
        restored = self.providers[0].objects['manifest.json']
        latest = self.prepare(); pub.publish_candidate(latest, self.state, self.providers)
        prior = pub.read_receipt(self.state / 'published.json')
        for provider in self.providers:
            provider.objects['manifest.json'] = restored
        self.events.clear()
        with self.assertRaisesRegex(pub.PublicationError, 'writer_fence_required'):
            pub.reconcile_root(self.state, self.providers, restored)
        with pub.publication_lock(self.state):
            result = pub.reconcile_root(self.state, self.providers, restored, writers_fenced=True)
        self.assertEqual(result, {'status': 'root_reconciled', 'release_id': second.manifest['id']})
        self.assertFalse(any(event[0] == 'put' for event in self.events))
        for provider in self.providers:
            for candidate in [first, second]:
                for name in [*candidate.manifest['objects'], 'manifest.json']:
                    self.assertIn(('get', provider.name, 'releases/' + candidate.manifest['id'] + '/' + name), self.events)
        record = next(self.state.glob('root-reconciliation-*.json'))
        self.assertEqual(json.loads(record.read_bytes())['previous_receipt'], prior)
        self.assertEqual(stat.S_IMODE(record.stat().st_mode), 0o600)
        newest = self.prepare()
        self.assertEqual(pub.publish_candidate(newest, self.state, self.providers)['status'], 'published')
        self.assertEqual(pub.read_receipt(self.state / 'published.json')['root']['previous'], second.descriptor)

    def test_rollback_refuses_pending_mismatched_noncanonical_and_unsupported_roots(self):
        candidate = self.prepare(); pub.publish_candidate(candidate, self.state, self.providers)
        restored = self.providers[0].objects['manifest.json']
        before = (self.state / 'published.json').read_bytes()
        pending = self.state / 'pending-promotion.json'; pending.write_text('{}')
        with self.assertRaisesRegex(pub.PublicationError, 'pending_recovery'):
            pub.reconcile_root(self.state, self.providers, restored, True)
        pending.unlink()
        with self.assertRaisesRegex(pub.PublicationError, 'canonical_root_required'):
            pub.reconcile_root(self.state, self.providers, json.dumps(json.loads(restored), indent=2).encode(), True)
        self.providers[1].objects['manifest.json'] = b'different root'
        with self.assertRaisesRegex(pub.PublicationError, 'rollback_roots_disagree'):
            pub.reconcile_root(self.state, self.providers, restored, True)
        self.providers[1].objects['manifest.json'] = restored
        for bad_version in (4, 5, 6):
            manifest = dict(candidate.manifest, data_schema_version=bad_version)
            raw = pub.json_bytes(manifest)
            root = json.loads(restored); root['current'] = pub.descriptor(raw, manifest['id'])
            new_root = pub.json_bytes(root)
            for provider in self.providers:
                provider.objects['manifest.json'] = new_root
                provider.objects['releases/' + manifest['id'] + '/manifest.json'] = raw
            with self.assertRaisesRegex(pub.PublicationError, 'invalid_release_manifest'):
                pub.reconcile_root(self.state, self.providers, new_root, True)
        self.assertEqual((self.state / 'published.json').read_bytes(), before)
        self.assertEqual(list(self.state.glob('root-reconciliation-*.json')), [])

    def test_parse_json_rejects_non_finite_and_nesting_bypass(self):
        with self.assertRaisesRegex(pub.PublicationError, 'invalid_json'):
            pub.parse_json(b'{"v":1e400}')
        with self.assertRaisesRegex(pub.PublicationError, 'invalid_json'):
            pub.validate_root(b'{"v":1e400}')
        self.assertEqual(pub.parse_json(('{"a": "' + '[' * 70 + '"}').encode('utf-8')), {'a': '[' * 70})
        with self.assertRaisesRegex(pub.PublicationError, 'invalid_json'):
            pub.parse_json(('{"a": "' + ']' * 100 + '", "b": ' + '[' * 120 + '1' + ']' * 120 + '}').encode('utf-8'))

    def test_previous_v6_forbids_hourly_fields_hours_periods_and_24h_csv_and_rejects_v4_v5(self):
        v6_ranges = copy.deepcopy(DEFAULT_TIME_RANGES)
        v6_ranges['periods'] = v6_ranges['periods'][1:]
        v6_ranges['default_range'] = '1mo'
        v6_ranges['card_slots'] = {'fallback_dynamic': '1mo', 'fixed': ['6mo', 'all']}
        window = {'date': '2026-11-18', 'end': END - 86400, 'start': END - 10 * 86400}
        ref = {'window': window, 'method_sha256': 'a' * 64, 'source_input_sha256': 'b' * 64,
               'source_snapshot_end': END - 86400, 'distributions': {}}
        raw_ref = pub.json_bytes(ref)
        ref_id = 'reference-' + window['date'] + '-' + pub.sha256(raw_ref)[:12]
        ref_name = 'reference/' + ref_id + '.json'
        v6_base = {'schema_version': 6, 'window_seconds': 86400, 'start': END - 86400, 'end': END,
                   'time_ranges': v6_ranges, 'charts': [{'id': 'metric0', 'series': [{'key': 'metric0'}]}],
                   'reference_window': window, 'reference_cutoff': window['end'],
                   'reference_document': ref_name, 'reference_id': ref_id, 'reference_sha256': pub.sha256(raw_ref),
                   'provenance': {'reference_method_sha256': 'a' * 64}}
        v6_base_objs = {'data.json': pub.json_bytes(v6_base), ref_name: raw_ref,
                        **{f"exports/metric0-{p['export_suffix']}.csv": b'timestamp,value\n1,2\n'
                           for p in v6_ranges['periods']}}
        self.assertEqual(pub.validate_data_objects(v6_base_objs, allow_previous_v6=True)['schema_version'], 6)

        for unsupported in (4, 5):
            bad_objs = dict(v6_base_objs, **{'data.json': pub.json_bytes(dict(v6_base, schema_version=unsupported))})
            with self.subTest(unsupported_previous_schema=unsupported), self.assertRaisesRegex(pub.PublicationError, 'unsupported_data_schema'):
                pub.validate_data_objects(bad_objs, allow_previous_v6=True)

        for key in ('hour', 'sampled_hour', 'hourly_relays', 'hourly_expected_relays', 'current24'):
            bad_v6 = dict(v6_base, charts=[{'id': 'metric0', 'series': [{'key': 'metric0', key: {}}]}])
            with self.subTest(forbidden_v6_key=key), self.assertRaisesRegex(pub.PublicationError, 'forbidden_sub_24h_field'):
                pub.validate_data_objects(dict(v6_base_objs, **{'data.json': pub.json_bytes(bad_v6)}),
                                          allow_previous_v6=True)
        bad_v6_values = dict(v6_base, charts=[{'id': 'metric0', 'series': [{'values': []}]}])
        with self.assertRaisesRegex(pub.PublicationError, 'forbidden_sub_24h_field'):
            pub.validate_data_objects(dict(v6_base_objs, **{'data.json': pub.json_bytes(bad_v6_values)}),
                                      allow_previous_v6=True)
        bad_v6_hours_tr = dict(v6_base, time_ranges=copy.deepcopy(DEFAULT_TIME_RANGES))
        bad_v6_hours_objs = {'data.json': pub.json_bytes(bad_v6_hours_tr), ref_name: raw_ref,
                             **{f"exports/metric0-{p['export_suffix']}.csv": b'timestamp,value\n1,2\n'
                                for p in DEFAULT_TIME_RANGES['periods']}}
        with self.assertRaisesRegex(pub.PublicationError, 'invalid_time_ranges'):
            pub.validate_data_objects(bad_v6_hours_objs, allow_previous_v6=True)

        bad_v6_suffix_tr = copy.deepcopy(v6_ranges)
        bad_v6_suffix_tr['periods'][0]['export_suffix'] = '24h'
        bad_v6_suffix_objs = {'data.json': pub.json_bytes(dict(v6_base, time_ranges=bad_v6_suffix_tr)), ref_name: raw_ref,
                              **{f"exports/metric0-{p['export_suffix']}.csv": b'timestamp,value\n1,2\n'
                                 for p in bad_v6_suffix_tr['periods']}}
        with self.assertRaisesRegex(pub.PublicationError, 'invalid_time_ranges'):
            pub.validate_data_objects(bad_v6_suffix_objs, allow_previous_v6=True)

        v6_manifest_24h = {
            'format': 'onionsurge-release-v1', 'id': '20261118T002000Z-' + 'd' * 12, 'data_schema_version': 6,
            'observation_end': END, 'generated_at': END + 60, 'application_commit': 'a' * 40,
            'deployment_commit': 'b' * 40, 'query_contract_sha256': 'c' * 64,
            'objects': {'data.json': {'sha256': 'a' * 64, 'bytes': 10},
                        'exports/metric0-24h.csv': {'sha256': 'b' * 64, 'bytes': 10}},
        }
        raw_bad_m = pub.json_bytes(v6_manifest_24h)
        with self.assertRaisesRegex(pub.PublicationError, 'invalid_object_descriptor'):
            pub.validate_manifest(raw_bad_m, pub.descriptor(raw_bad_m, v6_manifest_24h['id']), allow_previous_v6=True)

    def test_providers_from_config_validates_inputs(self):
        with self.assertRaisesRegex(pub.PublicationError, 'no_enabled_storage'):
            pub.providers_from_config({})
        with self.assertRaisesRegex(pub.PublicationError, 'missing_storage_configuration'):
            pub.providers_from_config({'DO_ENABLED': 'true', 'DO_SPACES_REGION': 'nyc3'})
        with self.assertRaisesRegex(pub.PublicationError, 'invalid_storage_configuration'):
            pub.providers_from_config({'DO_ENABLED': 'maybe'})
        with self.assertRaisesRegex(pub.PublicationError, 'invalid_rclone_path'):
            pub.providers_from_config({
                'DO_ENABLED': 'true', 'DO_SPACES_REGION': 'nyc3', 'DO_SPACES_BUCKET': 'example-bucket',
                'DO_SPACES_PREFIX': 'p', 'DO_SPACES_KEY': 'k', 'DO_SPACES_SECRET': 's', 'RCLONE_PATH': 'relative/rclone',
            })

    def test_prepare_candidate_requires_private_state_and_candidates_directories(self):
        bad_candidates = self.state / 'candidates'
        bad_candidates.mkdir(mode=0o755)
        bad_candidates.chmod(0o755)
        with self.assertRaisesRegex(pub.PublicationError, 'unsafe_state_directory'):
            self.prepare()
        bad_candidates.rmdir()
        bad_candidates.symlink_to(self.directory)
        with self.assertRaisesRegex(pub.PublicationError, 'unsafe_state_directory'):
            self.prepare()
        bad_candidates.unlink()
        self.state.chmod(0o755)
        try:
            with self.assertRaisesRegex(pub.PublicationError, 'unsafe_state_directory'):
                self.prepare()
        finally:
            self.state.chmod(0o700)

    def test_dangling_symlinks_in_receipts_and_pending_fail_closed(self):
        candidate = self.prepare()
        pub.publish_candidate(candidate, self.state, self.providers)
        saved_receipt = (self.state / 'published.json').read_bytes()
        manifest_path = self.state / 'candidates' / candidate.manifest['id'] / 'manifest.json'
        manifest_path.unlink()
        manifest_path.symlink_to(self.directory / 'missing-manifest.json')
        with self.assertRaises(OSError):
            pub.previous_manifest(json.loads(saved_receipt), self.state, self.providers[0])
        manifest_path.unlink()
        manifest_path.write_bytes(pub.json_bytes(candidate.manifest))

        (self.state / 'published.json').unlink()
        (self.state / 'published.json').symlink_to(self.directory / 'missing-published.json')
        next_candidate = self.prepare()
        with self.assertRaises(OSError):
            pub.publish_candidate(next_candidate, self.state, self.providers)
        (self.state / 'published.json').unlink()

        (self.state / 'pending-promotion.json').symlink_to(self.directory / 'missing-pending.json')
        with self.assertRaisesRegex(pub.PublicationError, 'promotion_blocked_pending_recovery'):
            pub.publish_candidate(next_candidate, self.state, self.providers)
        (self.state / 'pending-promotion.json').unlink()

    def test_zero_byte_csv_rejected_before_persisting_candidate(self):
        zero_build = self.directory / 'zero-byte-build'
        fixture(zero_build)
        (zero_build / 'exports/metric0-7d.csv').write_bytes(b'')
        rewrite_build(zero_build)
        candidates_dir = self.state / 'candidates'
        candidates_dir.mkdir(mode=0o700, exist_ok=True)
        before_candidates = set(candidates_dir.iterdir())
        with self.assertRaisesRegex(pub.PublicationError, 'invalid_object_integrity'):
            pub.prepare_candidate(zero_build, self.state, 'a' * 40, 'b' * 40, now=END + 1000)
        self.assertEqual(set(candidates_dir.iterdir()), before_candidates)

    def test_duplicate_release_id_rejected_before_writing_root(self):
        fresh = self.prepare()
        pub.publish_candidate(fresh, self.state, self.providers)
        same_id_build = self.directory / 'same-id-build'
        fixture(same_id_build)
        with mock.patch.object(pub, 'stamped_id', return_value=fresh.manifest['id']):
            colliding = pub.prepare_candidate(same_id_build, self.directory / 'other-state', 'a' * 40, 'b' * 40, now=END + 1000)
        self.events.clear()
        with self.assertRaisesRegex(pub.PublicationError, 'duplicate_root_release'):
            pub.publish_candidate(colliding, self.state, self.providers)
        self.assertFalse((colliding.directory / 'root.json').exists())
        self.assertFalse((self.state / 'pending-promotion.json').exists())
        self.assertFalse(any(e[0] == 'put' and e[2] == 'manifest.json' for e in self.events))

    def test_directory_read_and_lock_fail_closed_without_fd_leaks(self):
        probe_before = os.open(os.devnull, os.O_RDONLY)
        os.close(probe_before)
        with self.assertRaisesRegex(pub.PublicationError, 'invalid_local_file'):
            pub.read_regular(self.directory, 1024)
        probe_mid = os.open(os.devnull, os.O_RDONLY)
        os.close(probe_mid)
        self.assertEqual(probe_mid, probe_before)
        dir_lock = self.state / 'publication.lock'
        dir_lock.unlink(missing_ok=True)
        dir_lock.mkdir(mode=0o700)
        try:
            with self.assertRaisesRegex(pub.PublicationError, 'unsafe_publication_lock'), pub.publication_lock(self.state):
                pass
            probe_after = os.open(os.devnull, os.O_RDONLY)
            os.close(probe_after)
            self.assertEqual(probe_after, probe_before)
        finally:
            dir_lock.rmdir()

    def test_publish_main_applies_private_umask_before_state_creation(self):
        umask_state = self.directory / 'umask-parent' / 'state'
        umask_cfg = self.directory / 'umask-config.env'
        umask_cfg.write_text('STATE_DIR="' + str(umask_state) + '"\n')
        umask_cfg.chmod(0o600)
        prior_umask = os.umask(0o277)
        try:
            with mock.patch('builtins.print'), mock.patch.object(pub.time, 'time', return_value=END + 1000):
                publish.main(['--config', str(umask_cfg), '--build-dir', str(self.build), '--application-commit', 'a' * 40,
                              '--deployment-commit', 'b' * 40, '--prepare-only'])
            restored = os.umask(0o277)
            self.assertEqual(restored, 0o277)
        finally:
            os.umask(prior_umask)
        self.assertEqual(stat.S_IMODE(umask_state.stat().st_mode), 0o700)
        self.assertEqual(stat.S_IMODE((umask_state / 'publication.lock').stat().st_mode), 0o600)
        self.assertEqual(stat.S_IMODE((umask_state / 'candidates').stat().st_mode), 0o700)

    def test_reconcile_root_cli_uses_explicit_file_without_build_or_remote_writes(self):
        config = self.directory / 'config.env'; config.write_text('STATE_DIR="' + str(self.state) + '"\n'); config.chmod(0o600)
        path = self.directory / 'restored-root.json'; path.write_bytes(b'{}')
        with (mock.patch.object(publish, 'providers_from_config', return_value=self.providers),
              mock.patch.object(publish, 'reconcile_root', return_value={'status': 'root_reconciled'}) as reconcile,
              mock.patch.object(publish, 'prepare_candidate', side_effect=AssertionError('unexpected preparation')),
              mock.patch('builtins.print')):
            publish.main(['--config', str(config), '--reconcile-root', str(path), '--confirm-writers-fenced'])
        reconcile.assert_called_once_with(self.state, self.providers, b'{}', True)

    def test_cli_prepare_only_has_no_provider_access(self):
        config = self.directory / 'config.env'
        config.write_text('STATE_DIR="' + str(self.state) + '"\n')
        config.chmod(0o600)
        with (mock.patch.object(publish, 'providers_from_config', side_effect=AssertionError('unexpected network configuration')),
              mock.patch('builtins.print'), mock.patch.object(pub.time, 'time', return_value=END + 1000)):
            publish.main(['--config', str(config), '--build-dir', str(self.build), '--application-commit', 'a' * 40,
                          '--deployment-commit', 'b' * 40, '--prepare-only'])

    def test_dos_thresholds_allowed_and_private_or_unknown_fields_rejected(self):
        build_dir = self.directory / 'build-dos'
        data = fixture(build_dir)
        data['dos_thresholds'] = {
            'as_of': END,
            'reported_relays': 240,
            'settings': [
                {
                    'family_id': 'transport',
                    'key': 'dosconnectionenabled',
                    'option': 'DoSConnectionEnabled',
                    'relays': 240,
                    'suppressed_relays': 8,
                    'unit': 'flag',
                    'values': [
                        {'relays': 200, 'source': 'consensus', 'value': '1'},
                        {'relays': 32, 'source': 'torrc', 'value': '0'},
                    ],
                },
            ],
        }
        (build_dir / 'data.json').write_bytes(pub.json_bytes(data))
        rewrite_build(build_dir)
        objects = {
            path.relative_to(build_dir).as_posix(): path.read_bytes()
            for path in build_dir.rglob('*')
            if path.is_file() and pub.object_name(path.relative_to(build_dir).as_posix())
        }
        validated = pub.validate_data_objects(objects)
        self.assertEqual(validated['dos_thresholds']['reported_relays'], 240)

        unknown = dict(data, unknown_extra_field=1)
        objects['data.json'] = pub.json_bytes(unknown)
        with self.assertRaisesRegex(pub.PublicationError, 'invalid_data_fields'):
            pub.validate_data_objects(objects)

        leaked = json.loads(json.dumps(data))
        leaked['dos_thresholds']['settings'][0]['instance'] = 'relay-01:9100'
        objects['data.json'] = pub.json_bytes(leaked)
        with self.assertRaisesRegex(pub.PublicationError, 'private_metadata'):
            pub.validate_data_objects(objects)


class TransportTests(unittest.TestCase):
    def test_stdin_upload_scoped_keys_private_child_env_and_explicit_cache_metadata(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            trace = root / 'trace.json'
            binary = root / 'rclone'
            binary.write_text('#!' + sys.executable + '\nimport json,os,sys\n' +
                'open(' + repr(str(trace)) + ',"w").write(json.dumps({"argv":sys.argv[1:],"env":dict(os.environ),"stdin":sys.stdin.buffer.read().hex()}))\n')
            binary.chmod(0o700)
            raw = b'{"verified":"bytes"}\n'
            with mock.patch.dict(os.environ, {'RCLONE_DELETE_EXCLUDED': 'true', 'AWS_SECRET_ACCESS_KEY': 'unrelated-fixture-secret'}):
                provider = pub.RcloneProvider('do', str(binary), 'example-bucket', 'onionsurge',
                    'https://nyc3.digitaloceanspaces.com', 'nyc3', 'fixture-key', 'fixture-secret')
            for key, immutable in [('releases/release-a/data.json', True), ('manifest.json', False)]:
                provider.put(key, raw, immutable)
                called = json.loads(trace.read_text())
                args, env = called['argv'], called['env']
                self.assertEqual(args[0], 'rcat')
                self.assertNotIn('--size', args)
                self.assertEqual(args[args.index('--streaming-upload-cutoff') + 1], str(pub.MAX_OBJECT + 1))
                self.assertEqual(called['stdin'], raw.hex())
                self.assertIn('onionsurge:example-bucket/onionsurge/' + key, args)
                self.assertEqual(args[args.index('--retries') + 1], '1')
                self.assertEqual(args[args.index('--low-level-retries') + 1], '1')
                self.assertEqual(args[args.index('--config') + 1], '/dev/null')
                self.assertIn('--s3-no-check-bucket', args)
                self.assertNotIn('--metadata', args)
                self.assertNotIn('--metadata-set', args)
                self.assertIn('Cache-Control: ' + (pub.IMMUTABLE_CACHE_CONTROL if immutable else 'no-store'), args)
                self.assertIn('Content-Type: application/json; charset=utf-8', args)
                self.assertNotIn('fixture-secret', ' '.join(args))
                self.assertEqual(env['RCLONE_CONFIG_ONIONSURGE_SECRET_ACCESS_KEY'], 'fixture-secret')
                self.assertNotIn('RCLONE_DELETE_EXCLUDED', env)
                self.assertNotIn('AWS_SECRET_ACCESS_KEY', env)
            for key in ['../other', 'releases/a/../../other', 'data.js', 'releases/a/config.env', 'releases/a/data.json?x=1']:
                with self.subTest(key=key), self.assertRaises(pub.PublicationError):
                    provider.remote(key)
            for prefix in ['', '/', '../other', 'one/two', 'onionsurge/']:
                with self.subTest(prefix=prefix), self.assertRaises(pub.PublicationError):
                    pub.RcloneProvider('do', str(binary), 'example-bucket', prefix,
                                       'https://nyc3.digitaloceanspaces.com', 'nyc3', 'fixture-key', 'fixture-secret')

    def test_exact_stat_distinguishes_absent_virtual_directory_from_empty_file(self):
        provider = pub.RcloneProvider('do', '/synthetic/rclone', 'example-bucket', 'onionsurge',
                                     'https://nyc3.digitaloceanspaces.com', 'nyc3', 'fixture-key', 'fixture-secret')
        absent = pub.json_bytes({'IsDir': True, 'Size': -1, 'MimeType': 'inode/directory'})
        with mock.patch.object(provider, '_run', return_value=(0, absent)) as run:
            self.assertIsNone(provider.get('manifest.json', pub.MAX_ROOT, missing_ok=True))
            self.assertEqual(run.call_count, 1)
            self.assertEqual(run.call_args.args[0][:2], ['lsjson', '--stat'])
        empty = pub.json_bytes({'IsDir': False, 'Size': 0, 'MimeType': 'application/json'})
        with mock.patch.object(provider, '_run', side_effect=[(0, empty), (0, b'')]) as run:
            self.assertEqual(provider.get('manifest.json', pub.MAX_ROOT, missing_ok=True), b'')
            self.assertEqual(run.call_args_list[1].args[0][0], 'cat')
        with mock.patch.object(provider, '_run', return_value=(0, b'')):
            self.assertEqual(provider.get('manifest.json', pub.MAX_ROOT), b'')
        for stat_result in [(5, b''), (0, b'{}'), (0, pub.json_bytes({'IsDir': False, 'Size': True}))]:
            with self.subTest(stat_result=stat_result), mock.patch.object(provider, '_run', return_value=stat_result):
                with self.assertRaises(pub.PublicationError):
                    provider.get('manifest.json', pub.MAX_ROOT, missing_ok=True)

    def test_upload_passes_bytes_to_timeout_bounded_subprocess(self):
        provider = pub.RcloneProvider('do', '/synthetic/rclone', 'example-bucket', 'onionsurge',
                                     'https://nyc3.digitaloceanspaces.com', 'nyc3', 'fixture-key', 'fixture-secret')
        with mock.patch.object(pub.subprocess, 'run', side_effect=pub.subprocess.TimeoutExpired('rcat', 90)) as run:
            with self.assertRaisesRegex(pub.PublicationError, 'storage_write_failed'):
                provider.put('manifest.json', b'{}', immutable=False)
        self.assertEqual(run.call_args.kwargs['input'], b'{}')
        self.assertEqual(run.call_args.kwargs['timeout'], 90)

    def test_cache_control_is_read_from_stored_metadata_and_must_match(self):
        provider = pub.RcloneProvider('do', '/synthetic/rclone', 'example-bucket', 'onionsurge',
                                     'https://nyc3.digitaloceanspaces.com', 'nyc3', 'fixture-key', 'fixture-secret')
        for immutable, expected in [(False, 'no-store'), (True, pub.IMMUTABLE_CACHE_CONTROL)]:
            key = 'releases/release-a/manifest.json' if immutable else 'manifest.json'
            info = {'IsDir': False, 'Metadata': {'cache-control': expected}}
            with mock.patch.object(provider, '_run', return_value=(0, pub.json_bytes(info))) as run:
                provider.verify_cache_control(key, immutable)
                self.assertEqual(run.call_args.args[0][:3], ['lsjson', '--stat', '--metadata'])
            for bad in [{}, {'cache-control': 'public, max-age=60'}, {'cache-control': None}]:
                with mock.patch.object(provider, '_run', return_value=(0, pub.json_bytes({'IsDir': False, 'Metadata': bad}))):
                    with self.assertRaisesRegex(pub.PublicationError, 'cache_control_mismatch'):
                        provider.verify_cache_control(key, immutable)

    def test_full_read_limit_kills_oversized_response(self):
        with tempfile.TemporaryDirectory() as folder:
            binary = Path(folder) / 'rclone'
            binary.write_text('#!' + sys.executable + '\nimport sys\nsys.stdout.buffer.write(b"x" * 1000)\n')
            binary.chmod(0o700)
            provider = pub.RcloneProvider('do', str(binary), 'example-bucket', 'onionsurge',
                'https://nyc3.digitaloceanspaces.com', 'nyc3', 'fixture-key', 'fixture-secret')
            with self.assertRaisesRegex(pub.PublicationError, 'storage_response_limit'):
                provider.get('manifest.json', 100)

    def test_rclone_provider_enforces_strict_object_keys(self):
        provider = pub.RcloneProvider('do', '/synthetic/rclone', 'example-bucket', 'onionsurge',
                                      'https://nyc3.digitaloceanspaces.com', 'nyc3', 'fixture-key', 'fixture-secret')
        for suffix in ('24h', '7d', '1mo', '3mo', '6mo', '12mo', 'all-time', '14d', '24mo'):
            self.assertIn(f'exports/metric0-{suffix}.csv', provider.remote(f'releases/release-a/exports/metric0-{suffix}.csv'))
        self.assertIn('exports/connection-rate-24h.csv', provider.remote('releases/release-a/exports/connection-rate-24h.csv'))
        for bad_key in ('releases/release-a/exports/metric0-0h.csv', 'releases/release-a/exports/metric0-1h.csv',
                        'releases/release-a/exports/metric0-23h.csv', 'releases/release-a/exports/metric0-024h.csv',
                        'releases/release-a/exports/metric0-30m.csv', 'releases/release-a/exports/metric0-1m.csv',
                        'releases/release-a/exports/metric0-60s.csv', 'releases/release-a/exports/metric0-1d.csv',
                        'releases/release-a/exports/metric0-0d.csv', 'releases/release-a/exports/metric0-foo-1h.csv'):
            with self.assertRaisesRegex(pub.PublicationError, 'unsafe_storage_key'):
                provider.remote(bad_key)
        primary_24h_key = 'releases/release-a/exports/metric0-24h.csv'
        meta = pub.json_bytes({'IsDir': False, 'Metadata': {'cache-control': pub.IMMUTABLE_CACHE_CONTROL}})
        with mock.patch.object(provider, '_run', side_effect=[(0, b't,v\n'), (0, meta)]) as run:
            self.assertEqual(provider.get(primary_24h_key, 100), b't,v\n')
            provider.verify_cache_control(primary_24h_key, immutable=True)
            self.assertEqual(run.call_count, 2)


if __name__ == '__main__':
    unittest.main()
