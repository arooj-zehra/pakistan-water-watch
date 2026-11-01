"""Phase 3: backtest by year, then train the production models.

    python -m models.backtest                  # full run (a few minutes)
    python -m models.backtest --quick          # horizons 1 and 5 only, for a smoke test

For each test year Y (2021, 2023, 2024, 2025, and 2026 so far as the final
holdout): train on targets before Y-1, calibrate the intervals on Y-1, test on
Y. Nothing from Y or later is ever seen in training. Two variants:

  live    weather known at forecast time only (what the daily job can use)
  oracle  plus the actual weather of the forecast window: an upper bound for
          perfect weather forecasts (the "look-ahead trap" made explicit)

Writes models/results/ (scores and every prediction), docs/img/ (skill and
reliability charts), docs/MODEL_CARD.md, and the production models in
models/artifacts/<version>/ (trained on everything but the last 365 days,
which calibrate the intervals).
"""
from __future__ import annotations

import argparse
import json
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path

import numpy as np
import pandas as pd

from pipeline.config import DOCS_DIR, ROOT, WEATHER_DIR

from . import metrics as M
from .data import HISTORY_JSON, load_history, load_weather
from .features import HORIZONS, feature_names, horizon_rows, station_frame
from .train import LEVELS, QUANTILES, fit, importance, save_booster

RESULTS_DIR = ROOT / "models" / "results"
ARTIFACTS_DIR = ROOT / "models" / "artifacts"
TEST_YEARS = (2021, 2023, 2024, 2025)
PRIMARY_YEARS = (2021, 2023)  # the near-complete years from the revised gate
HOLDOUT_YEAR = 2026


def build_frames(history_path, weather_dir):
    hist = load_history(history_path)
    weather = load_weather(weather_dir)
    return {sid: station_frame(sid, h, weather.get(sid)) for sid, h in hist.items()}


def split(rows: pd.DataFrame, year: int):
    """Train on targets before year-1, calibrate on targets in year-1, test on issues in year."""
    y0, y1 = pd.Timestamp(year - 1, 1, 1), pd.Timestamp(year, 1, 1)
    end = pd.Timestamp(year + 1, 1, 1)
    labelled = rows[rows["z"].notna()]
    train = labelled[labelled["target_date"] < y0]
    cal = labelled[(labelled["target_date"] >= y0) & (labelled["target_date"] < y1)]
    test = labelled[(labelled["issue_date"] >= y1) & (labelled["issue_date"] < end)]
    return train, cal, test


def run_fold(frames, rows, year, h, feats, min_train: int = 200):
    y1 = pd.Timestamp(year, 1, 1)
    train, cal, test = split(rows, year)
    if len(train) < min_train or len(test) < 20:
        return None
    hm = fit(train, cal, h, feats)
    pred = hm.predict(test)
    clim = M.climatology_table(frames, before=y1)
    base = M.baselines(test, frames, clim)
    out = test[["sid", "issue_date", "target_date", "q0", "q_target"]].copy()
    out["year"] = year
    out["horizon"] = h
    out["model"] = test["q0"] * np.exp(pred["z50"])
    for lvl in LEVELS:
        k = int(lvl * 100)
        out[f"lo{k}"] = test["q0"] * np.exp(pred[f"zlo_{k}"])
        out[f"hi{k}"] = test["q0"] * np.exp(pred[f"zhi_{k}"])
    out = out.join(base)
    return out, hm, len(train), len(cal)


def scores(p: pd.DataFrame) -> dict:
    y = p["q_target"]
    s = {"n": int(len(p)),
         "mae_model": M.mae(y, p["model"]), "mae_persistence": M.mae(y, p["persistence"]),
         "mae_climatology": M.mae(y, p["climatology"]), "mae_seasonal_naive": M.mae(y, p["seasonal_naive"]),
         "nse_model": M.nse(y, p["model"]), "nse_persistence": M.nse(y, p["persistence"])}
    s["skill_vs_persistence"] = 1 - s["mae_model"] / s["mae_persistence"] if s["mae_persistence"] else float("nan")
    for lvl in LEVELS:
        k = int(lvl * 100)
        s[f"coverage{k}"] = float(((y >= p[f"lo{k}"]) & (y <= p[f"hi{k}"])).mean())
    s["width90_rel"] = float(((p["hi90"] - p["lo90"]) / p["q0"]).median())
    return s


def summarise(preds: pd.DataFrame) -> dict:
    out = {}
    for h, ph in preds.groupby("horizon"):
        d = {"by_year": {int(y): scores(g) for y, g in ph.groupby("year")}}
        prim = ph[ph["year"].isin(PRIMARY_YEARS)]
        tests = ph[ph["year"].isin(TEST_YEARS)]
        d["primary"] = scores(prim) if len(prim) else None
        d["all_test_years"] = scores(tests) if len(tests) else None
        d["by_season"] = {s: scores(g) for s, g in tests.groupby(M.season(tests["target_date"]))}
        hold = ph[ph["year"] == HOLDOUT_YEAR]
        d["holdout"] = scores(hold) if len(hold) else None
        out[int(h)] = d
    return out


def plot_skill(summary: dict, path: Path):
    from matplotlib.figure import Figure

    hs = sorted(summary["live"])
    fig = Figure(figsize=(8, 3.6))
    ax = fig.subplots()
    x = np.arange(len(hs))
    live = [summary["live"][h]["all_test_years"]["skill_vs_persistence"] * 100 for h in hs]
    ax.bar(x, live, width=0.55, color="#3e8e98", label="model (weather known at forecast time)")
    if "oracle" in summary:
        orc = [summary["oracle"][h]["all_test_years"]["skill_vs_persistence"] * 100 for h in hs]
        ax.scatter(x, orc, color="#1c2b33", zorder=3, marker="D", s=28, label="with perfect weather forecasts")
    for xi, v in zip(x, live):
        ax.text(xi, v / 2, f"{v:.0f}%", ha="center", va="center", fontsize=9, color="white", fontweight="bold")
    ax.axhline(0, color="#4d5e66", lw=1)
    ax.set_xticks(x, [f"{h} day{'s' if h > 1 else ''}" for h in hs])
    ax.set_ylabel("MAE improvement over persistence (%)")
    ax.set_title("Skill against 'tomorrow equals today', test years 2021 and 2023 to 2025", fontsize=11)
    ax.legend(frameon=False, fontsize=8, loc="upper left")
    for sp in ("top", "right"):
        ax.spines[sp].set_visible(False)
    path.parent.mkdir(parents=True, exist_ok=True)
    fig.savefig(path, dpi=130, bbox_inches="tight")


def plot_reliability(summary: dict, path: Path):
    from matplotlib.figure import Figure

    fig = Figure(figsize=(4.6, 4.2))
    ax = fig.subplots()
    ax.plot([40, 100], [40, 100], color="#9fb0b5", lw=1, ls="--", label="perfect calibration")
    for h, mk in zip(sorted(summary["live"]), "os^Dv"):
        for key, col in (("all_test_years", "#3e8e98"), ("holdout", "#b5a27f")):
            s = summary["live"][h][key]
            if not s:
                continue
            ys = [s[f"coverage{int(l * 100)}"] * 100 for l in LEVELS]
            ax.plot([l * 100 for l in LEVELS], ys, marker=mk, color=col, lw=0.8, alpha=0.85,
                    label=f"{'test years' if key == 'all_test_years' else 'holdout 2026'}" if h == 1 else None)
    ax.set_xlabel("Nominal interval coverage (%)")
    ax.set_ylabel("Share of actual inflows inside (%)")
    ax.set_title("Interval calibration, all horizons", fontsize=11)
    ax.set_xlim(40, 100)
    ax.set_ylim(40, 100)
    ax.legend(frameon=False, fontsize=8, loc="upper left")
    for sp in ("top", "right"):
        ax.spines[sp].set_visible(False)
    fig.savefig(path, dpi=130, bbox_inches="tight")


def git_commit() -> str:
    try:
        return subprocess.run(["git", "rev-parse", "--short", "HEAD"], cwd=ROOT, capture_output=True,
                              text=True, timeout=10).stdout.strip() or "unknown"
    except Exception:
        return "unknown"


def train_production(frames, horizons, version: str, out_dir: Path, summary: dict) -> dict:
    """Train on everything but the last 365 days of targets; calibrate on those days."""
    feats = feature_names(False)
    out_dir.mkdir(parents=True, exist_ok=True)
    meta = {"version": version, "created_at": datetime.now(timezone.utc).replace(microsecond=0).isoformat(),
            "git_commit": git_commit(), "features": feats, "quantiles": list(QUANTILES),
            "levels": [int(l * 100) for l in LEVELS], "horizons": {}, "importance": {}}
    for h in horizons:
        rows = horizon_rows(frames, h)
        lab = rows[rows["z"].notna()]
        cut = lab["target_date"].max() - pd.Timedelta(days=365)
        train, cal = lab[lab["target_date"] <= cut], lab[lab["target_date"] > cut]
        hm = fit(train, cal, h, feats)
        for a, m in hm.models.items():
            save_booster(m.booster_, out_dir / f"h{h}_q{int(a * 100):02d}.txt.gz")
        meta["horizons"][str(h)] = {
            "qhat": {str(int(l * 100)): v for l, v in hm.qhat.items()},
            "train_rows": int(len(train)), "cal_rows": int(len(cal)),
            "train_from": str(train["target_date"].min().date()), "train_to": str(train["target_date"].max().date()),
            "cal_from": str(cal["target_date"].min().date()), "cal_to": str(cal["target_date"].max().date()),
            "backtest": summary["live"].get(h, {}).get("all_test_years"),
            "holdout": summary["live"].get(h, {}).get("holdout"),
        }
        meta["importance"][str(h)] = {k: round(float(v), 5) for k, v in importance(hm, lab[feats]).head(12).items()}
    (out_dir / "meta.json").write_text(json.dumps(meta, indent=2), encoding="utf-8")
    (out_dir.parent / "LATEST").write_text(version + "\n", encoding="utf-8")
    return meta


def fmt_pct(v):
    return "–" if v is None or v != v else f"{v * 100:.0f}%"


def fmt_n(v):
    return "–" if v is None or v != v else f"{v:,.0f}"


def model_card(summary: dict, meta: dict, data_through: str, path: Path):
    live = summary["live"]
    hs = sorted(live)
    L = ["# Model card: inflow forecasts", "",
         "_Generated by `python -m models.backtest`; numbers update on every run._", "",
         "## What it forecasts", "",
         "Daily mean inflow (cusecs, 24 hours to 06:00 PKT) at Tarbela (Indus) and Mangla (Jhelum), "
         f"{', '.join(map(str, hs))} days ahead, with calibrated 50%, 80% and 90% intervals.", "",
         "## Intended use", "",
         "- **For:** a public, independent view of where inflows are likely heading over the next ten days, with honest "
         "uncertainty; research and teaching about forecasting snow- and glacier-fed rivers; a worked example of "
         "prospective, tamper-evident forecast evaluation.",
         "- **Not for:** operational reservoir management, flood warning or any safety decision. Use WAPDA, IRSA and the "
         "PMD Flood Forecasting Division. The model forecasts inflow only, not levels or releases.",
         "- **Users should know:** the ranges are calibrated on past years; in conditions unlike any training year they "
         "can be too narrow (the drift monitor flags such periods on the Accuracy page).", "",
         "## Data", "",
         "Daily history rebuilt from news reports of the WAPDA/IRSA release, 2015 to today, with gaps; see the "
         "[data card](DATA_CARD.md). Weather: Open-Meteo ERA5 at 10 points in the upper Indus and Jhelum basins.", "",
         "## Model", "",
         "- LightGBM quantile regression, one set of models per horizon, both dams pooled (a station feature).",
         "- Target: log(inflow on the target day) minus log(inflow today), so predicting zero is the persistence forecast.",
         "- Features: today's inflow and level, changes over 1 to 14 days, rolling means, how many of the last 14 days were "
         "observed, season, and catchment weather from ERA5 (positive degree-days, temperature, precipitation and snowfall "
         "over 3 to 30 days, ending the day before the forecast).",
         "- Intervals: conformalized quantile regression on the year before the test year (Romano et al., 2019).",
         f"- Production version `{meta['version']}` (commit `{meta['git_commit']}`), data through {data_through}.", "",
         "## Backtest", "",
         "Each test year is predicted by models trained only on earlier years, with the intervals calibrated on the year "
         "before it. Targets are observed values only, never interpolated ones.", "",
         "| Horizon | Rows | MAE model (cusecs) | MAE persistence | MAE climatology | MAE seasonal naive | NSE | Skill vs persistence | 90% coverage |",
         "| --- | --- | --- | --- | --- | --- | --- | --- | --- |"]
    for h in hs:
        s = live[h]["all_test_years"]
        L.append(f"| {h} d | {s['n']} | {fmt_n(s['mae_model'])} | {fmt_n(s['mae_persistence'])} | "
                 f"{fmt_n(s['mae_climatology'])} | {fmt_n(s['mae_seasonal_naive'])} | {s['nse_model']:.3f} | "
                 f"{fmt_pct(s['skill_vs_persistence'])} | {fmt_pct(s['coverage90'])} |")
    L += ["", "Test years 2021, 2023, 2024 and 2025 pooled. Skill = 1 − MAE(model) / MAE(persistence).", "",
          "![Skill by horizon](img/skill_by_horizon.png)", "",
          "### Final holdout: 2026 so far", "",
          "| Horizon | Rows | MAE model | MAE persistence | Skill | 50% cov. | 80% cov. | 90% cov. | Median 90% width |",
          "| --- | --- | --- | --- | --- | --- | --- | --- | --- |"]
    for h in hs:
        s = live[h]["holdout"]
        if s:
            L.append(f"| {h} d | {s['n']} | {fmt_n(s['mae_model'])} | {fmt_n(s['mae_persistence'])} | "
                     f"{fmt_pct(s['skill_vs_persistence'])} | {fmt_pct(s['coverage50'])} | {fmt_pct(s['coverage80'])} | "
                     f"{fmt_pct(s['coverage90'])} | ±{s['width90_rel'] * 50:.0f}% of today's inflow |")
    L += ["", "![Interval calibration](img/reliability.png)", "",
          "### By season (test years pooled)", "",
          "| Horizon | Season | MAE model | Skill vs persistence | 90% coverage |", "| --- | --- | --- | --- | --- |"]
    for h in hs:
        for sname, s in live[h]["by_season"].items():
            L.append(f"| {h} d | {sname} | {fmt_n(s['mae_model'])} | {fmt_pct(s['skill_vs_persistence'])} | {fmt_pct(s['coverage90'])} |")
    if "oracle" in summary:
        L += ["", "### The look-ahead trap", "",
              "The live model uses only weather known when the forecast is made. The oracle variant is also given the "
              "actual weather of the forecast window, which no forecast can know; the gap between them is the most that "
              "better weather forecasts could add.", "",
              "| Horizon | Skill, live | Skill, perfect weather | 90% coverage, live | 90% coverage, perfect weather |",
              "| --- | --- | --- | --- | --- |"]
        for h in hs:
            a, b = live[h]["all_test_years"], summary["oracle"][h]["all_test_years"]
            L.append(f"| {h} d | {fmt_pct(a['skill_vs_persistence'])} | {fmt_pct(b['skill_vs_persistence'])} | "
                     f"{fmt_pct(a['coverage90'])} | {fmt_pct(b['coverage90'])} |")
    L += ["", "## What drives the forecasts", "",
          "Mean absolute SHAP contribution (LightGBM `pred_contrib`) of the production median models:", ""]
    for h in (hs[0], hs[-1]):
        top = list(meta["importance"][str(h)].items())[:6]
        L.append(f"- **{h} day{'s' if h > 1 else ''}:** " + ", ".join(f"`{k}`" for k, _ in top))
    L += ["", "## Live evaluation and versioning", "",
          "- Every live forecast is stored once in a database table that rejects edits and deletions, and appended to "
          "[`data/forecasts/issued.csv`](../data/forecasts/issued.csv), whose git history timestamps it.",
          "- Forecasts are scored automatically when their day's figures are published, against the reported value and "
          "against persistence; results are on the [Accuracy page](https://pakistan-water-watch.vercel.app/accuracy).",
          "- Alerts open a GitHub issue if the 90% intervals catch under 75% of outcomes, the model does worse than "
          "persistence over 60 days, or recent inputs are far more unusual than the same season in any training year.",
          "- A candidate model is retrained monthly and replaces this one only if it beats persistence at 3+ days, keeps "
          "holdout 90% coverage within 80-97%, and is not more than 2 points of skill worse; decisions are logged in "
          "`models/results/retrain_log.json`. Each version's files and metadata are kept in `models/artifacts/`.", "",
          "## Limitations", "",
          "- History is rebuilt from news stories: 2024 Q1, February 2025, October to December 2025, and Tarbela "
          "from February to July 2022 are missing (see the [data card](DATA_CARD.md)).",
          "- ERA5 runs about 5 days behind real time. Live forecasts fill the newest days from Open-Meteo's forecast API, "
          "bias-corrected against ERA5 over the last 60 days; the backtest used ERA5 only, so live skill may be a little lower.",
          "- Weather comes from 10 grid points (~25 km), not from stations on the glaciers.",
          "- Outflow decisions (dam operations) are not modelled; inflow is a natural flow and is not affected by them.",
          f"- On the {HOLDOUT_YEAR} holdout the 50% intervals held {cov50_range(live)} of outcomes; the 50% and 80% "
          "intervals were calibrated the same way as the 90% ones but are less stable with a year of calibration data.",
          "- Training spans about eight usable years; floods or droughts beyond that range may be badly under-forecast, "
          "and the intervals would not know it.",
          "- Glacier melt in the upper Indus is changing over time; monthly retraining limits but does not remove the "
          "risk that the past is a poor guide.",
          "- Prospective scoring of live forecasts starts in November 2026, in the low-flow season; the melt season from "
          "April onward is the real test.", "",
          "## Responsible use", "",
          "This is an independent student project, not an official forecast. It publishes its full track record, "
          "including misses, so its reliability can be judged rather than assumed. Report problems on "
          "[GitHub](https://github.com/arooj-zehra/pakistan-water-watch/issues).", ""]
    path.write_text("\n".join(L), encoding="utf-8")


def cov50_range(live: dict) -> str:
    vals = [d["holdout"]["coverage50"] for d in live.values() if d.get("holdout")]
    return f"{min(vals) * 100:.0f}-{max(vals) * 100:.0f}%" if vals else "–"


def regenerate_card(root: Path = ROOT) -> Path:
    """Rebuild docs/MODEL_CARD.md from the saved results and production meta (no retraining)."""
    res = json.loads((root / "models" / "results" / "backtest_summary.json").read_text(encoding="utf-8"))
    summary = {v: {int(h): d for h, d in hs.items()} for v, hs in res["summary"].items()}
    version = (root / "models" / "artifacts" / "LATEST").read_text().strip()
    meta = json.loads((root / "models" / "artifacts" / version / "meta.json").read_text(encoding="utf-8"))
    path = root / "docs" / "MODEL_CARD.md"
    model_card(summary, meta, res["data_through"], path)
    return path


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--history", type=Path, default=HISTORY_JSON)
    ap.add_argument("--weather-dir", type=Path, default=WEATHER_DIR)
    ap.add_argument("--root", type=Path, default=ROOT, help="where results/, artifacts/ and docs/ are written")
    ap.add_argument("--quick", action="store_true", help="horizons 1 and 5, no oracle variant")
    ap.add_argument("--no-production", action="store_true")
    ap.add_argument("--card-only", action="store_true", help="rebuild docs/MODEL_CARD.md from saved results")
    args = ap.parse_args(argv)
    if args.card_only:
        print(f"Wrote {regenerate_card(args.root)}")
        return 0

    results_dir = args.root / "models" / "results"
    artifacts_dir = args.root / "models" / "artifacts"
    docs_dir = args.root / "docs"
    horizons = (1, 5) if args.quick else HORIZONS
    frames = build_frames(args.history, args.weather_dir)
    data_through = str(max(f["_raw"].dropna().index.max() for f in frames.values()).date())
    print(f"History through {data_through}; stations: {', '.join(frames)}")

    summary, all_preds = {}, []
    for variant, future in (("live", False), ("oracle", True)):
        if args.quick and variant == "oracle":
            continue
        feats = feature_names(future)
        preds = []
        for h in horizons:
            rows = horizon_rows(frames, h, future_weather=future)
            for year in TEST_YEARS + (HOLDOUT_YEAR,):
                r = run_fold(frames, rows, year, h, feats)
                if r is None:
                    print(f"  {variant:<6} h={h:<2} {year}: not enough rows, skipped")
                    continue
                p, _, ntr, ncal = r
                preds.append(p)
                s = scores(p)
                print(f"  {variant:<6} h={h:<2} {year}: train {ntr:>5} cal {ncal:>4} test {s['n']:>4} | "
                      f"MAE {s['mae_model']:>8,.0f} vs persistence {s['mae_persistence']:>8,.0f} "
                      f"(skill {s['skill_vs_persistence'] * 100:>4.0f}%) | 90% cov {s['coverage90'] * 100:>3.0f}%", flush=True)
        dfp = pd.concat(preds, ignore_index=True)
        dfp["variant"] = variant
        all_preds.append(dfp)
        summary[variant] = summarise(dfp)

    results_dir.mkdir(parents=True, exist_ok=True)
    pd.concat(all_preds).to_csv(results_dir / "backtest_predictions.csv", index=False, float_format="%.1f")
    (results_dir / "backtest_summary.json").write_text(json.dumps(
        {"generated_at": datetime.now(timezone.utc).replace(microsecond=0).isoformat(), "data_through": data_through,
         "test_years": TEST_YEARS, "primary_years": PRIMARY_YEARS, "holdout_year": HOLDOUT_YEAR,
         "summary": summary}, indent=2, default=float), encoding="utf-8")
    plot_skill(summary, docs_dir / "img" / "skill_by_horizon.png")
    plot_reliability(summary, docs_dir / "img" / "reliability.png")

    print("\nPooled test years (2021, 2023-2025), live model:")
    print("  horizon   MAE model  MAE persist  skill   NSE   90% cov | holdout 2026: skill  90% cov")
    for h in horizons:
        s, ho = summary["live"][h]["all_test_years"], summary["live"][h]["holdout"]
        print(f"  {h:>4} d   {s['mae_model']:>9,.0f}  {s['mae_persistence']:>11,.0f}  {s['skill_vs_persistence'] * 100:>4.0f}%  "
              f"{s['nse_model']:.3f}   {s['coverage90'] * 100:>3.0f}%  |  {fmt_pct(ho and ho['skill_vs_persistence']):>5}  "
              f"{fmt_pct(ho and ho['coverage90']):>5}")

    if not args.no_production:
        version = f"v{data_through.replace('-', '')}"
        meta = train_production(frames, horizons, version, artifacts_dir / version, summary)
        model_card(summary, meta, data_through, docs_dir / "MODEL_CARD.md")
        print(f"\nProduction models saved to models/artifacts/{version}/; model card at docs/MODEL_CARD.md")

    ok3 = all(summary["live"][h]["all_test_years"]["skill_vs_persistence"] > 0 for h in horizons if h >= 3)
    ho = [summary["live"][h]["holdout"] for h in horizons if summary["live"][h]["holdout"]]
    cov_ok = all(0.85 <= s["coverage90"] <= 0.95 for s in ho) if ho else False
    print(f"\nDone-when check: beats persistence at 3+ days: {'YES' if ok3 else 'NO'}; "
          f"90% coverage within 85-95% on the 2026 holdout at every horizon: {'YES' if cov_ok else 'NO'}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
