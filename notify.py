"""
notify.py - send a phone alert through ntfy (https://ntfy.sh) when new scores come in.

The workflow runs this after each update. It reads data/last_update.json and only sends
an alert when the run found new scores this season. Use --test to send one anyway.

To get alerts: install the free ntfy app (iPhone or Android), tap +, and subscribe to the
topic in config.json -> notify.ntfy_topic. Anyone who knows the topic can subscribe, so it
doubles as a public alert channel. Set a repo secret named NTFY_TOPIC to override it.
"""
from __future__ import annotations

import argparse
import json
import os
from pathlib import Path

import requests

ROOT = Path(__file__).resolve().parent


def pct(p):
    if p is None:
        return "?"
    return ">99%" if p >= 0.995 else "<1%" if 0 < p < 0.005 else f"{round(p * 100)}%"


def short(name: str) -> str:
    return name.removesuffix(" HS")


def build_message(u: dict, cfg: dict) -> tuple[str, str]:
    f = u["focus"]
    events = sorted({e.rsplit(" ", 1)[0] for e in u["new_events"]})
    title = "New scores: " + (", ".join(events) if events else "dashboard updated")
    lines = []
    if f.get("latest") is not None:
        lines.append(f"{short(f['school'])}: {f['latest']:.3f} at {f['latest_event']} "
                     f"(#{f['rank']} of {f['of']} in {cfg['focus_class']})")
    if f.get("p_state") is not None:
        def move(now, before):
            if before is None or round(now * 100) == round(before * 100):
                return pct(now)
            return f"{pct(now)} (was {pct(before)})"
        lines.append(f"State {move(f['p_state'], f['prev_p_state'])} · Finals {move(f['p_finals'], f['prev_p_finals'])}")
    others = [r for r in u["new_class_rows"] if r["school"] != f["school"]]
    if others:
        seen, parts = set(), []
        for r in others:
            if r["school"] in seen:
                continue
            seen.add(r["school"])
            parts.append(f"{short(r['school'])} {r['total']:.2f}")
        lines.append(f"Other {cfg['focus_class']}: " + ", ".join(parts[:8]))
    return title, "\n".join(lines) or "The dashboard has new data."


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--test", action="store_true", help="send even if nothing is new")
    ap.add_argument("--dry-run", action="store_true", help="print the message, don't send")
    a = ap.parse_args()
    cfg = json.loads((ROOT / "config.json").read_text())
    n = cfg.get("notify", {})
    topic = os.environ.get("NTFY_TOPIC") or n.get("ntfy_topic")
    path = ROOT / "data" / "last_update.json"
    if not topic or not path.exists():
        print("No topic or no update summary; nothing to send.")
        return
    u = json.loads(path.read_text())
    if u.get("new_rows", 0) == 0 and not a.test:
        print("No new scores this run; no alert.")
        return
    title, body = build_message(u, cfg)
    if a.test:
        title = "Test alert: " + title
    print(title, "\n" + body)
    if a.dry_run:
        return
    r = requests.post(f"{n.get('ntfy_server', 'https://ntfy.sh').rstrip('/')}/{topic}",
                      data=body.encode("utf-8"),
                      headers={"Title": title.encode("utf-8"), "Tags": "musical_note",
                               "Click": n.get("dashboard_url", ""), "Priority": "default"},
                      timeout=30)
    r.raise_for_status()
    print("Sent to", topic)


if __name__ == "__main__":
    main()
