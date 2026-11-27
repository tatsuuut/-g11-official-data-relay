"""Offline adapter guards; real frozen-consumer execution is tested separately."""
from concurrent.futures import Future
from contextlib import contextmanager
from datetime import datetime, timedelta
from functools import wraps
import json
import re
from pathlib import Path
import tempfile
import threading
import time
import types
import unittest
from unittest.mock import patch

import exhibition_input_compatibility_v1 as adapter


NOW = datetime.fromisoformat("2026-11-27T15:00:00+09:00")


class AdapterCoreTests(unittest.TestCase):
    def _guard_fixture(self, tmp):
        store_root = Path(tmp) / "store"
        key = "20261127-05-02"
        cutoff = NOW + timedelta(minutes=3)
        calls = []
        payload = {"OPERATIONAL_DATE_JST": adapter.DAY, "PREDEADLINE_PLAN": [{
            "KIND": "BEFOREINFO", "RACE_KEY": key, "MUST_FINISH_BEFORE_JST": cutoff.isoformat()}]}
        class Store:
            def __init__(self, root): self.root = root
            def load_verified_snapshot(self, *_): return {"SNAPSHOT": {"PAYLOAD": payload}}
            def commit_response(self, *args, **kwargs): calls.append(args); return "saved"
        receipts = [{"CAPTURED_AT_JST": (NOW + timedelta(seconds=1)).isoformat()} for _ in range(4)]
        receipts.append({"CAPTURED_AT_JST": (NOW - timedelta(hours=1)).isoformat()})
        packet = types.SimpleNamespace(
            SID=re.compile(r"boatcast-preview-(tkz|stt|sui|str3)-(\d{8}-\d{2}-\d{2})-([a-f0-9]{64})"),
            read_observation=lambda *_: {"SNAPSHOT": {"SOURCE_RECEIPTS": receipts}},
            due_observation=lambda *_a, **_k: "due")
        class Fun:
            def parse(self, *args): return "parsed"
        fun = types.SimpleNamespace(FunProvider=Fun)
        official = types.SimpleNamespace(OfficialArtifactStore=Store)
        modules = {"g11/canonical/official_pipeline.py": official}
        pure = {"g11.canonical.boatcast_preview_v1": packet, "g11.canonical.fun_original_display_v1": fun}
        adapter._guards(modules, pure, store_root, NOW, lambda: NOW + timedelta(seconds=30), [])
        return Store(store_root), packet, receipts, calls, key, cutoff

    def test_new_resource_capture_must_be_after_activation_and_for_known_future_race(self):
        with tempfile.TemporaryDirectory() as tmp:
            store, _packet, _receipts, calls, key, _cutoff = self._guard_fixture(tmp)
            sid = "boatcast-preview-tkz-" + key + "-" + "a" * 64
            with self.assertRaisesRegex(ValueError, "BEFORE_ACTIVATION"):
                store.commit_response(sid, "source", NOW - timedelta(seconds=1), b"raw")
            with self.assertRaisesRegex(ValueError, "BEFORE_ACTIVATION_OR_FUTURE"):
                store.commit_response(sid, "source", NOW + timedelta(seconds=31), b"raw")
            with self.assertRaisesRegex(ValueError, "NEW_SOURCE_RACE"):
                store.commit_response(sid.replace("-05-02-", "-05-03-"), "source", NOW, b"raw")
            self.assertEqual(calls, [])
            self.assertEqual(store.commit_response(sid, "source", NOW, b"raw"), "saved")
            self.assertEqual(len(calls), 1)

    def test_old_orientation_receipt_is_allowed_but_old_new_resource_is_rejected(self):
        with tempfile.TemporaryDirectory() as tmp:
            store, packet, receipts, _calls, key, cutoff = self._guard_fixture(tmp)
            self.assertIsNotNone(packet.read_observation(store, key, cutoff, {}))
            receipts[0]["CAPTURED_AT_JST"] = (NOW - timedelta(seconds=1)).isoformat()
            with self.assertRaisesRegex(ValueError, "BEFORE_ACTIVATION"):
                packet.read_observation(store, key, cutoff, {})

    def test_new_provider_reader_cannot_substitute_another_store_or_deadline(self):
        with tempfile.TemporaryDirectory() as tmp:
            store, packet, _receipts, _calls, key, cutoff = self._guard_fixture(tmp)
            with self.assertRaisesRegex(ValueError, "INPUT.*|DEADLINE"):
                packet.read_observation(store, key, cutoff + timedelta(seconds=1), {})
            store.root = Path(tmp) / "other-store"
            with self.assertRaisesRegex(ValueError, "NEW_SOURCE_STORE"):
                packet.read_observation(store, key, cutoff, {})

    def test_transplant_keeps_native_function_and_class_objects_and_candidate_source_locations(self):
        old = "def native(x):\n    return x * 3\n\nclass Engine:\n    def source(self):\n        return 'old'\n"
        new = old.replace("return 'old'", "return 'new'")
        module = types.ModuleType("synthetic_frozen")
        exec(compile(old, "frozen.py", "exec"), module.__dict__)
        native, engine = module.native, module.Engine
        changes, unchanged = [], []
        adapter._transplant(module, old, new, "candidate.py", changes, unchanged)
        adapter._identity(unchanged)
        self.assertIs(module.native, native)
        self.assertIs(module.Engine, engine)
        self.assertEqual(module.Engine().source(), "new")
        self.assertEqual(module.Engine.source.__code__.co_filename, "candidate.py")
        self.assertEqual(module.Engine.source.__code__.co_firstlineno, 5)
        for owner, name, value in reversed(changes):
            setattr(owner, name, value)
        self.assertEqual(module.Engine().source(), "old")

    def test_daily_boundary_wrapper_preserves_guard_code_and_closure(self):
        events, marker = [], object()
        def original(self):
            return "old"
        def bind(function, role):
            @wraps(function)
            def guard(self):
                events.append((role, marker))
                return function(self)
            guard._g11_daily_authority_boundary = True
            return guard
        guarded = bind(original, "PREDEADLINE")
        def candidate(self):
            return "new"
        result = adapter._keep_wrappers(guarded, candidate)
        self.assertEqual(result(None), "new")
        self.assertEqual(events, [("PREDEADLINE", marker)])
        self.assertIs(result.__code__, guarded.__code__)
        self.assertIs(result.__globals__, guarded.__globals__)
        self.assertIs(result.__wrapped__, candidate)
        self.assertTrue(result._g11_daily_authority_boundary)
        self.assertEqual(guarded(None), "old")

    def test_added_globals_and_methods_have_reversible_attribute_records(self):
        old = "VALUE = 1\ndef native():\n    return VALUE\n"
        new = "VALUE = 2\nEXTRA = 'provider'\ndef native():\n    return VALUE\n"
        module = types.ModuleType("synthetic_globals")
        exec(old, module.__dict__)
        native = module.native
        changes, unchanged = [], []
        adapter._transplant(module, old, new, "candidate.py", changes, unchanged)
        self.assertIs(module.native, native)
        self.assertEqual(module.native(), 2)
        for owner, name, value in reversed(changes):
            if value is adapter._MISSING:
                owner.__dict__.pop(name)
            else:
                setattr(owner, name, value)
        self.assertNotIn("EXTRA", module.__dict__)
        self.assertEqual(module.native(), 1)

    def test_unknown_wrapper_without_exact_inner_cell_is_rejected(self):
        def original():
            return 1
        def bad():
            return 2
        bad.__wrapped__ = original
        with self.assertRaisesRegex(ValueError, "UNKNOWN_FUNCTION_WRAPPER"):
            adapter._keep_wrappers(bad, lambda: 3)

    def test_explicit_day_and_real_jst_day_fail_before_candidate_or_module_mutation(self):
        with patch.object(adapter, "_candidate", side_effect=AssertionError("should not validate source")):
            with self.assertRaisesRegex(ValueError, "EXPLICIT_DAY_ONLY"):
                with adapter.context(Path("unused"), "2026-11-28", Path("unused"), nowfunc=lambda: NOW):
                    self.fail("entered")
            with self.assertRaisesRegex(ValueError, "REAL_DAY"):
                with adapter.context(Path("unused"), adapter.DAY, Path("unused"), nowfunc=lambda: NOW - timedelta(days=1)):
                    self.fail("entered")

    def test_wrong_candidate_head_is_rejected_before_reading_any_module(self):
        with tempfile.TemporaryDirectory() as tmp, patch.object(adapter, "_candidate_head", return_value="0" * 40):
            with self.assertRaisesRegex(ValueError, "CANDIDATE_HEAD"):
                adapter._candidate(Path(tmp))

    def test_protocol_original_bytes_are_checked_before_parsing(self):
        with tempfile.TemporaryDirectory() as tmp, patch.object(adapter, "_candidate_head", return_value=adapter.CANDIDATE_SHA):
            root = Path(tmp)
            path = root / adapter.PROTOCOL_PATH
            path.parent.mkdir(parents=True)
            path.write_text('{"EXHIBITION_THIRD_SOURCE": {}}')
            with self.assertRaisesRegex(ValueError, "PROTOCOL_BYTES"):
                adapter._candidate(root)

    def test_activation_is_immutable_bound_and_reuses_first_clock(self):
        with tempfile.TemporaryDirectory() as tmp:
            state = Path(tmp)
            class Daily:
                @staticmethod
                def immutable(path, raw):
                    path.parent.mkdir(parents=True, exist_ok=True)
                    if path.exists() and path.read_bytes() != raw:
                        raise ValueError("immutable")
                    path.write_bytes(raw)
            authority = {"authority_sha256": "a" * 64}
            path, first = adapter._activation(state, adapter.DAY, authority, Daily, NOW)
            original = path.read_bytes()
            _, later = adapter._activation(state, adapter.DAY, authority, Daily, NOW + timedelta(minutes=2))
            self.assertEqual(first, later)
            self.assertEqual(path.read_bytes(), original)
            self.assertEqual(first["activated_at_jst"], NOW.isoformat())
            with self.assertRaisesRegex(ValueError, "ACTIVATION_BINDING"):
                adapter._activation(state, adapter.DAY, {"authority_sha256": "b" * 64}, Daily, NOW)
            with self.assertRaisesRegex(ValueError, "ACTIVATION_CLOCK"):
                adapter._activation(state, adapter.DAY, authority, Daily, NOW - timedelta(seconds=1))

    def test_read_only_cannot_create_activation(self):
        with tempfile.TemporaryDirectory() as tmp:
            daily = types.SimpleNamespace(immutable=lambda *_: self.fail("read-only write"))
            with self.assertRaisesRegex(ValueError, "READ_ONLY_ACTIVATION_MISSING"):
                adapter._activation(Path(tmp), adapter.DAY, {"authority_sha256": "a" * 64}, daily, NOW,
                                    read_only=True)

    def test_pending_original_capture_is_finished_before_namespace_can_be_restored(self):
        pending, complete = Future(), threading.Event()
        module = types.SimpleNamespace(_guard=threading.Lock(), _pending={"race": pending})
        def worker():
            time.sleep(.03)
            complete.set()
            pending.set_result({"STATUS": "CAPTURED"})
        thread = threading.Thread(target=worker)
        thread.start()
        with patch.dict(adapter.sys.modules, {"g11.growth_p3.v1_1.official_display_tickets_v1": module}):
            adapter._quiet_original_futures()
            self.assertTrue(complete.is_set())
            self.assertTrue(pending.done())
        thread.join(1)

    def test_still_pending_future_is_not_reported_as_quiet(self):
        pending = Future()
        module = types.SimpleNamespace(_guard=threading.Lock(), _pending={"race": pending})
        with patch.dict(adapter.sys.modules, {"g11.growth_p3.v1_1.official_display_tickets_v1": module}), \
             patch.object(adapter, "wait", return_value=(set(), {pending})):
            with self.assertRaisesRegex(ValueError, "PENDING_ORIGINAL_CAPTURE"):
                adapter._quiet_original_futures()


if __name__ == "__main__":
    unittest.main()
