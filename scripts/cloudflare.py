#!/usr/bin/env python3
"""Deploy a reviewed live frontend through the pinned Cloudflare toolchain."""
import argparse
from http.client import HTTPException
import json
import os
from pathlib import Path
import re
import shutil
import stat
import subprocess
import tempfile
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode
from urllib.request import Request, ProxyHandler, build_opener

from configuration import CF_ID, PREFIX, REGION, enabled, expand_path, load_config, matched
from operations import (FRONTEND, NoRedirect, atomic_private, child_environment, json_bytes, parse_state_retain_runs,
                        paths_disjoint, sync_directory, verify_checkout, verify_frontend as _verify_frontend)
from publication import providers_from_config, publication_lock, read_receipt, read_regular

ROOT = Path(__file__).resolve().parents[1]
API_BASE = 'https://api.cloudflare.com/client/v4'
CANONICAL = 'example.com'
ALIAS = 'onionsurge.example.com'
ROUTES = (CANONICAL + '/onionsurge*', ALIAS + '/*')
OWNER = '1aeo/onionsurge-deploy'
NODE_VERSION = '24.21.0'
WRANGLER_VERSION = '4.158.0'
COMPATIBILITY_DATE = '2026-11-18'
NAME = re.compile(r'[a-z0-9][a-z0-9-]{0,62}\Z')
LABEL = re.compile(r'[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?\Z')
BUCKET = re.compile(r'[a-z0-9][a-z0-9-]{1,61}[a-z0-9]\Z')
GENERATION = re.compile(r'[A-Za-z0-9_-]{1,128}\Z')
API_BYTES = 2 * 1024**2
COMMAND_SECONDS = 240
MAX_DNS_PAGES = 100


class RolloutError(ValueError):
    """Only fixed, non-sensitive error codes cross the CLI boundary."""


def require(condition, code):
    if not condition:
        raise RolloutError(code)


class Cloudflare:
    def __init__(self, token):
        self.token = token
        self.opener = build_opener(ProxyHandler({}), NoRedirect())

    def request(self, method, path, payload=None, missing=False):
        require(path.startswith('/'), 'invalid_api_path')
        request = Request(API_BASE + path, data=None if payload is None else json_bytes(payload), method=method,
                          headers={'Authorization': 'Bearer ' + self.token, 'Content-Type': 'application/json'})
        try:
            with self.opener.open(request, timeout=30) as response:
                raw = response.read(API_BYTES + 1)
        except HTTPError as error:
            with error:
                if missing and error.code == 404:
                    return None
            raise RolloutError('cloudflare_api_failed') from None
        except (OSError, URLError, HTTPException):
            raise RolloutError('cloudflare_api_failed') from None
        require(len(raw) <= API_BYTES, 'cloudflare_response_limit')
        try:
            value = json.loads(raw)
        except (ValueError, UnicodeError):
            raise RolloutError('invalid_cloudflare_response') from None
        require(isinstance(value, dict) and value.get('success') is True and 'result' in value, 'cloudflare_api_failed')
        return value


def _valid_hostname(value):
    return (
        isinstance(value, str)
        and 1 <= len(value) <= 253
        and '.' in value
        and not value.startswith('.')
        and not value.endswith('.')
        and all(bool(LABEL.fullmatch(part)) for part in value.split('.'))
    )


def _zone_routes(canonical=CANONICAL, alias=ALIAS):
    return (canonical + '/onionsurge*', alias + '/*')


def settings(config, deploy_root):
    application = expand_path(config['APPLICATION_DIR']).resolve(strict=True)
    verify_checkout(application, 'onionsurge', config['APPLICATION_COMMIT'])
    verify_checkout(deploy_root, 'onionsurge-deploy', config['DEPLOYMENT_COMMIT'])
    state = expand_path(config['STATE_DIR'])
    paths_disjoint((state,), (application, deploy_root), RolloutError, 'state_overlaps_checkout')
    account, zone = config.get('CLOUDFLARE_ACCOUNT_ID'), config.get('CLOUDFLARE_ZONE_ID')
    worker, generation = config.get('WORKER_NAME', 'onionsurge'), config.get('ROOT_CACHE_GENERATION')
    token = config.get('CLOUDFLARE_API_TOKEN', '')
    require(matched(CF_ID, account) and matched(CF_ID, zone), 'invalid_cloudflare_identity')
    require(matched(NAME, worker) and matched(GENERATION, generation), 'invalid_worker_configuration')
    require(isinstance(token, str) and 20 <= len(token) <= 512 and not any(c.isspace() for c in token), 'invalid_cloudflare_credential')
    raw_canonical = config.get('CLOUDFLARE_CANONICAL_HOST', '')
    require(isinstance(raw_canonical, str), 'invalid_cloudflare_host')
    canonical = CANONICAL if raw_canonical == '' else raw_canonical
    require(_valid_hostname(canonical), 'invalid_cloudflare_host')
    raw_alias = config.get('CLOUDFLARE_REDIRECT_HOST', '')
    require(isinstance(raw_alias, str), 'invalid_cloudflare_host')
    alias = f'onionsurge.{canonical}' if raw_alias == '' else raw_alias
    require(_valid_hostname(alias) and alias != canonical and alias.endswith('.' + canonical), 'invalid_cloudflare_host')
    retain_runs = parse_state_retain_runs(config, error_type=RolloutError)
    require(enabled(config, 'DO_ENABLED'), 'spaces_primary_required')
    bucket, region, prefix = (config.get(key, '') for key in ('DO_SPACES_BUCKET', 'DO_SPACES_REGION', 'DO_SPACES_PREFIX'))
    require(matched(BUCKET, bucket) and matched(REGION, region) and matched(PREFIX, prefix), 'invalid_spaces_destination')
    r2 = enabled(config, 'R2_ENABLED')
    result = {'application': application, 'state': state, 'account': account, 'zone': zone, 'worker': worker,
              'generation': generation, 'token': token, 'canonical': canonical, 'alias': alias,
              'routes': _zone_routes(canonical, alias), 'retain_runs': retain_runs,
              'origin': f'https://{bucket}.{region}.digitaloceanspaces.com/{prefix}',
              'r2_enabled': r2, 'r2_bucket': config.get('R2_BUCKET', '') if r2 else '',
              'r2_prefix': config.get('R2_PREFIX', '') if r2 else ''}
    if result['r2_enabled']:
        require(matched(BUCKET, result['r2_bucket']) and matched(PREFIX, result['r2_prefix']), 'invalid_r2_destination')
    return result


def verify_frontend(directory, application_commit):
    return _verify_frontend(directory, application_commit, error_type=RolloutError)


def route_overlaps(pattern, *, canonical=CANONICAL, alias=ALIAS):
    require(isinstance(pattern, str) and 0 < len(pattern) <= 2048 and not any(c.isspace() for c in pattern), 'invalid_route_response')
    if '://' in pattern:
        scheme, pattern = pattern.split('://', 1)
        require(scheme in {'http', 'https', '*'}, 'unsupported_route_pattern')
    host, separator, route_path = pattern.partition('/')
    require(bool(re.fullmatch(r'[A-Za-z0-9.*-]+', host)) and separator, 'unsupported_route_pattern')
    route_path = '/' + route_path
    host_re = re.compile(re.escape(re.sub(r'\*+', '*', host.lower())).replace(r'\*', '.*'))
    if host_re.fullmatch(alias):
        return True
    if not host_re.fullmatch(canonical):
        return False
    prefix = '/onionsurge'
    if '*' not in route_path:
        return route_path.startswith(prefix)
    fixed = route_path.split('*', 1)[0]
    return fixed.startswith(prefix) or prefix.startswith(fixed)


def check_routes(routes, worker, complete=False, *, canonical=CANONICAL, alias=ALIAS):
    require(isinstance(routes, list), 'invalid_routes_response')
    expected_routes = _zone_routes(canonical, alias)
    found = set()
    for route in routes:
        require(isinstance(route, dict) and isinstance(route.get('pattern'), str), 'invalid_routes_response')
        pattern, script = route['pattern'], route.get('script')
        require(script is None or isinstance(script, str), 'invalid_routes_response')
        if pattern in expected_routes:
            require(pattern not in found and script == worker, 'namespace_route_conflict')
            found.add(pattern)
        elif script == worker:
            raise RolloutError('worker_has_unrelated_routes')
        elif route_overlaps(pattern, canonical=canonical, alias=alias):
            raise RolloutError('namespace_route_conflict')
    if complete:
        require(found == set(expected_routes), 'routes_not_complete')


def dns_records(api, zone, name):
    query = {'name': name, 'per_page': 100}
    first = api.request('GET', f'/zones/{zone}/dns_records?' + urlencode(query))
    records = list(complete_account_inventory(first, 'incomplete_dns_response', filtered=True, page=1))
    info = first.get('result_info')
    total_pages = info.get('total_pages') if isinstance(info, dict) else None
    if type(total_pages) is int and total_pages > 1:
        for page in range(2, total_pages + 1):
            page_response = api.request('GET', f'/zones/{zone}/dns_records?' + urlencode({**query, 'page': page}))
            page_records = complete_account_inventory(
                page_response, 'incomplete_dns_response', filtered=True, page=page, expected_total_pages=total_pages)
            if not page_records:
                break
            records.extend(page_records)
    require(all(isinstance(record, dict) and isinstance(record.get('name'), str)
                and record['name'].lower().removesuffix('.') == name for record in records), 'invalid_dns_response')
    return records


def check_alias(records, *, canonical=CANONICAL):
    if not records:
        return False
    require(len(records) == 1, 'alias_dns_conflict')
    record = records[0]
    require(record.get('type') == 'CNAME' and record.get('proxied') is True
            and isinstance(record.get('content'), str) and record['content'].lower().removesuffix('.') == canonical,
            'alias_dns_conflict')
    return True


def plain_bindings(settings_value):
    require(isinstance(settings_value, dict) and isinstance(settings_value.get('bindings'), list), 'invalid_worker_settings')
    values = {}
    for binding in settings_value['bindings']:
        require(isinstance(binding, dict) and isinstance(binding.get('name'), str), 'invalid_worker_binding')
        require(binding['name'] not in values, 'duplicate_worker_binding')
        values[binding['name']] = binding
    return values


def worker_destination(bindings):
    return (bindings.get('PUBLIC_DATA_ORIGIN', {}).get('text'),
            bindings.get('ONIONSURGE_DATA', {}).get('bucket_name', ''), bindings.get('R2_PREFIX', {}).get('text', ''))


def check_published_destination(config, state, required):
    require(not os.path.lexists(state / 'pending-promotion.json'), 'pending_publication_recovery')
    receipt = state / 'published.json'
    if not os.path.lexists(receipt):
        require(not required, 'published_release_required')
        return
    expected = sorted(provider.identity for provider in providers_from_config(config))
    require(read_receipt(receipt)['destinations'] == expected, 'publisher_destination_migration_required')


def complete_account_inventory(response, code, *, filtered=False, page=1, expected_total_pages=None):
    """Validate inventory pagination; unfiltered account endpoints never accept multiple pages."""
    require(isinstance(response, dict) and isinstance(response.get('result'), list), code)
    records = response['result']
    info = response.get('result_info')
    if info is None:
        require(expected_total_pages is None and page == 1, code)
        return records
    require(isinstance(info, dict) and set(info) <= {'count', 'page', 'per_page', 'total_count', 'total_pages'}, code)
    require(not info or 'total_count' in info or 'total_pages' in info or (filtered and 'count' in info), code)
    require('count' not in info or (type(info['count']) is int and info['count'] == len(records)), code)
    require('total_count' not in info or (type(info['total_count']) is int and (
        info['total_count'] >= len(records) if filtered else info['total_count'] == len(records))), code)
    total_pages = info.get('total_pages')
    if 'total_pages' in info:
        max_pages = MAX_DNS_PAGES if filtered else 1
        require(type(total_pages) is int and 0 <= total_pages <= max_pages and (total_pages != 0 or not records), code)
        if expected_total_pages is not None:
            require(total_pages == expected_total_pages, code)
    elif expected_total_pages is not None:
        raise RolloutError(code)
    paginated = filtered and ((total_pages is not None and total_pages > 1) or expected_total_pages is not None)
    if paginated:
        require(type(info.get('page')) is int and info['page'] == page, code)
        require(type(info.get('count')) is int and info['count'] == len(records), code)
    elif 'page' in info:
        require(type(info['page']) is int and info['page'] in (0, 1), code)
    require('per_page' not in info or (type(info['per_page']) is int and (info['per_page'] >= len(records)
            or (info['per_page'] == 0 and (info.get('total_count') == len(records)
                or (filtered and info.get('count') == len(records) and 'total_pages' in info))))), code)
    if filtered and info and not paginated and 'total_pages' not in info:
        require(('per_page' in info and info['per_page'] > len(records))
                or ('total_count' in info and info['total_count'] == len(records)), code)
    return records


def check_worker_attachments(api, account, zone, worker, *, canonical=CANONICAL, alias=ALIAS):
    # This is Wrangler's account-scoped remote-configuration endpoint. A denied
    # read is fatal: enumerating only token-visible zones cannot prove coverage.
    routes = complete_account_inventory(api.request('GET',
        f'/accounts/{account}/workers/services/{worker}/environments/production/routes?show_zonename=true'),
        'incomplete_worker_route_inventory')
    expected_routes = _zone_routes(canonical, alias)
    patterns = set()
    for route in routes:
        require(isinstance(route, dict), 'worker_has_unrelated_routes')
        selected_zone = route.get('zone_id') == zone if 'zone_id' in route else route.get('zone_name') == canonical
        require(route.get('script') == worker and selected_zone
                and ('zone_name' not in route or route['zone_name'] == canonical)
                and route.get('pattern') in expected_routes
                and route['pattern'] not in patterns, 'worker_has_unrelated_routes')
        patterns.add(route['pattern'])
    domains = complete_account_inventory(api.request('GET', f'/accounts/{account}/workers/domains'),
                                         'incomplete_worker_domain_inventory')
    for domain in domains:
        require(isinstance(domain, dict) and isinstance(domain.get('service'), str) and domain['service']
                and isinstance(domain.get('hostname'), str) and domain['hostname'], 'invalid_worker_domain_inventory')
        require(domain['service'].casefold() != worker.casefold(), 'worker_has_custom_domains')
    subdomain = api.request('GET', f'/accounts/{account}/workers/scripts/{worker}/subdomain')['result']
    require(isinstance(subdomain, dict) and subdomain.get('enabled') is False
            and subdomain.get('previews_enabled') is False, 'unscoped_worker_urls_enabled')
    return patterns


def preflight(api, values, complete=False):
    account, zone, worker = (values[key] for key in ('account', 'zone', 'worker'))
    canonical, alias = values.get('canonical', CANONICAL), values.get('alias', ALIAS)
    found = api.request('GET', f'/zones/{zone}')['result']
    require(isinstance(found, dict) and found.get('name') == canonical and found.get('status') == 'active'
            and isinstance(found.get('account'), dict) and found['account'].get('id') == account, 'zone_account_mismatch')
    response = api.request('GET', f'/accounts/{account}/workers/scripts/{worker}/settings', missing=True)
    bindings = None if response is None else plain_bindings(response['result'])
    if bindings is not None:
        owner = bindings.get('ONIONSURGE_OWNER', {})
        require(owner.get('type') == 'plain_text' and owner.get('text') == OWNER, 'worker_ownership_conflict')
        require(set(bindings) <= {'ONIONSURGE_OWNER', 'ASSETS', 'PUBLIC_DATA_ORIGIN', 'ROOT_CACHE_GENERATION',
                                  'CANONICAL_HOST', 'REDIRECT_HOST', 'ONIONSURGE_DATA', 'R2_PREFIX'},
                'unexpected_worker_bindings')
        account_patterns = check_worker_attachments(api, account, zone, worker, canonical=canonical, alias=alias)
    elif complete:
        raise RolloutError('worker_missing_after_deploy')
    zone_routes = complete_account_inventory(api.request('GET', f'/zones/{zone}/workers/routes'), 'invalid_routes_response')
    check_routes(zone_routes, worker, complete, canonical=canonical, alias=alias)
    if bindings is not None:
        require(account_patterns == {route['pattern'] for route in zone_routes if route.get('script') == worker},
                'worker_route_inventories_disagree')
    apex = dns_records(api, zone, canonical)
    require(any(record.get('type') in {'A', 'AAAA', 'CNAME'} and record.get('proxied') is True for record in apex), 'canonical_dns_not_proxied')
    alias_ok = check_alias(dns_records(api, zone, alias), canonical=canonical)
    if values['r2_enabled']:
        bucket = api.request('GET', f"/accounts/{account}/r2/buckets/{values['r2_bucket']}")['result']
        require(isinstance(bucket, dict) and bucket.get('name') == values['r2_bucket'], 'r2_bucket_mismatch')
    if complete:
        assets_b, origin_b, gen_b, canonical_b, redirect_b = (
            bindings.get(k, {})
            for k in ('ASSETS', 'PUBLIC_DATA_ORIGIN', 'ROOT_CACHE_GENERATION', 'CANONICAL_HOST', 'REDIRECT_HOST')
        )
        require(assets_b.get('type') == 'assets'
                and origin_b.get('type') == 'plain_text' and origin_b.get('text') == values['origin']
                and gen_b.get('type') == 'plain_text' and gen_b.get('text') == values['generation']
                and canonical_b.get('type') == 'plain_text' and canonical_b.get('text') == canonical
                and redirect_b.get('type') == 'plain_text' and redirect_b.get('text') == alias,
                'worker_binding_mismatch')
        if values['r2_enabled']:
            r2_data, r2_prefix = bindings.get('ONIONSURGE_DATA', {}), bindings.get('R2_PREFIX', {})
            require(r2_data.get('type') == 'r2_bucket' and r2_data.get('bucket_name') == values['r2_bucket']
                    and r2_prefix.get('type') == 'plain_text' and r2_prefix.get('text') == values['r2_prefix'],
                    'worker_binding_mismatch')
        else:
            require('ONIONSURGE_DATA' not in bindings and 'R2_PREFIX' not in bindings, 'worker_binding_mismatch')
        deployments = api.request('GET', f'/accounts/{account}/workers/scripts/{worker}/deployments')['result']
        history = deployments.get('deployments') if isinstance(deployments, dict) else None
        require(isinstance(history, list) and history and isinstance(history[0], dict), 'worker_rollout_not_complete')
        versions = history[0].get('versions')
        require(isinstance(versions, list) and len(versions) == 1 and isinstance(versions[0], dict)
                and versions[0].get('percentage') == 100, 'worker_rollout_not_complete')
    return bindings, alias_ok


def wrangler_config(values, public, deploy_root):
    canonical, alias = values.get('canonical', CANONICAL), values.get('alias', ALIAS)
    routes = values.get('routes', _zone_routes(canonical, alias))
    result = {'name': values['worker'], 'account_id': values['account'], 'main': str(deploy_root / 'edge/worker.mjs'),
              'compatibility_date': COMPATIBILITY_DATE, 'workers_dev': False, 'preview_urls': False,
              'routes': [{'pattern': pattern, 'zone_id': values['zone']} for pattern in routes],
              'assets': {'directory': str(public), 'binding': 'ASSETS', 'run_worker_first': True,
                         'html_handling': 'none', 'not_found_handling': 'none'},
              'vars': {'ONIONSURGE_OWNER': OWNER, 'PUBLIC_DATA_ORIGIN': values['origin'],
                       'ROOT_CACHE_GENERATION': values['generation'],
                       'CANONICAL_HOST': canonical, 'REDIRECT_HOST': alias}}
    if values['r2_enabled']:
        result['r2_buckets'] = [{'binding': 'ONIONSURGE_DATA', 'bucket_name': values['r2_bucket']}]
        result['vars']['R2_PREFIX'] = values['r2_prefix']
    return result


def toolchain(deploy_root, run=subprocess.run):
    for package, expected in [('node', NODE_VERSION), ('wrangler', WRANGLER_VERSION)]:
        metadata = json.loads(read_regular(deploy_root / 'node_modules' / package / 'package.json', 65536))
        require(isinstance(metadata, dict) and metadata.get('version') == expected, 'pinned_toolchain_required')
    node = deploy_root / 'node_modules/node/bin/node'
    wrangler = deploy_root / 'node_modules/wrangler/bin/wrangler.js'
    require(node.is_file() and wrangler.is_file(), 'pinned_toolchain_required')
    version = run([str(node), '--version'], capture_output=True, text=True, timeout=10, env=child_environment())
    require(version.returncode == 0 and version.stdout.strip() == 'v' + NODE_VERSION, 'pinned_node_required')
    return node, wrangler


def write_log(path, secrets, *outputs):
    lines = []
    for output in outputs:
        text = output.decode('utf-8', errors='replace') if isinstance(output, bytes) else str(output or '')
        for secret in secrets:
            if secret:
                text = text.replace(secret, '[redacted]')
        lines.append(text + '\n')
    with path.open('a', encoding='utf-8') as stream:
        stream.writelines(lines)


def run_wrangler(node, wrangler, config_path, work, values, dry_run, run=subprocess.run):
    env = child_environment()
    env.update({'PATH': str(node.parent) + os.pathsep + env.get('PATH', ''),
                'CLOUDFLARE_API_TOKEN': values['token'], 'CLOUDFLARE_ACCOUNT_ID': values['account'],
                'WRANGLER_SEND_METRICS': 'false', 'WRANGLER_LOG_SANITIZE': 'true',
                'WRANGLER_LOG_PATH': str(work / 'wrangler-logs'), 'CI': 'true'})
    arguments = [str(node), str(wrangler), 'deploy', '--config', str(config_path)]
    if dry_run:
        arguments += ['--dry-run', '--outdir', str(work / 'bundle')]
    log = work / 'tool-output.log'
    try:
        result = run(arguments, cwd=work, env=env, capture_output=True, text=True, timeout=COMMAND_SECONDS)
    except subprocess.TimeoutExpired as error:
        write_log(log, [values['token']], error.stdout, error.stderr)
        raise RolloutError('wrangler_timed_out') from None
    write_log(log, [values['token']], result.stdout, result.stderr)
    require(result.returncode == 0, 'wrangler_failed')


def _prune_cloudflare_dirs(state, keep_work, retain_runs):
    completed = []
    try:
        entries = list(state.iterdir())
    except OSError:
        return
    for entry in entries:
        if entry == keep_work or not entry.name.startswith('cloudflare-') or entry.is_symlink():
            continue
        try:
            info = entry.lstat()
            if not stat.S_ISDIR(info.st_mode) or info.st_uid != os.geteuid():
                continue
            done = entry / 'completed.json'
            if not os.path.lexists(done) or done.is_symlink():
                continue
            done_info = done.lstat()
            if not stat.S_ISREG(done_info.st_mode) or done_info.st_uid != os.geteuid():
                continue
        except OSError:
            continue
        completed.append((done_info.st_mtime_ns, info.st_mtime_ns, entry.name, entry))
    completed.sort(reverse=True)
    removed = False
    for _, _, _, entry in completed[max(0, retain_runs - 1):]:
        try:
            shutil.rmtree(entry)
            removed = True
        except OSError:
            pass
    if removed:
        try:
            sync_directory(state)
        except OSError:
            pass


def rollout(config, frontend_release, dry_run=False, *, deploy_root=ROOT, api=None, run=subprocess.run):
    deploy_root = Path(deploy_root).resolve(strict=True)
    values = settings(config, deploy_root)
    canonical, alias = values['canonical'], values['alias']
    public, receipt = verify_frontend(frontend_release, config['APPLICATION_COMMIT'])
    previous_umask = os.umask(0o077)
    try:
        with publication_lock(values['state']):
            work = Path(tempfile.mkdtemp(prefix='cloudflare-', dir=values['state']))
            log = work / 'tool-output.log'
            log.touch(mode=0o600)
            try:
                node, wrangler = toolchain(deploy_root, run)
                api = api or Cloudflare(values['token'])
                bindings, _ = preflight(api, values)
                changed = bindings is not None and worker_destination(bindings) != (values['origin'], values['r2_bucket'], values['r2_prefix'])
                if changed:
                    require(bindings.get('ROOT_CACHE_GENERATION', {}).get('text') != values['generation'], 'fresh_cache_generation_required')
                check_published_destination(config, values['state'], required=not dry_run or changed)
                config_path = work / 'wrangler.json'
                atomic_private(config_path, json_bytes(wrangler_config(values, public, deploy_root)))
                run_wrangler(node, wrangler, config_path, work, values, True, run)
                if dry_run:
                    return {'status': 'dry-run-complete'}
                verify_checkout(values['application'], 'onionsurge', config['APPLICATION_COMMIT'])
                verify_checkout(deploy_root, 'onionsurge-deploy', config['DEPLOYMENT_COMMIT'])
                current_public, current_receipt = verify_frontend(frontend_release, config['APPLICATION_COMMIT'])
                require(current_public == public and current_receipt == receipt, 'frontend_release_changed')
                preflight(api, values)
                run_wrangler(node, wrangler, config_path, work, values, False, run)
                preflight(api, values, complete=True)
                if not check_alias(dns_records(api, values['zone'], alias), canonical=canonical):
                    api.request('POST', f"/zones/{values['zone']}/dns_records",
                                {'type': 'CNAME', 'name': alias, 'content': canonical, 'proxied': True, 'ttl': 1})
                    require(check_alias(dns_records(api, values['zone'], alias), canonical=canonical), 'alias_not_ready')
                atomic_private(work / 'completed.json', json_bytes({'status': 'complete',
                    'application_commit': config['APPLICATION_COMMIT'], 'deployment_commit': config['DEPLOYMENT_COMMIT'],
                    'frontend_receipt': receipt, 'worker': values['worker'], 'account': values['account'], 'zone': values['zone']}))
                _prune_cloudflare_dirs(values['state'], work, values['retain_runs'])
                return {'status': 'deployed'}
            except Exception as error:
                code = str(error) if isinstance(error, RolloutError) else type(error).__name__
                try:
                    write_log(log, [values['token']], 'Rollout stopped: ' + code)
                except OSError:
                    pass
                raise
    finally:
        os.umask(previous_umask)


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--config', required=True)
    parser.add_argument('--frontend-release', required=True, type=Path)
    parser.add_argument('--dry-run', action='store_true')
    args = parser.parse_args(argv)
    try:
        config = load_config(args.config, required=('APPLICATION_DIR', 'APPLICATION_COMMIT', 'DEPLOYMENT_COMMIT', 'STATE_DIR'))
        result = rollout(config, args.frontend_release, args.dry_run)
    except (OSError, ValueError, TypeError, KeyError, subprocess.SubprocessError):
        parser.exit(1, 'Cloudflare rollout failed. Inspect the private rollout log.\n')
    print(json.dumps(result, sort_keys=True))


if __name__ == '__main__':
    main()
