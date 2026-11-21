#!/usr/bin/env python3
"""Stage the live OnionSurge frontend privately and render private Caddy config."""
import argparse
import ipaddress
import json
import os
from pathlib import Path
import re
import shutil
import stat
import tempfile
from urllib.error import HTTPError, URLError
from urllib.parse import urlparse
from urllib.request import ProxyHandler, Request, build_opener
import uuid

from configuration import enabled, expand_path, load_config
from operations import (FRONTEND, INDEX_CSP, MAX_OBJECT, MAX_ROOT, NoRedirect, PERMISSIONS_POLICY, SANDBOX_CSP,
                        SUB_24H_SUFFIX, atomic_write, ensure_web_directory, rename_noreplace, sha256, sync_directory,
                        verify_checkout, verify_frontend)
from publication import (IMMUTABLE_CACHE_CONTROL, LocalDirectoryProvider, MAX_MANIFEST, PublicationError, matches,
                         parse_json, parse_local_retain_releases, publication_lock, read_receipt, read_regular,
                         validate_manifest, validate_root)

ROOT = Path(__file__).resolve().parents[1]
URL_PREFIX_RE = re.compile(r'/[a-z][a-z0-9_-]{0,62}\Z')
FRONTEND_TAG_RE = re.compile(r'[a-f0-9]{12}-[a-f0-9]{12}\Z')
ALLOWED_BIND_NETWORKS = (
    ipaddress.ip_network('127.0.0.0/8'),
    ipaddress.ip_network('10.0.0.0/8'),
    ipaddress.ip_network('172.16.0.0/12'),
    ipaddress.ip_network('192.168.0.0/16'),
    ipaddress.ip_network('100.64.0.0/10'),
    ipaddress.ip_network('::1/128'),
    ipaddress.ip_network('fc00::/7'),
    ipaddress.ip_network('fd7a:115c:a1e0::/48'),
)


class LocalSiteError(ValueError):
    """Fixed, non-sensitive error codes for private local/VPN rollout."""


def require(condition, code):
    if not condition:
        raise LocalSiteError(code)


def parse_private_bind(raw):
    """Validate and normalize `<private-or-loopback-ip>:<port>`."""
    require(isinstance(raw, str) and bool(raw.strip()), 'invalid_local_bind_addr')
    text = raw.strip()
    if text.startswith('['):
        require(']:' in text, 'invalid_local_bind_addr')
        host, port_str = text[1:].split(']:', 1)
    else:
        require(text.count(':') == 1, 'invalid_local_bind_addr')
        host, port_str = text.split(':', 1)
    require(port_str.isdigit(), 'invalid_local_bind_addr')
    port = int(port_str)
    require(1 <= port <= 65535, 'invalid_local_bind_addr')
    try:
        ip = ipaddress.ip_address(host)
    except ValueError as error:
        raise LocalSiteError('invalid_local_bind_addr') from error
    require(not ip.is_unspecified and not ip.is_multicast and not ip.is_link_local
            and any(ip in network for network in ALLOWED_BIND_NETWORKS), 'non_private_local_bind_addr')
    formatted_host = f'[{ip}]' if ip.version == 6 else str(ip)
    return str(ip), port, f'{formatted_host}:{port}'


def _caddy_path(path):
    raw = str(path)
    require(not any(ord(ch) < 32 or ord(ch) == 127 or ch in '"{}$' for ch in raw),
            'invalid_local_site_directory')
    return '"' + raw.replace('\\', '\\\\') + '"'


def normalize_url_prefix(raw):
    """Return `''` for root `'/'` or normalized `'/slug'` for path-scoped mounts."""
    value = '/' if raw is None else str(raw).strip()
    if not value or value == '/':
        return ''
    require(bool(URL_PREFIX_RE.fullmatch(value)), 'invalid_local_url_prefix')
    return value


def settings(config, deploy_root, *, require_checkouts=True):
    """Validate local/VPN hosting settings and isolate paths from state/checkouts."""
    require(enabled(config, 'LOCAL_ENABLED'), 'local_site_not_enabled')
    application = expand_path(config['APPLICATION_DIR']).resolve(strict=True)
    deploy_resolved = Path(deploy_root).resolve(strict=True)
    if require_checkouts:
        verify_checkout(application, 'onionsurge', config['APPLICATION_COMMIT'])
        verify_checkout(deploy_resolved, 'onionsurge-deploy', config['DEPLOYMENT_COMMIT'])
    state_raw = config.get('STATE_DIR', '')
    require(isinstance(state_raw, str) and bool(state_raw.strip()), 'missing_state_dir')
    state = expand_path(state_raw)
    logs_raw = config.get('LOG_DIR', str(state / 'logs'))
    logs = expand_path(logs_raw)
    site_raw = config.get('LOCAL_SITE_DIR', '')
    require(isinstance(site_raw, str) and bool(site_raw.strip()), 'missing_local_site_directory')
    site_expanded = expand_path(site_raw)
    require(not os.path.islink(site_expanded), 'unsafe_local_site_directory')
    site_dir = site_expanded.resolve()
    _caddy_path(site_dir)
    forbidden_overlaps = [application, deploy_resolved, state.resolve(), logs.resolve()]
    source_raw = config.get('SOURCE_CONFIG', '')
    if isinstance(source_raw, str) and source_raw.strip():
        forbidden_overlaps.append(expand_path(source_raw).resolve())
    tr_raw = config.get('TIME_RANGES_CONFIG', '')
    require(isinstance(tr_raw, str), 'invalid_time_ranges_config')
    if tr_raw.strip():
        try:
            tr_expanded = expand_path(tr_raw)
        except ValueError as error:
            raise LocalSiteError('invalid_time_ranges_config') from error
        require(not os.path.islink(tr_expanded), 'invalid_time_ranges_config')
        forbidden_overlaps.append(tr_expanded.resolve())
    for other in forbidden_overlaps:
        require(site_dir != other and not site_dir.is_relative_to(other) and not other.is_relative_to(site_dir),
                'local_site_overlaps_state_or_checkout')
    bind_ip, bind_port, bind_addr = parse_private_bind(config.get('LOCAL_BIND_ADDR', '127.0.0.1:8781'))
    tls_raw = str(config.get('LOCAL_TLS_BIND_ADDR', '') or '').strip()
    tls_parsed = parse_private_bind(tls_raw) if tls_raw else None
    if tls_parsed is not None:
        require(tls_parsed[2] != bind_addr, 'conflicting_local_bind_addrs')
    try:
        retain = parse_local_retain_releases(config)
    except PublicationError as error:
        raise LocalSiteError(str(error)) from error
    return {
        'application': application,
        'deploy_root': deploy_resolved,
        'state': state,
        'site_dir': site_dir,
        'bind_ip': bind_ip,
        'bind_port': bind_port,
        'bind_addr': bind_addr,
        'tls_ip': tls_parsed[0] if tls_parsed else None,
        'tls_bind_addr': tls_parsed[2] if tls_parsed else None,
        'prefix': normalize_url_prefix(config.get('LOCAL_URL_PREFIX', '/')),
        'retain': retain,
    }


def _site_directives(values, indent='    '):
    current_root = _caddy_path(values['site_dir'] / 'current')
    site_root = _caddy_path(values['site_dir'])
    p = values['prefix']
    root_path = f'{p}/' if p else '/'
    index_path = f'{p}/index.html'
    assets = ' '.join(f'{p}/{name}' for name in ('app.js', 'live.js', 'style.css'))
    manifest_path = f'{p}/data/manifest.json'
    strip = f'\n{indent}    uri strip_prefix {p}' if p else ''
    # Go RE2 in Caddy lacks negative lookahead; reject sub-24h suffixes first.
    legacy_re = f'^{re.escape(p)}/data/releases/.+-{SUB_24H_SUFFIX}\\.csv$'
    release_re = (
        f'^{re.escape(p)}/data/releases/[A-Za-z0-9][A-Za-z0-9_.-]{{0,95}}/'
        '(?:manifest\\.json|data\\.json|'
        'reference/reference-[0-9]{4}-[0-9]{2}-[0-9]{2}-[a-f0-9]{12}\\.json|'
        'exports/[a-z][a-z0-9_-]{0,95}-[a-z0-9]+(?:-[a-z0-9]+){0,7}\\.csv)$'
    )
    lines = [
        f'{indent}header {{',
        f'{indent}    X-Robots-Tag "noindex, nofollow"',
        f'{indent}    X-Content-Type-Options "nosniff"',
        f'{indent}    Referrer-Policy "no-referrer"',
        f'{indent}    X-Frame-Options "DENY"',
        f'{indent}    Cross-Origin-Opener-Policy "same-origin"',
        f'{indent}    Cross-Origin-Resource-Policy "same-origin"',
        f'{indent}    Permissions-Policy "{PERMISSIONS_POLICY}"',
        f'{indent}    -Server',
        f'{indent}}}',
    ]
    if p:
        lines.extend([
            f'{indent}@onionsurge_bare path {p}',
            f'{indent}handle @onionsurge_bare {{',
            f'{indent}    redir * {p}/ 308',
            f'{indent}}}',
        ])
    lines.extend([
        f'{indent}@onionsurge_root path {root_path}',
        f'{indent}handle @onionsurge_root {{{strip}',
        f'{indent}    root * {current_root}',
        f'{indent}    rewrite * /index.html',
        f'{indent}    header Content-Security-Policy "{INDEX_CSP}"',
        f'{indent}    header Cache-Control "no-store"',
        f'{indent}    file_server',
        f'{indent}}}',
        f'{indent}@onionsurge_index path {index_path}',
        f'{indent}handle @onionsurge_index {{{strip}',
        f'{indent}    root * {current_root}',
        f'{indent}    header Content-Security-Policy "{INDEX_CSP}"',
        f'{indent}    header Cache-Control "no-store"',
        f'{indent}    file_server',
        f'{indent}}}',
        f'{indent}@onionsurge_assets path {assets}',
        f'{indent}handle @onionsurge_assets {{{strip}',
        f'{indent}    root * {current_root}',
        f'{indent}    header Content-Security-Policy "{SANDBOX_CSP}"',
        f'{indent}    header Cache-Control "no-store"',
        f'{indent}    file_server',
        f'{indent}}}',
        f'{indent}@onionsurge_manifest path {manifest_path}',
        f'{indent}handle @onionsurge_manifest {{{strip}',
        f'{indent}    root * {site_root}',
        f'{indent}    header Content-Security-Policy "{SANDBOX_CSP}"',
        f'{indent}    header Cache-Control "no-store"',
        f'{indent}    file_server',
        f'{indent}}}',
        f'{indent}@onionsurge_legacy path_regexp legacy_csv {legacy_re}',
        f'{indent}handle @onionsurge_legacy {{',
        f'{indent}    respond 404',
        f'{indent}}}',
        f'{indent}@onionsurge_release path_regexp release_obj {release_re}',
        f'{indent}handle @onionsurge_release {{{strip}',
        f'{indent}    root * {site_root}',
        f'{indent}    header Content-Security-Policy "{SANDBOX_CSP}"',
        f'{indent}    header Cache-Control "{IMMUTABLE_CACHE_CONTROL}"',
        f'{indent}    file_server',
        f'{indent}}}',
        f'{indent}handle {{',
        f'{indent}    respond 404',
        f'{indent}}}',
    ])
    return '\n'.join(lines)


def render_caddyfile(values, *, include_global=True):
    """Render private Caddy v2 configuration bound strictly to loopback/VPN IPs."""
    body = _site_directives(values)
    http_addr = f"http://:{values['bind_port']}"
    if not values.get('tls_bind_addr'):
        return f"{http_addr} {{\n    bind {values['bind_ip']}\n{body}\n}}\n"
    global_block = "{\n    auto_https disable_redirects\n}\n\n" if include_global else ""
    return (
        f"{global_block}"
        f"(onionsurge_private_site) {{\n{body}\n}}\n\n"
        f"{http_addr} {{\n    bind {values['bind_ip']}\n    import onionsurge_private_site\n}}\n\n"
        f"https://{values['tls_bind_addr']} {{\n    bind {values['tls_ip']}\n    tls internal\n    import onionsurge_private_site\n}}\n"
    )


def _same_verified_frontend(target_dir, public_dir, receipt):
    if not os.path.isdir(target_dir) or target_dir.is_symlink():
        return False
    actual = {p.name for p in target_dir.iterdir()}
    if actual != FRONTEND:
        return False
    for name, expected in receipt['objects'].items():
        try:
            raw = read_regular(target_dir / name, expected['bytes'])
        except (OSError, ValueError):
            return False
        if len(raw) != expected['bytes'] or sha256(raw) != expected['sha256']:
            return False
        if raw != read_regular(public_dir / name, expected['bytes']):
            return False
    return True


def _prune_frontend_dirs(frontend_root, keep_tag, retain):
    if not os.path.lexists(frontend_root):
        return []
    entries = []
    for entry in frontend_root.iterdir():
        if entry.name == keep_tag or entry.is_symlink() or not FRONTEND_TAG_RE.fullmatch(entry.name):
            continue
        info = entry.lstat()
        if stat.S_ISDIR(info.st_mode) and info.st_uid == os.geteuid():
            entries.append((info.st_mtime_ns, entry.name, entry))
    entries.sort(reverse=True)
    extra_keep = max(0, retain - 1)
    removed = []
    for _, name, entry in entries[extra_keep:]:
        shutil.rmtree(entry)
        removed.append(name)
    if removed:
        sync_directory(frontend_root)
    return removed


def _http_get(opener, url, maximum):
    req = Request(url, method='GET')
    try:
        with opener.open(req, timeout=15) as resp:
            raw = resp.read(maximum + 1)
            return resp.status, {k.lower(): v for k, v in resp.headers.items()}, raw
    except HTTPError as error:
        with error:
            raw = error.read(1024)
            return error.code, {k.lower(): v for k, v in error.headers.items()}, raw
    except (OSError, URLError) as error:
        raise LocalSiteError('local_http_verify_failed') from error


def verify_http(base_url, *, state, url_prefix='', expected_release=None):
    """Verify serving status, zero redirects, security headers, and manifest match."""
    parsed = urlparse(base_url)
    require(parsed.scheme in {'http', 'https'} and bool(parsed.hostname), 'invalid_verify_url')
    base = base_url.rstrip('/') + url_prefix
    opener = build_opener(ProxyHandler({}), NoRedirect())

    status, headers, body = _http_get(opener, base + '/', MAX_OBJECT)
    require(status == 200 and b'data-mode="live"' in body, 'verify_root_failed')
    require('noindex' in headers.get('x-robots-tag', '').lower(), 'verify_headers_failed')
    require(headers.get('x-content-type-options', '').lower() == 'nosniff', 'verify_headers_failed')
    require(headers.get('x-frame-options', '').upper() == 'DENY', 'verify_headers_failed')
    require('no-store' in headers.get('cache-control', '').lower(), 'verify_headers_failed')
    require("script-src 'self'" in headers.get('content-security-policy', ''), 'verify_headers_failed')

    status, mheaders, manifest_raw = _http_get(opener, base + '/data/manifest.json', MAX_ROOT)
    require(status == 200 and len(manifest_raw) <= MAX_ROOT, 'verify_manifest_failed')
    require('no-store' in mheaders.get('cache-control', '').lower(), 'verify_headers_failed')
    require('sandbox' in mheaders.get('content-security-policy', ''), 'verify_headers_failed')
    try:
        root = validate_root(manifest_raw)
    except PublicationError as error:
        raise LocalSiteError('verify_manifest_failed') from error

    receipt_path = state / 'published.json'
    if os.path.lexists(receipt_path):
        receipt_root = read_receipt(receipt_path)['root']
        require(root == receipt_root, 'verify_manifest_mismatch')

    release_id = expected_release or root['current']['id']
    if release_id == root['current']['id']:
        desc = root['current']
    elif isinstance(root.get('previous'), dict) and release_id == root['previous'].get('id'):
        desc = root['previous']
    else:
        raise LocalSiteError('verify_manifest_mismatch')
    allow_previous_v6 = release_id != root['current']['id']

    status, rmheaders, rel_manifest_raw = _http_get(
        opener, f'{base}/data/releases/{release_id}/manifest.json', MAX_MANIFEST)
    require(status == 200 and len(rel_manifest_raw) <= MAX_MANIFEST, 'verify_release_manifest_failed')
    require('immutable' in rmheaders.get('cache-control', '').lower(), 'verify_headers_failed')
    require('sandbox' in rmheaders.get('content-security-policy', ''), 'verify_headers_failed')
    try:
        rel_manifest = validate_manifest(rel_manifest_raw, desc, allow_previous_v6=allow_previous_v6)
    except PublicationError as error:
        raise LocalSiteError('verify_release_manifest_failed') from error

    status, dheaders, data_raw = _http_get(opener, f'{base}/data/releases/{release_id}/data.json', MAX_OBJECT)
    require(status == 200 and len(data_raw) <= MAX_OBJECT, 'verify_data_failed')
    require('immutable' in dheaders.get('cache-control', '').lower(), 'verify_headers_failed')
    require('sandbox' in dheaders.get('content-security-policy', ''), 'verify_headers_failed')
    require(matches(data_raw, rel_manifest['objects']['data.json']), 'verify_data_failed')

    csv_24h = sorted(name for name in rel_manifest['objects'] if name.endswith('-24h.csv'))
    if csv_24h:
        csv_name = csv_24h[0]
        status, cheaders, csv_raw = _http_get(
            opener, f'{base}/data/releases/{release_id}/{csv_name}', MAX_OBJECT)
        require(status == 200 and len(csv_raw) <= MAX_OBJECT, 'verify_data_failed')
        require('immutable' in cheaders.get('cache-control', '').lower(), 'verify_headers_failed')
        require('sandbox' in cheaders.get('content-security-policy', ''), 'verify_headers_failed')
        require(matches(csv_raw, rel_manifest['objects'][csv_name]), 'verify_data_failed')

    probe_1h = f'{csv_24h[0][:-8]}-1h.csv' if csv_24h else 'exports/probe-1h.csv'
    status, _, _ = _http_get(opener, f'{base}/data/releases/{release_id}/{probe_1h}', 4096)
    require(status == 404, 'verify_404_failed')

    status, _, _ = _http_get(opener, f'{base}/nonexistent-onionsurge-probe-{uuid.uuid4().hex}', 4096)
    require(status == 404, 'verify_404_failed')
    return {'url': base + '/', 'release_id': release_id}


def rollout(config, frontend_release, dry_run=False, *, deploy_root=ROOT, write_caddy=None, verify_url=None,
            include_global=True):
    """Validate and atomically activate a private local frontend release."""
    values = settings(config, deploy_root)
    public, receipt = verify_frontend(frontend_release, config['APPLICATION_COMMIT'], error_type=LocalSiteError)
    site_dir = values['site_dir']
    state = values['state']
    app_commit = config['APPLICATION_COMMIT']
    manifest_sha = receipt['build_manifest_sha256']
    tag = f'{app_commit[:12]}-{manifest_sha[:12]}'
    caddy_text = render_caddyfile(values, include_global=include_global)
    caddy_path = Path(write_caddy) if write_caddy is not None else None
    if caddy_path is not None:
        require(not os.path.islink(caddy_path), 'unsafe_caddy_output_path')

    with publication_lock(state):
        require(not os.path.lexists(state / 'pending-promotion.json'), 'pending_publication_recovery')
        receipt_path = state / 'published.json'
        has_published = os.path.lexists(receipt_path)
        if not has_published:
            require(dry_run, 'published_release_required')
        else:
            pub_receipt = read_receipt(receipt_path)
            provider = LocalDirectoryProvider(site_dir, retain_releases=values['retain'])
            require(provider.identity in pub_receipt['destinations'], 'local_destination_not_published')

        if dry_run:
            return {'status': 'dry-run-complete', 'frontend_id': tag, 'site_dir': str(site_dir),
                    'bind_addr': values['bind_addr'], 'tls_bind_addr': values['tls_bind_addr']}

        verify_checkout(values['application'], 'onionsurge', app_commit)
        verify_checkout(values['deploy_root'], 'onionsurge-deploy', config['DEPLOYMENT_COMMIT'])
        verify_frontend(frontend_release, app_commit, error_type=LocalSiteError)

        ensure_web_directory(site_dir, site_dir / 'frontend', create=True)
        frontend_root = site_dir / 'frontend'
        target_dir = frontend_root / tag
        if not _same_verified_frontend(target_dir, public, receipt):
            require(not os.path.lexists(target_dir), 'frontend_target_conflict')
            parent_sgid = stat.S_ISGID if (frontend_root.lstat().st_mode & stat.S_ISGID) else 0
            stage = Path(tempfile.mkdtemp(prefix='.stage-', dir=frontend_root))
            try:
                for name, expected in receipt['objects'].items():
                    raw = read_regular(public / name, expected['bytes'])
                    require(len(raw) == expected['bytes'] and sha256(raw) == expected['sha256'], 'frontend_integrity_failure')
                    atomic_write(stage / name, raw, mode=0o640)
                os.chmod(stage, parent_sgid | 0o750)
                sync_directory(stage)
                rename_noreplace(stage, target_dir)
                sync_directory(frontend_root)
            finally:
                if stage.exists():
                    shutil.rmtree(stage, ignore_errors=True)

        temp_link = site_dir / f'.current-{uuid.uuid4().hex[:12]}'
        activated = False
        try:
            os.symlink(f'frontend/{tag}', temp_link)
            os.replace(temp_link, site_dir / 'current')
            activated = True
            sync_directory(site_dir)
            if caddy_path is not None:
                require(not os.path.islink(caddy_path), 'unsafe_caddy_output_path')
                atomic_write(caddy_path, caddy_text.encode('utf-8'), mode=0o640)
            verified = verify_http(verify_url, state=state, url_prefix=values['prefix']) if verify_url else None
        except (OSError, ValueError, TypeError, KeyError) as error:
            if activated:
                raise LocalSiteError('activated_unverified_frontend') from error
            raise
        finally:
            if os.path.lexists(temp_link):
                try:
                    temp_link.unlink()
                except OSError:
                    pass

        _prune_frontend_dirs(frontend_root, tag, values['retain'])
        result = {'status': 'deployed', 'frontend_id': tag, 'site_dir': str(site_dir), 'bind_addr': values['bind_addr']}
        if values['tls_bind_addr']:
            result['tls_bind_addr'] = values['tls_bind_addr']
        if verified:
            result['verified_url'] = verified['url']
        return result


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--config', required=True)
    parser.add_argument('--frontend-release', type=Path)
    parser.add_argument('--dry-run', action='store_true')
    parser.add_argument('--print-caddy', action='store_true')
    parser.add_argument('--write-caddy', type=Path)
    parser.add_argument('--verify-url')
    parser.add_argument('--no-global', action='store_true')
    args = parser.parse_args(argv)
    if not args.print_caddy and args.frontend_release is None and not args.verify_url:
        parser.error('--frontend-release is required unless --print-caddy or --verify-url is used')
    try:
        config = load_config(args.config, required=('APPLICATION_DIR', 'APPLICATION_COMMIT', 'DEPLOYMENT_COMMIT', 'STATE_DIR'))
        if args.print_caddy:
            print(render_caddyfile(settings(config, ROOT, require_checkouts=False), include_global=not args.no_global), end='')
            return
        if args.frontend_release is None and args.verify_url:
            vals = settings(config, ROOT, require_checkouts=False)
            print(json.dumps(verify_http(args.verify_url, state=vals['state'], url_prefix=vals['prefix']), sort_keys=True))
            return
        result = rollout(config, args.frontend_release, args.dry_run, write_caddy=args.write_caddy,
                         verify_url=args.verify_url, include_global=not args.no_global)
    except LocalSiteError as error:
        if str(error) == 'activated_unverified_frontend':
            parser.exit(1, 'New frontend is active, but post-activation verification failed; previous frontend retained.\n')
        parser.exit(1, 'Local site rollout failed. Check private configuration and permissions.\n')
    except (OSError, ValueError, TypeError, KeyError):
        parser.exit(1, 'Local site rollout failed. Check private configuration and permissions.\n')
    print(json.dumps(result, sort_keys=True))


if __name__ == '__main__':
    main()
