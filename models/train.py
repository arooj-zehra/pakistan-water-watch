"""Quantile LightGBM per horizon, calibrated with conformalized quantile regression.

For each horizon h and each interval level (50%, 80%, 90%), a lower and upper
quantile model is trained on the training years; the calibration year then
widens (or narrows) every interval by the amount needed for the calibration
year to have the right coverage (Romano et al., 2019, "Conformalized quantile
regression"). The median model gives the point forecast.
"""
from __future__ import annotations

from dataclasses import dataclass, field

import lightgbm as lgb
import numpy as np
import pandas as pd

QUANTILES = (0.05, 0.10, 0.25, 0.50, 0.75, 0.90, 0.95)
LEVELS = {0.50: (0.25, 0.75), 0.80: (0.10, 0.90), 0.90: (0.05, 0.95)}
PARAMS = dict(
    n_estimators=400, learning_rate=0.04, num_leaves=15, min_child_samples=20,
    subsample=0.8, subsample_freq=1, colsample_bytree=0.8, reg_lambda=1.0, verbose=-1,
)


@dataclass
class HorizonModel:
    h: int
    features: list[str]
    models: dict[float, lgb.LGBMRegressor] = field(default_factory=dict)
    qhat: dict[float, float] = field(default_factory=dict)  # conformal widening per level

    def raw_quantiles(self, X: pd.DataFrame) -> dict[float, np.ndarray]:
        return {a: m.predict(X[self.features]) for a, m in self.models.items()}

    def predict(self, X: pd.DataFrame) -> pd.DataFrame:
        """Columns z50 and, per level, zlo_<lvl> / zhi_<lvl> (calibrated)."""
        q = self.raw_quantiles(X)
        out = pd.DataFrame(index=X.index)
        out["z50"] = q[0.50]
        for lvl, (lo, hi) in LEVELS.items():
            a, b = np.minimum(q[lo], q[hi]), np.maximum(q[lo], q[hi])
            out[f"zlo_{int(lvl * 100)}"] = a - self.qhat.get(lvl, 0.0)
            out[f"zhi_{int(lvl * 100)}"] = b + self.qhat.get(lvl, 0.0)
        return out


def fit(train: pd.DataFrame, cal: pd.DataFrame | None, h: int, features: list[str], seed: int = 7) -> HorizonModel:
    hm = HorizonModel(h=h, features=features)
    for a in QUANTILES:
        m = lgb.LGBMRegressor(objective="quantile", alpha=a, random_state=seed, **PARAMS)
        m.fit(train[features], train["z"])
        hm.models[a] = m
    if cal is not None and len(cal) >= 30:
        q = hm.raw_quantiles(cal)
        z = cal["z"].to_numpy()
        for lvl, (lo, hi) in LEVELS.items():
            a, b = np.minimum(q[lo], q[hi]), np.maximum(q[lo], q[hi])
            scores = np.maximum(a - z, z - b)
            n = len(scores)
            k = min(1.0, np.ceil((n + 1) * lvl) / n)
            hm.qhat[lvl] = float(np.quantile(scores, k, method="higher"))
    return hm


def save_booster(booster: lgb.Booster, path) -> None:
    """Gzipped LightGBM text model (about a fifth of the plain text size)."""
    import gzip

    with gzip.open(path, "wt", encoding="utf-8") as f:
        f.write(booster.model_to_string())


def load_booster(path) -> lgb.Booster:
    import gzip

    with gzip.open(path, "rt", encoding="utf-8") as f:
        return lgb.Booster(model_str=f.read())


def importance(hm: HorizonModel, X: pd.DataFrame) -> pd.Series:
    """Mean |SHAP value| per feature for the median model (LightGBM's pred_contrib)."""
    contrib = hm.models[0.50].booster_.predict(X[hm.features], pred_contrib=True)[:, :-1]
    return pd.Series(np.abs(contrib).mean(axis=0), index=hm.features).sort_values(ascending=False)


def contributions(hm: HorizonModel, X: pd.DataFrame) -> pd.DataFrame:
    contrib = hm.models[0.50].booster_.predict(X[hm.features], pred_contrib=True)[:, :-1]
    return pd.DataFrame(contrib, columns=hm.features, index=X.index)
