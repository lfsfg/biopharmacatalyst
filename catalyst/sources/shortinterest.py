"""Second, independent source of short-interest data.

v1 had a single point of failure: if Finviz changed a column name, short
float -- the field that DEFINES this screen -- silently became empty for
every row. That is exactly what happened on the first live run.

yfinance is already a dependency of this repo and exposes short interest
from a different pipeline, so it serves two purposes: a fallback when Finviz
omits the field, and a cross-check when both are present. A large
disagreement between them is itself a signal worth surfacing.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Optional

log = logging.getLogger(__name__)


@dataclass
class ShortData:
    short_float_pct: Optional[float] = None
    days_to_cover: Optional[float] = None
    float_shares: Optional[float] = None
    shares_short: Optional[float] = None
    avg_volume: Optional[float] = None
    as_of: str = ""
    source: str = ""

    @property
    def usable(self) -> bool:
        return self.short_float_pct is not None


def _first_number(info: dict, *keys: str) -> Optional[float]:
    for key in keys:
        value = info.get(key)
        if isinstance(value, (int, float)) and value == value:   # not NaN
            return float(value)
    return None


def fetch(ticker: str) -> ShortData:
    """Best-effort short-interest lookup. Never raises."""
    try:
        import yfinance as yf
    except ImportError:
        log.debug("yfinance not installed; short-interest fallback unavailable")
        return ShortData()

    try:
        info = yf.Ticker(ticker).info or {}
    except Exception as exc:                      # yfinance raises broadly
        log.debug("yfinance lookup failed for %s: %s", ticker, exc)
        return ShortData()

    pct = _first_number(info, "shortPercentOfFloat")
    if pct is not None and pct <= 1.5:
        pct *= 100.0                              # sometimes a fraction

    if pct is None:
        shares_short = _first_number(info, "sharesShort")
        float_shares = _first_number(info, "floatShares")
        if shares_short and float_shares:
            pct = 100.0 * shares_short / float_shares

    as_of = info.get("dateShortInterest") or ""
    return ShortData(
        short_float_pct=pct,
        days_to_cover=_first_number(info, "shortRatio"),
        float_shares=_first_number(info, "floatShares"),
        shares_short=_first_number(info, "sharesShort"),
        avg_volume=_first_number(info, "averageVolume", "averageDailyVolume10Day"),
        as_of=str(as_of),
        source="yfinance",
    )


def reconcile(primary: Optional[float], secondary: Optional[float], *,
              tolerance_pp: float = 5.0) -> tuple[Optional[float], str, str]:
    """Combine two short-float readings.

    Returns (value, source_label, note). Prefers the primary reading, falls
    back to the secondary, and flags material disagreement instead of hiding
    it behind whichever number happened to arrive first.
    """
    if primary is not None and secondary is not None:
        gap = abs(primary - secondary)
        if gap > tolerance_pp:
            return (primary, "finviz",
                    f"disagrees with yfinance by {gap:.1f}pp "
                    f"(finviz {primary:.1f}%, yfinance {secondary:.1f}%)")
        return primary, "finviz", ""
    if primary is not None:
        return primary, "finviz", "yfinance unavailable"
    if secondary is not None:
        return secondary, "yfinance", "finviz did not supply short float"
    return None, "none", "no short-float data from any source"
