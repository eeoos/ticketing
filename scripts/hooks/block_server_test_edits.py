#!/usr/bin/env python3
"""Claude Code PreToolUse hook: stop the server seat from editing tests/ and e2e/.

This catches the common mistake only. It can be bypassed (e.g. shell redirection
via Bash), so the final check is `python3 scripts/verify_tests.py check`.
"""
import json
import os
import sys
from pathlib import Path

SERVER_SESSIONS = {"dev-server@tkt"}
PROTECTED_DIRS = ("tests", "e2e")


def main() -> int:
    if os.environ.get("OPENRIG_SESSION_NAME") not in SERVER_SESSIONS:
        return 0
    try:
        payload = json.load(sys.stdin)
    except (json.JSONDecodeError, ValueError):
        return 0
    tool_input = payload.get("tool_input") or {}
    raw = tool_input.get("file_path") or tool_input.get("notebook_path")
    if not raw:
        return 0

    root = Path(os.environ.get("CLAUDE_PROJECT_DIR") or payload.get("cwd") or os.getcwd()).resolve()
    target = Path(raw)
    if not target.is_absolute():
        target = Path(payload.get("cwd") or root) / target
    target = target.resolve()  # follows symlinks and normalizes ../
    try:
        rel = target.relative_to(root)
    except ValueError:
        return 0
    if rel.parts and rel.parts[0] in PROTECTED_DIRS:
        print(
            f"차단: server 자리는 {rel} 를 수정할 수 없습니다. tests/·e2e/ 는 test 자리 소유입니다. "
            "테스트 변경이 필요하면 OpenRig 큐로 lead/test에 요청하세요.",
            file=sys.stderr,
        )
        return 2
    return 0


if __name__ == "__main__":
    sys.exit(main())
