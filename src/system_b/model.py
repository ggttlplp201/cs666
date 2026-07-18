"""Cross-sectional ranker (System B §4): walk-forward tree ensemble on
N-day forward log returns.

Empirical grounding (RESEARCH_INDEX):
- target = returns, never prices (Nikolaenko: prices non-stationary);
- trees (RF/XGBoost) as the workhorse, linear as baseline, no LSTM
  (Pettersson: RF 0.49 > XGB 0.45 > Linear 0.42 >> LSTM 0.18);
- strictly time-ordered refits with an embargo of `horizon` days so no
  training target overlaps the prediction day (look-ahead control);
- regularized (min_samples_leaf / depth caps) against the 0.77-train vs
  0.49-test overfit gap the paper measured;
- expectation: ~0.5 R2 ceiling on daily horizons — rank, don't believe levels.
"""

from __future__ import annotations

from dataclasses import dataclass, field

import numpy as np
import pandas as pd

from .features import MODEL_FEATURES

try:  # optional dependency; sklearn fallback below
    from xgboost import XGBRegressor
    HAS_XGB = True
except Exception:  # pragma: no cover
    HAS_XGB = False


def _make_model(model_type: str, seed: int = 0):
    from sklearn.ensemble import HistGradientBoostingRegressor, RandomForestRegressor
    from sklearn.linear_model import Ridge
    from sklearn.pipeline import make_pipeline
    from sklearn.preprocessing import StandardScaler

    # Hyperparameters seeded from Pettersson's winning configs (RF baseline
    # n=200/depth=None/leaf=5/sqrt beat its own tuned variant; XGB 500/6/0.05),
    # with leaf sizes raised for our smaller cross-sections. NO early stopping
    # on test data (the paper's one leakage sin — not repeated here).
    if model_type == "xgboost" and HAS_XGB:
        return XGBRegressor(
            n_estimators=500, max_depth=6, learning_rate=0.05,
            subsample=0.8, colsample_bytree=0.8, min_child_weight=20,
            reg_lambda=1.0, random_state=seed, verbosity=0,
        )
    if model_type in ("xgboost", "hist_gb"):  # xgboost unavailable -> sklearn GBT
        return HistGradientBoostingRegressor(
            max_iter=500, max_depth=6, learning_rate=0.05,
            min_samples_leaf=40, l2_regularization=1.0, random_state=seed,
        )
    if model_type == "random_forest":
        return RandomForestRegressor(
            n_estimators=200, max_depth=None, min_samples_leaf=10,
            max_features="sqrt", random_state=seed, n_jobs=-1,
        )
    if model_type == "ridge":
        return make_pipeline(StandardScaler(), Ridge(alpha=10.0))
    raise ValueError(f"unknown model type {model_type}")


def forward_log_returns(frames: dict[str, pd.DataFrame], horizon: int) -> pd.DataFrame:
    """day x item frame of H-CALENDAR-day forward log returns (TRAINING TARGETS
    ONLY — never features).

    Shifted on a daily calendar, not by rows: on gapped panels (missed collector
    days) a row shift would realize targets `horizon + gap` days out, past the
    walk-forward embargo (which is measured in calendar days), leaking future
    returns into training. Prices are forward-filled onto the calendar so the
    target at t uses the last observed price at or before t+horizon."""
    cols = {}
    for item, df in frames.items():
        px = df["sell_price"]
        if px.empty:
            continue
        daily = px.reindex(pd.date_range(px.index.min(), px.index.max(), freq="D")).ffill()
        fwd = np.log(daily.shift(-horizon, freq="D").reindex(daily.index) / daily)
        cols[item] = fwd.reindex(px.index)
    return pd.DataFrame(cols)


@dataclass
class RefitRecord:
    day: pd.Timestamp
    n_train: int
    train_r2: float
    importances: dict[str, float]


@dataclass
class WalkForwardRanker:
    """Refits every `refit_every` days on all feature rows with
    day <= t - horizon - 1 (embargo), predicts today's cross-section.

    Training data accumulates in `history` as the caller feeds each decision
    day's feature frame — the same rows the strategy saw, so train and
    inference distributions match by construction.
    """

    model_type: str = "xgboost"
    horizon: int = 21
    refit_every: int = 30
    min_train_rows: int = 500
    seed: int = 0
    feature_cols: list[str] = field(default_factory=lambda: list(MODEL_FEATURES))

    history: list[pd.DataFrame] = field(default_factory=list)
    _model: object | None = None
    _last_fit: pd.Timestamp | None = None
    refits: list[RefitRecord] = field(default_factory=list)

    def observe(self, day: pd.Timestamp, features: pd.DataFrame) -> None:
        """Store today's cross-section for future training."""
        if features.empty:
            return
        df = features.copy()
        df["__day"] = day
        self.history.append(df)

    def _training_matrix(self, day: pd.Timestamp, targets: pd.DataFrame) -> tuple[pd.DataFrame, pd.Series]:
        cutoff = day - pd.Timedelta(days=self.horizon + 1)  # embargo: target window fully realized
        xs, ys = [], []
        for df in self.history:
            d = df["__day"].iloc[0]
            if d > cutoff or d not in targets.index:
                continue
            t_row = targets.loc[d]
            y = t_row.reindex(df.index)
            ok = y.notna()
            if ok.sum() == 0:
                continue
            xs.append(df.loc[ok, self.feature_cols])
            ys.append(y[ok])
        if not xs:
            return pd.DataFrame(), pd.Series(dtype=float)
        X = pd.concat(xs)
        y = pd.concat(ys)
        # winsorize fat-tailed targets so the loss isn't owned by outliers
        lo, hi = y.quantile([0.005, 0.995])
        return X, y.clip(lo, hi)

    def maybe_refit(self, day: pd.Timestamp, targets: pd.DataFrame) -> bool:
        if self._last_fit is not None and (day - self._last_fit).days < self.refit_every:
            return False
        X, y = self._training_matrix(day, targets)
        if len(X) < self.min_train_rows:
            return False
        model = _make_model(self.model_type, self.seed)
        model.fit(X.to_numpy(), y.to_numpy())
        self._model = model
        self._last_fit = day
        train_r2 = float(model.score(X.to_numpy(), y.to_numpy()))
        self.refits.append(
            RefitRecord(day=day, n_train=len(X), train_r2=train_r2,
                        importances=self.feature_importances())
        )
        return True

    def predict(self, features: pd.DataFrame) -> pd.Series | None:
        """Predicted H-day forward log return per item; None before first fit
        (caller falls back to the structural composite alone)."""
        if self._model is None or features.empty:
            return None
        X = features[self.feature_cols].to_numpy()
        preds = self._model.predict(X)
        return pd.Series(preds, index=features.index, name="pred")

    def feature_importances(self) -> dict[str, float]:
        m = self._model
        if m is None:
            return {}
        imp = getattr(m, "feature_importances_", None)
        if imp is None and hasattr(m, "named_steps"):  # ridge pipeline
            ridge = list(m.named_steps.values())[-1]
            coef = getattr(ridge, "coef_", None)
            imp = np.abs(coef) if coef is not None else None
        if imp is None:
            return {}
        pairs = sorted(zip(self.feature_cols, imp), key=lambda kv: -abs(kv[1]))
        return {k: float(v) for k, v in pairs}

    # ------------------------------------------------------------ diagnostics
    def rank_ic(self, targets: pd.DataFrame, predictions: dict[pd.Timestamp, pd.Series]) -> pd.Series:
        """Daily Spearman IC between predictions and realized forward returns."""
        from scipy.stats import spearmanr

        out = {}
        for day, pred in predictions.items():
            if day not in targets.index:
                continue
            realized = targets.loc[day].reindex(pred.index)
            ok = realized.notna() & pred.notna()
            if ok.sum() < 5:
                continue
            rho = spearmanr(pred[ok], realized[ok]).statistic
            if np.isfinite(rho):
                out[day] = float(rho)
        return pd.Series(out).sort_index()
