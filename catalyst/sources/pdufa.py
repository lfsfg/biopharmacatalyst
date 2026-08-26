"""pdufa.bio -- public PDUFA / readout calendar.

v1 parsed zero rows from this site. The parser assumed a plain <tr>/<td>
table with a bare ticker cell; when that assumption missed, the source
contributed nothing and the run still passed.

v2 parses more tolerantly (tables, definition lists, and ticker-in-parens
text), and reports health so a zero-row result is visible rather than silent.
PDUFA dates are FDA-set regulatory deadlines, so anything found here is the
strongest tier available.
"""

from __future__ import annotations

import logging
import re
from typing import Optional

import requests
from bs4 import BeautifulSoup

from ..periods import Period, find_period, parse_period

URLS = ["https://pdufa.bio/", "https://pdufa.bio/calendar"]
TIMEOUT = 30
TIER = "HARD"

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
    ),
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
}

_TICKER_PAREN = re.compile(r"\(([A-Z]{1,5})\)")
_TICKER_BARE = re.compile(r"^([A-Z]{1,5})$")

log = logging.getLogger(__name__)


def _tickers_in(text: str) -> list[str]:
    found = _TICKER_PAREN.findall(text)
    if found:
        return found
    m = _TICKER_BARE.match(text.strip())
    return [m.group(1)] if m else []


def parse_calendar(html: str) -> dict[str, list[dict]]:
    """Extract {TICKER: [{period, raw}]} from whatever markup is present."""
    soup = BeautifulSoup(html, "lxml")
    out: dict[str, list[dict]] = {}

    def add(ticker: str, period: Optional[Period], raw: str) -> None:
        if not ticker or not period:
            return
        out.setdefault(ticker, []).append({"period": period, "raw": raw[:300]})

    # Table rows.
    for row in soup.find_all("tr"):
        cells = [c.get_text(" ", strip=True) for c in row.find_all(["td", "th"])]
        if len(cells) < 2:
            continue
        joined = " | ".join(cells)
        tickers: list[str] = []
        for cell in cells:
            tickers.extend(_tickers_in(cell))
        period = next(
            (p for p in (parse_period(c) or find_period(c) for c in cells) if p),
            None,
        )
        for t in dict.fromkeys(tickers):
            add(t, period, joined)

    # Card / list layouts: any block carrying both a ticker and a date.
    if not out:
        for node in soup.find_all(["li", "article", "div"]):
            text = node.get_text(" ", strip=True)
            if not (8 <= len(text) <= 400):
                continue
            tickers = _TICKER_PAREN.findall(text)
            if not tickers:
                continue
            period = find_period(text)
            for t in dict.fromkeys(tickers):
                add(t, period, text)

    return out


def fetch(session: requests.Session) -> tuple[dict[str, list[dict]], str]:
    """Try each known URL; return the first that parses to something."""
    last = "no URL attempted"
    for url in URLS:
        try:
            resp = session.get(url, headers=HEADERS, timeout=TIMEOUT)
        except requests.RequestException as exc:
            last = f"{url}: {exc}"
            continue
        if resp.status_code != 200:
            last = f"{url}: HTTP {resp.status_code}"
            continue
        parsed = parse_calendar(resp.text)
        if parsed:
            return parsed, f"{url}: {len(parsed)} tickers"
        last = f"{url}: fetched {len(resp.text)} bytes but parsed 0 rows"
    return {}, last


MIN_EXPECTED_TICKERS = 5


def probe(session: requests.Session) -> tuple[bool, str, dict]:
    """Does the calendar parse to a plausible number of rows?

    "More than zero" is too weak a contract: the first dry run returned a
    single ticker and was graded OK, when a PDUFA calendar with one entry is
    obviously a parse failure. Require a floor, and dump a sample so a
    shortfall is diagnosable.
    """
    parsed, detail = fetch(session)
    metrics = {"tickers": len(parsed)}
    if parsed:
        metrics["sample"] = ",".join(list(parsed)[:8])
    if len(parsed) >= MIN_EXPECTED_TICKERS:
        return True, detail, metrics
    return False, (f"parsed only {len(parsed)} ticker(s), expected at least "
                   f"{MIN_EXPECTED_TICKERS} — the layout has probably changed "
                   f"({detail})"), metrics
