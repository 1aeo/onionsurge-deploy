"""Immutable public data releases and fail-closed single-writer promotion."""
from dataclasses import dataclass
import os
from pathlib import Path
import re
import selectors
import shutil
import stat
import subprocess
import time

from configuration import COMMIT, CF_ID, PREFIX, REGION, SHA256, enabled, expand_path, matched, read_regular_bytes
from operations import (FORBIDDEN_HOURLY_KEYS, FRONTEND as LIVE_FRONTEND, MAX_OBJECT, MAX_ROOT, PROMOTION_WARNINGS,
                        SUB_24H_SUFFIX, atomic_private, atomic_write, child_environment, ensure_private_directory,
                        ensure_web_directory as _ensure_web_directory, json_bytes, link_or_atomic_write,
                        private_lock_file, public_values as _public_values, sha256, stamped_id,
                        strict_json_bytes, sync_directory)

MAX_CANDIDATE = 32 * 1024**2
MAX_MANIFEST = 128 * 1024
IMMUTABLE_CACHE_CONTROL = 'public, max-age=31536000, immutable'
ID = re.compile(r'[A-Za-z0-9][A-Za-z0-9_.-]{0,95}\Z')
BUCKET = re.compile(r'[a-z0-9][a-z0-9.-]{1,61}[a-z0-9]\Z')
RELEASE_DIR = re.compile(r'\d{8}T\d{6}Z-[a-f0-9]{12}\Z')
SLUG = re.compile(r'[a-z][a-z0-9_-]{0,95}\Z')
EXPORT_SUFFIX_RE = re.compile(rf'(?!.*(?:^|-){SUB_24H_SUFFIX}\Z)[a-z0-9]+(?:-[a-z0-9]+){{0,7}}\Z')
REFERENCE = re.compile(r'reference/reference-\d{4}-\d{2}-\d{2}-[a-f0-9]{12}\.json\Z')
CSV = re.compile(rf'exports/(?!.*-{SUB_24H_SUFFIX}\.csv\Z)[a-z][a-z0-9_-]{{0,95}}-[a-z0-9]+(?:-[a-z0-9]+){{0,7}}\.csv\Z')
FRONTEND = set(LIVE_FRONTEND) | {'data.js', 'catalogue.json'}
DATA_FIELDS = set('charts custom defense dircache_cohort end expected_samples families flags fleet generated_at history_methods input_provenance input_schema_version minimum_samples product provenance reference_cutoff reference_document reference_id reference_sha256 reference_window schema_version snapshot_flags sources start time_ranges versions window_seconds'.split())


class PublicationError(ValueError):
    """Messages intentionally contain no provider diagnostics or credentials."""


def require(condition, message):
    if not condition:
        raise PublicationError(message)


def parse_json(raw):
    try:
        return strict_json_bytes(raw)
    except ValueError as error:
        raise PublicationError('invalid_json') from error


def public_values(value, allow_sub_24h=False):
    _public_values(value, allow_sub_24h=allow_sub_24h, error_type=PublicationError,
                   private_message='private_metadata', nonfinite_message='nonfinite_json',
                   forbidden_message='forbidden_sub_24h_field')


def timestamp(value):
    return type(value) is int and 0 <= value <= 8640000000000


def object_name(name):
    return isinstance(name, str) and (name == 'data.json' or bool(REFERENCE.fullmatch(name)) or bool(CSV.fullmatch(name)))


def read_regular(path, maximum):
    try:
        return read_regular_bytes(path, maximum)
    except ValueError as error:
        raise PublicationError('local_file_limit' if 'size limit' in str(error) else 'invalid_local_file') from error


def _expected_export_paths(chart_ids, suffixes):
    paths = set()
    for chart_id in chart_ids:
        for suffix in suffixes:
            path = f'exports/{chart_id}-{suffix}.csv'
            require(path not in paths, 'duplicate_export_path')
            paths.add(path)
    return paths


def validate_data_objects(objects, allow_previous_v6=False):
    require('data.json' in objects, 'missing_data')
    data = parse_json(objects['data.json'])
    require(isinstance(data, dict) and set(data) <= DATA_FIELDS, 'invalid_data_fields')
    schema_version = data.get('schema_version')
    allowed_versions = (6, 7) if allow_previous_v6 else (7,)
    require(type(schema_version) is int and schema_version in allowed_versions, 'unsupported_data_schema')
    is_v6 = schema_version == 6
    if is_v6:
        require('window_seconds' not in data or (type(data['window_seconds']) is int and data['window_seconds'] == 86400),
                'invalid_observation_window')
        alignment = 86400
        allow_sub_24h = False
    else:
        require(type(data.get('window_seconds')) is int and data['window_seconds'] in (3600, 86400),
                'invalid_observation_window')
        alignment = 86400 if data['window_seconds'] == 86400 else 3600
        allow_sub_24h = data['window_seconds'] == 3600
    public_values(data, allow_sub_24h=allow_sub_24h)
    require(timestamp(data.get('start')) and timestamp(data.get('end')) and data['end'] - data['start'] == 86400
            and data['end'] % alignment == 0, 'invalid_observation_window')
    time_ranges = data.get('time_ranges')
    require(isinstance(time_ranges, dict) and isinstance(time_ranges.get('periods'), list)
            and 1 <= len(time_ranges['periods']) <= 16, 'invalid_time_ranges')
    suffixes = []
    for period in time_ranges['periods']:
        require(isinstance(period, dict), 'invalid_time_ranges')
        suffix = period.get('export_suffix')
        require(isinstance(suffix, str) and len(suffix) <= 32 and matched(EXPORT_SUFFIX_RE, suffix),
                'invalid_time_ranges')
        if not allow_sub_24h:
            window_spec = period.get('window')
            require(isinstance(window_spec, dict) and window_spec.get('unit') != 'hours' and suffix != '24h',
                    'invalid_time_ranges')
        suffixes.append(suffix)
    require(len(set(suffixes)) == len(suffixes), 'invalid_time_ranges')
    charts = data.get('charts')
    require(isinstance(charts, list) and 1 <= len(charts) <= 64 and all(isinstance(c, dict) for c in charts), 'invalid_chart_set')
    ids = [chart.get('id') for chart in charts]
    require(all(matched(SLUG, key) for key in ids) and len(set(ids)) == len(ids), 'invalid_chart_ids')
    reference_name = data.get('reference_document')
    require(matched(REFERENCE, reference_name), 'invalid_reference_path')
    export_paths = _expected_export_paths(ids, suffixes)
    if not allow_sub_24h:
        require(not any(name.endswith('-24h.csv') for name in export_paths), 'invalid_time_ranges')
    expected = {'data.json', reference_name} | export_paths
    require(set(objects) == expected, 'incomplete_release_objects')
    digest = sha256(objects[reference_name])
    require(data.get('reference_sha256') == digest, 'reference_hash_mismatch')
    window = data.get('reference_window')
    require(isinstance(window, dict) and isinstance(window.get('date'), str), 'invalid_reference_window')
    require(timestamp(window.get('start')) and timestamp(window.get('end'))
            and window['start'] < window['end'] <= data['start'], 'invalid_reference_window')
    reference_id = 'reference-' + window['date'] + '-' + digest[:12]
    require(data.get('reference_id') == reference_id and reference_name == 'reference/' + reference_id + '.json',
            'reference_identity_mismatch')
    reference = parse_json(objects[reference_name])
    public_values(reference, allow_sub_24h=allow_sub_24h)
    require(isinstance(reference, dict) and reference.get('window') == window, 'reference_window_mismatch')
    require(timestamp(reference.get('source_snapshot_end')) and reference['source_snapshot_end'] <= data['end'],
            'reference_source_time_mismatch')
    require(matched(SHA256, reference.get('source_input_sha256')), 'invalid_reference_source_hash')
    provenance = data.get('provenance', {})
    require(isinstance(provenance, dict), 'invalid_data_provenance')
    method = provenance.get('reference_method_sha256')
    require(method is None or (matched(SHA256, method) and reference.get('method_sha256') == method),
            'reference_method_mismatch')
    require(data.get('reference_cutoff') == window.get('end'), 'reference_cutoff_mismatch')
    return data


def descriptor(raw, release_id):
    return {'id': release_id, 'manifest_sha256': sha256(raw), 'manifest_bytes': len(raw)}


def validate_descriptor(value):
    require(isinstance(value, dict) and set(value) == {'id', 'manifest_sha256', 'manifest_bytes'}, 'invalid_release_descriptor')
    require(matched(ID, value['id']), 'invalid_release_id')
    require(matched(SHA256, value['manifest_sha256']), 'invalid_manifest_hash')
    require(type(value['manifest_bytes']) is int and 0 < value['manifest_bytes'] <= MAX_MANIFEST, 'invalid_manifest_size')


def _validate_root_dict(root):
    require(isinstance(root, dict) and set(root) == {'format', 'updated_at', 'current', 'previous'}
            and root['format'] == 'onionsurge-root-v1' and timestamp(root['updated_at']), 'invalid_root')
    validate_descriptor(root['current'])
    if root['previous'] is not None:
        validate_descriptor(root['previous'])
        require(root['current']['id'] != root['previous']['id'], 'duplicate_root_release')
    return root


def validate_root(raw):
    require(len(raw) <= MAX_ROOT, 'root_limit')
    return _validate_root_dict(parse_json(raw))


def validate_manifest(raw, expected, allow_previous_v6=False):
    validate_descriptor(expected)
    require(len(raw) == expected['manifest_bytes'] and sha256(raw) == expected['manifest_sha256'], 'manifest_integrity_failure')
    manifest = parse_json(raw)
    fields = {'format', 'id', 'data_schema_version', 'observation_end', 'generated_at', 'application_commit',
              'deployment_commit', 'query_contract_sha256', 'objects'}
    allowed_versions = (6, 7) if allow_previous_v6 else (7,)
    require(isinstance(manifest, dict) and set(manifest) == fields and manifest['format'] == 'onionsurge-release-v1'
            and manifest['id'] == expected['id'] and type(manifest['data_schema_version']) is int
            and manifest['data_schema_version'] in allowed_versions, 'invalid_release_manifest')
    is_v6 = manifest['data_schema_version'] == 6
    alignment = 86400 if is_v6 else 3600
    require(timestamp(manifest['observation_end']) and manifest['observation_end'] % alignment == 0
            and timestamp(manifest['generated_at'])
            and manifest['observation_end'] <= manifest['generated_at'], 'invalid_release_time')
    require(all(matched(COMMIT, manifest[key]) for key in ('application_commit', 'deployment_commit')), 'invalid_commit')
    require(matched(SHA256, manifest['query_contract_sha256']), 'invalid_query_contract_hash')
    objects = manifest['objects']
    require(isinstance(objects, dict) and 1 <= len(objects) <= 512, 'invalid_object_map')
    for name, item in objects.items():
        require(object_name(name) and not (is_v6 and name.endswith('-24h.csv'))
                and isinstance(item, dict) and set(item) == {'sha256', 'bytes'}, 'invalid_object_descriptor')
        require(matched(SHA256, item['sha256']) and type(item['bytes']) is int and 0 < item['bytes'] <= MAX_OBJECT, 'invalid_object_integrity')
    require(sum(item['bytes'] for item in objects.values()) + len(raw) <= MAX_CANDIDATE, 'candidate_limit')
    return manifest


def publication_lock(state):
    return private_lock_file(
        state, 'publication.lock', busy_error=PublicationError, error_type=PublicationError,
        messages=('unsafe_state_directory', 'unsafe_publication_lock', 'publication_busy'),
    )


@dataclass
class Candidate:
    directory: Path
    manifest: dict
    descriptor: dict

    def summary(self):
        return {'release_id': self.manifest['id'], 'observation_end': self.manifest['observation_end'],
                'object_count': len(self.manifest['objects']),
                'object_bytes': sum(item['bytes'] for item in self.manifest['objects'].values())}


def _candidate_donor_dir(state):
    receipt_path = state / 'published.json'
    if not os.path.lexists(receipt_path):
        return None
    try:
        receipt = read_receipt(receipt_path)
        current_desc = receipt['root']['current']
        donor_dir = state / 'candidates' / current_desc['id']
        if not os.path.lexists(donor_dir) or donor_dir.is_symlink():
            return None
        info = donor_dir.lstat()
        if not stat.S_ISDIR(info.st_mode) or info.st_uid != os.geteuid() or info.st_mode & 0o077:
            return None
        manifest_path = donor_dir / 'manifest.json'
        if not os.path.lexists(manifest_path):
            return None
        validate_manifest(read_regular(manifest_path, MAX_MANIFEST), current_desc, allow_previous_v6=True)
        return donor_dir
    except (OSError, PublicationError):
        return None


def _candidate_donor_file(donor_dir, name):
    if donor_dir is None:
        return None
    candidate = donor_dir / name
    try:
        rel_parts = Path(name).parts
        cur = donor_dir
        for part in rel_parts[:-1]:
            cur = cur / part
            if cur.is_symlink():
                return None
            info = cur.lstat()
            if not stat.S_ISDIR(info.st_mode) or info.st_uid != os.geteuid() or info.st_mode & 0o077:
                return None
        info = candidate.lstat()
        if stat.S_ISREG(info.st_mode) and info.st_uid == os.geteuid():
            return candidate
    except OSError:
        return None
    return None


def prepare_candidate(build_dir, state, application_commit, deployment_commit, now=None):
    require(all(matched(COMMIT, value) for value in (application_commit, deployment_commit)), 'invalid_commit')
    source = Path(build_dir).resolve(strict=True)
    require(source.is_dir(), 'invalid_build_directory')
    build = parse_json(read_regular(source / 'build-manifest.json', MAX_MANIFEST))
    require(isinstance(build, dict) and build.get('format') == 'onionsurge-build-v1' and build.get('mode') == 'live',
            'live_build_required')
    build_schema = build.get('schema_version')
    require(type(build_schema) is int and build_schema == 7, 'unsupported_data_schema')
    require('release_objects' in build, 'invalid_build_manifest')
    declared = build.get('files')
    require(isinstance(declared, dict) and {'index.html', 'app.js', 'style.css', 'live.js', 'data.json'} <= set(declared)
            and len(declared) <= 528, 'invalid_build_manifest')
    require(all(isinstance(name, str) and (name in FRONTEND or object_name(name)) and matched(SHA256, digest)
                for name, digest in declared.items()), 'unapproved_build_path')
    actual = set()
    for path in source.rglob('*'):
        relative = path.relative_to(source).as_posix()
        info = path.lstat()
        if stat.S_ISDIR(info.st_mode):
            require(relative in {'exports', 'reference'}, 'unapproved_build_directory')
        else:
            require(stat.S_ISREG(info.st_mode), 'nonregular_build_path')
            actual.add(relative)
    require(actual == set(declared) | {'build-manifest.json'}, 'build_manifest_file_mismatch')
    objects = {}
    selected_bytes = 0
    for name, expected in declared.items():
        raw = read_regular(source / name, MAX_OBJECT)
        require(sha256(raw) == expected, 'build_hash_mismatch')
        if object_name(name):
            selected_bytes += len(raw)
            require(selected_bytes <= MAX_CANDIDATE, 'candidate_limit')
            objects[name] = raw
    release_objects = build['release_objects']
    require(isinstance(release_objects, list)
            and all(isinstance(name, str) and object_name(name) for name in release_objects)
            and len(set(release_objects)) == len(release_objects), 'invalid_build_manifest')
    require(set(objects) == set(release_objects), 'build_manifest_file_mismatch')
    data = validate_data_objects(objects)
    require(build.get('snapshot_end') == data['end'], 'build_observation_mismatch')
    generated = int(time.time()) if now is None else now
    require(timestamp(generated) and generated >= data['end'], 'invalid_generation_time')
    release_id = stamped_id(when=generated)
    contract_sha = build.get('query_contract_sha256')
    require(matched(SHA256, contract_sha) and isinstance(data.get('provenance'), dict)
            and data['provenance'].get('query_contract_sha256') == contract_sha,
            'invalid_query_contract_hash')
    require(len(objects) <= 512, 'invalid_object_map')
    manifest = {'format': 'onionsurge-release-v1', 'id': release_id, 'data_schema_version': data['schema_version'],
                'observation_end': data['end'], 'generated_at': generated, 'application_commit': application_commit,
                'deployment_commit': deployment_commit, 'query_contract_sha256': contract_sha,
                'objects': {name: {'sha256': declared[name], 'bytes': len(objects[name])} for name in sorted(objects)}}
    encoded = json_bytes(manifest)
    require(len(encoded) <= MAX_MANIFEST, 'invalid_manifest_size')
    require(selected_bytes + len(encoded) <= MAX_CANDIDATE, 'candidate_limit')
    desc = descriptor(encoded, release_id)
    validate_manifest(encoded, desc)
    ensure_private_directory(state, PublicationError, 'unsafe_state_directory')
    candidates = state / 'candidates'
    ensure_private_directory(candidates, PublicationError, 'unsafe_state_directory')
    donor_dir = _candidate_donor_dir(state)
    directory = candidates / release_id
    directory.mkdir(mode=0o700)
    subdirs = set()
    for name, raw in objects.items():
        target = directory / name
        if target.parent != directory:
            target.parent.mkdir(exist_ok=True, mode=0o700)
            subdirs.add(target.parent)
        link_or_atomic_write(target, raw, donor_path=_candidate_donor_file(donor_dir, name),
                             mode=0o600, sync_parent=False)
    sync_directory(directory)
    for subdir in sorted(subdirs):
        sync_directory(subdir)
    # This completion marker is last; an interrupted candidate is never selected.
    atomic_private(directory / 'manifest.json', encoded)
    sync_directory(candidates)
    return Candidate(directory, manifest, desc)


def _validate_storage_key(key):
    if key == 'manifest.json':
        return
    parts = key.split('/', 2) if isinstance(key, str) else []
    require(len(parts) == 3 and parts[0] == 'releases' and matched(ID, parts[1])
            and (parts[2] == 'manifest.json' or object_name(parts[2])),
            'unsafe_storage_key')


class RcloneProvider:
    def __init__(self, name, binary, bucket, prefix, endpoint, region, key, secret):
        require(name in {'do', 'r2'}, 'invalid_provider')
        require(matched(BUCKET, bucket), 'invalid_bucket')
        require(matched(PREFIX, prefix), 'nonempty_project_prefix_required')
        require(isinstance(key, str) and isinstance(secret, str) and key and secret
                and not any(ord(c) < 32 for c in key + secret), 'missing_storage_credentials')
        require(isinstance(binary, (str, Path)) and str(binary), 'invalid_rclone_path')
        try:
            resolved_binary = str(expand_path(binary))
        except ValueError as error:
            raise PublicationError('invalid_rclone_path') from error
        self.name, self.binary, self.bucket, self.prefix = name, resolved_binary, bucket, prefix
        self.identity = sha256(json_bytes({'name': name, 'bucket': bucket, 'prefix': prefix, 'endpoint': endpoint}))
        self.env = child_environment()
        self.env.update({'RCLONE_CONFIG_ONIONSURGE_TYPE': 's3',
                         'RCLONE_CONFIG_ONIONSURGE_PROVIDER': 'DigitalOcean' if name == 'do' else 'Cloudflare',
                         'RCLONE_CONFIG_ONIONSURGE_ENDPOINT': endpoint, 'RCLONE_CONFIG_ONIONSURGE_REGION': region,
                         'RCLONE_CONFIG_ONIONSURGE_ACCESS_KEY_ID': key, 'RCLONE_CONFIG_ONIONSURGE_SECRET_ACCESS_KEY': secret,
                         'RCLONE_CONFIG_ONIONSURGE_ACL': 'public-read' if name == 'do' else 'private'})

    def remote(self, key):
        _validate_storage_key(key)
        return f'onionsurge:{self.bucket}/{self.prefix}/{key}'

    def _command(self, arguments):
        return [self.binary, *arguments, '--config', '/dev/null', '--s3-no-check-bucket',
                '--retries', '1', '--low-level-retries', '1', '--contimeout', '10s', '--timeout', '45s', '--log-level', 'ERROR']

    def _run(self, arguments, maximum):
        command = self._command(arguments)
        try:
            with subprocess.Popen(command, env=self.env, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL) as process:
                output = bytearray()
                end = time.monotonic() + 90
                stdout_fd = process.stdout.fileno()
                try:
                    with selectors.DefaultSelector() as ready:
                        ready.register(process.stdout, selectors.EVENT_READ)
                        while True:
                            remaining = end - time.monotonic()
                            require(remaining > 0 and ready.select(remaining), 'storage_timeout')
                            chunk = os.read(stdout_fd, 65536)
                            if not chunk:
                                break
                            output.extend(chunk)
                            require(len(output) <= maximum, 'storage_response_limit')
                    code = process.wait(timeout=max(0.001, end - time.monotonic()))
                except BaseException:
                    process.kill()
                    process.wait()
                    raise
                return code, bytes(output)
        except (OSError, subprocess.TimeoutExpired) as error:
            raise PublicationError('storage_command_failed') from error

    def get(self, key, maximum, missing_ok=False):
        remote_path = self.remote(key)
        if missing_ok:
            # S3 cat can exit successfully with no bytes for a missing key.
            # An exact-key stat distinguishes its virtual directory result from
            # an actual zero-byte object. Every real file still gets a full GET.
            code, metadata = self._run(['lsjson', '--stat', remote_path], MAX_ROOT)
            if code in (3, 4) and not metadata:
                return None
            require(code == 0, 'storage_stat_failed')
            info = parse_json(metadata)
            require(isinstance(info, dict) and type(info.get('IsDir')) is bool, 'invalid_storage_stat')
            if info['IsDir']:
                return None
            require(type(info.get('Size')) is int and 0 <= info['Size'] <= maximum, 'storage_response_limit')
        code, raw = self._run(['cat', remote_path], maximum)
        require(code == 0, 'storage_read_failed')
        return raw

    def put(self, key, raw, immutable):
        require(type(raw) is bytes and 0 < len(raw) <= (MAX_ROOT if key == 'manifest.json' else MAX_OBJECT),
                'invalid_upload_bytes')
        mime = 'text/csv; charset=utf-8' if key.endswith('.csv') else 'application/json; charset=utf-8'
        cache = IMMUTABLE_CACHE_CONTROL if immutable else 'no-store'
        # In rclone 1.72, rcat --size bypasses upload options. The no-size path
        # applies these explicit HTTP headers; a cutoff above the bounded input
        # keeps the whole upload in memory without using filesystem metadata.
        args = ['rcat', self.remote(key), '--streaming-upload-cutoff', str(MAX_OBJECT + 1),
                '--header-upload', 'Cache-Control: ' + cache, '--header-upload', 'Content-Type: ' + mime]
        # rcat receives the validated immutable bytes directly; it never reopens
        # a mutable candidate pathname. Existing-key conflicts are checked by
        # the publisher under the single cooperating writer contract.
        try:
            result = subprocess.run(self._command(args), input=raw, env=self.env, stdout=subprocess.DEVNULL,
                                    stderr=subprocess.DEVNULL, timeout=90)
        except (OSError, subprocess.TimeoutExpired) as error:
            raise PublicationError('storage_write_failed') from error
        require(result.returncode == 0, 'storage_write_failed')

    def verify_cache_control(self, key, immutable):
        code, raw = self._run(['lsjson', '--stat', '--metadata', self.remote(key)], MAX_ROOT)
        require(code == 0, 'storage_metadata_read_failed')
        info = parse_json(raw)
        require(isinstance(info, dict) and info.get('IsDir') is False
                and isinstance(info.get('Metadata'), dict), 'invalid_storage_metadata')
        expected = IMMUTABLE_CACHE_CONTROL if immutable else 'no-store'
        require(info['Metadata'].get('cache-control') == expected, 'cache_control_mismatch')


def ensure_web_directory(base_dir, target_dir, *, create=False):
    """Ensure base_dir..target_dir exist without symlinks or world-writable bits."""
    return _ensure_web_directory(base_dir, target_dir, create=create, error_type=PublicationError)


def parse_local_retain_releases(config):
    """Parse and validate optional LOCAL_RETAIN_RELEASES count (`1..365`, default 3)."""
    retain_raw = config.get('LOCAL_RETAIN_RELEASES', '3')
    require(isinstance(retain_raw, str) and retain_raw.isascii() and retain_raw.isdigit()
            and 1 <= int(retain_raw) <= 365, 'invalid_local_retain_releases')
    return int(retain_raw)


def _is_completed_release_dir(entry):
    if not RELEASE_DIR.fullmatch(entry.name) or entry.is_symlink():
        return False
    info = entry.lstat()
    if not stat.S_ISDIR(info.st_mode) or info.st_uid != os.geteuid():
        return False
    manifest_path = entry / 'manifest.json'
    if not os.path.lexists(manifest_path) or manifest_path.is_symlink():
        return False
    try:
        manifest = parse_json(read_regular(manifest_path, MAX_MANIFEST))
    except (OSError, PublicationError):
        return False
    return (isinstance(manifest, dict) and manifest.get('format') == 'onionsurge-release-v1'
            and manifest.get('id') == entry.name)


def _prune_release_dirs(container, keep_ids, retain_count):
    retained_keep = 0
    extras = []
    for entry in container.iterdir():
        if not _is_completed_release_dir(entry):
            continue
        info = entry.lstat()
        if entry.name in keep_ids:
            retained_keep += 1
        else:
            extras.append((entry.name[:16], info.st_mtime_ns, entry.name, entry))
    extras.sort(reverse=True)
    extra_keep = max(0, retain_count - retained_keep)
    removed = []
    for _, _, name, entry in extras[extra_keep:]:
        shutil.rmtree(entry)
        removed.append(name)
    if removed:
        sync_directory(container)
    return removed


def prune_candidates(state, retain_runs):
    """Prune completed candidate directories beyond retain_runs while keeping active root releases."""
    require(type(retain_runs) is int and 2 <= retain_runs <= 168, 'invalid_state_retain_runs')
    if os.path.lexists(state / 'pending-promotion.json') or not os.path.lexists(state / 'published.json'):
        return []
    receipt = read_receipt(state / 'published.json')
    root = receipt['root']
    keep_ids = {root['current']['id']}
    if root['previous'] is not None:
        keep_ids.add(root['previous']['id'])
    candidates_dir = state / 'candidates'
    if not os.path.lexists(candidates_dir):
        return []
    info = candidates_dir.lstat()
    require(stat.S_ISDIR(info.st_mode) and info.st_uid == os.geteuid() and not (info.st_mode & 0o077),
            'unsafe_state_directory')
    return _prune_release_dirs(candidates_dir, keep_ids, retain_runs)


class LocalDirectoryProvider:
    """Publish verified immutable data releases into a private local site directory."""

    def __init__(self, site_dir, *, retain_releases=3):
        raw_path = expand_path(site_dir)
        require(not os.path.islink(raw_path), 'unsafe_local_site_directory')
        require(type(retain_releases) is int and 1 <= retain_releases <= 365, 'invalid_local_retain_releases')
        self.name = 'local'
        self.site_dir = raw_path.resolve()
        self.data_dir = self.site_dir / 'data'
        self.retain_releases = retain_releases
        self._synced_dirs = set()
        self.identity = sha256(json_bytes({'name': 'local', 'site_dir': str(self.site_dir), 'data_dir': str(self.data_dir)}))

    def _sync_ancestors(self, directory):
        cur = directory
        to_sync = []
        while True:
            if cur not in self._synced_dirs:
                to_sync.append(cur)
            if cur == self.site_dir or cur == cur.parent:
                break
            cur = cur.parent
        for item in to_sync:
            sync_directory(item)
            self._synced_dirs.add(item)

    def _target(self, key, *, create_dirs=False):
        _validate_storage_key(key)
        target = self.data_dir / key
        if not ensure_web_directory(self.site_dir, target.parent, create=create_dirs):
            return None
        return target

    def _donor_file(self, release_id, subpath, size):
        manifest_path = self.data_dir / 'manifest.json'
        if not os.path.lexists(manifest_path):
            return None
        try:
            root = validate_root(read_regular(manifest_path, MAX_ROOT))
        except (OSError, PublicationError):
            return None
        ids = [root['current']['id']]
        if root['previous'] is not None:
            ids.append(root['previous']['id'])
        for donor_id in ids:
            if donor_id == release_id:
                continue
            donor = self.data_dir / 'releases' / donor_id / subpath
            try:
                if not ensure_web_directory(self.site_dir, donor.parent, create=False):
                    continue
                info = donor.lstat()
            except (OSError, PublicationError):
                continue
            if stat.S_ISREG(info.st_mode) and info.st_uid == os.geteuid() and info.st_size == size:
                return donor
        return None

    def get(self, key, maximum, missing_ok=False):
        target = self._target(key, create_dirs=False)
        if target is None or not os.path.lexists(target):
            if missing_ok:
                return None
            raise PublicationError('storage_read_failed')
        try:
            return read_regular(target, maximum)
        except OSError as error:
            raise PublicationError('storage_read_failed') from error

    def put(self, key, raw, immutable):
        require(type(raw) is bytes and 0 < len(raw) <= (MAX_ROOT if key == 'manifest.json' else MAX_OBJECT),
                'invalid_upload_bytes')
        target = self._target(key, create_dirs=True)
        if immutable and os.path.lexists(target):
            try:
                existing = read_regular(target, len(raw) + 1)
            except (OSError, PublicationError) as error:
                raise PublicationError('immutable_object_conflict') from error
            require(len(existing) == len(raw) and sha256(existing) == sha256(raw), 'immutable_object_conflict')
            return
        donor_path = None
        if immutable and key.startswith('releases/'):
            parts = key.split('/', 2)
            if len(parts) == 3:
                donor_path = self._donor_file(parts[1], parts[2], len(raw))
        link_or_atomic_write(target, raw, donor_path=donor_path, mode=0o640)

    def verify_cache_control(self, key, immutable):
        target = self._target(key, create_dirs=False)
        require(target is not None and os.path.lexists(target), 'storage_metadata_read_failed')
        info = target.lstat()
        require(stat.S_ISREG(info.st_mode) and info.st_uid == os.geteuid()
                and not (info.st_mode & 0o002) and info.st_size > 0, 'unsafe_local_site_file')
        if immutable:
            self._sync_ancestors(target.parent)

    def _is_published_release_dir(self, entry):
        return _is_completed_release_dir(entry)

    def prune_releases(self, keep_ids):
        releases_dir = self.data_dir / 'releases'
        if not ensure_web_directory(self.site_dir, releases_dir, create=False):
            return []
        return _prune_release_dirs(releases_dir, keep_ids, self.retain_releases)


def providers_from_config(config):
    try:
        providers = []
        if enabled(config, 'DO_ENABLED'):
            region = config.get('DO_SPACES_REGION', '')
            require(matched(REGION, region), 'invalid_spaces_region')
            providers.append(RcloneProvider('do', config['RCLONE_PATH'], config.get('DO_SPACES_BUCKET', ''),
                config.get('DO_SPACES_PREFIX', ''), f'https://{region}.digitaloceanspaces.com', region,
                config.get('DO_SPACES_KEY', ''), config.get('DO_SPACES_SECRET', '')))
        if enabled(config, 'R2_ENABLED'):
            account = config.get('CLOUDFLARE_ACCOUNT_ID', '')
            require(matched(CF_ID, account), 'invalid_r2_account')
            providers.append(RcloneProvider('r2', config['RCLONE_PATH'], config.get('R2_BUCKET', ''),
                config.get('R2_PREFIX', ''), f'https://{account}.r2.cloudflarestorage.com', 'auto',
                config.get('R2_ACCESS_KEY_ID', ''), config.get('R2_SECRET_ACCESS_KEY', '')))
        if enabled(config, 'LOCAL_ENABLED'):
            site_raw = config.get('LOCAL_SITE_DIR', '')
            require(isinstance(site_raw, str) and bool(site_raw.strip()), 'missing_local_site_directory')
            site_resolved = expand_path(site_raw).resolve()
            state_raw = config.get('STATE_DIR', '')
            if isinstance(state_raw, str) and state_raw.strip():
                state_resolved = expand_path(state_raw).resolve()
                require(not (site_resolved.is_relative_to(state_resolved) or state_resolved.is_relative_to(site_resolved)),
                        'local_site_overlaps_state')
            providers.append(LocalDirectoryProvider(site_raw, retain_releases=parse_local_retain_releases(config)))
        require(providers, 'no_enabled_storage')
        return providers
    except PublicationError:
        raise
    except KeyError as error:
        raise PublicationError('missing_storage_configuration') from error
    except (ValueError, OSError) as error:
        raise PublicationError('invalid_storage_configuration') from error


def matches(raw, expected):
    return raw is not None and len(raw) == expected['bytes'] and sha256(raw) == expected['sha256']


def verify_release(provider, desc, allow_previous_v6=False):
    prefix = 'releases/' + desc['id'] + '/'
    manifest = validate_manifest(provider.get(prefix + 'manifest.json', MAX_MANIFEST), desc,
                                 allow_previous_v6=allow_previous_v6)
    provider.verify_cache_control(prefix + 'manifest.json', immutable=True)
    objects = {}
    for name, expected in manifest['objects'].items():
        obj_key = prefix + name
        raw = provider.get(obj_key, expected['bytes'])
        require(matches(raw, expected), 'object_integrity_failure')
        provider.verify_cache_control(obj_key, immutable=True)
        objects[name] = raw
    data = validate_data_objects(objects, allow_previous_v6=allow_previous_v6)
    require(data['end'] == manifest['observation_end'], 'release_observation_mismatch')
    require(data['schema_version'] == manifest['data_schema_version'], 'release_schema_mismatch')
    return manifest


def read_receipt(path):
    receipt = parse_json(read_regular(path, MAX_ROOT * 2))
    require(isinstance(receipt, dict) and set(receipt) == {'destinations', 'root'}, 'invalid_private_receipt')
    destinations = receipt['destinations']
    require(isinstance(destinations, list) and 1 <= len(destinations) <= 3
            and all(matched(SHA256, value) for value in destinations)
            and len(set(destinations)) == len(destinations), 'invalid_private_receipt')
    _validate_root_dict(receipt['root'])
    return receipt


def publish_candidate(candidate, state, providers):
    """Publish a prepared candidate; the caller must hold publication_lock."""
    require(1 <= len(providers) <= 3, 'no_enabled_storage')
    pending = state / 'pending-promotion.json'
    require(not os.path.lexists(pending), 'promotion_blocked_pending_recovery')
    identities = sorted(provider.identity for provider in providers)
    receipt_path = state / 'published.json'
    previous = read_receipt(receipt_path) if os.path.lexists(receipt_path) else None
    require(previous is None or previous.get('destinations') == identities, 'storage_destination_changed')
    old_raw = json_bytes(previous['root']) if previous else None
    for provider in providers:
        require(provider.get('manifest.json', MAX_ROOT, missing_ok=True) == old_raw, 'provider_root_drift')
    if previous is not None:
        prev_manifest = previous_manifest(previous, state, providers[0])
        prev_end, curr_end = prev_manifest['observation_end'], candidate.manifest['observation_end']
        require(curr_end == prev_end or curr_end >= prev_end + 3600, 'observation_interval_too_short')
    items = {**candidate.manifest['objects'], 'manifest.json': {
        'sha256': candidate.descriptor['manifest_sha256'], 'bytes': candidate.descriptor['manifest_bytes']}}
    blobs = []
    for name, expected in items.items():
        raw = read_regular(candidate.directory / name, expected['bytes'])
        require(matches(raw, expected), 'candidate_changed')
        blobs.append((name, raw, expected))
    prefix = 'releases/' + candidate.manifest['id'] + '/'
    for provider in providers:
        for name, raw, expected in blobs:
            existing = provider.get(prefix + name, expected['bytes'], missing_ok=True)
            if existing is None:
                provider.put(prefix + name, raw, immutable=True)
            else:
                require(matches(existing, expected), 'immutable_object_conflict')
        verify_release(provider, candidate.descriptor)
    # Detect an unrelated writer before issuing our own mutable write. The
    # deployment still requires one authorized writer; this is not distributed CAS.
    for provider in providers:
        require(provider.get('manifest.json', MAX_ROOT, missing_ok=True) == old_raw, 'provider_root_drift')
    root = {'format': 'onionsurge-root-v1', 'updated_at': int(time.time()), 'current': candidate.descriptor,
            'previous': previous['root']['current'] if previous else None}
    encoded = json_bytes(root)
    validate_root(encoded)
    atomic_private(candidate.directory / 'root.json', encoded)
    receipt_bytes = json_bytes({'destinations': identities, 'root': root})
    # Durable before the first root PUT. Interrupts, timeouts and even failed
    # final local bookkeeping retain this fence for explicit operator recovery.
    atomic_private(pending, receipt_bytes)
    try:
        for provider in providers:
            provider.put('manifest.json', encoded, immutable=False)
            require(provider.get('manifest.json', MAX_ROOT) == encoded, 'root_verification_failed')
            provider.verify_cache_control('manifest.json', immutable=False)
        atomic_private(receipt_path, receipt_bytes)
    except BaseException as error:
        raise PublicationError('promotion_uncertain_operator_recovery_required') from error
    cleared = clear_confirmed_pending(state, pending)
    result = {'status': 'published', **candidate.summary(), **cleared}
    if not cleared:
        keep_ids = {root['current']['id']}
        if root['previous'] is not None:
            keep_ids.add(root['previous']['id'])
        for provider in providers:
            if hasattr(provider, 'prune_releases'):
                try:
                    provider.prune_releases(keep_ids)
                except OSError:
                    pass
    return result


def previous_manifest(previous, state, provider=None):
    prev_desc = previous['root']['current']
    cand_dir = state / 'candidates' / prev_desc['id']
    for directory in (cand_dir.parent, cand_dir):
        if os.path.lexists(directory):
            info = directory.lstat()
            require(stat.S_ISDIR(info.st_mode) and info.st_uid == os.geteuid(), 'unsafe_state_directory')
    local_manifest = cand_dir / 'manifest.json'
    if os.path.lexists(local_manifest):
        raw = read_regular(local_manifest, MAX_MANIFEST)
    else:
        resolved = provider() if callable(provider) else provider
        require(resolved is not None, 'missing_previous_manifest')
        raw = resolved.get('releases/' + prev_desc['id'] + '/manifest.json', MAX_MANIFEST)
    return validate_manifest(raw, prev_desc, allow_previous_v6=True)


def clear_confirmed_pending(state, pending):
    """Roots and published.json are durable; cleanup cannot undo publication."""
    try:
        pending.unlink()
    except OSError:
        return {'warning': 'pending_marker_cleanup_failed'}
    try:
        sync_directory(state)
    except OSError:
        return {'warning': 'pending_marker_removal_not_durable'}
    return {}


def verify_root_releases(providers, raw, mismatch_code):
    root = validate_root(raw)
    for provider in providers:
        require(provider.get('manifest.json', MAX_ROOT) == raw, mismatch_code)
        provider.verify_cache_control('manifest.json', immutable=False)
        verify_release(provider, root['current'])
        if root['previous'] is not None:
            verify_release(provider, root['previous'], allow_previous_v6=True)
    for provider in providers:
        require(provider.get('manifest.json', MAX_ROOT) == raw, mismatch_code)
        provider.verify_cache_control('manifest.json', immutable=False)
    return root


def recover_promotion(state, providers, writers_fenced=False):
    """Read-only provider reconciliation while the caller holds publication_lock."""
    require(1 <= len(providers) <= 3, 'no_enabled_storage')
    require(writers_fenced, 'operator_writer_fence_required')
    pending = state / 'pending-promotion.json'
    receipt = read_receipt(pending)
    require(receipt.get('destinations') == sorted(provider.identity for provider in providers), 'storage_destination_changed')
    raw = json_bytes(receipt['root'])
    # Re-read every pointer after the complete-release checks. The human fence
    # must cover old in-flight writes; repeated GETs alone cannot establish it.
    root = verify_root_releases(providers, raw, 'recovery_roots_disagree')
    atomic_private(state / 'published.json', json_bytes(receipt))
    return {'status': 'recovered', 'release_id': root['current']['id'], **clear_confirmed_pending(state, pending)}


def _fenced_receipt_setup(state, providers, writers_fenced):
    require(writers_fenced, 'operator_writer_fence_required')
    require(1 <= len(providers) <= 3, 'no_enabled_storage')
    require(not os.path.lexists(state / 'pending-promotion.json'), 'promotion_blocked_pending_recovery')
    previous = read_receipt(state / 'published.json')
    identities = sorted(provider.identity for provider in providers)
    return previous, identities


def _save_reconciled_receipt(state, previous, updated, fmt, file_prefix):
    now = int(time.time())
    record = {'format': fmt, 'verified_at': now, 'previous_receipt': previous, 'new_receipt': updated}
    atomic_private(state / stamped_id(file_prefix, when=now, suffix='.json'), json_bytes(record))
    atomic_private(state / 'published.json', json_bytes(updated))


def migrate_destinations(state, providers, writers_fenced=False):
    """Verify copied releases, then reconcile only the private destination pins.

    The caller holds publication_lock; all old and new root writers must already
    be fenced. This function never uploads, changes roots or deletes objects.
    """
    previous, identities = _fenced_receipt_setup(state, providers, writers_fenced)
    raw = json_bytes(previous['root'])
    require(len(set(identities)) == len(identities)
            and all(matched(SHA256, value) for value in identities), 'invalid_storage_identity')
    root = verify_root_releases(providers, raw, 'migration_roots_disagree')
    if identities == previous['destinations']:
        return {'status': 'destination_unchanged', 'release_id': root['current']['id']}
    updated = {'destinations': identities, 'root': root}
    # Preserve the old receipt durably before the only state pointer changes.
    _save_reconciled_receipt(state, previous, updated, 'onionsurge-destination-migration-v1', 'destination-migration-')
    return {'status': 'destination_migrated', 'release_id': root['current']['id']}


def reconcile_root(state, providers, expected_raw, writers_fenced=False):
    """Adopt an explicitly selected, already-restored root; never write remotely."""
    previous, identities = _fenced_receipt_setup(state, providers, writers_fenced)
    require(identities == previous['destinations'], 'storage_destination_changed')
    root = validate_root(expected_raw)
    require(json_bytes(root) == expected_raw, 'canonical_root_required')
    verify_root_releases(providers, expected_raw, 'rollback_roots_disagree')
    updated = {'destinations': identities, 'root': root}
    if updated == previous:
        return {'status': 'root_unchanged', 'release_id': root['current']['id']}
    _save_reconciled_receipt(state, previous, updated, 'onionsurge-root-reconciliation-v1', 'root-reconciliation-')
    return {'status': 'root_reconciled', 'release_id': root['current']['id']}
