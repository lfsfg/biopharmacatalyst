"""Quarter-over-quarter comparison.

v1 produced standalone snapshots, so the questions that matter most over
time were unanswerable: which names carried over, did a catalyst date slip,
is short interest building or unwinding? Outputs are versioned in git, so
the previous run is always available -- it just was not being read.
"""

from __future__ import annotations

import csv
import glob
import os
import re
from typing import Optional

FILENAME_RE = re.compile(r"catalyst_candidates_(\d{4}-\d{2}-\d{2})\.csv$")


def find_previous(output_dir: str, current_date: str) -> Optional[str]:
    """Newest candidates CSV strictly older than `current_date`."""
    dated: list[tuple[str, str]] = []
    for path in glob.glob(os.path.join(output_dir, "catalyst_candidates_*.csv")):
        m = FILENAME_RE.search(os.path.basename(path))
        if m and m.group(1) < current_date:
            dated.append((m.group(1), path))
    if not dated:
        return None
    return max(dated)[1]


def load_rows(path: str) -> dict[str, dict]:
    try:
        with open(path, newline="", encoding="utf-8") as fh:
            return {r["ticker"]: r for r in csv.DictReader(fh) if r.get("ticker")}
    except (OSError, csv.Error):
        return {}


def _to_float(value) -> Optional[float]:
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def annotate(rows: list[dict], previous_path: Optional[str]) -> dict:
    """Add carry-over, date-slip and short-interest-change columns in place."""
    summary = {"previous_file": previous_path or "", "carried_over": 0,
               "new_names": 0, "dropped": 0, "dates_slipped": 0}
    if not previous_path:
        for r in rows:
            r.update(prev_seen="", date_moved="", short_float_change="")
        return summary

    prev = load_rows(previous_path)
    current_tickers = set()

    for r in rows:
        ticker = r["ticker"]
        current_tickers.add(ticker)
        old = prev.get(ticker)
        if not old:
            r.update(prev_seen="new", date_moved="", short_float_change="")
            summary["new_names"] += 1
            continue

        summary["carried_over"] += 1
        r["prev_seen"] = "carried over"

        old_date = (old.get("catalyst_date") or "").strip()
        new_date = (r.get("catalyst_date") or "").strip()
        if old_date and new_date and old_date != new_date:
            r["date_moved"] = f"{old_date} -> {new_date}"
            summary["dates_slipped"] += 1
        else:
            r["date_moved"] = ""

        old_sf = _to_float(old.get("short_float_pct"))
        new_sf = _to_float(r.get("short_float_pct"))
        if old_sf is not None and new_sf is not None:
            delta = new_sf - old_sf
            r["short_float_change"] = f"{delta:+.1f}pp"
        else:
            r["short_float_change"] = ""

    summary["dropped"] = len(set(prev) - current_tickers)
    return summary
