"""Offline replay on an isolated copy of the actual failed-run checkpoint."""
import argparse
import contextlib
from datetime import datetime
import io
import json
import os
from pathlib import Path
from unittest.mock import patch

import saved_morning_publication_20261109 as repair
from g11.relay import v1 as relay
from g11.relay import daily_runtime_authority_v1 as daily
from g11.canonical import official_pipeline as official
from rescue_night_result_parser_v1 import official_input_compatibility


def audit(state, output):
    day = repair.DAY
    store = state / 'store'
    authority = daily.verify_runtime(state, day, Path(relay.__file__).resolve().parents[2])
    before = repair.inventory(store)
    original_authority = (state / 'daily-runtime' / day / 'authority.json').read_bytes()
    projection = relay._morning_projection(store)
    keys = {row[relay.feedlib.LOCK49_FIELDS.index('標準レースキー')]
            for row in projection['LOCK49_ROWS']}
    try:
        relay._display_research(store, keys)
    except ValueError as error:
        assert str(error) == 'DISPLAY_RACE_PARTITION', str(error)
    else:
        raise AssertionError('INCIDENT_NOT_REPRODUCED')
    argv = ['--phase', 'morning', '--operational-date', day,
        '--state-root', str(state), '--feed-output', str(output),
        '--public-repository', 'tatsuuut/-g11-official-data-relay',
        '--public-run-id', os.environ['GITHUB_RUN_ID'], '--runtime-ref', 'g11-runtime',
        '--app-origin', 'https://g11-race-lab.higatatsunori2.chatgpt.site',
        '--oidc-audience', 'g11-race-lab']
    now = datetime.fromisoformat(day + 'T04:00:00+09:00')
    with patch.dict(os.environ, {'G11_PRIVATE_SOURCE_SHA': repair.SOURCE}), \
         patch.object(relay, '_now_jst', return_value=now), \
         patch.object(relay, '_run_morning', side_effect=AssertionError('NO_REGENERATION')) as generate, \
         patch.object(official.OfficialHttpClient, 'fetch_once', side_effect=AssertionError('NO_ACQUISITION')) as acquire, \
         patch.object(relay.current_growth_p3, 'publish_future', side_effect=AssertionError('NO_NEW_P3_LOCK')) as predict, \
         official_input_compatibility(), contextlib.redirect_stdout(io.StringIO()):
        assert repair.run_if_saved(argv)
        generate.assert_not_called()
        acquire.assert_not_called()
        predict.assert_not_called()
    assert repair.inventory(store) == before, 'ORIGINAL_STATE_CHANGED'
    assert (state / 'daily-runtime' / day / 'authority.json').read_bytes() == original_authority
    feed = json.loads(output.read_bytes())
    assert feed['stage'] == 'MORNING' and feed['operational_date_jst'] == day
    assert feed['original_display_research']['status'] == 'TECHNICAL_HOLD'
    assert feed['daily_runtime']['runtime_source_sha'] == authority['runtime_source_sha']
    daily.verify_locks(state, day, 'MORNING', exact=True)
    daily.verify_locks(state, day, 'PREDEADLINE', exact=True)
    # Once metadata is completed, the special replay route cannot run again.
    assert not repair.run_if_saved(argv)
    print(json.dumps({'SAVED_MORNING_AUDIT': 'PASS', 'incident_reproduced': True,
        'counts': feed['counts'], 'original_files_preserved': len(before),
        'prediction_recalculation': 0, 'network_acquisition': 0,
        'publication': False, 'source_unchanged': True}, sort_keys=True))


if __name__ == '__main__':
    parser = argparse.ArgumentParser()
    parser.add_argument('--state', required=True, type=Path)
    parser.add_argument('--output', required=True, type=Path)
    args = parser.parse_args()
    audit(args.state, args.output)
