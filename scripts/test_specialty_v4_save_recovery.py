"""Bounded recovery contract tests. All writes use disposable test directories."""
from copy import deepcopy
from pathlib import Path
import json
import os
import subprocess
import tempfile
import unittest
from unittest.mock import patch

import specialty_v4_save_recovery as r


class RecoveryTests(unittest.TestCase):
    def test_existing_projection_reconstructed_without_any_new_lock(self):
        path = os.environ.get("SPECIALTY_TEST_APP")
        if not path: self.skipTest("Saved read-only October 17 app fixture is required")
        app = json.loads(Path(path).read_bytes())
        before = deepcopy(app); projection = r.projection_from_app(app)
        self.assertIn(r.digest(r.canonical(projection)), (r.PROJECTION_SHA, r.SAVED_PROJECTION_SHA))
        self.assertEqual(len(projection["races"]), 154)
        self.assertEqual(app, before)

    def test_nonresearch_byte_or_model_state_change_rejected(self):
        state = {"active": {"identity": "original"}, "history": ["retained"],
            "generations": {"original": {"qualification": {"valid_races": 99}}}}
        files = {r.LIFECYCLE: "a", "model-registry/models/original/model.cbm": "b", "lock.json": "c"}
        after = {**files, r.LIFECYCLE: "research-only"}
        updated = deepcopy(state); updated["specialty"] = {"saved": True}
        r.validate_difference(files, after, state, updated)
        for key in ("lock.json", "model-registry/models/original/model.cbm"):
            with self.assertRaisesRegex(RuntimeError, "BYTES_CHANGED"):
                r.validate_difference(files, {**after, key: "changed"}, state, updated)
        updated["generations"]["original"]["qualification"]["valid_races"] = 100
        with self.assertRaisesRegex(RuntimeError, "STATE_CHANGED"):
            r.validate_difference(files, after, state, updated)
        with self.assertRaisesRegex(RuntimeError, "FILE_SET_CHANGED"):
            r.validate_difference(files, {**after, "new-lock.json": "x"}, state, state)

    def test_old_adoptions_cannot_be_rewritten(self):
        old = {"specialty": {"engines": {"ABEKEN_V4": {"generations": {"g": {
            "adoptions": {"v4:initial": {"saved_at": "actual-first-save"}}}}}}}}
        new = deepcopy(old); new["specialty"]["engines"]["ABEKEN_V4"]["generations"]["g"]["adoptions"]["v4:initial"]["saved_at"] = "backdated"
        with self.assertRaisesRegex(RuntimeError, "PRIOR_ADOPTION_CHANGED"):
            r.validate_difference({}, {}, old, new)

    def test_readback_protects_all_prediction_and_finance_fields(self):
        before = {"goripan": {"observation": {"lifecycle": {"specialty": {}, "state_sha256": "old", "active": "fixed"}}},
            "races": [{"bets": ["1-2-3"]}], "final_accounting": {"profit": -100}}
        after = deepcopy(before); after["goripan"]["observation"]["lifecycle"].update(specialty={"evaluation": "saved"}, state_sha256="new")
        self.assertEqual(r.protected_public(before), r.protected_public(after))
        after["final_accounting"]["profit"] = 100
        self.assertNotEqual(r.protected_public(before), r.protected_public(after))

    def test_existing_but_changed_artifact_and_new_day_fail_closed(self):
        artifact = {"id": r.ARTIFACT_ID, "name": r.ARTIFACT_NAME, "expired": False,
            "created_at": "2026-10-17T14:03:30Z", "digest": r.ARTIFACT_SHA,
            "workflow_run": {"id": r.ARTIFACT_RUN, "repository_id": 1367703708, "head_repository_id": 1367703708}}
        run = {"conclusion": "success", "path": r.WORKFLOW, "head_branch": "main"}
        with patch.dict(os.environ, GITHUB_RUN_ID="999999"):
            with patch.object(r, "api", side_effect=[{"artifacts": [artifact]}, run, {"artifacts": []}]):
                r.artifact_guard()
            with patch.object(r, "api", return_value={"artifacts": [{**artifact, "digest": "changed"}]}):
                with self.assertRaisesRegex(RuntimeError, "SOURCE_ARTIFACT_CHANGED"): r.artifact_guard()
            today = {**artifact, "name": "g11-wild4-20261018-future-oos-locks-encrypted"}
            with patch.object(r, "api", side_effect=[{"artifacts": [artifact]}, run, {"artifacts": [today]}]):
                with self.assertRaisesRegex(RuntimeError, "NEWER_DAY_LEDGER_EXISTS"): r.artifact_guard()

    def test_explicit_scope_and_expiry(self):
        with tempfile.TemporaryDirectory() as directory:
            event = Path(directory) / "event.json"
            event.write_text(json.dumps({"head_commit": {"message": "ordinary commit"}}))
            env = {"GITHUB_EVENT_PATH": str(event), "GITHUB_EVENT_NAME": "push", "GITHUB_REPOSITORY": r.REPO,
                "GITHUB_REPOSITORY_ID": "1367703708", "GITHUB_REF": "refs/heads/main"}
            with patch.dict(os.environ, env):
                with self.assertRaisesRegex(RuntimeError, "EXPLICIT_RESEARCH_ONLY_SCOPE"): r.scope()
                event.write_text(json.dumps({"head_commit": {"message": r.MARKER}}))
                with patch.object(r, "THROUGH", "2000-01-01"):
                    with self.assertRaisesRegex(RuntimeError, "ONE_DAY_AUTHORITY_EXPIRED"): r.scope()

    def test_finish_saved_never_evaluates_or_posts_again(self):
        import ast
        tree = ast.parse(Path(r.__file__).read_text())
        for fn in (n for n in tree.body if isinstance(n, ast.FunctionDef)
                   and n.name in ("finish_saved", "readback_saved")):
            calls = {ast.unparse(n.func) for n in ast.walk(fn) if isinstance(n, ast.Call)}
            self.assertFalse(calls & {"life.refresh_specialty_v4", "life.save", "publish", "subprocess.run"})
            self.assertNotIn("ACTIONS_ID_TOKEN_REQUEST_TOKEN", ast.unparse(fn))

    def test_encrypted_saved_readback_full_byte_equality(self):
        with tempfile.TemporaryDirectory() as directory:
            work = Path(directory); root = work / "g11-wild4-future-locks"
            (root / "model-registry").mkdir(parents=True)
            (root / r.LIFECYCLE).write_bytes(b'{"test_only":true}')
            (root / "lock.json").write_bytes(b'fixture-only-no-production-lock')
            (work / "expected-ledger-files.json").write_bytes(r.canonical(r.inventory(root)))
            subprocess.run(["tar", "-czf", str(work / "test.tgz"), "-C", str(work), root.name], check=True)
            secret = "isolated-test-passphrase"
            gpg_home = work / "gnupg"; gpg_home.mkdir(mode=0o700)
            encrypted = subprocess.run(["gpg", "--batch", "--yes", "--pinentry-mode", "loopback", "--passphrase-fd", "0",
                "--symmetric", "--cipher-algo", "AES256", "--output", str(work / "test.gpg"), str(work / "test.tgz")],
                input=(secret + "\n").encode(), env={**os.environ, "GNUPGHOME": str(gpg_home)}, capture_output=True)
            if encrypted.returncode and b"failed to start gpg-agent" in encrypted.stderr and os.environ.get("GITHUB_ACTIONS") != "true":
                self.skipTest("Local gpg-agent unavailable; mandatory encryption test runs in existing Actions job before upload")
            encrypted.check_returncode()
            args = type("Args", (), {"work": work, "lock_archive": work / "test.gpg"})()
            with patch.dict(os.environ, STATE_PASSPHRASE=secret, GNUPGHOME=str(gpg_home)): r.verify_saved(args)


if __name__ == "__main__": unittest.main()
