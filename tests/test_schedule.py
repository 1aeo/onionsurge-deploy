import json
import os
from datetime import datetime, timezone
from pathlib import Path
import shlex
import stat
import struct
import subprocess
import sys
import tempfile
from types import SimpleNamespace
import unittest
from unittest import mock
import zoneinfo

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'scripts'))
from schedule import Busy
import schedule


class FakeCrontab:
    def __init__(self, text=''):
        self.text = text
        self.writes = []

    def read(self):
        return self.text

    def write(self, text):
        self.writes.append(text)
        self.text = text


class ScheduleTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        patcher = mock.patch.object(schedule, 'host_timezone_is_utc', return_value=True)
        patcher.start()
        self.addCleanup(patcher.stop)
        self.root = Path(self.temporary.name)
        self.config = {'STATE_DIR': str(self.root / 'state'), 'LOG_DIR': str(self.root / 'logs'),
                       'CRON_HOUR': '0', 'CRON_MINUTE': '20'}
        self.config_path = self.root / 'config.env'

    def test_install_verify_remove_preserve_every_unrelated_entry(self):
        original = 'SHELL=/bin/sh\n# unrelated\n7 * * * * /usr/bin/true\n\n'
        cron = FakeCrontab(original)
        default_cfg = {'STATE_DIR': self.config['STATE_DIR'], 'LOG_DIR': self.config['LOG_DIR']}
        self.assertIn('\n5 * * * * ', schedule.managed_block(default_cfg, self.config_path))
        result = schedule.change('install', self.config, self.config_path, cron)
        self.assertEqual(result['schedule'], 'installed')
        self.assertTrue(result['log_private'])
        self.assertTrue(cron.text.startswith(original))
        self.assertIn('\n20 * * * * ', cron.text)
        self.assertEqual(stat.S_IMODE(schedule.log_path(self.config).stat().st_mode), 0o600)
        schedule.change('install', self.config, self.config_path, cron)
        self.assertEqual(len(cron.writes), 1, 'install is idempotent')
        self.assertEqual(schedule.verify(self.config, self.config_path, cron)['schedule'], 'installed')
        result = schedule.change('remove', self.config, self.config_path, cron)
        self.assertEqual(result['schedule'], 'paused')
        self.assertEqual(cron.text, original)
        schedule.change('remove', self.config, self.config_path, cron)
        self.assertEqual(len(cron.writes), 2)

    def test_migrates_legacy_daily_managed_block_to_hourly_in_place(self):
        legacy_block = (
            schedule.LEGACY_BEGIN + '\n'
            + '20 0 * * * umask 077; TZ=UTC /usr/bin/python3 old.py >> /tmp/cron.log 2>&1\n'
            + schedule.LEGACY_END + '\n'
        )
        before = 'SHELL=/bin/sh\n# before\n'
        after = '# after\nSHELL=/bin/tcsh\nCRON_TZ=America/New_York\n'
        cron = FakeCrontab(before + legacy_block + after)
        self.assertEqual(schedule.verify(self.config, self.config_path, cron)['schedule'], 'drifted')
        res = schedule.change('install', self.config, self.config_path, cron)
        self.assertEqual(res['schedule'], 'installed')
        expected = schedule.managed_block(self.config, self.config_path)
        self.assertEqual(cron.text, before + expected + after)
        self.assertNotIn(schedule.LEGACY_BEGIN, cron.text)
        self.assertEqual(schedule.verify(self.config, self.config_path, cron)['schedule'], 'installed')

        cron_remove = FakeCrontab(before + legacy_block + after)
        self.assertEqual(schedule.change('remove', self.config, self.config_path, cron_remove)['schedule'], 'paused')
        self.assertEqual(cron_remove.text, before + after)

    def test_replacement_retains_block_position_and_rejects_ambiguous_markers(self):
        first = schedule.managed_block(self.config, self.config_path)
        changed = schedule.managed_block(dict(self.config, CRON_MINUTE='19'), self.config_path)
        text = '# before\n' + first + '# after\n'
        self.assertEqual(schedule.replace_managed(text, changed), '# before\n' + changed + '# after\n')
        legacy = schedule.LEGACY_BEGIN + '\n0 0 * * * cmd\n' + schedule.LEGACY_END + '\n'
        for bad in [schedule.BEGIN + '\n', schedule.END + '\n', first + first, legacy + legacy,
                    first + legacy, schedule.END + '\n' + schedule.BEGIN + '\n',
                    schedule.LEGACY_BEGIN + '\n' + schedule.END + '\n',
                    schedule.BEGIN + '\n' + schedule.LEGACY_END + '\n']:
            with self.subTest(text=bad), self.assertRaises(schedule.ScheduleError):
                schedule.replace_managed(bad, changed)
        with self.assertRaises(schedule.ScheduleError):
            schedule.replace_managed('# no final newline', first)

    def test_quotes_percent_escaping_and_private_log_creation(self):
        deploy = self.root / "deployment space's 100%"; (deploy / 'scripts').mkdir(parents=True)
        trace = self.root / 'argv.json'
        (deploy / 'scripts/run_hourly.py').write_text('import json,sys\nopen(' + repr(str(trace)) + ',"w").write(json.dumps(sys.argv[1:]))\n')
        config_path = self.root / "settings space's 50%.env"
        self.root.joinpath('logs').mkdir(mode=0o700)
        block = schedule.managed_block(self.config, config_path, deploy)
        line = block.splitlines()[1]
        self.assertIn('TZ=UTC', line)
        self.assertIn('\\%', line)
        command = line.split(' ', 5)[5]
        # Cron removes the percent escape before passing the command to the shell.
        result = subprocess.run(['/bin/sh', '-c', command.replace('\\%', '%')], capture_output=True, text=True)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(json.loads(trace.read_text()), ['--config', str(config_path)])
        self.assertEqual(stat.S_IMODE(schedule.log_path(self.config).stat().st_mode), 0o600)
        for path in [str(self.root / 'bad\npath'), str(self.root / 'bad\\path')]:
            with self.subTest(path=path), self.assertRaises(schedule.ScheduleError):
                schedule.managed_block(self.config, path)

    def test_detects_crontab_drift_without_overwriting_it(self):
        cron = FakeCrontab('# original\n')
        calls = 0
        def changing_read():
            nonlocal calls
            calls += 1
            return '# original\n' if calls == 1 else '# intervening edit\n'
        with mock.patch.object(cron, 'read', side_effect=changing_read), self.assertRaises(schedule.ScheduleError):
            schedule.change('install', self.config, self.config_path, cron)
        self.assertEqual(cron.writes, [])
        cron.text = schedule.managed_block(dict(self.config, CRON_MINUTE='6'), self.config_path)
        self.assertEqual(schedule.verify(self.config, self.config_path, cron)['schedule'], 'drifted')

    def test_crontab_missing_is_distinct_from_permission_failure(self):
        client = schedule.Crontab()
        with mock.patch.object(schedule.subprocess, 'run', return_value=SimpleNamespace(returncode=1, stdout=b'', stderr=b'no crontab for fixture\n')) as run:
            self.assertEqual(client.read(), '')
            self.assertEqual(run.call_args.kwargs['env']['LC_ALL'], 'C')
        with mock.patch.object(schedule.subprocess, 'run', return_value=SimpleNamespace(returncode=1, stdout=b'', stderr=b'permission denied')):
            with self.assertRaises(schedule.ScheduleError):
                client.read()
        with mock.patch.object(schedule.subprocess, 'run', return_value=SimpleNamespace(returncode=0, stdout=b'# comment\rTZ=Asia/Tokyo\n', stderr=b'')):
            raw = client.read()
            self.assertEqual(raw, '# comment\rTZ=Asia/Tokyo\n')
            self.assertFalse(schedule.utc_cron_context(raw))

    def test_bad_minutes_and_insecure_log_symlinks_are_rejected(self):
        for value in ['-1', '60', '*/5', '5; command', '', None]:
            with self.subTest(value=value), self.assertRaises(schedule.ScheduleError):
                schedule.managed_block(dict(self.config, CRON_MINUTE=value), self.config_path)
        logs = self.root / 'logs'; logs.mkdir(mode=0o700)
        target = self.root / 'unrelated'; target.write_text('keep')
        (logs / 'cron.log').symlink_to(target)
        with self.assertRaises(OSError):
            schedule.change('install', self.config, self.config_path, FakeCrontab())
        self.assertEqual(target.read_text(), 'keep')

    def test_effective_shell_is_checked_at_the_managed_block_without_changing_other_jobs(self):
        for shell in ['/bin/tcsh', '/usr/bin/fish', '/private/custom-shell', '', '/bin/sh # not an inline comment']:
            cron = FakeCrontab('SHELL=' + shell + '\n7 * * * * other-command\n')
            before = cron.text
            with self.subTest(shell=shell), self.assertRaisesRegex(schedule.ScheduleError, 'POSIX cron SHELL'):
                schedule.change('install', self.config, self.config_path, cron)
            self.assertEqual(cron.text, before)
            self.assertEqual(cron.writes, [])
        self.assertFalse(schedule.log_path(self.config).exists())
        with mock.patch.object(schedule.os, 'access', return_value=False):
            self.assertFalse(schedule.compatible_shell('SHELL=/bin/sh\n'))
        for shell in ['/bin/sh', '"/bin/sh"', "'/bin/sh'"]:
            cron = FakeCrontab('SHELL=/bin/tcsh\n  SHELL = ' + shell + '\n')
            before = cron.text
            self.assertEqual(schedule.change('install', self.config, self.config_path, cron)['schedule'], 'installed')
            cron.text += 'SHELL=/bin/tcsh\n11 * * * * unrelated-command\n'
            self.assertEqual(schedule.verify(self.config, self.config_path, cron)['schedule'], 'installed')
            changed = dict(self.config, CRON_MINUTE='19')
            self.assertEqual(schedule.change('install', changed, self.config_path, cron)['schedule'], 'installed')
            self.assertTrue(cron.text.startswith(before))
            self.assertTrue(cron.text.endswith('SHELL=/bin/tcsh\n11 * * * * unrelated-command\n'))
            cron.text = 'SHELL=/bin/tcsh\n' + schedule.managed_block(changed, self.config_path)
            self.assertEqual(schedule.verify(changed, self.config_path, cron)['schedule'], 'drifted')
            self.assertEqual(schedule.change('remove', changed, self.config_path, cron)['schedule'], 'paused')
            self.assertEqual(cron.text, 'SHELL=/bin/tcsh\n')

    def test_utc_cron_timezone_enforced_without_rewriting_other_jobs(self):
        for tz_line in ('CRON_TZ=America/New_York\n', 'TZ=Europe/Berlin\n'):
            cron = FakeCrontab(tz_line + '7 * * * * unrelated-command\n')
            with self.subTest(tz_line=tz_line), self.assertRaisesRegex(schedule.ScheduleError, 'UTC cron timezone'):
                schedule.change('install', self.config, self.config_path, cron)
            self.assertEqual(cron.writes, [])
            cron.text = tz_line + schedule.managed_block(self.config, self.config_path)
            self.assertEqual(schedule.verify(self.config, self.config_path, cron)['schedule'], 'drifted')

        with mock.patch.object(schedule, 'host_timezone_is_utc', return_value=False):
            cron = FakeCrontab('7 * * * * unrelated-command\n')
            with self.assertRaisesRegex(schedule.ScheduleError, 'UTC cron timezone'):
                schedule.change('install', self.config, self.config_path, cron)
            cron_with_utc = FakeCrontab('CRON_TZ=UTC\n7 * * * * unrelated-command\n')
            res = schedule.change('install', self.config, self.config_path, cron_with_utc)
            self.assertEqual(res['schedule'], 'installed')
            self.assertTrue(cron_with_utc.text.startswith('CRON_TZ=UTC\n7 * * * * unrelated-command\n'))

    def test_line_separator_smuggling_rejected(self):
        for sep in ('\r', '\v', '\f', '\x85', '\u2028', '\u2029'):
            smuggled_tz = f'# comment{sep}CRON_TZ=UTC\n7 * * * * cmd\n'
            smuggled_shell = f'SHELL=/bin/tcsh\n# comment{sep}SHELL=/bin/sh\n7 * * * * cmd\n'
            with self.subTest(sep=repr(sep)):
                with self.assertRaisesRegex(schedule.ScheduleError, 'Invalid crontab'):
                    schedule.split_managed(smuggled_tz)
                with mock.patch.object(schedule, 'host_timezone_is_utc', return_value=False):
                    self.assertFalse(schedule.utc_cron_context(smuggled_tz))
                self.assertFalse(schedule.compatible_shell(smuggled_shell))
                self.assertFalse(schedule.compatible_shell(f'SHELL=/bin/sh{sep}\n'))
        for sep in ('\x85', '\u2028', '\u2029'):
            with self.subTest(path_sep=repr(sep)), self.assertRaisesRegex(schedule.ScheduleError, 'control characters'):
                schedule.managed_block(self.config, self.root / f'bad{sep}cfg.env')

    def test_change_keeps_verify_under_schedule_lock(self):
        cron = FakeCrontab('SHELL=/bin/sh\n')
        real_verify = schedule.verify
        state = Path(self.config['STATE_DIR'])
        observed_busy = []
        def checking_verify(*args, **kwargs):
            with self.assertRaises(Busy), schedule.private_lock(state, 'schedule.lock'):
                pass
            observed_busy.append(True)
            return real_verify(*args, **kwargs)
        with mock.patch.object(schedule, 'verify', side_effect=checking_verify):
            res = schedule.change('install', self.config, self.config_path, cron)
        self.assertEqual(res['schedule'], 'installed')
        self.assertEqual(observed_busy, [True])

        # Removing the managed block succeeds with a health warning even if last-success.json is broken,
        # while verify() continues to fail closed.
        dangling_receipt = state / 'last-success.json'
        dangling_receipt.symlink_to(self.root / 'missing-last-success')
        try:
            with self.assertRaises(OSError):
                schedule.verify(self.config, self.config_path, cron)
            removed = schedule.change('remove', self.config, self.config_path, cron)
            self.assertEqual(removed, {'schedule': 'paused', 'log_private': True, 'health_warning': 'hourly_health_unavailable'})
            self.assertEqual(cron.text, 'SHELL=/bin/sh\n')
        finally:
            dangling_receipt.unlink(missing_ok=True)

        # Ambient restrictive umask (e.g. 0o277) still creates fresh state/, schedule.lock, logs/, cron.log cleanly.
        fresh_state = self.root / 'fresh-state' / 'state'
        fresh_logs = self.root / 'fresh-logs' / 'logs'
        fresh_cfg = dict(self.config, STATE_DIR=str(fresh_state), LOG_DIR=str(fresh_logs))
        fresh_cron = FakeCrontab('SHELL=/bin/sh\n')
        prior_umask = os.umask(0o277)
        try:
            fresh_res = schedule.change('install', fresh_cfg, self.config_path, fresh_cron)
            restored = os.umask(0o277)
            self.assertEqual(restored, 0o277)
        finally:
            os.umask(prior_umask)
        self.assertEqual(fresh_res['schedule'], 'installed')
        self.assertTrue(fresh_res['log_private'])
        self.assertEqual(stat.S_IMODE(fresh_state.stat().st_mode), 0o700)
        self.assertEqual(stat.S_IMODE((fresh_state / 'schedule.lock').stat().st_mode), 0o600)
        self.assertEqual(stat.S_IMODE((fresh_logs / 'cron.log').stat().st_mode), 0o600)

    def test_opt_in_utc_gate_preserves_non_utc_crontab_and_other_jobs(self):
        original = 'SHELL=/bin/sh\nTZ=America/Los_Angeles\nCRON_TZ=Europe/Berlin\n7 3 * * * unrelated-command\n'
        cron = FakeCrontab(original)
        config = dict(self.config, CRON_UTC_GATE='true')
        with mock.patch.object(schedule, 'host_timezone_is_utc', return_value=False):
            result = schedule.change('install', config, self.config_path, cron)
            self.assertEqual(result['schedule'], 'installed')
            self.assertTrue(cron.text.startswith(original))
            _, block = schedule.split_managed(cron.text)
            self.assertTrue(block.splitlines()[1].startswith('* * * * * '))
            self.assertNotIn('\nTZ=', block)
            self.assertNotIn('\nCRON_TZ=', block)
            schedule.change('install', config, self.config_path, cron)
            self.assertEqual(len(cron.writes), 1)
            self.assertEqual(schedule.verify(config, self.config_path, cron)['schedule'], 'installed')
            self.assertEqual(schedule.verify(self.config, self.config_path, cron)['schedule'], 'drifted')
            self.assertEqual(schedule.change('remove', config, self.config_path, cron)['schedule'], 'paused')
            self.assertEqual(cron.text, original)

    def test_gate_only_invokes_runner_at_utc_time_across_pst_pdt_and_tz_environment(self):
        real_date = schedule.utc_date_binary()
        deploy = self.root / "deployment space's 100%"; (deploy/'scripts').mkdir(parents=True)
        trace = self.root/'runner.json'
        (deploy/'scripts/run_hourly.py').write_text(
            'import json,os,sys\nopen(' + repr(str(trace)) + ',"w").write(json.dumps({"argv":sys.argv[1:],"tz":os.environ.get("TZ")}))\n')
        config_path = self.root / "settings space's 50%.env"
        config = dict(self.config, CRON_UTC_GATE='true')
        self.root.joinpath('logs').mkdir(mode=0o700)
        clock = self.root/'clock'
        for month, hour, minute, due in [(1,0,20,True),(7,0,20,True),(1,8,20,True),(7,15,20,True),
                                         (1,0,19,False),(7,15,21,False)]:
            instant = datetime(2026,month,15,hour,minute,tzinfo=timezone.utc)
            epoch = int(instant.timestamp())
            # Use the actual platform date implementation at a fixed instant.
            args = [real_date, '-d', '@'+str(epoch)] if sys.platform.startswith('linux') else [real_date, '-r', str(epoch)]
            clock.write_text('#!/bin/sh\nexec ' + shlex.join(args) + ' "$@"\n'); clock.chmod(0o700)
            with mock.patch.object(schedule, 'utc_date_binary', return_value=str(clock)):
                block = schedule.managed_block(config, config_path, deploy)
            self.assertIn('+\\%M', block)
            self.assertNotIn('+\\%H:\\%M', block)
            command = block.splitlines()[1].split(' ',5)[5].replace('\\%', '%')
            for tz in ['America/Los_Angeles', 'UTC', 'PST8PDT', 'Asia/Kathmandu']:
                trace.unlink(missing_ok=True)
                result = subprocess.run(['/bin/sh','-c',command],capture_output=True,text=True,
                                        env={'PATH':'/nonexistent','TZ':tz,'LC_ALL':'C'})
                with self.subTest(month=month, hour=hour, minute=minute, tz=tz):
                    self.assertEqual(result.returncode,0,result.stderr)
                    self.assertEqual(trace.exists(),due)
                    if due:
                        self.assertEqual(json.loads(trace.read_text()),{'argv':['--config',str(config_path)],'tz':'UTC'})
                    self.assertFalse((Path(config['STATE_DIR'])/'last-run.json').exists())
        self.assertEqual(stat.S_IMODE(schedule.log_path(config).stat().st_mode),0o600)

    def test_failed_clock_cannot_invoke_runner_even_if_stdout_matches_due_time(self):
        deploy = self.root/'deploy'; (deploy/'scripts').mkdir(parents=True)
        trace = self.root/'unexpected'
        (deploy/'scripts/run_hourly.py').write_text('open(' + repr(str(trace)) + ',"w").write("ran")\n')
        self.root.joinpath('logs').mkdir(mode=0o700)
        clock = self.root/'clock'
        clock.write_text('#!/bin/sh\nprintf "20\\n"\nprintf "clock failed\\n" >&2\nexit 1\n'); clock.chmod(0o700)
        config = dict(self.config, CRON_UTC_GATE='true')
        with mock.patch.object(schedule, 'utc_date_binary', return_value=str(clock)):
            block = schedule.managed_block(config, self.config_path, deploy)
        command = block.splitlines()[1].split(' ',5)[5].replace('\\%', '%')
        result = subprocess.run(['/bin/sh','-c',command],capture_output=True,text=True)
        self.assertEqual(result.returncode,1)
        self.assertFalse(trace.exists())
        self.assertEqual(result.stderr,'')
        self.assertIn('clock failed',schedule.log_path(config).read_text())

    def test_gate_settings_are_explicit_and_bad_times_fail_before_clock_probe(self):
        legacy_ignored = {'STATE_DIR': self.config['STATE_DIR'], 'CRON_HOUR': 'invalid'}
        self.assertIn('\n5 * * * * ', schedule.managed_block(legacy_ignored, self.config_path))
        self.assertIn('20', schedule.managed_block(dict(self.config, CRON_UTC_GATE='true'), self.config_path))
        for field,values in [('CRON_UTC_GATE',[True,None,'','1','yes','true; command']),
                             ('CRON_MINUTE',['-1','60','*/5',None])]:
            for value in values:
                with self.subTest(field=field,value=value), mock.patch.object(schedule,'utc_date_binary') as probe:
                    config = dict(self.config, CRON_UTC_GATE='true')
                    config[field] = value
                    with self.assertRaises(schedule.ScheduleError):
                        schedule.managed_block(config,self.config_path)
                    probe.assert_not_called()
        self.assertFalse(schedule.utc_gate_enabled({}))
        self.assertFalse(schedule.utc_gate_enabled({'CRON_UTC_GATE':'false'}))

    def test_gate_requires_a_trusted_executable_with_proven_utc_output(self):
        path = schedule.utc_date_binary()
        self.assertTrue(Path(path).is_absolute())
        self.assertEqual(Path(path).stat().st_uid,0)
        with mock.patch.object(schedule.os,'access',return_value=False), self.assertRaisesRegex(schedule.ScheduleError,'trusted system date'):
            schedule.utc_date_binary()
        for result in [SimpleNamespace(returncode=0,stdout='99\n'),SimpleNamespace(returncode=1,stdout='20\n')]:
            with mock.patch.object(schedule.subprocess,'run',return_value=result), self.assertRaisesRegex(schedule.ScheduleError,'UTC clock format'):
                schedule.utc_date_binary()


class HostTimezoneTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)

    def test_host_timezone_is_utc_ignores_installer_tz_and_rejects_dst_or_factory_zones(self):
        def synth_tzif(footer, utoff=0, isdst=0, abbr=b'UTC\x00'):
            hdr = b'TZif2' + b'\x00' * 15 + struct.pack('>6I', 0, 0, 0, 0, 1, len(abbr))
            body = struct.pack('>iBB', utoff, isdst, 0) + abbr
            return hdr + body + hdr + body + b'\n' + footer + b'\n'

        utc_file = self.root / 'localtime-utc'
        utc_file.write_bytes(synth_tzif(b'UTC0'))
        canary_like = self.root / 'localtime-canary'
        canary_like.write_bytes(synth_tzif(b'WET0WEST,M3.5.0/1,M10.5.0'))
        factory_like = self.root / 'localtime-factory'
        factory_like.write_bytes(synth_tzif(b'<-00>0'))
        tokyo_like = self.root / 'localtime-tokyo'
        tokyo_like.write_bytes(synth_tzif(b'JST-9', utoff=9 * 3600, abbr=b'JST\x00'))
        missing_tz = self.root / 'missing-timezone'
        utc_tz = self.root / 'timezone-utc'; utc_tz.write_text('Etc/UTC\n')
        bad_tz = self.root / 'timezone-bad'; bad_tz.write_text('Atlantic/Canary\n')
        bad_cron_env = self.root / 'cron-env-bad'; bad_cron_env.write_text('TZ=Asia/Tokyo\n')
        good_cron_env = self.root / 'cron-env-good'; good_cron_env.write_text('# comment\nexport TZ="UTC"\n')

        with mock.patch.dict(os.environ, {'TZ': 'UTC'}):
            self.assertTrue(schedule.host_timezone_is_utc(utc_file, missing_tz, env_paths=()))
            self.assertTrue(schedule.host_timezone_is_utc(utc_file, utc_tz, env_paths=(good_cron_env,)))
            self.assertFalse(schedule.host_timezone_is_utc(utc_file, bad_tz, env_paths=()))
            self.assertFalse(schedule.host_timezone_is_utc(utc_file, missing_tz, env_paths=(bad_cron_env,)))
            dangling_env = self.root / 'dangling-env'
            dangling_env.symlink_to(self.root / 'nonexistent-env')
            self.assertFalse(schedule.host_timezone_is_utc(utc_file, missing_tz, env_paths=(dangling_env,)))
            leap_utc = self.root / 'localtime-right-utc'
            leap_hdr = b'TZif2' + b'\x00' * 15 + struct.pack('>6I', 0, 0, 1, 0, 1, 4)
            leap_body_v1 = struct.pack('>iBB', 0, 0, 0) + b'UTC\x00' + struct.pack('>II', 78796800, 1)
            leap_body_v2 = struct.pack('>iBB', 0, 0, 0) + b'UTC\x00' + struct.pack('>qi', 78796800, 1)
            leap_utc.write_bytes(leap_hdr + leap_body_v1 + leap_hdr + leap_body_v2 + b'\nUTC0\n')
            self.assertFalse(schedule.host_timezone_is_utc(leap_utc, missing_tz, env_paths=()))
            dangling = self.root / 'dangling-timezone'
            dangling.symlink_to(self.root / 'nonexistent')
            self.assertFalse(schedule.host_timezone_is_utc(utc_file, dangling, env_paths=()))
            self.assertFalse(schedule.host_timezone_is_utc(canary_like, missing_tz, env_paths=()))
            may_dst = self.root / 'localtime-may-dst'
            may_dst.write_bytes(synth_tzif(b'UTC0BST,M5.1.0,M6.1.0'))
            self.assertFalse(schedule.host_timezone_is_utc(may_dst, missing_tz, env_paths=()))
            malformed_v2 = self.root / 'localtime-malformed-v2'
            malformed_v2.write_bytes(b'TZif2' + b'\x00' * 15 + (0).to_bytes(4, 'big') * 6 + b'TZif2' + b'\x00' * 39 + b'BAD')
            self.assertFalse(schedule.host_timezone_is_utc(malformed_v2, missing_tz, env_paths=()))
            bom_bad_env = self.root / 'cron-bom-bad'
            bom_bad_env.write_bytes(b'\xef\xbb\xbfTZ=EST5EDT\n')
            self.assertFalse(schedule.host_timezone_is_utc(utc_file, missing_tz, env_paths=(bom_bad_env,)))
            bom_good_env = self.root / 'cron-bom-good'
            bom_good_env.write_bytes(b'\xef\xbb\xbfTZ=UTC\n')
            self.assertTrue(schedule.host_timezone_is_utc(utc_file, missing_tz, env_paths=(bom_good_env,)))
            self.assertFalse(schedule.host_timezone_is_utc(factory_like, missing_tz, env_paths=()))
            self.assertFalse(schedule.host_timezone_is_utc(tokyo_like, missing_tz, env_paths=()))
            abbrs = b'UTC\x00JST\x00'
            brief_non_utc = utc_file.parent / 'brief-non-utc'
            brief_non_utc.write_bytes(
                b'TZif2' + b'\x00' * 15 + b''.join(n.to_bytes(4, 'big') for n in (0, 0, 0, 0, 1, 4))
                + b'\x00' * 6 + b'UTC\x00'
                + b'TZif2' + b'\x00' * 15 + b''.join(n.to_bytes(4, 'big') for n in (0, 0, 0, 2, 2, len(abbrs)))
                + (1798848000).to_bytes(8, 'big', signed=True) + (1798934400).to_bytes(8, 'big', signed=True)
                + b'\x01\x00'
                + (0).to_bytes(4, 'big', signed=True) + b'\x00\x00'
                + (3600).to_bytes(4, 'big', signed=True) + b'\x00\x04'
                + abbrs + b'\nUTC0\n'
            )
            self.assertFalse(schedule.host_timezone_is_utc(brief_non_utc, missing_tz, env_paths=()))
            malformed_isut = utc_file.parent / 'malformed-isut'
            malformed_isut.write_bytes(
                b'TZif2' + b'\x00' * 15 + struct.pack('>6I', 0, 0, 0, 0, 1, 4)
                + b'\x00' * 6 + b'UTC\x00'
                + b'TZif2' + b'\x00' * 15 + struct.pack('>6I', 5, 0, 0, 0, 1, 4)
                + b'\x00' * 6 + b'UTC\x00\nUTC0\n'
            )
            misplaced_footer = utc_file.parent / 'misplaced-footer'
            misplaced_footer.write_bytes(
                b'TZif2' + b'\x00' * 15 + struct.pack('>6I', 0, 0, 0, 0, 1, 4)
                + b'\x00' * 6 + b'UTC\x00'
                + b'TZif2' + b'\x00' * 15 + struct.pack('>6I', 1, 1, 0, 0, 1, 4)
                + b'\x00' * 6 + b'UTC\x00\nUTC0\n'
            )
            fake_zoneinfo = mock.Mock()
            fake_zoneinfo.ZoneInfo.from_file.side_effect = AssertionError(
                'ZoneInfo.from_file must not run on malformed footer layout'
            )
            with mock.patch.object(schedule, 'zoneinfo', fake_zoneinfo):
                self.assertFalse(schedule.host_timezone_is_utc(malformed_isut, missing_tz, env_paths=()))
                self.assertFalse(schedule.host_timezone_is_utc(misplaced_footer, missing_tz, env_paths=()))
            fake_zoneinfo.ZoneInfo.from_file.assert_not_called()
            self.assertFalse(schedule.host_timezone_is_utc(self.root / 'absent', missing_tz, env_paths=()))

        with mock.patch.dict(os.environ, {'TZ': 'UTC'}):
            for zone, expected in [('UTC', True), ('Etc/UTC', True), ('Atlantic/Canary', False), ('Asia/Tokyo', False), ('Factory', False)]:
                for base in zoneinfo.TZPATH:
                    candidate = Path(base) / zone
                    if candidate.is_file():
                        with self.subTest(zone=zone):
                            self.assertEqual(schedule.host_timezone_is_utc(candidate, missing_tz, env_paths=()), expected)
                        break

    def test_host_timezone_rejects_multistatement_and_line_separator_smuggling_in_env_files(self):
        utc_localtime = self.root / 'localtime-utc-check'
        hdr = b'TZif2' + b'\x00' * 15 + struct.pack('>6I', 0, 0, 0, 0, 1, 4)
        body = struct.pack('>iBB', 0, 0, 0) + b'UTC\x00'
        utc_localtime.write_bytes(hdr + body + hdr + body + b'\nUTC0\n')
        missing_tz = self.root / 'absent-tz'
        for bad_env in (
            '# comment\rTZ=Asia/Tokyo\n',
            '# comment\rCRON_TZ=UTC\n',
            'FOO=1; TZ=America/New_York\n',
            'TZ=UTC; export TZ\n',
            'EXTRA="ok" CRON_TZ=America/Los_Angeles\n',
            'FOO=bar \\\nTZ=America/New_York\n',
            'FOO=bar \\\nTZ=UTC\n',
            '# trailing backslash \\\nTZ=Asia/Tokyo\n',
            'FOO="unclosed\n',
            'FOO=bar#note; export TZ=America/New_York\n',
            'EXTRA_OPTS="" ; TZ=America/New_York\n',
            'EXTRA_OPTS="" TZ=America/New_York\n',
            'FOO=1 TZ = UTC\n',
            'export "TZ=Asia/Tokyo"\n',
            'export FOO=1 "TZ=UTC"\n',
            'export -n TZ\n',
            'export TZ\n',
            'FOO=1; export "TZ=Asia/Tokyo"\n',
            'FOO=1 export "TZ=Asia/Kathmandu"\n',
            'command -p export TZ=Asia/Kathmandu\n',
            'command -p -- export "TZ=Asia/Kathmandu"\n',
            'command -v export TZ=UTC\n',
            'if :; then export TZ=Asia/Kathmandu; fi\n',
            '! export TZ=Asia/Kathmandu\n',
            '{ export TZ=Asia/Kathmandu; }\n',
            'eval "export \'TZ\'=Asia/Kathmandu"\n',
            'eval \'export "TZ"=Asia/Kathmandu\'\n',
            'eval export "\'TZ\'=Asia/Kathmandu"\n',
            'sh -c "export \'TZ\'=Asia/Kathmandu"\n',
            "eval : '; export TZ=Asia/Tokyo'\n",
            'export `echo TZ=Asia/Kathmandu`\n',
            'export $(echo TZ=Asia/Kathmandu)\n',
            ': ${TZ:=Asia/Kathmandu}\n',
            "  'TZ'=UTC\n",
            'declare -n REF=TZ\n',
            "export $'\\x54\\x5a=Asia/Kathmandu'\n",
            '. /etc/default/other-tz\n',
            'printf -v TZ %s Asia/Tokyo\n',
            'read TZ <<< Asia/Tokyo\n',
            'mapfile TZ\n',
            'getopts x TZ\n',
            ': ${TZ:=UTC}\n',
            'TZ=${FOO}\n',
            'FOO=${BAR:-${BAZ}}\n',
            'FOO=$(date)\n',
            'FOO=${BAR:-$(date)}\n',
            'FOO=${BAR:-`date`}\n',
            'FOO=${BAR\n',
            'umask ${UMASK}\n',
            '${CMD} 0027\n',
            'echo ${FOO}\n',
            'FOO="${BAR}" ; TZ=UTC\n',
            'FOO=${BAR} TZ=UTC\n',
            'FOO=${!REF:=Asia/Tokyo}\n',
            'export ${!REF:=UTC}\n',
            'export FOO\\=${BAR}\n',
            'export FOO=${BAR}\n',
            'export FOO\\="${BAR}"\n',
            'FOO\\=${BAR}\n',
            'export "TZ=${UTC_VAR}"\n',
        ):
            env_file = self.root / 'cron-default'
            env_file.write_text(bad_env, encoding='utf-8')
            with self.subTest(bad_env=bad_env):
                self.assertFalse(schedule.host_timezone_is_utc(utc_localtime, missing_tz, env_paths=(env_file,)))
        for good_env in (
            '# comment mentioning CRON_TZ=America/New_York\n'
            'EXTRA_OPTS="" # TZ=America/Los_Angeles\n'
            'QUOTED="TZ=America/New_York"\n'
            'OTHER_TZ=America/New_York\n'
            'export TZ="UTC"\n',
            'export "TZ=UTC"\n',
            'echo "TZ=UTC is the default"\n',
            'EXTRA_OPTS="" # TZ=UTC is the default\n',
            'TZ="UTC"\n',
            'export EXTRA_OPTS="-L 2"\n',
            'FOO=1 export EXTRA_OPTS="-L 2"\n',
            'export QUOTED="TZ=America/New_York"\n',
            'command -p export TZ=UTC\n',
            "echo '{'\n",
            "echo '}'\n",
            "eval ':'\n",
            "echo ';'\n",
            "echo '; export TZ=Asia/Tokyo'\n",
            'PATH="/usr/local/bin:$PATH"\n',
            'PATH="/usr/local/bin:${PATH}"\n',
            'FOO="${BAR:-baz}"\n',
            'export FOO="${BAR}"\n',
            "export FOO='${BAR}'\n",
            'umask 0027\n',
            'ulimit -n 4096\n',
            '# trailing backslash comment \\\nTZ=UTC\n',
            "FOO='literal\\'\nTZ=UTC\n",
            'FOO="escaped\\\\"\nTZ=UTC\n',
        ):
            benign_env = self.root / 'cron-default-benign'
            benign_env.write_text(good_env, encoding='utf-8')
            with self.subTest(good_env=good_env):
                self.assertTrue(schedule.host_timezone_is_utc(utc_localtime, missing_tz, env_paths=(benign_env,)))


if __name__ == '__main__':
    unittest.main()
