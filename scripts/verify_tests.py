#!/usr/bin/env python3
"""Record and verify SHA-256 hashes of protected test files (tests/, e2e/).

  python3 scripts/verify_tests.py record   # lead: after RED tests are accepted
  python3 scripts/verify_tests.py check    # lead: before integrating GREEN work

`check` exits 1 if any protected file was changed, removed, or added since `record`.
This is the final gate; the Claude Code hook only catches common mistakes.
"""
import argparse
import hashlib
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
PROTECTED_DIRS = ("tests", "e2e")
MANIFEST = ROOT / "scripts" / "test_hashes.json"
IGNORED_PARTS = {"__pycache__", ".pytest_cache", "node_modules", ".DS_Store"}


def collect() -> dict[str, str]:
    hashes = {}
    for d in PROTECTED_DIRS:
        base = ROOT / d
        if not base.exists():
            continue
        for p in sorted(base.rglob("*")):
            if p.is_dir() or IGNORED_PARTS.intersection(p.relative_to(ROOT).parts):
                continue
            hashes[p.relative_to(ROOT).as_posix()] = hashlib.sha256(p.read_bytes()).hexdigest()
    return hashes


def record() -> int:
    hashes = collect()
    MANIFEST.write_text(json.dumps({"files": hashes}, indent=2, sort_keys=True) + "\n")
    print(f"recorded {len(hashes)} file(s) -> {MANIFEST.relative_to(ROOT)}")
    return 0


def check() -> int:
    if not MANIFEST.exists():
        print(f"FAIL: {MANIFEST.relative_to(ROOT)} not found; run `record` first", file=sys.stderr)
        return 1
    expected = json.loads(MANIFEST.read_text())["files"]
    actual = collect()
    changed = sorted(f for f in expected.keys() & actual.keys() if expected[f] != actual[f])
    removed = sorted(expected.keys() - actual.keys())
    added = sorted(actual.keys() - expected.keys())
    for label, files in (("changed", changed), ("removed", removed), ("added", added)):
        for f in files:
            print(f"{label}: {f}")
    if changed or removed or added:
        print("FAIL: protected test files differ from the recorded hashes", file=sys.stderr)
        return 1
    print(f"OK: {len(actual)} protected test file(s) match")
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("command", choices=("record", "check"))
    args = parser.parse_args()
    return record() if args.command == "record" else check()


if __name__ == "__main__":
    sys.exit(main())
