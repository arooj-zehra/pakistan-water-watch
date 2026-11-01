import gzip
from types import SimpleNamespace

from conftest import FIXTURES
from pipeline import backfill_html as bh
from pipeline import backfill_news as bn
from pipeline import db, probe_sitemaps
from pipeline.sources import sitemaps as sm

IRSA = (FIXTURES / "press_releases" / "synthetic_irsa.txt").read_text(encoding="utf-8").split("\n", 1)[1]

INDEX = b"""<?xml version="1.0"?><sitemapindex xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">
<sitemap><loc>https://s.pk/sitemap-2016-01.xml</loc><lastmod>2016-01-31</lastmod></sitemap>
<sitemap><loc>https://s.pk/sitemap-2024-01.xml.gz</loc><lastmod>2024-01-31</lastmod></sitemap>
<sitemap><loc>https://s.pk/private/sitemap-x.xml</loc></sitemap>
</sitemapindex>"""
URLSET = b"""<?xml version="1.0"?><urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9"
 xmlns:news="http://www.google.com/schemas/sitemap-news/0.9">
<url><loc>https://s.pk/news/1/irsa-releases-34900-cusecs-water</loc><lastmod>2024-01-14T16:00:00+05:00</lastmod></url>
<url><loc>https://s.pk/news/2/cricket-team-wins</loc><lastmod>2024-01-14</lastmod></url>
<url><loc>https://s.pk/news/3/a</loc><news:news><news:title>Rivers flow, reservoirs level report</news:title>
<news:publication_date>2024-01-15</news:publication_date></news:news></url>
<url><loc>https://s.pk/private/irsa-secret</loc><lastmod>2024-01-16</lastmod></url>
</urlset>"""
ROBOTS = "User-agent: *\nDisallow: /private/\nSitemap: https://s.pk/sitemap_index.xml\n"


def fake_get(log):
    def get(url, **kw):
        log.append(url)
        if url.endswith("robots.txt"):
            return SimpleNamespace(status_code=200, text=ROBOTS, content=ROBOTS.encode())
        if url.endswith("sitemap_index.xml"):
            return SimpleNamespace(status_code=200, content=INDEX)
        if url.endswith("2024-01.xml.gz"):
            return SimpleNamespace(status_code=200, content=gzip.compress(URLSET))
        return SimpleNamespace(status_code=404, content=b"")
    return get


def test_parse_sitemap_kinds_and_news_titles():
    kind, entries = sm.parse_sitemap(INDEX)
    assert kind == "index" and len(entries) == 3
    kind, entries = sm.parse_sitemap(gzip.compress(URLSET))
    assert kind == "urlset"
    third = entries[2]
    assert third["title"] == "Rivers flow, reservoirs level report" and third["lastmod"] == "2024-01-15"


def test_filters():
    assert sm.too_old({"loc": "https://s.pk/sitemap-2016-01.xml", "lastmod": "2016-01-31"})
    assert not sm.too_old({"loc": "https://s.pk/post-sitemap3.xml", "lastmod": ""})
    assert sm.looks_like_report({"loc": "https://s.pk/n/irsa-releases-34900-cusecs-water", "title": ""})
    assert sm.looks_like_report({"loc": "https://s.pk/n/123", "title": "Rivers flow, reservoirs level report"})
    assert not sm.looks_like_report({"loc": "https://s.pk/n/cricket-team-wins", "title": ""})
    # false positives seen in UrduPoint's sitemaps
    for slug in ("lok-virsa-to-screen-film-sacchai-on-saturda-190348.html", "irsaal-name-meaning-in-english-88540.html",
                 "new-reservoirs-of-oil-and-gas-explored-in-pak-1312.html"):
        assert not sm.looks_like_report({"loc": "https://www.urdupoint.com/en/x/" + slug, "title": ""}), slug
    for slug in ("irsa-releases-83800-cusecs-water-1594773.html", "indus-river-system-authority-irsa-releases-838451.html",
                 "rivers-flow-reserviors-level-report-9420.html", "wapda-issues-water-position-in-rivers"):
        assert sm.looks_like_report({"loc": "https://www.urdupoint.com/en/pakistan/" + slug, "title": ""}), slug


def test_walk_respects_robots_and_skips_old_sitemaps():
    log = []
    site = sm.Site("s", "S", "https://s.pk")
    entries, status = sm.walk(fake_get(log), site, log=lambda *_: None)
    assert status == "ok"
    assert [e["loc"].rsplit("/", 1)[-1] for e in entries] == ["irsa-releases-34900-cusecs-water", "a"]
    assert not any("2016-01" in u or "/private/" in u for u in log)  # too old / disallowed: never fetched


def test_probe_candidate_day_and_known_days(tmp_path, monkeypatch):
    assert probe_sitemaps.candidate_day({"loc": "https://x/2024/01/15/irsa", "lastmod": "2024-02-01"}) == "2024-01-15"
    assert probe_sitemaps.candidate_day({"loc": "https://x/n/1", "lastmod": "2024-01-14T16:00:00+05:00"}) == "2024-01-14"
    parsed = tmp_path / "news_parsed.csv"
    parsed.write_text("date,station_id,inflow_cusecs\n2024-01-14,tarbela,1\n2024-01-14,mangla,2\n2024-01-15,tarbela,3\n")
    monkeypatch.setattr(probe_sitemaps, "NEWS_PARSED", parsed)
    assert probe_sitemaps.known_days() == {"2024-01-14"}  # 15th has only one dam


ARTICLE = f"""<html><head><title>Site | IRSA releases</title>
<meta property="og:title" content="IRSA releases 89,400 cusecs water">
<script type="application/ld+json">{{"@type":"NewsArticle","datePublished":"2023-11-18T16:20:00+05:00"}}</script>
</head><body><nav>Home Sports 999 cusecs</nav><article><p>ISLAMABAD, Nov 18: {IRSA}</p></article></body></html>"""


def test_page_to_post_extracts_title_date_and_article_text():
    p = bh.page_to_post("https://s.pk/news/9", ARTICLE, {"lastmod": "2023-11-20"})
    assert p["id"] == bh.post_id("https://s.pk/news/9") and isinstance(p["id"], int)
    assert p["title"] == "IRSA releases 89,400 cusecs water"
    assert p["date"] == "2023-11-18T16:20:00"
    assert "Tarbela" in p["content"] and "Sports" not in p["content"] and "<" not in p["content"]
    rel = bn.parse_post(p)
    assert rel.date.isoformat() == "2023-11-18" and rel.stations["tarbela"].inflow_cusecs == 24200


def test_backfill_news_merges_sitemap_site_caches(tmp_path, monkeypatch):
    monkeypatch.setattr(bn, "DATA_DIR", tmp_path)
    monkeypatch.setattr(bn, "NEWS_INVENTORY", tmp_path / "news")
    monkeypatch.setattr(bn, "LEGACY_APP_LIST", tmp_path / "none.csv")
    for name in ("PARSED_CSV", "CONFLICTS_CSV"):
        monkeypatch.setattr(bn, name, tmp_path / f"{name}.csv")
    eng = db.get_engine("sqlite:///:memory:")
    monkeypatch.setattr(db, "get_engine", lambda *a, **k: eng)
    p = bh.page_to_post("https://www.brecorder.com/news/9", ARTICLE, {})
    bn.save_cache("brecorder", {p["id"]: p})
    assert bn.main([]) == 0
    with eng.connect() as c:
        rows = c.execute(db.observations.select()).mappings().all()
    assert {(r["source"], r["station_id"]) for r in rows} == {("brecorder", "tarbela"), ("brecorder", "mangla")}


def test_select_todo_newest_uncovered_first():
    entries = [{"loc": "https://s.pk/a/irsa-releases-1-cusecs", "lastmod": "2024-01-10"},
               {"loc": "https://s.pk/b/irsa-releases-2-cusecs", "lastmod": "2025-11-02"},
               {"loc": "https://s.pk/c/irsa-releases-3-cusecs", "lastmod": "2023-05-05"},   # covered already
               {"loc": "https://s.pk/d/irsa-releases-4-cusecs", "lastmod": "2016-03-01"},   # too old
               {"loc": "https://s.pk/e/lok-virsa-film", "lastmod": "2025-01-01"},          # not a report
               {"loc": "https://s.pk/f/irsa-releases-5-cusecs", "lastmod": "2024-02-01"}]  # cached
    cache = {bh.post_id("https://s.pk/f/irsa-releases-5-cusecs"): {}}
    todo = bh.select_todo(entries, cache, {"2023-05-05"})
    assert [e["loc"][-19:] for e in todo] == ["irsa-releases-2-cusecs"[-19:], "irsa-releases-1-cusecs"[-19:]]
    assert len(bh.select_todo(entries, cache, {"2023-05-05"}, include_covered=True)) == 3
