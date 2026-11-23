"""Reproject an already sealed morning without acquisition or inference."""
import argparse
import hashlib
import json
from pathlib import Path
import socket
import sys

from daily_runtime_bootstrap_v1 import retained_module


def require(ok, reason):
    if not ok:
        raise RuntimeError(reason)


def project(state, day, source, output, run_id):
    implementation = retained_module(state, day)
    authority = implementation.restore_source(state, day, source)
    implementation.verify_runtime(state, day, source)
    for phase in ('MORNING', 'PREDEADLINE'):
        implementation.verify_locks(state, day, phase, exact=True)
    sys.path.insert(0, str(source.resolve()))
    from g11.relay import v1 as relay
    from optional_display_isolation_v1 import isolate_optional_display
    from rescue_night_result_parser_v1 import official_input_compatibility
    store = state / 'store'
    before = relay._tree_sha(store)
    now = relay._now_jst()
    require(now.date().isoformat() == day, 'SAVED_MORNING_CURRENT_DAY')
    metadata = relay._metadata(state, day)
    require(metadata.get('MORNING_COMPLETE') is True, 'SAVED_MORNING_NOT_COMPLETE')
    require(not any((store / 'snapshots/predeadline').glob('*.json'))
            and not any((store / 'snapshots/night').glob('*.json')),
            'SAVED_MORNING_LATER_PHASE_EXISTS')
    def forbidden(*args, **kwargs):
        raise RuntimeError('SAVED_MORNING_NETWORK_OR_PREDICTION_FORBIDDEN')
    old_connect = socket.socket.connect
    socket.socket.connect = forbidden
    old_prepare = relay.prepare_prediction_models
    relay.prepare_prediction_models = forbidden
    try:
        morning = relay._morning_projection(store)
        publication = relay.current_growth_p3.load_locked_publication(store, morning, now)
        require(publication.get('READ_ONLY') is True, 'SAVED_MORNING_READ_ONLY')
        with official_input_compatibility(), isolate_optional_display(relay):
            feed = relay.build_feed(store, run_id=int(run_id), github_sha=authority['runtime_source_sha'],
                                   current_p3_publication=publication, relay_phase='morning')
    finally:
        socket.socket.connect = old_connect
        relay.prepare_prediction_models = old_prepare
    require(feed.get('stage') == 'MORNING' and feed.get('status') == 'PASS'
            and feed.get('operational_date_jst') == day, 'SAVED_MORNING_FEED_IDENTITY')
    require(relay._tree_sha(store) == before, 'SAVED_MORNING_STORE_CHANGED')
    for phase in ('MORNING', 'PREDEADLINE'):
        implementation.verify_locks(state, day, phase, exact=True)
    feed['daily_runtime'] = implementation.feed_binding(authority, 'MORNING')
    output.parent.mkdir(parents=True, exist_ok=True)
    relay._write_json(output, feed)
    print(json.dumps({'status':'PASS','races':len(feed['races']),
        'authority_sha256':authority['authority_sha256'],
        'feed_sha256':hashlib.sha256(output.read_bytes()).hexdigest(),
        'prediction_recalculation':0,'official_http':0,'store_changed':False},sort_keys=True))
    return feed


if __name__ == '__main__':
    parser = argparse.ArgumentParser()
    parser.add_argument('--state-root', type=Path, required=True)
    parser.add_argument('--source-root', type=Path, required=True)
    parser.add_argument('--day', required=True)
    parser.add_argument('--output', type=Path, required=True)
    parser.add_argument('--run-id', required=True)
    args = parser.parse_args()
    project(args.state_root.resolve(), args.day, args.source_root.resolve(), args.output.resolve(), args.run_id)
