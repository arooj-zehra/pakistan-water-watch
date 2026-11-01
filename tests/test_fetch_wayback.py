from types import SimpleNamespace

from pipeline import fetch_wayback as fw


def test_page_name_filters_candidates():
    base = "http://www.pmd.gov.pk:80/FFD/index_files/daily/"
    assert fw.page_name(base + "tarbela.htm") == "tarbela.htm"
    assert fw.page_name(base + "damsMar14.htm") == "damsmar14.htm"
    assert fw.page_name(base + "damsjan15_files/sheet001.htm") == "damsjan15_files/sheet001.htm"
    assert fw.page_name(base + "rainfalljan15.htm") is None
    assert fw.page_name(base + "damsjan15_files/filelist.xml") is None


def test_select_captures_dedupes_and_keeps_200s():
    base = "http://pmd.gov.pk/FFD/index_files/daily/"
    rows = [{"original": base + "tarbela.htm", "timestamp": "20170101", "statuscode": "200"},
            {"original": "http://www." + base[7:] + "tarbela.htm", "timestamp": "20170101", "statuscode": "200"},
            {"original": base + "tarbela.htm", "timestamp": "20170202", "statuscode": "404"},
            {"original": base + "mangla.htm", "timestamp": "20180101", "statuscode": "200"}]
    assert fw.select_captures(rows) == [("mangla.htm", "20180101", base + "mangla.htm"),
                                        ("tarbela.htm", "20170101", base + "tarbela.htm")]


def test_fetch_uses_raw_id_url_and_retries():
    calls = []
    replies = iter([SimpleNamespace(status_code=503), SimpleNamespace(status_code=200, content=b"<html>")])

    def get(url, **kw):
        calls.append(url)
        return next(replies)

    assert fw.fetch(get, "20170101", "http://pmd.gov.pk/x.htm", sleep=lambda s: None) == b"<html>"
    assert calls[0] == "https://web.archive.org/web/20170101id_/http://pmd.gov.pk/x.htm" and len(calls) == 2


def test_describe():
    html = b"<table><tr><td>TARBELA</td><td>Inflow</td><td>01-05-2017</td></tr></table>"
    d = fw.describe(html)
    assert "TARBELA=y" in d and "INFLOW=y" in d and "date-like strings=1" in d
