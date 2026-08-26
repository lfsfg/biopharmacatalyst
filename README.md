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

---

# Quarterly Biotech Catalyst Screener (v2)

A second, independent pipeline in this repo. Where the FDA scanner runs daily
and filters by float, this runs **once per quarter** and filters by **short
float**, hunting next-quarter binary catalysts in heavily-shorted US biotech
and drug-manufacturer names.

## Why there is a v2

v1's first live run (2026-08-26) exited 0, committed a candidates file, and
reported success — while being substantially broken:

| Symptom | Cause |
|---|---|
| Every ticker corrupted (`BBHVN` for `BHVN`) | `get_text()` concatenated a nested element in the ticker cell |
| **Short float empty on all 213 names** | Looked up one header name (`Float Short`); Finviz used another |
| One of three industries returned 0 rows | Ambiguous — bad token or genuinely no matches, indistinguishable |
| EDGAR: 0 hits across 1,278 requests, no errors | Silently empty responses |
| pdufa.bio: 0 rows parsed | Assumed one exact table layout |

Two of four catalyst sources contributed nothing, and the filter that
*defines* the screen produced no data — yet the job went green, because the
only guard asked "is the universe empty?"

**The lesson v2 is built around: a screener must assert what each source is
supposed to deliver, not merely that something came back.**

## Architecture

| Stage | Runs in | Does |
|---|---|---|
| **A — collection** | GitHub Actions (`quarterly_catalyst_screen.yml`) | Screen + catalyst check → ranked CSV + run manifest |
| **B — research** | Claude Routine (quarterly) | Reads the manifest and CSV, researches the top 20, files the report |

```
catalyst_screener.py            # orchestration + health gates
catalyst/
├── periods.py                  # date-interval semantics
├── health.py                   # per-source health contract
├── catalysts.py                # merging + confidence tiers
├── fundamentals.py             # cash runway / dilution risk
├── diffing.py                  # quarter-over-quarter comparison
├── report.py                   # CSV, summary, manifest
└── sources/
    ├── finviz.py               # universe + short interest
    ├── shortinterest.py        # yfinance fallback + cross-check
    ├── edgar.py                # SEC full-text search + date extraction
    ├── ctgov.py                # ClinicalTrials.gov API v2
    └── pdufa.py                # pdufa.bio calendar
tests/                          # 57 tests, offline
```

## What changed, and why

### 1. Per-source health contract (`catalyst/health.py`)

Every source declares what it must deliver. `assess_coverage()` grades a field
by how much of the universe it actually covered — so short float missing on
100% of rows is a `FAILED`, not a shrug. Required failures exit non-zero and
suppress the CSV entirely.

### 2. Probes that distinguish "broken" from "empty"

A source returning nothing is ambiguous, and that ambiguity is what hid v1's
bugs. Each fragile source now answers a question with a **guaranteed
non-empty** answer:

- `edgar.probe()` — "PDUFA" across all 8-Ks in 150 days. Zero hits proves the
  integration is broken, not that nobody mentioned PDUFA.
- `finviz.probe_industry_token()` — re-runs the industry without the
  short-float filter. Rows there mean the token is valid and the empty result
  is real; no rows mean the token is wrong.
- `ctgov.probe()` — a query that must return studies.

### 3. Confidence tiers, and ranking that respects them

| Tier | Meaning | Source |
|---|---|---|
| **HARD** | FDA statutory deadline | pdufa.bio, PDUFA/target action date in a filing |
| **FIRM** | Company-guided timing | "topline expected in Q4" in an 8-K/10-Q |
| **SOFT** | Sponsor-entered estimate | CT.gov estimated primary completion |

v1 ranked purely by proximity, which systematically promotes a soft CT.gov
estimate above a hard FDA deadline that happens to fall later. Default is now
`tier-then-date`; `--rank-by date` restores v1 behaviour.

### 4. EDGAR hits must carry a date

v1 counted a keyword hit as a catalyst. "This company said PDUFA recently" is
not "this company has a catalyst next quarter" — those rows survived with an
**empty date**, quietly violating the requirement they existed to enforce.
v2 fetches the matched documents, extracts a date expression near the keyword,
and keeps the hit only when it overlaps the target quarter. Bare `BLA`/`NDA`
searches were dropped — pure boilerplate noise that never yielded a date.

### 5. Date ranges instead of string comparison

CT.gov returns partial dates (`2026-10`); companies guide to "Q4 2026",
"second half of 2026", "mid-2026". Everything resolves to a closed interval
with a precision label, and "is it in the target quarter?" is an overlap test.

### 6. Two independent short-interest sources

yfinance backs up Finviz, so a column rename degrades to a cross-check
failure rather than an empty screen. When both are present and disagree by
more than 5pp, the row is flagged rather than silently taking one.

### 7. Ticker validation instead of string repair

Corrupted symbols are caught by validating against the SEC ticker→CIK map,
which is authoritative. Deliberately **not** done: "repairing" `BBHVN` by
de-duplicating characters, which would turn `AAPL` into `APL`.

### 8. Cash runway / dilution risk (new)

From SEC XBRL company facts. Material to this strategy: a biotech with two
quarters of cash will raise into a positive readout, capping the upside the
whole thesis rests on — and a company that can print shares is a far weaker
squeeze candidate.

### 9. Quarter-over-quarter diffing (new)

Outputs are versioned in git but v1 never read the previous run. v2 reports
carried-over names, **slipped catalyst dates**, and short-interest build/unwind.

### 10. Run manifest (new)

`catalyst_manifest_<date>.json` gives Stage B machine-readable run status and
per-source health, instead of inferring success from a filename.

### 11. SEC_USER_AGENT is required

v1 fell back to a fabricated contact address — an SEC fair-access violation
that risks banning the runner. Missing config is now a hard error.

### 12. Tests, and CI that runs them

57 offline tests; the ones reproducing the bugs above are marked
`REGRESSION`. `tests.yml` runs them on every push, and the quarterly workflow
gates on them before touching the network.

## Schedule

`cron: '30 13 17 3,6,9,12 *'` — 13:30 UTC on the 17th of Mar/Jun/Sep/Dec.

| Run date | Screens for |
|---|---|
| 17 Mar | Q2 (Apr 1 – Jun 30) |
| 17 Jun | Q3 (Jul 1 – Sep 30) |
| 17 Sep | Q4 (Oct 1 – Dec 31) |
| 17 Dec | Q1 of the following year |

> Scheduled workflows only run from the default branch.

## Screen definition

Finviz `sec_healthcare, geo_usa, sh_opt_optionshort, sh_short_o10` plus one
industry token per pass (Biotechnology, Drug Manufacturers—General, Drug
Manufacturers—Specialty & Generic), unioned. Overview (`v=111`) and Ownership
(`v=131`) views are joined on ticker. Short Ratio is days-to-cover.

## Running it

```bash
export SEC_USER_AGENT="Jane Doe jane@company.com"
export GITHUB_REPO_PATH=$(pwd)

python catalyst_screener.py --dry-run     # probe sources, report health, stop
python catalyst_screener.py               # full run
python catalyst_screener.py --rank-by date

python -m pytest tests/ -q
```

| Env var | Effect |
|---|---|
| `SEC_USER_AGENT` | **Required.** Real contact string. |
| `RANK_BY` | `tier-then-date` (default) or `date` |
| `SKIP_SHORT_FALLBACK` | `true` to skip the yfinance cross-check |
| `SKIP_FUNDAMENTALS` | `true` to skip cash-runway lookups |
| `ALLOW_DEGRADED` | `true` to exit 0 despite a required-source failure |

## Output

```
catalyst_screen/
├── catalyst_candidates_YYYY-MM-DD.csv     # ranked candidates
├── catalyst_candidates_YYYY-MM-DD_log.txt # human summary + health table
├── catalyst_manifest_YYYY-MM-DD.json      # machine-readable status
└── catalyst_run_YYYY-MM-DD.debug.log      # full run log
```

## Repository secret

| Secret | Purpose |
|---|---|
| `SEC_USER_AGENT` | Required. SEC blocks clients that do not identify themselves. |

## Dry run

`--dry-run` probes all four sources and reports health without running the
screen or writing candidates. Use it to check the pipeline before trusting a
scheduled run, and after any upstream site change.

| Source | What the probe proves |
|---|---|
| `finviz` | Industry tokens are valid, the results table parses, tickers are clean symbols, and the short-float column exists under one of its aliases |
| `edgar:fts` | "PDUFA" across all 8-Ks in 150 days returns hits — zero means the integration is broken, not that nobody mentioned it |
| `clinicaltrials.gov` | A query that must return studies does |
| `pdufa.bio` | The calendar fetches and parses to some rows |

It writes `catalyst_probe_<date>.txt` and a manifest with status
`DRY_RUN_OK` / `DRY_RUN_FAILED`, and exits non-zero if a required source
failed — so a red job means a genuinely broken source.

## Known limitation

The four upstream sources are unreachable from the environment this was
authored in, so no live request was made against any of them. Everything is
verified offline against fixtures — including fixtures reproducing each
observed bug.
