"""Disposable-copy integration check; no network, publication or cache save."""
import argparse
from copy import deepcopy
import json
import os
from pathlib import Path
import subprocess
from unittest.mock import patch

import daily_runtime_delivery_v1 as delivery
import repair_saved_morning_receipts_v1 as repair
from g11.canonical import official_pipeline as official
from g11.relay import delayed_morning_recovery_v1 as recovery


def audit(state, output):
    before = recovery.inventory(state)
    argv = ['--date', repair.DAY, '--state', str(state), '--source-sha', repair.SOURCE,
            '--output', str(output), '--run-id', os.environ['GITHUB_RUN_ID']]
    with patch.dict(os.environ, {'GITHUB_EVENT_NAME': 'workflow_dispatch'}), \
            patch.object(recovery, 'run', side_effect=AssertionError('NO_REGENERATION')) as generate, \
            patch.object(official.OfficialHttpClient, 'fetch_once', side_effect=AssertionError('NO_ACQUISITION')) as acquire:
        assert repair.run_if_reviewed(argv)
        generate.assert_not_called()
        acquire.assert_not_called()
    assert recovery.inventory(state) == before
    assert output.read_bytes() == (state / 'rescue/feed.json').read_bytes()
    source = json.loads(output.read_bytes())
    posted = []

    def send(args, **kwargs):
        assert args[:1] == ['curl']
        data = Path(args[args.index('--data-binary') + 1][1:]).read_bytes()
        feed = json.loads(data)
        actual = sum(isinstance((r.get('abeken_shadow') or {}).get(stage), dict)
            for r in feed['races'] for stage in ('morning', 'live'))
        assert feed['abeken']['predictions_emitted'] == actual
        original_rows = {r['key']: r for r in source['races']}
        assert set(original_rows) == {r['key'] for r in feed['races']}
        for row in feed['races']:
            for field in ('same_day_rescue_lock','practical_bets','p3_snapshot_sha256','abeken_shadow'):
                assert row.get(field) == original_rows[row['key']].get(field), (row['key'],field)
        posted.append((feed, data))
        Path(args[args.index('--output') + 1]).write_text(json.dumps({
            'status':'PASS','payload_sha256':delivery.hot.hash_bytes(data)}))
        return subprocess.CompletedProcess(args, 0, '200', '')

    def fetch(url):
        feed, data = posted[-1]
        return deepcopy(feed), {'x-g11-source-feed-sha256':delivery.hot.hash_bytes(data)}, data

    Path('.relay-output').mkdir(exist_ok=True)
    with patch.dict(os.environ, {'G11_SYNC_OIDC':'offline-test-only'}), \
            patch.object(delivery.subprocess, 'run', side_effect=send), patch.object(delivery.hot,'fetch',side_effect=fetch):
        delivery.deliver_rescue(state, output, delivery.ORIGIN, 1, lambda _: None)
    after = recovery.inventory(state)
    assert {k:v for k,v in after.items() if k != 'rescue/delivery.json'} == {
        k:v for k,v in before.items() if k != 'rescue/delivery.json'}
    print(json.dumps({'SAVED_RESCUE_PUBLICATION_AUDIT':'PASS','published':False,
        'saved_races':source['same_day_rescue']['rescued_race_count'],
        'original_abeken_count':source['abeken']['predictions_emitted'],
        'transport_abeken_count':posted[0][0]['abeken']['predictions_emitted'],
        'prediction_recalculation':0,'saved_state_unchanged':True},sort_keys=True))


if __name__ == '__main__':
    parser = argparse.ArgumentParser()
    parser.add_argument('--state', type=Path, required=True)
    parser.add_argument('--output', type=Path, required=True)
    args = parser.parse_args()
    audit(args.state, args.output)
