"""US-01 공연 목록 보기 (docs/stories/US-01-list-shows.md)"""
from api_support import assert_error, parse_dt  # noqa: F401


def test_us01_ac1_lists_shows_with_id_title_venue_start(api):
    r = api.shows()
    assert r.status_code == 200, r.text
    shows = r.json()
    assert isinstance(shows, list) and len(shows) == 1  # 시드: 공연 1개
    for s in shows:
        assert isinstance(s["id"], str) and s["id"]
        assert isinstance(s["title"], str) and s["title"]
        assert isinstance(s["venue"], str) and s["venue"]
        parse_dt(s["startsAt"])
    assert len({s["id"] for s in shows}) == len(shows)


def test_us01_ac1_lists_all_shows_when_multiple(api):
    seeded = api.shows().json()
    added = api.add_shows(2)
    r = api.shows()
    assert r.status_code == 200, r.text
    shows = r.json()
    assert len(shows) == len(seeded) + 2
    by_id = {s["id"]: s for s in shows}
    assert len(by_id) == len(shows)
    for s in seeded:
        assert by_id[s["id"]] == s  # 시드 공연도 그대로
    for sid, title, venue, starts_at in added:
        got = by_id[sid]
        assert got["title"] == title and got["venue"] == venue
        assert parse_dt(got["startsAt"]) == starts_at


def test_us01_ac2_no_shows_returns_empty_list_not_error(api):
    api.empty_database()
    r = api.shows()
    assert r.status_code == 200, r.text
    assert r.json() == []


def test_us01_ac3_same_list_without_user_header(api):
    without = api.shows()
    with_header = api.shows(user="A")
    assert without.status_code == 200 and with_header.status_code == 200
    assert without.json() == with_header.json()
