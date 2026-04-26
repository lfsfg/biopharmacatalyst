# BioPharma Catalyst — FDA Low-Float Scanner

Scrapes the [BioPharma Catalyst FDA calendar](https://www.biopharmacatalyst.com/calendars/fda-calendar),
cross-references each ticker against float data (via yfinance), and outputs a
filtered CSV of upcoming FDA events on stocks with **< 50 M shares float**.

Runs automatically every day at **15:50 ET** via GitHub Actions.

---

## File structure

```
├── fda_scanner.py                   # Main script
├── requirements.txt
├── .github/
│   └── workflows/
│       └── fda_daily_scan.yml       # Scheduled GitHub Actions workflow
└── fda_calendar/                    # Output directory (created by the script)
    ├── fda_lowfloat_YYYY-MM-DD.csv
    └── fda_lowfloat_YYYY-MM-DD_log.txt
```

---

## GitHub Secrets you must create

Go to **Settings → Secrets and variables → Actions → New repository secret**
in your GitHub repository and add the following:

| Secret name | What to put in it |
|---|---|
| *(none required)* | The workflow uses the built-in `GITHUB_TOKEN` — no PAT needed for pushing to the same repo. |

> If you fork the repo or push to a *different* repo, add:
>
> | Secret name | Value |
> |---|---|
> | `PAT_TOKEN` | A GitHub Personal Access Token with `repo` scope |
>
> Then replace `token: ${{ secrets.GITHUB_TOKEN }}` with
> `token: ${{ secrets.PAT_TOKEN }}` in `fda_daily_scan.yml`.

---

## Getting started from scratch

### 1. Clone the repo

```bash
git clone https://github.com/YOUR_USERNAME/biopharmacatalyst.git
cd biopharmacatalyst
```

### 2. Push all files to GitHub

```bash
git add .
git commit -m "Initial commit — FDA scanner setup"
git push -u origin main
```

### 3. Enable GitHub Actions

Actions are automatically enabled for public repositories.  
For **private** repos: go to **Settings → Actions → General** and select
*Allow all actions and reusable workflows*.

### 4. (Optional) Test locally

```bash
# Install Python 3.11
python3.11 -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt

# Point the script at this repo directory
export GITHUB_REPO_PATH=$(pwd)
export SKIP_GIT_PUSH=true          # don't push during local tests

python fda_scanner.py
```

Output files will appear in `./fda_calendar/`.

### 5. Trigger a manual run on GitHub Actions

1. Go to **Actions → FDA Low-Float Daily Scan**
2. Click **Run workflow → Run workflow**
3. After ~2-3 minutes the run completes and a new commit appears in the repo

---

## Scheduled run times

The workflow uses **two cron entries** to fire at 15:50 ET regardless of
daylight-saving time:

| Cron | UTC | ET equivalent |
|---|---|---|
| `50 19 * * *` | 19:50 UTC | 15:50 EDT (Mar – Nov) |
| `50 20 * * *` | 20:50 UTC | 15:50 EST (Nov – Mar) |

A `concurrency` lock ensures only one run executes even when both crons fire
during the same minute at DST transitions.

---

## How the scraper works

The script uses a three-stage fallback chain:

1. **Direct API probe** — tries a set of likely REST endpoint URLs with JSON
   headers, looking for a machine-readable response.
2. **HTML + embedded-JSON parse** — fetches the full page via `requests`,
   then scans `<script>` tags for Nuxt/Next SSR payloads
   (`window.__NUXT__`, `window.__NEXT_DATA__`, etc.) and falls back to
   HTML table parsing.
3. **Selenium headless Chrome** — renders the page in a real browser, injects
   an XHR/fetch interceptor to capture the API call the page's JavaScript
   makes, and falls back to parsing the rendered HTML.

---

## Output CSV columns

| Column | Description |
|---|---|
| `ticker` | Stock ticker symbol |
| `company_name` | Company name |
| `event_type` | PDUFA / AdCom / FDA Decision / Clinical Data / etc. |
| `event_date` | Date of the FDA event (YYYY-MM-DD) |
| `days_until_event` | Days from today to the event |
| `drug_indication` | Drug name and/or indication (if available) |
| `float_shares` | Float shares (integer) or `N/A` |
| `float_millions` | Float in millions (e.g. `12.3`) or `N/A` |
| `needs_manual_review` | `True` when float could not be fetched — may be micro-cap |

Rows are sorted by `event_date` ascending, then `float_shares` ascending
(smallest float first within the same date). `N/A` floats sort last.

---

## Verifying the first scheduled run

1. After the scheduled time passes, go to
   **Actions → FDA Low-Float Daily Scan → latest run**.
2. Check the *Run FDA scanner* step for any errors.
3. Confirm a commit like `FDA low-float scan 2026-04-26 15:50 ET [skip ci]`
   appears in the repo.
4. Open `fda_calendar/fda_lowfloat_YYYY-MM-DD.csv` to see the results.
5. Open `fda_calendar/fda_lowfloat_YYYY-MM-DD_log.txt` for the summary log.

---

## Troubleshooting

| Symptom | Fix |
|---|---|
| CSV is empty but log says events were scraped | All events were filtered out (none have float < 50 M or date in window) |
| Log says "No events extracted" | BioPharma Catalyst changed their HTML/API — inspect the Selenium screenshot or page source and update `_parse_events_from_html` |
| `yfinance` errors for many tickers | Temporary API rate-limiting; the script marks those `N/A` and continues |
| `git push` fails in Actions | Check the repo has **contents: write** permission; confirm the workflow YAML has `permissions: contents: write` |
| Workflow runs twice per day | Expected during DST; the concurrency lock prevents double-processing |

---

## Dependencies

| Package | Purpose |
|---|---|
| `requests` | HTTP fetching |
| `beautifulsoup4` + `lxml` | HTML parsing |
| `selenium` | JS-rendered page fallback |
| `yfinance` | Float / shares data |
| `pandas` | CSV output |
| `pytz` | ET timezone handling |
| `gitpython` | (Available for local git ops if needed) |
