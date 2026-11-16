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


# ---- verify_default_config: 개발 Redis(DB 0, 'tkt:') 복구 확인 (인프라 없이, compose 를 가짜로) ----
def fake_compose(db_url="postgresql://ticketing:ticketing@db:5432/ticketing", redis_url="redis://redis:6379/0",
                 prefix="tkt:"):
    import json

    def fake(*a, **k):
        if a[0] == "ps":
            rows = [{"Service": s, "State": "running", "Health": "healthy"} for s in ("app1", "app2", "app3", "nginx")]
            return cp(0, "\n".join(json.dumps(r) for r in rows))
        if a[0] == "exec":
            return cp(0, {"DATABASE_URL": db_url, "REDIS_URL": redis_url, "REDIS_KEY_PREFIX": prefix}[a[-1]] + "\n")
        return cp()
    return fake


def test_infra_unit_verify_default_config_accepts_dev_defaults(monkeypatch):
    monkeypatch.setattr(infra_support, "compose", fake_compose())
    assert Infra().verify_default_config() == []


def test_infra_unit_verify_default_config_reports_test_redis_db_and_prefix_left_behind(monkeypatch):
    monkeypatch.setattr(infra_support, "compose",
                        fake_compose(redis_url="redis://redis:6379/3", prefix="infratest-1234abcd:"))
    problems = Infra().verify_default_config()
    assert sum("REDIS_URL" in p for p in problems) == 3, problems  # 앱 3대 모두
    assert sum("REDIS_KEY_PREFIX" in p for p in problems) == 3, problems


def test_infra_unit_verify_default_config_still_reports_test_database_left_behind(monkeypatch):
    monkeypatch.setattr(infra_support, "compose",
                        fake_compose(db_url="postgresql://ticketing:ticketing@db:5432/ticketing_infra"))
    problems = Infra().verify_default_config()
    assert sum("DATABASE_URL" in p for p in problems) == 3, problems


def test_infra_unit_delete_redis_keys_never_raises_when_redis_is_unreachable(monkeypatch):
    monkeypatch.setattr(infra_support, "REDIS_HOST_URL", "redis://127.0.0.1:1")
    Infra().delete_redis_keys()  # 예외 없음
