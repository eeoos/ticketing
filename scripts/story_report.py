#!/usr/bin/env python3
"""스토리별 테스트 통과표를 출력한다.

  uv run python scripts/story_report.py [pytest 인자...]   (기본: tests/contract)

테스트 이름 test_usNN_... 의 NN 으로 docs/stories/US-NN-*.md 와 묶는다.
하나라도 실패·건너뜀이거나 테스트가 없는 스토리가 있거나, 스토리 밖 테스트가 실패하거나, 수집 오류·pytest 종료 코드가 0 이 아니면 종료 코드 1.
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
        proc = subprocess.run([sys.executable, "-m", "pytest", "-q", "--tb=no", f"--junitxml={xml}", *args],
                              cwd=ROOT, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        if not xml.exists():
            print("pytest 결과를 얻지 못했다 (uv run 으로 실행했는지 확인)", file=sys.stderr)
            return 2
        stat: dict[str, dict[str, int]] = {}
        other = {"pass": 0, "fail": 0, "skip": 0}
        broken: list[str] = []  # 수집 오류 등 테스트 함수가 아닌 실패
        for c in ET.parse(xml).getroot().iter("testcase"):
            name = c.get("name", "")
            bad = c.find("failure") is not None or c.find("error") is not None
            skipped = c.find("skipped") is not None
            m = re.search(r"test_us(\d+)_", name)
            if m:
                s = stat.setdefault(f"US-{m.group(1)}", {"pass": 0, "fail": 0, "skip": 0})
            elif name.startswith("test"):
                s = other
            else:
                if bad:
                    broken.append(f'{c.get("classname", "")}::{name}')
                continue
            s["fail" if bad else "skip" if skipped else "pass"] += 1

    rows, ok = [], True
    for sid, title in stories().items():
        s = stat.get(sid, {"pass": 0, "fail": 0, "skip": 0})
        total = s["pass"] + s["fail"] + s["skip"]
        verdict = ("NO TESTS" if total == 0 else "FAIL" if s["fail"] else "SKIPPED" if s["skip"] else "PASS")
        ok &= verdict == "PASS"
        rows.append((sid, title, f'{s["pass"]}/{total}', verdict))
    o_total = other["pass"] + other["fail"] + other["skip"]
    o_verdict = "FAIL" if other["fail"] else "SKIPPED" if other["skip"] else "PASS"
    rows.append(("기타", "스토리 밖(계약 보충·동시성·캐시·인프라 등)", f'{other["pass"]}/{o_total}', o_verdict))
    ok &= o_verdict == "PASS"
    w = max(len(r[1]) for r in rows)
    print(f'{"스토리":<6}  {"제목":<{w}}  통과    결과')
    for sid, title, frac, verdict in rows:
        print(f"{sid:<6}  {title:<{w}}  {frac:<7} {verdict}")
    for b in broken:
        print(f"수집/실행 오류: {b}")
    if proc.returncode != 0:
        print(f"pytest 종료 코드 {proc.returncode} (0 이 아니면 실패로 본다)")
    return 0 if ok and not broken and proc.returncode == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
