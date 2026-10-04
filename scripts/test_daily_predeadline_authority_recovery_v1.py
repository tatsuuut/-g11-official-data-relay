"""Focused fail-closed regression for the Oct 4 frozen-runtime adapter."""
from datetime import datetime
import json
import os
from pathlib import Path
import tempfile
import types
import unittest

import daily_predeadline_authority_recovery_v1 as recovery


class RecoveryTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.state = Path(temp.name)
        self.store = self.state / "store"
        self.deadline = "2026-10-04T12:00:00+09:00"
        self.early = "2026-10-04T11:50:00+09:00"
        self.key = "20261004-01-01"
        self._write("snapshots/morning/official-day-input.json", {
            "STAGE": "MORNING", "NAME": "official-day-input", "PAYLOAD": {
                "PREDEADLINE_PLAN": [{"RACE_KEY": self.key,
                    "DUE_AT_JST": "2026-10-04T11:45:00+09:00",
                    "MUST_FINISH_BEFORE_JST": self.deadline}]}})
        self.locks = {}
        self.records = []

        def inventory(store, day, phase):
            return {p.relative_to(store).as_posix(): recovery.hashlib.sha256(p.read_bytes()).hexdigest()
                    for p in sorted((store / "snapshots" / phase.lower()).glob("*.json"))}

        def old_verify(state, day, phase, *, exact=False):
            observed = inventory(state / "store", day, phase)
            if exact and observed != self.records[-1]["lock_files"]:
                raise RuntimeError("OLD_EXACT_MISMATCH")
            return observed

        self.module = types.SimpleNamespace(lock_inventory=inventory,
            verify_locks=old_verify, bindings=lambda state, day, phase: self.records)
        recovery.apply(self.module, recovery.DAY)
        self._seal()

    def _write(self, relative, value, *, late=False):
        path = self.store / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(value, sort_keys=True))
        timestamp = datetime.fromisoformat(
            "2026-10-04T12:01:00+09:00" if late else self.early).timestamp()
        os.utime(path, (timestamp, timestamp))
        return path

    def _new(self, suffix="beforeinfo"):
        name = f"snapshots/predeadline/{suffix}-{self.key}.json"
        path = self._write(name, {"STAGE": "PREDEADLINE", "NAME": Path(name).stem,
            "PAYLOAD": {"RACE_KEY": self.key, "CAPTURED_AT_JST": self.early,
                        "RESULT_USED_AS_PREDICTION_FEATURE": False},
            "SOURCE_RECEIPTS": [{"CAPTURED_AT_JST": self.early}]})
        return path

    def _seal(self):
        observed = self.module.lock_inventory(self.store, recovery.DAY, "PREDEADLINE")
        self.records.append({"lock_files": observed})

    def test_A_first_binding(self):
        self.assertEqual(self.module.verify_locks(self.state, recovery.DAY, "PREDEADLINE", exact=True), {})

    def test_B_append_future_lock(self):
        path = self._new()
        observed = self.module.verify_locks(self.state, recovery.DAY, "PREDEADLINE", exact=True)
        self.assertIn(path.relative_to(self.store).as_posix(), observed)

    def test_C_existing_lock_mutation_fails(self):
        path = self._new()
        self.module.verify_locks(self.state, recovery.DAY, "PREDEADLINE")
        self._seal()
        path.write_bytes(b"changed")
        with self.assertRaisesRegex(RuntimeError, "LOCK_MISMATCH"):
            self.module.verify_locks(self.state, recovery.DAY, "PREDEADLINE")

    def test_D_existing_lock_deletion_fails(self):
        path = self._new()
        self._seal()
        path.unlink()
        with self.assertRaisesRegex(RuntimeError, "LOCK_MISMATCH"):
            self.module.verify_locks(self.state, recovery.DAY, "PREDEADLINE")

    def test_E_postdeadline_append_fails(self):
        path = self._new()
        timestamp = datetime.fromisoformat("2026-10-04T12:01:00+09:00").timestamp()
        os.utime(path, (timestamp, timestamp))
        with self.assertRaisesRegex(RuntimeError, "AFTER_DEADLINE"):
            self.module.verify_locks(self.state, recovery.DAY, "PREDEADLINE")

    def test_F_retry_idempotent(self):
        self._new()
        first = self.module.verify_locks(self.state, recovery.DAY, "PREDEADLINE")
        self._seal()
        self.assertEqual(first, self.module.verify_locks(self.state, recovery.DAY, "PREDEADLINE", exact=True))

    def test_G_multiple_heartbeats_monotonic(self):
        self._new("beforeinfo")
        first = self.module.verify_locks(self.state, recovery.DAY, "PREDEADLINE")
        self._seal()
        self._new("odds3t")
        second = self.module.verify_locks(self.state, recovery.DAY, "PREDEADLINE", exact=True)
        self._seal()
        self.assertLess(len(first), len(second))
        self.assertEqual(second, self.module.verify_locks(self.state, recovery.DAY, "PREDEADLINE", exact=True))

    def test_H_morning_gate_unchanged(self):
        with self.assertRaisesRegex(RuntimeError, "OLD_EXACT_MISMATCH"):
            self.module.verify_locks(self.state, recovery.DAY, "MORNING", exact=True)

    def test_I_result_backflow_fails(self):
        path = self._new()
        value = json.loads(path.read_bytes())
        value["PAYLOAD"]["RESULT_USED_AS_PREDICTION_FEATURE"] = True
        self._write(path.relative_to(self.store).as_posix(), value)
        with self.assertRaisesRegex(RuntimeError, "RESULT_BACKFLOW"):
            self.module.verify_locks(self.state, recovery.DAY, "PREDEADLINE")


if __name__ == "__main__":
    unittest.main()
