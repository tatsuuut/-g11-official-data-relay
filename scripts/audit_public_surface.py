from __future__ import annotations

import pathlib
import re
import sys


ROOT = pathlib.Path(__file__).resolve().parents[1]
ALLOWED_FILES = {
    ".github/workflows/g11-free-runner.yml",
    ".github/workflows/g11-explicit-night-dispatcher.yml",
    ".github/workflows/g11-night-backfill-20260917-20260919.yml",
    ".github/workflows/g11-wild-position-bootstrap.yml",
    ".gitignore",
    "README.md",
    "SECURITY.md",
    "scripts/audit_public_surface.py",
}
IGNORED_PARTS = {".git", "__pycache__"}
FORBIDDEN_SUFFIXES = {
    ".cbm",
    ".gz",
    ".joblib",
    ".json",
    ".onnx",
    ".pickle",
    ".pkl",
    ".tar",
    ".zip",
}
FORBIDDEN_PATTERNS = {
    "github_token_literal": re.compile(r"gh[pousr]_[A-Za-z0-9_]{20,}"),
    "private_key": re.compile(r"-----BEGIN (?:RSA |EC |OPENSSH )?PRIVATE KEY-----"),
    "auth_header": re.compile(r"Authorization:\\s*Bearer\\s+(?!\\$|\\{)", re.IGNORECASE),
    "production_logic": re.compile(
        r"(?:P3_(?:COEFFICIENT|WEIGHT)|AI_COEFFICIENT_LEGACY|catboost_model|SHAP_VALUES)",
        re.IGNORECASE,
    ),
}
PATTERN_DEFINITION_FILE = "scripts/audit_public_surface.py"


def fail(message: str) -> None:
    raise SystemExit(f"PUBLIC_SURFACE_AUDIT_FAIL: {message}")


def main() -> int:
    discovered: set[str] = set()
    for path in ROOT.rglob("*"):
        relative = path.relative_to(ROOT)
        if any(part in IGNORED_PARTS for part in relative.parts):
            continue
        if path.is_symlink():
            fail(f"symlink forbidden: {relative}")
        if not path.is_file():
            continue
        name = relative.as_posix()
        discovered.add(name)
        if name not in ALLOWED_FILES:
            fail(f"unexpected public file: {name}")
        if any(name.endswith(suffix) for suffix in FORBIDDEN_SUFFIXES):
            fail(f"forbidden artifact type: {name}")
        if path.stat().st_size > 100_000:
            fail(f"oversized public file: {name}")
        text = path.read_text(encoding="utf-8")
        if name != PATTERN_DEFINITION_FILE:
            for label, pattern in FORBIDDEN_PATTERNS.items():
                if pattern.search(text):
                    fail(f"{label}: {name}")

    missing = sorted(ALLOWED_FILES - discovered)
    if missing:
        fail(f"missing allowlisted files: {', '.join(missing)}")
    print("PUBLIC_SURFACE_AUDIT_PASS")
    return 0


if __name__ == "__main__":
    sys.exit(main())
