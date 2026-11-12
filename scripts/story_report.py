#!/usr/bin/env python3
"""스토리별 테스트 통과표를 출력한다.

  uv run python scripts/story_report.py [pytest 인자...]   (기본: tests/contract)

테스트 이름 test_usNN_... 의 NN 으로 docs/stories/US-NN-*.md 와 묶는다.
하나라도 실패하거나 테스트가 없는 스토리가 있으면 종료 코드 1.
"""
import re
import subprocess
import sys
import tempfile
import xml.etree.ElementTree as ET
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent


def stories() -> dict[str, str]:
    out = {}
    for p in sorted((ROOT / "docs" / "stories").glob("US-*.md")):
        m = re.match(r"US-(\d+)", p.name)
        title = p.read_text().splitlines()[0].lstrip("# ").strip()
        out[f"US-{m.group(1)}"] = title
    return out


def main() -> int:
    args = sys.argv[1:] or ["tests/contract"]
    with tempfile.TemporaryDirectory() as tmp:
        xml = Path(tmp) / "r.xml"
        subprocess.run([sys.executable, "-m", "pytest", "-q", "--tb=no", f"--junitxml={xml}", *args],
                       cwd=ROOT, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        if not xml.exists():
            print("pytest 결과를 얻지 못했다 (uv run 으로 실행했는지 확인)", file=sys.stderr)
            return 2
        cases = ET.parse(xml).getroot().iter("testcase")
        stat: dict[str, dict[str, int]] = {}
        for c in cases:
            m = re.search(r"test_us(\d+)_", c.get("name", ""))
            if not m:
                continue
            s = stat.setdefault(f"US-{m.group(1)}", {"pass": 0, "fail": 0})
            bad = c.find("failure") is not None or c.find("error") is not None
            s["fail" if bad else "pass"] += 1

    rows, ok = [], True
    for sid, title in stories().items():
        s = stat.get(sid, {"pass": 0, "fail": 0})
        total = s["pass"] + s["fail"]
        verdict = "NO TESTS" if total == 0 else ("PASS" if s["fail"] == 0 else "FAIL")
        ok &= verdict == "PASS"
        rows.append((sid, title, f'{s["pass"]}/{total}', verdict))
    w = max(len(r[1]) for r in rows)
    print(f'{"스토리":<6}  {"제목":<{w}}  통과    결과')
    for sid, title, frac, verdict in rows:
        print(f"{sid:<6}  {title:<{w}}  {frac:<6}  {verdict}")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
