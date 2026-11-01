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


def test_full_dam_at_maximum_level_is_accepted():
    """Tarbela really was at 1550.00 ft (its maximum) in Sep 2015."""
    txt = ("Tarbela: Minimum Operating Level 1402.00 feet, Present Level 1550.00 feet, "
           "Maximum Conservation Level 1550.00 feet, Live Storage 5.912 MAF; Mangla: inflows 10,000 cusecs")
    assert pr.parse_release(txt, date(2026, 1, 1)).stations["tarbela"].level_ft == 1550.0


def test_observation_rows_shape():
    rel = pr.parse_release((DIR / "synthetic_app_summer.txt").read_text(), date(2026, 7, 15))
    rows = rel.observation_rows()
    assert {r["station_id"] for r in rows} == {"tarbela", "mangla"}
    assert all(r["date"] == date(2026, 7, 15) and r["source"] == "wapda_press" for r in rows)


def test_html_to_text_strips_chrome():
    html = "<html><nav>Menu 999 cusecs</nav><article>Indus at Tarbela: Inflows 1,000 cusecs</article></html>"
    assert pr.html_to_text(html) == "Indus at Tarbela: Inflows 1,000 cusecs"


# Real sentence shapes from APP's IRSA stories (2019-2022), Mangla part only.
APP_MANGLA = ("The water level in the Jhelum River at Mangla Dam was {lvl} feet, which was 42.02 feet "
              "higher than its dead level of 1050 feet whereas the inflow and outflow of water was {flows}. "
              "The release of water at Kalabagh, Taunsa and Sukkur was recorded as 30151, 34414 and 20000 cusecs.")


@pytest.mark.parametrize("flows,inflow,outflow", [
    ("recorded a 20588 and 36757 cusecs, respectively", 20588, 36757),   # cusecs only after 2nd number
    ("recorded 6,800 cusecs and 32,000 cusecs, respectively", 6800, 32000),
    ("recorded as 57,000 cusecs each", 57000, 57000),                    # 'each' = both equal
    ("recorded as 4,8461 cusecs and 12,000 cusecs respectively", None, None),  # typo: reject, don't guess
])
def test_irsa_inflow_outflow_shapes(flows, inflow, outflow):
    rel = pr.parse_release(APP_MANGLA.format(lvl="1092.20", flows=flows), date(2022, 3, 4))
    m = rel.stations["mangla"]
    assert (m.inflow_cusecs, m.outflow_cusecs) == (inflow, outflow)
    assert m.level_ft == 1092.20 and m.dead_level_ft == 1050.0


def test_outflow_after_while_and_spaced_thousands():
    # Real APP shapes: 2022-12-05 (Tarbela) and 2022-08-24 (Tarbela/Mangla)
    t1 = ("water level in River Indus at Tarbela Dam was 1504.63 feet and was 106.63 feet higher than its dead "
          "level of 1,398 feet. Water inflow and outflow in the dam was recorded as 22,500 cusecs while outflow "
          "as 46,000 cusecs. The water level in River Jhelum at Mangla Dam was 1133.00 feet.")
    t = pr.parse_release(t1, date(2022, 12, 5)).stations["tarbela"]
    assert (t.inflow_cusecs, t.outflow_cusecs) == (22500, 46000)
    t2 = ("water level in River Indus at Tarbela Dam was 1550 feet, which was 152 feet higher than its dead level "
          "of 1,398 feet. Water inflow in the dam was recorded 224, 700 cusecs and outflow 224, 700 cusecs. The "
          "water level in River Jhelum at Mangla Dam was 1178.45 feet. The inflow and outflow of water was "
          "recorded 25, 438 cusecs and 10,000 cusecs respectively.")
    rel = pr.parse_release(t2, date(2022, 8, 24))
    assert (rel.stations["tarbela"].inflow_cusecs, rel.stations["tarbela"].outflow_cusecs) == (224700, 224700)
    assert (rel.stations["mangla"].inflow_cusecs, rel.stations["mangla"].outflow_cusecs) == (25438, 10000)
