"""Phase 5: how well does the anomaly detection catch real kinds of error?

    python -m pipeline.anomaly_eval             # 10 trials (a few minutes)
    python -m pipeline.anomaly_eval --trials 3  # quicker

1. Planted errors: 20 typing errors of the kinds found in the news stories are
   planted at random clean days of the real history (a digit wrong, two digits
   swapped, a digit dropped or doubled, inflow and outflow swapped, the other
   dam's inflow, a level typo), the detection is rerun, and each counts as
   caught if its value is queued. Repeated over several random draws.
2. The 11 stories the validation rules quarantined: put back into the history,
   would the detection have queued them too?
3. The 73 days where two outlets disagreed: if the other outlet's figure had
   been used, would it have been queued?

Writes docs/ANOMALY_REPORT.md and data/inventory/anomaly_eval.json.
"""
from __future__ import annotations

import argparse
import json
import random
import sys
from collections import defaultdict

import numpy as np
import pandas as pd

from . import anomaly as A
from .config import DOCS_DIR, INVENTORY_DIR

PLAN = [("digit_wrong", 4), ("transpose", 3), ("drop_digit", 2), ("extra_digit", 1),
        ("swap_in_out", 3), ("wrong_dam", 3), ("level_typo", 4)]
LABELS = {"digit_wrong": "one digit wrong (28,800 → 38,800)", "transpose": "two digits swapped (28,800 → 82,800)",
          "drop_digit": "a digit dropped (145,000 → 14,500)", "extra_digit": "a digit doubled (28,800 → 288,800)",
          "swap_in_out": "inflow and outflow swapped", "wrong_dam": "the other dam's inflow",
          "level_typo": "level typo (1,510.82 → 1,501.82)"}
SEP_DAYS = 20  # planted errors on the same dam at least this far apart


def _digits(v: float) -> str:
    return str(int(round(v)))


def typo(kind: str, v: float, rng: random.Random) -> float | None:
    s = _digits(v)
    if len(s) < 3:
        return None
    if kind == "digit_wrong":
        i = rng.randrange(min(3, len(s)))
        d = rng.choice([c for c in "0123456789" if c != s[i] and not (i == 0 and c == "0")])
        return float(s[:i] + d + s[i + 1:])
    if kind == "transpose":
        pairs = [i for i in range(min(3, len(s) - 1)) if s[i] != s[i + 1] and not (i == 0 and s[1] == "0")]
        if not pairs:
            return None
        i = rng.choice(pairs)
        return float(s[:i] + s[i + 1] + s[i] + s[i + 2:])
    if kind == "drop_digit":
        i = rng.randrange(1, len(s))
        return float(s[:i] + s[i + 1:])
    if kind == "extra_digit":
        i = rng.randrange(len(s))
        return float(s[:i] + s[i] + s[i:])
    return None


def level_typo(v: float, rng: random.Random) -> float | None:
    whole, frac = f"{v:.2f}".split(".")
    if len(whole) != 4:
        return None
    i = rng.choice([2, 3])  # tens or units digit of e.g. 1510
    if rng.random() < 0.5 and whole[2] != whole[3]:
        whole = whole[:2] + whole[3] + whole[2]
    else:
        whole = whole[:i] + rng.choice([c for c in "0123456789" if c != whole[i]]) + whole[i + 1:]
    return float(f"{whole}.{frac}")


def plant(frames: dict, rng: random.Random, baseline: set) -> tuple[dict, list[dict]]:
    """Copy of frames with the PLAN's 20 errors planted at clean days."""
    out = {sid: f.copy() for sid, f in frames.items()}
    used = defaultdict(list)
    planted = []
    sids = list(frames)

    def free(sid, d):
        return all(abs((d - u).days) >= SEP_DAYS for u in used[sid])

    for kind, n in PLAN:
        tries = 0
        while sum(p["kind"] == kind for p in planted) < n and tries < 5000:
            tries += 1
            sid = rng.choice(sids)
            f = out[sid]
            d = f.index[rng.randrange(len(f))]
            if d.year < 2017 or not free(sid, d):
                continue
            row = frames[sid].loc[d]
            if kind == "swap_in_out":
                a, b = row["inflow_cusecs"], row["outflow_cusecs"]
                if np.isnan(a) or np.isnan(b) or abs(a - b) / max(a, b) < 0.1 or \
                        (sid, str(d.date()), "inflow_cusecs") in baseline or (sid, str(d.date()), "outflow_cusecs") in baseline:
                    continue
                f.at[d, "inflow_cusecs"], f.at[d, "outflow_cusecs"] = b, a
                keys = [(sid, str(d.date()), "inflow_cusecs"), (sid, str(d.date()), "outflow_cusecs")]
                planted.append({"kind": kind, "station_id": sid, "date": str(d.date()), "field": "inflow/outflow",
                                "was": f"{a:,.0f} / {b:,.0f}", "now": f"{b:,.0f} / {a:,.0f}", "keys": keys,
                                "change": abs(np.log(b / a))})
            elif kind == "wrong_dam":
                other = [s for s in sids if s != sid][0]
                a = row["inflow_cusecs"]
                b = frames[other]["inflow_cusecs"].get(d, np.nan)
                if np.isnan(a) or np.isnan(b) or (sid, str(d.date()), "inflow_cusecs") in baseline:
                    continue
                f.at[d, "inflow_cusecs"] = b
                planted.append({"kind": kind, "station_id": sid, "date": str(d.date()), "field": "inflow_cusecs",
                                "was": f"{a:,.0f}", "now": f"{b:,.0f}", "keys": [(sid, str(d.date()), "inflow_cusecs")],
                                "change": abs(np.log(b / a))})
            else:
                fld = "level_ft" if kind == "level_typo" else "inflow_cusecs"
                a = row[fld]
                if np.isnan(a) or (sid, str(d.date()), fld) in baseline:
                    continue
                b = level_typo(a, rng) if kind == "level_typo" else typo(kind, a, rng)
                if b is None or b == a:
                    continue
                f.at[d, fld] = b
                ch = abs(b - a) if fld == "level_ft" else abs(np.log(max(b, 1) / a))
                fmt = "{:,.2f}" if fld == "level_ft" else "{:,.0f}"
                planted.append({"kind": kind, "station_id": sid, "date": str(d.date()), "field": fld,
                                "was": fmt.format(a), "now": fmt.format(b), "keys": [(sid, str(d.date()), fld)],
                                "change": ch})
            used[sid].append(d)
    return out, planted


def flagged_keys(frames) -> set:
    return {fl.key for fl in A.detect(frames)}


def trial(frames, baseline: set, seed: int) -> tuple[list[dict], int]:
    rng = random.Random(seed)
    planted_frames, planted = plant(frames, rng, baseline)
    keys = flagged_keys(planted_frames)
    injected = {k for p in planted for k in p["keys"]}
    for p in planted:
        p["caught"] = any(k in keys for k in p["keys"])
    side = len(keys - baseline - injected)
    return planted, side


def substitute(frames, items: list[tuple[str, str, str, float]]) -> dict:
    out = {sid: f.copy() for sid, f in frames.items()}
    for sid, d, fld, v in items:
        t = pd.Timestamp(d)
        if sid in out and out[sid].index.min() <= t <= out[sid].index.max():
            out[sid].at[t, fld] = v
    return out


def batches(items, key=lambda x: (x[0], x[1])):
    """Split items so that no two on the same dam are within SEP_DAYS of each other."""
    groups: list[list] = []
    for it in items:
        sid, d = key(it)
        t = pd.Timestamp(d)
        for g in groups:
            if all(not (s == sid and abs((t - pd.Timestamp(e)).days) < SEP_DAYS) for s, e in (key(x) for x in g)):
                g.append(it)
                break
        else:
            groups.append([it])
    return groups


def replay(frames, items) -> list[bool]:
    """Is each (station, date, field, value) queued when put into the history?"""
    caught = {}
    for g in batches(items):
        keys = flagged_keys(substitute(frames, g))
        for it in g:
            caught[it] = (it[0], it[1], it[2]) in keys
    return [caught[it] for it in items]


def quarantine_cases(engine) -> list[tuple]:
    out = []
    for q in A.quarantined(engine):
        p = q.get("parsed") or {}
        fld = "level_ft" if "level" in q["reason"] else "inflow_cusecs"
        if p.get("station_id") and p.get("date") and p.get(fld) is not None:
            out.append((p["station_id"], p["date"], fld, float(p[fld]), q["reason"]))
    return out


def is_big(p: dict) -> bool:
    return p["change"] >= 1.0 if p["kind"] == "level_typo" else p["change"] >= np.log(1.15)


def report(results: dict, path) -> None:
    pl, trials = results["planted"], results["trials"]
    L = ["# Anomaly detection: how well does it catch errors?", "",
         "_Generated by `python -m pipeline.anomaly_eval`._", "",
         "The history is rebuilt from news stories, so the errors that matter are typing errors in a story. "
         "Four checks look for them (see `pipeline/anomaly.py`): a robust z-score against a trend line through the 7 days either side, "
         "the same test after removing the yearly cycle with STL, an Isolation Forest over each day's changes, and "
         "a water balance (the lake level must move the way inflow minus outflow says). A value is queued for "
         "review when the evidence adds up (two strong signals, or one strong and two moderate), or the local check "
         "alone is extreme.", "",
         "## On the real history", "",
         f"{results['flagged']} of {results['station_days']:,} dam-days have a queued value "
         f"({results['flagged'] / results['station_days'] * 1000:.0f} per 1,000). Most are clear typos "
         "(outflows of 20 or 35 cusecs where 35,000 is usual, a Mangla inflow ten times its neighbours); "
         "some are real sudden changes, which is why a person decides.", "",
         f"## 20 planted errors, {trials} random draws", "",
         f"Each draw plants 20 errors at random clean days from 2017 on, at least {SEP_DAYS} days apart on the same dam. "
         f"**Caught {results['recall'] * 100:.0f}% on average** "
         f"(range {results['recall_min'] * 100:.0f}% to {results['recall_max'] * 100:.0f}% across draws), "
         f"with {results['side_flags_mean']:.1f} extra queued values per draw caused by the planted ones "
         "(neighbours that look odd next to an error).", "",
         "| Kind of error | Planted | Caught | Typical size of the change |", "| --- | --- | --- | --- |"]
    for kind, _ in PLAN:
        ps = [p for p in pl if p["kind"] == kind]
        sizes = [p["change"] for p in ps]
        unit = "ft" if kind == "level_typo" else "× (log ratio)"
        size = (f"median {np.median(sizes):.1f} ft" if kind == "level_typo"
                else f"median {np.expm1(np.median(sizes)) * 100:.0f}%")
        L.append(f"| {LABELS[kind]} | {len(ps)} | {sum(p['caught'] for p in ps)} "
                 f"({sum(p['caught'] for p in ps) / len(ps) * 100:.0f}%) | {size} |")
    big = [p for p in pl if is_big(p)]
    small = [p for p in pl if not is_big(p)]
    L += ["", f"**Errors big enough to matter** (a flow off by 15% or more, a level off by 1 ft or more): caught "
          f"{sum(p['caught'] for p in big)} of {len(big)} ({sum(p['caught'] for p in big) / max(len(big), 1) * 100:.0f}%). "
          f"Smaller ones: {sum(p['caught'] for p in small)} of {len(small)}. A slip in the third digit "
          "(28,800 → 28,300) is within normal day-to-day change and can't be told apart from a real reading.", "",
          "### The first draw in full", "", "| Dam | Date | Error | Reported | Planted | Caught |",
          "| --- | --- | --- | --- | --- | --- |"]
    for p in results["first_draw"]:
        L.append(f"| {p['station_id']} | {p['date']} | {LABELS[p['kind']]} | {p['was']} | {p['now']} | "
                 f"{'yes' if p['caught'] else '**no**'} |")
    q = results["quarantine"]
    L += ["", "## The 11 quarantined stories", "",
          f"The validation rules rejected these when loading. Put back into the history, the detection queues "
          f"{sum(x['caught'] for x in q)} of {len(q)}, so the two layers back each other up.", "",
          "| Dam | Date | Rejected because | Queued by detection |", "| --- | --- | --- | --- |"]
    for x in q:
        L.append(f"| {x['station_id']} | {x['date']} | {x['reason']} | {'yes' if x['caught'] else 'no'} |")
    c = results["conflicts"]
    L += ["", "## Days where two outlets disagreed", "",
          "For each of the days where outlets reported different inflows, the other outlet's figure was put in place "
          "of the one used:", "", "| Disagreement | Days | Other figure queued |", "| --- | --- | --- |"]
    for b in c:
        L.append(f"| {b['bin']} | {b['n']} | {b['caught']} |")
    L += ["", "Small disagreements are usually two reporting times or rounding, and neither figure is wrong enough "
          "to stand out. Disagreements over 5% go to the review queue regardless.", "",
          "## Limits", "",
          "- Errors that repeat for several days (a story copied forward with the same typo) look like a real "
          "change; the checks compare a value with its neighbours.",
          "- Thin stretches of history (2017 to 2019) have fewer neighbours, so fewer values can be checked.",
          "- Outflows change in steps by operator decision, so outflow checks are looser than inflow checks.", ""]
    path.write_text("\n".join(L), encoding="utf-8")


def main(argv=None) -> int:
    from . import db

    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--trials", type=int, default=10)
    args = ap.parse_args(argv)
    engine = db.get_engine()
    frames = A.frames_from_rows(A.raw_rows(engine))
    base_flags = A.detect(frames)
    baseline = {f.key for f in base_flags}
    station_days = int(sum(f[list(A.FIELDS)].notna().any(axis=1).sum() for f in frames.values()))
    print(f"Real history: {len(baseline)} queued values over {station_days:,} dam-days")

    planted_all, recalls, sides, first = [], [], [], None
    for seed in range(args.trials):
        planted, side = trial(frames, baseline, seed)
        r = sum(p["caught"] for p in planted) / len(planted)
        print(f"  draw {seed + 1}: caught {sum(p['caught'] for p in planted)}/{len(planted)}, {side} extra flags", flush=True)
        recalls.append(r)
        sides.append(side)
        planted_all += planted
        first = first or planted

    qc = quarantine_cases(engine)
    q_caught = replay(frames, [x[:4] for x in qc])
    quarantine = [{"station_id": x[0], "date": x[1], "reason": x[4], "caught": c} for x, c in zip(qc, q_caught)]
    print(f"Quarantined stories queued: {sum(q_caught)}/{len(qc)}")

    conf = []
    for c in A.conflicts(min_gap=0.0):
        conf.append((c["station_id"], c["date"], "inflow_cusecs", c["other"], c["gap"]))
    c_caught = replay(frames, [x[:4] for x in conf]) if conf else []
    bins = []
    for name, lo, hi in (("under 5%", 0, 0.05), ("5% to 20%", 0.05, 0.2), ("over 20%", 0.2, 9)):
        sel = [cc for x, cc in zip(conf, c_caught) if lo <= x[4] < hi]
        bins.append({"bin": name, "n": len(sel), "caught": sum(sel)})
    print("Outlet disagreements, other figure queued: " + ", ".join(f"{b['bin']} {b['caught']}/{b['n']}" for b in bins))

    big = [p for p in planted_all if is_big(p)]
    results = {"flagged": len(baseline), "station_days": station_days, "trials": args.trials,
               "recall_big": sum(p["caught"] for p in big) / max(len(big), 1),
               "recall": float(np.mean(recalls)), "recall_min": float(min(recalls)), "recall_max": float(max(recalls)),
               "side_flags_mean": float(np.mean(sides)),
               "planted": [{k: v for k, v in p.items() if k != "keys"} for p in planted_all],
               "first_draw": [{k: v for k, v in p.items() if k != "keys"} for p in first],
               "quarantine": quarantine, "conflicts": bins}
    (INVENTORY_DIR / "anomaly_eval.json").write_text(json.dumps(results, indent=1, default=float), encoding="utf-8")
    report(results, DOCS_DIR / "ANOMALY_REPORT.md")
    print(f"\nCaught {results['recall'] * 100:.0f}% of planted errors on average, "
          f"{results['recall_big'] * 100:.0f}% of those big enough to matter. Report: docs/ANOMALY_REPORT.md")
    return 0


if __name__ == "__main__":
    sys.exit(main())
