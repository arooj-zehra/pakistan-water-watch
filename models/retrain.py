"""Phase 4: monthly retrain with a promotion check (run by .github/workflows/retrain.yml).

    python -m models.retrain            # train a candidate; promote it only if it passes
    python -m models.retrain --force    # promote even if it is slightly worse (not if it fails the checks)

The candidate is trained and backtested in a scratch folder with all data to
date. It replaces the production model only if:
  * it beats persistence at every horizon of 3 days or more in the backtest,
  * its 90% intervals catch 80-97% of outcomes on the current-year holdout, and
  * its average backtest skill is no more than 2 points below the current model's.
Otherwise the current model stays, and the decision is logged either way in
models/results/retrain_log.json. Only the newest two model versions are kept.
"""
from __future__ import annotations

import argparse
import json
import os
import shutil
import sys
import tempfile
from datetime import datetime, timezone
from pathlib import Path

from pipeline.config import ROOT

from . import backtest

ARTIFACTS = ROOT / "models" / "artifacts"
RESULTS = ROOT / "models" / "results"
DOCS = ROOT / "docs"
LOG = RESULTS / "retrain_log.json"
MAX_SKILL_DROP = 0.02
HOLDOUT_COVERAGE = (0.80, 0.97)
KEEP_VERSIONS = 2


def mean_skill(meta: dict) -> float | None:
    vals = [(h.get("backtest") or {}).get("skill_vs_persistence") for h in meta["horizons"].values()]
    vals = [v for v in vals if v is not None]
    return sum(vals) / len(vals) if vals else None


def decide(candidate: dict, current: dict | None, force: bool = False) -> tuple[bool, list[str]]:
    """(promote?, reasons). Pure function of the two meta.json documents."""
    reasons, ok = [], True
    for h, d in sorted(candidate["horizons"].items(), key=lambda kv: int(kv[0])):
        bt, ho = d.get("backtest") or {}, d.get("holdout") or {}
        if int(h) >= 3 and (bt.get("skill_vs_persistence") is None or bt["skill_vs_persistence"] <= 0):
            ok = False
            reasons.append(f"{h}-day backtest does not beat persistence")
        cov = ho.get("coverage90")
        if cov is not None and not (HOLDOUT_COVERAGE[0] <= cov <= HOLDOUT_COVERAGE[1]):
            ok = False
            reasons.append(f"{h}-day holdout 90% coverage {cov:.0%} is outside "
                           f"{HOLDOUT_COVERAGE[0]:.0%}-{HOLDOUT_COVERAGE[1]:.0%}")
    if not ok:
        return False, reasons
    if current is None:
        return True, ["no production model yet"]
    if candidate["version"] == current["version"]:
        return False, [f"no new data since {current['version']}"]
    new, old = mean_skill(candidate), mean_skill(current)
    if new is not None and old is not None:
        msg = f"average backtest skill {new:.1%} vs current {old:.1%}"
        if new < old - MAX_SKILL_DROP and not force:
            return False, [msg + f" (more than {MAX_SKILL_DROP * 100:.0f} points worse)"]
        reasons.append(msg)
    return True, reasons or ["passes all checks"]


def current_meta() -> dict | None:
    latest = ARTIFACTS / "LATEST"
    if not latest.exists():
        return None
    path = ARTIFACTS / latest.read_text().strip() / "meta.json"
    return json.loads(path.read_text(encoding="utf-8")) if path.exists() else None


def promote(scratch: Path, version: str) -> None:
    src = scratch / "models" / "artifacts" / version
    dst = ARTIFACTS / version
    if dst.exists():
        shutil.rmtree(dst)
    shutil.copytree(src, dst)
    (ARTIFACTS / "LATEST").write_text(version + "\n", encoding="utf-8")
    for name in ("backtest_summary.json", "backtest_predictions.csv"):
        shutil.copy2(scratch / "models" / "results" / name, RESULTS / name)
    shutil.copy2(scratch / "docs" / "MODEL_CARD.md", DOCS / "MODEL_CARD.md")
    for png in (scratch / "docs" / "img").glob("*.png"):
        shutil.copy2(png, DOCS / "img" / png.name)
    versions = sorted(p for p in ARTIFACTS.iterdir() if p.is_dir() and p.name.startswith("v"))
    for old in versions[:-KEEP_VERSIONS]:
        shutil.rmtree(old)


def log(entry: dict) -> None:
    RESULTS.mkdir(parents=True, exist_ok=True)
    history = json.loads(LOG.read_text(encoding="utf-8")) if LOG.exists() else []
    history.append(entry)
    LOG.write_text(json.dumps(history, indent=1), encoding="utf-8")


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--force", action="store_true")
    args = ap.parse_args(argv)
    cur = current_meta()
    with tempfile.TemporaryDirectory() as tmp:
        scratch = Path(tmp)
        backtest.main(["--root", str(scratch)])
        version = (scratch / "models" / "artifacts" / "LATEST").read_text().strip()
        cand = json.loads((scratch / "models" / "artifacts" / version / "meta.json").read_text(encoding="utf-8"))
        ok, reasons = decide(cand, cur, args.force)
        if ok:
            promote(scratch, version)
    entry = {"at": datetime.now(timezone.utc).replace(microsecond=0).isoformat(), "candidate": version,
             "current": cur["version"] if cur else None, "promoted": ok, "reasons": reasons,
             "candidate_mean_skill": mean_skill(cand), "current_mean_skill": mean_skill(cur) if cur else None}
    log(entry)
    verdict = f"{'PROMOTED' if ok else 'KEPT'} {'candidate ' + version if ok else (cur or {}).get('version')}: " + "; ".join(reasons)
    print("\n" + verdict)
    if os.environ.get("GITHUB_STEP_SUMMARY"):
        with open(os.environ["GITHUB_STEP_SUMMARY"], "a", encoding="utf-8") as f:
            f.write(f"### Monthly retrain\n\n{verdict}\n")
    return 0


if __name__ == "__main__":
    sys.exit(main())
