"""Date-range semantics for catalyst timing.

Catalyst dates are rarely exact. ClinicalTrials.gov returns partial dates
("2026-10"); companies guide to "Q4 2026", "the second half of 2026",
"mid-2026". v1 stored these as strings and compared them lexically, which
silently conflated a precise day with a six-month window.

Everything here resolves to a closed [start, end] interval plus a precision
label, so "is this catalyst in the target quarter?" becomes an interval
overlap test instead of a string compare.
"""

from __future__ import annotations

import calendar
import re
from dataclasses import dataclass
from datetime import date, datetime
from typing import Optional

__all__ = ["Period", "quarter_bounds", "next_quarter_window",
           "parse_period", "find_period", "DATE_EXPR"]


def _month_end(year: int, month: int) -> date:
    return date(year, month, calendar.monthrange(year, month)[1])


@dataclass(frozen=True)
class Period:
    """A closed date interval with a precision label."""

    start: date
    end: date
    precision: str          # day | month | quarter | half | year | fuzzy
    raw: str = ""

    def overlaps(self, other: "Period") -> bool:
        return self.start <= other.end and other.start <= self.end

    def contained_by(self, other: "Period") -> bool:
        return other.start <= self.start and self.end <= other.end

    @property
    def span_days(self) -> int:
        return (self.end - self.start).days + 1

    def display(self) -> str:
        if self.precision == "day":
            return self.start.isoformat()
        if self.precision == "month":
            return f"{self.start:%Y-%m}"
        if self.precision == "quarter":
            return f"Q{(self.start.month - 1) // 3 + 1} {self.start.year}"
        if self.precision == "half":
            half = 1 if self.start.month == 1 else 2
            return f"H{half} {self.start.year}"
        if self.precision == "year":
            return str(self.start.year)
        return f"{self.start.isoformat()}..{self.end.isoformat()}"

    def sort_key(self) -> tuple:
        """Earliest-first, and for equal starts the tighter window first."""
        return (self.start, self.span_days)


def quarter_bounds(year: int, q: int) -> Period:
    start_month = 3 * (q - 1) + 1
    return Period(
        date(year, start_month, 1),
        _month_end(year, start_month + 2),
        "quarter",
        f"Q{q} {year}",
    )


def next_quarter_window(today: date) -> tuple[Period, str]:
    """The calendar quarter AFTER today's, plus its label."""
    q = (today.month - 1) // 3 + 1
    year, nq = (today.year + 1, 1) if q == 4 else (today.year, q + 1)
    period = quarter_bounds(year, nq)
    return period, f"Q{nq} {year}"


# --------------------------------------------------------------------------
# Parsing
# --------------------------------------------------------------------------

_ORDINAL_Q = {
    "first": 1, "1st": 1, "second": 2, "2nd": 2,
    "third": 3, "3rd": 3, "fourth": 4, "4th": 4,
}
_ORDINAL_H = {"first": 1, "1st": 1, "second": 2, "2nd": 2}

_EXPLICIT_FORMATS = (
    ("%Y-%m-%d", "day"),
    ("%B %d, %Y", "day"),
    ("%b %d, %Y", "day"),
    ("%B %d %Y", "day"),
    ("%b %d %Y", "day"),
    ("%m/%d/%Y", "day"),
    ("%d %B %Y", "day"),
    ("%d %b %Y", "day"),
    ("%Y-%m", "month"),
    ("%B %Y", "month"),
    ("%b %Y", "month"),
)


def _try_explicit(text: str) -> Optional[Period]:
    for fmt, precision in _EXPLICIT_FORMATS:
        try:
            dt = datetime.strptime(text, fmt).date()
        except ValueError:
            continue
        if precision == "day":
            return Period(dt, dt, "day", text)
        return Period(dt.replace(day=1), _month_end(dt.year, dt.month), "month", text)
    return None


def _year_from(match_year: str) -> int:
    y = int(match_year)
    return y + 2000 if y < 100 else y


def parse_period(text: str, *, default_year: Optional[int] = None) -> Optional[Period]:
    """Parse a date or vague timing expression into a Period.

    Handles exact dates, partial dates, quarters, halves and the fuzzy
    guidance language companies actually use on earnings calls.
    Returns None when nothing date-like is present.
    """
    if not text:
        return None
    raw = text.strip()
    if not raw:
        return None

    exact = _try_explicit(raw)
    if exact:
        return exact

    low = raw.lower()

    # "Q4 2026" / "4Q26" / "Q4-2026"
    m = re.search(r"\bq([1-4])[\s\-/]*(\d{2,4})\b", low) or \
        re.search(r"\b([1-4])q[\s\-/]*(\d{2,4})\b", low)
    if m:
        return quarter_bounds(_year_from(m.group(2)), int(m.group(1)))

    # "fourth quarter of 2026"
    m = re.search(r"\b(first|second|third|fourth|1st|2nd|3rd|4th)\s+quarter\b"
                  r"(?:\s+of)?\s*(\d{4})?", low)
    if m:
        q = _ORDINAL_Q[m.group(1)]
        year = int(m.group(2)) if m.group(2) else default_year
        if year:
            return quarter_bounds(year, q)

    # "first half of 2026" / "2H26"
    m = re.search(r"\b(first|second|1st|2nd)\s+half\b(?:\s+of)?\s*(\d{4})?", low)
    if m:
        half = _ORDINAL_H[m.group(1)]
        year = int(m.group(2)) if m.group(2) else default_year
        if year:
            start = date(year, 1 if half == 1 else 7, 1)
            return Period(start, _month_end(year, 6 if half == 1 else 12), "half", raw)
    m = re.search(r"\b([12])h[\s\-/]*(\d{2,4})\b", low)
    if m:
        half, year = int(m.group(1)), _year_from(m.group(2))
        start = date(year, 1 if half == 1 else 7, 1)
        return Period(start, _month_end(year, 6 if half == 1 else 12), "half", raw)

    # Fuzzy guidance: "mid-2026", "early 2026", "late 2026", "year-end 2026"
    fuzzy = (
        (r"\bmid[\s\-]?(\d{4})\b", 4, 9),
        (r"\bearly\s+(\d{4})\b", 1, 4),
        (r"\b(?:late|end\s+of)\s+(\d{4})\b", 9, 12),
        (r"\byear[\s\-]?end\s*(\d{4})\b", 10, 12),
    )
    for pattern, m1, m2 in fuzzy:
        m = re.search(pattern, low)
        if m:
            year = int(m.group(1))
            return Period(date(year, m1, 1), _month_end(year, m2), "fuzzy", raw)

    # Bare year, last resort.
    m = re.fullmatch(r"\D*(\d{4})\D*", low)
    if m:
        year = int(m.group(1))
        if 2000 <= year <= 2100:
            return Period(date(year, 1, 1), date(year, 12, 31), "year", raw)

    return None


# --------------------------------------------------------------------------
# Extraction from free text
# --------------------------------------------------------------------------

_MONTHS = ("January|February|March|April|May|June|July|August|September|"
           "October|November|December")

DATE_EXPR = re.compile(
    r"("
    rf"(?:{_MONTHS})\s+\d{{1,2}},?\s+\d{{4}}"
    rf"|(?:{_MONTHS})\s+\d{{4}}"
    r"|(?:first|second|third|fourth|1st|2nd|3rd|4th)\s+quarter(?:\s+of)?\s+\d{4}"
    r"|Q[1-4]\s*,?\s*\d{4}"
    r"|[1-4]Q\s*\d{2,4}"
    r"|(?:first|second|1st|2nd)\s+half(?:\s+of)?\s+\d{4}"
    r"|[12]H\s*\d{2,4}"
    r"|mid[\s\-]?\d{4}"
    r"|year[\s\-]?end\s+\d{4}"
    r"|(?:late|early|end\s+of)\s+\d{4}"
    r"|\d{4}-\d{2}-\d{2}"
    r"|\d{1,2}/\d{1,2}/\d{4}"
    r")",
    re.IGNORECASE,
)


def find_period(text: str, *, default_year: Optional[int] = None) -> Optional[Period]:
    """Find the first parseable date expression inside free text.

    parse_period() requires the whole string to be a date. Real sources embed
    dates in prose ("Acme Bio (ACME) - PDUFA November 14, 2026"), so callers
    scanning documents or calendar cards need this instead.
    """
    if not text:
        return None
    for m in DATE_EXPR.finditer(text):
        period = parse_period(m.group(1), default_year=default_year)
        if period:
            return period
    return None


def find_all_periods(text: str) -> list[Period]:
    """Every distinct parseable date expression in `text`, in order."""
    out: list[Period] = []
    seen: set[tuple] = set()
    for m in DATE_EXPR.finditer(text or ""):
        period = parse_period(m.group(1))
        if not period:
            continue
        key = (period.start, period.end)
        if key not in seen:
            seen.add(key)
            out.append(period)
    return out
