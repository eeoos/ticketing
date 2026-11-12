#!/usr/bin/env python3
"""티켓 오픈 트래픽 계산 시트. 공식과 입력값은 docs/capacity.md.

  uv run python scripts/capacity.py   # 결과 표(Markdown) 출력
"""
import math
import sys
from dataclasses import dataclass, field
from fractions import Fraction


def _default_db_ops() -> dict[str, int]:
    return {"queue_enter": 0, "poll": 0, "seat_view": 2, "hold": 1, "payment": 3}


@dataclass(frozen=True)
class Assumptions:
    seats: int = 50_000                 # S [가정]
    arrivals: int = 1_000_000           # N [가정]
    arrival_window_s: float = 60.0      # T [가정]
    poll_interval_s: float = 5.0        # p [가정]
    admit_rate_per_s: float = 200.0     # μ [가정]
    seat_views_per_user: int = 2        # v [가정]
    holds_per_user: int = 2             # h [가정]
    pay_ratio: float = 0.4              # r [가정]
    payments_per_payer: int = 1         # k [가정]
    db_ops_per_request: dict[str, int] = field(default_factory=_default_db_ops)  # [가정]


@dataclass(frozen=True)
class Row:
    key: str
    label: str
    people: float | None
    requests: float
    duration_s: float
    rps_peak: float
    rps_avg: float


HTTP_ROWS = ("queue_enter", "poll", "seat_view", "hold", "payment")
LABELS = {
    "queue_enter": "대기열 진입",
    "poll": "순번 확인(폴링)",
    "seat_view": "좌석 조회",
    "hold": "홀드",
    "payment": "모의 결제",
    "db": "DB 작업",
    "total": "합계(HTTP)",
}


def admitted(a: Assumptions) -> int:
    """A = min(N, ceil(S / (r·k))). 0.4 같은 소수 오차를 피하려고 분수로 계산한다."""
    per_person = Fraction(str(a.pay_ratio)) * a.payments_per_payer
    if per_person <= 0:
        return a.arrivals
    return min(a.arrivals, math.ceil(Fraction(a.seats) / per_person))


def compute(a: Assumptions = Assumptions()) -> dict[str, Row]:
    n, t, p, mu = a.arrivals, a.arrival_window_s, a.poll_interval_s, a.admit_rate_per_s
    lam = n / t
    a_in = admitted(a)
    t_end = a_in / mu
    if not lam > mu:
        raise ValueError(f"대기열이 생기지 않는다: 도착률 {lam:g}/s ≤ 입장률 {mu:g}/s")
    if t_end < t:
        raise ValueError(f"입장이 오픈 구간보다 먼저 끝난다: t_end {t_end:g}s < T {t:g}s")

    q_peak = n - mu * t
    q_end = n - a_in
    waiting = 0.5 * t * q_peak + (t_end - t) * (q_peak + q_end) / 2
    v, h, r, k = a.seat_views_per_user, a.holds_per_user, a.pay_ratio, a.payments_per_payer

    def row(key, people, requests, duration, peak, avg=None):
        return Row(key, LABELS[key], people, requests, duration, peak, peak if avg is None else avg)

    rows = {
        "queue_enter": row("queue_enter", n, n, t, lam),
        "poll": row("poll", n, waiting / p, t_end, q_peak / p, (waiting / p) / t_end),
        "seat_view": row("seat_view", a_in, v * a_in, t_end, v * mu),
        "hold": row("hold", a_in, h * a_in, t_end, h * mu),
        "payment": row("payment", r * a_in, r * k * a_in, t_end, r * k * mu),
    }
    ops = a.db_ops_per_request
    db_requests = sum(rows[key].requests * ops.get(key, 0) for key in HTTP_ROWS)
    db_peak = sum(rows[key].rps_peak * ops.get(key, 0) for key in HTTP_ROWS)
    rows["db"] = row("db", None, db_requests, t_end, db_peak, db_requests / t_end)
    total_requests = sum(rows[key].requests for key in HTTP_ROWS)
    total_peak = sum(rows[key].rps_peak for key in HTTP_ROWS)
    rows["total"] = row("total", None, total_requests, t_end, total_peak, total_requests / t_end)
    return rows


def _fmt(x: float | None) -> str:
    if x is None:
        return "—"
    return f"{x:,.0f}" if float(x).is_integer() else f"{x:,.2f}"


def to_markdown(rows: dict[str, Row]) -> str:
    lines = [
        "| 행 | 사람 수 | 요청 수 | 기간(초) | 초당 요청 수(피크) | 초당 요청 수(평균) |",
        "|---|---:|---:|---:|---:|---:|",
    ]
    for r in rows.values():
        lines.append(f"| {r.label} | {_fmt(r.people)} | {_fmt(r.requests)} | {_fmt(r.duration_s)} "
                     f"| {_fmt(r.rps_peak)} | {_fmt(r.rps_avg)} |")
    return "\n".join(lines)


def main() -> int:
    print(to_markdown(compute()))
    return 0


if __name__ == "__main__":
    sys.exit(main())
