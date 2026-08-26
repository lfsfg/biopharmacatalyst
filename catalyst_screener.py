#!/usr/bin/env python3
"""
Quarterly Biotech Catalyst Screener — Stage A (deterministic collection).

Runs in the second-to-last week of the current calendar quarter and builds the
candidate list for NEXT quarter's catalysts.

Pipeline
--------
1. Finviz screener  -> universe of US healthcare (Biotechnology + Drug
   Manufacturers General/Specialty&Generic) names that are optionable,
   shortable and carry short float > 10%.  Captures ticker, company,
   market cap, short float %, short ratio (= days to cover).
2. Per ticker, look for a verifiable NEXT-QUARTER catalyst in:
     - SEC EDGAR full-text search (efts.sec.gov)  8-K / 10-Q language
     - ClinicalTrials.gov API v2 (Phase 3, primary/estimated completion
       inside the target quarter, company as lead sponsor)
     - pdufa.bio public PDUFA / readout calendar
3. Emit a candidates CSV + a human-readable log.  Stage B (the Claude
   Routine) reads the CSV and does the research/synthesis half.

Design note: this script FAILS LOUDLY.  If the Finviz stage yields zero
tickers it exits non-zero rather than committing an empty CSV.  The sibling
fda_scanner.py in this repo has silently committed 122 consecutive empty
files; that failure mode is deliberately not repeated here.

Environment
-----------
GITHUB_REPO_PATH   repo root (defaults to this file's directory)
SKIP_GIT_PUSH      'true' to let CI handle the commit
SEC_USER_AGENT     required by SEC; "Name email@example.com"
ALLOW_EMPTY        'true' to downgrade the empty-universe failure to a warning
"""

from __future__ import annotations

import calendar
import csv
import json
import logging
import os
import re
import sys
import time
from datetime import date, datetime
from pathlib import Path
from typing import Iterable, Optional

import pytz
import requests
from bs4 import BeautifulSoup

# --------------------------------------------------------------------------
# Config
# --------------------------------------------------------------------------

ET_TZ = pytz.timezone("America/New_York")

FINVIZ_BASE = "https://finviz.com/screener.ashx"

# Sector/geo/option/short filters shared by every pass.
#   sec_healthcare        Sector = Healthcare
#   geo_usa               Country = USA
#   sh_opt_optionshort    Optionable = Yes AND Shortable = Yes
#   sh_short_o10          Short Float > 10%
FINVIZ_BASE_FILTERS = ["sec_healthcare", "geo_usa", "sh_opt_optionshort", "sh_short_o10"]

# Finviz allows only ONE industry token per request on the free screener, so we
# run one pass per industry and union the results.
FINVIZ_INDUSTRIES = {
    "Biotechnology": "ind_biotechnology",
    "Drug Manufacturers - General": "ind_drugmanufacturersgeneral",
    "Drug Manufacturers - Specialty & Generic": "ind_drugmanufacturersspecialtygeneric",
}

# v=111 Overview  -> Ticker, Company, Sector, Industry, Country, Market Cap
# v=131 Ownership -> Ticker, Float Short, Short Ratio (= days to cover)
FINVIZ_VIEWS = {"overview": "111", "ownership": "131"}

FINVIZ_ROWS_PER_PAGE = 20
FINVIZ_MAX_PAGES = 25          # 500 rows per industry/view is far beyond need

SEC_TICKER_MAP_URL = "https://www.sec.gov/files/company_tickers.json"
EDGAR_FTS_URL = "https://efts.sec.gov/LATEST/search-index"
EDGAR_FORMS = "8-K,10-Q"

# Phrases that indicate a dated, near-term catalyst.
EDGAR_PHRASES = [
    "topline",
    "top-line",
    "PDUFA",
    "target action date",
    "BLA",
    "NDA",
]

CTGOV_URL = "https://clinicaltrials.gov/api/v2/studies"
CTGOV_FIELDS = (
    "protocolSection.identificationModule.nctId,"
    "protocolSection.identificationModule.briefTitle,"
    "protocolSection.sponsorCollaboratorsModule.leadSponsor.name,"
    "protocolSection.designModule.phases,"
    "protocolSection.statusModule.primaryCompletionDateStruct.date,"
    "protocolSection.statusModule.completionDateStruct.date,"
    "protocolSection.statusModule.overallStatus,"
    "protocolSection.conditionsModule.conditions,"
    "protocolSection.armsInterventionsModule.interventions"
)

PDUFA_URL = "https://pdufa.bio/"

HTML_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
    ),
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "en-US,en;q=0.9",
}

REQUEST_TIMEOUT = 30
POLITE_DELAY = 0.4      # between requests to the same host


# --------------------------------------------------------------------------
# Quarter maths
# --------------------------------------------------------------------------

def next_quarter_window(today: date) -> tuple[date, date, str]:
    """Return (start, end, label) for the calendar quarter AFTER today's."""
    q = (today.month - 1) // 3 + 1
    if q == 4:
        year, nq = today.year + 1, 1
    else:
        year, nq = today.year, q + 1
    start_month = 3 * (nq - 1) + 1
    end_month = start_month + 2
    start = date(year, start_month, 1)
    end = date(year, end_month, calendar.monthrange(year, end_month)[1])
    return start, end, f"Q{nq} {year}"


# --------------------------------------------------------------------------
# Logging
# --------------------------------------------------------------------------

def setup_logging(log_path: Path) -> logging.Logger:
    log_path.parent.mkdir(parents=True, exist_ok=True)
    logger = logging.getLogger("catalyst_screener")
    logger.setLevel(logging.INFO)
    logger.handlers.clear()
    fmt = logging.Formatter("%(asctime)s [%(levelname)s] %(message)s")
    fh = logging.FileHandler(log_path, encoding="utf-8")
    fh.setFormatter(fmt)
    sh = logging.StreamHandler(sys.stdout)
    sh.setFormatter(fmt)
    logger.addHandler(fh)
    logger.addHandler(sh)
    return logger


# --------------------------------------------------------------------------
# Finviz
# --------------------------------------------------------------------------

def _finviz_page(view: str, industry_token: str, offset: int,
                 logger: logging.Logger) -> Optional[str]:
    filters = ",".join(FINVIZ_BASE_FILTERS + [industry_token])
    params = {"v": view, "f": filters, "r": str(offset)}
    try:
        resp = requests.get(FINVIZ_BASE, params=params, headers=HTML_HEADERS,
                            timeout=REQUEST_TIMEOUT)
    except requests.RequestException as exc:
        logger.error("Finviz request failed (%s, r=%d): %s", industry_token, offset, exc)
        return None
    if resp.status_code != 200:
        logger.error("Finviz HTTP %d (%s, r=%d)", resp.status_code, industry_token, offset)
        return None
    return resp.text


def _parse_finviz_table(html: str) -> list[dict]:
    """Parse the screener results table, mapping columns by HEADER NAME.

    Index-based parsing breaks whenever Finviz reorders columns; name-based
    parsing degrades gracefully instead.
    """
    soup = BeautifulSoup(html, "lxml")

    best: list[dict] = []
    for table in soup.find_all("table"):
        rows = table.find_all("tr")
        if len(rows) < 2:
            continue
        header_cells = [c.get_text(strip=True) for c in rows[0].find_all(["td", "th"])]
        if "Ticker" not in header_cells:
            continue
        idx = {name: i for i, name in enumerate(header_cells)}
        parsed: list[dict] = []
        for row in rows[1:]:
            cells = [c.get_text(strip=True) for c in row.find_all("td")]
            if len(cells) != len(header_cells):
                continue
            rec = {name: cells[i] for name, i in idx.items() if i < len(cells)}
            if rec.get("Ticker"):
                parsed.append(rec)
        if len(parsed) > len(best):
            best = parsed
    return best


def _finviz_collect(view_key: str, industry_token: str,
                    logger: logging.Logger) -> dict[str, dict]:
    view = FINVIZ_VIEWS[view_key]
    out: dict[str, dict] = {}
    offset = 1
    for _ in range(FINVIZ_MAX_PAGES):
        html = _finviz_page(view, industry_token, offset, logger)
        if html is None:
            break
        rows = _parse_finviz_table(html)
        if not rows:
            break
        for rec in rows:
            out.setdefault(rec["Ticker"].upper(), rec)
        if len(rows) < FINVIZ_ROWS_PER_PAGE:
            break
        offset += FINVIZ_ROWS_PER_PAGE
        time.sleep(POLITE_DELAY)
    return out


def _pct_to_float(value: str) -> Optional[float]:
    if not value:
        return None
    m = re.search(r"-?\d+(?:\.\d+)?", value.replace(",", ""))
    return float(m.group()) if m else None


def run_finviz_screen(logger: logging.Logger) -> tuple[list[dict], list[str]]:
    """Union the per-industry screens and join overview + ownership views."""
    errors: list[str] = []
    universe: dict[str, dict] = {}

    for industry_name, token in FINVIZ_INDUSTRIES.items():
        overview = _finviz_collect("overview", token, logger)
        time.sleep(POLITE_DELAY)
        ownership = _finviz_collect("ownership", token, logger)

        logger.info("Finviz %-42s overview=%3d ownership=%3d",
                    industry_name, len(overview), len(ownership))
        if not overview:
            errors.append(f"Finviz returned no rows for industry '{industry_name}'")

        for ticker, ov in overview.items():
            own = ownership.get(ticker, {})
            short_float = _pct_to_float(own.get("Float Short", ""))
            short_ratio = _pct_to_float(own.get("Short Ratio", ""))
            if ticker in universe:
                universe[ticker]["industry"] = (
                    f"{universe[ticker]['industry']} / {industry_name}"
                )
                continue
            universe[ticker] = {
                "ticker": ticker,
                "company_name": ov.get("Company", ""),
                "industry": industry_name,
                "market_cap": ov.get("Market Cap", ""),
                "short_float_pct": short_float if short_float is not None else "",
                "days_to_cover": short_ratio if short_ratio is not None else "",
            }
            if short_float is None:
                errors.append(f"{ticker}: short float missing from Finviz ownership view")

    return list(universe.values()), errors


# --------------------------------------------------------------------------
# SEC EDGAR
# --------------------------------------------------------------------------

def _sec_headers() -> dict:
    ua = os.environ.get("SEC_USER_AGENT", "").strip()
    if not ua:
        ua = "biopharmacatalyst-screener contact@example.com"
    return {"User-Agent": ua, "Accept": "application/json"}


def load_cik_map(logger: logging.Logger) -> dict[str, str]:
    """ticker -> 10-digit zero-padded CIK."""
    try:
        resp = requests.get(SEC_TICKER_MAP_URL, headers=_sec_headers(),
                            timeout=REQUEST_TIMEOUT)
        resp.raise_for_status()
        data = resp.json()
    except Exception as exc:
        logger.error("Could not load SEC ticker->CIK map: %s", exc)
        return {}
    out = {}
    for entry in data.values():
        out[str(entry["ticker"]).upper()] = str(entry["cik_str"]).zfill(10)
    logger.info("Loaded SEC ticker->CIK map (%d entries)", len(out))
    return out


def check_edgar(ticker: str, cik: Optional[str], start: date, end: date,
                logger: logging.Logger) -> list[dict]:
    """Full-text search recent filings for catalyst language.

    Searches the ~120 days BEFORE the run (companies announce guidance ahead of
    the event), not the target window itself.
    """
    if not cik:
        return []
    today = date.today()
    lookback_start = date.fromordinal(today.toordinal() - 120)
    hits: list[dict] = []

    for phrase in EDGAR_PHRASES:
        params = {
            "q": f'"{phrase}"',
            "forms": EDGAR_FORMS,
            "ciks": cik,
            "dateRange": "custom",
            "startdt": lookback_start.isoformat(),
            "enddt": today.isoformat(),
        }
        try:
            resp = requests.get(EDGAR_FTS_URL, params=params,
                                headers=_sec_headers(), timeout=REQUEST_TIMEOUT)
            if resp.status_code != 200:
                logger.warning("EDGAR FTS HTTP %d for %s (%s)",
                               resp.status_code, ticker, phrase)
                continue
            payload = resp.json()
        except Exception as exc:
            logger.warning("EDGAR FTS failed for %s (%s): %s", ticker, phrase, exc)
            continue

        total = (payload.get("hits", {}).get("total", {}) or {}).get("value", 0)
        if not total:
            continue
        for hit in payload.get("hits", {}).get("hits", [])[:3]:
            src = hit.get("_source", {})
            hits.append({
                "phrase": phrase,
                "form": src.get("root_form") or src.get("form", ""),
                "filed": src.get("file_date", ""),
                "adsh": src.get("adsh", ""),
            })
        time.sleep(POLITE_DELAY)

    return hits


# --------------------------------------------------------------------------
# ClinicalTrials.gov v2
# --------------------------------------------------------------------------

def check_ctgov(company: str, start: date, end: date,
                logger: logging.Logger) -> list[dict]:
    """Phase 3 trials led by `company` completing inside the target window."""
    if not company:
        return []
    advanced = (
        f"AREA[Phase]PHASE3 AND "
        f"AREA[PrimaryCompletionDate]RANGE[{start.isoformat()},{end.isoformat()}]"
    )
    params = {
        "query.spons": company,
        "filter.advanced": advanced,
        "fields": CTGOV_FIELDS,
        "pageSize": "50",
    }
    try:
        resp = requests.get(CTGOV_URL, params=params, timeout=REQUEST_TIMEOUT,
                            headers={"Accept": "application/json"})
        if resp.status_code != 200:
            logger.warning("CT.gov HTTP %d for %s", resp.status_code, company)
            return []
        payload = resp.json()
    except Exception as exc:
        logger.warning("CT.gov failed for %s: %s", company, exc)
        return []

    out: list[dict] = []
    for study in payload.get("studies", []):
        proto = study.get("protocolSection", {})
        ident = proto.get("identificationModule", {})
        status = proto.get("statusModule", {})
        pcd = (status.get("primaryCompletionDateStruct") or {}).get("date", "")
        out.append({
            "nct_id": ident.get("nctId", ""),
            "title": ident.get("briefTitle", ""),
            "sponsor": (proto.get("sponsorCollaboratorsModule", {})
                        .get("leadSponsor", {}) or {}).get("name", ""),
            "primary_completion": pcd,
            "status": status.get("overallStatus", ""),
            "conditions": "; ".join(
                (proto.get("conditionsModule", {}) or {}).get("conditions", []) or []
            ),
        })
    return out


# --------------------------------------------------------------------------
# pdufa.bio
# --------------------------------------------------------------------------

_DATE_PATTERNS = (
    "%Y-%m-%d", "%b %d, %Y", "%B %d, %Y", "%m/%d/%Y", "%d %b %Y", "%b %d %Y",
)


def _parse_loose_date(value: str) -> Optional[date]:
    value = (value or "").strip()
    if not value:
        return None
    for fmt in _DATE_PATTERNS:
        try:
            return datetime.strptime(value, fmt).date()
        except ValueError:
            continue
    return None


def fetch_pdufa_calendar(logger: logging.Logger) -> tuple[dict[str, list[dict]], list[str]]:
    """Scrape pdufa.bio into {TICKER: [ {date, raw} ]}."""
    errors: list[str] = []
    try:
        resp = requests.get(PDUFA_URL, headers=HTML_HEADERS, timeout=REQUEST_TIMEOUT)
        if resp.status_code != 200:
            errors.append(f"pdufa.bio HTTP {resp.status_code}")
            return {}, errors
        html = resp.text
    except requests.RequestException as exc:
        errors.append(f"pdufa.bio fetch failed: {exc}")
        return {}, errors

    soup = BeautifulSoup(html, "lxml")
    out: dict[str, list[dict]] = {}
    for row in soup.find_all("tr"):
        cells = [c.get_text(" ", strip=True) for c in row.find_all("td")]
        if len(cells) < 2:
            continue
        joined = " | ".join(cells)
        ticker = None
        for cell in cells[:3]:
            m = re.fullmatch(r"\(?([A-Z]{1,5})\)?", cell.strip())
            if m:
                ticker = m.group(1)
                break
        if not ticker:
            continue
        parsed = next((d for d in (_parse_loose_date(c) for c in cells) if d), None)
        out.setdefault(ticker, []).append({
            "date": parsed.isoformat() if parsed else "",
            "raw": joined[:300],
        })

    if not out:
        errors.append("pdufa.bio returned no parseable rows (layout may have changed)")
    logger.info("pdufa.bio: parsed entries for %d tickers", len(out))
    return out, errors


# --------------------------------------------------------------------------
# Assembly
# --------------------------------------------------------------------------

CSV_COLUMNS = [
    "ticker", "company_name", "industry", "market_cap",
    "short_float_pct", "days_to_cover",
    "catalyst_sources", "earliest_catalyst_date", "catalyst_detail",
    "edgar_hits", "ctgov_hits", "pdufa_hits",
]


def screen_catalysts(universe: list[dict], start: date, end: date,
                     logger: logging.Logger) -> tuple[list[dict], list[str]]:
    errors: list[str] = []
    cik_map = load_cik_map(logger)
    pdufa_map, pdufa_errors = fetch_pdufa_calendar(logger)
    errors.extend(pdufa_errors)

    survivors: list[dict] = []
    for i, rec in enumerate(universe, 1):
        ticker = rec["ticker"]
        logger.info("[%d/%d] catalyst check %s", i, len(universe), ticker)

        edgar = check_edgar(ticker, cik_map.get(ticker), start, end, logger)
        ctgov = check_ctgov(rec.get("company_name", ""), start, end, logger)

        pdufa_all = pdufa_map.get(ticker, [])
        pdufa = [
            p for p in pdufa_all
            if p["date"] and start.isoformat() <= p["date"] <= end.isoformat()
        ]

        sources, details, dates = [], [], []
        if edgar:
            sources.append("EDGAR")
            phrases = sorted({h["phrase"] for h in edgar})
            latest = max((h["filed"] for h in edgar if h["filed"]), default="")
            details.append(f"EDGAR: {', '.join(phrases)} (latest filing {latest})")
        if ctgov:
            sources.append("CT.gov")
            for t in ctgov[:3]:
                details.append(
                    f"CT.gov {t['nct_id']} PCD {t['primary_completion']} "
                    f"[{t['status']}] {t['title'][:80]}"
                )
                if t["primary_completion"]:
                    dates.append(t["primary_completion"])
        if pdufa:
            sources.append("pdufa.bio")
            for p in pdufa[:3]:
                details.append(f"pdufa.bio {p['date']}: {p['raw'][:120]}")
                dates.append(p["date"])

        if not sources:
            continue

        out = dict(rec)
        out.update({
            "catalyst_sources": "; ".join(sources),
            "earliest_catalyst_date": min(dates) if dates else "",
            "catalyst_detail": " || ".join(details)[:1500],
            "edgar_hits": len(edgar),
            "ctgov_hits": len(ctgov),
            "pdufa_hits": len(pdufa),
        })
        survivors.append(out)

    return survivors, errors


def write_outputs(survivors: list[dict], universe_size: int, errors: list[str],
                  label: str, start: date, end: date,
                  csv_path: Path, log_path: Path, logger: logging.Logger) -> None:
    # Rank by catalyst-date proximity; undated catalysts sort last.
    def sort_key(r):
        d = r.get("earliest_catalyst_date") or ""
        return (0, d) if d else (1, "")

    survivors.sort(key=sort_key)
    for i, rec in enumerate(survivors, 1):
        rec["rank"] = i
        rec["research_tier"] = "FULL" if i <= 20 else "FLAGGED_ONLY"

    csv_path.parent.mkdir(parents=True, exist_ok=True)
    with csv_path.open("w", newline="", encoding="utf-8") as fh:
        writer = csv.DictWriter(fh, fieldnames=["rank", "research_tier"] + CSV_COLUMNS,
                                extrasaction="ignore")
        writer.writeheader()
        writer.writerows(survivors)
    logger.info("Wrote %d candidate rows -> %s", len(survivors), csv_path)

    now_et = datetime.now(ET_TZ)
    lines = [
        "Quarterly Biotech Catalyst Screener — Stage A summary",
        "=" * 64,
        f"Run timestamp (ET):     {now_et:%Y-%m-%d %H:%M:%S %Z}",
        f"Target catalyst window: {label}  ({start} .. {end})",
        "",
        f"Finviz universe size:        {universe_size}",
        f"Survived catalyst check:     {len(survivors)}",
        f"  full research (top 20):    {min(len(survivors), 20)}",
        f"  flagged-but-unresearched:  {max(len(survivors) - 20, 0)}",
        "",
    ]
    if survivors:
        lines.append("Candidates (rank. ticker  date  sources):")
        for rec in survivors:
            lines.append(
                f"  {rec['rank']:>3}. {rec['ticker']:<6} "
                f"{rec.get('earliest_catalyst_date') or 'undated':<12} "
                f"{rec['catalyst_sources']}"
            )
        lines.append("")
    if errors:
        lines.append(f"Errors / warnings ({len(errors)}):")
        lines.extend(f"  - {e}" for e in errors)
    else:
        lines.append("No errors.")
    log_path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    logger.info("Wrote summary -> %s", log_path)


def main() -> None:
    repo = Path(os.environ.get("GITHUB_REPO_PATH", Path(__file__).resolve().parent))
    out_dir = repo / "catalyst_screen"
    today = date.today()
    start, end, label = next_quarter_window(today)

    csv_path = out_dir / f"catalyst_candidates_{today.isoformat()}.csv"
    log_path = out_dir / f"catalyst_candidates_{today.isoformat()}_log.txt"
    logger = setup_logging(out_dir / f"catalyst_run_{today.isoformat()}.debug.log")

    logger.info("=== Quarterly catalyst screen: target %s (%s .. %s) ===",
                label, start, end)

    universe, errors = run_finviz_screen(logger)
    logger.info("Finviz universe: %d unique tickers", len(universe))

    if not universe:
        msg = ("Finviz screen returned ZERO tickers — refusing to write an empty "
               "candidate file. Check the screener URL/filters or whether Finviz "
               "is blocking the runner IP.")
        logger.error(msg)
        if os.environ.get("ALLOW_EMPTY", "").lower() != "true":
            log_path.parent.mkdir(parents=True, exist_ok=True)
            log_path.write_text(
                "Quarterly Biotech Catalyst Screener — Stage A FAILED\n"
                + "=" * 64 + f"\n\n{msg}\n\nErrors:\n"
                + "\n".join(f"  - {e}" for e in errors) + "\n",
                encoding="utf-8",
            )
            sys.exit(1)

    survivors, catalyst_errors = screen_catalysts(universe, start, end, logger)
    errors.extend(catalyst_errors)

    write_outputs(survivors, len(universe), errors, label, start, end,
                  csv_path, log_path, logger)
    logger.info("=== Stage A complete: %d candidates ===", len(survivors))


if __name__ == "__main__":
    main()
