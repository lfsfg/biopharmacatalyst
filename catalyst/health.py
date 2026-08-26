"""Per-source health contract.

v1 had exactly one guard: "did the universe come back empty?" Its first real
run passed that guard while short float was missing on all 213 names, one of
three Finviz industries returned nothing, and two of four catalyst sources
contributed zero rows. The job went green.

The lesson: a screener must assert what each source is *supposed* to deliver,
not merely that something came back. Every source registers a Health record;
the run fails if any required source is FAILED, and reports loudly when one
is DEGRADED.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum

__all__ = ["Status", "Health", "HealthRegistry"]


class Status(str, Enum):
    OK = "OK"
    DEGRADED = "DEGRADED"    # partial data; run continues, flagged in the report
    FAILED = "FAILED"        # contract violated; run exits non-zero
    SKIPPED = "SKIPPED"      # deliberately not run


@dataclass
class Health:
    name: str
    status: Status = Status.OK
    required: bool = False
    detail: str = ""
    metrics: dict = field(default_factory=dict)

    def __str__(self) -> str:
        bits = " ".join(f"{k}={v}" for k, v in self.metrics.items())
        line = f"[{self.status.value:<8}] {self.name:<22} {bits}"
        return f"{line}  -- {self.detail}" if self.detail else line


class HealthRegistry:
    def __init__(self) -> None:
        self._items: dict[str, Health] = {}

    def record(self, name: str, *, status: Status = Status.OK, required: bool = False,
               detail: str = "", **metrics) -> Health:
        h = Health(name=name, status=status, required=required,
                   detail=detail, metrics=metrics)
        self._items[name] = h
        return h

    def add(self, health: Health) -> Health:
        """Register a pre-built Health record (e.g. from assess_coverage)."""
        self._items[health.name] = health
        return health

    def get(self, name: str) -> Health | None:
        return self._items.get(name)

    @property
    def all(self) -> list[Health]:
        return list(self._items.values())

    @property
    def failures(self) -> list[Health]:
        return [h for h in self._items.values()
                if h.required and h.status is Status.FAILED]

    @property
    def degraded(self) -> list[Health]:
        return [h for h in self._items.values() if h.status is Status.DEGRADED]

    def should_abort(self) -> bool:
        return bool(self.failures)

    def summary(self) -> str:
        lines = ["Source health", "-" * 64]
        lines += [str(h) for h in self.all]
        if self.failures:
            lines += ["", "REQUIRED SOURCES FAILED:"]
            lines += [f"  - {h.name}: {h.detail}" for h in self.failures]
        return "\n".join(lines)


# --------------------------------------------------------------------------
# Contract helpers
# --------------------------------------------------------------------------

def assess_coverage(name: str, present: int, total: int, *,
                    required: bool, min_ratio: float,
                    what: str) -> Health:
    """Grade a field by how much of the universe it actually covered.

    This is the check that would have caught v1's empty short-float column:
    the universe was full, the field was empty, and nothing complained.
    """
    ratio = (present / total) if total else 0.0
    if total == 0:
        status, detail = Status.FAILED, "no rows to cover"
    elif ratio == 0.0:
        status = Status.FAILED
        detail = (f"{what} missing for ALL {total} rows — the source's column "
                  f"name or layout has almost certainly changed")
    elif ratio < min_ratio:
        status = Status.DEGRADED
        detail = f"{what} present for only {ratio:.0%} of rows (want >={min_ratio:.0%})"
    else:
        status, detail = Status.OK, ""
    return Health(name=name, status=status, required=required, detail=detail,
                  metrics={"present": present, "total": total, "coverage": f"{ratio:.0%}"})
