#!/usr/bin/env python3
"""
Quarterly Biotech Catalyst Screener — Stage A (v2).

Screens heavily-shorted US biotech / drug-manufacturer names for binary
catalysts landing in the NEXT calendar quarter, and emits a ranked candidate
CSV plus a machine-readable run manifest for Stage B.

What changed from v1, and why
-----------------------------
v1's first live run (2026-08-26) "succeeded" while being substantially
broken: every ticker was corrupted, short float was empty on all 213 names,
one of three industries silently returned nothing, and two of four catalyst
sources contributed zero rows. The job went green because the only guard
asked "is the universe empty?".

v2 therefore adds a per-source health contract (catalyst/health.py). Each
source declares what it must deliver; the run fails loudly when a required
one does not. Several sources also carry a probe() that asks a question with
a guaranteed non-empty answer, which distinguishes "this source is broken"
from "there genuinely are no matches" -- the ambiguity that hid v1's bugs.

Environment
-----------
GITHUB_REPO_PATH   repo root (defaults to this file's directory)
SEC_USER_AGENT     REQUIRED. "Name email@example.com" — SEC blocks anonymous
                   clients, and v1's fabricated fallback risked an IP ban.
SKIP_SHORT_FALLBACK  'true' to skip the yfinance cross-check (faster)
SKIP_FUNDAMENTALS    'true' to skip cash-runway lookups (faster)
RANK_BY            'tier-then-date' (default) or 'date'
ALLOW_DEGRADED     'true' to exit 0 even when a required source failed
"""

from __future__ import annotations

import argparse
import logging
import os
import sys
import time
from datetime import date
from pathlib import Path

import requests

from catalyst import fundamentals
from catalyst.catalysts import CatalystSet, TIER_LABEL, rank as rank_sets
from catalyst.diffing import annotate, find_previous
from catalyst.health import HealthRegistry, Status, assess_coverage
from catalyst.periods import next_quarter_window
from catalyst.report import write_csv, write_manifest, write_summary
from catalyst.sources import ctgov, edgar, finviz, pdufa, shortinterest

FULL_RESEARCH_LIMIT = 20
MIN_SHORT_FLOAT_COVERAGE = 0.80

log = logging.getLogger("catalyst_screener")


def setup_logging(path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fmt = logging.Formatter("%(asctime)s [%(levelname)s] %(name)s: %(message)s")
    root = logging.getLogger()
    root.setLevel(logging.INFO)
    root.handlers.clear()
    fh = logging.FileHandler(path, encoding="utf-8")
    fh.setFormatter(fmt)
    sh = logging.StreamHandler(sys.stdout)
    sh.setFormatter(fmt)
    root.addHandler(fh)
    root.addHandler(sh)


# --------------------------------------------------------------------------
# Stage 1 — universe
# --------------------------------------------------------------------------

def build_universe(session: requests.Session, health: HealthRegistry,
                   notes: list[str]) -> dict[str, dict]:
    universe: dict[str, dict] = {}
    industry_counts: dict[str, int] = {}

    for name, token in finviz.INDUSTRIES.items():
        filters = finviz.BASE_FILTERS + [token]
        overview = finviz.collect("overview", filters, session)
        time.sleep(finviz.DELAY)
        ownership = finviz.collect("ownership", filters, session)
        industry_counts[name] = len(overview)
        log.info("Finviz %-42s overview=%3d ownership=%3d",
                 name, len(overview), len(ownership))

        if not overview:
            # Disambiguate a bad token from a genuinely empty result.
            valid = finviz.probe_industry_token(token, session)
            if valid:
                notes.append(
                    f"Industry '{name}' returned 0 rows under the full filters, "
                    f"but token '{token}' is valid (it returns rows without the "
                    f"short-float filter) — so no company in it has >10% short "
                    f"float. This is a real result, not a bug.")
            else:
                notes.append(
                    f"Industry '{name}': token '{token}' returned 0 rows even "
                    f"without the short-float filter — the token is WRONG or "
                    f"Finviz is blocking. This industry is missing entirely.")
                health.record(f"finviz:{token}", status=Status.FAILED,
                              required=True,
                              detail=f"industry token '{token}' appears invalid",
                              rows=0)
            continue

        for ticker, ov in overview.items():
            own = ownership.get(ticker, {})
            if ticker in universe:
                universe[ticker]["industry"] += f" / {name}"
                continue
            merged = {**ov, **own}
            universe[ticker] = {
                "ticker": ticker,
                "company_name": finviz.resolve(ov, "company") or "",
                "industry": name,
                "market_cap": finviz.resolve(ov, "market_cap") or "",
                "short_float_finviz": finviz.to_number(
                    finviz.resolve(merged, "short_float")),
                "days_to_cover": finviz.to_number(
                    finviz.resolve(merged, "short_ratio")),
                "float_shares": finviz.to_number(
                    finviz.resolve(merged, "float_shares")),
                "avg_volume": finviz.to_number(
                    finviz.resolve(merged, "avg_volume")),
            }

    health.record("finviz:universe",
                  status=Status.OK if universe else Status.FAILED,
                  required=True,
                  detail="" if universe else "screener returned zero tickers",
                  tickers=len(universe),
                  industries=len([c for c in industry_counts.values() if c]))
    return universe


def validate_tickers(universe: dict[str, dict], cik_map: dict[str, str],
                     health: HealthRegistry, notes: list[str]) -> dict[str, dict]:
    """Drop symbols the SEC does not know.

    This is what catches v1's doubled tickers (BBHVN for BHVN) definitively,
    without guessing at string repairs that would corrupt real symbols.
    """
    if not cik_map:
        health.record("ticker:validation", status=Status.DEGRADED,
                      detail="SEC ticker map unavailable; symbols unvalidated",
                      checked=0)
        return universe

    good, bad = {}, []
    for ticker, rec in universe.items():
        if ticker in cik_map:
            rec["cik"] = cik_map[ticker]
            good[ticker] = rec
        else:
            bad.append(ticker)

    if bad:
        notes.append(
            f"{len(bad)} symbol(s) not found in the SEC ticker map and dropped: "
            f"{', '.join(sorted(bad)[:15])}"
            + (" ..." if len(bad) > 15 else ""))

    ratio = len(good) / len(universe) if universe else 0
    health.record(
        "ticker:validation",
        status=Status.OK if ratio >= 0.9 else
               (Status.DEGRADED if ratio >= 0.5 else Status.FAILED),
        required=True,
        detail="" if ratio >= 0.9 else
               f"only {ratio:.0%} of screener symbols are known to the SEC — "
               f"the ticker column is probably being parsed incorrectly",
        valid=len(good), dropped=len(bad))
    return good


def enrich_short_interest(universe: dict[str, dict], health: HealthRegistry,
                          notes: list[str], *, use_fallback: bool) -> None:
    total = len(universe)
    finviz_present = sum(1 for r in universe.values()
                         if r.get("short_float_finviz") is not None)
    health.add(assess_coverage("finviz:short_float", finviz_present, total,
                               required=False,
                               min_ratio=MIN_SHORT_FLOAT_COVERAGE,
                               what="short float"))

    if use_fallback and finviz_present < total:
        log.warning("Finviz short float covers %d/%d rows — using yfinance for "
                    "the remainder", finviz_present, total)

    resolved = 0
    for rec in universe.values():
        primary = rec.get("short_float_finviz")
        secondary = None
        if use_fallback and primary is None:
            data = shortinterest.fetch(rec["ticker"])
            secondary = data.short_float_pct
            for field in ("days_to_cover", "float_shares", "avg_volume"):
                if rec.get(field) is None:
                    rec[field] = getattr(data, field)

        value, source, note = shortinterest.reconcile(primary, secondary)
        rec["short_float_pct"] = value
        rec["short_float_source"] = source
        rec["short_float_note"] = note
        if value is not None:
            resolved += 1

    combined = health.add(
        assess_coverage("short_float:combined", resolved, total,
                        required=True, min_ratio=MIN_SHORT_FLOAT_COVERAGE,
                        what="short float (any source)"))
    if combined.status is not Status.OK:
        notes.append(
            "Short float is the filter that defines this screen. "
            f"{resolved}/{total} rows have it. "
            "v1 shipped a run where this was 0/213 and still passed.")


# --------------------------------------------------------------------------
# Stage 2 — catalysts
# --------------------------------------------------------------------------

def collect_catalysts(session: requests.Session, universe: dict[str, dict],
                      target, health: HealthRegistry, notes: list[str]
                      ) -> dict[str, CatalystSet]:
    sets: dict[str, CatalystSet] = {t: CatalystSet(t) for t in universe}

    # --- pdufa.bio (HARD) -------------------------------------------------
    pdufa_map, pdufa_detail = pdufa.fetch(session)
    health.record("pdufa.bio",
                  status=Status.OK if pdufa_map else Status.DEGRADED,
                  detail="" if pdufa_map else
                         f"parsed 0 rows ({pdufa_detail}) — layout likely changed",
                  tickers=len(pdufa_map))
    pdufa_hits = 0
    for ticker, cs in sets.items():
        for entry in pdufa_map.get(ticker, []):
            if entry["period"].overlaps(target):
                cs.add({"source": "pdufa.bio", "tier": pdufa.TIER,
                        "period": entry["period"], "raw": entry["raw"]})
                pdufa_hits += 1

    # --- EDGAR (HARD / FIRM) ---------------------------------------------
    edgar_ok, edgar_detail = edgar.probe(session)
    if not edgar_ok:
        health.record("edgar:fts", status=Status.FAILED, required=True,
                      detail=edgar_detail, hits=0)
        notes.append("EDGAR full-text search failed its probe; no EDGAR "
                     "catalysts were collected. " + edgar_detail)
    else:
        edgar_hits = 0
        for i, (ticker, rec) in enumerate(universe.items(), 1):
            if i % 25 == 0:
                log.info("EDGAR %d/%d", i, len(universe))
            for item in edgar.catalysts_for(session, rec.get("cik", ""), target):
                sets[ticker].add(item)
                edgar_hits += 1
        health.record("edgar:fts", status=Status.OK, required=True,
                      detail=edgar_detail, dated_hits=edgar_hits)

    # --- ClinicalTrials.gov (SOFT) ---------------------------------------
    ct_ok, ct_detail = ctgov.probe(session)
    if not ct_ok:
        health.record("clinicaltrials.gov", status=Status.FAILED, required=True,
                      detail=ct_detail, hits=0)
        notes.append("ClinicalTrials.gov failed its probe. " + ct_detail)
    else:
        ct_hits = 0
        for i, (ticker, rec) in enumerate(universe.items(), 1):
            if i % 25 == 0:
                log.info("CT.gov %d/%d", i, len(universe))
            for item in ctgov.phase3_completions(session, rec["company_name"], target):
                sets[ticker].add(item)
                ct_hits += 1
        health.record("clinicaltrials.gov", status=Status.OK, required=True,
                      detail=ct_detail, hits=ct_hits)

    health.record("pdufa.bio:matched", status=Status.OK, hits=pdufa_hits)

    contributing = [name for name, count in (
        ("pdufa.bio", pdufa_hits),
        ("EDGAR", sum(1 for s in sets.values()
                      for i in s.items if i["source"] == "EDGAR")),
        ("CT.gov", sum(1 for s in sets.values()
                       for i in s.items if i["source"] == "CT.gov")),
    ) if count]
    if len(contributing) <= 1:
        notes.append(
            f"Only {len(contributing)} of 3 catalyst sources contributed any "
            f"rows ({', '.join(contributing) or 'none'}). v1 shipped exactly "
            "this state — every survivor came from CT.gov alone — and reported "
            "success.")
    return sets


# --------------------------------------------------------------------------
# Assembly
# --------------------------------------------------------------------------

def build_rows(ranked: list[CatalystSet], universe: dict[str, dict],
               session: requests.Session, *, with_fundamentals: bool) -> list[dict]:
    rows: list[dict] = []
    for idx, cs in enumerate(ranked, 1):
        rec = universe[cs.ticker]
        period = cs.best_period
        row = {
            "rank": idx,
            "research_tier": "FULL" if idx <= FULL_RESEARCH_LIMIT else "FLAGGED_ONLY",
            "confidence": cs.best_tier or "",
            "ticker": cs.ticker,
            "company_name": rec.get("company_name", ""),
            "industry": rec.get("industry", ""),
            "market_cap": rec.get("market_cap", ""),
            "short_float_pct": ("" if rec.get("short_float_pct") is None
                                else round(rec["short_float_pct"], 2)),
            "short_float_source": rec.get("short_float_source", ""),
            "short_float_note": rec.get("short_float_note", ""),
            "days_to_cover": ("" if rec.get("days_to_cover") is None
                              else rec["days_to_cover"]),
            "float_shares": ("" if rec.get("float_shares") is None
                             else int(rec["float_shares"])),
            "avg_volume": ("" if rec.get("avg_volume") is None
                           else int(rec["avg_volume"])),
            "catalyst_type": cs.catalyst_type,
            "catalyst_date": period.display() if period else "",
            "catalyst_window_start": period.start.isoformat() if period else "",
            "catalyst_window_end": period.end.isoformat() if period else "",
            "catalyst_sources": "; ".join(cs.sources),
            "catalyst_detail": cs.detail()[:1500],
        }
        if with_fundamentals and idx <= FULL_RESEARCH_LIMIT:
            try:
                fin = fundamentals.cash_runway(session, rec.get("cik", ""),
                                               edgar.headers())
            except Exception as exc:            # never fail the run on this
                log.debug("runway lookup failed for %s: %s", cs.ticker, exc)
                fin = {}
            row.update({
                "cash_usd": fin.get("cash_usd") or "",
                "quarterly_burn_usd": fin.get("quarterly_burn_usd") or "",
                "runway_quarters": fin.get("runway_quarters") or "",
                "dilution_risk": fin.get("dilution_risk", "unknown"),
            })
        else:
            row.update({"cash_usd": "", "quarterly_burn_usd": "",
                        "runway_quarters": "", "dilution_risk": ""})
        rows.append(row)
    return rows


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--rank-by", default=os.environ.get("RANK_BY", "tier-then-date"),
                        choices=["tier-then-date", "date"])
    parser.add_argument("--dry-run", action="store_true",
                        help="run health probes only, write nothing")
    args = parser.parse_args()

    repo = Path(os.environ.get("GITHUB_REPO_PATH", Path(__file__).resolve().parent))
    out_dir = repo / "catalyst_screen"
    today = date.today()
    run_date = today.isoformat()
    target, label = next_quarter_window(today)

    setup_logging(out_dir / f"catalyst_run_{run_date}.debug.log")
    log.info("=== Catalyst screen v2: target %s (%s .. %s) ===",
             label, target.start, target.end)

    health = HealthRegistry()
    notes: list[str] = []
    session = requests.Session()

    try:
        edgar.headers()
    except edgar.SecUserAgentMissing as exc:
        log.error("%s", exc)
        health.record("config:sec_user_agent", status=Status.FAILED,
                      required=True, detail=str(exc))
        _finish(out_dir, run_date, label, target, 0, [], health, notes,
                {}, args.rank_by, aborted=True)
        sys.exit(1)

    if args.dry_run:
        for name, fn in (("edgar:fts", edgar.probe),
                         ("clinicaltrials.gov", ctgov.probe)):
            ok, detail = fn(session)
            health.record(name, status=Status.OK if ok else Status.FAILED,
                          required=True, detail=detail)
        print(health.summary())
        sys.exit(1 if health.should_abort() else 0)

    universe = build_universe(session, health, notes)
    log.info("Finviz universe: %d tickers", len(universe))

    cik_map: dict[str, str] = {}
    if universe:
        try:
            cik_map = edgar.load_cik_map(session)
            log.info("Loaded SEC ticker->CIK map (%d entries)", len(cik_map))
        except Exception as exc:
            log.error("SEC ticker map failed: %s", exc)
        universe = validate_tickers(universe, cik_map, health, notes)
        enrich_short_interest(
            universe, health, notes,
            use_fallback=os.environ.get("SKIP_SHORT_FALLBACK", "").lower() != "true")

    if health.should_abort():
        log.error("Aborting before catalyst stage:\n%s", health.summary())
        _finish(out_dir, run_date, label, target, len(universe), [], health,
                notes, {}, args.rank_by, aborted=True)
        sys.exit(0 if os.environ.get("ALLOW_DEGRADED", "").lower() == "true" else 1)

    sets = collect_catalysts(session, universe, target, health, notes)
    ranked = rank_sets(sets.values(), by=args.rank_by)
    log.info("Survivors: %d", len(ranked))

    rows = build_rows(
        ranked, universe, session,
        with_fundamentals=os.environ.get("SKIP_FUNDAMENTALS", "").lower() != "true")

    diff_summary = annotate(rows, find_previous(str(out_dir), run_date))

    aborted = health.should_abort()
    _finish(out_dir, run_date, label, target, len(universe), rows, health,
            notes, diff_summary, args.rank_by, aborted=aborted)

    if aborted and os.environ.get("ALLOW_DEGRADED", "").lower() != "true":
        log.error("Required source(s) failed; exiting non-zero.")
        sys.exit(1)
    log.info("=== Stage A complete: %d candidates ===", len(rows))


def _finish(out_dir: Path, run_date: str, label: str, target,
            universe_size: int, rows: list[dict], health: HealthRegistry,
            notes: list[str], diff_summary: dict, rank_by: str, *,
            aborted: bool) -> None:
    status = "FAILED" if aborted else ("DEGRADED" if health.degraded else "OK")
    if rows:
        write_csv(rows, out_dir / f"catalyst_candidates_{run_date}.csv")
    write_summary(
        out_dir / f"catalyst_candidates_{run_date}_log.txt",
        run_date=run_date, target_label=label,
        target_start=target.start.isoformat(), target_end=target.end.isoformat(),
        universe=universe_size, rows=rows, health_summary=health.summary(),
        status=status, diff_summary=diff_summary, rank_by=rank_by, notes=notes)
    write_manifest(
        out_dir / f"catalyst_manifest_{run_date}.json",
        run_date=run_date, target_label=label,
        target_start=target.start.isoformat(), target_end=target.end.isoformat(),
        universe=universe_size, survivors=len(rows),
        researched=sum(1 for r in rows if r.get("research_tier") == "FULL"),
        status=status, health=health.all, diff_summary=diff_summary,
        rank_by=rank_by)
    log.info("Run status: %s", status)


if __name__ == "__main__":
    main()
