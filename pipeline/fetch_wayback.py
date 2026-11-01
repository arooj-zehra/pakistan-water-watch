"""Download Internet Archive snapshots of PMD's dam pages for inspection.

    python -m pipeline.probe_sources   # first: writes data/inventory/wayback_pmd.csv
    python -m pipeline.fetch_wayback

The probe found ~1,200 archived captures under PMD's FFD "daily" folder. Besides
a few monthly sheets there are repeated snapshots of pages such as tarbela.htm,
mangla.htm, damsdata.htm and the older damsMMMYY.htm. If any of these were
daily-updated tables, every snapshot can hold weeks of figures that no longer
exist anywhere else.

This saves the raw HTML of every successful capture of those pages to
data/raw/wayback/<page>/<timestamp>.htm (committed, so it never has to be
downloaded again) and prints a short summary of what each page contains.
Parsing comes after we've looked at them.
"""
from __future__ import annotations

import csv
import re
import sys
import time
from collections import Counter, defaultdict

from .config import DATA_DIR, INVENTORY_DIR

WAYBACK_CSV = INVENTORY_DIR / "wayback_pmd.csv"
OUT_DIR = DATA_DIR / "raw" / "wayback"
# Pages worth a look (lower-case file names under .../FFD/index_files/daily/)
CANDIDATES = re.compile(
    r"^(tarbela|mangla|damsdata|damfor|flowtemp|kharifseason|kharif-rabi|kabul|teledata"
    r"|dams(jan|feb|mar|apr|may|jun|jul|aug|sep|oct|nov|dec)\d\d"
    r"|dams(jan|feb|mar|apr|may|jun|jul|aug|sep|oct|nov|dec)\d\d_files/sheet\d+)\.htm$")
RETRY_WAITS_S = (15, 60)


def page_name(original: str) -> str | None:
    """'http://www.pmd.gov.pk:80/FFD/index_files/daily/tarbela.htm' -> 'tarbela.htm'"""
    path = re.sub(r"^https?://(www\.)?pmd\.gov\.pk(:80)?/ffd/index_files/daily/", "",
                  original.lower().split("?")[0])
    return path if CANDIDATES.match(path) else None


def select_captures(rows: list[dict]) -> list[tuple[str, str, str]]:
    """(page, timestamp, original) for each distinct successful capture."""
    seen, out = set(), []
    for r in rows:
        if r.get("statuscode") != "200":
            continue
        page = page_name(r["original"])
        if page and (page, r["timestamp"]) not in seen:
            seen.add((page, r["timestamp"]))
            out.append((page, r["timestamp"], r["original"]))
    return sorted(out)


def local_path(page: str, ts: str):
    return OUT_DIR / page.replace("/", "__").removesuffix(".htm") / f"{ts}.htm"


def fetch(get, ts: str, original: str, sleep=time.sleep) -> bytes:
    # "id_" asks the Wayback Machine for the original bytes, without its toolbar
    url = f"https://web.archive.org/web/{ts}id_/{original}"
    for attempt in range(len(RETRY_WAITS_S) + 1):
        r = get(url, delay=2.0, timeout=90)
        if r.status_code == 200:
            return r.content
        if r.status_code in (429, 502, 503, 504) and attempt < len(RETRY_WAITS_S):
            sleep(RETRY_WAITS_S[attempt])
            continue
        raise RuntimeError(f"HTTP {r.status_code}")
    raise RuntimeError("gave up")  # pragma: no cover


def describe(html: bytes) -> str:
    """One-line hint of what a page holds."""
    text = re.sub(r"\s+", " ", re.sub(rb"<[^>]+>", b" ", html).decode("latin-1"))
    up = text.upper()
    dates = len(re.findall(r"\b\d{1,2}[-/.](?:\d{1,2}|[A-Za-z]{3})[-/.]\d{2,4}\b", text))
    return (f"{len(html) // 1024:>4} KB, TARBELA={'y' if 'TARBELA' in up else 'n'} "
            f"MANGLA={'y' if 'MANGLA' in up else 'n'} INFLOW={'y' if 'INFLOW' in up else 'n'} "
            f"date-like strings={dates}")


def main() -> int:
    from . import http

    if not WAYBACK_CSV.exists():
        print(f"{WAYBACK_CSV} not found. Run: python -m pipeline.probe_sources")
        return 1
    with WAYBACK_CSV.open(newline="") as f:
        caps = select_captures(list(csv.DictReader(f)))
    todo = [c for c in caps if not local_path(c[0], c[1]).exists()]
    print(f"{len(caps)} captures of candidate pages; {len(caps) - len(todo)} already saved, "
          f"{len(todo)} to download (about {len(todo) * 3 // 60 + 1} min)")
    failed = 0
    for i, (page, ts, original) in enumerate(todo, 1):
        try:
            html = fetch(http.get, ts, original)
        except Exception as e:
            failed += 1
            print(f"  [{i}/{len(todo)}] {page} {ts}: FAILED {e}")
            continue
        path = local_path(page, ts)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(html)
        if i % 20 == 0 or i == len(todo):
            print(f"  [{i}/{len(todo)}] saved", flush=True)

    print("\nWhat each page looks like (newest snapshot):")
    by_page = defaultdict(list)
    for page, ts, _ in caps:
        if local_path(page, ts).exists():
            by_page[page].append(ts)
    years = Counter()
    for page in sorted(by_page):
        tss = sorted(by_page[page])
        years.update(t[:4] for t in tss)
        print(f"  {page:<34} {len(tss):>3} snapshots {tss[0][:8]}..{tss[-1][:8]}  "
              f"{describe(local_path(page, tss[-1]).read_bytes())}")
    print("\nSnapshots per year:", ", ".join(f"{y}={n}" for y, n in sorted(years.items())))
    print("\nPaste this output back into the chat.")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
