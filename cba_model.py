"""
cba_model.py - projections and state / finals odds for one class.

The idea, in plain terms:
  * Bands keep improving all season. Using last season's data, we fit how many
    points a band typically gains between a given date and State semifinals.
  * Each band's current scores are pushed forward to a projected semifinals score,
    with an error band sized from how wrong that projection was last year.
  * Bands with no score yet this season start from last year's semifinals score,
    shifted by how much this year's returning bands are running ahead or behind,
    with wider uncertainty.
  * We then replay regionals and State thousands of times. Each run draws a score
    for every band, takes the top N per regional into State, and the top N at
    semifinals into finals. The share of runs a band makes it is its probability.

Caveats: different shows have different judges, early-season scores are noisy,
and the regional allotments come from config.json (last season's pattern unless
you update them once CBA publishes this year's numbers).
"""
from __future__ import annotations

import datetime as dt
import math

import numpy as np
import pandas as pd

PRE_STATE_ROUNDS = {"Prelims", "Finals", "Regional"}
ROUND_ORDER = {"Prelims": 1, "Finals": 2, "Regional": 3, "State Quarterfinals": 4, "State Semifinals": 5, "State Finals": 6}


def chrono(df: pd.DataFrame) -> pd.DataFrame:
    """Sort by date, and within a day put prelims before finals."""
    return df.assign(_ro=df["round"].map(ROUND_ORDER).fillna(9)).sort_values(["event_date", "_ro"]).drop(columns="_ro")


def _d(s) -> dt.date:
    return pd.to_datetime(s).date()


# ---------------------------------------------------------------- calibration
def calibrate(scores: pd.DataFrame, cls: str, season: int) -> dict:
    """Fit gain(days) = rate * max(0, days - d0) and error sd(days) = s0 + s1*days
    from earlier seasons' scores vs that season's State semifinals score."""
    pairs = []
    hist = scores[(scores["class"] == cls) & (scores["season"] < season)]
    for (yr, school), g in hist.groupby(["season", "school"]):
        semis = g[g["round"] == "State Semifinals"]
        if semis.empty:
            continue
        s_date, s_score = _d(semis["event_date"].iloc[0]), float(semis["total"].iloc[0])
        for _, r in g[g["round"].isin(PRE_STATE_ROUNDS)].iterrows():
            days = (s_date - _d(r["event_date"])).days
            if days > 0:
                pairs.append((days, s_score - float(r["total"])))
    if len(pairs) < 6:
        return {"rate": 0.25, "d0": 10, "s0": 1.5, "s1": 0.06, "n_pairs": len(pairs), "fallback": True}

    days = np.array([p[0] for p in pairs], float)
    gain = np.array([p[1] for p in pairs], float)
    best = None
    for d0 in range(0, 21):
        x = np.maximum(0, days - d0)
        rate = float((x @ gain) / (x @ x)) if (x @ x) > 0 else 0.0
        sse = float(((gain - rate * x) ** 2).sum())
        if best is None or sse < best[0]:
            best = (sse, rate, d0)
    _, rate, d0 = best
    resid = gain - rate * np.maximum(0, days - d0)
    # error grows with distance: regress |resid| on days, scale to an sd
    absr = np.abs(resid) * math.sqrt(math.pi / 2)
    A = np.vstack([np.ones_like(days), days]).T
    s0, s1 = np.linalg.lstsq(A, absr, rcond=None)[0]
    s0, s1 = max(float(s0), 1.0), max(float(s1), 0.0)
    return {"rate": round(rate, 4), "d0": int(d0), "s0": round(s0, 3), "s1": round(s1, 4),
            "n_pairs": len(pairs), "resid_sd": round(float(resid.std(ddof=1)), 3), "fallback": False}


def gain_to(days: float, cal: dict) -> float:
    return cal["rate"] * max(0.0, days - cal["d0"])


def err_sd(days: float, cal: dict) -> float:
    return cal["s0"] + cal["s1"] * max(0.0, days)


# ---------------------------------------------------------------- projections
def project(scores: pd.DataFrame, classes: pd.DataFrame, cfg: dict, cal: dict, as_of: dt.date | None = None) -> pd.DataFrame:
    season, cls = cfg["season"], cfg["focus_class"]
    semis_date = _d(cfg["state_semis_date"])
    as_of = as_of or dt.date.today()
    cur = scores[(scores["season"] == season) & (scores["class"] == cls)
                 & (pd.to_datetime(scores["event_date"]).dt.date <= as_of)]
    region = dict(zip(classes["school"], classes["region"].fillna("")))
    field = sorted(set(cur["school"]) | set(classes.loc[classes["class"] == cls, "school"]))

    out = []
    for school in field:
        g = chrono(cur[(cur["school"] == school) & cur["round"].isin(PRE_STATE_ROUNDS)])
        rec = {"school": school, "region": region.get(school, ""), "n_scores": len(g)}
        if len(g):
            proj, sds = [], []
            for _, r in g.iterrows():
                days = (semis_date - _d(r["event_date"])).days
                proj.append(float(r["total"]) + gain_to(days, cal))
                sds.append(err_sd(days, cal))
            w = 1 / np.square(sds)
            last = g.iloc[-1]
            rec.update({
                "basis": "this season",
                "last_score": float(last["total"]), "last_event": last["event"],
                "last_date": str(last["event_date"])[:10],
                "proj_semis": float((w * np.array(proj)).sum() / w.sum()),
                # scores from one band are correlated, so don't shrink below the best single sd
                "proj_sd": float(min(sds)),
            })
        else:
            rec.update({"basis": "last season", "last_score": np.nan, "last_event": "", "last_date": "",
                        "proj_semis": np.nan, "proj_sd": np.nan})
        out.append(rec)
    df = pd.DataFrame(out)

    # carry-over bands: last season's semis (or regional + gain), shifted by the
    # average change among returning bands that do have scores this season
    prev = scores[(scores["season"] == season - 1) & (scores["class"] == cls)]
    def prev_level(school):
        s = prev[(prev["school"] == school) & (prev["round"] == "State Semifinals")]
        if len(s):
            return float(s["total"].iloc[0])
        r = chrono(prev[(prev["school"] == school) & prev["round"].isin(PRE_STATE_ROUNDS)])
        if len(r):
            psemis = prev[prev["round"] == "State Semifinals"]["event_date"]
            ref = _d(psemis.iloc[0]) if len(psemis) else _d(r["event_date"].iloc[-1])
            return float(r["total"].iloc[-1]) + gain_to((ref - _d(r["event_date"].iloc[-1])).days, cal)
        return np.nan
    df["prev_level"] = df["school"].map(prev_level)
    both = df[(df["basis"] == "this season") & df["prev_level"].notna()]
    shift = float((both["proj_semis"] - both["prev_level"]).mean()) if len(both) else 0.0
    yoy_sd = float(max((both["proj_semis"] - both["prev_level"]).std(ddof=1), 3.0)) if len(both) > 2 else 4.0
    co = df["basis"] == "last season"
    df.loc[co, "proj_semis"] = df.loc[co, "prev_level"] + shift
    df.loc[co, "proj_sd"] = math.sqrt(yoy_sd ** 2 + cal["s0"] ** 2)
    df.attrs.update({"season_shift": round(shift, 2), "yoy_sd": round(yoy_sd, 2)})
    return df[df["proj_semis"].notna()].reset_index(drop=True)


# ---------------------------------------------------------------- simulation
def simulate(proj: pd.DataFrame, scores: pd.DataFrame, cfg: dict, cal: dict, n: int | None = None,
             spots_override: dict | None = None, seed: int = 7, qualifiers: int | None = None) -> pd.DataFrame:
    rng = np.random.default_rng(seed)
    if qualifiers is not None:
        cfg = {**cfg, "state_qualifiers": qualifiers}
    n = n or cfg.get("simulations", 20000)
    season, cls = cfg["season"], cfg["focus_class"]
    semis_date = _d(cfg["state_semis_date"])
    spots = {**cfg["state_spots_by_region"], **(spots_override or {})}
    k = len(proj)
    mu, sd = proj["proj_semis"].to_numpy(), proj["proj_sd"].to_numpy()
    regions = proj["region"].fillna("").to_numpy()

    level = rng.normal(mu[None, :], sd[None, :], size=(n, k))       # true semis-day level
    panel = cal["s0"]                                               # show-to-show judging noise
    reg_score = np.empty_like(level)
    for j, reg in enumerate(regions):
        days = (semis_date - _d(cfg["regional_dates"].get(reg, cfg["state_semis_date"]))).days
        reg_score[:, j] = level[:, j] - gain_to(days, cal) + rng.normal(0, panel, n)

    # use real results once regionals / state have happened
    cur = scores[(scores["season"] == season) & (scores["class"] == cls)]
    actual_reg = cur[cur["round"] == "Regional"].set_index("school")["total"].to_dict()
    actual_semis = cur[cur["round"] == "State Semifinals"].set_index("school")["total"].to_dict()
    for j, school in enumerate(proj["school"]):
        if school in actual_reg:
            reg_score[:, j] = float(actual_reg[school])

    qualified = np.zeros((n, k), bool)
    if actual_semis:
        for j, school in enumerate(proj["school"]):
            qualified[:, j] = school in actual_semis
    elif cfg.get("qualifying_mode", "statewide") == "statewide":
        take = min(int(cfg.get("state_qualifiers", k)), k)
        order = np.argsort(-reg_score, axis=1)[:, :take]
        rows = np.repeat(np.arange(n)[:, None], take, axis=1)
        qualified[rows, order] = True
    else:
        for reg in set(regions):
            idx = np.where(regions == reg)[0]
            take = spots.get(reg, len(idx)) if reg else len(idx)
            order = np.argsort(-reg_score[:, idx], axis=1)[:, :take]
            rows = np.repeat(np.arange(n)[:, None], order.shape[1], axis=1)
            qualified[rows, idx[order]] = True

    semis = level + rng.normal(0, panel * 0.5, size=(n, k))
    for j, school in enumerate(proj["school"]):
        if school in actual_semis:
            semis[:, j] = float(actual_semis[school])
    semis_q = np.where(qualified, semis, -np.inf)
    rank = (-semis_q).argsort(axis=1).argsort(axis=1) + 1
    finals = qualified & (rank <= cfg["finals_spots"])
    first = qualified & (rank == 1)

    res = proj[["school", "region", "basis", "last_score", "last_event", "last_date",
                "proj_semis", "proj_sd"]].copy()
    res["p_state"] = qualified.mean(0)
    res["p_finals"] = finals.mean(0)
    res["p_first"] = first.mean(0)
    res["median_semis_rank"] = [float(np.median(rank[qualified[:, j], j])) if qualified[:, j].any() else np.nan
                                for j in range(k)]
    return res.sort_values("proj_semis", ascending=False).reset_index(drop=True)


def weekly_snapshot(scores: pd.DataFrame, cfg: dict, end: dt.date | None = None) -> pd.DataFrame:
    """Last known score for every band in the class as of each Saturday."""
    season, cls = cfg["season"], cfg["focus_class"]
    cur = scores[(scores["season"] == season) & (scores["class"] == cls)].copy()
    if cur.empty:
        return pd.DataFrame()
    cur["d"] = pd.to_datetime(cur["event_date"]).dt.date
    start = min(cur["d"])
    end = end or max(max(cur["d"]), dt.date.today())
    sat = start + dt.timedelta(days=(5 - start.weekday()) % 7)
    rows = []
    while sat <= end:
        seen = cur[cur["d"] <= sat + dt.timedelta(days=1)]          # include Sunday/late posts
        last = chrono(seen).groupby("school").tail(1)
        last = last.assign(week_ending=sat.isoformat()).sort_values("total", ascending=False)
        last["rank_last_known"] = range(1, len(last) + 1)
        rows.append(last[["week_ending", "school", "total", "event", "event_date", "rank_last_known"]])
        sat += dt.timedelta(days=7)
    return pd.concat(rows, ignore_index=True).rename(columns={"total": "last_known_score"})
