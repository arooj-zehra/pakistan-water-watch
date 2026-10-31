"""PMD Flood Forecasting Division monthly Tarbela/Mangla sheets.

Each month is an Excel-exported HTML page, e.g.
    https://www.pmd.gov.pk/FFD/index_files/daily/damsdec15_files/sheet001.htm

Layout (verified against Dec 2015):
    TARBELA & MANGLA DAMS / DAILY LEVEL, INFLOW & OUTFLOW / December,2015
    DATE | Tarbela: Level (ft), Mean Inflow (cusecs), Mean Outflow (cusecs)
         | Mangla:  Level (ft), Mean Inflow (cusecs), Mean Outflow (cusecs)
    Unfilled days show only the day number with blank cells.
    Footnote: means are calculated at 0600 Hrs PST.
"""
from __future__ import annotations

import calendar
import re
from dataclasses import dataclass, field
from datetime import date
from pathlib import Path

from bs4 import BeautifulSoup

from ..config import RAW_PMD_DIR

BASE = "https://www.pmd.gov.pk/FFD/index_files/daily"
# Candidate URL templates, tried in order. Only the first is verified; the
# others are cheap guesses in case some years were exported differently.
URL_TEMPLATES = [
    BASE + "/dams{mon}{yy}_files/sheet001.htm",
    BASE + "/Dams{Mon}{yy}_files/sheet001.htm",
]

MONTH_NAMES = {name.lower(): i for i, name in enumerate(calendar.month_name) if name}


def month_slug(d: date) -> str:
    """date(2015, 12, 1) -> 'dec15'"""
    return f"{calendar.month_abbr[d.month].lower()}{d.year % 100:02d}"


def candidate_urls(d: date) -> list[str]:
    mon = calendar.month_abbr[d.month]
    return [t.format(mon=mon.lower(), Mon=mon, yy=f"{d.year % 100:02d}") for t in URL_TEMPLATES]


def raw_path(d: date) -> Path:
    return RAW_PMD_DIR / f"dams{month_slug(d)}.htm"


def iter_months(start: date, end: date):
    y, m = start.year, start.month
    while (y, m) <= (end.year, end.month):
        yield date(y, m, 1)
        y, m = (y + 1, 1) if m == 12 else (y, m + 1)


# ---------------------------------------------------------------------------
# Parsing
# ---------------------------------------------------------------------------
@dataclass
class PmdRow:
    date: date
    station_id: str
    level_ft: float | None
    inflow_cusecs: float | None
    outflow_cusecs: float | None


@dataclass
class PmdMonth:
    month: date
    rows: list[PmdRow] = field(default_factory=list)
    days_in_month: int = 0
    days_filled: dict[str, int] = field(default_factory=dict)  # per station
    header_month: date | None = None  # month stated inside the sheet
    problems: list[str] = field(default_factory=list)  # unparseable cells etc.


_NUM_RE = re.compile(r"^-?\d+(?:\.\d+)?$")


def _num(cell: str) -> float | None:
    s = cell.replace(",", "").replace("\xa0", " ").strip()
    if s in ("", "-", "--", "—", "N/A", "n/a", "NA"):
        return None
    return float(s) if _NUM_RE.match(s) else float("nan")


def _header_month(text: str) -> date | None:
    """Find e.g. 'December,2015' / 'December 2015' in the sheet text."""
    m = re.search(r"\b(" + "|".join(MONTH_NAMES) + r")\s*[,\-]?\s*(\d{4})\b", text, re.I)
    if not m:
        return None
    return date(int(m.group(2)), MONTH_NAMES[m.group(1).lower()], 1)


def parse_month_html(html: str | bytes, month: date) -> PmdMonth:
    """Parse one monthly sheet. `month` is the month we requested (from the URL).

    Never raises on bad cells: they are recorded in `problems` so the probe can
    report them and validation can quarantine them.
    """
    soup = BeautifulSoup(html, "lxml")
    out = PmdMonth(month=month, days_in_month=calendar.monthrange(month.year, month.month)[1])
    out.header_month = _header_month(soup.get_text(" "))
    if out.header_month and out.header_month != month:
        out.problems.append(f"sheet says {out.header_month:%b %Y} but URL is {month:%b %Y}")

    filled = {"tarbela": 0, "mangla": 0}
    for tr in soup.find_all("tr"):
        cells = [td.get_text(" ", strip=True) for td in tr.find_all(["td", "th"])]
        # Drop leading empty spacer cells Excel sometimes emits
        while cells and cells[0] == "":
            cells.pop(0)
        if not cells or not re.fullmatch(r"\d{1,2}", cells[0]):
            continue
        day = int(cells[0])
        if not 1 <= day <= 31:
            continue
        values = cells[1:7] + [""] * max(0, 7 - len(cells))
        if all(v.strip() == "" for v in values):
            continue  # unfilled day
        if day > out.days_in_month:
            out.problems.append(f"day {day} has data but {month:%b %Y} has {out.days_in_month} days")
            continue
        nums = [_num(v) for v in values[:6]]
        bad = [values[i] for i, n in enumerate(nums) if n is not None and n != n]  # NaN
        if bad:
            out.problems.append(f"day {day}: unparseable cell(s) {bad}")
            nums = [None if (n is not None and n != n) else n for n in nums]
        d = date(month.year, month.month, day)
        for sid, (lvl, inf, outf) in (("tarbela", nums[0:3]), ("mangla", nums[3:6])):
            if lvl is None and inf is None and outf is None:
                continue
            out.rows.append(PmdRow(d, sid, lvl, inf, outf))
            if inf is not None:
                filled[sid] += 1
    out.days_filled = filled
    return out
