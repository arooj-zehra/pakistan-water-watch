import csv
from datetime import date
from types import SimpleNamespace

from sqlalchemy import select

from conftest import FIXTURES
from pipeline import backfill_news as bn
from pipeline import daily, db, probe_news

IRSA = (FIXTURES / "press_releases" / "synthetic_irsa.txt").read_text(encoding="utf-8").split("\n", 1)[1]
FULL = {"id": 77, "date": "2026-10-31T16:00:00", "link": "https://app/77",
        "title": {"rendered": "IRSA releases"}, "content": {"rendered": f"<p>ISLAMABAD, Oct 31 (APP): {IRSA}</p>"}}


def fake_site(calls):
    def get(url, **kw):
        p = kw.get("params", {})
        calls.append(p)
        if "include" in p:
            return SimpleNamespace(status_code=200, json=lambda: [FULL], headers={})
        if p.get("per_page") == 1:  # endpoint check
            return SimpleNamespace(status_code=200, json=lambda: [{"id": 1}], headers={})
        listing = [{k: FULL[k] for k in ("id", "date", "link", "title")}]
        return SimpleNamespace(status_code=200, json=lambda: listing, headers={"X-WP-TotalPages": "1"})
    return get


def test_step_news_fetches_recent_posts_and_grows_history(engine, tmp_path, monkeypatch):
    monkeypatch.setattr(bn, "DATA_DIR", tmp_path)
    monkeypatch.setattr(bn, "NEWS_INVENTORY", tmp_path / "news")
    monkeypatch.setattr(bn, "LEGACY_APP_LIST", tmp_path / "none.csv")
    (tmp_path / "news").mkdir()
    (tmp_path / "news" / "app_posts.csv").write_text("id,date,title,link\n1,2026-10-01,t,l\n")
    calls = []
    with engine.begin() as c:
        status, n, err = daily.step_news(c, date(2026, 10, 31), get=fake_site(calls))
        got = c.execute(select(db.observations)).mappings().all()
    assert (status, n) == ("ok", 2), err
    assert {r["date"] for r in got} == {date(2026, 10, 31)}
    assert any(p.get("after") == "2026-10-27T00:00:00" for p in calls)
    # the new post joins the raw cache and the post list, so rebuilds include it
    assert 77 in bn.load_cache("app")
    with (tmp_path / "news" / "app_posts.csv").open() as f:
        assert [r["id"] for r in csv.DictReader(f)] == ["1", "77"]


def test_probe_outlet_and_extra_days(tmp_path, monkeypatch):
    monkeypatch.setattr(probe_news, "NEWS_INVENTORY", tmp_path)
    from pipeline.sources import wordpress as wp

    posts, status = probe_news.probe_outlet(fake_site([]), wp.OUTLET_BY_KEY["profit"])
    assert status == "ok" and [p["id"] for p in posts] == [77]
    probe_news.save_posts("profit", posts)
    assert probe_news.load_posts("profit")[0]["id"] == "77"
    assert probe_news.days_by_year(posts) == {"2026": {"2026-10-31"}}


def test_probe_outlet_reports_missing_api():
    from pipeline.sources import wordpress as wp

    posts, status = probe_news.probe_outlet(
        lambda *a, **k: SimpleNamespace(status_code=403, json=lambda: {}, headers={}), wp.OUTLET_BY_KEY["nation"])
    assert posts == [] and "no WordPress API" in status
