from types import SimpleNamespace

from pipeline import probe_sources as ps


def _r(status, payload, headers=None):
    return SimpleNamespace(status_code=status, json=lambda: payload, headers=headers or {},
                           raise_for_status=lambda: None)


def test_wayback_summary_counts_distinct_ok_sheets():
    cdx = [["original", "timestamp", "statuscode", "mimetype"],
           ["http://www.pmd.gov.pk/FFD/index_files/daily/damsjan16_files/sheet001.htm", "20160203", "200", "text/html"],
           ["http://pmd.gov.pk/FFD/index_files/daily/damsjan16_files/sheet001.htm", "20160301", "200", "text/html"],
           ["http://pmd.gov.pk/FFD/index_files/daily/damsfeb16_files/sheet001.htm", "20160301", "404", "text/html"],
           ["http://pmd.gov.pk/FFD/index_files/daily/damsmar17_files/sheet001.htm", "20170401", "200", "text/html"],
           ["http://pmd.gov.pk/FFD/index_files/daily/damsmar17.htm", "20170401", "200", "text/html"]]
    caps = ps.wayback_pmd(lambda *a, **k: _r(200, cdx))
    months, per_year = ps.summarise_wayback(caps)
    assert months == ["jan16", "mar17"]
    assert per_year == {"2016": 1, "2017": 1}


def test_app_search_pages_until_done():
    pages = {1: [{"id": 1, "date": "2024-05-01T10:00:00", "link": "u1", "title": {"rendered": "IRSA releases 1 cusecs"}}],
             2: [{"id": 2, "date": "2023-05-01T10:00:00", "link": "u2", "title": {"rendered": "IRSA <b>releases</b>"}}]}

    def get(url, **kw):
        return _r(200, pages[kw["params"]["page"]], {"X-WP-TotalPages": "2"})

    posts, status = ps.app_search(get, "IRSA releases cusecs")
    assert status == "ok" and [p["id"] for p in posts] == [1, 2]
    assert posts[1]["title"] == "IRSA releases" and posts[0]["date"] == "2024-05-01"


def test_app_search_reports_blocked_api():
    posts, status = ps.app_search(lambda *a, **k: _r(403, {}), "x")
    assert posts == [] and status == "HTTP 403"
