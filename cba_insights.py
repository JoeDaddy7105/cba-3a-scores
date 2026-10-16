"""
cba_insights.py - panel-adjusted ratings, growth trends, head-to-head and caption analysis.

The core idea
-------------
Every show has its own judging panel, and some panels score higher than others. A raw 65 at one
show is not the same as a 65 somewhere else. But lots of bands (all classes) go to more than one
show, so we can estimate how generous each panel was and take that out. Model, for band b at
show s on day t:

    score = strength_b  -  (G + d_b) * max(0, (T - lag) - t)  +  panel_s  +  noise

    strength_b  band b's level on State day T, on an "average panel" scale
    G           typical improvement per day (fitted on past seasons), d_b this band's deviation
    panel_s     how generous show s's panel was (shrunk toward 0 when few bands link it)

Fitted as a Bayesian linear regression with Gaussian priors, so every estimate comes with an
uncertainty and the uncertainties are correlated the way they should be (two bands that only ever
met at one show have strongly linked estimates).

At State, all 3A bands face the same panel, so that panel's effect cancels out of the ranking.
Who makes finals depends only on the strengths and on how each band performs that day.
"""
from __future__ import annotations

import datetime as dt
import math

import numpy as np
import pandas as pd

PRE_STATE = {"Prelims", "Finals", "Regional"}


def _days(a, b) -> float:
    return (pd.Timestamp(b) - pd.Timestamp(a)).days


def panel_key(row) -> str:
    return f"{row['event_date']}|{row['event']}|{row['round']}"


# ---------------------------------------------------------------- fit
# Settings picked by backtesting on 2024 and 2025 State semifinals (all classes, 108 band results)
DEFAULTS = {"G": 0.27, "lag": 3, "tau_growth": 0.04, "tau_panel": 2.0, "perf_sd": 1.2}
# share of a season's improvement that shows up in each caption (measured on 2024-2025)
CAPTION_GROWTH_SHARE = {"mp": 0.19, "vp": 0.24, "ge": 0.57}
CAPTION_WEIGHT = {"mp": 0.2, "vp": 0.2, "ge": 0.6}


def fit_ratings(scores: pd.DataFrame, season: int, target_date: str, as_of: str | None = None,
                G: float = 0.27, lag: int = 3, tau_growth: float = 0.04, tau_panel: float = 2.0,
                tau_strength: float = 25.0, sigma: float | None = None, min_shows: int = 1,
                iters: int = 4, value: str = "total") -> dict:
    """Fit strength/growth/panel model on one season's pre-State scores (all classes)."""
    df = scores[(scores["season"] == season) & scores["round"].isin(PRE_STATE)].copy()
    if value != "total":
        df = df.dropna(subset=[value])
        df["total_raw"] = df["total"]
        df["total"] = df[value]
    if as_of:
        df = df[pd.to_datetime(df["event_date"]) <= pd.Timestamp(as_of)]
    df = df[df["class"].isin(["1A", "2A", "3A", "4A", "5A"])]
    df["panel"] = df.apply(panel_key, axis=1)
    counts = df.groupby("school")["panel"].nunique()
    df = df[df["school"].isin(counts[counts >= min_shows].index)].reset_index(drop=True)
    if df.empty:
        return {}

    bands = sorted(df["school"].unique())
    panels = sorted(df["panel"].unique())
    bi = {b: i for i, b in enumerate(bands)}
    pi = {p: i for i, p in enumerate(panels)}
    nb, npn = len(bands), len(panels)
    T = pd.Timestamp(target_date)
    x = np.maximum(0.0, (T - pd.Timedelta(days=lag) - pd.to_datetime(df["event_date"])).dt.days.to_numpy(float))
    y = df["total"].to_numpy(float) + G * x           # move every score forward to State day

    n = len(df)
    X = np.zeros((n, 2 * nb + npn))
    rows = np.arange(n)
    b_idx = df["school"].map(bi).to_numpy()
    X[rows, b_idx] = 1.0
    X[rows, nb + b_idx] = -x
    X[rows, 2 * nb + df["panel"].map(pi).to_numpy()] = 1.0

    prior_prec = np.concatenate([np.full(nb, 1 / tau_strength ** 2),
                                 np.full(nb, 1 / tau_growth ** 2),
                                 np.full(npn, 1 / tau_panel ** 2)])
    prior_mean = np.zeros(2 * nb + npn)
    # center strengths on each band's mean forward score so the weak prior doesn't pull them to 0
    start = pd.Series(y).groupby(b_idx).mean().reindex(range(nb)).to_numpy()
    prior_mean[:nb] = start

    s2 = (sigma or 1.5) ** 2
    for _ in range(iters if sigma is None else 1):
        A = X.T @ X / s2 + np.diag(prior_prec)
        cov = np.linalg.inv(A)
        mu = cov @ (X.T @ y / s2 + prior_prec * prior_mean)
        resid = y - X @ mu
        if sigma is None:
            # effective degrees of freedom so the noise estimate isn't too small
            hat_trace = np.trace(X @ cov @ X.T) / s2
            s2 = max(float(resid @ resid) / max(n - hat_trace, 1.0), 0.25)
    sd = np.sqrt(np.diag(cov))

    band_tab = pd.DataFrame({
        "school": bands,
        "strength": mu[:nb], "strength_sd": sd[:nb],
        "growth_per_week": (G + mu[nb:2 * nb]) * 7, "growth_sd_week": sd[nb:2 * nb] * 7,
        "n_scores": pd.Series(b_idx).value_counts().reindex(range(nb)).fillna(0).astype(int).to_numpy(),
        "n_shows": df.groupby("school")["panel"].nunique().reindex(bands).to_numpy(),
    })
    cls = df.sort_values("event_date").groupby("school")["class"].last()
    band_tab["class"] = band_tab["school"].map(cls)
    panel_tab = pd.DataFrame({"panel": panels, "effect": mu[2 * nb:], "effect_sd": sd[2 * nb:]})
    panel_tab[["event_date", "event", "round"]] = panel_tab["panel"].str.split("|", expand=True)
    panel_tab["n_bands"] = panel_tab["panel"].map(df.groupby("panel")["school"].nunique())
    df["fitted"] = X @ mu - G * x
    df["resid"] = df["total"] - df["fitted"]
    df["adjusted"] = df["total"] - df["panel"].map(dict(zip(panels, mu[2 * nb:])))
    return {"bands": band_tab, "panels": panel_tab, "obs": df, "mu": mu, "cov": cov,
            "band_index": bi, "nb": nb, "sigma": math.sqrt(s2), "G": G, "lag": lag,
            "target_date": target_date}


# ---------------------------------------------------------------- simulate State
def simulate_state(fit: dict, field: list[str], finals_spots: int = 4, perf_sd: float = 1.2,
                   n: int = 20000, seed: int = 11, extra_sd: dict | None = None) -> pd.DataFrame:
    """Draw strengths from the posterior (jointly), add a State-day performance wobble,
    rank the field. The State panel's own effect is the same for every band, so it drops out."""
    rng = np.random.default_rng(seed)
    bi = fit["band_index"]
    have = [b for b in field if b in bi]
    idx = np.array([bi[b] for b in have])
    mu, cov = fit["mu"][idx], fit["cov"][np.ix_(idx, idx)]
    draws = rng.multivariate_normal(mu, cov, size=n, method="cholesky")
    sd_extra = np.array([(extra_sd or {}).get(b, 0.0) for b in have])
    perf = rng.normal(0, 1, size=draws.shape) * np.sqrt(perf_sd ** 2 + sd_extra ** 2)
    semis = draws + perf
    rank = (-semis).argsort(axis=1).argsort(axis=1) + 1
    out = pd.DataFrame({"school": have, "strength": mu, "strength_sd": np.sqrt(np.diag(cov))})
    out["p_finals"] = (rank <= finals_spots).mean(0)
    out["p_first"] = (rank == 1).mean(0)
    out["p_top3"] = (rank <= 3).mean(0)
    out["median_rank"] = np.median(rank, axis=0)
    out["rank_dist"] = [np.bincount(rank[:, j], minlength=len(have) + 1)[1:] / n for j in range(len(have))]
    # how far each band is from the finals line in a typical run
    cut = np.sort(semis, axis=1)[:, -finals_spots]          # score of the last finalist
    nxt = np.sort(semis, axis=1)[:, -finals_spots - 1]      # first band out
    line = (cut + nxt) / 2
    out["gap_to_line_median"] = np.median(semis - line[:, None], axis=0)
    return out.sort_values("strength", ascending=False).reset_index(drop=True)


# ---------------------------------------------------------------- head-to-head
def head_to_head(scores: pd.DataFrame, season: int, cls: str, focus: str) -> pd.DataFrame:
    """Every time the focus band and another band in the class were scored by the same panel."""
    df = scores[(scores["season"] == season) & (scores["class"] == cls) & scores["round"].isin(PRE_STATE)]
    out = []
    for (d, e, r), g in df.groupby(["event_date", "event", "round"]):
        if focus not in set(g["school"]):
            continue
        f = float(g.loc[g["school"] == focus, "total"].iloc[0])
        for _, o in g[g["school"] != focus].iterrows():
            out.append({"opponent": o["school"], "event": e, "event_date": d, "round": r,
                        "focus_score": f, "opp_score": float(o["total"]), "margin": round(f - float(o["total"]), 3)})
    return pd.DataFrame(out).sort_values(["opponent", "event_date"]) if out else pd.DataFrame()


# ---------------------------------------------------------------- captions
CAPTION_COLS = ["mp", "vp", "ge"]
CAPTION_NAMES = {"mp": "Music Performance", "vp": "Visual Performance", "ge": "General Effect"}
CAPTION_MAX = {"mp": 20, "vp": 20, "ge": 60}


def caption_table(scores: pd.DataFrame, season: int, cls: str) -> pd.DataFrame:
    if not set(CAPTION_COLS) <= set(scores.columns):
        return pd.DataFrame()
    df = scores[(scores["season"] == season) & (scores["class"] == cls) & scores["round"].isin(PRE_STATE)]
    df = df.dropna(subset=CAPTION_COLS)
    return df[["school", "event", "event_date", "round", "total", *CAPTION_COLS]].sort_values(["school", "event_date"])


# ---------------------------------------------------------------- backtest
def backtest(scores: pd.DataFrame, season: int, classes=("1A", "2A", "3A", "4A", "5A"), **kw) -> pd.DataFrame:
    """Fit on a past season's pre-State scores, compare with that season's State semifinals."""
    semis = scores[(scores["season"] == season) & (scores["round"] == "State Semifinals")]
    if semis.empty:
        return pd.DataFrame()
    T = semis["event_date"].min()
    fit = fit_ratings(scores, season, T, **kw)
    if not fit:
        return pd.DataFrame()
    b = fit["bands"].set_index("school")
    out = []
    for (d, cl), g in semis.groupby(["event_date", "class"]):
        if cl not in classes:
            continue
        g = g[g["school"].isin(b.index)]
        if len(g) < 3:
            continue
        pred = b.loc[g["school"], "strength"].to_numpy()
        act = g["total"].to_numpy()
        offset = float(np.mean(act - pred))        # State panel effect for that class/round
        for sch, p, a_, sd in zip(g["school"], pred, act, b.loc[g["school"], "strength_sd"]):
            out.append({"season": season, "class": cl, "date": d, "school": sch,
                        "pred": p + offset, "actual": a_, "pred_sd": sd})
    res = pd.DataFrame(out)
    if res.empty:
        return res
    res["err"] = res["actual"] - res["pred"]
    res["pred_rank"] = res.groupby(["class", "date"])["pred"].rank(ascending=False)
    res["actual_rank"] = res.groupby(["class", "date"])["actual"].rank(ascending=False)
    return res


def naive_backtest(scores: pd.DataFrame, season: int, rate: float = 0.27, lag: int = 3,
                   classes=("1A", "2A", "3A", "4A", "5A")) -> pd.DataFrame:
    """The original approach: each band's scores pushed forward with the average gain, no panel adjustment."""
    semis = scores[(scores["season"] == season) & (scores["round"] == "State Semifinals")]
    if semis.empty:
        return pd.DataFrame()
    T = pd.Timestamp(semis["event_date"].min())
    pre = scores[(scores["season"] == season) & scores["round"].isin(PRE_STATE)].copy()
    days = (T - pd.to_datetime(pre["event_date"])).dt.days
    pre["fwd"] = pre["total"] + rate * np.maximum(0, days - lag)
    pre["w"] = 1 / (2.3 + 0.007 * days) ** 2
    est = pre.groupby("school").apply(lambda g: np.average(g["fwd"], weights=g["w"]), include_groups=False)
    out = []
    for (d, cl), g in semis.groupby(["event_date", "class"]):
        if cl not in classes:
            continue
        g = g[g["school"].isin(est.index)]
        if len(g) < 3:
            continue
        pred = est.loc[g["school"]].to_numpy()
        act = g["total"].to_numpy()
        offset = float(np.mean(act - pred))
        for sch, p, a_ in zip(g["school"], pred, act):
            out.append({"season": season, "class": cl, "date": d, "school": sch, "pred": p + offset, "actual": a_})
    res = pd.DataFrame(out)
    if res.empty:
        return res
    res["err"] = res["actual"] - res["pred"]
    res["pred_rank"] = res.groupby(["class", "date"])["pred"].rank(ascending=False)
    res["actual_rank"] = res.groupby(["class", "date"])["actual"].rank(ascending=False)
    return res


def score_backtest(res: pd.DataFrame) -> dict:
    if res.empty:
        return {}
    from itertools import combinations
    pairs = ok = 0
    for _, g in res.groupby(["season", "class", "date"]):
        for (_, a), (_, b) in combinations(g.iterrows(), 2):
            if a["actual"] == b["actual"]:
                continue
            pairs += 1
            ok += (a["pred"] > b["pred"]) == (a["actual"] > b["actual"])
    return {"n": int(len(res)), "rmse": float(np.sqrt((res["err"] ** 2).mean())),
            "mae": float(res["err"].abs().mean()), "pairs_right": ok / pairs if pairs else None,
            "rank_mae": float((res["pred_rank"] - res["actual_rank"]).abs().mean())}


# ---------------------------------------------------------------- everything the dashboard needs
def run_insights(scores: pd.DataFrame, cfg: dict, as_of: str | None = None) -> dict:
    season, cls, focus = cfg["season"], cfg["focus_class"], cfg["focus_school"]
    T, spots, nq = cfg["state_semis_date"], int(cfg["finals_spots"]), int(cfg.get("state_qualifiers", 10))
    p = {**DEFAULTS, **cfg.get("panel_model", {})}
    fit = fit_ratings(scores, season, T, as_of=as_of, G=p["G"], lag=p["lag"],
                      tau_growth=p["tau_growth"], tau_panel=p["tau_panel"])
    if not fit:
        return {}
    cur = scores[(scores["season"] == season) & (scores["class"] == cls)]
    bands = fit["bands"]
    rat = bands[bands["class"] == cls].sort_values("strength", ascending=False).reset_index(drop=True)

    # who is in the State field
    semis = cur[cur["round"] == "State Semifinals"]
    reg = cur[cur["round"] == "Regional"].sort_values("total", ascending=False)
    if len(semis):
        field, field_basis = list(semis["school"]), "State semifinals lineup"
    elif len(reg) >= nq:
        field, field_basis = list(reg["school"][:nq]), f"top {nq} regional scores"
    else:
        field, field_basis = list(rat["school"][:nq]), f"top {nq} panel-adjusted ratings (regionals not finished)"

    sim = simulate_state(fit, field, finals_spots=spots, perf_sd=p["perf_sd"])
    # pairwise and "what if" for the focus band, reusing the same posterior draws
    bi = fit["band_index"]
    have = [b for b in field if b in bi]
    idx = np.array([bi[b] for b in have])
    mu, cov = fit["mu"][idx], fit["cov"][np.ix_(idx, idx)]
    L = np.linalg.cholesky(cov + 1e-9 * np.eye(len(idx)))
    rng = np.random.default_rng(5)
    draws = mu + rng.standard_normal((12000, len(idx))) @ L.T + rng.normal(0, p["perf_sd"], (12000, len(idx)))
    pairwise, boost = [], []
    if focus in have:
        j = have.index(focus)
        for k, b in enumerate(have):
            if b != focus:
                pairwise.append({"opponent": b, "p_ahead": float((draws[:, j] > draws[:, k]).mean())})
        for d in np.arange(-2.0, 2.01, 0.25):
            dd = draws.copy(); dd[:, j] += d
            rank = (-dd).argsort(axis=1).argsort(axis=1)[:, j] + 1
            boost.append({"boost": round(float(d), 2), "p_finals": float((rank <= spots).mean())})

    # if State semis already happened, the answer is the real one
    if len(semis):
        fin = cur[cur["round"] == "State Finals"]
        top = set(fin["school"]) if len(fin) else set(semis.sort_values("total", ascending=False)["school"][:spots])
        sim["p_finals"] = sim["school"].isin(top).astype(float)

    # caption-level ratings (same model, one caption at a time)
    cap_rows = []
    if "mp" in scores.columns:
        for c in ("mp", "vp", "ge"):
            f = fit_ratings(scores, season, T, as_of=as_of, value=c, G=p["G"] * CAPTION_GROWTH_SHARE[c],
                            lag=p["lag"], tau_growth=p["tau_growth"] * CAPTION_GROWTH_SHARE[c],
                            tau_panel=p["tau_panel"] * math.sqrt(CAPTION_WEIGHT[c]))
            if f:
                bt = f["bands"][f["bands"]["school"].isin(rat["school"])]
                for _, r in bt.iterrows():
                    cap_rows.append({"school": r["school"], "caption": c, "strength": r["strength"], "sd": r["strength_sd"]})
    caps = pd.DataFrame(cap_rows)

    panels = fit["panels"].copy()
    obs = fit["obs"]
    cls_obs = obs[obs["class"] == cls][["school", "event", "event_date", "round", "total", "adjusted", "fitted", "resid"]]
    linked = panels["panel"].map(obs[obs["class"] == cls].groupby("panel")["school"].nunique()).fillna(0)
    panels["n_class_bands"] = linked.astype(int)

    return {"fit": fit, "ratings": rat, "field": have, "field_basis": field_basis, "finals": sim,
            "pairwise": pd.DataFrame(pairwise), "boost": pd.DataFrame(boost), "captions": caps,
            "panels": panels, "class_obs": cls_obs, "mu": mu.tolist(), "chol": L.tolist(),
            "perf_sd": p["perf_sd"], "sigma": fit["sigma"], "params": p,
            "h2h": head_to_head(scores, season, cls, focus)}


def run_backtests(scores: pd.DataFrame, cfg: dict) -> pd.DataFrame:
    p = {**DEFAULTS, **cfg.get("panel_model", {})}
    out = []
    for s in sorted(set(scores["season"]) - {cfg["season"]}):
        bt = backtest(scores, s, G=p["G"], lag=p["lag"], tau_growth=p["tau_growth"], tau_panel=p["tau_panel"])
        nb = naive_backtest(scores, s, rate=p["G"], lag=p["lag"])
        if len(bt):
            bt["model"] = "panel-adjusted"; out.append(bt)
        if len(nb):
            nb["model"] = "original"; out.append(nb)
    return pd.concat(out, ignore_index=True) if out else pd.DataFrame()
