#!/usr/bin/env python3
"""Run one pinned collection, live build and immutable publication cycle."""
import argparse
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import re
import selectors
import shutil
import stat
import subprocess
import sys
import time

from configuration import enabled, expand_path, load_config, read_regular_bytes
from operations import (RUN_ID, atomic_private, child_environment, ensure_private_directory, json_bytes,
                        parse_state_retain_runs, paths_disjoint, private_lock_file, private_log as _private_log,
                        rotate_private_log, runner_health, stamped_id, strict_json_bytes, sync_directory,
                        verify_checkout)
import publication

ROOT = Path(__file__).resolve().parents[1]
HOUR = 3600
DAY = 86400
MAX_AUTO_CATCHUP_DAYS = 31
PLAN_TIMEOUT_SECONDS = 60
MAX_CHILD_STDOUT = 65536
REFERENCE_CACHE_RETAIN_DAYS = 3
_DATE_DIR_RE = re.compile(r'(\d{4})-(\d{2})-(\d{2})\Z')


class HourlyError(ValueError):
    pass


class Busy(HourlyError):
    pass


def require(condition, message):
    if not condition:
        raise HourlyError(message)


def _parse_json_output(text, error_message):
    try:
        return strict_json_bytes(text)
    except ValueError as error:
        raise HourlyError(error_message) from error


def _run_bounded_stdout(command, *, timeout, cwd, error_message):
    with subprocess.Popen(
        command, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
        env=child_environment(), cwd=cwd,
    ) as process:
        output = bytearray()
        end = time.monotonic() + timeout
        stdout_fd = process.stdout.fileno()
        try:
            with selectors.DefaultSelector() as ready:
                ready.register(process.stdout, selectors.EVENT_READ)
                while True:
                    remaining = end - time.monotonic()
                    if remaining <= 0 or not ready.select(remaining):
                        raise subprocess.TimeoutExpired(command, timeout)
                    chunk = os.read(stdout_fd, MAX_CHILD_STDOUT + 1 - len(output))
                    if not chunk:
                        break
                    output.extend(chunk)
                    require(len(output) <= MAX_CHILD_STDOUT, error_message)
            remaining = end - time.monotonic()
            if remaining <= 0:
                raise subprocess.TimeoutExpired(command, timeout)
            code = process.wait(timeout=remaining)
        except BaseException:
            process.kill()
            process.wait()
            raise
    require(code == 0, error_message)
    return output.decode('utf-8', errors='replace')


def private_directory(path):
    ensure_private_directory(path, HourlyError, 'Private runtime directory ownership or permissions are invalid')


def private_log(path):
    return _private_log(path, error_type=HourlyError)


def private_lock(state, name):
    return private_lock_file(
        state, name, busy_error=Busy, error_type=HourlyError,
        messages=('Private runtime directory ownership or permissions are invalid',
                  'Invalid private lock file', 'Another operation holds this lock'),
    )


_STATE_SUBPATH_NAMES = (
    'runs', 'reference-cache', 'candidates',
    'published.json', 'pending-promotion.json', 'last-run.json', 'last-success.json',
    'run.lock', 'publication.lock', 'schedule.lock', 'crontab-before.txt', 'collector-checkpoint.json',
)


def _state_subpaths(state_resolved):
    return tuple(state_resolved / name for name in _STATE_SUBPATH_NAMES)


def _resolve_time_ranges_config(config, application, deploy_resolved, state_resolved, logs_resolved, source_config):
    raw = config.get('TIME_RANGES_CONFIG', '')
    require(isinstance(raw, str), 'Invalid TIME_RANGES_CONFIG')
    if not raw.strip():
        return None
    try:
        tr_path = expand_path(raw)
    except ValueError as error:
        raise HourlyError('Invalid TIME_RANGES_CONFIG') from error
    read_regular_bytes(tr_path, 65536, error_type=HourlyError)
    require(not (tr_path.lstat().st_mode & 0o022), 'TIME_RANGES_CONFIG must not be group- or world-writable')
    tr_config = tr_path.resolve(strict=True)
    paths_disjoint(
        (tr_config,), (application, deploy_resolved), HourlyError,
        'Runtime directories and source configuration must be outside and separate from source checkouts',
    )
    paths_disjoint(
        (tr_config,), (logs_resolved, state_resolved / 'collector', *_state_subpaths(state_resolved), source_config),
        HourlyError,
        'TIME_RANGES_CONFIG cannot overlap logs, SOURCE_CONFIG, or operational state paths',
    )
    return tr_config


def runtime_paths(config, deploy_dir=ROOT):
    application = expand_path(config['APPLICATION_DIR']).resolve(strict=True)
    state = expand_path(config['STATE_DIR'])
    logs = expand_path(config.get('LOG_DIR', str(state / 'logs')))
    source_raw = config.get('SOURCE_CONFIG')
    require(isinstance(source_raw, str) and bool(source_raw.strip()), 'Missing SOURCE_CONFIG')
    source_path = expand_path(source_raw)
    read_regular_bytes(source_path, 65536, private=True, error_type=HourlyError)
    source_config = source_path.resolve(strict=True)
    deploy_resolved = Path(deploy_dir).resolve()
    paths_disjoint(
        (state, logs, source_config), (application, deploy_resolved), HourlyError,
        'Runtime directories and source configuration must be outside and separate from source checkouts',
    )
    state_resolved = state.resolve()
    logs_resolved = logs.resolve()
    state_subpaths = _state_subpaths(state_resolved)
    paths_disjoint(
        (source_config,), (logs_resolved, state_resolved / 'collector', *state_subpaths), HourlyError,
        'SOURCE_CONFIG cannot overlap logs or operational state paths',
    )
    tr_config = _resolve_time_ranges_config(
        config, application, deploy_resolved, state_resolved, logs_resolved, source_config,
    )
    forbid_overlap = [application, deploy_resolved, logs_resolved, *state_subpaths]
    if tr_config is not None:
        forbid_overlap.append(tr_config)
    if enabled(config, 'LOCAL_ENABLED'):
        site_raw = config.get('LOCAL_SITE_DIR', '')
        require(isinstance(site_raw, str) and bool(site_raw.strip()), 'Missing LOCAL_SITE_DIR')
        local_site = expand_path(site_raw)
        require(not local_site.is_symlink(), 'LOCAL_SITE_DIR cannot be a symlink')
        site_resolved = local_site.resolve()
        paths_disjoint(
            (site_resolved,),
            (application, deploy_resolved, state_resolved, logs_resolved, source_config,
             *(() if tr_config is None else (tr_config,))),
            HourlyError,
            'LOCAL_SITE_DIR must be outside and separate from checkouts and private state',
        )
        forbid_overlap.append(site_resolved)
    return application, state, logs, source_config, tuple(forbid_overlap)


def _overlap_flags(forbid_overlap):
    return [arg for path in forbid_overlap for arg in ('--forbid-overlap', str(path))]


def _with_run_lock(config, deploy_dir, callback):
    previous_umask = os.umask(0o077)
    try:
        application, state, logs, source_config, forbid_overlap = runtime_paths(config, deploy_dir)
        with private_lock(state, 'run.lock'):
            return callback(application, state, logs, Path(deploy_dir), source_config, forbid_overlap)
    except Busy:
        return {'status': 'busy'}
    finally:
        os.umask(previous_umask)


def run(config, end=None, deploy_dir=ROOT):
    """Run one locked hourly collection, live build and publication cycle."""
    return _with_run_lock(
        config, deploy_dir,
        lambda app, state, logs, deploy, src, overlap: _run_locked(config, app, state, logs, end, deploy, src, overlap),
    )


def record_checkpoint(config, end=None, deploy_dir=ROOT):
    """Record collector checkpoint via collect.py after a verified manual catch-up publication."""
    return _with_run_lock(
        config, deploy_dir,
        lambda app, state, _logs, deploy, src, overlap: _record_checkpoint_locked(
            config, app, state, end, deploy, src, overlap),
    )


def published_result(published, receipt_warning=False):
    warning = published.get('warning')
    result = {'status': 'published_receipt_warning' if receipt_warning or warning else 'published',
              'release_id': published['release_id']}
    if isinstance(warning, str) and warning in publication.PROMOTION_WARNINGS:
        result['warning'] = warning
    return result


def _verify_runner_state(config, application, state, deploy_dir):
    """Verify pinned checkouts and confirm no pending promotion blocks updates."""
    commits = {
        'application_commit': verify_checkout(application, 'onionsurge', config['APPLICATION_COMMIT']),
        'deployment_commit': verify_checkout(deploy_dir, 'onionsurge-deploy', config['DEPLOYMENT_COMMIT']),
    }
    require(not os.path.lexists(state / 'pending-promotion.json'), 'Pending publication requires operator recovery')
    return commits


def _read_published_manifest(config, state):
    """Return `(current_descriptor, manifest)` for `state/published.json`, or `(None, None)`."""
    published_receipt = state / 'published.json'
    if not os.path.lexists(published_receipt):
        return None, None
    previous = publication.read_receipt(published_receipt)
    return previous['root']['current'], publication.previous_manifest(
        previous, state, lambda: publication.providers_from_config(config)[0])


def _record_checkpoint_locked(config, application, state, end, deploy_dir, source_config, forbid_overlap):
    _verify_runner_state(config, application, state, deploy_dir)
    _, prev_manifest = _read_published_manifest(config, state)
    require(prev_manifest is not None and prev_manifest['data_schema_version'] == 7
            and publication.timestamp(prev_manifest.get('observation_end'))
            and prev_manifest['observation_end'] % HOUR == 0,
            'Published schema-7 release is required to record checkpoint')
    now_hour = (int(time.time()) // HOUR) * HOUR
    obs_end = prev_manifest['observation_end'] if end is None else end
    require(type(obs_end) is int and obs_end == prev_manifest['observation_end']
            and obs_end % HOUR == 0 and obs_end <= now_hour, 'Invalid collection end')
    stdout = _run_bounded_stdout(
        [sys.executable, '-B', str(application / 'scripts/collect.py'),
         '--record-checkpoint',
         '--config', str(source_config),
         '--application-dir', str(application),
         '--state-dir', str(state / 'collector'),
         '--end', str(obs_end),
         *_overlap_flags(forbid_overlap)],
        timeout=PLAN_TIMEOUT_SECONDS, cwd=deploy_dir, error_message='Checkpoint recording failed',
    )
    summary = _parse_json_output(stdout, 'Checkpoint recording failed')
    require(isinstance(summary, dict) and summary.get('status') == 'checkpoint_recorded'
            and summary.get('observation_end') == obs_end, 'Checkpoint recording failed')
    return {'status': 'checkpoint_recorded', 'observation_end': obs_end}


def _plan_collection(application, source_config, collector_dir, target_end, prev_end, deploy_dir, forbid_overlap):
    command = [
        sys.executable, '-B', str(application / 'scripts/collect.py'),
        '--plan',
        '--config', str(source_config),
        '--application-dir', str(application),
        '--state-dir', str(collector_dir),
        '--end', str(target_end),
        '--max-catchup-days', str(MAX_AUTO_CATCHUP_DAYS),
    ]
    if prev_end is not None:
        command.extend(['--previous-end', str(prev_end)])
    command.extend(_overlap_flags(forbid_overlap))
    stdout = _run_bounded_stdout(
        command, timeout=PLAN_TIMEOUT_SECONDS, cwd=deploy_dir, error_message='Collection plan failed',
    )
    plan = _parse_json_output(stdout, 'Collection plan failed')
    require(isinstance(plan, dict)
            and {'snapshot_end', 'catchup_days', 'recommended_timeout_seconds'} <= set(plan)
            and type(plan['snapshot_end']) is int and plan['snapshot_end'] == target_end
            and type(plan['catchup_days']) is int and 1 <= plan['catchup_days'] <= MAX_AUTO_CATCHUP_DAYS
            and type(plan['recommended_timeout_seconds']) is int
            and 1 <= plan['recommended_timeout_seconds'] <= 360000,
            'Collection plan failed')
    return plan


def _is_completed_run_dir(entry):
    if not RUN_ID.fullmatch(entry.name) or entry.is_symlink():
        return False
    info = entry.lstat()
    if not stat.S_ISDIR(info.st_mode) or info.st_uid != os.geteuid() or (info.st_mode & 0o077):
        return False
    done = entry / 'completed.json'
    if not os.path.lexists(done) or done.is_symlink():
        return False
    dinfo = done.lstat()
    return stat.S_ISREG(dinfo.st_mode) and dinfo.st_uid == os.geteuid()


def _prune_completed_runs(runs_dir, active_run_id, retain_runs):
    if not os.path.lexists(runs_dir):
        return
    rinfo = runs_dir.lstat()
    require(stat.S_ISDIR(rinfo.st_mode) and rinfo.st_uid == os.geteuid() and not (rinfo.st_mode & 0o077),
            'Private runtime directory ownership or permissions are invalid')
    retained_keep = 0
    extras = []
    for entry in runs_dir.iterdir():
        if not _is_completed_run_dir(entry):
            continue
        info = entry.lstat()
        if entry.name == active_run_id:
            retained_keep += 1
        else:
            extras.append((entry.name[:16], info.st_mtime_ns, entry.name, entry))
    extras.sort(reverse=True)
    extra_keep = max(0, retain_runs - retained_keep)
    removed = False
    for _, _, _, entry in extras[extra_keep:]:
        shutil.rmtree(entry)
        removed = True
    if removed:
        sync_directory(runs_dir)


def _prune_reference_cache(ref_cache_dir, observation_end):
    if not os.path.lexists(ref_cache_dir):
        return
    info = ref_cache_dir.lstat()
    if not stat.S_ISDIR(info.st_mode) or info.st_uid != os.geteuid():
        return
    cutoff_day = (observation_end // DAY) * DAY - REFERENCE_CACHE_RETAIN_DAYS * DAY
    removed = False
    for entry in ref_cache_dir.iterdir():
        if entry.is_symlink() or not _DATE_DIR_RE.fullmatch(entry.name):
            continue
        try:
            day_ts = int(datetime.strptime(entry.name, '%Y-%m-%d').replace(tzinfo=timezone.utc).timestamp())
        except ValueError:
            continue
        einfo = entry.lstat()
        if not stat.S_ISDIR(einfo.st_mode) or einfo.st_uid != os.geteuid():
            continue
        if day_ts < cutoff_day:
            shutil.rmtree(entry)
            removed = True
    if removed:
        sync_directory(ref_cache_dir)


def _prune_retained_state(state, active_run_id, observation_end, retain_runs):
    with publication.publication_lock(state):
        if os.path.lexists(state / 'pending-promotion.json'):
            return
        publication.prune_candidates(state, retain_runs)
        _prune_completed_runs(state / 'runs', active_run_id, retain_runs)
        _prune_reference_cache(state / 'reference-cache', observation_end)


def _run_locked(config, application, state, logs, end, deploy_dir, source_config, forbid_overlap):
    stage = 'checkout'
    receipt = {'status': 'running', 'started_at': int(time.time())}
    published = None
    retain_runs = parse_state_retain_runs(config, error_type=HourlyError)
    try:
        rotate_private_log(logs / 'hourly.log', error_type=HourlyError)
        rotate_private_log(logs / 'cron.log', error_type=HourlyError)
        with private_log(logs / 'hourly.log') as log:
            try:
                commits = _verify_runner_state(config, application, state, deploy_dir)
                receipt.update(commits)
                stage = 'check_current'
                now_hour = (int(time.time()) // HOUR) * HOUR
                target_end = now_hour if end is None else end
                require(type(target_end) is int and publication.timestamp(target_end)
                        and target_end % HOUR == 0 and target_end <= now_hour,
                        'Invalid collection end')
                prev_desc, prev_manifest = _read_published_manifest(config, state)
                prev_end = None
                if prev_manifest is not None:
                    if prev_manifest['data_schema_version'] == 7 and prev_manifest['observation_end'] == target_end:
                        receipt.update(status='up_to_date', finished_at=int(time.time()),
                                       release_id=prev_desc['id'], observation_end=target_end)
                        atomic_private(state / 'last-run.json', json_bytes(receipt))
                        log.write(json.dumps(receipt, sort_keys=True) + '\n')
                        return {'status': 'up_to_date', 'release_id': prev_desc['id'], 'observation_end': target_end}
                    prev_end = prev_manifest['observation_end']
                    require(target_end >= prev_end, 'Invalid collection end')
                collector_dir = state / 'collector'
                stage = 'plan'
                plan = _plan_collection(
                    application, source_config, collector_dir, target_end, prev_end, deploy_dir, forbid_overlap,
                )
                runs = state / 'runs'
                private_directory(runs)
                run_id = stamped_id()
                directory = runs / run_id
                directory.mkdir(mode=0o700)
                receipt['run_id'] = run_id
                atomic_private(state / 'last-run.json', json_bytes(receipt))
                stage = 'collect'
                log.write(json.dumps({'run_id': run_id, 'stage': stage, 'at': int(time.time())}) + '\n')
                log.flush()
                stdout = _run_bounded_stdout(
                    [sys.executable, '-B', str(application / 'scripts/collect.py'),
                     '--config', str(source_config), '--state-dir', str(collector_dir),
                     '--output', str(directory / 'input.json'), '--end', str(target_end),
                     *_overlap_flags(forbid_overlap)],
                    timeout=plan['recommended_timeout_seconds'], cwd=deploy_dir, error_message='Collection failed',
                )
                collection = _parse_json_output(stdout, 'Invalid collection summary')
                require(isinstance(collection, dict)
                        and {'snapshot_end'} <= set(collection) <= {
                            'snapshot_end', 'charts', 'series', 'source_requests',
                            'cache_committed', 'checkpoint_updated'}
                        and all((type(value) is bool if key in {'cache_committed', 'checkpoint_updated'}
                                 else (type(value) is int and value >= 0))
                                for key, value in collection.items())
                        and collection['snapshot_end'] == target_end,
                        'Invalid collection summary')
                stage = 'build'
                log.write(json.dumps({'run_id': run_id, 'stage': stage, 'at': int(time.time())}) + '\n')
                log.flush()
                tr_config = _resolve_time_ranges_config(
                    config, application, Path(deploy_dir).resolve(), state.resolve(), logs.resolve(), source_config,
                )
                build_cmd = [
                    sys.executable, '-B', str(application / 'scripts/build.py'), '--live',
                    '--input', str(directory / 'input.json'), '--output', str(directory / 'build'),
                    '--cache', str(state / 'reference-cache'),
                    *(('--time-ranges', str(tr_config)) if tr_config is not None else ()),
                ]
                built = subprocess.run(build_cmd, stdout=log, stderr=log, timeout=300,
                                       env=child_environment(), cwd=deploy_dir)
                require(built.returncode == 0, 'Live build failed')
                stage = 'recheck'
                require(verify_checkout(application, 'onionsurge', config['APPLICATION_COMMIT']) == commits['application_commit']
                        and verify_checkout(deploy_dir, 'onionsurge-deploy', config['DEPLOYMENT_COMMIT']) == commits['deployment_commit'],
                        'Source checkout changed during the run')
                stage = 'publish'
                with publication.publication_lock(state):
                    candidate = publication.prepare_candidate(directory / 'build', state, **commits)
                    published = publication.publish_candidate(candidate, state, publication.providers_from_config(config))
                result = published_result(published)
                receipt.update(status=result['status'], finished_at=int(time.time()),
                               release_id=published['release_id'], observation_end=collection['snapshot_end'])
                if 'warning' in result:
                    receipt['publication_warning'] = result['warning']
                stage = 'receipt'
                raw_receipt = json_bytes(receipt)
                atomic_private(state / 'last-run.json', raw_receipt)
                atomic_private(state / 'last-success.json', raw_receipt)
                atomic_private(directory / 'completed.json', json_bytes({
                    'run_id': run_id,
                    'release_id': published['release_id'],
                    'observation_end': collection['snapshot_end'],
                }))
                log.write(json.dumps({'run_id': run_id, **result}) + '\n')
                if not os.path.lexists(state / 'pending-promotion.json') and 'warning' not in result:
                    try:
                        _prune_retained_state(state, run_id, collection['snapshot_end'], retain_runs)
                    except (OSError, HourlyError, publication.PublicationError) as error:
                        log.write(json.dumps({'run_id': run_id, 'prune_warning': type(error).__name__}) + '\n')
                return result
            except Exception as error:
                receipt.update(status='published_receipt_warning' if published else 'failed',
                               stage=stage, finished_at=int(time.time()), error_type=type(error).__name__)
                try:
                    atomic_private(state / 'last-run.json', json_bytes(receipt))
                    log.write(json.dumps(receipt, sort_keys=True) + '\n')
                    log.flush()
                except (OSError, ValueError):
                    pass
                if published:
                    return published_result(published, receipt_warning=True)
                raise HourlyError('Hourly cycle failed; inspect private state') from error
    except Exception:
        # Closing a buffered log can fail after the successful return above.
        # That cannot undo an already-confirmed provider-root promotion.
        if published:
            return published_result(published, receipt_warning=True)
        raise


def health(config):
    return runner_health(config, error_type=HourlyError)


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--config', required=True)
    parser.add_argument('--end', type=int)
    parser.add_argument('--record-checkpoint', action='store_true')
    args = parser.parse_args(argv)
    try:
        config = load_config(args.config, required=('APPLICATION_DIR', 'STATE_DIR', 'APPLICATION_COMMIT',
                                                   'DEPLOYMENT_COMMIT', 'SOURCE_CONFIG'))
        result = record_checkpoint(config, args.end, ROOT) if args.record_checkpoint else run(config, args.end, ROOT)
    except (OSError, ValueError, KeyError, TypeError, subprocess.SubprocessError):
        parser.exit(1, 'Hourly update failed; inspect private state before retrying.\n')
    print(json.dumps(result, sort_keys=True))


if __name__ == '__main__':
    main()
