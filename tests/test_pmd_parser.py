from datetime import date

from conftest import FIXTURES
from pipeline.sources import pmd


def test_month_slug_and_urls():
    assert pmd.month_slug(date(2015, 12, 1)) == "dec15"
    assert pmd.candidate_urls(date(2015, 12, 1))[0] == (
        "https://www.pmd.gov.pk/FFD/index_files/daily/damsdec15_files/sheet001.htm")
    assert len(list(pmd.iter_months(date(2010, 11, 1), date(2011, 2, 1)))) == 4


def test_real_dec2015_layout_partial_month():
    """Values copied from the real Dec 2015 sheet (only days 1-2 filled)."""
    m = pmd.parse_month_html((FIXTURES / "pmd_dec15.htm").read_bytes(), date(2015, 12, 1))
    assert m.header_month == date(2015, 12, 1)
    assert m.days_in_month == 31
    assert m.days_filled == {"tarbela": 2, "mangla": 2}
    assert m.problems == []
    by = {(r.date.day, r.station_id): r for r in m.rows}
    t1, m2 = by[(1, "tarbela")], by[(2, "mangla")]
    assert (t1.level_ft, t1.inflow_cusecs, t1.outflow_cusecs) == (1499.52, 27200, 48000)
    assert (m2.level_ft, m2.inflow_cusecs, m2.outflow_cusecs) == (1192.25, 13597, 35000)


def test_full_month_with_commas_blanks_and_typos():
    m = pmd.parse_month_html((FIXTURES / "pmd_jun22_synthetic.htm").read_bytes(), date(2022, 6, 1))
    assert m.days_in_month == 30
    # day 20 Tarbela inflow is '1O5000' (letter O): kept as None and reported
    assert m.days_filled == {"tarbela": 29, "mangla": 30}
    assert any("day 20" in p for p in m.problems)
    by = {(r.date.day, r.station_id): r for r in m.rows}
    assert by[(1, "tarbela")].outflow_cusecs == 120000  # "120,000"
    assert by[(10, "mangla")].outflow_cusecs is None     # blank cell
    assert by[(10, "mangla")].inflow_cusecs is not None


def test_month_mismatch_is_reported():
    m = pmd.parse_month_html((FIXTURES / "pmd_dec15.htm").read_bytes(), date(2015, 11, 1))
    assert any("sheet says Dec 2015" in p for p in m.problems)


def test_garbage_page_gives_no_rows():
    m = pmd.parse_month_html(b"<html><body><h1>404 Not Found</h1></body></html>", date(2020, 1, 1))
    assert m.rows == [] and m.days_filled == {"tarbela": 0, "mangla": 0}
