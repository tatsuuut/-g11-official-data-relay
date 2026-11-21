"""Exercise the diagnostic with sealed-input fixtures and forbidden mutators."""
from copy import deepcopy
import hashlib
import json
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

import ast
import textwrap

_WORKFLOW=Path(__file__).resolve().parents[1]/'.github/workflows/g11-wild4-shadow-lock-only.yml'
_source=_WORKFLOW.read_text()
_start=_source.index('          def frozen_source_context_readback(')
_end=_source.index('          def input_context_health(',_start)
_namespace={}
exec(compile(ast.parse(textwrap.dedent(_source[_start:_end])),str(_WORKFLOW),'exec'),_namespace)
frozen_source_context_readback=_namespace['frozen_source_context_readback']
try:
    from g11.wild_pack.v1 import runtime_orchestrator_v1 as real_runtime
    from g11.wild_pack.v1 import prospective_oos_v1 as prospective
    from g11.wild_pack.v1 import test_prospective_oos_v1 as fixture
    from g11.wild_pack.v1 import test_bet_oos_collection_v1 as bet_fixture
    import g11.wild_pack.v1 as package
    PRIVATE_READY = True
except ImportError:
    PRIVATE_READY = False


@unittest.skipUnless(PRIVATE_READY, 'Pinned private source and test fixtures required by CI')
class ReadbackTests(unittest.TestCase):
    def setUp(self):
        temporary = TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.source = self.root / "g11/wild_pack/v1/runtime_orchestrator_v1.py"
        self.source.parent.mkdir(parents=True)
        self.source.write_bytes(b"fixture-frozen-runtime")
        self.raw_sha = hashlib.sha256
        self.runtime_sha = "852bd01a9474c7c6ef85c46a36658e99bb5b038fa50faf39b7fc5ee795a62945"
        self.sources = {
            "POSITION": "ce2d42946fb8f25d91dd0fcfff8b801f9f45c4ef21fb23a0d8bd4daeef17013c",
            "PROSPECTIVE": "a94c6df067a2d1ae32b22059192e2e8cb98041d81f3081d6b775302aa967c60a",
            "BET": "d8cf97ec2dc4309becb1ce51c40d53e10e790f2eab9a15855b92199b534149f4",
        }
        self.feature = fixture._batch()
        self.head, self.head_binding = bet_fixture._head()
        self.head_binding["UNDERLYING_MODEL_VERSION"] = self.head["MODEL_VERSION"]
        self.day = self.feature["OPERATIONAL_DATE_JST"]
        self.registries = {wid: fixture._binding(wid) for wid in prospective.WILD_IDS}
        self.input_hash = self.canonical_sha(self.feature)
        self.admission = prospective.build_admission(
            self.feature, atomic_input_snapshot_id=f"MORNING:{self.day}:{'f' * 64}:{self.input_hash}",
            input_hash=self.input_hash, evaluation_context_pack_id="e" * 64,
            model_bindings=self.registries, admitted_at="2026-09-20T00:00:00+00:00")
        schema = {k: self.feature[k] for k in ("FEATURE_SCHEMA_VERSION", "NUMERIC_FEATURES", "CATEGORICAL_FEATURES")}
        expected = prospective.evaluation_context_id(
            head=self.head_binding, second=self.registries[real_runtime.position.SECOND_ID],
            third=self.registries[real_runtime.position.THIRD_ID], prediction_stage="MORNING",
            snapshot_contract_hash=self.canonical_sha(schema), inference_pipeline_hash=self.canonical_sha(self.sources))
        self.binding = {"EVALUATION_CONTEXT_PACK_ID": expected, "SECRET": "never-output-this"}
        self.forbidden = Mock(side_effect=AssertionError("MUTATOR_CALLED"))
        position = SimpleNamespace(
            __file__=str(self.source.parent / "conditional_position_model_v1.py"),
            SECOND_ID=real_runtime.position.SECOND_ID, THIRD_ID=real_runtime.position.THIRD_ID,
            load_json=self.load_json, validate_registry=Mock(), predict_runtime_batch=self.forbidden)
        self.runtime = SimpleNamespace(
            __file__=str(self.source), position=position, bet=SimpleNamespace(predict_and_lock=self.forbidden),
            prospective=SimpleNamespace(WILD_IDS=prospective.WILD_IDS,
                load_admission=lambda path: self.admission,
                evaluation_context_id=prospective.evaluation_context_id,
                build_admission=self.forbidden, persist_admission=self.forbidden),
            _true_ai_day_root=real_runtime._true_ai_day_root,
            _load_manifest_artifact=self.load_manifest,
            _filter_to_admission=real_runtime._filter_to_admission,
            _canonical_sha=self.canonical_sha, _position_model_root=real_runtime._position_model_root,
            _head_binding=lambda store, manifest: self.head_binding,
            run_morning=self.forbidden)
        self.runtime._source_sha=lambda module: self.sources[
            "POSITION" if module is position else "BET" if module is self.runtime.bet else "PROSPECTIVE"]

    def canonical_sha(self, value):
        return self.raw_sha(json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False).encode()).hexdigest()

    def load_json(self, path):
        if path.name == "POSITION_MODEL_CONTRACT_V1.json":
            return {}
        wid = path.parent.name
        if path.name == "current.json":
            return {"STATUS": "CURRENT", "WILD_ID": wid, "PREDICTION_STAGE": "MORNING", "REGISTRY_FILE": "registry.json"}
        return self.registries[wid]

    def load_manifest(self, path):
        if path.name == "morning-features.manifest.json":
            return {"ARTIFACT_SHA256": "a" * 64, "MORNING_ARTIFACT_SHA256": "f" * 64}, self.feature
        return {"ARTIFACT_SHA256": "b" * 64}, self.head

    def check(self, commit="7faaacfec8a8643a588af7e23d062fc787ba24b8"):
        # Substitute only source I/O. Context hashing uses actual prospective code.
        def source_sha(raw):
            if raw == b"fixture-frozen-runtime":
                return SimpleNamespace(hexdigest=lambda: self.runtime_sha)
            return self.raw_sha(raw)
        with patch.object(package, "runtime_orchestrator_v1", self.runtime), \
             patch("subprocess.check_output", return_value=commit), \
             patch("hashlib.sha256", side_effect=source_sha):
            return frozen_source_context_readback(self.root / "store", self.day, self.binding)

    def test_source_only_recalculation_matches_candidate_and_preserves_saved_mismatch(self):
        protected = deepcopy((self.binding, self.feature, self.head, self.admission, self.registries))
        report = self.check()
        self.assertEqual(report["status"], "MATCH")
        self.assertTrue(report["matches_frozen_candidate"])
        self.assertFalse(report["matches_saved_admission"])
        self.assertEqual(report["inference_pipeline_hash"], "ea948d20103f32d9b9bf7db73db4ac07772d8b3fbda3f96876d485f44872e967")
        self.assertEqual((self.binding, self.feature, self.head, self.admission, self.registries), protected)
        self.assertEqual(self.source.read_bytes(), b"fixture-frozen-runtime")
        self.forbidden.assert_not_called()
        self.assertNotIn("never-output-this", json.dumps(report))

    def test_context_difference_is_reported_without_rebinding(self):
        self.binding["EVALUATION_CONTEXT_PACK_ID"] = "9" * 64
        report = self.check()
        self.assertEqual(report["status"], "MISMATCH")
        self.assertFalse(report["matches_frozen_candidate"])
        self.assertEqual(self.binding["EVALUATION_CONTEXT_PACK_ID"], "9" * 64)
        self.forbidden.assert_not_called()

    def test_unpinned_source_does_not_claim_identity_or_interrupt(self):
        report = self.check(commit="0" * 40)
        self.assertEqual(report["status"], "READBACK_UNAVAILABLE")
        self.assertNotIn("computed_context_id", report)
        self.runtime_sha = "0" * 64
        self.assertEqual(self.check()["status"], "READBACK_UNAVAILABLE")

    def test_saved_input_or_registry_difference_does_not_claim_recovery(self):
        self.admission["INPUT_HASH"] = "0" * 64
        self.assertEqual(self.check()["status"], "READBACK_UNAVAILABLE")
        self.admission["INPUT_HASH"] = self.input_hash
        self.registries[prospective.WILD_IDS[0]]["MODEL_SHA256"] = "0" * 64
        self.assertEqual(self.check()["status"], "READBACK_UNAVAILABLE")


if __name__ == "__main__":
    unittest.main()

