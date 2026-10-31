"""Parser tests driven by fixture files.

Every *.txt in tests/fixtures/press_releases/ with an entry in expected.json is
checked field by field. Drop real releases in as real_<date>.txt, add their
values to expected.json, and they are tested automatically.
"""
import json
from datetime import date

import pytest

from conftest import FIXTURES
from pipeline.sources import press_release as pr

DIR = FIXTURES / "press_releases"
EXPECTED = {k: v for k, v in json.loads((DIR / "expected.json").read_text()).items() if not k.startswith("_")}


@pytest.mark.parametrize("name", sorted(EXPECTED))
def test_fixture(name):
    rel = pr.parse_release((DIR / name).read_text(encoding="utf-8"), date(2026, 1, 1))
    for sid, fields in EXPECTED[name].items():
        got = rel.stations[sid]
        for f, want in fields.items():
            assert getattr(got, f) == pytest.approx(want) if want is not None else getattr(got, f) is None, \
                f"{name} {sid}.{f}: got {getattr(got, f)}, want {want}"


def test_every_real_fixture_has_expected_values():
    real = sorted(p.name for p in DIR.glob("real_*.txt"))
    missing = [n for n in real if n not in EXPECTED]
    assert not missing, f"add expected values for {missing} to expected.json"


def test_broken_release_raises():
    with pytest.raises(pr.ParseError):
        pr.parse_release((DIR / "broken_no_data.txt").read_text(), date(2026, 1, 1))


def test_max_level_is_never_taken_as_present_level():
    txt = "Tarbela: Maximum Conservation Level 1550.00 feet, Present Level 1500.10 feet. Mangla: inflows 10,000 cusecs"
    rel = pr.parse_release(txt, date(2026, 1, 1))
    assert rel.stations["tarbela"].level_ft == 1500.10


def test_observation_rows_shape():
    rel = pr.parse_release((DIR / "synthetic_app_summer.txt").read_text(), date(2026, 7, 15))
    rows = rel.observation_rows()
    assert {r["station_id"] for r in rows} == {"tarbela", "mangla"}
    assert all(r["date"] == date(2026, 7, 15) and r["source"] == "wapda_press" for r in rows)


def test_html_to_text_strips_chrome():
    html = "<html><nav>Menu 999 cusecs</nav><article>Indus at Tarbela: Inflows 1,000 cusecs</article></html>"
    assert pr.html_to_text(html) == "Indus at Tarbela: Inflows 1,000 cusecs"
