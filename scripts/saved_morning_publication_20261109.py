"""Publish the reviewed, already saved November 9 MORNING without inference.

The interrupted relay saved all morning snapshots and CURRENT publication but
failed in optional display projection before saving MORNING_COMPLETE. Do not
re-enter morning acquisition or prediction to repair that delivery interruption.
"""
import hashlib
import json
import os
from pathlib import Path

DAY = '2026-11-09'
SOURCE = '5e728bb8ab2cdd608fbd4aeddb62c288dd1efa71'


def require(ok, reason):
    if not ok:
        raise ValueError(reason)


def inventory(root):
    return {str(p.relative_to(root)): hashlib.sha256(p.read_bytes()).hexdigest()
            for p in root.rglob('*') if p.is_file()}


def run_if_saved(argv):
    from g11.relay import v1 as relay
    from g11.relay import daily_runtime_authority_v1 as daily
    from optional_display_isolation_v1 import isolate_optional_display
    args = relay._parser().parse_args(argv)
    if args.phase != 'morning' or args.operational_date != DAY:
        return False
    state = args.state_root.resolve()
    store = state / 'store'
    metadata = relay._metadata(state, DAY)
    if metadata.get('MORNING_COMPLETE') is True:
        return False
    projection_path = store / 'snapshots/morning/workbook-projection.json'
    if not projection_path.exists():
        return False
    now = relay._now_jst()
    require(now.date().isoformat() == DAY and now.hour < 8, 'SAVED_MORNING_CURRENT_WINDOW')
    require(args.public_repository == relay.ALLOWED_PUBLIC_REPOSITORY
            and args.runtime_ref == 'g11-runtime'
            and args.app_origin == 'https://g11-race-lab.higatatsunori2.chatgpt.site'
            and args.oidc_audience == 'g11-race-lab', 'SAVED_MORNING_RUNTIME_TARGET')
    authority = daily.verify_runtime(state, DAY, Path(relay.__file__).resolve().parents[2])
    require(authority['runtime_source_sha'] == SOURCE
            and os.environ.get('G11_PRIVATE_SOURCE_SHA') == SOURCE,
            'SAVED_MORNING_REVIEWED_SOURCE')
    require(not any((store / 'snapshots/predeadline').glob('*.json'))
            and not any((store / 'snapshots/night').glob('*.json')),
            'SAVED_MORNING_NO_LATER_PHASE')
    before = inventory(store)
    artifact = relay.OfficialArtifactStore(store)
    for path in sorted((store / 'snapshots/morning').glob('*.json')):
        artifact.load_verified_snapshot('morning', path.stem)
    projection = artifact.load_verified_snapshot('morning', 'workbook-projection')['SNAPSHOT']['PAYLOAD']
    require(projection.get('OPERATIONAL_DATE_JST') == DAY
            and bool(projection.get('LOCK49_ROWS')), 'SAVED_MORNING_PROJECTION_DAY')
    publication = relay.current_growth_p3.load_locked_publication(store, projection, now)
    require(publication.get('READ_ONLY') is True, 'SAVED_MORNING_READ_ONLY_PUBLICATION')
    with isolate_optional_display(relay):
        feed = relay.build_feed(store, run_id=int(args.public_run_id), github_sha=SOURCE,
                                current_p3_publication=publication, relay_phase='morning')
    require(feed.get('status') == 'PASS' and feed.get('stage') == 'MORNING'
            and feed.get('operational_date_jst') == DAY, 'SAVED_MORNING_FEED_IDENTITY')
    binding = relay.night_handoff.last_race_binding(store, now.date())
    require(inventory(store) == before, 'SAVED_MORNING_READ_ONLY_CHANGED')
    # These are the original relay's post-build seals, not a new authority.
    daily.seal(state, DAY, 'MORNING')
    daily.seal(state, DAY, 'PREDEADLINE', executed=False)
    daily.verify_locks(state, DAY, 'MORNING', exact=True)
    daily.verify_locks(state, DAY, 'PREDEADLINE', exact=True)
    feed['daily_runtime'] = daily.feed_binding(authority, 'MORNING')
    args.feed_output.parent.mkdir(parents=True, exist_ok=True)
    relay._write_json(args.feed_output, feed)
    feed_sha = relay._sha(args.feed_output)
    metadata.update(MORNING_COMPLETE=True, LAST_PHASE='MORNING',
        LAST_RACE_BINDING=binding, NIGHT_STATE='PRE_RACE_ACTIVE',
        LAST_STORE_SHA256=relay._tree_sha(store), LAST_PUBLISHED_FEED_SHA256=feed_sha,
        UPDATED_AT_JST=now.replace(microsecond=0).isoformat())
    relay._save_metadata(state, metadata)
    print(json.dumps({'STATUS': 'RELAY_PHASE_COMPLETE', 'PHASE': 'MORNING',
        'OPERATIONAL_DATE_JST': DAY, 'STATE_CHANGED': True,
        'FEED_EMITTED': True, 'FEED_SHA256': feed_sha, 'PREDICTION_RECALCULATION': 0,
        'SAVED_MORNING_REPLAY': True, 'AUTHORITY_ISSUED': 0, 'AUTHORITY_CONSUMED': 0,
        'PURCHASE_EXECUTED': 0}, sort_keys=True))
    return True
