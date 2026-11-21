#!/usr/bin/env python3
"""Stage an explicit public application build with pinned release provenance."""
import argparse
import json
import os
from pathlib import Path
import re
import stat
import sys
import tempfile

SCRIPTS_DIR = Path(__file__).resolve().parent
if str(SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPTS_DIR))

from configuration import COMMIT, SHA256, matched, read_regular_bytes as _read_regular_bytes
from operations import FRONTEND, SUB_24H_SUFFIX, public_values, rename_noreplace, sha256, strict_json_bytes

ROOT = SCRIPTS_DIR.parent
CORE = {'index.html', 'app.js', 'style.css', 'data.js', 'data.json'}
LIVE_REQUIRED = FRONTEND | {'data.json'}
ALLOWED_EXACT = CORE | {'catalogue.json', 'live.js'}

MAX_MANIFEST_BYTES = 1024 * 1024
MAX_ASSET_BYTES = 25 * 1024 * 1024
EXPORT_SUFFIX_RE = re.compile(rf'(?!.*(?:^|-){SUB_24H_SUFFIX}\Z)[a-z0-9]+(?:-[a-z0-9]+){{0,7}}\Z')
CHART_ID_RE = re.compile(r'[a-z][a-z0-9_-]{0,95}\Z')
RELEASE_OBJECT_RE = re.compile(
    rf'(?:exports/(?!.*-{SUB_24H_SUFFIX}\.csv\Z)[a-z][a-z0-9_-]{{0,95}}-[a-z0-9]+(?:-[a-z0-9]+){{0,7}}\.csv|'
    r'reference/reference-\d{4}-\d{2}-\d{2}-[a-f0-9]{12}\.json)\Z')
ALLOWED_PUBLIC_PATH_RE = re.compile(rf'(?:design-[123]\.html|{RELEASE_OBJECT_RE.pattern})')


def _validate_staged_data(raw):
    payload = strict_json_bytes(raw)
    if not isinstance(payload, dict):
        raise ValueError('Expected JSON object')
    schema_version = payload.get('schema_version')
    if type(schema_version) is not int or schema_version != 7:
        raise ValueError('Unsupported data schema_version')
    window_seconds = payload.get('window_seconds')
    if type(window_seconds) is not int or window_seconds not in (3600, 86400):
        raise ValueError('Invalid window_seconds (must be 3600 or 86400)')
    end = payload.get('end')
    alignment = 86400 if window_seconds == 86400 else 3600
    if type(end) is not int or end <= 0 or end % alignment != 0:
        label = 'UTC day (86400s)' if alignment == 86400 else 'UTC hour (3600s)'
        raise ValueError(f'Observation end must align to {label}')
    start = payload.get('start')
    if type(start) is not int or end - start != 86400:
        raise ValueError('Observation window must equal 86400s')
    time_ranges = payload.get('time_ranges')
    if not isinstance(time_ranges, dict) or not isinstance(time_ranges.get('periods'), list) or not 1 <= len(time_ranges['periods']) <= 16:
        raise ValueError('Invalid time_ranges metadata')
    if window_seconds == 86400:
        for period in time_ranges['periods']:
            if (not isinstance(period, dict) or not isinstance(period.get('window'), dict)
                    or period['window'].get('unit') == 'hours' or period.get('export_suffix') == '24h'):
                raise ValueError('Invalid daily time_ranges period')
    public_values(payload, allow_sub_24h=(window_seconds == 3600))
    return payload


def _verify_release_objects_manifest(declared, release_objects):
    """Verify declared release objects against build-manifest.json release_objects."""
    if (not isinstance(release_objects, list) or not all(isinstance(name, str) for name in release_objects)
            or len(set(release_objects)) != len(release_objects)):
        raise ValueError('Invalid release_objects in build-manifest.json')
    expected = set(release_objects)
    if ('data.json' not in expected
            or any(name != 'data.json' and not RELEASE_OBJECT_RE.fullmatch(name) for name in expected)):
        raise ValueError('Unapproved path in build-manifest.json release_objects')
    expected_exports = {name for name in expected if name.startswith('exports/')}
    declared_exports = {name for name in declared if name.startswith('exports/')}
    if declared_exports != expected_exports:
        raise ValueError('Declared exports/*.csv files do not match build-manifest.json release_objects')
    declared_release = {name for name in declared if name == 'data.json' or name.startswith(('exports/', 'reference/'))}
    if declared_release != expected:
        raise ValueError('Declared release files do not match build-manifest.json release_objects')


def _verify_non_live_exports(declared, payload):
    """Verify staged non-live exports when build-manifest.json omits release_objects."""
    charts = payload.get('charts')
    if not isinstance(charts, list) or not all(
            isinstance(c, dict) and isinstance(c.get('id'), str) and CHART_ID_RE.fullmatch(c['id']) for c in charts):
        raise ValueError('Invalid chart descriptor in data.json')
    chart_ids = [c['id'] for c in charts]
    if len(set(chart_ids)) != len(chart_ids):
        raise ValueError('Duplicate chart id in data.json')
    periods = payload['time_ranges']['periods']
    suffixes = []
    for period in periods:
        if not isinstance(period, dict):
            raise ValueError('Invalid time_ranges period in data.json')
        suffix = period.get('export_suffix')
        if not isinstance(suffix, str) or not EXPORT_SUFFIX_RE.fullmatch(suffix):
            raise ValueError('Invalid time_ranges export_suffix')
        suffixes.append(suffix)
    if len(set(suffixes)) != len(suffixes):
        raise ValueError('Duplicate time_ranges export_suffix')
    expected = set()
    for chart_id in chart_ids:
        for suffix in suffixes:
            path = f'exports/{chart_id}-{suffix}.csv'
            if path in expected:
                raise ValueError(f'Duplicate export CSV path: {path}')
            expected.add(path)
    if {name for name in declared if name.startswith('exports/')} != expected:
        raise ValueError('Declared exports/*.csv files do not match data.json charts and time_ranges')


def read_regular_bytes(path, max_bytes=None):
    return _read_regular_bytes(path, MAX_ASSET_BYTES if max_bytes is None else max_bytes)


def stage_asset_file(source, destination):
    raw = read_regular_bytes(source)
    destination.write_bytes(raw)
    return len(raw), sha256(raw)


def allowed_public_path(name):
    return name in ALLOWED_EXACT or bool(ALLOWED_PUBLIC_PATH_RE.fullmatch(name))


def prepare(source, destination, commit, live=False):
    source, destination = Path(source).resolve(strict=True), Path(destination)
    if destination.name in {'', '..'}:
        raise ValueError('Release root must name a new directory')
    destination = destination.parent.resolve() / destination.name
    if not matched(COMMIT, commit):
        raise ValueError('Supply the exact 40-character application commit SHA')
    if not source.is_dir() or not (source / 'index.html').is_file():
        raise ValueError('Expected a public build directory with index.html')
    if os.path.lexists(destination):
        raise ValueError('Release root already exists; verify it or select a new release root')
    if source.is_relative_to(destination) or destination.is_relative_to(source):
        raise ValueError('Input and output trees must not overlap')
    manifest_bytes = read_regular_bytes(source / 'build-manifest.json', MAX_MANIFEST_BYTES)
    manifest = strict_json_bytes(manifest_bytes)
    declared = manifest.get('files') if isinstance(manifest, dict) else None
    manifest_schema = manifest.get('schema_version') if isinstance(manifest, dict) else None
    required_files = LIVE_REQUIRED if live else CORE
    if (not isinstance(manifest, dict) or manifest.get('format') != 'onionsurge-build-v1'
            or type(manifest_schema) is not int or manifest_schema != 7
            or 'release_objects' not in manifest
            or not isinstance(declared, dict)):
        raise ValueError('Expected an onionsurge-build-v1 manifest with all required assets')
    if live and manifest.get('mode') != 'live':
        raise ValueError('Live frontend staging requires an explicit live application build')
    if not live and manifest.get('mode') == 'live':
        raise ValueError('Use --live when staging a live application build')
    if not required_files <= set(declared):
        raise ValueError('Expected an onionsurge-build-v1 manifest with all required assets')
    if any(not allowed_public_path(name) or not matched(SHA256, digest) for name, digest in declared.items()):
        raise ValueError('Manifest contains an unapproved filename or invalid digest')
    _verify_release_objects_manifest(declared, manifest['release_objects'])
    paths = []
    for path in source.rglob('*'):
        relative = path.relative_to(source)
        mode = path.lstat().st_mode
        if stat.S_ISLNK(mode) or any(part.startswith('.') for part in relative.parts):
            raise ValueError('Hidden paths and symlinks are not public build assets')
        rel = relative.as_posix()
        if stat.S_ISREG(mode):
            if rel == 'build-manifest.json':
                continue
            if rel not in declared:
                raise ValueError('Undeclared public build asset')
            paths.append((path, relative, rel))
        elif not stat.S_ISDIR(mode) or rel not in {'exports', 'reference'}:
            raise ValueError('Unexpected build subdirectory')
    if {rel for _, _, rel in paths} != set(declared):
        raise ValueError('Build is missing declared files')
    destination.parent.mkdir(parents=True, exist_ok=True)
    committed = attempted = False
    original_error = None
    result = None
    try:
        with tempfile.TemporaryDirectory(prefix='.onionsurge-stage-', dir=destination.parent) as staging:
            try:
                release = Path(staging) / 'release'
                public = release / 'public'
                public.mkdir(parents=True)
                made_dirs = {public}
                objects = {}
                data_payload = None
                for path, relative, rel in sorted(paths, key=lambda item: item[2]):
                    if live and rel not in FRONTEND:
                        raw = read_regular_bytes(path)
                        if sha256(raw) != declared[rel]:
                            raise ValueError('Asset digest differs from application build manifest')
                        if rel == 'data.json':
                            data_payload = _validate_staged_data(raw)
                        continue
                    output = public / relative
                    if output.parent not in made_dirs:
                        output.parent.mkdir(parents=True, exist_ok=True)
                        made_dirs.add(output.parent)
                    size, digest = stage_asset_file(path, output)
                    if digest != declared[rel]:
                        raise ValueError('Asset digest differs from application build manifest')
                    if rel == 'data.json':
                        data_payload = _validate_staged_data(read_regular_bytes(output))
                    objects[rel] = {'bytes': size, 'sha256': digest}
                if data_payload is None:
                    raise ValueError('Missing data.json payload')
                if data_payload['window_seconds'] == 86400 and any(name.endswith('-24h.csv') for name in declared):
                    raise ValueError('Daily release cannot declare -24h.csv exports')
                if not live:
                    _verify_non_live_exports(declared, data_payload)
                    ref_doc = data_payload.get('reference_document')
                    if not isinstance(ref_doc, str) or ref_doc not in declared:
                        raise ValueError('Declared files missing data.json reference_document')
                receipt = {'schema_version': 1, 'application_commit': commit,
                           'build_manifest_sha256': sha256(manifest_bytes), 'objects': objects}
                if live:
                    receipt['mode'] = 'live'
                (release / 'asset-release.json').write_text(json.dumps(receipt, indent=2) + '\n', encoding='utf-8')
                result = {'status': 'complete', 'release_directory': str(destination),
                          'public_directory': str(destination / 'public'), 'receipt': receipt, 'warnings': []}
                # Both assets and their receipt cross the publication boundary
                # together. Never delete or roll back a published release path.
                attempted = True
                rename_noreplace(release, destination)
                committed = True
            except BaseException as error:
                original_error = error
                raise
    except BaseException as error:
        if committed:
            message = f'Release completed at {destination}; temporary staging cleanup failed for {staging}: {error}'
            if isinstance(error, Exception):
                result['warnings'].append(message)
                return result
            # Preserve interrupts, but make the completed publication explicit.
            error.add_note(message + '. The published release was retained.')
            raise
        primary = original_error if original_error is not None else error
        if primary is not error:
            primary.add_note(f'Temporary-directory cleanup also failed: {error}')
        if attempted:
            primary.add_note(f'Publication was attempted at {destination}. Inspect that release root and its '
                             'asset-release.json before retrying; no published path was removed.')
        if primary is error:
            raise
        raise primary from error
    return result


def main(argv=None):
    parser = argparse.ArgumentParser()
    parser.add_argument('--source', required=True, type=Path)
    parser.add_argument('--application-commit', required=True)
    parser.add_argument('--output', type=Path, default=ROOT / 'build',
                        help='New immutable release root (contains public/ and asset-release.json)')
    parser.add_argument('--live', action='store_true',
                        help='Stage only the live frontend; omit bundled sample data and downloads')
    args = parser.parse_args(argv)
    try:
        result = prepare(args.source, args.output, args.application_commit, live=args.live)
    except (OSError, ValueError, RecursionError) as error:
        notes = '\n'.join(getattr(error, '__notes__', []))
        parser.exit(1, f'Asset staging failed: {error}\n' + (notes + '\n' if notes else ''))
    receipt = result['receipt']
    print(f"Completed release {result['release_directory']}: {len(receipt['objects'])} public assets "
          f"at application commit {receipt['application_commit']}.")
    for warning in result['warnings']:
        print('Warning: ' + warning, file=sys.stderr)


if __name__ == '__main__':
    main()
