"""Today's explicit exhibition-input adapter over the immutable daily engines.

Only reviewed source/receipt transport nodes are installed in memory. Native
models, unchanged functions, class objects, daily authority, files and prior
LOCKs retain their original identities. No acquisition is performed here.
"""
from __future__ import annotations

import ast
from concurrent.futures import wait
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from functools import wraps
import hashlib
import importlib
import importlib.util
import inspect
import json
import os
from pathlib import Path
import subprocess
import sys
import textwrap
import types

DAY = "2026-11-27"
FROZEN_SHA = "6797fa3205f62e0f969b0d76b5612aeb556802fe"
CANDIDATE_SHA = "614905a4001d6f4392ece21f49a30b3b4991581c"
PROTOCOL_PATH = "docs/P3_ORIGINAL_INPUT_RUNTIME_PROTOCOL_20261114.json"
PROTOCOL_SHA = "60cffdb3f516cbd14832cbddca53595ac8c0eb4379d9fe27e55f2b704e2eb759"
SCOPE_PATH = "tools/verify_exhibition_third_source_scope_v1.py"
SCOPE_SHA = "7ce7d5dfa0bc2e21f379509fc89aff9e34750ad4ebf7ff942f866af756ad08c4"
CANDIDATE_PATHS = ('.github/workflows/g11-predeadline-critical-path-ci.yml', 'docs/EXHIBITION_THIRD_SOURCE_20261127.md', 'docs/P3_ORIGINAL_INPUT_RUNTIME_PROTOCOL_20261114.json', 'g11/canonical/boatcast_preview_v1.py', 'g11/canonical/fixtures/boatcast_preview_20261127_18_03_metadata.json', 'g11/canonical/fixtures/boatcast_preview_20261127_18_03_str3.txt', 'g11/canonical/fixtures/boatcast_preview_20261127_18_03_stt.txt', 'g11/canonical/fixtures/boatcast_preview_20261127_18_03_sui.txt', 'g11/canonical/fixtures/boatcast_preview_20261127_18_03_tkz.txt', 'g11/canonical/fixtures/fun_original_20261126_20_12.html', 'g11/canonical/fixtures/fun_original_20261126_20_12.json', 'g11/canonical/fun_original_display_v1.py', 'g11/canonical/official_pipeline.py', 'g11/canonical/published_preview_v1.py', 'g11/canonical/shared_original_display_v1.py', 'g11/canonical/test_boatcast_preview_admission_v1.py', 'g11/canonical/test_boatcast_preview_v1.py', 'g11/canonical/test_exhibition_third_routing_v1.py', 'g11/canonical/test_fun_original_display_v1.py', 'g11/canonical/test_p3_boatcast_preview_transport_v1.py', 'g11/canonical/test_shared_original_display_v1.py', 'g11/growth_p3/v1_1/current_runtime_v1.py', 'g11/growth_p3/v1_1/test_official_display_tickets_v1.py', 'g11/growth_p3/v1_1/true_only_models_v2.py', 'g11/relay/test_verified_venue_live_v1.py', 'g11/relay/verified_venue_live_v1.py', 'g11/wild_pack/v1/runtime_orchestrator_v1.py', 'g11/wild_pack/v1/test_runtime_orchestrator_v1.py', 'p3/formal_input_admission/v1/audit/formal_input_admission_static_auditor_v1.py', 'p3/formal_input_admission/v1/design/FORMAL_INPUT_ADMISSION_GATE_DESIGN_V1.json', 'p3/formal_input_admission/v1/evidence/FORMAL_INPUT_ADMISSION_STATIC_AUDIT_EVIDENCE_V1.json', 'p3/formal_input_admission/v1/evidence/FORMAL_INPUT_ADMISSION_STATIC_AUDIT_READBACK_VERIFICATION_V1.json', 'p3/formal_input_admission/v1/src/formal_input_admission_gate_v1.py', 'p3/formal_invocation_adapter/v1/audit/formal_p3_invocation_adapter_static_auditor_v1.py', 'p3/formal_invocation_adapter/v1/design/FORMAL_P3_INVOCATION_ADAPTER_DESIGN_V1.json', 'p3/formal_invocation_adapter/v1/evidence/FORMAL_P3_INVOCATION_ADAPTER_STATIC_AUDIT_EVIDENCE_V1.json', 'p3/formal_invocation_adapter/v1/evidence/FORMAL_P3_INVOCATION_ADAPTER_STATIC_AUDIT_READBACK_VERIFICATION_V1.json', 'p3/formal_invocation_adapter/v1/src/formal_p3_invocation_adapter_v1.py', 'tests/test_exhibition_third_source_authority_v1.py', 'tests/test_exhibition_third_source_scope_v1.py', 'tools/verify_exhibition_third_source_scope_v1.py', 'tools/verify_p3_original_input_runtime_patch_v1.py')
JST = timezone(timedelta(hours=9))
_ACTIVE = False
_MISSING = object()
PURE_MODULES = ("g11.canonical.fun_original_display_v1", "g11.canonical.boatcast_preview_v1")


def require(ok, reason):
    if not ok:
        raise ValueError("EXHIBITION_INPUT_COMPAT:" + reason)


def _sha(raw):
    return hashlib.sha256(raw).hexdigest()


def _canonical(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True,
                      separators=(",", ":"), allow_nan=False).encode()


def _stamp(value):
    parsed = datetime.fromisoformat(value) if isinstance(value, str) else value
    require(isinstance(parsed, datetime) and parsed.utcoffset() == timedelta(hours=9), "JST_CLOCK")
    return parsed


def _candidate_head(root):
    return subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=root, text=True).strip()


def _file(path):
    require(path.is_file() and not path.is_symlink(), "REGULAR_FILE:" + path.name)
    return path.read_bytes()


def _load_module(name, path):
    spec = importlib.util.spec_from_file_location(name, path)
    require(spec is not None and spec.loader is not None, "MODULE_SPEC")
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


def _candidate(root):
    require(_candidate_head(root) == CANDIDATE_SHA, "CANDIDATE_HEAD")
    raw = _file(root / PROTOCOL_PATH)
    require(_sha(raw) == PROTOCOL_SHA, "CANDIDATE_PROTOCOL_BYTES")
    authority = json.loads(raw)["EXHIBITION_THIRD_SOURCE"]
    require(authority["PRIOR_APPROVED"] == FROZEN_SHA
            and authority["USER_AUTHORIZED_EXHIBITION_THIRD_SOURCE"] is True
            and authority["MODEL_WEIGHTS_OR_SELECTION_RULES_CHANGED"] is False
            and authority["NATIONAL_POLLING_INCREASED"] is False
            and authority["GLOBAL_STOP_BYPASS_ALLOWED"] is False
            and authority["PROVIDER_REFUSAL_PAUSE_BYPASS_ALLOWED"] is False
            and authority["ODDS_THIRD_SOURCE_ENABLED"] is False, "CANDIDATE_AUTHORITY")
    files = authority["FILE_SHA256"]
    require(isinstance(files, dict) and files.get(SCOPE_PATH) == SCOPE_SHA, "SCOPE_PIN")
    for path, expected in files.items():
        relative = Path(path)
        require(not relative.is_absolute() and ".." not in relative.parts
                and _sha(_file(root / relative)) == expected, "CANDIDATE_FILE_BYTES:" + path)
    return files


def _key(node):
    if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
        return type(node).__name__ + ":" + node.name
    if isinstance(node, ast.Assign):
        return "assign:" + ",".join(ast.unparse(target) for target in node.targets)
    if isinstance(node, (ast.Import, ast.ImportFrom)):
        return "import:" + ast.dump(node, include_attributes=False)
    return ast.dump(node, include_attributes=False)


def _dump(node):
    return ast.dump(node, include_attributes=False)


def _assign(owner, name, value, changes):
    before = owner.__dict__.get(name, _MISSING)
    changes.append((owner, name, before))
    setattr(owner, name, value)


def _cell(value):
    return (lambda: value).__closure__[0]


def _keep_wrappers(original, replacement):
    """Retain existing daily-authority guard code and its frozen-root closure."""
    if isinstance(original, (staticmethod, classmethod)):
        return type(original)(_keep_wrappers(original.__func__, replacement.__func__ if isinstance(replacement, (classmethod, staticmethod)) else replacement))
    wrapped = getattr(original, "__wrapped__", None)
    if wrapped is None:
        return replacement
    require(isinstance(original, types.FunctionType) and original.__closure__, "UNKNOWN_FUNCTION_WRAPPER")
    inner = _keep_wrappers(wrapped, replacement)
    changed = 0
    closure = []
    for cell in original.__closure__:
        if cell.cell_contents is wrapped:
            closure.append(_cell(inner)); changed += 1
        else:
            closure.append(cell)
    require(changed == 1, "WRAPPER_FUNCTION_CELL")
    clone = types.FunctionType(original.__code__, original.__globals__, original.__name__,
                               original.__defaults__, tuple(closure))
    clone.__kwdefaults__ = original.__kwdefaults__
    clone.__annotations__ = original.__annotations__.copy()
    clone.__dict__.update(original.__dict__)
    clone.__wrapped__ = inner
    clone.__qualname__ = original.__qualname__
    clone.__module__ = original.__module__
    return clone


def _transplant(module, old_source, new_source, filename, changes, unchanged):
    old, new = ast.parse(old_source), ast.parse(new_source)
    before = {_key(node): node for node in old.body}
    flags = 0
    if any(isinstance(node, ast.ImportFrom) and node.module == "__future__"
           and any(alias.name == "annotations" for alias in node.names) for node in new.body):
        import __future__
        flags = __future__.annotations.compiler_flag
    for node in new.body:
        prior = before.get(_key(node))
        if prior is not None and _dump(prior) == _dump(node):
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
                unchanged.append((module, node.name, module.__dict__[node.name]))
            continue
        if isinstance(node, ast.ClassDef):
            require(isinstance(prior, ast.ClassDef), "NEW_CLASS_PROHIBITED")
            owner = module.__dict__[node.name]
            unchanged.append((module, node.name, owner))
            old_members = {_key(member): member for member in prior.body}
            for member in node.body:
                old_member = old_members.get(_key(member))
                if old_member is not None and _dump(old_member) == _dump(member):
                    if isinstance(member, (ast.FunctionDef, ast.AsyncFunctionDef)):
                        unchanged.append((owner, member.name, owner.__dict__[member.name]))
                    continue
                require(isinstance(member, (ast.FunctionDef, ast.AsyncFunctionDef)), "CLASS_DATA_CHANGE")
                local = {}
                exec(compile(ast.Module(body=[member], type_ignores=[]), filename, "exec", flags=flags, dont_inherit=True), module.__dict__, local)
                old_value = owner.__dict__.get(member.name, _MISSING)
                value = local[member.name]
                if old_value is not _MISSING:
                    value = _keep_wrappers(old_value, value)
                _assign(owner, member.name, value, changes)
        else:
            require(isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.Import, ast.ImportFrom, ast.Assign)),
                    "UNREVIEWED_TOPLEVEL_NODE")
            local = {}
            exec(compile(ast.Module(body=[node], type_ignores=[]), filename, "exec", flags=flags, dont_inherit=True), module.__dict__, local)
            for name, value in local.items():
                if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and name in module.__dict__:
                    value = _keep_wrappers(module.__dict__[name], value)
                _assign(module, name, value, changes)


def _identity(unchanged):
    for owner, name, expected in unchanged:
        require(owner.__dict__.get(name, _MISSING) is expected, "UNCHANGED_NATIVE_OBJECT:" + name)


def _activation(state, day, authority, daily, now, *, read_only=False):
    path = state / "store/g11/exhibition-input-compat" / day / "activation.json"
    base = {"schema": "G11_EXHIBITION_INPUT_COMPAT_ACTIVATION_V1", "operational_date_jst": day,
            "frozen_source_sha": FROZEN_SHA, "authority_sha256": authority["authority_sha256"],
            "model_set_sha256": authority.get("model_set_sha256"),
            "source_archive_sha256": authority.get("source_archive_sha256"),
            "candidate_source_sha": CANDIDATE_SHA, "candidate_protocol_sha256": PROTOCOL_SHA,
            "adapter_sha256": _sha(_file(Path(__file__).resolve())),
            "only_future_inputs": True, "existing_morning_and_locks_unchanged": True}
    if path.exists():
        record = json.loads(_file(path))
        require(set(record) == set(base) | {"activated_at_jst"}
                and all(record.get(key) == value for key, value in base.items()), "ACTIVATION_BINDING")
        require(_stamp(record["activated_at_jst"]).date().isoformat() == day
                and _stamp(record["activated_at_jst"]) <= now, "ACTIVATION_CLOCK")
    else:
        require(not read_only, "READ_ONLY_ACTIVATION_MISSING")
        record = {**base, "activated_at_jst": now.isoformat()}
        daily.immutable(path, _canonical(record) + b"\n")
    return path, record


def _schedule(store, key, cutoff):
    require(isinstance(key, str) and key.startswith(DAY.replace("-", "") + "-"), "INPUT_DAY")
    payload = store.load_verified_snapshot("morning", "official-day-input")["SNAPSHOT"]["PAYLOAD"]
    rows = [row for row in payload["PREDEADLINE_PLAN"] if row["RACE_KEY"] == key and row["KIND"] == "BEFOREINFO"]
    require(payload["OPERATIONAL_DATE_JST"] == DAY and len(rows) == 1
            and _stamp(rows[0]["MUST_FINISH_BEFORE_JST"]) == cutoff, "MORNING_INPUT_SCOPE")


def _guards(modules, pure, store_root, activated, nowfunc, changes):
    official = modules["g11/canonical/official_pipeline.py"]
    packet, fun = pure["g11.canonical.boatcast_preview_v1"], pure["g11.canonical.fun_original_display_v1"]
    def clock_ok(at):
        require(activated <= _stamp(at) <= _stamp(nowfunc()), "INPUT_BEFORE_ACTIVATION_OR_FUTURE")
    original_commit = official.OfficialArtifactStore.commit_response
    @wraps(original_commit)
    def commit(self, source_id, url, captured_at, *args, **kwargs):
        if source_id.startswith("boatcast-preview-"):
            match = packet.SID.fullmatch(source_id)
            require(match is not None and self.root.resolve() == store_root, "NEW_SOURCE_STORE_OR_ID")
            clock_ok(captured_at)
            morning = self.load_verified_snapshot("morning", "official-day-input")["SNAPSHOT"]["PAYLOAD"]
            rows = [r for r in morning["PREDEADLINE_PLAN"] if r["RACE_KEY"] == match[2] and r["KIND"] == "BEFOREINFO"]
            require(len(rows) == 1, "NEW_SOURCE_RACE")
            cutoff = _stamp(rows[0]["MUST_FINISH_BEFORE_JST"])
            _schedule(self, match[2], cutoff)
            require(_stamp(nowfunc()) < cutoff, "NEW_SOURCE_CLOSED")
        return original_commit(self, source_id, url, captured_at, *args, **kwargs)
    _assign(official.OfficialArtifactStore, "commit_response", commit, changes)
    original_read = packet.read_observation
    @wraps(original_read)
    def read(store, key, cutoff, roster):
        require(store.root.resolve() == store_root, "NEW_SOURCE_STORE")
        _schedule(store, key, cutoff)
        checked = original_read(store, key, cutoff, roster)
        for receipt in checked["SNAPSHOT"]["SOURCE_RECEIPTS"][:4]:
            clock_ok(receipt["CAPTURED_AT_JST"])
        return checked
    _assign(packet, "read_observation", read, changes)
    original_due = packet.due_observation
    @wraps(original_due)
    def due(store, key, cutoff, roster, clock, **kwargs):
        require(store.root.resolve() == store_root, "NEW_SOURCE_STORE")
        _schedule(store, key, cutoff)
        clock_ok(clock())
        require(_stamp(nowfunc()) < cutoff, "NEW_SOURCE_CLOSED")
        return original_due(store, key, cutoff, roster, clock, **kwargs)
    _assign(packet, "due_observation", due, changes)
    original_parse = fun.FunProvider.parse
    @wraps(original_parse)
    def parse(self, raw, key, deadline, captured, published=None):
        clock_ok(captured)
        _schedule(official.OfficialArtifactStore(store_root), key, deadline)
        return original_parse(self, raw, key, deadline, captured, published)
    _assign(fun.FunProvider, "parse", parse, changes)


def _source_receipts(store):
    found = {}
    for path in (store / "responses").glob("boatcast-preview-*/receipt.json"):
        value = json.loads(_file(path))
        found[path.relative_to(store).as_posix()] = {key: value.get(key) for key in
            ("SOURCE_ID", "HOST", "URL", "SHA256", "CAPTURED_AT_JST")}
    for path in (store / "g11/shared-original-display-v1/races").glob("*/proof.json"):
        value = json.loads(_file(path))
        if value.get("PROVIDER") == "BOATRACE_FUN_ORIGINAL":
            found[path.relative_to(store).as_posix()] = {key: value.get(key) for key in
                ("RACE_ID", "PROVIDER", "HOST", "URL", "SHA256", "CAPTURED_AT")}
    return found


def _quiet_original_futures():
    module = sys.modules.get("g11.growth_p3.v1_1.official_display_tickets_v1")
    if module is not None:
        guard = getattr(module, "_guard", None)
        if guard is not None:
            with guard:
                pending = tuple(task for task in module._pending.values() if not task.done())
            if pending:
                _done, remaining = wait(pending, timeout=4.0)
                require(not remaining, "PENDING_ORIGINAL_CAPTURE")


def _model_metadata(store, daily):
    """Native daily guards retain full model hashes; avoid another GB-scale pass."""
    result = {}
    for name in daily.MODEL_ROOTS:
        for path in (store / name).rglob("*"):
            require(not path.is_symlink(), "MODEL_SYMLINK")
            if path.is_file():
                st = path.stat()
                result[path.relative_to(store).as_posix()] = (st.st_size, st.st_mtime_ns, st.st_ino)
    return result


def _store_metadata(store):
    result = {}
    for path in store.rglob("*"):
        require(not path.is_symlink(), "STORE_SYMLINK")
        if path.is_file() and "__pycache__" not in path.parts:
            st = path.stat()
            result[path.relative_to(store).as_posix()] = (st.st_size, st.st_mtime_ns, st.st_ino)
    return result


def _phase_receipt(state, day, authority, daily, activation_path, activation,
                   protected, before_new, nowfunc, status):
    after_locks = daily.lock_inventory(state / "store", day, "PREDEADLINE")
    after_sources = _source_receipts(state / "store")
    record = {"schema": "G11_EXHIBITION_INPUT_COMPAT_PHASE_V1", "operational_date_jst": day,
        "status": status, "authority_sha256": authority["authority_sha256"],
        "candidate_source_sha": CANDIDATE_SHA, "adapter_sha256": activation["adapter_sha256"],
        "activation_sha256": _sha(_file(activation_path)),
        "activated_at_jst": activation["activated_at_jst"], "completed_at_jst": _stamp(nowfunc()).isoformat(),
        "new_lock_files": {p: v for p, v in after_locks.items() if p not in protected},
        "new_source_receipts": {p: v for p, v in after_sources.items() if p not in before_new},
        "existing_morning_models_authority_and_locks_unchanged": True,
        "native_unchanged_objects_preserved": True}
    raw = _canonical(record) + b"\n"
    daily.immutable(activation_path.parent / ("phase-" + _sha(raw) + ".json"), raw)


@contextmanager
def context(state_root, day, candidate_root, nowfunc=None, *, read_only=False):
    """Apply today's explicit input-only adapter; always restore native objects."""
    global _ACTIVE
    require(not _ACTIVE, "NESTED_CONTEXT")
    require(day == DAY, "EXPLICIT_DAY_ONLY")
    state, candidate = Path(state_root).resolve(), Path(candidate_root).resolve()
    nowfunc = nowfunc or (lambda: datetime.now(JST))
    now = _stamp(nowfunc())
    require(now.date().isoformat() >= day if read_only else now.date().isoformat() == day, "REAL_DAY")
    _candidate(candidate)
    from g11.canonical import official_pipeline as official
    from g11.relay import daily_runtime_authority_v1 as daily
    frozen = Path(official.__file__).resolve().parents[2]
    require(frozen != candidate, "FROZEN_SOURCE_SEPARATION")
    authority = daily.load(state, day)
    require(authority["runtime_source_sha"] == FROZEN_SHA, "FROZEN_AUTHORITY")
    before_models = _model_metadata(state / "store", daily)
    protected = {**daily.lock_inventory(state / "store", day, "MORNING"),
                 **daily.lock_inventory(state / "store", day, "PREDEADLINE")}
    before_new = _source_receipts(state / "store")
    before_modules, parent_attributes = dict(sys.modules), []
    changes, unchanged = [], []
    activation_path = None
    finalized = None
    _quiet_original_futures()
    _ACTIVE = True
    try:
        scope = _load_module("_g11_exhibition_source_scope_" + SCOPE_SHA, candidate / SCOPE_PATH)
        old = {path: _file(frozen / path).decode() for path in scope.SOURCE_PATHS}
        new = {path: _file(candidate / path).decode() for path in scope.SOURCE_PATHS}
        require(all(_sha(source.encode()) == authority["source_files"].get(path)
                    for path, source in old.items()), "FROZEN_SOURCE_AUTHORITY")
        scope.verify_sources(new, old)
        modules = {path: importlib.import_module(path[:-3].replace("/", ".")) for path in scope.SOURCE_PATHS}
        for path, module in modules.items():
            require(Path(module.__file__).resolve() == frozen / path, "LOADED_FROZEN_MODULE:" + path)
        pure = {}
        for name in PURE_MODULES:
            require(name not in before_modules, "NEW_PROVIDER_ALREADY_IMPORTED")
            parent_name, attr = name.rsplit(".", 1)
            parent = importlib.import_module(parent_name)
            parent_attributes.append((parent, attr, parent.__dict__.get(attr, _MISSING)))
            pure[name] = _load_module(name, candidate / (name.replace(".", "/") + ".py"))
            setattr(parent, attr, pure[name])
        for path, module in modules.items():
            _transplant(module, old[path], new[path], str(candidate / path), changes, unchanged)
        _identity(unchanged)
        activation_path, activation = _activation(state, day, authority, daily, now, read_only=read_only)
        _guards(modules, pure, state / "store", _stamp(activation["activated_at_jst"]), nowfunc, changes)
        if read_only:
            def no_acquisition(*_args, **_kwargs):
                raise ValueError("EXHIBITION_INPUT_COMPAT:READ_ONLY_ACQUISITION_PROHIBITED")
            for owner, names in ((modules["g11/canonical/published_preview_v1.py"],
                                  ("capture_missing", "due_observation", "fetch_day", "_read")),
                                 (modules["g11/canonical/shared_original_display_v1.py"], ("acquire",)),
                                 (pure["g11.canonical.boatcast_preview_v1"], ("due_observation", "_read"))):
                for name in names:
                    unchanged[:] = [entry for entry in unchanged
                                    if not (entry[0] is owner and entry[1] == name)]
                    _assign(owner, name, no_acquisition, changes)
        else:
            # CRITICAL hashes the complete store before returning. Finalize our
            # own audit and bounded background reads BEFORE that existing hash;
            # context exit must not append another file after the checkpoint.
            critical = importlib.import_module("g11.relay.predeadline_critical_v1")
            require(Path(critical.__file__).resolve() == frozen / "g11/relay/predeadline_critical_v1.py"
                    and _sha(_file(Path(critical.__file__)))
                    == authority["source_files"].get("g11/relay/predeadline_critical_v1.py"),
                    "FROZEN_CRITICAL_SOURCE")
            original_tree = critical._tree_sha
            @wraps(original_tree)
            def tree(root):
                nonlocal finalized
                require(Path(root).resolve() == state / "store", "CHECKPOINT_STORE")
                _quiet_original_futures()
                _identity(unchanged)
                if finalized is None:
                    _phase_receipt(state, day, authority, daily, activation_path, activation,
                                   protected, before_new, nowfunc, "FINALIZED_BEFORE_CHECKPOINT")
                    finalized = _store_metadata(state / "store")
                else:
                    require(_store_metadata(state / "store") == finalized, "POST_CHECKPOINT_STORE_WRITE")
                return original_tree(root)
            _assign(critical, "_tree_sha", tree, changes)
        yield activation
    finally:
        try:
            _quiet_original_futures()
            _identity(unchanged)
            if "old" in locals():
                require(all(_file(frozen / path).decode() == source for path, source in old.items()),
                        "FROZEN_SOURCE_FILES_CHANGED")
            require(_model_metadata(state / "store", daily) == before_models, "FROZEN_MODEL_FILES_CHANGED")
            for path, digest in protected.items():
                require(_sha(_file(state / "store" / path)) == digest, "PRIOR_LOCK_CHANGED:" + path)
            verified = daily.load(state, day)
            require(verified == authority, "DAILY_AUTHORITY_CHANGED")
            if finalized is not None:
                require(_store_metadata(state / "store") == finalized, "POST_CHECKPOINT_STORE_WRITE")
            elif activation_path is not None and not read_only:
                _phase_receipt(state, day, authority, daily, activation_path, activation,
                               protected, before_new, nowfunc, "FINALIZED_ON_CONTEXT_EXIT")
        finally:
            for owner, name, previous in reversed(changes):
                if previous is _MISSING:
                    owner.__dict__.pop(name, None) if isinstance(owner, types.ModuleType) else delattr(owner, name)
                else:
                    setattr(owner, name, previous)
            for parent, name, previous in reversed(parent_attributes):
                if previous is _MISSING:
                    parent.__dict__.pop(name, None)
                else:
                    setattr(parent, name, previous)
            for name in tuple(sys.modules):
                if name not in before_modules and (name in PURE_MODULES or name.startswith("_g11_exhibition_source_scope_")):
                    sys.modules.pop(name, None)
            _ACTIVE = False


exhibition_input_compatibility = context
