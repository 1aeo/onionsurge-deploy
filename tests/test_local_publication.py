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
from test_publication import END, CONTRACT_SHA256, fixture


class LocalPublicationTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name).resolve()
        self.build = self.root / 'build'
        fixture(self.build)
        self.state = self.root / 'state'
        self.state.mkdir(mode=0o700)
        self.site_dir = self.root / 'site-out'

    def test_providers_from_config_accepts_local_only_and_rejects_overlap_and_bad_retain(self):
        cfg = {'DO_ENABLED': 'false', 'R2_ENABLED': 'false', 'LOCAL_ENABLED': 'true',
               'LOCAL_SITE_DIR': str(self.site_dir), 'STATE_DIR': str(self.state),
               'LOCAL_RETAIN_RELEASES': '2'}
        providers = pub.providers_from_config(cfg)
        self.assertEqual(len(providers), 1)
        self.assertIsInstance(providers[0], pub.LocalDirectoryProvider)
        self.assertEqual(providers[0].retain_releases, 2)

        bad_overlap = dict(cfg, LOCAL_SITE_DIR=str(self.state / 'site'))
        with self.assertRaisesRegex(pub.PublicationError, 'local_site_overlaps_state'):
            pub.providers_from_config(bad_overlap)

        for bad_retain_val in ('0', '', 'abc', '366'):
            bad_retain = dict(cfg, LOCAL_RETAIN_RELEASES=bad_retain_val)
            with self.assertRaisesRegex(pub.PublicationError, 'invalid_local_retain_releases'):
                pub.providers_from_config(bad_retain)

        symlink_site = self.root / 'sym-site'
        symlink_site.symlink_to(self.state)
        with self.assertRaisesRegex(pub.PublicationError, 'unsafe_local_site_directory'):
            pub.LocalDirectoryProvider(symlink_site)

    def test_local_directory_provider_publish_prunes_old_releases_and_enforces_symlinks_and_conflicts(self):
        cfg = {'DO_ENABLED': 'false', 'R2_ENABLED': 'false', 'LOCAL_ENABLED': 'true',
               'LOCAL_SITE_DIR': str(self.site_dir), 'STATE_DIR': str(self.state),
               'LOCAL_RETAIN_RELEASES': '2'}
        providers = pub.providers_from_config(cfg)
        provider = providers[0]
        commits = {'application_commit': 'a' * 40, 'deployment_commit': 'b' * 40}

        # Publish release 1 (day 1)
        with pub.publication_lock(self.state):
            cand1 = pub.prepare_candidate(self.build, self.state, **commits, now=END + 10)
            res1 = pub.publish_candidate(cand1, self.state, providers)
        self.assertEqual(res1['status'], 'published')
        manifest_file = self.site_dir / 'data/manifest.json'
        self.assertTrue(manifest_file.is_file())
        self.assertEqual(stat.S_IMODE(manifest_file.stat().st_mode), 0o640)
        self.assertEqual(stat.S_IMODE((self.site_dir / 'data').stat().st_mode), 0o750)

        # Symlink attack inside data/ must fail closed.
        rel1 = res1['release_id']
        data_file = self.site_dir / 'data/releases' / rel1 / 'data.json'
        raw_data = data_file.read_bytes()
        evil = self.root / 'evil.json'
        evil.write_bytes(b'{}')
        data_file.unlink()
        data_file.symlink_to(evil)
        with self.assertRaises(pub.PublicationError):
            provider.get(f'releases/{rel1}/data.json', pub.MAX_OBJECT)
        with self.assertRaisesRegex(pub.PublicationError, 'immutable_object_conflict'):
            provider.put(f'releases/{rel1}/data.json', raw_data, immutable=True)
        data_file.unlink()
        provider.put(f'releases/{rel1}/data.json', raw_data, immutable=True)

        # Immutable identical re-put succeeds; mismatched bytes fail closed.
        provider.put(f'releases/{rel1}/data.json', raw_data, immutable=True)
        with self.assertRaisesRegex(pub.PublicationError, 'immutable_object_conflict'):
            provider.put(f'releases/{rel1}/data.json', raw_data + b' ', immutable=True)

        # Create an unrelated backup directory under data/releases/ that pruning must preserve.
        backup_dir = self.site_dir / 'data/releases/backup'
        backup_dir.mkdir(parents=True)
        (backup_dir / 'note.txt').write_text('keep me\n')

        # Advance 2 more days so release 1 is pruned while current (rel3), previous (rel2), and backup/ remain.
        b2 = self.root / 'build2'
        fixture(b2, end=END + 86400)
        with pub.publication_lock(self.state):
            cand2 = pub.prepare_candidate(b2, self.state, **commits, now=END + 86400 + 10)
            res2 = pub.publish_candidate(cand2, self.state, providers)
        b3 = self.root / 'build3'
        fixture(b3, end=END + 2 * 86400)
        with pub.publication_lock(self.state):
            cand3 = pub.prepare_candidate(b3, self.state, **commits, now=END + 2 * 86400 + 10)
            res3 = pub.publish_candidate(cand3, self.state, providers)

        self.assertTrue((backup_dir / 'note.txt').is_file())
        releases_present = {p.name for p in (self.site_dir / 'data/releases').iterdir()}
        self.assertEqual(releases_present, {res2['release_id'], res3['release_id'], 'backup'})

        # Hardlink deduplication: unchanged CSV/reference objects share inodes between releases; changed data.json differs.
        ref2_rel = next(p.relative_to(b2).as_posix() for p in (b2 / 'reference').glob('*.json'))
        ref3_rel = next(p.relative_to(b3).as_posix() for p in (b3 / 'reference').glob('*.json'))
        self.assertNotEqual(ref2_rel, ref3_rel)
        csv_rel = 'exports/metric0-7d.csv'
        s2_csv = (self.site_dir / 'data/releases' / res2['release_id'] / csv_rel).stat()
        s3_csv = (self.site_dir / 'data/releases' / res3['release_id'] / csv_rel).stat()
        self.assertEqual((s2_csv.st_dev, s2_csv.st_ino), (s3_csv.st_dev, s3_csv.st_ino))
        self.assertEqual(stat.S_IMODE(s3_csv.st_mode), 0o640)
        s2_data = (self.site_dir / 'data/releases' / res2['release_id'] / 'data.json').stat()
        s3_data = (self.site_dir / 'data/releases' / res3['release_id'] / 'data.json').stat()
        self.assertNotEqual(s2_data.st_ino, s3_data.st_ino)
        s3_ref = (self.site_dir / 'data/releases' / res3['release_id'] / ref3_rel).stat()
        b4 = self.root / 'build4'
        fixture(b4, end=END + 2 * 86400)  # identical end -> identical reference + unchanged CSVs!
        with pub.publication_lock(self.state):
            cand4 = pub.prepare_candidate(b4, self.state, **commits, now=END + 2 * 86400 + 20)
            res4 = pub.publish_candidate(cand4, self.state, providers)
        self.assertNotIn(res2['release_id'], {p.name for p in (self.site_dir / 'data/releases').iterdir()})
        s4_ref = (self.site_dir / 'data/releases' / res4['release_id'] / ref3_rel).stat()
        self.assertEqual((s3_ref.st_dev, s3_ref.st_ino), (s4_ref.st_dev, s4_ref.st_ino))
        s4_csv = (self.site_dir / 'data/releases' / res4['release_id'] / csv_rel).stat()
        self.assertEqual((s3_csv.st_dev, s3_csv.st_ino), (s4_csv.st_dev, s4_csv.st_ino))
        self.assertEqual((self.site_dir / 'data/releases' / res4['release_id'] / csv_rel).read_bytes(),
                         (b4 / csv_rel).read_bytes())

        # A fresh provider instance simulating crash retry syncs reused ancestor directories before promotion.
        fresh_provider = pub.LocalDirectoryProvider(self.site_dir, retain_releases=2)
        with mock.patch.object(pub, 'sync_directory', wraps=pub.sync_directory) as sync_spy:
            fresh_provider.verify_cache_control(f"releases/{res4['release_id']}/data.json", immutable=True)
        synced = {call.args[0] for call in sync_spy.call_args_list}
        self.assertIn(self.site_dir / 'data/releases', synced)
        self.assertIn(self.site_dir / 'data/releases' / res4['release_id'], synced)
        root_json = json.loads(manifest_file.read_text())
        self.assertEqual(root_json['current']['id'], res4['release_id'])
        self.assertEqual(root_json['previous']['id'], res3['release_id'])

    def test_local_directory_provider_supports_recovery_after_uncertain_root(self):
        providers = pub.providers_from_config({
            'DO_ENABLED': 'false', 'R2_ENABLED': 'false', 'LOCAL_ENABLED': 'true',
            'LOCAL_SITE_DIR': str(self.site_dir), 'STATE_DIR': str(self.state),
        })
        commits = {'application_commit': 'a' * 40, 'deployment_commit': 'b' * 40}
        with pub.publication_lock(self.state):
            cand = pub.prepare_candidate(self.build, self.state, **commits, now=END + 10)
            old_put = providers[0].put
            def fail_root(key, raw, immutable):
                if key == 'manifest.json':
                    old_put(key, raw, immutable)
                    raise OSError('simulated failure after root write')
                old_put(key, raw, immutable)
            providers[0].put = fail_root
            with self.assertRaisesRegex(pub.PublicationError, 'promotion_uncertain_operator_recovery_required'):
                pub.publish_candidate(cand, self.state, providers)
            providers[0].put = old_put
            recovered = pub.recover_promotion(self.state, providers, writers_fenced=True)
        self.assertEqual(recovered['status'], 'recovered')
        self.assertTrue((self.state / 'published.json').is_file())
        self.assertFalse(os.path.lexists(self.state / 'pending-promotion.json'))

    def test_ensure_web_directory_uses_0750_under_strict_umask_and_skips_redundant_chmod(self):
        base = self.root / 'web-base'
        base.mkdir(mode=0o750)
        os.chmod(base, stat.S_ISGID | 0o750)
        target = base / 'data' / 'releases' / 'rel-1'

        old_umask = os.umask(0o077)
        try:
            self.assertTrue(pub.ensure_web_directory(base, target, create=True))
            self.assertEqual(os.umask(0o077), 0o077)
        finally:
            os.umask(old_umask)

        for cur in (base / 'data', base / 'data' / 'releases', target):
            mode = cur.lstat().st_mode
            self.assertEqual(stat.S_IMODE(mode) & 0o777, 0o750)
            self.assertTrue(mode & stat.S_ISGID)

        target2 = base / 'data' / 'releases' / 'rel-2'
        real_lstat = Path.lstat
        with mock.patch.object(pub.os, 'chmod', wraps=pub.os.chmod) as chmod_spy:
            def fake_lstat(path_obj):
                st = real_lstat(path_obj)
                if path_obj == target2:
                    return os.stat_result((stat.S_IFDIR | stat.S_ISGID | 0o750, *st[1:]))
                return st

            with mock.patch.object(Path, 'lstat', new=fake_lstat):
                old_umask = os.umask(0o077)
                try:
                    self.assertTrue(pub.ensure_web_directory(base, target2, create=True))
                finally:
                    os.umask(old_umask)
            chmod_spy.assert_not_called()


if __name__ == '__main__':
    unittest.main()
