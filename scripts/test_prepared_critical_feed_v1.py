import json
import os
import tempfile
import unittest
from datetime import datetime
from pathlib import Path
from unittest.mock import patch

import critical_saved_publication_v1 as c


class PreparedFeed(unittest.TestCase):
    def test_binding_and_remote_readback_gate(self):
        with tempfile.TemporaryDirectory() as temp, patch.dict(os.environ, {
                'GITHUB_RUN_ID': '21', 'GITHUB_RUN_ATTEMPT': '1', 'CRITICAL_CHECKPOINT_VERIFIED': 'true'}):
            root = Path(temp)
            day = datetime.now(c.JST).date().isoformat()
            dest = root / '.relay-output'
            state = root / '.relay-state'
            dest.mkdir()
            state.mkdir()
            (state / 'relay-state.json').write_text('{}')
            (dest / 'publication-state.sha256').write_text('a' * 64 + '  .relay-cache/state.tgz.gpg\n')
            output = dest / 'critical-feed.json'
            def project(state, date, run, source, target):
                target.write_text(json.dumps({'stage': 'PREDEADLINE', 'status': 'PASS', 'operational_date_jst': date}))
                return True
            with patch.object(c, 'project_saved', side_effect=project) as actual:
                c.prepare_feed(root, day)
                actual.assert_called_once()
            self.assertFalse(output.exists())
            for key, value in [('CRITICAL_CHECKPOINT_VERIFIED','false'), ('GITHUB_RUN_ID','22'), ('GITHUB_RUN_ATTEMPT','2')]:
                with patch.dict(os.environ,{key:value}):
                    self.assertFalse(c.use_prepared_feed(root, day, output))
            self.assertFalse(output.exists())
            self.assertTrue(c.use_prepared_feed(root, day, output))
            for path in [dest/'critical-prepared-feed.json', dest/'publication-state.sha256', state/'relay-state.json']:
                prior = path.read_bytes()
                path.write_bytes(prior+b'x')
                self.assertFalse(c.use_prepared_feed(root, day, output))
                path.write_bytes(prior)
            self.assertFalse(c.use_prepared_feed(root, '2000-01-01', output))
