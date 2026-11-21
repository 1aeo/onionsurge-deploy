"""Small shared checks for running explicitly pinned, reviewed checkouts."""
from contextlib import contextmanager
import ctypes
from datetime import datetime, timezone
import errno
import fcntl
import hashlib
import json
import math
import os
from pathlib import Path
import re
import stat
import subprocess
import sys
import tempfile
import urllib.request
import uuid

from configuration import COMMIT, SHA256, expand_path, matched, read_regular_bytes

MAX_JSON_DEPTH = 64
MAX_ROOT = 64 * 1024
MAX_OBJECT = 8 * 1024**2
PRIVATE_KEYS = frozenset({
    'host', 'hostname', 'instance', 'relay_fingerprint', 'member_id', 'pid',
    'boot_id', 'start_ticks', 'cookie_file', 'password', 'private_key', 'token', 'credentials',
})
FORBIDDEN_HOURLY_KEYS = frozenset({'hour', 'sampled_hour', 'hourly_relays', 'hourly_expected_relays', 'current24'})
SUB_24H_SUFFIX = r'(?:0*[01]d|0+[0-9]*h|(?:[0-9]|1[0-9]|2[0-3])h|[0-9]+[sm])'
FRONTEND = frozenset({'index.html', 'app.js', 'style.css', 'live.js'})
RUN_ID = re.compile(r'\d{8}T\d{6}Z-[a-f0-9]{12}\Z')
PROMOTION_WARNINGS = frozenset({'pending_marker_cleanup_failed', 'pending_marker_removal_not_durable'})
INDEX_CSP = ("default-src 'none'; script-src 'self'; style-src 'self' 'unsafe-inline'; "
             "style-src-elem 'self'; style-src-attr 'unsafe-inline'; img-src 'self'; "
             "connect-src 'self'; base-uri 'none'; form-action 'none'; "
             "frame-ancestors 'none'; object-src 'none'")
SANDBOX_CSP = "default-src 'none'; sandbox; frame-ancestors 'none'"
PERMISSIONS_POLICY = ("accelerometer=(), browsing-topics=(), camera=(), geolocation=(), "
                      "gyroscope=(), magnetometer=(), microphone=(), payment=(), usb=()")


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, request, fp, code, msg, headers, newurl):
        return None


def _check_json_depth(text, limit=MAX_JSON_DEPTH):
    """Enforce structural bracket depth limit in O(N) time and O(1) auxiliary memory."""
    depth = 0
    in_string = False
    escaped = False
    for ch in text:
        if in_string:
            if escaped:
                escaped = False
            elif ch == '\\':
                escaped = True
            elif ch == '"':
                in_string = False
        elif ch == '"':
            in_string = True
        elif ch in '[{':
            depth += 1
            if depth > limit:
                raise ValueError('Manifest JSON nesting exceeds limit')
        elif ch in ']}':
            depth -= 1


def strict_json_bytes(raw):
    def reject_constant(value):
        raise ValueError('Non-finite JSON constant in manifest: ' + value)

    def parse_finite_float(text):
        value = float(text)
        if not math.isfinite(value):
            raise ValueError('Non-finite JSON float in manifest')
        return value

    def unique_object(pairs):
        result = {}
        for key, value in pairs:
            if key in result:
                raise ValueError('Duplicate JSON object key in manifest: ' + key)
            result[key] = value
        return result

    if isinstance(raw, str):
        text = raw
    elif isinstance(raw, (bytes, bytearray)):
        raw_bytes = bytes(raw)
        try:
            text = raw_bytes.decode(json.detect_encoding(raw_bytes))
        except UnicodeDecodeError as error:
            raise ValueError('Invalid JSON text encoding') from error
    else:
        raise ValueError('Invalid JSON input type')
    _check_json_depth(text)
    try:
        return json.loads(text, parse_constant=reject_constant, parse_float=parse_finite_float,
                          object_pairs_hook=unique_object)
    except RecursionError as error:
        raise ValueError('Manifest JSON nesting exceeds limit') from error


def json_bytes(value):
    return (json.dumps(value, sort_keys=True, separators=(',', ':'), allow_nan=False) + '\n').encode()


def sha256(raw):
    return hashlib.sha256(raw).hexdigest()


def stamped_id(prefix='', when=None, suffix=''):
    stamp = datetime.now(timezone.utc) if when is None else datetime.fromtimestamp(when, timezone.utc)
    return prefix + stamp.strftime('%Y%m%dT%H%M%SZ-') + uuid.uuid4().hex[:12] + suffix


def sync_directory(path):
    fd = os.open(path, os.O_RDONLY | os.O_DIRECTORY)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def atomic_write(path, raw, mode=0o600, *, sync_parent=True):
    fd, temporary = tempfile.mkstemp(prefix='.' + path.name + '-', dir=path.parent)
    try:
        with os.fdopen(fd, 'wb') as stream:
            stream.write(raw)
            os.fchmod(stream.fileno(), mode)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
        temporary = None
        if sync_parent:
            sync_directory(path.parent)
    finally:
        if temporary is not None:
            try:
                os.unlink(temporary)
            except OSError:
                pass  # Never replace the original write failure with cleanup.


def atomic_private(path, raw, *, sync_parent=True):
    atomic_write(path, raw, mode=0o600, sync_parent=sync_parent)


def _try_hardlink(path, raw, donor_path, mode, sync_parent):
    fd = None
    try:
        fd = os.open(donor_path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
        info = os.fstat(fd)
        if (not stat.S_ISREG(info.st_mode) or info.st_uid != os.geteuid()
                or stat.S_IMODE(info.st_mode) != mode or info.st_size != len(raw)):
            return False
        with os.fdopen(fd, 'rb') as stream:
            fd = None
            donor_bytes = stream.read(len(raw) + 1)
    except OSError:
        return False
    finally:
        if fd is not None:
            try:
                os.close(fd)
            except OSError:
                pass
    if len(donor_bytes) != len(raw) or sha256(donor_bytes) != sha256(raw):
        return False
    temp_link = path.parent / f'.{path.name}-{uuid.uuid4().hex[:12]}.link'
    try:
        os.link(donor_path, temp_link, follow_symlinks=False)
        link_info = temp_link.lstat()
        if ((link_info.st_dev, link_info.st_ino) != (info.st_dev, info.st_ino)
                or not stat.S_ISREG(link_info.st_mode)
                or link_info.st_uid != os.geteuid()
                or stat.S_IMODE(link_info.st_mode) != mode
                or link_info.st_size != len(raw)):
            return False
        os.replace(temp_link, path)
        temp_link = None
        if sync_parent:
            sync_directory(path.parent)
        return True
    except (OSError, NotImplementedError):
        return False
    finally:
        if temp_link is not None:
            try:
                os.unlink(temp_link)
            except OSError:
                pass


def link_or_atomic_write(path, raw, *, donor_path=None, mode=0o600, sync_parent=True):
    """Hardlink a verified identical immutable donor inode when safe, else atomic_write."""
    if donor_path is not None and _try_hardlink(path, raw, donor_path, mode, sync_parent):
        return True
    atomic_write(path, raw, mode=mode, sync_parent=sync_parent)
    return False


def parse_state_retain_runs(config, *, error_type=ValueError):
    """Parse and validate optional STATE_RETAIN_RUNS count (`2..168`, default 3)."""
    retain_raw = config.get('STATE_RETAIN_RUNS', '3')
    if not (isinstance(retain_raw, str) and retain_raw.isascii() and retain_raw.isdigit()
            and 2 <= int(retain_raw) <= 168):
        raise error_type('invalid_state_retain_runs')
    return int(retain_raw)


def rotate_private_log(path, *, max_bytes=10 * 1024 * 1024, backups=3, error_type=ValueError):
    """Rotate an owned non-symlink private log through .1..backups when >= max_bytes."""
    if not os.path.lexists(path):
        return False
    fd = None
    try:
        fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
        info = os.fstat(fd)
        if not stat.S_ISREG(info.st_mode) or info.st_uid != os.geteuid():
            raise error_type('Invalid private log file')
        if info.st_size < max_bytes:
            return False
        if stat.S_IMODE(info.st_mode) != 0o600:
            os.fchmod(fd, 0o600)
    except error_type:
        raise
    except OSError as error:
        raise error_type('Invalid private log file') from error
    finally:
        if fd is not None:
            try:
                os.close(fd)
            except OSError:
                pass
    for idx in range(1, backups + 1):
        backup = path.with_name(f'{path.name}.{idx}')
        if not os.path.lexists(backup):
            continue
        b_fd = None
        try:
            b_fd = os.open(backup, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
            b_info = os.fstat(b_fd)
            if not stat.S_ISREG(b_info.st_mode) or b_info.st_uid != os.geteuid():
                raise error_type('Invalid private log backup')
            if stat.S_IMODE(b_info.st_mode) != 0o600:
                os.fchmod(b_fd, 0o600)
        except error_type:
            raise
        except OSError as error:
            raise error_type('Invalid private log backup') from error
        finally:
            if b_fd is not None:
                try:
                    os.close(b_fd)
                except OSError:
                    pass
    for idx in range(backups, 1, -1):
        src = path.with_name(f'{path.name}.{idx - 1}')
        dst = path.with_name(f'{path.name}.{idx}')
        if os.path.lexists(src):
            os.replace(src, dst)
    os.replace(path, path.with_name(f'{path.name}.1'))
    fresh_fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW | os.O_NONBLOCK, 0o600)
    try:
        os.fchmod(fresh_fd, 0o600)
    finally:
        os.close(fresh_fd)
    sync_directory(path.parent)
    return True


def rename_noreplace(source, destination):
    """Atomically publish a complete directory without replacing any target."""
    source_bytes, destination_bytes = os.fsencode(source), os.fsencode(destination)
    if b'\0' in source_bytes or b'\0' in destination_bytes:
        raise ValueError('Paths cannot contain NUL bytes')
    if sys.platform.startswith('linux'):
        # Linux UAPI linux/fs.h: RENAME_NOREPLACE=1; linux/fcntl.h: AT_FDCWD=-100.
        name = 'renameat2'
        arguments = (-100, source_bytes, -100, destination_bytes, 1)
        types = [ctypes.c_int, ctypes.c_char_p, ctypes.c_int, ctypes.c_char_p, ctypes.c_uint]
    elif sys.platform == 'darwin':
        # macOS SDK sys/stdio.h: RENAME_EXCL=0x00000004.
        name = 'renamex_np'
        arguments = (source_bytes, destination_bytes, 0x00000004)
        types = [ctypes.c_char_p, ctypes.c_char_p, ctypes.c_uint]
    else:
        raise OSError(errno.ENOTSUP, 'Atomic no-replace directory publication is unsupported')
    try:
        operation = getattr(ctypes.CDLL(None, use_errno=True), name)
    except AttributeError as error:
        raise OSError(errno.ENOTSUP, 'Atomic no-replace directory publication is unavailable') from error
    operation.argtypes, operation.restype = types, ctypes.c_int
    if operation(*arguments) != 0:
        code = ctypes.get_errno()
        raise OSError(code, os.strerror(code), os.fspath(destination))


def private_log(path, *, error_type=ValueError):
    ensure_private_directory(path.parent, error_type, 'Private runtime directory ownership or permissions are invalid')
    fd = os.open(path, os.O_WRONLY | os.O_APPEND | os.O_CREAT | os.O_NOFOLLOW | os.O_NONBLOCK, 0o600)
    try:
        info = os.fstat(fd)
        if not (stat.S_ISREG(info.st_mode) and info.st_uid == os.geteuid()):
            raise error_type('Invalid private log file')
        if stat.S_IMODE(info.st_mode) != 0o600:
            os.fchmod(fd, 0o600)
        return os.fdopen(fd, 'a', encoding='utf-8')
    except BaseException:
        os.close(fd)
        raise


def ensure_web_directory(base_dir, target_dir, *, create=False, error_type=ValueError):
    """Ensure base_dir..target_dir exist without symlinks or world-writable bits."""
    rel = target_dir.relative_to(base_dir)
    cur = base_dir
    for part in ('', *rel.parts):
        if part:
            cur = cur / part
        created = False
        sgid = 0
        if not os.path.lexists(cur):
            if not create:
                return False
            parent = cur.parent
            sgid = stat.S_ISGID if os.path.lexists(parent) and (parent.lstat().st_mode & stat.S_ISGID) else 0
            old_umask = os.umask(0o027)
            try:
                cur.mkdir(mode=0o750, exist_ok=True)
            finally:
                os.umask(old_umask)
            created = True
        info = cur.lstat()
        if not (stat.S_ISDIR(info.st_mode) and info.st_uid == os.geteuid() and not (info.st_mode & 0o002)):
            raise error_type('unsafe_local_site_directory')
        if created and ((info.st_mode & 0o777) != 0o750 or (sgid and not (info.st_mode & stat.S_ISGID))):
            os.chmod(cur, sgid | 0o750)
        if created and cur != base_dir:
            sync_directory(cur.parent)
    return True


def public_values(value, *, allow_sub_24h=False, error_type=ValueError,
                  private_message='Forbidden private metadata field',
                  nonfinite_message='Non-finite JSON value',
                  forbidden_message='Forbidden sub-24h field'):
    stack = [value]
    while stack:
        node = stack.pop()
        if isinstance(node, dict):
            if PRIVATE_KEYS.intersection(node) or '__proto__' in node:
                raise error_type(private_message)
            if not allow_sub_24h:
                if FORBIDDEN_HOURLY_KEYS.intersection(node):
                    raise error_type(forbidden_message)
                series = node.get('series')
                if isinstance(series, list) and any(isinstance(item, dict) and 'values' in item for item in series):
                    raise error_type(forbidden_message)
            stack.extend(node.values())
        elif isinstance(node, list):
            stack.extend(node)
        elif isinstance(node, (int, float)) and not isinstance(node, bool):
            try:
                finite = math.isfinite(node)
            except OverflowError as error:
                raise error_type(nonfinite_message) from error
            if not finite:
                raise error_type(nonfinite_message)


def verify_frontend(directory, application_commit, *, error_type=ValueError):
    directory = Path(directory).absolute()
    if directory.is_symlink():
        raise error_type('invalid_frontend_release')
    directory = directory.resolve(strict=True)
    public = directory / 'public'
    if not stat.S_ISDIR(public.lstat().st_mode):
        raise error_type('invalid_frontend_public_directory')
    try:
        receipt = strict_json_bytes(read_regular_bytes(directory / 'asset-release.json', 65536, error_type=error_type))
    except ValueError as error:
        if isinstance(error, error_type):
            raise
        raise error_type('invalid_frontend_receipt') from error
    if not (isinstance(receipt, dict)
            and set(receipt) == {'schema_version', 'mode', 'application_commit', 'build_manifest_sha256', 'objects'}
            and type(receipt.get('schema_version')) is int and receipt['schema_version'] == 1
            and receipt.get('mode') == 'live'
            and receipt.get('application_commit') == application_commit
            and matched(SHA256, receipt.get('build_manifest_sha256'))
            and isinstance(receipt.get('objects'), dict) and set(receipt['objects']) == FRONTEND):
        raise error_type('invalid_frontend_receipt')
    if {path.name for path in public.iterdir()} != FRONTEND:
        raise error_type('unexpected_frontend_asset')
    page_bytes = None
    for name, expected in receipt['objects'].items():
        if not (isinstance(expected, dict) and set(expected) == {'sha256', 'bytes'}
                and matched(SHA256, expected['sha256']) and type(expected['bytes']) is int
                and 0 < expected['bytes'] <= MAX_OBJECT):
            raise error_type('invalid_frontend_object')
        raw = read_regular_bytes(public / name, expected['bytes'], error_type=error_type)
        if len(raw) != expected['bytes'] or sha256(raw) != expected['sha256']:
            raise error_type('frontend_integrity_failure')
        if name == 'index.html':
            page_bytes = raw
    page = page_bytes.decode('utf-8')
    if ('data-mode="live"' not in page or '<script src="data.js"' in page
            or '<script src="app.js"></script><script src="live.js"></script>' not in page):
        raise error_type('invalid_live_entrypoint')
    return public, receipt


def runner_health(config, *, error_type=ValueError):
    state = expand_path(config['STATE_DIR'])
    result = {'pending_promotion': os.path.lexists(state / 'pending-promotion.json'), 'run_busy': False,
              'last_run_status': 'none', 'last_release_id': None, 'publication_warning': None}
    lock = state / 'run.lock'
    if os.path.lexists(lock):
        fd = os.open(lock, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
        try:
            if not stat.S_ISREG(os.fstat(fd).st_mode):
                raise error_type('Invalid run lock')
            locked = False
            try:
                fcntl.flock(fd, fcntl.LOCK_SH | fcntl.LOCK_NB)
                locked = True
            except BlockingIOError:
                result['run_busy'] = True
            finally:
                if locked:
                    fcntl.flock(fd, fcntl.LOCK_UN)
        finally:
            os.close(fd)
    for filename in ('last-run.json', 'last-success.json'):
        path = state / filename
        if not os.path.lexists(path):
            continue
        raw = read_regular_bytes(path, MAX_ROOT, error_type=error_type)
        try:
            value = strict_json_bytes(raw)
        except ValueError as error:
            raise error_type('Invalid run receipt') from error
        if not isinstance(value, dict):
            raise error_type('Invalid run receipt')
        if filename == 'last-run.json':
            status = value.get('status')
            result['last_run_status'] = (
                status
                if isinstance(status, str) and status in {'running', 'failed', 'published', 'published_receipt_warning', 'up_to_date'}
                else 'invalid'
            )
            warning = value.get('publication_warning')
            if isinstance(warning, str) and warning in PROMOTION_WARNINGS:
                result['publication_warning'] = warning
        elif matched(RUN_ID, value.get('release_id')):
            result['last_release_id'] = value['release_id']
    return result


def child_environment():
    env = {k: os.environ[k] for k in ('PATH', 'HOME', 'TMPDIR', 'SYSTEMROOT') if k in os.environ}
    env.update({'LANG': 'C', 'LC_ALL': 'C', 'PYTHONDONTWRITEBYTECODE': '1'})
    return env


def paths_disjoint(runtime_paths, checkout_paths, error_type=ValueError,
                   message='Runtime path overlaps source checkout'):
    """Ensure every runtime path stays strictly outside and separate from checkouts."""
    checkouts = [Path(checkout).resolve() for checkout in checkout_paths]
    for runtime in runtime_paths:
        actual = Path(runtime).resolve()
        if any(actual.is_relative_to(c) or c.is_relative_to(actual) for c in checkouts):
            raise error_type(message)


def ensure_private_directory(path, error_type=ValueError, message='Unsafe private directory'):
    """Create a 0700 directory if missing and verify owner-only permissions."""
    try:
        if not os.path.lexists(path):
            path.mkdir(parents=True, exist_ok=True, mode=0o700)
        info = path.lstat()
    except OSError as error:
        raise error_type(message) from error
    if not stat.S_ISDIR(info.st_mode) or info.st_uid != os.geteuid() or info.st_mode & 0o077:
        raise error_type(message)


@contextmanager
def private_lock_file(state, name, *, busy_error, error_type=ValueError,
                      messages=('Unsafe private directory', 'Unsafe private lock file', 'Lock busy')):
    """Acquire a non-blocking exclusive flock inside a verified private directory."""
    ensure_private_directory(state, error_type, messages[0])
    try:
        fd = os.open(state / name, os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW | os.O_NONBLOCK, 0o600)
    except IsADirectoryError as error:
        raise error_type(messages[1]) from error
    try:
        info = os.fstat(fd)
        if not stat.S_ISREG(info.st_mode) or info.st_uid != os.geteuid() or info.st_mode & 0o077:
            raise error_type(messages[1])
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as error:
            raise busy_error(messages[2]) from error
        stream = os.fdopen(fd, 'a+b')
    except BaseException:
        os.close(fd)
        raise
    with stream:
        yield


def verify_checkout(path, repo_name, expected_commit):
    if repo_name not in {'onionsurge', 'onionsurge-deploy'} or not matched(COMMIT, expected_commit):
        raise ValueError('An approved repository and exact commit pin are required')
    path = Path(path).resolve(strict=True)
    env, repo = child_environment(), str(path)

    def git(*arguments, optional=False):
        result = subprocess.run(['git', '--no-optional-locks', '-c', 'core.fsmonitor=false', '-C', repo, *arguments],
                                capture_output=True, text=True, timeout=20, env=env)
        if len(result.stdout) > 1024**2 or (result.returncode and not optional):
            raise ValueError('Checkout inspection failed')
        return result

    rev = git('rev-parse', '--show-toplevel', 'HEAD^{commit}').stdout.rstrip('\n').rsplit('\n', 1)
    if len(rev) != 2:
        raise ValueError('Checkout inspection failed')
    top, commit = rev
    if Path(top).resolve() != path:
        raise ValueError('Configured path is not a checkout root')
    origin = git('remote', 'get-url', '--all', 'origin').stdout.strip()
    if origin not in {f'https://github.com/1aeo/{repo_name}', f'https://github.com/1aeo/{repo_name}.git',
                      f'git@github.com:1aeo/{repo_name}.git', f'ssh://git@github.com/1aeo/{repo_name}.git'}:
        raise ValueError('Checkout origin is not the approved repository')
    if git('status', '--porcelain=v1', '--untracked-files=all').stdout:
        raise ValueError('Checkout has uncommitted changes')
    if commit != expected_commit:
        raise ValueError('Checkout HEAD differs from its configured pin')
    remote_main = git('show-ref', '--verify', '--quiet', 'refs/remotes/origin/main', optional=True)
    if remote_main.returncode == 0:
        if git('merge-base', '--is-ancestor', commit, 'refs/remotes/origin/main', optional=True).returncode:
            raise ValueError('Pinned commit is not in the cached reviewed main history')
    elif remote_main.returncode != 1:
        raise ValueError('Could not inspect cached main history')
    return commit
