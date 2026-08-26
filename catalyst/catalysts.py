"""Merge catalysts from every source and grade their reliability.

v1 treated all catalysts as equivalent and ranked purely by date proximity.
That put a ClinicalTrials.gov *estimated* completion date -- sponsor-entered,
routinely slipped by quarters or years -- above a PDUFA date, which is a
statutory FDA deadline. The first live run produced 18 names that were all
SOFT, presented as if they were equally solid.

Tiers
-----
HARD  Regulatory deadline set by the FDA (PDUFA / target action date).
      Slips are newsworthy and rare.
FIRM  Company-guided timing in a filing or on an earnings call
      ("topline data expected in Q4"). Usually honoured, sometimes slips.
SOFT  Inferred from a CT.gov estimated completion date. Weakest signal.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Iterable, Optional

from .periods import Period

TIER_ORDER = {"HARD": 0, "FIRM": 1, "SOFT": 2}
TIER_LABEL = {
    "HARD": "HARD (FDA deadline)",
    "FIRM": "FIRM (company guidance)",
    "SOFT": "SOFT (CT.gov estimate)",
}


@dataclass
class CatalystSet:
    ticker: str
    items: list[dict] = field(default_factory=list)

    def add(self, item: dict) -> None:
        self.items.append(item)

    @property
    def sources(self) -> list[str]:
        return sorted({i["source"] for i in self.items})

    @property
    def best_tier(self) -> Optional[str]:
        if not self.items:
            return None
        return min((i["tier"] for i in self.items), key=lambda t: TIER_ORDER[t])

    @property
    def best_period(self) -> Optional[Period]:
        """Earliest period among the items sharing the best tier."""
        tier = self.best_tier
        if tier is None:
            return None
        periods = [i["period"] for i in self.items if i["tier"] == tier]
        return min(periods, key=lambda p: p.sort_key()) if periods else None

    @property
    def catalyst_type(self) -> str:
        kinds: list[str] = []
        for i in self.items:
            if i["source"] == "pdufa.bio":
                kinds.append("PDUFA")
            elif i["source"] == "EDGAR":
                kw = i.get("keyword", "").lower()
                kinds.append("PDUFA" if "pdufa" in kw or "action date" in kw
                             else "Topline data")
            elif i["source"] == "CT.gov":
                kinds.append("Ph3 completion")
        seen: list[str] = []
        for k in kinds:
            if k not in seen:
                seen.append(k)
        return " / ".join(seen)

    def detail(self, limit: int = 4) -> str:
        parts: list[str] = []
        ordered = sorted(self.items,
                         key=lambda i: (TIER_ORDER[i["tier"]], i["period"].sort_key()))
        for i in ordered[:limit]:
            period = i["period"].display()
            if i["source"] == "CT.gov":
                dt = i.get("date_type", "")
                parts.append(f"CT.gov {i.get('nct_id','')} PCD {period}"
                             f"{' ['+dt+']' if dt else ''} {i.get('title','')[:70]}")
            elif i["source"] == "EDGAR":
                parts.append(f"EDGAR '{i.get('keyword','')}' -> {period} "
                             f"(filed {i.get('filed','?')})")
            elif i["source"] == "pdufa.bio":
                parts.append(f"pdufa.bio {period}: {i.get('raw','')[:100]}")
        return " || ".join(parts)

    def sort_key(self, *, by: str = "tier-then-date") -> tuple:
        period = self.best_period
        if period is None:
            return (99, 10 ** 9, 10 ** 9)      # undated sorts last
        if by == "date":
            return (period.start.toordinal(), period.span_days,
                    TIER_ORDER[self.best_tier])
        return (TIER_ORDER[self.best_tier], period.start.toordinal(),
                period.span_days)


def rank(sets: Iterable[CatalystSet], *, by: str = "tier-then-date"
         ) -> list[CatalystSet]:
    """Order survivors.

    Default is tier first, then proximity. Ranking by proximity alone (the v1
    behaviour, available as by="date") systematically promotes soft CT.gov
    estimates above hard FDA deadlines simply because an estimate happens to
    land earlier in the quarter.
    """
    return sorted([s for s in sets if s.items], key=lambda s: s.sort_key(by=by))
