"""Read private assignment-only configuration without executing a shell."""
import os
from pathlib import Path
import re
import shlex
import stat

SHA256 = re.compile(r'[a-f0-9]{64}\Z')
COMMIT = re.compile(r'[a-f0-9]{40}\Z')
CF_ID = re.compile(r'[a-f0-9]{32}\Z')
PREFIX = re.compile(r'[a-z][a-z0-9_-]{0,62}\Z')
REGION = re.compile(r'[a-z]{3}[0-9]\Z')
ENV_LINE = re.compile(r'(?:export\s+)?([A-Z][A-Z0-9_]*)\s*=(.*)')
HOME_VAR = re.compile(r'\$\{HOME\}|\$HOME(?![A-Za-z0-9_])')


def matched(pattern, value):
    return isinstance(value, str) and bool(pattern.fullmatch(value))


def read_regular_bytes(path, max_bytes, *, follow_symlinks=False, private=False, error_type=ValueError):
    """Read bounded bytes from a regular file, refusing symlinks unless follow_symlinks=True."""
    flags = os.O_RDONLY | os.O_NONBLOCK | (0 if follow_symlinks else os.O_NOFOLLOW)
    fd = os.open(path, flags)
    try:
        info = os.fstat(fd)
        if not stat.S_ISREG(info.st_mode):
            raise error_type('Path must be a regular file')
        if private and (info.st_uid != os.geteuid() or info.st_mode & 0o077):
            raise error_type('Private file must be owned by the current user with mode 0600')
        if info.st_size > max_bytes:
            raise error_type('File exceeds size limit')
        stream = os.fdopen(fd, 'rb')
    except BaseException:
        os.close(fd)
        raise
    with stream:
        if len(raw := stream.read(max_bytes + 1)) > max_bytes:
            raise error_type('File exceeds size limit')
        return raw


def parse_env(text):
    values = {}
    for line in text.splitlines():
        line = line.strip()
        if not line or line.startswith('#'):
            continue
        match = ENV_LINE.fullmatch(line)
        if not match:
            raise ValueError('Configuration must contain only KEY=value assignments')
        key, raw = match.groups()
        if key in values:
            raise ValueError(f'Duplicate configuration key: {key}')
        try:
            tokens = shlex.split(raw, comments=True)
        except ValueError as error:
            raise ValueError(f'Invalid quoting for configuration key: {key}') from error
        if len(tokens) > 1:
            raise ValueError(f'Quote spaces in configuration key: {key}')
        value = tokens[0] if tokens else ''
        if any(ord(character) < 32 or ord(character) == 127 for character in value):
            raise ValueError(f'Control character in configuration key: {key}')
        values[key] = value
    return values


def expand_path(value):
    text = str(value)
    if HOME_VAR.search(text):
        home = str(Path.home())
        text = HOME_VAR.sub(lambda _match: home, text)
    path = Path(text).expanduser()
    if not path.is_absolute():
        raise ValueError('Configured filesystem paths must be absolute or home-relative')
    return path


def load_config(path, required=(), private=True):
    raw = read_regular_bytes(expand_path(path), 65536, private=private)
    values = parse_env(raw.decode('utf-8'))
    missing = [key for key in required if not values.get(key)]
    if missing:
        raise ValueError('Missing configuration keys: ' + ', '.join(missing))
    return values


def enabled(values, key, default=False):
    value = values.get(key, 'true' if default else 'false')
    if not isinstance(value, str) or (lowered := value.lower()) not in {'true', 'false'}:
        raise ValueError(f'{key} must be true or false')
    return lowered == 'true'
