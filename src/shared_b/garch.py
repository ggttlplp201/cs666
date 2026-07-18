"""GARCH(1,1) conditional-volatility estimation (Shared §11, Nikolaenko).

Direction is barely predictable but volatility IS (alpha+beta ~ 0.82-0.91 on
BUFF skins). Used two ways: as a model feature and for volatility-targeted
sizing (scale size inversely to forecast vol). Implemented directly on
numpy/scipy — no external `arch` dependency.
"""

from __future__ import annotations

from dataclasses import dataclass

import numpy as np
from scipy import optimize


@dataclass
class GarchFit:
    omega: float
    alpha: float
    beta: float
    last_sigma2: float
    loglik: float

    @property
    def persistence(self) -> float:
        return self.alpha + self.beta

    @property
    def unconditional_var(self) -> float:
        p = self.persistence
        return self.omega / (1 - p) if p < 0.999 else self.last_sigma2

    def forecast_sigma(self, horizon: int = 1) -> float:
        """Sqrt of h-step-ahead conditional variance (per-day vol)."""
        s2 = self.last_sigma2
        p = self.persistence
        var_h = self.unconditional_var + (p**horizon) * (s2 - self.unconditional_var)
        return float(np.sqrt(max(var_h, 1e-12)))


def _neg_loglik(params: np.ndarray, r: np.ndarray) -> float:
    omega, alpha, beta = params
    if omega <= 0 or alpha < 0 or beta < 0 or alpha + beta >= 0.9999:
        return 1e10
    n = len(r)
    s2 = np.empty(n)
    s2[0] = r.var() if r.var() > 1e-12 else 1e-8
    for t in range(1, n):
        s2[t] = omega + alpha * r[t - 1] ** 2 + beta * s2[t - 1]
    s2 = np.clip(s2, 1e-12, None)
    ll = -0.5 * (np.log(2 * np.pi) + np.log(s2) + r**2 / s2)
    return float(-ll.sum())


def fit_garch(returns: np.ndarray, min_obs: int = 60) -> GarchFit | None:
    """MLE fit of GARCH(1,1) on (demeaned) daily returns. None if too short
    or degenerate."""
    r = np.asarray(returns, dtype=float)
    r = r[np.isfinite(r)]
    if len(r) < min_obs or r.std() < 1e-8:
        return None
    r = r - r.mean()
    var = r.var()
    x0 = np.array([0.1 * var, 0.1, 0.8])
    res = optimize.minimize(
        _neg_loglik,
        x0,
        args=(r,),
        method="Nelder-Mead",
        options={"maxiter": 2000, "xatol": 1e-8, "fatol": 1e-8},
    )
    omega, alpha, beta = res.x
    if not res.success and res.fun >= 1e9:
        return None
    # recompute final sigma2
    n = len(r)
    s2 = r.var() if r.var() > 1e-12 else 1e-8
    for t in range(1, n):
        s2 = omega + alpha * r[t - 1] ** 2 + beta * s2
    return GarchFit(
        omega=float(omega),
        alpha=float(alpha),
        beta=float(beta),
        last_sigma2=float(max(s2, 1e-12)),
        loglik=-float(res.fun),
    )


def ewma_sigma(returns: np.ndarray, lam: float = 0.94) -> float:
    """RiskMetrics EWMA fallback when GARCH can't fit (short history)."""
    r = np.asarray(returns, dtype=float)
    r = r[np.isfinite(r)]
    if len(r) < 10:
        return 0.03  # conservative default daily vol for skins
    r = r - r.mean()
    s2 = r[:10].var()
    for x in r[10:]:
        s2 = lam * s2 + (1 - lam) * x * x
    return float(np.sqrt(max(s2, 1e-12)))


def forecast_daily_vol(returns: np.ndarray) -> float:
    """Best-effort next-day vol: GARCH(1,1) if fittable, EWMA otherwise."""
    fit = fit_garch(returns)
    if fit is not None and 0.3 <= fit.persistence <= 0.9999:
        return fit.forecast_sigma(1)
    return ewma_sigma(returns)
