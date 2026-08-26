"""Tests for the quarterly catalyst screener.

Several cases here are regression tests reproducing bugs found in v1's first
live run (2026-08-26). They are marked REGRESSION and should not be relaxed.
"""

from __future__ import annotations

import sys
from datetime import date
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from catalyst.catalysts import CatalystSet, rank
from catalyst.diffing import annotate
from catalyst.health import HealthRegistry, Status, assess_coverage
from catalyst.periods import (Period, find_period, next_quarter_window,
                              parse_period, quarter_bounds)
from catalyst.sources import ctgov, finviz, pdufa


# ---------------------------------------------------------------- periods --

@pytest.mark.parametrize("run_day,label,start,end", [
    (date(2026, 3, 17),  "Q2 2026", date(2026, 4, 1),  date(2026, 6, 30)),
    (date(2026, 6, 16),  "Q3 2026", date(2026, 7, 1),  date(2026, 9, 30)),
    (date(2026, 9, 17),  "Q4 2026", date(2026, 10, 1), date(2026, 12, 31)),
    (date(2026, 12, 17), "Q1 2027", date(2027, 1, 1),  date(2027, 3, 31)),
    (date(2026, 2, 28),  "Q2 2026", date(2026, 4, 1),  date(2026, 6, 30)),
])
def test_next_quarter_window(run_day, label, start, end):
    period, got_label = next_quarter_window(run_day)
    assert (got_label, period.start, period.end) == (label, start, end)


@pytest.mark.parametrize("text,start,end,precision", [
    ("2026-10-31",              date(2026, 10, 31), date(2026, 10, 31), "day"),
    ("October 15, 2026",        date(2026, 10, 15), date(2026, 10, 15), "day"),
    ("2026-10",                 date(2026, 10, 1),  date(2026, 10, 31), "month"),
    ("Q4 2026",                 date(2026, 10, 1),  date(2026, 12, 31), "quarter"),
    ("4Q26",                    date(2026, 10, 1),  date(2026, 12, 31), "quarter"),
    ("fourth quarter of 2026",  date(2026, 10, 1),  date(2026, 12, 31), "quarter"),
    ("second half of 2026",     date(2026, 7, 1),   date(2026, 12, 31), "half"),
    ("2H26",                    date(2026, 7, 1),   date(2026, 12, 31), "half"),
    ("mid-2026",                date(2026, 4, 1),   date(2026, 9, 30),  "fuzzy"),
    ("late 2026",               date(2026, 9, 1),   date(2026, 12, 31), "fuzzy"),
    ("year-end 2026",           date(2026, 10, 1),  date(2026, 12, 31), "fuzzy"),
])
def test_parse_period(text, start, end, precision):
    p = parse_period(text)
    assert p is not None, f"failed to parse {text!r}"
    assert (p.start, p.end, p.precision) == (start, end, precision)


def test_parse_period_rejects_junk():
    for junk in ["", "   ", "no date here", "TBD", "topline data"]:
        assert parse_period(junk) is None


def test_overlap_is_not_string_comparison():
    """REGRESSION: v1 compared '2026-10' to '2026-10-01' lexically.

    A partial month must overlap the quarter that contains it.
    """
    q4 = quarter_bounds(2026, 4)
    assert parse_period("2026-10").overlaps(q4)
    assert parse_period("2026-09").overlaps(q4) is False
    # A vague half-year window straddling the quarter still counts.
    assert parse_period("second half of 2026").overlaps(q4)


def test_period_display_and_sort():
    q4 = quarter_bounds(2026, 4)
    assert q4.display() == "Q4 2026"
    # Same start date: the tighter window sorts first.
    exact = parse_period("2026-10-01")
    month = parse_period("2026-10")
    assert exact.start == month.start
    assert exact.sort_key() < month.sort_key()
    # Earlier start always wins regardless of width.
    assert parse_period("2026-10-05").sort_key() > month.sort_key()


# ----------------------------------------------------------------- finviz --

def test_ticker_read_from_href_not_text():
    """REGRESSION: v1 emitted BBHVN for BHVN across all 213 rows."""
    html = """<table>
    <tr><th>No.</th><th>Ticker</th><th>Company</th></tr>
    <tr><td>1</td>
        <td><a href="quote.ashx?t=BHVN&ty=c"><span>B</span>BHVN</a></td>
        <td>Biohaven Ltd</td></tr></table>"""
    rows = finviz.parse_screener_table(html)
    assert rows[0]["_ticker"] == "BHVN"


@pytest.mark.parametrize("cell,expected", [
    # Classic Finviz quote link.
    ('<td><a href="quote.ashx?t=ABEO&ty=c"><span>A</span>ABEO</a></td>', "ABEO"),
    # REGRESSION: badge element sits OUTSIDE the anchor, so cell text reads
    # "AABEO". The live dry run produced exactly this for every row.
    ('<td><span class="badge">A</span><a href="/x/y">ABEO</a></td>', "ABEO"),
    # Path-style link.
    ('<td><span>A</span><a href="/quote/ABEO/">ABEO</a></td>', "ABEO"),
    # Plain cell, no anchor.
    ('<td>ABEO</td>', "ABEO"),
    # Real symbols with repeated leading letters must survive untouched.
    ('<td><a href="quote.ashx?t=AA">AA</a></td>', "AA"),
    ('<td><a href="quote.ashx?t=AAPL">AAPL</a></td>', "AAPL"),
    ('<td><span class="badge">A</span><a href="/x">AAPL</a></td>', "AAPL"),
])
def test_ticker_extraction_across_markup_variants(cell, expected):
    html = (f'<table><tr><th>Ticker</th><th>Company</th></tr>'
            f'<tr>{cell}<td>Co</td></tr></table>')
    rows = finviz.parse_screener_table(html)
    assert rows and rows[0]["_ticker"] == expected


def test_ticker_fallback_never_mangles_real_symbols():
    """A repair heuristic would turn AAPL into APL. It must not exist."""
    html = """<table><tr><th>Ticker</th><th>Company</th></tr>
    <tr><td>AAPL</td><td>Apple Inc</td></tr>
    <tr><td>AA</td><td>Alcoa Corp</td></tr></table>"""
    tickers = [r["_ticker"] for r in finviz.parse_screener_table(html)]
    assert tickers == ["AAPL", "AA"]


@pytest.mark.parametrize("header", ["Short Float", "Float Short", "Short Float %"])
def test_short_float_alias_resolution(header):
    """REGRESSION: v1 looked up only 'Float Short' and got nothing on all rows."""
    rec = {header: "18.42%", "Short Ratio": "6.24"}
    assert finviz.to_number(finviz.resolve(rec, "short_float")) == 18.42
    assert finviz.to_number(finviz.resolve(rec, "short_ratio")) == 6.24


def test_missing_short_float_returns_none_not_zero():
    assert finviz.resolve({"Short Ratio": "6.24"}, "short_float") is None


@pytest.mark.parametrize("raw,expected", [
    ("18.42%", 18.42), ("1.23B", 1.23e9), ("437.96M", 4.3796e8),
    ("12,345", 12345.0), ("6.24", 6.24), ("-", None), ("", None), (None, None),
])
def test_to_number(raw, expected):
    assert finviz.to_number(raw) == expected


def test_parse_table_ignores_non_result_tables():
    html = """<html>
    <table><tr><td>nav</td></tr></table>
    <table><tr><th>Ticker</th><th>Company</th></tr>
           <tr><td>XYZ</td><td>Xyz Inc</td></tr></table></html>"""
    rows = finviz.parse_screener_table(html)
    assert len(rows) == 1 and rows[0]["_ticker"] == "XYZ"


# ------------------------------------------------------------------ ctgov --

@pytest.mark.parametrize("company,sponsor,expected", [
    ("Mirum Pharmaceuticals Inc", "Mirum Pharmaceuticals, Inc.", True),
    ("Biohaven Ltd", "Biohaven Pharmaceutical Holding Company Ltd.", True),
    ("Annexon Inc", "Annexon, Inc.", True),
    ("Regenxbio Inc", "REGENXBIO Inc.", True),
    ("Mirum Pharmaceuticals Inc", "Takeda Pharmaceutical Company", False),
    ("EyePoint Inc", "Alimera Sciences", False),
])
def test_sponsor_matching(company, sponsor, expected):
    """REGRESSION: v1 trusted a text query that also matches collaborators."""
    assert ctgov.sponsor_matches(company, sponsor) is expected


def test_normalise_sponsor_strips_noise():
    assert ctgov.normalise_sponsor("Acme Therapeutics, Inc.") == "acme"
    assert ctgov.normalise_sponsor("") == ""


# ------------------------------------------------------------------ pdufa --

def test_pdufa_table_parsing():
    html = """<table>
    <tr><th>Company</th><th>Ticker</th><th>Date</th><th>Drug</th></tr>
    <tr><td>Acme Bio</td><td>ACME</td><td>November 14, 2026</td><td>acmezumab</td></tr>
    </table>"""
    parsed = pdufa.parse_calendar(html)
    assert "ACME" in parsed
    assert parsed["ACME"][0]["period"].start == date(2026, 11, 14)


def test_pdufa_card_layout_fallback():
    """REGRESSION: v1 parsed 0 rows because it assumed one exact layout."""
    html = """<div><li>Acme Bio (ACME) — PDUFA November 14, 2026</li></div>"""
    parsed = pdufa.parse_calendar(html)
    assert "ACME" in parsed
    assert parsed["ACME"][0]["period"].start == date(2026, 11, 14)


# -------------------------------------------------------------- catalysts --

def _item(source, tier, text):
    return {"source": source, "tier": tier, "period": parse_period(text)}


def test_tier_beats_proximity_by_default():
    """A hard PDUFA date should outrank an earlier soft CT.gov estimate."""
    soft = CatalystSet("SOFTY")
    soft.add(_item("CT.gov", "SOFT", "2026-10-01"))
    hard = CatalystSet("HARDY")
    hard.add(_item("pdufa.bio", "HARD", "2026-12-20"))

    assert [s.ticker for s in rank([soft, hard])] == ["HARDY", "SOFTY"]
    # v1 behaviour remains available explicitly.
    assert [s.ticker for s in rank([soft, hard], by="date")] == ["SOFTY", "HARDY"]


def test_best_tier_and_period_selection():
    cs = CatalystSet("MULTI")
    cs.add(_item("CT.gov", "SOFT", "2026-10-05"))
    cs.add(_item("pdufa.bio", "HARD", "2026-11-20"))
    cs.add(_item("EDGAR", "HARD", "2026-11-02"))
    assert cs.best_tier == "HARD"
    assert cs.best_period.start == date(2026, 11, 2)
    assert cs.sources == ["CT.gov", "EDGAR", "pdufa.bio"]


def test_empty_set_is_excluded_from_ranking():
    assert rank([CatalystSet("NONE")]) == []


# ----------------------------------------------------------------- health --

def test_coverage_zero_is_failure_not_success():
    """REGRESSION: short float was 0/213 in v1 and the run still passed."""
    h = assess_coverage("finviz:short_float", 0, 213, required=True,
                        min_ratio=0.8, what="short float")
    assert h.status is Status.FAILED


def test_coverage_partial_is_degraded():
    h = assess_coverage("x", 100, 213, required=True, min_ratio=0.8, what="f")
    assert h.status is Status.DEGRADED


def test_coverage_full_is_ok():
    h = assess_coverage("x", 210, 213, required=True, min_ratio=0.8, what="f")
    assert h.status is Status.OK


def test_registry_aborts_only_on_required_failures():
    reg = HealthRegistry()
    reg.record("optional", status=Status.FAILED, required=False)
    assert reg.should_abort() is False
    reg.record("critical", status=Status.FAILED, required=True, detail="boom")
    assert reg.should_abort() is True
    assert "boom" in reg.summary()


# ---------------------------------------------------------------- diffing --

def test_quarter_over_quarter_annotation(tmp_path):
    prev = tmp_path / "catalyst_candidates_2026-06-17.csv"
    prev.write_text(
        "ticker,catalyst_date,short_float_pct\n"
        "AAA,Q3 2026,12.0\n"
        "BBB,2026-08-01,20.0\n", encoding="utf-8")

    rows = [
        {"ticker": "AAA", "catalyst_date": "Q4 2026", "short_float_pct": 18.5},
        {"ticker": "CCC", "catalyst_date": "Q4 2026", "short_float_pct": 11.0},
    ]
    summary = annotate(rows, str(prev))

    assert rows[0]["prev_seen"] == "carried over"
    assert rows[0]["date_moved"] == "Q3 2026 -> Q4 2026"
    assert rows[0]["short_float_change"] == "+6.5pp"
    assert rows[1]["prev_seen"] == "new"
    assert summary["carried_over"] == 1
    assert summary["new_names"] == 1
    assert summary["dropped"] == 1
    assert summary["dates_slipped"] == 1


def test_diff_without_previous_run_is_safe():
    rows = [{"ticker": "AAA", "catalyst_date": "Q4 2026", "short_float_pct": 1.0}]
    summary = annotate(rows, None)
    assert rows[0]["prev_seen"] == ""
    assert summary["carried_over"] == 0


def test_find_period_in_free_text():
    """parse_period needs a bare date; find_period digs one out of prose."""
    assert parse_period("Acme Bio (ACME) - PDUFA November 14, 2026") is None
    p = find_period("Acme Bio (ACME) - PDUFA November 14, 2026")
    assert p is not None and p.start == date(2026, 11, 14)

    p2 = find_period("expects topline data in the fourth quarter of 2026")
    assert p2 is not None and p2.start == date(2026, 10, 1)

    assert find_period("no dates at all here") is None
