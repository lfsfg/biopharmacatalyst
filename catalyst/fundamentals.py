"""Cash runway and dilution risk from SEC XBRL company facts.

Missing from v1 and material to this specific strategy: a clinical-stage
biotech with two quarters of cash will very likely raise equity into a
positive readout, which caps the upside the entire thesis rests on. A short
squeeze also becomes far less likely when the company can print shares into
it. Runway therefore belongs next to short float, not in a footnote.

All figures are best-effort from public filings; absence is reported as
unknown rather than guessed.
"""

from __future__ import annotations

import logging
from typing import Optional

import requests

CONCEPT_URL = "https://data.sec.gov/api/xbrl/companyconcept/CIK{cik}/us-gaap/{tag}.json"
TIMEOUT = 30

CASH_TAGS = [
    "CashAndCashEquivalentsAtCarryingValue",
    "CashCashEquivalentsRestrictedCashAndRestrictedCashEquivalents",
]
BURN_TAGS = ["NetCashProvidedByUsedInOperatingActivities"]

log = logging.getLogger(__name__)


def _latest_value(session: requests.Session, cik: str, tag: str,
                  headers: dict, *, want_quarterly: bool = False) -> Optional[dict]:
    try:
        resp = session.get(CONCEPT_URL.format(cik=cik, tag=tag),
                           headers=headers, timeout=TIMEOUT)
    except requests.RequestException:
        return None
    if resp.status_code != 200:
        return None
    try:
        units = resp.json().get("units", {}).get("USD", [])
    except ValueError:
        return None
    if not units:
        return None

    rows = [u for u in units if u.get("end")]
    if want_quarterly:
        # ~one quarter of elapsed time between start and end.
        def is_quarterly(u: dict) -> bool:
            s, e = u.get("start"), u.get("end")
            if not (s and e):
                return False
            from datetime import date as _d
            try:
                days = (_d.fromisoformat(e) - _d.fromisoformat(s)).days
            except ValueError:
                return False
            return 60 <= days <= 120
        quarterly = [u for u in rows if is_quarterly(u)]
        rows = quarterly or rows

    rows.sort(key=lambda u: u.get("end", ""))
    return rows[-1] if rows else None


def cash_runway(session: requests.Session, cik: str,
                headers: dict) -> dict:
    """Return cash, quarterly burn, runway in quarters, and a risk label."""
    result = {
        "cash_usd": None,
        "quarterly_burn_usd": None,
        "runway_quarters": None,
        "dilution_risk": "unknown",
        "as_of": "",
    }
    if not cik:
        return result

    cash_row = None
    for tag in CASH_TAGS:
        cash_row = _latest_value(session, cik, tag, headers)
        if cash_row:
            break
    if not cash_row:
        return result

    result["cash_usd"] = cash_row.get("val")
    result["as_of"] = cash_row.get("end", "")

    burn_row = None
    for tag in BURN_TAGS:
        burn_row = _latest_value(session, cik, tag, headers, want_quarterly=True)
        if burn_row:
            break
    if not burn_row:
        return result

    burn = burn_row.get("val")
    if burn is None:
        return result

    # Operating cash flow is negative for a company burning cash.
    quarterly_burn = abs(burn) if burn < 0 else 0.0
    result["quarterly_burn_usd"] = quarterly_burn

    if quarterly_burn > 0 and result["cash_usd"]:
        runway = result["cash_usd"] / quarterly_burn
        result["runway_quarters"] = round(runway, 1)
        if runway < 2:
            result["dilution_risk"] = "HIGH"
        elif runway < 4:
            result["dilution_risk"] = "MEDIUM"
        else:
            result["dilution_risk"] = "LOW"
    elif quarterly_burn == 0:
        result["dilution_risk"] = "LOW"       # cash-flow positive
    return result
