#!/usr/bin/env python3
"""Manage only this application's block in the current user's crontab."""
import argparse
from datetime import datetime, timedelta
import io
import json
import os
from pathlib import Path
import re
import shlex
import stat
import subprocess
import sys
import time
import zoneinfo

from configuration import enabled, expand_path, load_config, read_regular_bytes
from operations import (atomic_private, child_environment, private_lock_file, private_log as _private_log,
                        runner_health)

ROOT = Path(__file__).resolve().parents[1]
BEGIN = '# BEGIN ONIONSURGE MANAGED HOURLY'
END = '# END ONIONSURGE MANAGED HOURLY'
LEGACY_BEGIN = '# BEGIN ONIONSURGE MANAGED DAILY'
LEGACY_END = '# END ONIONSURGE MANAGED DAILY'
MAX_CRONTAB = 1024 * 1024
FORBIDDEN_LINE_SEPS = ('\0', '\r', '\v', '\f', '\x1c', '\x1d', '\x1e', '\x85', '\u2028', '\u2029')
POSIX_SHELLS = {directory + name for directory in ('/bin/', '/usr/bin/')
                for name in ('sh', 'bash', 'dash', 'ksh', 'zsh')}
ZERO_OFFSET = timedelta(0)


class ScheduleError(ValueError):
    pass


class Busy(ScheduleError):
    pass


def require(condition, message):
    if not condition:
        raise ScheduleError(message)


def private_log(path):
    return _private_log(path, error_type=ScheduleError)


def private_lock(state, name):
    return private_lock_file(
        state, name, busy_error=Busy, error_type=ScheduleError,
        messages=('Private runtime directory ownership or permissions are invalid',
                  'Invalid private lock file', 'Another operation holds this lock'),
    )


def split_managed(text):
    require(not any(ch in text for ch in FORBIDDEN_LINE_SEPS) and len(text) <= MAX_CRONTAB, 'Invalid crontab content')
    parts = text.split('\n')
    lines = [part + '\n' for part in parts[:-1]] + ([parts[-1]] if parts[-1] else [])
    h_starts = [i for i, line in enumerate(lines) if line.rstrip('\n') == BEGIN]
    h_ends = [i for i, line in enumerate(lines) if line.rstrip('\n') == END]
    d_starts = [i for i, line in enumerate(lines) if line.rstrip('\n') == LEGACY_BEGIN]
    d_ends = [i for i, line in enumerate(lines) if line.rstrip('\n') == LEGACY_END]
    has_hourly = bool(h_starts or h_ends)
    has_daily = bool(d_starts or d_ends)
    if not has_hourly and not has_daily:
        return text, None
    require(not (has_hourly and has_daily), 'Ambiguous managed crontab markers')
    starts, ends = (h_starts, h_ends) if has_hourly else (d_starts, d_ends)
    require(len(starts) == len(ends) == 1 and starts[0] < ends[0], 'Ambiguous managed crontab markers')
    begin, end = starts[0], ends[0]
    return ''.join(lines[:begin] + lines[end + 1:]), ''.join(lines[begin:end + 1])


def replace_managed(text, block):
    other, existing = split_managed(text)
    if existing is not None:
        return text.replace(existing, block or '', 1)
    if block is None:
        return other
    require(not other or other.endswith('\n'), 'Existing crontab must end with a newline')
    return other + block


def _unquote(value):
    if len(value) >= 2 and value[0] == value[-1] and value[0] in {'"', "'"}:
        return value[1:-1]
    return value


def compatible_shell(text):
    """Cron environment assignments apply to the entries that follow them."""
    if any(ch in text for ch in FORBIDDEN_LINE_SEPS):
        return False
    shell = '/bin/sh'
    for line in text.split('\n'):
        if line in (BEGIN, LEGACY_BEGIN):
            break
        match = re.fullmatch(r'[ \t]*SHELL[ \t]*=[ \t]*(.*?)[ \t]*', line)
        if match:
            shell = _unquote(match[1])
    return shell in POSIX_SHELLS and Path(shell).is_file() and os.access(shell, os.X_OK)


FIXED_UTC_ZONES = {
    'UTC', 'Etc/UTC', 'Etc/GMT', 'Etc/GMT+0', 'Etc/GMT-0', 'Etc/GMT0',
    'Etc/Greenwich', 'Etc/UCT', 'Etc/Universal', 'Etc/Zulu',
    'GMT', 'GMT+0', 'GMT-0', 'GMT0', 'Greenwich', 'UCT', 'Universal', 'Zulu',
}
FIXED_UTC_FOOTERS = {'UTC0', 'GMT0', 'UCT0'}
FIXED_UTC_ABBRS = {'UTC', 'GMT', 'UCT', 'Z'}


def _read_bounded_regular(path, limit):
    try:
        return read_regular_bytes(path, limit, follow_symlinks=True)
    except (OSError, ValueError):
        return None


def _tzif_is_fixed_utc(raw):
    if not raw or len(raw) < 44 or len(raw) > 65536 or raw[:4] != b'TZif' or raw[4:5] not in {b'\x00', b'2', b'3', b'4'}:
        return False
    isutcnt, isstdcnt, leapcnt, timecnt, typecnt, charcnt = (
        int.from_bytes(raw[offset:offset + 4], 'big') for offset in range(20, 44, 4)
    )
    if leapcnt != 0 or typecnt < 1 or isutcnt not in (0, typecnt) or isstdcnt not in (0, typecnt):
        return False
    try:
        v1_end = 44 + timecnt * 5 + typecnt * 6 + charcnt + isstdcnt + isutcnt
        if raw[4:5] != b'\x00':
            if (len(raw) < v1_end + 44 or raw[v1_end:v1_end + 4] != b'TZif'
                    or raw[v1_end + 4:v1_end + 5] != raw[4:5]):
                return False
            b_isutcnt, b_isstdcnt, b_leapcnt, b_timecnt, b_typecnt, b_charcnt = (
                int.from_bytes(raw[v1_end + offset:v1_end + offset + 4], 'big') for offset in range(20, 44, 4)
            )
            if (b_leapcnt != 0 or b_typecnt < 1
                    or b_isutcnt not in (0, b_typecnt) or b_isstdcnt not in (0, b_typecnt)):
                return False
            block_start, t_size = v1_end + 44, 8
        else:
            b_isutcnt, b_isstdcnt, b_timecnt, b_typecnt, b_charcnt = isutcnt, isstdcnt, timecnt, typecnt, charcnt
            block_start, t_size = 44, 4
        types_offset = block_start + b_timecnt * t_size
        ttinfo_offset = types_offset + b_timecnt
        abbrs_offset = ttinfo_offset + b_typecnt * 6
        section_end = abbrs_offset + b_charcnt + b_isstdcnt + b_isutcnt
        if raw[4:5] == b'\x00':
            if len(raw) != section_end:
                return False
        else:
            if (len(raw) < section_end + 3 or raw[section_end:section_end + 1] != b'\n'
                    or not raw.endswith(b'\n')):
                return False
            footer_bytes = raw[section_end + 1:-1]
            if b'\n' in footer_bytes or footer_bytes.decode('ascii', 'strict') not in FIXED_UTC_FOOTERS:
                return False
        used_types = {0} | set(raw[types_offset:ttinfo_offset])
        for idx in used_types:
            if idx >= b_typecnt:
                return False
            rec = ttinfo_offset + idx * 6
            utoff = int.from_bytes(raw[rec:rec + 4], 'big', signed=True)
            isdst = raw[rec + 4]
            abbr_idx = raw[rec + 5]
            if utoff != 0 or isdst != 0 or abbr_idx >= b_charcnt:
                return False
            abbr_end = raw.find(b'\x00', abbrs_offset + abbr_idx, abbrs_offset + b_charcnt)
            if abbr_end == -1:
                return False
            if raw[abbrs_offset + abbr_idx:abbr_end].decode('ascii', 'strict') not in FIXED_UTC_ABBRS:
                return False
        tz = zoneinfo.ZoneInfo.from_file(io.BytesIO(raw), key='localtime')
    except (ValueError, AssertionError, EOFError, IndexError, KeyError, UnicodeDecodeError):
        return False
    probes = [datetime(1970, 1, 1, 12, tzinfo=tz), datetime(2030, 1, 1, 12, tzinfo=tz), datetime.now(tz)]
    for pos in range(block_start, types_offset, t_size):
        ts = int.from_bytes(raw[pos:pos + t_size], 'big', signed=True)
        for sample in (ts - 1, ts):
            if 0 <= sample <= 253402200000:
                probes.append(datetime.fromtimestamp(sample, tz=tz))
    return all(tz.utcoffset(dt) == ZERO_OFFSET and tz.dst(dt) == ZERO_OFFSET and tz.tzname(dt) in FIXED_UTC_ABBRS
               for dt in probes)


def _strip_posix_comment(line):
    in_single = in_double = escaped = in_word = False
    for index, ch in enumerate(line):
        if in_single:
            in_single = ch != "'"
        elif escaped:
            escaped = False
            in_word = True
        elif in_double:
            if ch == '\\':
                escaped = True
            elif ch == '"':
                in_double = False
        elif ch == "'":
            in_single = in_word = True
        elif ch == '"':
            in_double = in_word = True
        elif ch == '\\':
            escaped = in_word = True
        elif ch in ' \t':
            in_word = False
        elif ch == '#' and not in_word:
            return line[:index]
        else:
            in_word = True
    return line


SHELL_COMPOUND_KEYWORDS = frozenset({
    'if', 'then', 'elif', 'else', 'fi', 'case', 'esac', 'for', 'select',
    'while', 'until', 'do', 'done', '{', '}', '[[', ']]', 'function',
    'coproc', 'time', '!',
})


def _is_env_assignment(token):
    if '=' not in token:
        return False
    name, _ = token.split('=', 1)
    return name.isascii() and name.isidentifier()


def _scan_param_expansion(text, start):
    """Validate `${...}` outside single quotes and return index past `}` or -1."""
    close = text.find('}', start + 2)
    if close == -1:
        return -1
    inner = text[start + 2:close]
    if not inner or any(ch in inner for ch in '\\\'\"`$;|&(){!') or 'TZ' in inner:
        return -1
    return close + 1


def _split_raw_words(text):
    """Split a POSIX segment into raw words preserving quotes and escapes."""
    words = []
    cur = []
    in_word = in_single = in_double = escaped = False
    for ch in text:
        if escaped:
            cur.append(ch)
            escaped = False
            in_word = True
        elif in_single:
            cur.append(ch)
            if ch == "'":
                in_single = False
            in_word = True
        elif in_double:
            cur.append(ch)
            if ch == '\\':
                escaped = True
            elif ch == '"':
                in_double = False
            in_word = True
        elif ch == '\\':
            cur.append(ch)
            escaped = True
            in_word = True
        elif ch == "'":
            cur.append(ch)
            in_single = True
            in_word = True
        elif ch == '"':
            cur.append(ch)
            in_double = True
            in_word = True
        elif ch in ' \t\r\n':
            if in_word:
                words.append(''.join(cur))
                cur.clear()
                in_word = False
        else:
            cur.append(ch)
            in_word = True
    if in_word:
        words.append(''.join(cur))
    return words


def _is_unquoted_lhs_assign(raw_word):
    return '=' in raw_word and not any(ch in raw_word.split('=', 1)[0] for ch in '\\\'\"')


def _has_unquoted_param_expansion(raw_word):
    in_single = in_double = escaped = False
    index = 0
    while index < len(raw_word):
        ch = raw_word[index]
        if escaped:
            escaped = False
        elif in_single:
            if ch == "'":
                in_single = False
        elif in_double:
            if ch == '\\':
                escaped = True
            elif ch == '"':
                in_double = False
        elif ch == '\\':
            escaped = True
        elif ch == "'":
            in_single = True
        elif ch == '"':
            in_double = True
        elif raw_word.startswith('${', index):
            return True
        index += 1
    return False


def _segment_is_utc(segment, *, raw_words, allow_tz_set, raw_stripped, _depth=0):
    if len(raw_words) != len(segment):
        return False
    if any(('`' in tok or '$(' in tok) and ('TZ' in tok or 'CRON_TZ' in tok) for tok in segment):
        return False
    idx = 0
    while idx < len(segment) and _is_env_assignment(segment[idx]) and _is_unquoted_lhs_assign(raw_words[idx]):
        idx += 1
    assigns, rest, raw_rest = segment[:idx], segment[idx:], raw_words[idx:]
    while rest:
        if '${' in rest[0]:
            return False
        cmd_word = rest[0].rsplit('/', 1)[-1]
        if cmd_word not in ('command', 'builtin', 'noglob'):
            break
        rest = rest[1:]
        raw_rest = raw_rest[1:]
        while rest and rest[0].startswith('-') and rest[0] != '-':
            if '${' in rest[0]:
                return False
            opt = rest.pop(0)
            raw_rest.pop(0)
            if opt not in ('-p', '--'):
                allow_tz_set = False
            if opt == '--':
                break
    if len(assigns) == 1 and not rest and allow_tz_set:
        name, val = assigns[0].split('=', 1)
        if name in ('TZ', 'CRON_TZ'):
            return raw_stripped.lstrip().startswith(('TZ=', 'CRON_TZ=')) and val in FIXED_UTC_ZONES
        return True
    if any(tok.split('=', 1)[0] in ('TZ', 'CRON_TZ') for tok in assigns):
        return False
    if not rest:
        return True
    if '${' in rest[0] or rest[0] in SHELL_COMPOUND_KEYWORDS:
        return False
    prog, args, raw_args = rest[0].rsplit('/', 1)[-1], rest[1:], raw_rest[1:]
    if '${' in prog or prog in ('TZ', 'CRON_TZ') or (_is_env_assignment(rest[0]) and rest[0].split('=', 1)[0] in ('TZ', 'CRON_TZ')):
        return False
    if prog in ('export', 'typeset', 'declare', 'local', 'readonly'):
        if prog in ('typeset', 'declare', 'local') and any(
            (a.startswith('-') and not a.startswith('--') and 'n' in a[1:]) or a == '--nameref'
            for a in args
        ):
            return False
        if any(
            '${' in arg and (
                not _is_env_assignment(arg)
                or not _is_unquoted_lhs_assign(raw_arg)
                or _has_unquoted_param_expansion(raw_arg)
            )
            for arg, raw_arg in zip(args, raw_args)
        ):
            return False
        if allow_tz_set and not assigns and prog == 'export' and len(args) == 1:
            if _is_env_assignment(args[0]):
                name, val = args[0].split('=', 1)
                return val in FIXED_UTC_ZONES if name in ('TZ', 'CRON_TZ') else True
            return args[0] not in ('TZ', 'CRON_TZ') and (args[0].startswith('-') or (args[0].isascii() and args[0].isidentifier()))
        return all(
            (arg.split('=', 1)[0] not in ('TZ', 'CRON_TZ')
             if _is_env_assignment(arg)
             else (arg not in ('TZ', 'CRON_TZ') and (arg.startswith('-') or (arg.isascii() and arg.isidentifier()))))
            for arg in args
        )
    if prog in ('unset', 'env'):
        return not any(
            '${' in arg
            or arg in ('TZ', 'CRON_TZ', '-S', '-uTZ', '--unset=TZ', '--unset=CRON_TZ')
            or arg.startswith(('TZ=', 'CRON_TZ=', '-S', '--split-string'))
            for arg in args
        )
    if prog in ('.', 'source', 'exec'):
        return False
    if prog in ('eval', 'sh', 'bash', 'dash', 'ksh', 'zsh'):
        if any('$' in arg or '`' in arg or any(c in arg for c in '\r\x85\u2028\u2029') for arg in args):
            return False
        candidates = [' '.join(args)] if prog == 'eval' else args
        for candidate in candidates:
            for subline in candidate.splitlines():
                if not _env_line_is_utc(subline, _depth=_depth + 1):
                    return False
        return True
    return prog in ('echo', ':', 'true', 'umask', 'ulimit') and not any(op in arg for arg in args for op in ('$(', '${', '`'))


def _env_line_is_utc(line, *, _depth=0):
    if _depth > 3:
        return False
    stripped = _strip_posix_comment(line)
    raw_segments = []
    buf = []
    in_single = in_double = escaped = False
    has_op = False
    index = 0
    length = len(stripped)
    while index < length:
        ch = stripped[index]
        if escaped:
            buf.append(ch)
            escaped = False
            index += 1
        elif in_single:
            buf.append(ch)
            if ch == "'":
                in_single = False
            index += 1
        elif ch == '`' or stripped.startswith(('${', '$(', "$'", '$"'), index):
            if stripped.startswith('${', index):
                after = _scan_param_expansion(stripped, index)
                if after == -1:
                    return False
                buf.append(stripped[index:after])
                index = after
            else:
                return False
        elif in_double:
            buf.append(ch)
            if ch == '\\':
                escaped = True
            elif ch == '"':
                in_double = False
            index += 1
        elif ch == '\\':
            buf.append(ch)
            escaped = True
            index += 1
        elif ch == "'":
            buf.append(ch)
            in_single = True
            index += 1
        elif ch == '"':
            buf.append(ch)
            in_double = True
            index += 1
        elif ch in '();&|':
            has_op = True
            seg = ''.join(buf).strip()
            if seg:
                raw_segments.append(seg)
            buf.clear()
            index += 1
        else:
            buf.append(ch)
            index += 1
    if escaped or in_single or in_double:
        return False
    seg = ''.join(buf).strip()
    if seg:
        raw_segments.append(seg)
    try:
        parsed_segments = []
        for part in raw_segments:
            seg = shlex.split(part, comments=False, posix=True)
            if seg:
                parsed_segments.append((_split_raw_words(part), seg))
    except ValueError:
        return False
    if not parsed_segments:
        return True
    allow_tz_set = _depth == 0 and not has_op and len(parsed_segments) == 1
    return all(
        _segment_is_utc(seg, raw_words=raw_words, allow_tz_set=allow_tz_set, raw_stripped=stripped, _depth=_depth)
        for raw_words, seg in parsed_segments
    )


def host_timezone_is_utc(localtime_path='/etc/localtime', timezone_path='/etc/timezone',
                         env_paths=('/etc/environment', '/etc/default/cron', '/etc/sysconfig/crond')):
    """Inspect system timezone files directly, independent of installer TZ."""
    if not _tzif_is_fixed_utc(_read_bounded_regular(localtime_path, 65536)):
        return False
    if os.path.lexists(timezone_path):
        tz_raw = _read_bounded_regular(timezone_path, 256)
        if tz_raw is None:
            return False
        try:
            tz_name = tz_raw.decode('ascii', 'strict').strip()
        except UnicodeDecodeError:
            return False
        if not tz_name or tz_name not in FIXED_UTC_ZONES:
            return False
    for env_path in env_paths:
        if not os.path.lexists(env_path):
            continue
        env_raw = _read_bounded_regular(env_path, 65536)
        if env_raw is None:
            return False
        try:
            env_text = env_raw.decode('utf-8-sig', 'strict')
        except UnicodeDecodeError:
            return False
        if any(ch in env_text for ch in FORBIDDEN_LINE_SEPS):
            return False
        if not all(_env_line_is_utc(line) for line in env_text.split('\n')):
            return False
    return True


def utc_cron_context(text):
    """Ensure cron itself fires in UTC via CRON_TZ=UTC or a UTC host daemon."""
    if any(ch in text for ch in FORBIDDEN_LINE_SEPS):
        return False
    seen = {}
    for line in text.split('\n'):
        if line in (BEGIN, LEGACY_BEGIN):
            break
        match = re.fullmatch(r'[ \t]*(CRON_TZ|TZ)[ \t]*=[ \t]*(.*?)[ \t]*', line)
        if match:
            seen[match[1]] = _unquote(match[2])
    if 'TZ' in seen and seen['TZ'] not in {'UTC', 'Etc/UTC'}:
        return False
    if 'CRON_TZ' in seen:
        return seen['CRON_TZ'] in {'UTC', 'Etc/UTC'}
    return host_timezone_is_utc()


def log_path(config):
    state = expand_path(config['STATE_DIR'])
    return expand_path(config.get('LOG_DIR', str(state / 'logs'))) / 'cron.log'


def utc_gate_enabled(config):
    try:
        return enabled(config, 'CRON_UTC_GATE')
    except ValueError as error:
        raise ScheduleError(str(error)) from error


def utc_date_binary():
    """Pin a trusted system date and prove its -u output without trusting TZ."""
    for candidate in ('/bin/date', '/usr/bin/date'):
        try:
            path = Path(candidate).resolve(strict=True)
        except FileNotFoundError:
            continue
        info = path.stat()
        require(stat.S_ISREG(info.st_mode) and info.st_uid == 0 and not info.st_mode & 0o022
                and os.access(path, os.X_OK), 'UTC gate requires a trusted system date executable')
        for parent in path.parents:
            parent_info = parent.stat()
            require(parent_info.st_uid == 0 and not parent_info.st_mode & 0o022,
                    'UTC gate requires a trusted system date path')
        before = time.strftime('%M\n', time.gmtime())
        result = subprocess.run([str(path), '-u', '+%M'], capture_output=True, text=True, timeout=5,
                                env=dict(child_environment(), TZ='PST8PDT'))
        after = time.strftime('%M\n', time.gmtime())
        require(result.returncode == 0 and result.stdout in {before, after},
                'System date does not provide the required UTC clock format')
        return str(path)
    raise ScheduleError('UTC gate requires /bin/date or /usr/bin/date')


def managed_block(config, config_path, deploy_dir=ROOT, interpreter=None):
    minute = config.get('CRON_MINUTE', '5')
    require(isinstance(minute, str) and re.fullmatch(r'[0-9]{1,2}', minute) and 0 <= int(minute) <= 59,
            'CRON_MINUTE must be an integer from 0 through 59')
    values = [str(Path(interpreter or sys.executable).resolve()),
              str(Path(deploy_dir).resolve() / 'scripts/run_hourly.py'), '--config', str(expand_path(config_path)),
              str(log_path(config))]
    require(all(not any(ord(c) < 32 or ord(c) == 127 or c == '\\' or c in FORBIDDEN_LINE_SEPS for c in value)
                for value in values),
            'Cron paths cannot contain backslashes or control characters')
    runner = 'TZ=UTC ' + shlex.join(values[:-1])
    if utc_gate_enabled(config):
        clock = shlex.join([utc_date_binary(), '-u', '+%M'])
        due = f'{int(minute):02d}'
        # Only the clock command runs outside the due UTC minute. Its errors
        # and the due runner's output share the existing private cron log.
        command = ('umask 077; if onionsurge_utc_minute=$(' + clock + '); then '
                   'if [ "$onionsurge_utc_minute" = ' + shlex.quote(due) + ' ]; then ' + runner
                   + '; fi; else exit 1; fi')
        timing = '* * * * *'
    else:
        command = 'umask 077; ' + runner
        timing = f'{int(minute)} * * * *'
    command += ' >> ' + shlex.quote(values[-1]) + ' 2>&1'
    # Cron interprets percent signs before the shell, including inside quotes.
    command = command.replace('%', '\\%')
    return BEGIN + '\n' + timing + ' ' + command + '\n' + END + '\n'


class Crontab:
    def read(self):
        result = subprocess.run(['crontab', '-l'], capture_output=True, timeout=10, env=child_environment())
        stderr = result.stderr.decode('utf-8', errors='replace') if isinstance(result.stderr, bytes) else result.stderr
        if result.returncode == 1 and re.fullmatch(r'no crontab for [^\r\n]+\n?', stderr):
            return ''
        require(result.returncode == 0 and isinstance(result.stdout, bytes) and len(result.stdout) <= MAX_CRONTAB * 4,
                'Could not read user crontab')
        try:
            text = result.stdout.decode('utf-8', 'strict')
        except UnicodeDecodeError as error:
            raise ScheduleError('Could not read user crontab') from error
        require(len(text) <= MAX_CRONTAB, 'Could not read user crontab')
        return text

    def write(self, text):
        require(len(text) <= MAX_CRONTAB, 'Crontab size limit')
        result = subprocess.run(['crontab', '-'], input=text.encode('utf-8'), capture_output=True,
                                timeout=10, env=child_environment())
        require(result.returncode == 0, 'Crontab update failed; verify before retrying')


def verify(config, config_path, crontab=None, deploy_dir=ROOT, *, _expected_block=None, _tolerate_health_error=False):
    crontab = crontab or Crontab()
    text = crontab.read()
    _, block = split_managed(text)
    matches = (block is not None and compatible_shell(text)
               and (utc_gate_enabled(config) or utc_cron_context(text))
               and block == (managed_block(config, config_path, deploy_dir) if _expected_block is None else _expected_block))
    state = 'paused' if block is None else 'installed' if matches else 'drifted'
    path = log_path(config)
    private = False
    try:
        info = path.lstat()
        private = stat.S_ISREG(info.st_mode) and info.st_uid == os.geteuid() and not info.st_mode & 0o077
    except FileNotFoundError:
        pass
    result = {'schedule': state, 'log_private': private}
    try:
        return {**result, **runner_health(config)}
    except (OSError, ValueError):
        if not _tolerate_health_error:
            raise
        return {**result, 'health_warning': 'hourly_health_unavailable'}


def change(action, config, config_path, crontab=None, deploy_dir=ROOT):
    require(action in {'install', 'remove'}, 'Unknown schedule operation')
    crontab = crontab or Crontab()
    state = expand_path(config['STATE_DIR'])
    block = None
    previous_umask = os.umask(0o077)
    try:
        with private_lock(state, 'schedule.lock'):
            before = crontab.read()
            if action == 'install':
                require(compatible_shell(before), 'The managed entry requires a supported POSIX cron SHELL')
                require(utc_gate_enabled(config) or utc_cron_context(before), 'The managed hourly entry requires a UTC cron timezone')
                block = managed_block(config, config_path, deploy_dir)
                with private_log(log_path(config)):
                    pass
            after = replace_managed(before, block)
            if after != before:
                # No crontab CAS API exists. Detect an intervening edit, then verify
                # the result. Administrators must serialize unrelated crontab edits.
                require(crontab.read() == before, 'User crontab changed during preparation')
                atomic_private(state / 'crontab-before.txt', before.encode())
                crontab.write(after)
                require(crontab.read() == after, 'Crontab verification failed; inspect current entries')
            return verify(config, config_path, crontab, deploy_dir, _expected_block=block,
                          _tolerate_health_error=(action == 'remove'))
    finally:
        os.umask(previous_umask)


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('action', choices=['install', 'verify', 'remove'])
    parser.add_argument('--config', required=True)
    args = parser.parse_args(argv)
    try:
        config = load_config(args.config, required=('STATE_DIR',))
        result = verify(config, args.config) if args.action == 'verify' else change(args.action, config, args.config)
    except (OSError, ValueError, TypeError, KeyError, subprocess.SubprocessError):
        parser.exit(1, 'Schedule operation failed; inspect private configuration and current user crontab.\n')
    print(json.dumps(result, sort_keys=True))
    if result['schedule'] == 'drifted':
        raise SystemExit(1)


if __name__ == '__main__':
    main()
