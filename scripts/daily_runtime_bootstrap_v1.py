"""Source-selection adapter for the private immutable daily authority.

The public repository has no prediction code. It verifies the retained bundle
before loading the private authority implementation, without fetching main.
"""
from __future__ import annotations
import argparse
import hashlib
import importlib.util
import json
import os
from pathlib import Path
import re
import tarfile
import tempfile
import shutil
from datetime import date, datetime, timedelta

EFFECTIVE_FROM = "2026-10-04"
MODULE_PATH = "g11/relay/daily_runtime_authority_v1.py"


def canonical(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True,
                      separators=(",", ":"), allow_nan=False).encode()


def module(path):
    spec = importlib.util.spec_from_file_location("_g11_daily_runtime_authority", path)
    value = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(value)
    return value


def retained_module(state, day):
    root = state / "daily-runtime" / day
    path = root / "authority.json"
    if not path.is_file() or path.is_symlink():
        raise RuntimeError("DAILY_AUTHORITY_UNKNOWN")
    authority = json.loads(path.read_bytes())
    unsigned = dict(authority)
    sha = unsigned.pop("authority_sha256", None)
    if (sha != hashlib.sha256(canonical(unsigned)).hexdigest()
            or authority.get("schema") != "G11_DAILY_RUNTIME_AUTHORITY_V1"
            or authority.get("operational_date_jst") != day or authority.get("immutable") is not True):
        raise RuntimeError("DAILY_AUTHORITY_SHA_MISMATCH")
    name = authority.get("source_archive", "")
    if not re.fullmatch(r"source-[a-f0-9]{64}\.tgz", name):
        raise RuntimeError("DAILY_SOURCE_ARCHIVE_NAME")
    archive = root / name
    if (archive.is_symlink() or not archive.is_file()
            or hashlib.sha256(archive.read_bytes()).hexdigest() != authority["source_archive_sha256"]):
        raise RuntimeError("DAILY_SOURCE_ARCHIVE_SHA_MISMATCH")
    with tarfile.open(archive, "r:gz") as handle:
        data = handle.extractfile(MODULE_PATH).read()
    if hashlib.sha256(data).hexdigest() != authority["source_files"].get(MODULE_PATH):
        raise RuntimeError("DAILY_AUTHORITY_IMPLEMENTATION_HASH")
    with tempfile.TemporaryDirectory() as temporary:
        implementation = Path(temporary) / "authority.py"
        implementation.write_bytes(data)
        result = module(implementation)
    if day == "2026-10-04":
        from daily_predeadline_authority_recovery_v1 import apply
        apply(result, day)
    return result


def prepare(state: Path, day: str, phase: str, source: Path) -> dict:
    if day < EFFECTIVE_FROM or phase == "dry-run":
        return {"checkout_candidate": "true", "frozen": "false"}
    path = state / "daily-runtime" / day / "authority.json"
    if phase in {"predeadline", "night"} and not path.exists() and day == "2026-10-06":
        from daily_predeadline_authority_recovery_v1 import rescue_certificate
        cert = rescue_certificate(state, day)
        return {"checkout_candidate": "true", "frozen": "false",
                "rescue_bootstrap": "true", "source_sha": cert["runtime_source_sha"]}
    if not path.exists():
        if phase != "morning":
            raise RuntimeError("DAILY_AUTHORITY_UNKNOWN")
        if any((state / "store/snapshots/morning").glob("*.json")):
            raise RuntimeError("DAILY_AUTHORITY_EXISTING_MORNING_LOCK_PROHIBITED")
        return {"checkout_candidate": "true", "frozen": "false"}
    authority_module = retained_module(state, day)
    authority = authority_module.restore_source(state, day, source)
    authority_module.verify_runtime(state, day, source)
    return {"checkout_candidate": "false", "frozen": "true",
            "source_sha": authority["runtime_source_sha"],
            "authority_sha256": authority["authority_sha256"]}


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("action", choices=("prepare", "freeze", "checkpoint-export", "candidate-dry-run"))
    parser.add_argument("--state-root", required=True, type=Path)
    parser.add_argument("--operational-date", required=True)
    parser.add_argument("--phase", default="morning")
    parser.add_argument("--source-root", type=Path, default=Path(".g11-private"))
    parser.add_argument("--source-sha")
    parser.add_argument("--output", type=Path)
    args = parser.parse_args()
    if args.action == "prepare":
        value = prepare(args.state_root, args.operational_date, args.phase, args.source_root)
    elif args.action == "freeze":
        if args.operational_date < EFFECTIVE_FROM or args.phase == "dry-run":
            value = {"status": "PRE_ACTIVATION_UNCHANGED"}
        else:
            implementation = module(args.source_root / MODULE_PATH)
            authority = (implementation.create(args.state_root, args.operational_date,
                         args.source_root, args.source_sha) if args.phase == "morning"
                         else implementation.load(args.state_root, args.operational_date))
            implementation.verify_runtime(args.state_root, args.operational_date, args.source_root)
            value = {"status": "PASS", "source_sha": authority["runtime_source_sha"],
                     "authority_sha256": authority["authority_sha256"]}
    elif args.action == "candidate-dry-run":
        implementation = module(args.source_root / MODULE_PATH)
        candidate_day = (date.fromisoformat(args.operational_date) + timedelta(days=1)).isoformat()
        with tempfile.TemporaryDirectory(prefix="g11-next-morning-") as directory:
            state = Path(directory) / "state"
            for name in implementation.MODEL_ROOTS:
                source = args.state_root / "store" / name
                if source.is_dir():
                    shutil.copytree(source, state / "store" / name)
            authority = implementation.create(state, candidate_day, args.source_root, args.source_sha,
                now=datetime.fromisoformat(candidate_day + "T02:15:00+09:00"))
            frozen = Path(directory) / "frozen"
            implementation.restore_source(state, candidate_day, frozen)
            implementation.verify_runtime(state, candidate_day, frozen)
            value = {"status": "PASS", "test": "NEXT_OPERATIONAL_DAY_AUTHORITY_DRY_RUN",
                     "operational_date_jst": candidate_day,
                     "authority_sha256": authority["authority_sha256"],
                     "runtime_source_sha": authority["runtime_source_sha"],
                     "p3_version": authority["p3"]["version"],
                     "abeken_v53_version": authority["abeken_v53"]["version"],
                     "source_file_count": len(authority["source_files"]), "model_file_count": len(authority["model_files"]),
                     "production_state_mutated": False, "prediction_executed": False, "site_write": False}
        if args.output:
            args.output.parent.mkdir(parents=True, exist_ok=True)
            args.output.write_bytes(canonical(value) + b"\n")
    else:
        implementation = retained_module(args.state_root, args.operational_date)
        value = implementation.emit_checkpoint(args.state_root, args.operational_date, args.output)
    if os.environ.get("GITHUB_OUTPUT"):
        with open(os.environ["GITHUB_OUTPUT"], "a") as output:
            for key, item in value.items():
                if isinstance(item, (str, int, bool)):
                    output.write(f"{key}={item}\n")
    print(json.dumps(value, sort_keys=True))


if __name__ == "__main__":
    main()