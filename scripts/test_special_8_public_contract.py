"""The public relay must never truncate a verified P3 eight-ticket lock."""
import importlib.util
import json
from pathlib import Path
import tempfile
import unittest

SCRIPT = Path(__file__).with_name("audit_public_surface.py")
SPEC = importlib.util.spec_from_file_location("relay_audit", SCRIPT)
relay_audit = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(relay_audit)
EIGHT = ["4-2-1", "4-2-5", "4-1-2", "4-5-2", "5-2-1", "5-2-4", "5-1-2", "5-4-2"]


class SpecialEightPublicContract(unittest.TestCase):
    def test_keeps_all_eight_in_order_and_bytes_unchanged(self):
        race = {"key": "20260926-02-10", "special_case_id": "G11_P3_SPECIAL_45_2_EQ_145_V1",
                "practical_bets": EIGHT, "p3_production_bets": EIGHT, "production_points": 8,
                "predeadline": {"special_case_id": "G11_P3_SPECIAL_45_2_EQ_145_V1",
                                "practical_bets": EIGHT, "production_points": 8}}
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "feed.json"
            path.write_text(json.dumps({"races": [race]}), encoding="utf-8")
            original = path.read_bytes()
            self.assertEqual(relay_audit.special_eight_site_compat(path), 0)
            self.assertEqual(path.read_bytes(), original)
            race["practical_bets"] = EIGHT[:7]
            race["production_points"] = 7
            race["special_case_id"] = None
            race["caution"] = "正本特例8点=" + "/".join(EIGHT)
            path.write_text(json.dumps({"races": [race]}), encoding="utf-8")
            with self.assertRaisesRegex(SystemExit, "special-eight public artifact mismatch"):
                relay_audit.special_eight_site_compat(path)


if __name__ == "__main__":
    unittest.main()
