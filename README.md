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

---

# Quarterly Biotech Catalyst Screener

A second, independent pipeline in this repo. Where the FDA scanner runs daily
and filters by float, this one runs **once per quarter** and filters by
**short float**, hunting next-quarter binary catalysts in heavily-shorted
US biotech / drug-manufacturer names.

It is deliberately split into two stages, because the two halves need
different capabilities:

| Stage | Runs in | Does | Why there |
|---|---|---|---|
| **A — collection** | GitHub Actions (`quarterly_catalyst_screen.yml`) | Finviz screen + EDGAR / ClinicalTrials.gov / pdufa.bio catalyst check → `catalyst_screen/*.csv` | Deterministic, scriptable, and Actions runners have unrestricted egress |
| **B — research** | Claude Routine (quarterly trigger) | Reads Stage A's CSV, researches the top 20, writes the final dated document | Needs judgement + web research, not scraping |

## Schedule

`cron: '30 13 17 3,6,9,12 *'` — 13:30 UTC on the 17th of March, June,
September and December.

The 17th falls inside the second-to-last week of every quarter-ending month,
which leaves roughly two weeks of runway before the new quarter begins.

| Run date | Screens for |
|---|---|
| 17 Mar | Q2 (Apr 1 – Jun 30) |
| 17 Jun | Q3 (Jul 1 – Sep 30) |
| 17 Sep | Q4 (Oct 1 – Dec 31) |
| 17 Dec | Q1 of the following year |

> **Scheduled workflows only run from the repository default branch.** This
> workflow must be merged into the default branch before the cron will fire.
> Until then, trigger it with **Actions → Quarterly Biotech Catalyst Screen →
> Run workflow**.

## Screen definition (Stage A)

Finviz filters, applied as `sec_healthcare, geo_usa, sh_opt_optionshort,
sh_short_o10` plus one industry token per pass:

- Sector = Healthcare
- Industry = Biotechnology **or** Drug Manufacturers—General **or**
  Drug Manufacturers—Specialty & Generic
- Country = USA
- Optionable = Yes, Shortable = Yes
- Short Float > 10%

Finviz accepts only one industry token per request, so the script runs one
pass per industry and unions the results. For each industry it fetches both
the Overview view (`v=111`, for company name and market cap) and the
Ownership view (`v=131`, for Float Short and Short Ratio) and joins them on
ticker. **Short Ratio is days-to-cover.**

Tables are parsed by **column header name**, not by index, so a Finviz column
reorder degrades gracefully instead of silently mis-mapping data.

## Catalyst check (Stage A)

A ticker survives only with a hit from at least one of:

| Source | Query |
|---|---|
| SEC EDGAR full-text search | 8-K / 10-Q filed in the last 120 days from that CIK containing `topline`, `top-line`, `PDUFA`, `target action date`, `BLA` or `NDA` |
| ClinicalTrials.gov API v2 | company as lead sponsor, `AREA[Phase]PHASE3`, `AREA[PrimaryCompletionDate]RANGE[<qstart>,<qend>]` |
| pdufa.bio | a published PDUFA / readout date inside the target quarter |

Survivors are ranked by catalyst-date proximity. The CSV marks the top 20
`research_tier = FULL` and the remainder `FLAGGED_ONLY`.

## Fail-loud contract

If the Finviz stage returns **zero** tickers the script writes a failure log
and **exits 1** rather than committing an empty CSV, and the commit step is
skipped. The diagnostic log is still uploaded as a workflow artifact.

This is a direct response to a real failure in this repo: `fda_scanner.py`
has committed **122 consecutive empty CSVs** since 2026-04-26 while its
workflow reported success every single day. A screener that cannot
distinguish "no matches" from "scraper broken" is worse than no screener.

## Output

```
catalyst_screen/
├── catalyst_candidates_YYYY-MM-DD.csv       # ranked candidates
├── catalyst_candidates_YYYY-MM-DD_log.txt   # human-readable summary
└── catalyst_run_YYYY-MM-DD.debug.log        # full run log
```

CSV columns: `rank`, `research_tier`, `ticker`, `company_name`, `industry`,
`market_cap`, `short_float_pct`, `days_to_cover`, `catalyst_sources`,
`earliest_catalyst_date`, `catalyst_detail`, `edgar_hits`, `ctgov_hits`,
`pdufa_hits`.

## Optional repository secret

| Secret | Purpose |
|---|---|
| `SEC_USER_AGENT` | SEC requires a descriptive UA with contact info (e.g. `Jane Doe jane@example.com`). Without it EDGAR may rate-limit or reject requests. |
