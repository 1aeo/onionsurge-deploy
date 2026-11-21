#!/usr/bin/env python3
"""Prepare and publish one complete immutable OnionSurge data release."""
import argparse
import json
import os
from pathlib import Path

from configuration import expand_path, load_config
from publication import (MAX_ROOT, PublicationError, migrate_destinations, prepare_candidate, providers_from_config,
                         publication_lock, publish_candidate, read_regular, reconcile_root, recover_promotion)


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--config', required=True)
    parser.add_argument('--build-dir', type=Path)
    parser.add_argument('--application-commit')
    parser.add_argument('--deployment-commit')
    action = parser.add_mutually_exclusive_group()
    action.add_argument('--prepare-only', action='store_true')
    action.add_argument('--recover', action='store_true')
    action.add_argument('--migrate-destination', action='store_true')
    action.add_argument('--reconcile-root', type=Path, metavar='RESTORED_ROOT_JSON')
    parser.add_argument('--confirm-writers-fenced', action='store_true')
    args = parser.parse_args(argv)
    reconciling = bool(args.recover or args.migrate_destination or args.reconcile_root)
    if not reconciling and not all([args.build_dir, args.application_commit, args.deployment_commit]):
        parser.error('publication requires --build-dir, --application-commit and --deployment-commit')
    if args.confirm_writers_fenced and not reconciling:
        parser.error('--confirm-writers-fenced requires a reconciliation action')
    try:
        previous_umask = os.umask(0o077)
        try:
            config = load_config(args.config, required=('STATE_DIR',))
            state = expand_path(config['STATE_DIR'])
            with publication_lock(state):
                if args.recover:
                    result = recover_promotion(state, providers_from_config(config), args.confirm_writers_fenced)
                elif args.migrate_destination:
                    result = migrate_destinations(state, providers_from_config(config), args.confirm_writers_fenced)
                elif args.reconcile_root:
                    result = reconcile_root(state, providers_from_config(config), read_regular(args.reconcile_root, MAX_ROOT),
                                            args.confirm_writers_fenced)
                else:
                    if not args.prepare_only and os.path.lexists(state / 'pending-promotion.json'):
                        raise PublicationError('promotion_blocked_pending_recovery')
                    candidate = prepare_candidate(args.build_dir, state, args.application_commit, args.deployment_commit)
                    result = {'status': 'prepared', **candidate.summary()} if args.prepare_only else publish_candidate(
                        candidate, state, providers_from_config(config))
        finally:
            os.umask(previous_umask)
    except PublicationError as error:
        parser.exit(1, 'Publication failed: ' + str(error) + '\n')
    except (OSError, ValueError, TypeError, KeyError, RecursionError):
        parser.exit(1, 'Publication failed: invalid input or local I/O failure\n')
    print(json.dumps(result, sort_keys=True))


if __name__ == '__main__':
    main()
