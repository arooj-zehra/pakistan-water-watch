"""Parse WAPDA's daily river & reservoir release from article text.

Outlets republish the same WAPDA numbers in slightly different words, e.g.
    "Indus at Tarbela: Inflows 30,200 cusecs and Outflows 35,000 cusecs"
    "Tarbela: Minimum Operating Level 1402.00 feet, Present Level 1520.45 feet,
     Maximum Conservation Level 1550.00 feet, Live Storage 4.512 MAF"
    "Tarbela stood at 1,439.76 feet ... with 1.261 million acre-feet"
    "Tarbela: 1,432.04 feet, 560,000 acre-feet"

Strategy: cut the text into windows, each starting at a mention of Tarbela or
Mangla and ending at the next place name, then look for each field inside the
windows with patterns ordered from most to least specific. Every value is
range-checked, so a "maximum conservation level 1550" can't be mistaken for
the present level.

The release for day D reports 24-hour means to 06:00 PST on day D, the same
convention as the PMD sheet, so both are stored under date D.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import date

from ..config import STATIONS

PLACES = [
    "Tarbela", "Mangla", "Chashma", "Nowshera", "Marala", "Jinnah", "Taunsa",
    "Panjnad", "Guddu", "Sukkur", "Kotri", "Kabul", "Chenab", "Jhelum", "Indus",
    "Islam", "Trimmu", "Balloki", "Sulemanki", "Ravi", "Sutlej",
]
_PLACE_RE = re.compile(r"\b(" + "|".join(PLACES) + r")\b", re.I)
NUM = r"(\d{1,3}(?:,\d{3})+(?:\.\d+)?|\d+(?:\.\d+)?)"
GAP = r"[^\d;]{0,40}?"  # short run of words/punctuation between label and number

INFLOW_PATS = [re.compile(r"in-?flows?" + GAP + NUM, re.I)]
OUTFLOW_PATS = [re.compile(r"out-?flows?" + GAP + NUM, re.I)]
LEVEL_PATS = [
    re.compile(r"(?:present|current|existing)\s+(?:reservoir\s+)?level" + GAP + NUM, re.I),
    re.compile(r"(?:stood|stands|was|is)\s+at" + GAP + NUM + r"\s*(?:ft|feet)", re.I),
    re.compile(r"(?<!maximum )(?<!minimum )(?<!conservation )(?<!operating )(?<!dead )"
               r"level" + GAP + NUM + r"\s*(?:ft|feet)", re.I),
    re.compile(NUM + r"\s*(?:ft|feet)", re.I),  # bare "1,432.04 feet" (range check picks it)
]
DEAD_PATS = [re.compile(r"(?:minimum\s+operating|dead)\s+level" + GAP + NUM, re.I)]
STORAGE_MAF_PATS = [
    re.compile(r"live\s+storage" + GAP + NUM + r"\s*(?:MAF|million\s+acre)", re.I),
    re.compile(NUM + r"\s*(?:MAF|million\s+acre[- ]?f(?:ee|oo)t)", re.I),
]
STORAGE_AF_PATS = [re.compile(NUM + r"\s*acre[- ]?f(?:ee|oo)t", re.I)]


class ParseError(ValueError):
    pass


@dataclass
class StationRelease:
    level_ft: float | None = None
    inflow_cusecs: float | None = None
    outflow_cusecs: float | None = None
    live_storage_maf: float | None = None
    dead_level_ft: float | None = None


@dataclass
class Release:
    date: date
    stations: dict[str, StationRelease] = field(default_factory=dict)
    missing: list[str] = field(default_factory=list)

    def observation_rows(self, source: str = "wapda_press") -> list[dict]:
        return [
            {"date": self.date, "station_id": sid, "level_ft": s.level_ft,
             "inflow_cusecs": s.inflow_cusecs, "outflow_cusecs": s.outflow_cusecs,
             "live_storage_maf": s.live_storage_maf, "source": source}
            for sid, s in self.stations.items()
        ]


def _f(s: str) -> float:
    return float(s.replace(",", ""))


def _windows(text: str, station: str) -> list[str]:
    """Every chunk of text from a mention of `station` to the next place name."""
    out = []
    marks = list(_PLACE_RE.finditer(text))
    for i, m in enumerate(marks):
        if m.group(1).lower() != station.lower():
            continue
        end = len(text)
        for nxt in marks[i + 1:]:
            # "Indus at Tarbela" / "Jhelum at Mangla": a river name right before
            # the station belongs to it, so only stop at a *different* place
            if nxt.group(1).lower() != station.lower():
                end = nxt.start()
                break
        out.append(text[m.end():end])
    return out


def _first(windows: list[str], pats: list[re.Pattern], ok) -> float | None:
    for pat in pats:  # most specific pattern first, across all windows
        for w in windows:
            for m in pat.finditer(w):
                v = _f(m.group(1))
                if ok(v):
                    return v
    return None


def parse_release(text: str, release_date: date) -> Release:
    text = re.sub(r"\s+", " ", text)
    rel = Release(date=release_date)
    for sid in ("tarbela", "mangla"):
        st = STATIONS[sid]
        wins = _windows(text, st.name)
        if not wins:
            rel.missing.append(f"{sid}: not mentioned")
            continue
        level_ok = lambda v, st=st: st.plausible_min_ft <= v <= st.plausible_max_ft and abs(v - st.max_level_ft) > 0.001
        flow_ok = lambda v: 0 <= v <= 1_500_000
        sr = StationRelease(
            inflow_cusecs=_first(wins, INFLOW_PATS, flow_ok),
            outflow_cusecs=_first(wins, OUTFLOW_PATS, flow_ok),
            dead_level_ft=_first(wins, DEAD_PATS, lambda v, st=st: st.plausible_min_ft <= v <= st.plausible_max_ft),
            live_storage_maf=_first(wins, STORAGE_MAF_PATS, lambda v: 0 <= v <= 15),
        )
        # Level: exclude the dead level value from the bare-number fallback
        sr.level_ft = _first(wins, LEVEL_PATS, lambda v: level_ok(v) and v != sr.dead_level_ft)
        if sr.live_storage_maf is None:
            af = _first(wins, STORAGE_AF_PATS, lambda v: 1_000 <= v <= 15_000_000)
            sr.live_storage_maf = round(af / 1e6, 3) if af is not None else None
        for f in ("level_ft", "inflow_cusecs", "outflow_cusecs", "live_storage_maf"):
            if getattr(sr, f) is None:
                rel.missing.append(f"{sid}.{f}")
        rel.stations[sid] = sr
    if not any(s.inflow_cusecs is not None or s.level_ft is not None for s in rel.stations.values()):
        raise ParseError("no Tarbela/Mangla level or inflow found; format may have changed")
    return rel


def html_to_text(html: str | bytes) -> str:
    """Article body text from a news page (drops scripts, nav, footer)."""
    from bs4 import BeautifulSoup

    soup = BeautifulSoup(html, "lxml")
    for tag in soup(["script", "style", "nav", "header", "footer", "aside", "form"]):
        tag.decompose()
    body = soup.find("article") or soup.body or soup
    return body.get_text(" ", strip=True)
