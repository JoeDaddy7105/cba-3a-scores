# CBA 3A Marching Score Tracker

Pulls the recap PDFs from https://www.coloradomarching.org/scores, turns them into clean CSVs,
projects each 3A band's State semifinals score, and estimates the odds of making State and finals.
Built around Standley Lake HS, but the focus school and class are settings in `config.json`.

## What's in here

| File | What it does |
|---|---|
| `update.py` | The one command to run. Scrapes new PDFs, rebuilds every output and `dashboard.html`. |
| `cba_scraper.py` | Finds recap PDF links, downloads only new ones, parses band scores. |
| `cba_insights.py` | Panel-adjusted ratings: takes each judging panel's generosity out, fits growth, runs the State/finals simulation, head-to-head, caption ratings and backtests. |
| `cba_model.py` | Fits the improvement curve from last season, projects scores, runs the State/finals simulation. |
| `config.json` | Season, focus school, State and regional dates, State spots per regional, finals spots. |
| `data/manual_scores.csv` | Hand-entered scores (2024 to 2026 3A, already filled in). Use it for any PDF that won't parse. |
| `data/school_classes.csv` | School to class and regional. Needed because some recaps (Colorado West prelims) don't list class. |
| `data/school_aliases.csv` | Fixes name variations ("Wheat Ridge" vs "Wheat Ridge HS"). |
| `powerbi/queries.pq`, `powerbi/measures.dax` | Power Query and DAX to paste into Power BI. |
| `.github/workflows/update-scores.yml` | Runs `update.py` on a schedule in GitHub and commits new data. |
| `notify.py` | Sends alerts (GitHub issue comment, optional Discord/Telegram) after a run that found new scores. |
| `run_update.bat` | Same thing for Windows Task Scheduler if you'd rather run it at home. |

## First run

```
pip install -r requirements.txt
python update.py --history     # current season plus the 2024 and 2025 archive pages
```

After that, `python update.py` only downloads PDFs it hasn't seen. It prints one line per recap.
Watch for two messages:

* `no_text` means the PDF is a scanned image (the 2025 Northern Regional is one). Type those
  scores into `data/manual_scores.csv`.
* `Schools with unknown class` means a band showed up on a recap that doesn't list class.
  Add it to `data/school_classes.csv`. For 3A bands also fill in the regional (Metro, Southern,
  Western, Northern) since that drives the State odds.

Scraped rows always win over manual rows for the same band, date and round, so the hand-entered
seed data gets replaced automatically as the scraper picks things up.

## Outputs (in `data/`)

* `scores.csv`: every score, all classes and seasons, with class placement.
* `latest_scores.csv`: each 3A band's most recent score this season.
* `weekly_snapshot.csv`: last known score and rank for every 3A band as of each Saturday.
  This is the "compare bands that went to different shows" view.
* `projections.csv`: projected semis score, range, P(state), P(finals), P(first) per band.
* `focus_history.csv`: Standley Lake scores by season, lined up by days before State.
* `model_info.json`: the fitted numbers, so you can see what the model is assuming.

## Automating it

**Option A, GitHub (fully hands-off, what I'd do).**
1. Make a repo (public is simplest; the scores are public anyway) and push this folder.
2. In the repo: Settings > Actions > General > Workflow permissions > "Read and write".
3. The workflow runs twice a day Sept through Nov. You can also hit "Run workflow" on the Actions tab
   right after a show.
4. In Power BI set the `BaseUrl` parameter to
   `https://raw.githubusercontent.com/<you>/<repo>/main/data/` and publish to the Service.
   Web sources with no login refresh in the Service without a gateway. Set scheduled refresh
   for a bit after the workflow times (for example 7 am and midnight).
5. GitHub Pages (Settings > Pages > Deploy from branch > `main` / root) serves `index.html`, the dashboard, at
   `https://<you>.github.io/<repo>/`. It refreshes itself every time the workflow commits new data.

**Option B, your PC.** Put the folder in OneDrive, schedule `run_update.bat` in Task Scheduler,
and point Power BI at the CSVs through the SharePoint/OneDrive connector (see the bottom of
`queries.pq`). A plain local folder path works in Desktop but needs a gateway for Service refresh.

## Panel-adjusted model (finals odds)

Every show has its own judges, and some panels score higher than others. Bands of all classes go to
several shows, which links the panels together, so `cba_insights.py` estimates how generous each panel
was and puts every band on one neutral scale:

    score = band strength on State day - growth x days left + panel generosity + noise

At State every 3A band faces the same panel, so that panel's own generosity cancels out of the ranking.
The finals odds come from drawing band strengths from the model (with their uncertainty and how they
are linked) plus a State-day performance wobble, thousands of times.

Settings (in `cba_insights.DEFAULTS`, override in `config.json` under `panel_model`) were picked by
backtesting on the 2024 and 2025 State semifinals, all classes, using only scores from before State.
Compared with the original method it cut the average miss on semifinal scores from about 2.1 to 1.7
points and put more band pairs in the right order. The ±1.2 point State-day wobble was set so past
predictions landed inside their ranges as often as they should. Results are in `data/backtest.csv`.

New outputs: `ratings.csv`, `finals_odds.csv`, `panel_effects.csv`, `adjusted_scores.csv`,
`caption_ratings.csv`, `head_to_head.csv`, `pairwise.csv`, `finals_vs_boost.csv`, `backtest.csv`.
Scraped rows now include caption subtotals (`mp`, `vp`, `ge` and their parts).

## Alerts when new scores post

After a run that finds new scores, `notify.py` sends a short alert: which show posted, Standley
Lake's latest score and rank, the State and finals odds (and how they moved), other new 3A scores,
and a link to the dashboard.

* **GitHub (on by default).** The workflow comments on the open **Score alerts** issue in this
  repo. GitHub emails you because you watch your own repo, and the GitHub mobile app pushes it.
  Anyone with a GitHub account can click **Subscribe** on that issue to get the same emails.
  Check Settings > Notifications on github.com if the emails don't show up.
* **Discord (optional).** In a Discord channel: Edit Channel > Integrations > Webhooks > New Webhook >
  Copy URL. In this repo: Settings > Secrets and variables > Actions > New repository secret,
  name `DISCORD_WEBHOOK_URL`, paste the URL. Good for a band-parent server.
* **Telegram (optional).** Make a bot with @BotFather, then add secrets `TELEGRAM_BOT_TOKEN` and
  `TELEGRAM_CHAT_ID`.

To send a test alert, push any commit with `[notify-test]` in the message.

## Suggested Power BI pages

1. **Field standings**: bar chart of `latest_scores` (3A), colored with the `Focus Color` measure,
   event and date in the tooltip.
2. **Week by week**: line chart, X = `WeeklySnapshot[week_ending]`, Y = `Last Known Score`,
   legend = school. A second visual with `Weekly Rank` on an inverted axis makes a bump chart.
3. **Season trend**: line chart from `FocusHistory`, X = `days_before_state` (reverse the axis),
   legend = season.
4. **Odds**: table or bar chart from `Projections`: `p_state`, `p_finals`, and an error bar
   using `proj_low` / `proj_high`.

## How the odds are worked out

* Bands improve all season. From last season's data, a band gained about 1.9 points a week
  until a few days before State, and a projection made from one score landed within about
  2.3 points of the real semifinals score two times out of three.
* Each band's 2026 scores are pushed forward to a projected semis score. More scores closer
  to State mean a tighter estimate.
* Bands with no 2026 score yet start from their 2025 semifinals score, shifted by how far
  returning bands are running ahead or behind last year, with a wider range.
* The season is replayed 20,000 times: top N per regional go to State (`state_spots_by_region`),
  top N at semis go to finals (`finals_spots`, 4 in both 2024 and 2025).
* Once regional and State results post, the real scores replace the simulated ones.

Things to keep in mind: judges differ from show to show, early scores are noisy, and CBA sets the
real State allotments each season (Procedures and Policies 5.03 and 5.06). The defaults in
`config.json` copy last season's pattern, where every Metro 3A band made State. Update those
numbers when CBA publishes them, and confirm Windsor and Wheat Ridge are still 3A.
