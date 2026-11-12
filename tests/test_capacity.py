"""트래픽 계산 시트 공식 검증 (docs/capacity.md).

기대값은 문서의 공식으로 손계산한 값이다. 모듈 import 는 테스트 안에서 해서,
scripts/capacity.py 가 없을 때 테스트마다 개별 실패로 보이게 한다.

기본값 손계산 (N=1e6, T=60, S=5e4, p=5, μ=200, v=2, h=2, r=0.4, k=1):
  A = min(1e6, ⌈5e4/0.4⌉) = 125,000      t_end = 125,000/200 = 625
  Q_peak = 1e6 − 200·60 = 988,000          Q_end = 1e6 − 125,000 = 875,000
  W = ½·60·988,000 + (625−60)·(988,000+875,000)/2 = 29,640,000 + 526,297,500 = 555,937,500
  DB 요청 = 250,000·2 + 250,000·1 + 50,000·3 = 900,000   DB 피크 = 400·2 + 400·1 + 80·3 = 1,440
"""
import dataclasses
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
KEYS = ["queue_enter", "poll", "seat_view", "hold", "payment", "db", "total"]
LAMBDA = 1_000_000 / 60  # 도착률


def cap():
    from scripts import capacity  # noqa: PLC0415
    return capacity


# ---- 1. 기본 입력값 ----
def test_capacity_default_assumptions_match_doc():
    a = cap().Assumptions()
    assert a.seats == 50_000
    assert a.arrivals == 1_000_000
    assert a.arrival_window_s == 60
    assert a.poll_interval_s == 5
    assert a.admit_rate_per_s == 200
    assert a.seat_views_per_user == 2
    assert a.holds_per_user == 2
    assert a.pay_ratio == pytest.approx(0.4)
    assert a.payments_per_payer == 1
    assert a.db_ops_per_request == {"queue_enter": 0, "poll": 0, "seat_view": 2, "hold": 1, "payment": 3}


def test_capacity_assumptions_and_row_are_frozen():
    c = cap()
    with pytest.raises(dataclasses.FrozenInstanceError):
        c.Assumptions().seats = 1
    row = c.compute()["hold"]
    with pytest.raises(dataclasses.FrozenInstanceError):
        row.requests = 1


# ---- 2. 기본값 각 행 ----
def test_capacity_compute_returns_seven_rows_with_labels():
    rows = cap().compute()
    assert list(rows) == KEYS
    for key, row in rows.items():
        assert row.key == key
        assert isinstance(row.label, str) and row.label
    assert len({r.label for r in rows.values()}) == 7


def test_capacity_default_queue_enter_row():
    r = cap().compute()["queue_enter"]
    assert r.people == 1_000_000
    assert r.requests == pytest.approx(1_000_000)
    assert r.duration_s == pytest.approx(60)
    assert r.rps_peak == pytest.approx(LAMBDA)
    assert r.rps_avg == pytest.approx(LAMBDA)


def test_capacity_default_poll_row():
    r = cap().compute()["poll"]
    assert r.people == 1_000_000
    assert r.requests == pytest.approx(111_187_500)  # W/p
    assert r.duration_s == pytest.approx(625)
    assert r.rps_peak == pytest.approx(197_600)  # Q_peak/p
    assert r.rps_avg == pytest.approx(177_900)  # (W/p)/t_end


def test_capacity_default_seat_view_row():
    r = cap().compute()["seat_view"]
    assert r.people == 125_000
    assert r.requests == pytest.approx(250_000)
    assert r.duration_s == pytest.approx(625)
    assert r.rps_peak == pytest.approx(400)
    assert r.rps_avg == pytest.approx(400)


def test_capacity_default_hold_row():
    r = cap().compute()["hold"]
    assert r.people == 125_000
    assert r.requests == pytest.approx(250_000)
    assert r.duration_s == pytest.approx(625)
    assert r.rps_peak == pytest.approx(400)
    assert r.rps_avg == pytest.approx(400)


def test_capacity_default_payment_row():
    r = cap().compute()["payment"]
    assert r.people == pytest.approx(50_000)  # r·A
    assert r.requests == pytest.approx(50_000)  # r·k·A (= 판매 좌석 수)
    assert r.duration_s == pytest.approx(625)
    assert r.rps_peak == pytest.approx(80)  # r·k·μ
    assert r.rps_avg == pytest.approx(80)


def test_capacity_default_db_row():
    r = cap().compute()["db"]
    assert r.people is None
    assert r.requests == pytest.approx(900_000)
    assert r.duration_s == pytest.approx(625)
    assert r.rps_peak == pytest.approx(1_440)
    assert r.rps_avg == pytest.approx(1_440)  # 900,000 / 625


def test_capacity_default_total_row():
    r = cap().compute()["total"]
    assert r.people is None
    assert r.requests == pytest.approx(112_737_500)  # 1e6 + 111,187,500 + 250,000 + 250,000 + 50,000
    assert r.duration_s == pytest.approx(625)
    assert r.rps_peak == pytest.approx(LAMBDA + 197_600 + 400 + 400 + 80)  # 215,146.67
    assert r.rps_avg == pytest.approx(180_380)  # 112,737,500 / 625


# ---- 3. 매진 상한 A = min(N, ⌈S/(r·k)⌉) ----
def test_capacity_sellout_limits_admissions_below_arrivals():
    rows = cap().compute()  # S/(r·k) = 125,000 < N
    assert rows["seat_view"].people == 125_000
    assert rows["hold"].people == 125_000
    assert rows["seat_view"].duration_s == pytest.approx(625)  # A/μ


def test_capacity_admissions_capped_at_arrivals_when_seats_plentiful():
    c = cap()
    rows = c.compute(c.Assumptions(seats=500_000))  # S/(r·k) = 1,250,000 > N → A = N
    assert rows["seat_view"].people == pytest.approx(1_000_000)
    assert rows["seat_view"].requests == pytest.approx(2_000_000)
    assert rows["hold"].requests == pytest.approx(2_000_000)
    assert rows["payment"].requests == pytest.approx(400_000)  # r·k·A
    assert rows["seat_view"].duration_s == pytest.approx(5_000)  # N/μ
    # Q_end = 0 → W = ½·T·Q_peak + (t_end−T)·Q_peak/2 = 2,470,000,000
    assert rows["poll"].requests == pytest.approx(494_000_000)
    assert rows["poll"].rps_avg == pytest.approx(98_800)


def test_capacity_sellout_uses_ceiling():
    c = cap()
    rows = c.compute(c.Assumptions(seats=50_001))  # 50,001/0.4 = 125,002.5 → 125,003
    assert rows["seat_view"].people == 125_003
    assert rows["seat_view"].requests == pytest.approx(250_006)
    assert rows["seat_view"].duration_s == pytest.approx(125_003 / 200)


def test_capacity_pay_ratio_and_payments_per_payer_change_admissions():
    c = cap()
    rows = c.compute(c.Assumptions(payments_per_payer=2))  # A = ⌈5e4/(0.4·2)⌉ = 62,500, t_end = 312.5
    assert rows["seat_view"].people == 62_500
    assert rows["seat_view"].duration_s == pytest.approx(312.5)
    assert rows["payment"].people == pytest.approx(25_000)  # r·A
    assert rows["payment"].requests == pytest.approx(50_000)  # r·k·A
    assert rows["payment"].rps_peak == pytest.approx(160)  # r·k·μ


# ---- 4. 입력에 따라 움직이는지 ----
def test_capacity_poll_interval_scales_poll_requests():
    c = cap()
    rows = c.compute(c.Assumptions(poll_interval_s=10))
    assert rows["poll"].requests == pytest.approx(55_593_750)  # W/p, W 는 그대로
    assert rows["poll"].rps_peak == pytest.approx(98_800)
    assert rows["poll"].rps_avg == pytest.approx(88_950)
    assert rows["queue_enter"].requests == pytest.approx(1_000_000)  # 폴링과 무관


def test_capacity_admit_rate_changes_duration_and_rps():
    c = cap()
    rows = c.compute(c.Assumptions(admit_rate_per_s=400))  # t_end = 312.5, Q_peak = 976,000
    assert rows["seat_view"].duration_s == pytest.approx(312.5)
    assert rows["seat_view"].rps_peak == pytest.approx(800)  # v·μ
    assert rows["hold"].rps_peak == pytest.approx(800)
    assert rows["payment"].rps_peak == pytest.approx(160)
    assert rows["poll"].rps_peak == pytest.approx(195_200)  # Q_peak/p
    # W = ½·60·976,000 + 252.5·(976,000+875,000)/2 = 262,968,750
    assert rows["poll"].requests == pytest.approx(52_593_750)
    assert rows["poll"].rps_avg == pytest.approx(168_300)
    assert rows["db"].rps_peak == pytest.approx(2_880)  # (800·2) + (800·1) + (160·3)


def test_capacity_view_and_hold_counts_change_requests():
    c = cap()
    rows = c.compute(c.Assumptions(seat_views_per_user=3, holds_per_user=1))
    assert rows["seat_view"].requests == pytest.approx(375_000)
    assert rows["seat_view"].rps_peak == pytest.approx(600)
    assert rows["hold"].requests == pytest.approx(125_000)
    assert rows["hold"].rps_peak == pytest.approx(200)
    assert rows["db"].requests == pytest.approx(375_000 * 2 + 125_000 * 1 + 50_000 * 3)


def test_capacity_db_ops_per_request_changes_db_row():
    c = cap()
    ops = {"queue_enter": 1, "poll": 0, "seat_view": 0, "hold": 0, "payment": 0}
    rows = c.compute(c.Assumptions(db_ops_per_request=ops))
    assert rows["db"].requests == pytest.approx(1_000_000)
    assert rows["db"].rps_peak == pytest.approx(LAMBDA)
    assert rows["db"].rps_avg == pytest.approx(1_000_000 / 625)


# ---- 5. 모델 범위 밖 ----
def test_capacity_arrival_rate_below_admit_rate_is_value_error():
    c = cap()
    with pytest.raises(ValueError):
        c.compute(c.Assumptions(arrivals=10_000))  # λ = 166.7 < μ = 200


def test_capacity_arrival_rate_equal_to_admit_rate_is_value_error():
    c = cap()
    with pytest.raises(ValueError):
        c.compute(c.Assumptions(arrivals=12_000))  # λ = 200 = μ


def test_capacity_sellout_before_window_end_is_value_error():
    c = cap()
    with pytest.raises(ValueError):
        c.compute(c.Assumptions(seats=1_000))  # A = 2,500, t_end = 12.5 < T = 60


def test_capacity_t_end_equal_to_window_is_allowed():
    c = cap()
    rows = c.compute(c.Assumptions(seats=4_800))  # A = 12,000, t_end = 60 = T
    assert rows["seat_view"].people == 12_000
    assert rows["seat_view"].duration_s == pytest.approx(60)
    # Q_end = Q_peak = 988,000 → W = ½·60·988,000 = 29,640,000
    assert rows["poll"].requests == pytest.approx(5_928_000)


# ---- 6. CLI ----
def test_capacity_cli_runs_and_prints_all_rows():
    proc = subprocess.run([sys.executable, "scripts/capacity.py"], cwd=ROOT,
                          capture_output=True, text=True, timeout=60)
    assert proc.returncode == 0, proc.stderr
    for row in cap().compute().values():
        assert row.label in proc.stdout, f"표에 행 이름 '{row.label}' 이 없다"
