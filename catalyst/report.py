"""Output writers: candidates CSV, human summary, and a run manifest."""

from __future__ import annotations

import csv
import json
from datetime import datetime
from pathlib import Path
from typing import Optional

import pytz

ET_TZ = pytz.timezone("America/New_York")

CSV_COLUMNS = [
    "rank", "research_tier", "confidence", "ticker", "company_name", "industry",
    "market_cap", "short_float_pct", "short_float_source", "short_float_note",
    "days_to_cover", "float_shares", "avg_volume",
    "catalyst_type", "catalyst_date", "catalyst_window_start",
    "catalyst_window_end", "catalyst_sources", "catalyst_detail",
    "cash_usd", "quarterly_burn_usd", "runway_quarters", "dilution_risk",
    "prev_seen", "date_moved", "short_float_change",
]


def write_csv(rows: list[dict], path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", newline="", encoding="utf-8") as fh:
        writer = csv.DictWriter(fh, fieldnames=CSV_COLUMNS, extrasaction="ignore")
        writer.writeheader()
        writer.writerows(rows)


def write_manifest(path: Path, *, run_date: str, target_label: str,
                   target_start: str, target_end: str, universe: int,
                   survivors: int, researched: int, status: str,
                   health: list, diff_summary: dict, rank_by: str) -> None:
    """Machine-readable run status.

    Stage B reads this instead of guessing from filenames whether Stage A
    actually succeeded.
    """
    payload = {
        "schema": 2,
        "run_date": run_date,
        "status": status,
        "target_quarter": {"label": target_label,
                           "start": target_start, "end": target_end},
        "counts": {"universe": universe, "survivors": survivors,
                   "researched": researched},
        "rank_by": rank_by,
        "sources": [
            {"name": h.name, "status": h.status.value, "required": h.required,
             "detail": h.detail, "metrics": h.metrics}
            for h in health
        ],
        "quarter_over_quarter": diff_summary,
    }
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")


def write_summary(path: Path, *, run_date: str, target_label: str,
                  target_start: str, target_end: str, universe: int,
                  rows: list[dict], health_summary: str, status: str,
                  diff_summary: dict, rank_by: str,
                  notes: Optional[list[str]] = None) -> None:
    now_et = datetime.now(ET_TZ)
    researched = [r for r in rows if r.get("research_tier") == "FULL"]
    flagged = [r for r in rows if r.get("research_tier") != "FULL"]

    lines = [
        "Quarterly Biotech Catalyst Screener v2 — Stage A summary",
        "=" * 72,
        f"Run timestamp (ET):     {now_et:%Y-%m-%d %H:%M:%S %Z}",
        f"Target catalyst window: {target_label}  ({target_start} .. {target_end})",
        f"Overall status:         {status}",
        f"Ranking:                {rank_by}",
        "",
        f"Finviz universe size:        {universe}",
        f"Survived catalyst check:     {len(rows)}",
        f"  full research (top 20):    {len(researched)}",
        f"  flagged-but-unresearched:  {len(flagged)}",
        "",
    ]

    if diff_summary.get("previous_file"):
        lines += [
            "Versus previous run "
            f"({Path(diff_summary['previous_file']).name}):",
            f"  carried over:  {diff_summary.get('carried_over', 0)}",
            f"  new names:     {diff_summary.get('new_names', 0)}",
            f"  dropped off:   {diff_summary.get('dropped', 0)}",
            f"  dates moved:   {diff_summary.get('dates_slipped', 0)}",
            "",
        ]

    if rows:
        lines.append("Candidates (rank. ticker  tier  date  sources):")
        for r in rows:
            lines.append(
                f"  {r['rank']:>3}. {r['ticker']:<6} "
                f"{(r.get('confidence') or '?'):<5} "
                f"{(r.get('catalyst_date') or 'undated'):<12} "
                f"{r.get('catalyst_sources', '')}"
                + (f"   [{r['date_moved']}]" if r.get("date_moved") else "")
            )
        lines.append("")

    lines.append(health_summary)
    lines.append("")

    if notes:
        lines.append(f"Notes ({len(notes)}):")
        lines += [f"  - {n}" for n in notes]

    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
