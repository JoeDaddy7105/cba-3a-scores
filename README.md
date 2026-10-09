# CBA 3A Marching Score Tracker

Pulls the recap PDFs from https://www.coloradomarching.org/scores, turns them into clean CSVs,
projects each 3A band's State semifinals score, and estimates the odds of making State and finals.
Built around Standley Lake HS, but the focus school and class are settings in `config.json`.

## What's in here

| File | What it does |
|---|---|
| `update.py` | The one command to run. Scrapes new PDFs, rebuilds every output and `dashboard.html`. |
| `cba_scraper.py` | Finds recap PDF links, downloads only new ones, parses band scores. |
| `cba_model.py` | Fits the improvement curve from last season, projects scores, runs the State/finals simulation. |
| `config.json` | Season, focus school, State and regional dates, State spots per regional, finals spots. |
| `data/manual_scores.csv` | Hand-entered scores (2024 to 2026 3A, already filled in). Use it for any PDF that won't parse. |
| `data/school_classes.csv` | School to class and regional. Needed because some recaps (Colorado West prelims) don't list class. |
| `data/school_aliases.csv` | Fixes name variations ("Wheat Ridge" vs "Wheat Ridge HS"). |
| `powerbi/queries.pq`, `powerbi/measures.dax` | Power Query and DAX to paste into Power BI. |
| `.github/workflows/update-scores.yml` | Runs `update.py` on a schedule in GitHub and commits new data. |
| `notify.py` | Sends a phone alert (ntfy) after a run that found new scores. |
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

## Phone alerts

When a run finds new scores, `notify.py` sends a push alert through [ntfy](https://ntfy.sh), a free
notification service. The alert says which show posted, Standley Lake's latest score and rank, the
State and finals odds (and how they moved), and other new 3A scores. Tapping it opens the dashboard.

To subscribe: install the ntfy app (iPhone or Android), tap **+**, and enter the topic from
`config.json` (`notify.ntfy_topic`). You can also open `https://ntfy.sh/<topic>` in a browser.
Anyone with the topic name can subscribe, so you can share it with other parents.

To send a test alert, push any commit with `[notify-test]` in the message, or run
`python notify.py --test` locally. To change the topic privately, add a repo secret named `NTFY_TOPIC`.

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
