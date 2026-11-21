import ast
import errno
import json
import os
from pathlib import Path
import stat
import subprocess
import sys
import tempfile
import unittest
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'scripts'))
import operations
from operations import (atomic_write, json_bytes, link_or_atomic_write, parse_state_retain_runs,
                        private_log, public_values, rotate_private_log, runner_health, sha256,
                        strict_json_bytes, verify_checkout, verify_frontend)


class CheckoutTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.repo = Path(self.temporary.name) / 'checkout'
        self.repo.mkdir()
        self.git('init', '-q')
        self.git('remote', 'add', 'origin', 'https://github.com/1aeo/onionsurge.git')
        (self.repo / 'source.py').write_text('public fixture\n')
        (self.repo / '.gitignore').write_text('config.env\n')
        self.git('add', 'source.py', '.gitignore')
        self.git('commit', '-q', '-m', 'Test fixture')
        self.commit = self.git('rev-parse', 'HEAD').strip()

    def git(self, *arguments):
        return subprocess.check_output(['git', '-C', str(self.repo), '-c', 'core.hooksPath=/dev/null',
            '-c', 'user.name=1aeo', '-c', 'user.email=test@example.com', '-c', 'commit.gpgsign=false', *arguments],
            stderr=subprocess.PIPE, text=True,
            env={k: v for k, v in os.environ.items() if not k.startswith(('GIT_AUTHOR_', 'GIT_COMMITTER_'))})

    def test_exact_pin_clean_origin_and_ignored_private_config(self):
        (self.repo / 'config.env').write_text('PRIVATE_FIXTURE=value\n')
        self.assertEqual(verify_checkout(self.repo, 'onionsurge', self.commit), self.commit)
        self.git('update-ref', 'refs/remotes/origin/main', self.commit)
        self.assertEqual(verify_checkout(self.repo, 'onionsurge', self.commit), self.commit)
        with self.assertRaisesRegex(ValueError, 'pin'):
            verify_checkout(self.repo, 'onionsurge', 'f' * 40)
        (self.repo / 'source.py').write_text('changed\n')
        with self.assertRaisesRegex(ValueError, 'uncommitted'):
            verify_checkout(self.repo, 'onionsurge', self.commit)

    def test_untracked_wrong_origin_subdirectory_and_unreviewed_head_are_rejected(self):
        (self.repo / 'untracked.txt').write_text('fixture\n')
        with self.assertRaisesRegex(ValueError, 'uncommitted'):
            verify_checkout(self.repo, 'onionsurge', self.commit)
        (self.repo / 'untracked.txt').unlink()
        self.git('remote', 'set-url', 'origin', 'https://github.com/example/onionsurge.git')
        with self.assertRaisesRegex(ValueError, 'origin'):
            verify_checkout(self.repo, 'onionsurge', self.commit)
        self.git('remote', 'set-url', 'origin', 'git@github.com:1aeo/onionsurge.git')
        self.git('update-ref', 'refs/remotes/origin/main', self.commit)
        self.git('commit', '--allow-empty', '-q', '-m', 'Unreviewed fixture')
        later = self.git('rev-parse', 'HEAD').strip()
        with self.assertRaisesRegex(ValueError, 'main history'):
            verify_checkout(self.repo, 'onionsurge', later)
        subdir = self.repo / 'nested'; subdir.mkdir()
        with self.assertRaisesRegex(ValueError, 'checkout root'):
            verify_checkout(subdir, 'onionsurge', later)

    def test_checkout_path_with_embedded_newline(self):
        newline_repo = Path(self.temporary.name) / 'check\nout'
        newline_repo.mkdir()

        def run_git(*arguments):
            return subprocess.check_output(['git', '-C', str(newline_repo), '-c', 'core.hooksPath=/dev/null',
                '-c', 'user.name=1aeo', '-c', 'user.email=test@example.com', '-c', 'commit.gpgsign=false', *arguments],
                stderr=subprocess.PIPE, text=True,
                env={k: v for k, v in os.environ.items() if not k.startswith(('GIT_AUTHOR_', 'GIT_COMMITTER_'))})

        run_git('init', '-q')
        run_git('remote', 'add', 'origin', 'https://github.com/1aeo/onionsurge.git')
        (newline_repo / 'source.py').write_text('public fixture\n')
        run_git('add', 'source.py')
        run_git('commit', '-q', '-m', 'Test fixture')
        commit = run_git('rev-parse', 'HEAD').strip()
        self.assertEqual(verify_checkout(newline_repo, 'onionsurge', commit), commit)
        nested = newline_repo / 'sub\ndir'
        nested.mkdir()
        with self.assertRaisesRegex(ValueError, 'checkout root'):
            verify_checkout(nested, 'onionsurge', commit)


class OperationsBoundaryTests(unittest.TestCase):
    def test_module_import_boundaries_and_operations_stdlib_plus_configuration(self):
        scripts_dir = Path(__file__).resolve().parents[1] / 'scripts'
        local_modules = {p.stem for p in scripts_dir.glob('*.py')}

        def imported_names(module_name):
            tree = ast.parse((scripts_dir / f'{module_name}.py').read_text(encoding='utf-8'))
            found = set()
            for node in ast.walk(tree):
                if isinstance(node, ast.Import):
                    found.update(alias.name.split('.')[0] for alias in node.names)
                elif isinstance(node, ast.ImportFrom) and node.module:
                    found.add(node.module.split('.')[0])
            return found & local_modules

        self.assertEqual(imported_names('operations'), {'configuration'})
        self.assertNotIn('prepare_assets', imported_names('publication'))
        self.assertEqual(imported_names('local_site') & {'cloudflare', 'prepare_assets'}, set())
        self.assertEqual(imported_names('schedule') & {'run_hourly', 'publication'}, set())

    def test_public_values_strict_json_atomic_log_verify_frontend_and_runner_health(self):
        self.assertEqual(strict_json_bytes(b'{"a":1}'), {'a': 1})
        with self.assertRaisesRegex(ValueError, 'private metadata'):
            public_values({'token': 'secret'}, allow_sub_24h=True)
        with self.assertRaisesRegex(ValueError, 'Forbidden sub-24h'):
            public_values({'series': [{'values': []}]}, allow_sub_24h=False)
        public_values({'series': [{'values': []}]}, allow_sub_24h=True)

        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            log_file = root / 'logs/op.log'
            with private_log(log_file) as handle:
                handle.write('line\n')
            self.assertEqual(stat.S_IMODE(log_file.stat().st_mode), 0o600)

            target = root / 'file.json'
            atomic_write(target, b'{"ok":true}\n', mode=0o640)
            self.assertEqual(stat.S_IMODE(target.stat().st_mode), 0o640)

            rel = root / 'frontend-rel'
            public = rel / 'public'
            public.mkdir(parents=True)
            objs = {}
            html = (b'<!doctype html><html data-mode="live"><head></head>'
                    b'<body><script src="app.js"></script><script src="live.js"></script></body></html>\n')
            for name in sorted(operations.FRONTEND):
                raw = html if name == 'index.html' else b'/* asset */\n'
                (public / name).write_bytes(raw)
                objs[name] = {'bytes': len(raw), 'sha256': sha256(raw)}
            (rel / 'asset-release.json').write_bytes(json_bytes({
                'schema_version': 1, 'mode': 'live', 'application_commit': 'a' * 40,
                'build_manifest_sha256': 'b' * 64, 'objects': objs,
            }))
            verified_pub, receipt = verify_frontend(rel, 'a' * 40)
            self.assertEqual(verified_pub, public.resolve())
            self.assertEqual(receipt['mode'], 'live')

            state = root / 'state'
            state.mkdir(mode=0o700)
            (state / 'last-run.json').write_bytes(json_bytes({'status': []}))
            self.assertEqual(runner_health({'STATE_DIR': str(state)})['last_run_status'], 'invalid')
            (state / 'last-run.json').write_bytes(json_bytes({'status': 'published'}))
            (state / 'last-success.json').write_bytes(json_bytes({'release_id': '20261118T040000Z-' + 'a' * 12}))
            self.assertEqual(runner_health({'STATE_DIR': str(state)}), {
                'pending_promotion': False, 'run_busy': False, 'last_run_status': 'published',
                'last_release_id': '20261118T040000Z-' + 'a' * 12, 'publication_warning': None,
            })

            # link_or_atomic_write reuses verified immutable donor inodes and falls back safely on mismatch or EXDEV.
            donor = root / 'donor.bin'
            payload = b'immutable-release-bytes\n'
            atomic_write(donor, payload, mode=0o600)
            linked = root / 'linked.bin'
            self.assertTrue(link_or_atomic_write(linked, payload, donor_path=donor, mode=0o600))
            self.assertEqual(linked.stat().st_ino, donor.stat().st_ino)
            self.assertEqual(linked.read_bytes(), payload)

            mismatch = root / 'mismatch.bin'
            self.assertFalse(link_or_atomic_write(mismatch, b'different-release-byte\n', donor_path=donor, mode=0o600))
            self.assertNotEqual(mismatch.stat().st_ino, donor.stat().st_ino)

            wrong_mode = root / 'wrong-mode.bin'
            self.assertFalse(link_or_atomic_write(wrong_mode, payload, donor_path=donor, mode=0o640))
            self.assertEqual(stat.S_IMODE(wrong_mode.stat().st_mode), 0o640)
            self.assertNotEqual(wrong_mode.stat().st_ino, donor.stat().st_ino)

            exdev_target = root / 'exdev.bin'
            with mock.patch.object(operations.os, 'link', side_effect=OSError(errno.EXDEV, 'cross-device')):
                self.assertFalse(link_or_atomic_write(exdev_target, payload, donor_path=donor, mode=0o600))
            self.assertEqual(exdev_target.read_bytes(), payload)

            # parse_state_retain_runs bounds 2..168 with default 3.
            self.assertEqual(parse_state_retain_runs({}), 3)
            self.assertEqual(parse_state_retain_runs({'STATE_RETAIN_RUNS': '168'}), 168)
            for bad in ('', '0', '1', '169', 'abc', True):
                with self.subTest(bad=bad), self.assertRaisesRegex(ValueError, 'invalid_state_retain_runs'):
                    parse_state_retain_runs({'STATE_RETAIN_RUNS': bad})

            # rotate_private_log rotates .1 -> .2 -> .3 atomically and preserves 0o600 empty target.
            rot_log = root / 'logs/rotate.log'
            self.assertFalse(rotate_private_log(root / 'logs/absent.log', max_bytes=10))
            rot_log.write_bytes(b'short')
            rot_log.chmod(0o600)
            self.assertFalse(rotate_private_log(rot_log, max_bytes=10))
            for step in range(1, 5):
                rot_log.write_bytes(f'generation-{step}-payload\n'.encode())
                rot_log.chmod(0o600)
                self.assertTrue(rotate_private_log(rot_log, max_bytes=10, backups=3))
                self.assertEqual(rot_log.read_bytes(), b'')
                self.assertEqual(stat.S_IMODE(rot_log.stat().st_mode), 0o600)
            self.assertEqual((root / 'logs/rotate.log.1').read_bytes(), b'generation-4-payload\n')
            self.assertEqual((root / 'logs/rotate.log.2').read_bytes(), b'generation-3-payload\n')
            self.assertEqual((root / 'logs/rotate.log.3').read_bytes(), b'generation-2-payload\n')
            self.assertFalse((root / 'logs/rotate.log.4').exists())
            (root / 'logs/rotate.log.2').unlink()
            (root / 'logs/rotate.log.2').symlink_to(rot_log)
            rot_log.write_bytes(b'x' * 16)
            with self.assertRaisesRegex(ValueError, 'backup'):
                rotate_private_log(rot_log, max_bytes=10, backups=3)


if __name__ == '__main__':
    unittest.main()
