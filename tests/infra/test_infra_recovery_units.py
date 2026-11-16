"""복구 실패가 은폐되지 않는지 단위 확인(인프라 없이 실행). compose·응답 확인을 가짜로 바꿔 Infra 의 복구 로직만 검사한다."""
import subprocess

import pytest

import infra_support
from infra_support import Infra


def cp(code=0, out="", err=""):
    return subprocess.CompletedProcess([], code, out, err)


def fresh(stopped=("app2",)):
    infra = Infra()
    infra._touched = True
    infra.stopped = set(stopped)
    return infra


def test_infra_unit_restore_apps_failure_raises_with_app_name_and_keeps_tracking(monkeypatch):
    infra = fresh()
    monkeypatch.setattr(infra_support, "compose", lambda *a, **k: cp(1, err="boom") if a[0] == "start" else cp())
    monkeypatch.setattr(Infra, "wait_all_apps_up", lambda self, timeout=60: None)
    with pytest.raises(RuntimeError, match="app2.*start 실패"):
        infra.restore_apps()
    assert infra.stopped == {"app2"}, "복구가 확인되기 전에는 stopped 추적을 지우면 안 된다"


def test_infra_unit_restore_apps_not_answering_raises(monkeypatch):
    infra = fresh()
    monkeypatch.setattr(infra_support, "compose", lambda *a, **k: cp())

    def never_up(self, timeout=60):
        raise RuntimeError("세 앱이 모두 응답하지 않았다")
    monkeypatch.setattr(Infra, "wait_all_apps_up", never_up)
    with pytest.raises(RuntimeError, match="응답 확인 실패"):
        infra.restore_apps()
    assert infra.stopped == {"app2"}


def test_infra_unit_restore_apps_success_clears_tracking(monkeypatch):
    infra = fresh()
    monkeypatch.setattr(infra_support, "compose", lambda *a, **k: cp())
    monkeypatch.setattr(Infra, "wait_all_apps_up", lambda self, timeout=60: None)
    infra.restore_apps()
    assert infra.stopped == set()


def test_infra_unit_finalize_reports_every_unrecovered_item(monkeypatch):
    infra = fresh()

    def fake(*a, **k):
        if a[0] == "start":
            return cp(1, err="no start")
        if a[0] == "up":
            return cp(1, err="up failed")
        return cp()
    monkeypatch.setattr(infra_support, "compose", fake)
    monkeypatch.setattr(Infra, "wait_all_apps_up", lambda self, timeout=60: None)
    monkeypatch.setattr(Infra, "verify_default_config", lambda self: ["app1: DATABASE_URL 이 개발 DB 가 아니다(ticketing_infra)"])
    with pytest.raises(RuntimeError) as e:
        infra.finalize()
    msg = str(e.value)
    assert "app2" in msg and "기본 설정 복구(up) 실패" in msg and "app1: DATABASE_URL" in msg, msg


def test_infra_unit_finalize_normal_path_passes(monkeypatch):
    infra = fresh()
    monkeypatch.setattr(infra_support, "compose", lambda *a, **k: cp())
    monkeypatch.setattr(Infra, "wait_all_apps_up", lambda self, timeout=60: None)
    monkeypatch.setattr(Infra, "verify_default_config", lambda self: [])
    infra.finalize()
    assert infra.stopped == set()


def test_infra_unit_finalize_does_nothing_when_infra_was_never_touched():
    infra = Infra()
    infra.finalize()  # 예외 없음
