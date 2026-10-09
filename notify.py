"""
notify.py - send an alert when a run finds new scores.

The workflow runs this after each update. It reads data/last_update.json and only sends when the
run found new scores this season (use --test to send one anyway). Each channel below switches on
when its settings are present, so you can use any mix of them:

  GitHub issue  Always on inside GitHub Actions. Comments on the open "Score alerts" issue
                (label: score-alerts), creating it the first time. GitHub emails everyone watching
                the repo or subscribed to that issue, and the GitHub mobile app pushes it.
  Discord       Repo secret DISCORD_WEBHOOK_URL (channel > Edit > Integrations > Webhooks).
  Telegram      Repo secrets TELEGRAM_BOT_TOKEN and TELEGRAM_CHAT_ID.
  ntfy          Repo secret NTFY_TOPIC (optional, push via the ntfy app).
"""
from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path

import requests

ROOT = Path(__file__).resolve().parent
ALERT_LABEL = "score-alerts"


def pct(p):
    if p is None:
        return "?"
    return ">99%" if p >= 0.995 else "<1%" if 0 < p < 0.005 else f"{round(p * 100)}%"


def short(name: str) -> str:
    return name.removesuffix(" HS")


def build_message(u: dict, cfg: dict) -> tuple[str, list[str]]:
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
        lines.append(f"Chance of State {move(f['p_state'], f['prev_p_state'])}, "
                     f"finals {move(f['p_finals'], f['prev_p_finals'])}")
    others, seen = [], set()
    for r in u["new_class_rows"]:
        if r["school"] == f["school"] or r["school"] in seen:
            continue
        seen.add(r["school"])
        others.append(f"{short(r['school'])} {r['total']:.2f}")
    if others:
        lines.append(f"Other {cfg['focus_class']}: " + ", ".join(others[:8]))
    return title, lines or ["The dashboard has new data."]


# ---------------------------------------------------------------- channels
def send_github(title, lines, url):
    token, repo = os.environ.get("GITHUB_TOKEN"), os.environ.get("GITHUB_REPOSITORY")
    if not (token and repo):
        return None
    api = f"https://api.github.com/repos/{repo}"
    h = {"Authorization": f"Bearer {token}", "Accept": "application/vnd.github+json"}
    r = requests.get(f"{api}/issues", headers=h, params={"labels": ALERT_LABEL, "state": "open"}, timeout=30)
    r.raise_for_status()
    body = f"**{title}**\n\n" + "\n".join(f"- {l}" for l in lines) + (f"\n\n[Open the dashboard]({url})" if url else "")
    if r.json():
        n = r.json()[0]["number"]
        requests.post(f"{api}/issues/{n}/comments", headers=h, json={"body": body}, timeout=30).raise_for_status()
        return f"comment on issue #{n}"
    intro = ("New scores get posted here as comments. Click **Subscribe** on the right "
             "(or watch the repo) to get an email each time.\n\n" + body)
    r = requests.post(f"{api}/issues", headers=h, timeout=30,
                      json={"title": "Score alerts", "body": intro, "labels": [ALERT_LABEL]})
    r.raise_for_status()
    return f"new issue #{r.json()['number']}"


def send_discord(title, lines, url):
    hook = os.environ.get("DISCORD_WEBHOOK_URL")
    if not hook:
        return None
    text = f"**{title}**\n" + "\n".join(lines) + (f"\n{url}" if url else "")
    requests.post(hook, json={"content": text[:1900]}, timeout=30).raise_for_status()
    return "discord"


def send_telegram(title, lines, url):
    tok, chat = os.environ.get("TELEGRAM_BOT_TOKEN"), os.environ.get("TELEGRAM_CHAT_ID")
    if not (tok and chat):
        return None
    text = title + "\n" + "\n".join(lines) + (f"\n{url}" if url else "")
    requests.post(f"https://api.telegram.org/bot{tok}/sendMessage",
                  json={"chat_id": chat, "text": text}, timeout=30).raise_for_status()
    return "telegram"


def send_ntfy(title, lines, url):
    topic = os.environ.get("NTFY_TOPIC")
    if not topic:
        return None
    requests.post(f"https://ntfy.sh/{topic}", data="\n".join(lines).encode("utf-8"), timeout=30,
                  headers={"Title": title.encode("utf-8"), "Click": url or ""}).raise_for_status()
    return "ntfy"


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--test", action="store_true", help="send even if nothing is new")
    ap.add_argument("--dry-run", action="store_true", help="print the message, don't send")
    a = ap.parse_args()
    cfg = json.loads((ROOT / "config.json").read_text())
    url = cfg.get("notify", {}).get("dashboard_url", "")
    path = ROOT / "data" / "last_update.json"
    if not path.exists():
        print("No update summary yet; nothing to send.")
        return
    u = json.loads(path.read_text())
    if u.get("new_rows", 0) == 0 and not a.test:
        print("No new scores this run; no alert.")
        return
    title, lines = build_message(u, cfg)
    if a.test:
        title = "Test alert. Latest data: " + title.removeprefix("New scores: ")
    print(title, *lines, sep="\n")
    if a.dry_run:
        return
    failed = False
    for send in (send_github, send_discord, send_telegram, send_ntfy):
        try:
            where = send(title, lines, url)
            if where:
                print("Sent:", where)
        except Exception as e:  # noqa: BLE001  keep going so one bad channel doesn't block the others
            failed = True
            print(f"{send.__name__} failed: {e}", file=sys.stderr)
    if failed:
        sys.exit(1)


if __name__ == "__main__":
    main()
