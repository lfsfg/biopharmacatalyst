"""Finviz screener — the candidate universe and short-interest data.

Bugs this module exists to fix, all observed in v1's first live run
(2026-08-26, 213 tickers):

1. Every ticker came back with its first character doubled (BHVN -> BBHVN)
   because the cell text was read with get_text(), which concatenates a
   nested element inside the ticker cell. We now read the ticker out of the
   quote.ashx?t=... href, which is unambiguous.

2. Short float was empty for all 213 rows while Short Ratio populated fine,
   so the join was healthy and only the column NAME was wrong. Fields are now
   resolved against a list of candidate header names.

3. One industry returned zero rows, which is indistinguishable from "no
   company matches these filters" unless you ask a second question. See
   probe_industry_token().

Deliberately NOT fixed by string surgery: the doubling is repaired at the
source (the href), never by de-duplicating characters. A "repair" heuristic
would corrupt real tickers such as AAPL and AA. Symbols are instead validated
against the SEC ticker map, which is authoritative.
"""

from __future__ import annotations

import logging
import re
import time
from typing import Iterable, Optional
from urllib.parse import parse_qs, urlparse

import requests
from bs4 import BeautifulSoup

BASE_URL = "https://finviz.com/screener.ashx"

BASE_FILTERS = ["sec_healthcare", "geo_usa", "sh_opt_optionshort", "sh_short_o10"]

# Filters minus the short-float constraint, for the "is the token valid?" probe.
PROBE_FILTERS = ["sec_healthcare", "geo_usa"]

INDUSTRIES = {
    "Biotechnology": "ind_biotechnology",
    "Drug Manufacturers - General": "ind_drugmanufacturersgeneral",
    "Drug Manufacturers - Specialty & Generic": "ind_drugmanufacturersspecialtygeneric",
}

VIEWS = {"overview": "111", "ownership": "131"}

ROWS_PER_PAGE = 20
MAX_PAGES = 30

# Finviz renames columns between views and occasionally between releases.
# Each field lists candidate header names in preference order.
FIELD_ALIASES = {
    "company":      ["Company"],
    "market_cap":   ["Market Cap"],
    "short_float":  ["Short Float", "Float Short", "Short Float %",
                     "Short Interest", "Short Float / Ratio"],
    "short_ratio":  ["Short Ratio", "Short Interest Ratio", "Days to Cover"],
    "float_shares": ["Float", "Shs Float", "Shares Float"],
    "avg_volume":   ["Avg Volume", "Average Volume", "Avg Vol"],
    "price":        ["Price"],
}

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
    ),
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "en-US,en;q=0.9",
}

TIMEOUT = 30
DELAY = 0.5

log = logging.getLogger(__name__)


# --------------------------------------------------------------------------
# Parsing
# --------------------------------------------------------------------------

_SYMBOL_RE = re.compile(r"[A-Z][A-Z0-9.\-]{0,9}")


def _valid(sym: Optional[str]) -> Optional[str]:
    sym = (sym or "").strip().upper()
    return sym if _SYMBOL_RE.fullmatch(sym) else None


def _ticker_from_row(row, ticker_cell=None) -> Optional[str]:
    """Extract the ticker without trusting the cell's concatenated text.

    Cell text is unreliable: the cell wraps the symbol alongside another
    element (a badge or logo), so get_text() returns 'AABEO' for ABEO.
    Four strategies, most specific first:

      1. quote.ashx?t=SYM   -- the classic Finviz link
      2. /quote/SYM         -- path-style link
      3. the ANCHOR's own text, not the cell's. This is the robust one: it
         works whatever the href looks like, because the badge sits outside
         the <a> while the symbol sits inside it.
      4. a data-ticker / data-symbol attribute

    Returning None falls through to the raw cell text, which is then
    validated against the SEC ticker map rather than "repaired".
    """
    anchors = row.find_all("a", href=True)

    for a in anchors:
        href = a["href"]
        if "quote.ashx" in href:
            sym = _valid((parse_qs(urlparse(href).query).get("t") or [""])[0])
            if sym:
                return sym

    for a in anchors:
        parts = [p for p in urlparse(a["href"]).path.split("/") if p]
        if "quote" in parts:
            i = parts.index("quote")
            if i + 1 < len(parts):
                sym = _valid(parts[i + 1])
                if sym:
                    return sym

    # Anchor text inside the ticker cell, ignoring sibling badge elements.
    scope = ticker_cell if ticker_cell is not None else row
    for a in scope.find_all("a"):
        sym = _valid(a.get_text(strip=True))
        if sym:
            return sym

    for node in (scope, row):
        for attr in ("data-ticker", "data-symbol", "data-boxover"):
            sym = _valid(node.get(attr) if hasattr(node, "get") else None)
            if sym:
                return sym
    return None


def parse_screener_table(html: str) -> list[dict]:
    """Return one dict per result row, keyed by header name, plus '_ticker'."""
    soup = BeautifulSoup(html, "lxml")
    best: list[dict] = []

    for table in soup.find_all("table"):
        rows = table.find_all("tr")
        if len(rows) < 2:
            continue
        header = [c.get_text(strip=True) for c in rows[0].find_all(["td", "th"])]
        if "Ticker" not in header:
            continue

        parsed: list[dict] = []
        for row in rows[1:]:
            cells = row.find_all("td")
            if len(cells) != len(header):
                continue
            idx_t = header.index("Ticker")
            ticker = _ticker_from_row(
                row, cells[idx_t] if idx_t < len(cells) else None)
            if not ticker:
                # No quote link. Take the cell text verbatim -- never try to
                # "repair" it by de-duplicating characters, which would corrupt
                # legitimate tickers (AAPL -> APL, AA -> A). Suspicious symbols
                # are caught downstream by validation against the SEC ticker
                # map, which is authoritative.
                ticker = cells[idx_t].get_text(strip=True).upper()
            if not re.fullmatch(r"[A-Z][A-Z0-9.\-]{0,9}", ticker or ""):
                continue
            rec = {name: cells[i].get_text(strip=True)
                   for i, name in enumerate(header)}
            rec["_ticker"] = ticker
            parsed.append(rec)

        if len(parsed) > len(best):
            best = parsed
    return best


def resolve(rec: dict, field: str) -> Optional[str]:
    """Look up a logical field through its candidate header names."""
    for alias in FIELD_ALIASES.get(field, []):
        if alias in rec and rec[alias] not in ("", "-"):
            return rec[alias]
    return None


def to_number(value: Optional[str]) -> Optional[float]:
    """'18.42%' -> 18.42 ; '1.23B' -> 1230000000.0 ; '-' -> None."""
    if not value:
        return None
    v = value.strip().replace(",", "").replace("%", "")
    m = re.fullmatch(r"(-?\d+(?:\.\d+)?)\s*([KMBT])?", v, re.IGNORECASE)
    if not m:
        return None
    num = float(m.group(1))
    mult = {"K": 1e3, "M": 1e6, "B": 1e9, "T": 1e12}
    suffix = m.group(2)
    return num * mult[suffix.upper()] if suffix else num


# --------------------------------------------------------------------------
# Fetching
# --------------------------------------------------------------------------

def _fetch_page(view: str, filters: Iterable[str], offset: int,
                session: requests.Session) -> Optional[str]:
    params = {"v": view, "f": ",".join(filters), "r": str(offset)}
    try:
        resp = session.get(BASE_URL, params=params, headers=HEADERS, timeout=TIMEOUT)
    except requests.RequestException as exc:
        log.error("Finviz request failed (r=%d): %s", offset, exc)
        return None
    if resp.status_code != 200:
        log.error("Finviz HTTP %d (r=%d)", resp.status_code, offset)
        return None
    return resp.text


def collect(view_key: str, filters: list[str],
            session: requests.Session) -> dict[str, dict]:
    """Page through one view, returning {ticker: row}."""
    out: dict[str, dict] = {}
    offset = 1
    for _ in range(MAX_PAGES):
        html = _fetch_page(VIEWS[view_key], filters, offset, session)
        if html is None:
            break
        rows = parse_screener_table(html)
        if not rows:
            break
        for rec in rows:
            out.setdefault(rec["_ticker"], rec)
        if len(rows) < ROWS_PER_PAGE:
            break
        offset += ROWS_PER_PAGE
        time.sleep(DELAY)
    return out


def probe_industry_token(token: str, session: requests.Session) -> bool:
    """Is this industry token valid, independent of the short-float filter?

    An industry returning zero rows under the full filter set is ambiguous:
    the token could be wrong, or big pharma could genuinely have no names
    with >10% short float. Re-running with only sector+country filters
    settles it — a valid token always returns rows there.
    """
    html = _fetch_page(VIEWS["overview"], PROBE_FILTERS + [token], 1, session)
    return bool(html and parse_screener_table(html))


def probe(session: requests.Session) -> tuple[bool, str, dict]:
    """Full health check of the Finviz stage without running the screen.

    Verifies three separate things, because v1 proved they fail independently:
      1. the screener responds and its result table parses
      2. tickers come back as clean symbols (v1 doubled the first character)
      3. the short-float column is actually present under one of its aliases
         (v1's defining filter was silently empty on every row)
    """
    metrics: dict = {}
    problems: list[str] = []

    # Industry tokens.
    bad_tokens = [name for name, token in INDUSTRIES.items()
                  if not probe_industry_token(token, session)]
    metrics["industries_ok"] = f"{len(INDUSTRIES) - len(bad_tokens)}/{len(INDUSTRIES)}"
    if bad_tokens:
        problems.append("invalid industry token(s): " + ", ".join(bad_tokens))

    # Ownership view: does short float parse?
    filters = BASE_FILTERS + [INDUSTRIES["Biotechnology"]]
    html = _fetch_page(VIEWS["ownership"], filters, 1, session)
    rows = parse_screener_table(html) if html else []
    metrics["ownership_rows"] = len(rows)
    if not rows:
        problems.append("ownership view returned no parseable rows")
    else:
        with_sf = sum(1 for r in rows if resolve(r, "short_float") is not None)
        metrics["short_float_rows"] = f"{with_sf}/{len(rows)}"
        if with_sf == 0:
            header = sorted(k for k in rows[0] if not k.startswith("_"))
            problems.append(
                "short float absent under every known alias "
                f"{FIELD_ALIASES['short_float']}; headers seen: {header}")

    # Overview view: do tickers look sane?
    html_ov = _fetch_page(VIEWS["overview"], filters, 1, session)
    rows_ov = parse_screener_table(html_ov) if html_ov else []
    metrics["overview_rows"] = len(rows_ov)
    if rows_ov:
        sample = [r["_ticker"] for r in rows_ov[:5]]
        metrics["sample_tickers"] = ",".join(sample)
        doubled = [t for t in sample if len(t) > 2 and t[0] == t[1]]
        if len(doubled) == len(sample):
            problems.append(f"every sampled ticker looks doubled: {sample}")
            # The href-based extractor is not firing. Capture the actual
            # markup so the next run says exactly why, instead of guessing.
            metrics["ticker_markup"] = _describe_ticker_cell(html_ov)
    else:
        problems.append("overview view returned no parseable rows")

    return (not problems), ("; ".join(problems) or "probe OK"), metrics


def _describe_ticker_cell(html: str) -> str:
    """Diagnostic dump of the first result row's ticker cell.

    Reports the raw markup and every href present, so a change in Finviz's
    link format can be fixed from evidence rather than guessed at.
    """
    soup = BeautifulSoup(html, "lxml")
    for table in soup.find_all("table"):
        rows = table.find_all("tr")
        if len(rows) < 2:
            continue
        header = [c.get_text(strip=True) for c in rows[0].find_all(["td", "th"])]
        if "Ticker" not in header:
            continue
        idx = header.index("Ticker")
        for row in rows[1:]:
            cells = row.find_all("td")
            if len(cells) != len(header):
                continue
            cell = cells[idx]
            hrefs = [a.get("href", "") for a in row.find_all("a", href=True)]
            return (
                f"cell_html={str(cell)[:400]!r} "
                f"cell_text={cell.get_text(strip=True)!r} "
                f"row_hrefs={hrefs[:6]}"
            )
    return "no result row found to describe"
