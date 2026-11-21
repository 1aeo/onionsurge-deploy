import os
from pathlib import Path
import sys
import tempfile
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'scripts'))
from configuration import enabled, expand_path, load_config, parse_env, read_regular_bytes


class ConfigurationTests(unittest.TestCase):
    def test_values_are_parsed_without_shell_execution(self):
        values = parse_env('export TOKEN="literal$VALUE#data"\nPATH_VALUE="$(not-a-command)"\nEMPTY=\n')
        self.assertEqual(values['TOKEN'], 'literal$VALUE#data')
        self.assertEqual(values['PATH_VALUE'], '$(not-a-command)')
        self.assertEqual(values['EMPTY'], '')
        for text in ['echo command', 'A=one\nA=two', 'A=unquoted spaces', 'lower=value']:
            with self.subTest(text=text), self.assertRaises(ValueError):
                parse_env(text)

    def test_private_file_permissions_and_required_keys(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / 'config.env'
            path.write_text('TOKEN=example\n')
            path.chmod(0o644)
            with self.assertRaisesRegex(ValueError, '0600'):
                load_config(path)
            path.chmod(0o600)
            self.assertEqual(load_config(path, required=['TOKEN']), {'TOKEN': 'example'})
            with self.assertRaisesRegex(ValueError, 'Missing configuration'):
                load_config(path, required=['ABSENT'])
            link = Path(folder) / 'link'
            link.symlink_to(path)
            with self.assertRaises(OSError):
                load_config(link)
            subdir = Path(folder) / 'subdir'
            subdir.mkdir()
            probe_before = os.open(os.devnull, os.O_RDONLY)
            os.close(probe_before)
            with self.assertRaisesRegex(ValueError, 'regular file'):
                read_regular_bytes(subdir, 1024)
            probe_after = os.open(os.devnull, os.O_RDONLY)
            os.close(probe_after)
            self.assertEqual(probe_after, probe_before)

    def test_paths_and_booleans(self):
        self.assertEqual(expand_path('${HOME}/state'), Path.home() / 'state')
        self.assertEqual(expand_path('$HOME/state'), Path.home() / 'state')
        for bad_path in ('relative/state', '$HOME_OTHER/state', '${HOME_OTHER}/state'):
            with self.subTest(bad_path=bad_path), self.assertRaises(ValueError):
                expand_path(bad_path)
        self.assertTrue(enabled({'FLAG': 'true'}, 'FLAG'))
        for bad_flag in ('maybe', True, None, 1):
            with self.subTest(bad_flag=bad_flag), self.assertRaises(ValueError):
                enabled({'FLAG': bad_flag}, 'FLAG')


if __name__ == '__main__':
    unittest.main()
