"""Phase 5: go through the review queue in the terminal and decide each item.

    python -m pipeline.review                    # everything waiting, newest first
    python -m pipeline.review --kind quarantine  # only quarantined stories
    python -m pipeline.review --limit 10

For each suspicious value you see the nearby days and choose:
    b  the value is wrong: drop it        o  it is right as reported
    s  set the right value                Enter  skip for now     q  save and quit
Quarantined stories:  a accept as reported, s set a value, d keep it rejected.
Outlet disagreements: k keep the figure used, u use the other outlet's, b both wrong.

Each decision is saved at once to data/review/decisions.csv, so quitting
loses nothing. Decisions apply wherever the data is read (website files, API,
model training, forecast scoring). Afterwards run:
    python -m pipeline.export_static
and commit data/review/decisions.csv with the website files.
"""
from __future__ import annotations

import argparse
import sys
from datetime import date

from . import anomaly as A

FMT = {"inflow_cusecs": "{:,.0f}", "outflow_cusecs": "{:,.0f}", "level_ft": "{:,.2f}"}


def fmt(field: str, v) -> str:
    return "–" if v is None else FMT[field].format(v)


def neighbours(item: dict, around: int = 5) -> str:
    ctx = item.get("context")
    if not ctx:
        return ""
    parts = []
    for d, v in zip(ctx["dates"], ctx["values"]):
        gap = abs((date.fromisoformat(d) - date.fromisoformat(item["date"])).days)
        if gap <= around and v is not None:
            s = f"{d[5:]} {fmt(item['field'], v)}"
            parts.append(f"[{s}]" if d == item["date"] else s)
    return "  |  ".join(parts)


def ask(prompt: str, choices: str, read=input) -> str:
    while True:
        a = read(prompt).strip().lower()
        if a == "" or a in choices:
            return a
        print(f"  Type one of: {', '.join(choices)} (or press Enter to skip)")


def ask_value(field: str, read=input) -> float | None:
    while True:
        a = read(f"  Correct {A.FIELD_LABELS[field]} ({A.UNITS[field]}), or Enter to cancel: ").strip().replace(",", "")
        if not a:
            return None
        try:
            return float(a)
        except ValueError:
            print("  That isn't a number.")


def decide(item: dict, read=input) -> list[dict] | None:
    """Show one item and return the decision rows (None = skipped, 'quit' = stop)."""
    today = date.today().isoformat()
    base = {"kind": item["kind"], "ref": item["ref"], "decided_on": today}
    if item["kind"] == "anomaly":
        f = item["field"]
        print(f"\n{item['station_id'].title()} {A.FIELD_LABELS[f]} on {item['date']}: reported "
              f"{fmt(f, item['value'])} {A.UNITS[f]}, expected about {fmt(f, item['expected'])} "
              f"(checks: {', '.join(item['checks']) or 'local, extreme'})")
        print("  nearby: " + neighbours(item))
        a = ask("  [b] wrong  [o] right  [s] set value  [Enter] skip  [q] quit: ", "bosq", read)
        if a == "q":
            return "quit"
        row = {**base, "station_id": item["station_id"], "date": item["date"], "field": f}
        if a == "b":
            return [{**row, "decision": "bad", "note": f"was {item['value']}"}]
        if a == "o":
            return [{**row, "decision": "ok"}]
        if a == "s":
            v = ask_value(f, read)
            return [{**row, "decision": "set", "value": v, "note": f"was {item['value']}"}] if v is not None else None
        return None

    if item["kind"] == "quarantine":
        p = item.get("parsed") or {}
        print(f"\nQuarantined story ({item['source']}, flagged {item['flagged_at']}): {item['reason']}")
        if p:
            print("  parsed: " + ", ".join(f"{k} {v}" for k, v in p.items() if v is not None))
        else:
            print("  text: " + (item.get("raw_text") or "")[:300])
        a = ask("  [a] accept as reported  [s] set a value  [d] keep rejected  [Enter] skip  [q] quit: ", "asdq", read)
        if a == "q":
            return "quit"
        if a == "d":
            return [{**base, "decision": "dismiss"}]
        if a in "as" and p.get("station_id") and p.get("date"):
            row = {**base, "station_id": p["station_id"], "date": p["date"]}
            if a == "a":
                return [{**row, "field": fld, "decision": "set", "value": p[fld], "note": "accepted from quarantine"}
                        for fld in A.FIELDS if p.get(fld) is not None] or [{**base, "decision": "dismiss"}]
            fld = "level_ft" if "level" in item["reason"] else "inflow_cusecs"
            v = ask_value(fld, read)
            return [{**row, "field": fld, "decision": "set", "value": v, "note": "corrected from quarantine"}] if v is not None else None
        return None

    # conflict
    print(f"\n{item['station_id'].title()} inflow on {item['date']}: outlets disagree by {item['gap'] * 100:.0f}%")
    print(f"  used:  {fmt('inflow_cusecs', item['kept'])} ({item['kept_source']})  {item['kept_link']}")
    print(f"  other: {fmt('inflow_cusecs', item['other'])} ({item['other_source']})  {item['other_link']}")
    if item.get("context"):
        print("  nearby: " + neighbours({**item, "field": "inflow_cusecs"}))
    a = ask("  [k] keep used  [u] use other  [b] both wrong  [Enter] skip  [q] quit: ", "kubq", read)
    if a == "q":
        return "quit"
    row = {**base, "station_id": item["station_id"], "date": item["date"], "field": "inflow_cusecs"}
    if a == "k":
        return [{**row, "decision": "ok"}]
    if a == "u":
        return [{**row, "decision": "set", "value": item["other"], "note": f"was {item['kept']} ({item['kept_source']})"}]
    if a == "b":
        return [{**row, "decision": "bad"}]
    return None


def pending(engine) -> list[dict]:
    frames = A.frames_from_rows(A.raw_rows(engine))
    return A.build_review(frames, A.detect(frames), A.quarantined(engine), A.conflicts(), A.read_decisions())["pending"]


def run(items: list[dict], read=input) -> int:
    saved = 0
    for i, item in enumerate(items, 1):
        print(f"\n--- {i} of {len(items)} ---", end="")
        rows = decide(item, read)
        if rows == "quit":
            break
        if rows:
            A.append_decisions(rows)
            saved += len(rows)
    return saved


def main(argv=None) -> int:
    from . import db

    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--kind", choices=["anomaly", "quarantine", "conflict"])
    ap.add_argument("--limit", type=int)
    args = ap.parse_args(argv)
    print("Finding what needs review (about 10 seconds)...")
    items = [p for p in pending(db.get_engine()) if not args.kind or p["kind"] == args.kind]
    if args.limit:
        items = items[:args.limit]
    if not items:
        print("Nothing waiting for review.")
        return 0
    print(f"{len(items)} item(s) to review.")
    saved = run(items)
    print(f"\nSaved {saved} decision(s) to {A.DECISIONS_CSV}.")
    if saved:
        print("Next: python -m pipeline.export_static, then commit data/review/decisions.csv and web/public/data.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
