from datetime import date
from types import SimpleNamespace

from sqlalchemy import select

from conftest import FIXTURES
from pipeline import backfill_news as bn
from pipeline import db
from pipeline.sources import wordpress as wp

IRSA = (FIXTURES / "press_releases" / "synthetic_irsa.txt").read_text(encoding="utf-8").split("\n", 1)[1]


def post(pid, posted, content, title="IRSA releases", link=None):
    return {"id": pid, "date": f"{posted}T16:20:22", "link": link or f"https://x/{pid}",
            "title": title, "content": f"<p>{content}</p>"}


# --- dates -----------------------------------------------------------------
def test_release_date_from_dateline_and_title():
    # APP dateline matches the post date
    assert bn.release_date(post(1, "2023-11-18", ""), "ISLAMABAD, Nov 18 (APP): ...") == date(2023, 11, 18)
    # aggregator posts two days late; title carries the real date
    p = post(2, "2025-04-05", "", title="River Flows and Reservoir Levels Report – April 3, 2025")
    assert bn.release_date(p, "WAPDA figures...") == date(2025, 4, 3)
    # December release posted on 1 January
    assert bn.release_date(post(3, "2026-01-01", ""), "LAHORE, Dec 31 (APP): ...") == date(2025, 12, 31)
    # implausible dateline (a week off, or in the future) falls back to the post date
    assert bn.release_date(post(4, "2024-06-10", ""), "LAHORE, Jun 1 (APP)") == date(2024, 6, 10)
    assert bn.release_date(post(5, "2024-06-10", ""), "LAHORE, Jun 12 (APP)") == date(2024, 6, 10)


# --- combining outlets ---------------------------------------------------------
def test_best_rows_combines_outlets_and_logs_conflicts():
    partial = "Indus at Tarbela: Inflows 24,200 cusecs. Jhelum at Mangla: Inflows 6,800 cusecs."
    disagreeing = IRSA.replace("24,200", "30,000")
    posts = {
        "app": [post(1, "2023-11-18", partial), post(9, "2023-11-19", "Unrelated cricket story")],
        "profit": [post(2, "2023-11-18", IRSA)],               # more complete -> wins the day
        "dailytimes": [post(3, "2023-11-18", disagreeing)],    # same completeness, lower priority
    }
    rows, stats, conflicts = bn.best_rows(posts)
    assert stats["app"]["releases"] == 1 and stats["app"]["not_a_release"] == 1
    t = next(r for r in rows if r["station_id"] == "tarbela")
    assert t["source"] == "profit" and t["inflow_cusecs"] == 24200 and t["level_ft"] == 1508.41
    assert [(c["kept_source"], c["other_source"], c["other_inflow"]) for c in conflicts] == \
        [("profit", "dailytimes", 30000)]


def test_tie_goes_to_higher_priority_outlet():
    rows, _, _ = bn.best_rows({"app": [post(1, "2023-11-18", IRSA)], "profit": [post(2, "2023-11-18", IRSA)]})
    assert {r["source"] for r in rows} == {"app"}


# --- cache, lists, loading -------------------------------------------------------
def test_cache_round_trip(tmp_path, monkeypatch):
    monkeypatch.setattr(bn, "DATA_DIR", tmp_path)
    posts = {1: post(1, "2023-11-18", "a"), 2: post(2, "2024-01-02", "b")}
    bn.save_cache("profit", posts)
    assert sorted(p.name for p in (tmp_path / "raw" / "profit").iterdir()) == ["2023.jsonl", "2024.jsonl"]
    assert bn.load_cache("profit") == posts


def test_wanted_ids_falls_back_to_legacy_app_list(tmp_path, monkeypatch):
    monkeypatch.setattr(bn, "NEWS_INVENTORY", tmp_path / "news")
    legacy = tmp_path / "app_releases.csv"
    legacy.write_text("id,date,title,link,query\n5,2024-01-01,t,l,q\n3,2023-01-01,t,l,q\n")
    monkeypatch.setattr(bn, "LEGACY_APP_LIST", legacy)
    assert bn.wanted_ids("app") == [3, 5]
    assert bn.wanted_ids("profit") == []


def test_load_rows_into_db(engine):
    rows, _, _ = bn.best_rows({"profit": [post(2, "2023-11-18", IRSA)]})
    with engine.begin() as c:
        ok, bad = bn.load_rows(c, rows)
        got = c.execute(select(db.observations)).mappings().all()
    assert (ok, bad) == (2, 0)
    assert {r["source"] for r in got} == {"profit"}


# --- WordPress client ------------------------------------------------------------
def _resp(status, payload, headers=None):
    return SimpleNamespace(status_code=status, json=lambda: payload, headers=headers or {})


def test_find_endpoint_falls_back_to_rest_route():
    def get(url, **kw):
        return _resp(404, {}) if "wp-json" in url else _resp(200, [{"id": 1}])
    assert wp.find_endpoint(get, "https://site") == "https://site/?rest_route=/wp/v2/posts"


def test_find_endpoint_rejects_html_and_errors():
    import pytest

    with pytest.raises(wp.WordPressError, match="HTTP 403"):
        wp.find_endpoint(lambda *a, **k: _resp(403, {}), "https://site")
    with pytest.raises(wp.WordPressError):
        wp.find_endpoint(lambda *a, **k: _resp(200, {"code": "rest_no_route"}), "https://site")


def test_search_pages_and_passes_after():
    pages = {1: [{"id": 1, "date": "2024-05-01T10:00:00", "link": "u1", "title": {"rendered": "A <b>b</b>"}}],
             2: [{"id": 2, "date": "2024-04-01T10:00:00", "link": "u2", "title": {"rendered": "C"}}]}
    seen = []

    def get(url, **kw):
        seen.append(kw["params"].get("after"))
        return _resp(200, pages[kw["params"]["page"]], {"X-WP-TotalPages": "2"})

    out = wp.search(get, "https://e", "Tarbela cusecs", after="2024-01-01T00:00:00")
    assert [p["id"] for p in out] == [1, 2] and out[0]["title"] == "A b"
    assert seen == ["2024-01-01T00:00:00"] * 2


def test_fetch_batches():
    sizes = []

    def get(url, **kw):
        ids = kw["params"]["include"].split(",")
        sizes.append(len(ids))
        return _resp(200, [{"id": int(i), "date": "2024-01-01T00:00:00", "link": "l",
                            "title": {"rendered": "t"}, "content": {"rendered": "c"}} for i in ids])

    out = wp.fetch(get, "https://e", list(range(230)))
    assert sizes == [100, 100, 30] and out[0]["content"] == "c"
