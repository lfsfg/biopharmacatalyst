"""SEC EDGAR full-text search, with dates actually extracted.

v1's flaw was conceptual, not just technical: a keyword hit was treated as a
catalyst. But "this company said PDUFA in a recent 8-K" is not "this company
has a catalyst next quarter" -- the filing might be discussing a date two
years out, or one already passed. v1 therefore recorded EDGAR survivors with
an EMPTY date, which quietly violates the "verifiable next-quarter catalyst"
requirement it was built to enforce.

v2 fetches the matched documents and extracts a date expression near the
catalyst keyword, keeping the hit only when that date overlaps the target
quarter.

v1's live run also returned zero EDGAR hits across 1,278 requests with no
HTTP errors -- silently empty. probe() exists to make that state loud: it
asks a question that MUST return hits, so an empty result proves the
integration is broken rather than the universe being quiet.
"""

from __future__ import annotations

import logging
import os
import re
import time
from datetime import date, timedelta
from typing import Optional

import requests

from ..periods import DATE_EXPR, Period, parse_period

FTS_URL = "https://efts.sec.gov/LATEST/search-index"
ARCHIVE_URL = "https://www.sec.gov/Archives/edgar/data"
TICKER_MAP_URL = "https://www.sec.gov/files/company_tickers.json"

FORMS = "8-K,10-Q"
LOOKBACK_DAYS = 150
TIMEOUT = 30
DELAY = 0.15          # SEC asks for <=10 req/s; this is ~6/s

# Keywords that mark a dated, binary catalyst. Bare "BLA"/"NDA" were dropped
# from v1's list -- they appear in routine boilerplate and generated noise
# without ever contributing a date.
CATALYST_KEYWORDS = {
    "PDUFA": "HARD",
    "target action date": "HARD",
    "prescription drug user fee act": "HARD",
    "action date": "HARD",
    "topline": "FIRM",
    "top-line": "FIRM",
    "primary endpoint data": "FIRM",
    "readout": "FIRM",
    "data readout": "FIRM",
    "interim analysis": "FIRM",
}

# How far either side of a keyword to look for a date expression.
CONTEXT_CHARS = 320

log = logging.getLogger(__name__)


class SecUserAgentMissing(RuntimeError):
    pass


def headers() -> dict:
    """SEC requires a real contact address on every request.

    v1 fell back to a fabricated example address, which violates SEC fair
    access and risks an IP ban for the whole runner. Missing config is now a
    hard error rather than a quiet forgery.
    """
    ua = os.environ.get("SEC_USER_AGENT", "").strip()
    if not ua or "example.com" in ua.lower():
        raise SecUserAgentMissing(
            "SEC_USER_AGENT must be set to a real contact string, e.g. "
            "'Jane Doe jane@company.com'. SEC blocks clients that do not "
            "identify themselves."
        )
    return {"User-Agent": ua, "Accept": "application/json",
            "Accept-Encoding": "gzip, deflate"}


def load_cik_map(session: requests.Session) -> dict[str, str]:
    """ticker -> 10-digit zero-padded CIK. Doubles as a ticker validator."""
    resp = session.get(TICKER_MAP_URL, headers=headers(), timeout=TIMEOUT)
    resp.raise_for_status()
    return {str(e["ticker"]).upper(): str(e["cik_str"]).zfill(10)
            for e in resp.json().values()}


def _extract_total(payload: dict) -> int:
    """Read the hit count across the response shapes EDGAR has used."""
    hits = payload.get("hits", {})
    total = hits.get("total")
    if isinstance(total, dict):
        return int(total.get("value", 0))
    if isinstance(total, int):
        return total
    return len(hits.get("hits", []) or [])


def search(session: requests.Session, phrase: str, *,
           cik: Optional[str] = None,
           start: Optional[date] = None,
           end: Optional[date] = None,
           forms: str = FORMS) -> list[dict]:
    """Full-text search. Returns raw hit dicts (possibly empty)."""
    params = {"q": f'"{phrase}"', "forms": forms}
    if cik:
        params["ciks"] = cik
    if start and end:
        params.update({"dateRange": "custom",
                       "startdt": start.isoformat(),
                       "enddt": end.isoformat()})
    try:
        resp = session.get(FTS_URL, params=params, headers=headers(), timeout=TIMEOUT)
    except requests.RequestException as exc:
        log.warning("EDGAR FTS request failed (%s): %s", phrase, exc)
        return []
    if resp.status_code != 200:
        log.warning("EDGAR FTS HTTP %d (%s)", resp.status_code, phrase)
        return []
    try:
        payload = resp.json()
    except ValueError:
        log.warning("EDGAR FTS returned non-JSON for %s", phrase)
        return []
    if _extract_total(payload) == 0:
        return []
    return payload.get("hits", {}).get("hits", []) or []


def probe(session: requests.Session) -> tuple[bool, str]:
    """Ask a question that must have hits, to prove the integration works.

    'PDUFA' across all 8-Ks in the last 150 days is guaranteed non-empty in
    any normal market. Zero hits means the endpoint, params or response shape
    changed -- not that nobody mentioned PDUFA.
    """
    today = date.today()
    hits = search(session, "PDUFA", start=today - timedelta(days=LOOKBACK_DAYS),
                  end=today, forms="8-K")
    if hits:
        return True, f"probe returned {len(hits)} hits"
    return False, ("probe for 'PDUFA' across all 8-K filings in the last "
                   f"{LOOKBACK_DAYS} days returned ZERO hits -- EDGAR full-text "
                   "search integration is broken (endpoint, params or response "
                   "shape), not merely quiet")


def _document_url(hit: dict) -> Optional[str]:
    """Build the archive URL for a search hit."""
    src = hit.get("_source", {})
    ident = hit.get("_id", "")
    ciks = src.get("ciks") or []
    if not ciks:
        return None
    cik_int = str(ciks[0]).lstrip("0") or "0"
    if ":" in ident:
        adsh, filename = ident.split(":", 1)
    else:
        adsh, filename = src.get("adsh", ""), ""
    if not adsh:
        return None
    accession = adsh.replace("-", "")
    if not filename:
        return f"{ARCHIVE_URL}/{cik_int}/{accession}.txt"
    return f"{ARCHIVE_URL}/{cik_int}/{accession}/{filename}"


def fetch_text(session: requests.Session, url: str) -> str:
    try:
        resp = session.get(url, headers={**headers(), "Accept": "*/*"},
                           timeout=TIMEOUT)
    except requests.RequestException as exc:
        log.debug("EDGAR document fetch failed %s: %s", url, exc)
        return ""
    if resp.status_code != 200:
        return ""
    text = resp.text
    text = re.sub(r"<[^>]+>", " ", text)          # strip tags
    text = re.sub(r"&[a-z]+;", " ", text)
    return re.sub(r"\s+", " ", text)


def extract_dated_catalysts(text: str, target: Period) -> list[dict]:
    """Find catalyst keywords sitting near a date that hits the target window."""
    found: list[dict] = []
    seen: set[tuple] = set()
    low = text.lower()

    for keyword, tier in CATALYST_KEYWORDS.items():
        for m in re.finditer(re.escape(keyword.lower()), low):
            lo = max(0, m.start() - CONTEXT_CHARS)
            hi = min(len(text), m.end() + CONTEXT_CHARS)
            window = text[lo:hi]
            for dm in DATE_EXPR.finditer(window):
                period = parse_period(dm.group(1))
                if not period or not period.overlaps(target):
                    continue
                key = (keyword, period.start, period.end)
                if key in seen:
                    continue
                seen.add(key)
                found.append({
                    "keyword": keyword,
                    "tier": tier,
                    "period": period,
                    "snippet": re.sub(r"\s+", " ", window).strip()[:280],
                })
    return found


def catalysts_for(session: requests.Session, cik: str, target: Period, *,
                  max_docs: int = 3) -> list[dict]:
    """Dated catalysts for one company, from its recent filings."""
    today = date.today()
    start = today - timedelta(days=LOOKBACK_DAYS)

    hits: list[dict] = []
    for phrase in ("PDUFA", "topline", "target action date"):
        hits.extend(search(session, phrase, cik=cik, start=start, end=today))
        time.sleep(DELAY)
        if len(hits) >= max_docs:
            break
    if not hits:
        return []

    results: list[dict] = []
    for hit in hits[:max_docs]:
        url = _document_url(hit)
        if not url:
            continue
        text = fetch_text(session, url)
        time.sleep(DELAY)
        if not text:
            continue
        for item in extract_dated_catalysts(text, target):
            item["source"] = "EDGAR"
            item["filed"] = hit.get("_source", {}).get("file_date", "")
            item["url"] = url
            results.append(item)
    return results
