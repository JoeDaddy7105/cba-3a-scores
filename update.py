"""
update.py - one command to refresh everything.

    python update.py              # scrape new recaps, rebuild all outputs
    python update.py --offline    # skip the website, rebuild from what's on disk
    python update.py --history    # also scrape the 2024 and 2025 archive pages

Outputs (all in data/, ready for Power BI):
    scores.csv            every score, all seasons, all classes
    latest_scores.csv     each band's most recent score this season (focus class)
    weekly_snapshot.csv   last known score + rank for each band as of each Saturday
    projections.csv       projected semis score, P(state), P(finals) per band
    focus_history.csv     the focus school's scores by season, aligned by days before State
    model_info.json       fitted model numbers and run time
and index.html (the public dashboard page, served by GitHub Pages).
"""
from __future__ import annotations

import argparse
import datetime as dt
import json
from pathlib import Path

import pandas as pd

import cba_insights as I
import cba_model as M
import cba_scraper as S

ROOT = Path(__file__).resolve().parent
DATA = ROOT / "data"


def combined_scores() -> pd.DataFrame:
    frames = []
    manual = DATA / "manual_scores.csv"
    if manual.exists():
        m = pd.read_csv(manual)
        m["source"] = "manual"
        frames.append(m)
    if S.SCRAPED.exists():
        s = pd.read_csv(S.SCRAPED)
        s["source"] = "scraped"
        frames.append(s)
    df = pd.concat(frames, ignore_index=True)
    aliases = S.load_aliases()
    df["school"] = df["school"].map(lambda x: S.clean_name(str(x), aliases))
    df["event_date"] = pd.to_datetime(df["event_date"]).dt.date.astype(str)
    df["total"] = df["total"].astype(float)
    df["season"] = df["season"].astype(int)
    # scraped rows win over hand-entered ones for the same performance
    df["_pri"] = (df["source"] == "scraped").astype(int)
    df = (df.sort_values("_pri", ascending=False)
            .drop_duplicates(["event_date", "round", "school"])
            .drop(columns="_pri"))
    # fill class from the lookup table where the recap didn't say
    classes = pd.read_csv(S.CLASSES)
    cmap = dict(zip(classes["school"], classes["class"]))
    unk = df["class"].isna() | (df["class"] == "Unknown")
    df.loc[unk, "class"] = df.loc[unk, "school"].map(cmap).fillna("Unknown")
    df["class_place"] = (df.groupby(["event_date", "round", "class"])["total"]
                           .rank(ascending=False, method="min").astype(int))
    return df.sort_values(["season", "event_date", "round", "class", "class_place"]).reset_index(drop=True)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--offline", action="store_true")
    ap.add_argument("--history", action="store_true")
    ap.add_argument("--as-of", help="pretend today is YYYY-MM-DD (for testing)")
    a = ap.parse_args()
    cfg = json.loads((ROOT / "config.json").read_text())
    as_of = dt.date.fromisoformat(a.as_of) if a.as_of else dt.date.today()

    # remember what we had before this run, so we can say what's new
    old_scores = pd.read_csv(DATA / "scores.csv") if (DATA / "scores.csv").exists() else pd.DataFrame()
    old_keys = set(zip(old_scores.get("event_date", []), old_scores.get("round", []), old_scores.get("school", [])))
    old_proj = pd.read_csv(DATA / "projections.csv") if (DATA / "projections.csv").exists() else pd.DataFrame()

    if not a.offline:
        # past seasons are listed every run but only downloaded once (the manifest remembers them)
        seasons = [cfg["season"]] + list(cfg.get("history_seasons", [])) + \
                  ([cfg["season"] - 1, cfg["season"] - 2] if a.history else [])
        seasons = list(dict.fromkeys(seasons))
        for s in seasons:
            url = cfg["score_pages"].get(str(s))
            if url:
                S.scrape_season(s, url)

    scores = combined_scores()
    scores.to_csv(DATA / "scores.csv", index=False)
    classes = pd.read_csv(S.CLASSES)
    cls, season, focus = cfg["focus_class"], cfg["season"], cfg["focus_school"]
    classes = classes[classes["class"] != "NC"]

    cal = M.calibrate(scores, cls, season)
    proj = M.project(scores, classes, cfg, cal, as_of=as_of)
    sim = M.simulate(proj, scores, cfg, cal)
    # sensitivity: what if one fewer band qualifies for State
    tight = M.simulate(proj, scores, cfg, cal, qualifiers=int(cfg.get("state_qualifiers", len(proj))) - 1)
    sim["p_state_if_one_fewer_spot"] = sim["school"].map(tight.set_index("school")["p_state"])
    # panel-adjusted model (cba_insights): better at predicting State in 2024-2025 backtests,
    # so it supplies the finals odds; the original model still supplies P(state) before regionals
    ins = I.run_insights(scores, cfg, as_of=as_of.isoformat())
    if ins:
        fin = ins["finals"].set_index("school")
        sim["in_state_field"] = sim["school"].isin(ins["field"])
        sim["strength"] = sim["school"].map(ins["ratings"].set_index("school")["strength"])
        sim["strength_sd"] = sim["school"].map(ins["ratings"].set_index("school")["strength_sd"])
        sim["p_finals_original"] = sim["p_finals"]
        sim["p_finals"] = sim["school"].map(fin["p_finals"]).fillna(0.0)
        sim["p_first"] = sim["school"].map(fin["p_first"]).fillna(0.0)
        if "regional" in ins["field_basis"] or "semifinals" in ins["field_basis"]:
            sim["p_state"] = sim["in_state_field"].astype(float)
        sim = sim.sort_values(["p_finals", "strength"], ascending=False).reset_index(drop=True)
        write_insights(ins, cfg)
    bt = I.run_backtests(scores, cfg)
    if len(bt):
        bt.to_csv(DATA / "backtest.csv", index=False)
    sim.to_csv(DATA / "projections.csv", index=False)

    cur = scores[(scores["season"] == season) & (scores["class"] == cls)]
    latest = M.chrono(cur).groupby("school").tail(1).sort_values("total", ascending=False)
    latest.to_csv(DATA / "latest_scores.csv", index=False)
    snap = M.weekly_snapshot(scores, cfg, end=as_of)
    snap.to_csv(DATA / "weekly_snapshot.csv", index=False)

    # focus school by season, lined up by days before that season's State semis
    fh = scores[scores["school"] == focus].copy()
    semis_dates = scores[scores["round"] == "State Semifinals"].groupby("season")["event_date"].min().to_dict()
    semis_dates[season] = cfg["state_semis_date"]
    fh["days_before_state"] = fh.apply(
        lambda r: (pd.to_datetime(semis_dates.get(r["season"], r["event_date"])) - pd.to_datetime(r["event_date"])).days, axis=1)
    fh.to_csv(DATA / "focus_history.csv", index=False)

    info = {"run_at": dt.datetime.now().isoformat(timespec="seconds"), "as_of": as_of.isoformat(),
            "calibration": cal, "season_shift": proj.attrs.get("season_shift"),
            "yoy_sd": proj.attrs.get("yoy_sd"), "config": cfg}
    (DATA / "model_info.json").write_text(json.dumps(info, indent=2, default=str))

    info["insights"] = insights_payload(ins, bt, cfg) if ins else None
    render_dashboard(scores, sim, snap, fh, info)
    write_update_summary(scores, old_keys, sim, old_proj, latest, cfg)
    f = sim[sim["school"] == focus]
    if len(f):
        r = f.iloc[0]
        print(f"\n{focus}: neutral-panel rating {r.get('strength', r.proj_semis):.1f} | "
              f"P(state) {r.p_state:.0%} | P(finals) {r.p_finals:.0%}")
    print("Outputs written to", DATA)


def write_insights(ins, cfg):
    ins["ratings"].to_csv(DATA / "ratings.csv", index=False)
    ins["finals"].drop(columns="rank_dist").to_csv(DATA / "finals_odds.csv", index=False)
    ins["panels"].to_csv(DATA / "panel_effects.csv", index=False)
    ins["class_obs"].to_csv(DATA / "adjusted_scores.csv", index=False)
    if len(ins["captions"]):
        ins["captions"].to_csv(DATA / "caption_ratings.csv", index=False)
    if len(ins["h2h"]):
        ins["h2h"].to_csv(DATA / "head_to_head.csv", index=False)
    ins["pairwise"].to_csv(DATA / "pairwise.csv", index=False)
    ins["boost"].to_csv(DATA / "finals_vs_boost.csv", index=False)


def insights_payload(ins, bt, cfg):
    """Compact, JSON-safe version of the insights for the dashboard."""
    def recs(df, cols=None):
        if df is None or len(df) == 0:
            return []
        d = df[cols] if cols else df
        return json.loads(d.to_json(orient="records"))
    fin = ins["finals"].copy()
    fin["rank_dist"] = fin["rank_dist"].map(lambda a: [round(float(x), 4) for x in a])
    summary = {}
    if len(bt):
        for model, g in bt.groupby("model"):
            summary[model] = {k: (round(v, 3) if isinstance(v, float) else v) for k, v in I.score_backtest(g).items()}
    bt3 = bt[bt["class"] == cfg["focus_class"]] if len(bt) else bt
    return {
        "field": ins["field"], "field_basis": ins["field_basis"], "mu": ins["mu"], "chol": ins["chol"],
        "perf_sd": ins["perf_sd"], "sigma": ins["sigma"], "params": ins["params"],
        "ratings": recs(ins["ratings"], ["school", "strength", "strength_sd", "growth_per_week", "n_scores", "n_shows"]),
        "finals": recs(fin, ["school", "strength", "strength_sd", "p_finals", "p_first", "p_top3", "median_rank", "rank_dist"]),
        "pairwise": recs(ins["pairwise"]), "boost": recs(ins["boost"]),
        "captions": recs(ins["captions"]),
        "panels": recs(ins["panels"], ["event", "event_date", "round", "effect", "effect_sd", "n_bands", "n_class_bands"]),
        "adjusted": recs(ins["class_obs"]),
        "h2h": recs(ins["h2h"]),
        "backtest_summary": summary,
        "backtest_class": recs(bt3, ["season", "school", "pred", "actual", "pred_rank", "actual_rank", "model"]) if len(bt3) else [],
    }


def write_update_summary(scores, old_keys, sim, old_proj, latest, cfg):
    """data/last_update.json: what this run added. notify.py turns it into an alert."""
    season, cls, focus = cfg["season"], cfg["focus_class"], cfg["focus_school"]
    keys = list(zip(scores["event_date"], scores["round"], scores["school"]))
    new = scores[[k not in old_keys for k in keys]] if old_keys else scores.iloc[0:0]
    new = new[new["season"] == season]
    new_cls = M.chrono(new[new["class"] == cls]).sort_values("total", ascending=False)

    def odds(df):
        r = df[df["school"] == focus] if len(df) else df
        return (float(r["p_state"].iloc[0]), float(r["p_finals"].iloc[0])) if len(r) else (None, None)
    p_state, p_finals = odds(sim)
    prev_state, prev_finals = odds(old_proj)
    fl = latest[latest["school"] == focus]
    rank = int(latest.reset_index(drop=True).index[latest.reset_index(drop=True)["school"] == focus][0]) + 1 if len(fl) else None
    summary = {
        "run_at": dt.datetime.now().isoformat(timespec="seconds"),
        "new_rows": int(len(new)),
        "new_events": sorted({f"{e} {r}" for e, r in zip(new["event"], new["round"])}),
        "new_class_rows": [{"school": r.school, "total": float(r.total), "event": r.event, "round": r["round"]}
                           for _, r in new_cls.iterrows()],
        "focus": {
            "school": focus,
            "latest": float(fl["total"].iloc[0]) if len(fl) else None,
            "latest_event": fl["event"].iloc[0] if len(fl) else None,
            "rank": rank, "of": int(len(latest)),
            "p_state": p_state, "p_finals": p_finals,
            "prev_p_state": prev_state, "prev_p_finals": prev_finals,
        },
    }
    (DATA / "last_update.json").write_text(json.dumps(summary, indent=2, default=str))


def render_dashboard(scores, sim, snap, fh, info):
    tpl = ROOT / "dashboard_template.html"
    if not tpl.exists():
        return
    cfg = info["config"]
    payload = {
        "info": info,
        "scores": scores[scores["class"] == cfg["focus_class"]][
            ["season", "event", "event_date", "round", "school", "total", "class_place", "source"]
            + [c for c in ("mp", "vp", "ge") if c in scores.columns]].to_dict("records"),
        "projections": sim.fillna("").to_dict("records"),
        "snapshot": snap.to_dict("records"),
        "focus_history": fh[["season", "event", "event_date", "round", "total", "days_before_state"]].to_dict("records"),
    }
    html = tpl.read_text(encoding="utf-8").replace("/*__DATA__*/null", json.dumps(payload, default=str))
    page = ('<!doctype html><html lang="en"><head><meta charset="utf-8">'
            '<meta name="viewport" content="width=device-width, initial-scale=1, viewport-fit=cover">'
            '</head><body style="margin:0">' + html + '</body></html>')
    (ROOT / "dashboard.html").write_text(html, encoding="utf-8")   # fragment (Claude artifact)
    (ROOT / "index.html").write_text(page, encoding="utf-8")       # full page (GitHub Pages)


if __name__ == "__main__":
    main()
