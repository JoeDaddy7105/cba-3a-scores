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

    if not a.offline:
        seasons = [cfg["season"]] + ([cfg["season"] - 1, cfg["season"] - 2] if a.history else [])
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

    render_dashboard(scores, sim, snap, fh, info)
    f = sim[sim["school"] == focus]
    if len(f):
        r = f.iloc[0]
        print(f"\n{focus}: projected semis {r.proj_semis:.1f} ± {r.proj_sd:.1f} | "
              f"P(state) {r.p_state:.0%} | P(finals) {r.p_finals:.0%}")
    print("Outputs written to", DATA)


def render_dashboard(scores, sim, snap, fh, info):
    tpl = ROOT / "dashboard_template.html"
    if not tpl.exists():
        return
    cfg = info["config"]
    payload = {
        "info": info,
        "scores": scores[scores["class"] == cfg["focus_class"]][
            ["season", "event", "event_date", "round", "school", "total", "class_place", "source"]].to_dict("records"),
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
