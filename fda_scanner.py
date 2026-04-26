#!/usr/bin/env python3
"""
FDA Low-Float Scanner
Daily scan of BioPharma Catalyst FDA calendar filtered to low-float stocks (<50M shares).
"""

import json
import logging
import os
import re
import subprocess
import sys
import time
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Optional

import pandas as pd
import pytz
import requests
import yfinance as yf
from bs4 import BeautifulSoup

try:
    from selenium import webdriver
    from selenium.webdriver.chrome.options import Options as ChromeOptions
    from selenium.webdriver.common.by import By
    from selenium.webdriver.support import expected_conditions as EC
    from selenium.webdriver.support.ui import WebDriverWait
    SELENIUM_AVAILABLE = True
except ImportError:
    SELENIUM_AVAILABLE = False

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

BASE_URL = "https://www.biopharmacatalyst.com"
FDA_CALENDAR_URL = f"{BASE_URL}/calendars/fda-calendar"

# Candidate API endpoints — tried in order before falling back to HTML parsing
_API_CANDIDATES = [
    f"{BASE_URL}/api/fda-calendar",
    f"{BASE_URL}/api/catalysts/fda",
    f"{BASE_URL}/api/calendars/fda-calendar",
    f"{BASE_URL}/api/v1/fda-calendar",
    f"{BASE_URL}/api/v2/fda-calendar",
    f"{BASE_URL}/api/calendars",
]

FLOAT_THRESHOLD = 50_000_000   # 50 million shares
LOOK_AHEAD_DAYS = 7
MIN_DAYS_AHEAD = 2
ET_TZ = pytz.timezone("America/New_York")

_HTML_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/120.0.0.0 Safari/537.36"
    ),
    "Accept": (
        "text/html,application/xhtml+xml,application/xml;"
        "q=0.9,image/webp,*/*;q=0.8"
    ),
    "Accept-Language": "en-US,en;q=0.9",
    "Accept-Encoding": "gzip, deflate, br",
    "Connection": "keep-alive",
    "Upgrade-Insecure-Requests": "1",
}

_JSON_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/120.0.0.0 Safari/537.36"
    ),
    "Accept": "application/json, text/plain, */*",
    "Accept-Language": "en-US,en;q=0.9",
    "Referer": "https://www.biopharmacatalyst.com/",
    "X-Requested-With": "XMLHttpRequest",
}

# ---------------------------------------------------------------------------
# Logging
# ---------------------------------------------------------------------------

def setup_logging(log_path: Path) -> logging.Logger:
    logger = logging.getLogger("fda_scanner")
    logger.setLevel(logging.DEBUG)
    fmt = logging.Formatter("%(asctime)s [%(levelname)s] %(message)s")

    fh = logging.FileHandler(log_path, encoding="utf-8")
    fh.setFormatter(fmt)
    logger.addHandler(fh)

    ch = logging.StreamHandler(sys.stdout)
    ch.setLevel(logging.INFO)
    ch.setFormatter(fmt)
    logger.addHandler(ch)

    return logger


# ---------------------------------------------------------------------------
# Parsing helpers
# ---------------------------------------------------------------------------

def _coerce_field(item: dict, *keys: str) -> str:
    for k in keys:
        v = item.get(k)
        if v is not None and str(v).strip():
            return str(v).strip()
    return ""


def _parse_events_from_json(data) -> list[dict]:
    """Normalise a JSON payload (list or nested dict) into a flat event list."""
    events: list[dict] = []

    if isinstance(data, list):
        items = data
    elif isinstance(data, dict):
        for key in ("data", "events", "calendar", "results", "items", "catalysts"):
            candidate = data.get(key)
            if isinstance(candidate, list):
                items = candidate
                break
        else:
            # Recurse one level for structures like {"fda": {"data": [...]}}
            for v in data.values():
                if isinstance(v, (list, dict)):
                    sub = _parse_events_from_json(v)
                    if sub:
                        return sub
            return events
    else:
        return events

    for item in items:
        if not isinstance(item, dict):
            continue
        ticker = _coerce_field(item, "ticker", "symbol", "stockSymbol", "stock")
        if not ticker:
            continue
        events.append({
            "ticker": ticker.upper().strip("$"),
            "company_name": _coerce_field(item, "company", "companyName", "company_name", "name"),
            "event_type": _coerce_field(
                item, "eventType", "event_type", "type", "catalyst",
                "catalystType", "category", "event",
            ),
            "event_date": _coerce_field(
                item, "date", "eventDate", "event_date", "catalystDate",
                "pdufa_date", "pdufaDate", "actionDate",
            ),
            "drug_indication": _coerce_field(
                item, "drug", "drugName", "drug_name", "indication",
                "drugIndication", "therapy", "brandName",
            ),
            "stage": _coerce_field(item, "stage", "drugStage", "drug_stage", "phase"),
        })

    return events


def _parse_events_from_html(html: str, logger: logging.Logger) -> list[dict]:
    """Extract events from raw HTML via embedded JSON, tables, or element attributes."""
    soup = BeautifulSoup(html, "lxml")

    # --- Strategy 1: embedded JSON in <script> tags ---
    script_patterns = [
        r'window\.__NUXT__\s*=\s*(\{.+?\})\s*(?:;|</script)',
        r'window\.__NEXT_DATA__\s*=\s*(\{.+?\})\s*(?:;|</script)',
        r'window\.__INITIAL_STATE__\s*=\s*(\{.+?\})\s*(?:;|</script)',
        r'window\.__DATA__\s*=\s*(\{.+?\})\s*(?:;|</script)',
        # Inline data arrays
        r'"fdaCalendar"\s*:\s*(\[.+?\])',
        r'"fda_calendar"\s*:\s*(\[.+?\])',
        r'"catalysts"\s*:\s*(\[.+?\])',
        r'"events"\s*:\s*(\[.+?\])',
        r'"calendar"\s*:\s*(\[.+?\])',
    ]

    for script_tag in soup.find_all("script"):
        text = script_tag.string or ""
        if not text:
            continue
        for pattern in script_patterns:
            match = re.search(pattern, text, re.DOTALL)
            if not match:
                continue
            try:
                parsed_data = json.loads(match.group(1))
                events = _parse_events_from_json(parsed_data)
                if events:
                    logger.info(
                        f"Embedded-JSON extraction: {len(events)} events "
                        f"(pattern: {pattern[:50]}…)"
                    )
                    return events
            except (json.JSONDecodeError, ValueError):
                continue

    # --- Strategy 2: <script type="application/json"> or application/ld+json ---
    for script_tag in soup.find_all("script", attrs={"type": re.compile(r"json")}):
        try:
            parsed_data = json.loads(script_tag.string or "")
            events = _parse_events_from_json(parsed_data)
            if events:
                logger.info(f"application/json script block: {len(events)} events")
                return events
        except (json.JSONDecodeError, ValueError):
            continue

    # --- Strategy 3: HTML <table> parsing ---
    events: list[dict] = []
    for table in soup.find_all("table"):
        rows = table.find_all("tr")
        if len(rows) < 2:
            continue
        headers = [
            th.get_text(strip=True).lower()
            for th in rows[0].find_all(["th", "td"])
        ]
        col: dict[str, int] = {}
        for i, h in enumerate(headers):
            if re.search(r"tick|symb", h):
                col["ticker"] = i
            elif re.search(r"company|name", h):
                col["company_name"] = i
            elif re.search(r"type|event|catalyst", h):
                col["event_type"] = i
            elif re.search(r"date", h):
                col["event_date"] = i
            elif re.search(r"drug|indica", h):
                col["drug_indication"] = i
            elif re.search(r"stage|phase", h):
                col["stage"] = i

        if "ticker" not in col or "event_date" not in col:
            continue

        for row in rows[1:]:
            cells = row.find_all(["td", "th"])
            def cell(key: str) -> str:
                idx = col.get(key)
                return cells[idx].get_text(strip=True) if idx is not None and idx < len(cells) else ""

            ticker = cell("ticker").upper().strip("$")
            if not ticker or len(ticker) > 8:
                continue
            events.append({
                "ticker": ticker,
                "company_name": cell("company_name"),
                "event_type": cell("event_type"),
                "event_date": cell("event_date"),
                "drug_indication": cell("drug_indication"),
                "stage": cell("stage"),
            })

        if events:
            logger.info(f"Table parsing: {len(events)} events")
            return events

    # --- Strategy 4: data-* attribute elements ---
    containers = soup.find_all(attrs={"data-ticker": True}) or soup.find_all(attrs={"data-symbol": True})
    for el in containers:
        ticker = (el.get("data-ticker") or el.get("data-symbol", "")).upper().strip("$")
        if not ticker:
            continue
        date_el = el.find(class_=re.compile(r"date|when", re.I)) or el.find("time")
        events.append({
            "ticker": ticker,
            "company_name": "",
            "event_type": "",
            "event_date": date_el.get("datetime", "") if date_el else "",
            "drug_indication": "",
            "stage": "",
        })

    if events:
        logger.info(f"data-* attribute parsing: {len(events)} events")

    return events


# ---------------------------------------------------------------------------
# Fetch: requests
# ---------------------------------------------------------------------------

def _fetch_with_requests(logger: logging.Logger) -> list[dict]:
    session = requests.Session()

    # 1. Try API endpoints first (fastest path)
    for url in _API_CANDIDATES:
        try:
            logger.debug(f"Trying API endpoint: {url}")
            resp = session.get(url, headers=_JSON_HEADERS, timeout=12)
            if resp.status_code == 200 and "json" in resp.headers.get("Content-Type", ""):
                events = _parse_events_from_json(resp.json())
                if events:
                    logger.info(f"API hit at {url}: {len(events)} events")
                    return events
        except Exception as exc:
            logger.debug(f"API {url} → {exc}")

    # 2. Fetch the full HTML page
    try:
        logger.debug(f"Fetching HTML: {FDA_CALENDAR_URL}")
        resp = session.get(FDA_CALENDAR_URL, headers=_HTML_HEADERS, timeout=20)
        resp.raise_for_status()

        if len(resp.text) < 2000:
            logger.warning("Response too small — likely bot-block or redirect")
            return []

        low = resp.text.lower()
        if any(s in low for s in ["captcha", "access denied", "403 forbidden", "just a moment"]):
            logger.warning("Bot-detection page detected")
            return []

        return _parse_events_from_html(resp.text, logger)

    except requests.RequestException as exc:
        logger.error(f"requests fetch failed: {exc}")
        return []


# ---------------------------------------------------------------------------
# Fetch: Selenium (JavaScript fallback)
# ---------------------------------------------------------------------------

def _build_chrome_options() -> "ChromeOptions":
    opts = ChromeOptions()
    opts.add_argument("--headless=new")
    opts.add_argument("--no-sandbox")
    opts.add_argument("--disable-dev-shm-usage")
    opts.add_argument("--disable-gpu")
    opts.add_argument("--window-size=1920,1080")
    opts.add_argument("--disable-blink-features=AutomationControlled")
    opts.add_argument(
        "--user-agent=Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
    )
    opts.add_experimental_option("excludeSwitches", ["enable-automation"])
    opts.add_experimental_option("useAutomationExtension", False)
    return opts


# XHR/fetch interceptor injected before page load to capture API responses
_XHR_INTERCEPTOR_JS = """
window._captured_api_data = [];
const _origFetch = window.fetch;
window.fetch = async function(...args) {
    const resp = await _origFetch(...args);
    try {
        const url = typeof args[0] === 'string' ? args[0] : (args[0].url || '');
        if (url.includes('/api/') || url.includes('calendar') || url.includes('catalyst')) {
            const clone = resp.clone();
            const ct = clone.headers.get('content-type') || '';
            if (ct.includes('json')) {
                clone.json().then(d => window._captured_api_data.push({url: url, data: d}));
            }
        }
    } catch(e) {}
    return resp;
};
const _origXHROpen = XMLHttpRequest.prototype.open;
const _origXHRSend = XMLHttpRequest.prototype.send;
XMLHttpRequest.prototype.open = function(m, u) {
    this._url = u;
    return _origXHROpen.apply(this, arguments);
};
XMLHttpRequest.prototype.send = function() {
    this.addEventListener('load', function() {
        try {
            const u = this._url || '';
            if (u.includes('/api/') || u.includes('calendar') || u.includes('catalyst')) {
                const data = JSON.parse(this.responseText);
                window._captured_api_data.push({url: u, data: data});
            }
        } catch(e) {}
    });
    return _origXHRSend.apply(this, arguments);
};
"""


def _fetch_with_selenium(logger: logging.Logger) -> list[dict]:
    if not SELENIUM_AVAILABLE:
        logger.error("Selenium not installed — cannot use browser fallback")
        return []

    driver = None
    try:
        opts = _build_chrome_options()
        driver = webdriver.Chrome(options=opts)

        # Inject XHR interceptor so we can capture the API call the page makes
        driver.execute_cdp_cmd(
            "Page.addScriptToEvaluateOnNewDocument",
            {"source": _XHR_INTERCEPTOR_JS},
        )

        logger.info("Selenium: navigating to FDA calendar…")
        driver.get(FDA_CALENDAR_URL)

        # Wait up to 25 s for meaningful content
        try:
            WebDriverWait(driver, 25).until(
                lambda d: len(
                    d.find_elements(By.CSS_SELECTOR, "tr, [data-ticker], .catalyst-row, tbody tr")
                ) > 1
                or d.execute_script("return (window._captured_api_data || []).length") > 0
            )
        except Exception:
            logger.warning("Selenium: content wait timed out — proceeding anyway")
            time.sleep(4)

        # --- Priority: captured XHR/fetch API responses ---
        api_payloads = driver.execute_script("return window._captured_api_data || []") or []
        for payload in api_payloads:
            events = _parse_events_from_json(payload.get("data", {}))
            if events:
                logger.info(
                    f"Selenium XHR intercept: {len(events)} events from {payload.get('url')}"
                )
                return events

        # --- Fallback: window globals from SSR frameworks ---
        for js_var in ("__NUXT__", "__NEXT_DATA__", "__INITIAL_STATE__", "__DATA__"):
            try:
                data = driver.execute_script(f"return window.{js_var} || null")
                if data:
                    events = _parse_events_from_json(data)
                    if events:
                        logger.info(f"Selenium window.{js_var}: {len(events)} events")
                        return events
            except Exception:
                pass

        # --- Last resort: parse rendered HTML ---
        events = _parse_events_from_html(driver.page_source, logger)
        return events

    except Exception as exc:
        logger.error(f"Selenium error: {exc}")
        return []
    finally:
        if driver:
            driver.quit()


# ---------------------------------------------------------------------------
# Orchestrated scrape
# ---------------------------------------------------------------------------

def scrape_fda_calendar(logger: logging.Logger) -> tuple[list[dict], list[str]]:
    errors: list[str] = []

    logger.info("=== STEP 1: Scrape FDA Calendar ===")
    events = _fetch_with_requests(logger)

    if not events:
        logger.info("requests approach yielded no data — falling back to Selenium")
        events = _fetch_with_selenium(logger)

    if not events:
        msg = "No events extracted from any scraping method"
        logger.error(msg)
        errors.append(msg)

    logger.info(f"Raw events scraped: {len(events)}")
    return events, errors


# ---------------------------------------------------------------------------
# Date parsing & filtering
# ---------------------------------------------------------------------------

_DATE_FORMATS = (
    "%Y-%m-%d", "%m/%d/%Y", "%m-%d-%Y",
    "%B %d, %Y", "%b %d, %Y",
    "%d/%m/%Y", "%Y/%m/%d", "%m/%d/%y",
)


def _parse_date(value: str) -> Optional[date]:
    value = (value or "").strip()
    if not value:
        return None
    for fmt in _DATE_FORMATS:
        try:
            return datetime.strptime(value, fmt).date()
        except ValueError:
            pass
    # e.g. "Apr 26 2026" without comma
    m = re.match(r"([A-Za-z]+)\s+(\d{1,2}),?\s+(\d{4})", value)
    if m:
        for fmt in ("%B %d %Y", "%b %d %Y"):
            try:
                return datetime.strptime(f"{m.group(1)} {m.group(2)} {m.group(3)}", fmt).date()
            except ValueError:
                pass
    return None


def filter_by_date(events: list[dict], logger: logging.Logger) -> list[dict]:
    logger.info("=== STEP 2: Date filtering ===")
    today = datetime.now(ET_TZ).date()
    min_date = today + timedelta(days=MIN_DAYS_AHEAD)
    max_date = today + timedelta(days=LOOK_AHEAD_DAYS)
    logger.info(f"Window: {min_date} → {max_date}  (today ET = {today})")

    kept: list[dict] = []
    for event in events:
        d = _parse_date(event.get("event_date", ""))
        if d is None:
            logger.debug(f"  skip {event.get('ticker')!r}: unparseable date {event.get('event_date')!r}")
            continue
        if min_date <= d <= max_date:
            event["event_date_obj"] = d
            event["days_until_event"] = (d - today).days
            kept.append(event)

    logger.info(f"After date filter: {len(kept)} / {len(events)}")
    return kept


# ---------------------------------------------------------------------------
# Float data
# ---------------------------------------------------------------------------

def _get_float(ticker: str, logger: logging.Logger) -> tuple[Optional[int], str]:
    """Return (shares, source_field) or (None, 'N/A')."""
    try:
        info = yf.Ticker(ticker).info
        for field in ("floatShares", "sharesOutstanding"):
            val = info.get(field)
            if val and int(val) > 0:
                return int(val), field
        logger.warning(f"  {ticker}: no float data in yfinance")
        return None, "N/A"
    except Exception as exc:
        logger.warning(f"  {ticker}: yfinance error — {exc}")
        return None, "N/A"


def enrich_with_float(
    events: list[dict], logger: logging.Logger
) -> tuple[list[dict], list[str]]:
    logger.info("=== STEP 3: Fetch float data ===")
    unique_tickers = list(dict.fromkeys(e["ticker"] for e in events))
    logger.info(f"Fetching float for {len(unique_tickers)} tickers…")

    cache: dict[str, tuple[Optional[int], str]] = {}
    failed: list[str] = []

    for ticker in unique_tickers:
        shares, source = _get_float(ticker, logger)
        cache[ticker] = (shares, source)
        if shares is None:
            failed.append(ticker)
        time.sleep(0.35)   # polite rate-limit

    for event in events:
        shares, source = cache[event["ticker"]]
        event["float_shares"] = shares
        event["float_source"] = source
        event["float_millions"] = round(shares / 1_000_000, 2) if shares else None

    return events, failed


# ---------------------------------------------------------------------------
# Low-float filter
# ---------------------------------------------------------------------------

def filter_low_float(events: list[dict], logger: logging.Logger) -> list[dict]:
    logger.info("=== STEP 4: Low-float filter (<50 M shares) ===")
    kept: list[dict] = []
    for event in events:
        fs = event.get("float_shares")
        if fs is None:
            event["needs_manual_review"] = True
            kept.append(event)
        elif fs < FLOAT_THRESHOLD:
            event["needs_manual_review"] = False
            kept.append(event)
    logger.info(f"After float filter: {len(kept)} / {len(events)}")
    return kept


# ---------------------------------------------------------------------------
# Output helpers
# ---------------------------------------------------------------------------

def _output_paths(output_dir: Path) -> tuple[Path, Path]:
    today_str = datetime.now(ET_TZ).strftime("%Y-%m-%d")
    return (
        output_dir / f"fda_lowfloat_{today_str}.csv",
        output_dir / f"fda_lowfloat_{today_str}_log.txt",
    )


_CSV_COLUMNS = [
    "ticker", "company_name", "event_type", "event_date",
    "days_until_event", "drug_indication",
    "float_shares", "float_millions", "needs_manual_review",
]


def write_csv(events: list[dict], csv_path: Path, logger: logging.Logger) -> None:
    rows = []
    for e in events:
        rows.append({
            "ticker": e.get("ticker", ""),
            "company_name": e.get("company_name", ""),
            "event_type": e.get("event_type", ""),
            "event_date": e.get("event_date_obj", e.get("event_date", "")),
            "days_until_event": e.get("days_until_event", ""),
            "drug_indication": e.get("drug_indication", ""),
            "float_shares": e.get("float_shares") if e.get("float_shares") is not None else "N/A",
            "float_millions": e.get("float_millions") if e.get("float_millions") is not None else "N/A",
            "needs_manual_review": e.get("needs_manual_review", False),
        })

    def _sort_key(row: dict):
        d = row["event_date"]
        date_str = d.isoformat() if hasattr(d, "isoformat") else str(d)
        fs = row["float_shares"]
        float_val = float(fs) if isinstance(fs, (int, float)) else float("inf")
        return (date_str, float_val)

    rows.sort(key=_sort_key)
    pd.DataFrame(rows, columns=_CSV_COLUMNS).to_csv(csv_path, index=False)
    logger.info(f"CSV written → {csv_path}  ({len(rows)} rows)")


def write_summary_log(
    log_path: Path,
    run_ts: datetime,
    total_raw: int,
    total_final: int,
    failed_tickers: list[str],
    scrape_errors: list[str],
) -> None:
    lines = [
        "FDA Low-Float Scanner — Summary Log",
        "=" * 60,
        f"Run timestamp (ET): {run_ts.strftime('%Y-%m-%d %H:%M:%S %Z')}",
        "",
        f"Total FDA events scraped:        {total_raw}",
        f"Events after date + float filter: {total_final}",
        "",
    ]
    if failed_tickers:
        lines += [f"Tickers with no float data ({len(failed_tickers)}):"]
        lines += [f"  - {t}" for t in failed_tickers]
        lines.append("")
    if scrape_errors:
        lines += [f"Scraping / pipeline errors ({len(scrape_errors)}):"]
        lines += [f"  - {e}" for e in scrape_errors]
        lines.append("")
    if not failed_tickers and not scrape_errors:
        lines.append("No errors encountered.")

    log_path.write_text("\n".join(lines), encoding="utf-8")


# ---------------------------------------------------------------------------
# Git push (skipped inside GitHub Actions — the workflow step handles it)
# ---------------------------------------------------------------------------

def git_push(
    repo_path: Path,
    files: list[Path],
    commit_msg: str,
    logger: logging.Logger,
) -> bool:
    if os.environ.get("SKIP_GIT_PUSH", "").lower() in ("1", "true", "yes"):
        logger.info("SKIP_GIT_PUSH set — skipping local git push (handled by CI)")
        return True

    try:
        def _run(*args: str) -> subprocess.CompletedProcess:
            return subprocess.run(
                ["git", "-C", str(repo_path), *args],
                check=True, capture_output=True, text=True,
            )

        _run("config", "user.email", os.environ.get("GIT_EMAIL", "fda-scanner@noreply.local"))
        _run("config", "user.name",  os.environ.get("GIT_NAME",  "FDA Scanner Bot"))

        for f in files:
            _run("add", str(f.relative_to(repo_path)))

        # Nothing staged → skip commit
        diff = subprocess.run(
            ["git", "-C", str(repo_path), "diff", "--staged", "--quiet"],
            capture_output=True,
        )
        if diff.returncode == 0:
            logger.info("git: nothing to commit")
            return True

        _run("commit", "-m", commit_msg)

        # Build authenticated remote URL if token provided
        token = os.environ.get("GITHUB_TOKEN", "")
        user  = os.environ.get("GITHUB_USERNAME", "")
        repo  = os.environ.get("GITHUB_REPO_NAME", "")

        if token and user and repo:
            remote = f"https://{user}:{token}@github.com/{user}/{repo}.git"
            result = subprocess.run(
                ["git", "-C", str(repo_path), "push", remote, "HEAD"],
                capture_output=True, text=True,
            )
        else:
            result = subprocess.run(
                ["git", "-C", str(repo_path), "push"],
                capture_output=True, text=True,
            )

        if result.returncode != 0:
            logger.error(f"git push failed:\n{result.stderr}")
            return False

        logger.info("git push succeeded")
        return True

    except subprocess.CalledProcessError as exc:
        logger.error(f"git operation failed: {exc.stderr or exc}")
        return False


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main() -> None:
    run_ts = datetime.now(ET_TZ)

    repo_path = Path(os.environ.get("GITHUB_REPO_PATH", ".")).resolve()
    output_dir = repo_path / "fda_calendar"
    output_dir.mkdir(parents=True, exist_ok=True)

    csv_path, log_path = _output_paths(output_dir)
    logger = setup_logging(log_path)

    logger.info(f"FDA Low-Float Scanner starting  {run_ts.strftime('%Y-%m-%d %H:%M:%S %Z')}")
    logger.info(f"Output directory: {output_dir}")

    all_scrape_errors: list[str] = []
    all_failed_tickers: list[str] = []
    total_raw = 0
    final_events: list[dict] = []

    try:
        raw_events, scrape_errors = scrape_fda_calendar(logger)
        all_scrape_errors.extend(scrape_errors)
        total_raw = len(raw_events)

        date_filtered = filter_by_date(raw_events, logger)
        enriched, failed = enrich_with_float(date_filtered, logger)
        all_failed_tickers.extend(failed)
        final_events = filter_low_float(enriched, logger)

        write_csv(final_events, csv_path, logger)

    except Exception:
        logger.exception("Fatal pipeline error")
        all_scrape_errors.append("Fatal pipeline error — see log for traceback")

    finally:
        write_summary_log(
            log_path=log_path,
            run_ts=run_ts,
            total_raw=total_raw,
            total_final=len(final_events),
            failed_tickers=all_failed_tickers,
            scrape_errors=all_scrape_errors,
        )
        logger.info(f"Summary log written → {log_path}")

        commit_msg = f"FDA low-float scan {run_ts.strftime('%Y-%m-%d %H:%M')} ET"
        git_push(repo_path, [csv_path, log_path], commit_msg, logger)

        logger.info("=== Scan complete ===")


if __name__ == "__main__":
    main()
