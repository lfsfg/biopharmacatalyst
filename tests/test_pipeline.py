"""End-to-end wiring tests with the network stubbed out.

The point of these is to prove the health gates fire. v1's defect was not a
parsing bug in isolation -- it was that a run with no short-float data at all
still exited 0 and committed a candidates file.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import textwrap
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parent.parent


def run_screener(tmp_path: Path, stub: str, env_extra: dict | None = None):
    """Run catalyst_screener.py in a subprocess with sources stubbed."""
    sitecustomize = tmp_path / "sitecustomize.py"
    sitecustomize.write_text(textwrap.dedent(stub), encoding="utf-8")

    env = dict(os.environ)
    env.update({
        "GITHUB_REPO_PATH": str(tmp_path),
        "PYTHONPATH": f"{tmp_path}{os.pathsep}{REPO}",
        "SEC_USER_AGENT": "Test Runner test@example.org",
        "SKIP_SHORT_FALLBACK": "true",
        "SKIP_FUNDAMENTALS": "true",
    })
    env.update(env_extra or {})

    proc = subprocess.run(
        [sys.executable, str(REPO / "catalyst_screener.py")],
        cwd=tmp_path, env=env, capture_output=True, text=True, timeout=120)

    out_dir = tmp_path / "catalyst_screen"
    manifests = sorted(out_dir.glob("catalyst_manifest_*.json"))
    manifest = json.loads(manifests[-1].read_text()) if manifests else None
    return proc, manifest, out_dir


# A universe that parses fine but carries NO short float -- exactly v1's bug.
STUB_NO_SHORT_FLOAT = """
    import catalyst.sources.finviz as fv
    import catalyst.sources.edgar as ed
    import catalyst.sources.ctgov as ct
    import catalyst.sources.pdufa as pd

    fv.INDUSTRIES = {"Biotechnology": "ind_biotechnology"}
    fv.collect = lambda view, filters, session: {
        "BHVN": {"Ticker": "BHVN", "_ticker": "BHVN", "Company": "Biohaven Ltd",
                 "Market Cap": "2.25B", "Short Ratio": "6.24"},
    }
    fv.probe_industry_token = lambda token, session: True
    ed.load_cik_map = lambda session: {"BHVN": "0001935979"}
    ed.probe = lambda session: (True, "stub")
    ed.catalysts_for = lambda *a, **k: []
    ct.probe = lambda session: (True, "stub")
    ct.phase3_completions = lambda *a, **k: []
    pd.fetch = lambda session: ({}, "stub")
"""

# Same, but short float present.
STUB_HEALTHY = STUB_NO_SHORT_FLOAT.replace(
    '"Market Cap": "2.25B", "Short Ratio": "6.24"',
    '"Market Cap": "2.25B", "Short Ratio": "6.24", "Short Float": "18.42%"'
).replace(
    "ct.phase3_completions = lambda *a, **k: []",
    """ct.phase3_completions = lambda session, company, target: [
        {"source": "CT.gov", "tier": "SOFT",
         "period": __import__("catalyst.periods", fromlist=["x"]).parse_period("2026-11"),
         "nct_id": "NCT01", "title": "A Phase 3 study", "lead_sponsor": company,
         "date_type": "ESTIMATED", "status": "RECRUITING", "conditions": "X"}]"""
)


def test_missing_short_float_fails_the_run(tmp_path):
    """REGRESSION: v1 shipped 0/213 short-float coverage and exited 0."""
    proc, manifest, out_dir = run_screener(tmp_path, STUB_NO_SHORT_FLOAT)

    assert proc.returncode == 1, "run must fail when short float is absent"
    assert manifest is not None, "a manifest must be written even on failure"
    assert manifest["status"] == "FAILED"

    combined = next(s for s in manifest["sources"]
                    if s["name"] == "short_float:combined")
    assert combined["status"] == "FAILED"
    assert not list(out_dir.glob("catalyst_candidates_*.csv")), \
        "no candidates file may be written when the defining filter is empty"


def test_healthy_run_produces_ranked_candidates(tmp_path):
    proc, manifest, out_dir = run_screener(tmp_path, STUB_HEALTHY)

    assert proc.returncode == 0, proc.stdout + proc.stderr
    assert manifest["status"] in ("OK", "DEGRADED")
    assert manifest["counts"]["survivors"] == 1

    csvs = list(out_dir.glob("catalyst_candidates_*.csv"))
    assert len(csvs) == 1
    body = csvs[0].read_text()
    assert "BHVN" in body
    assert "18.42" in body, "short float must reach the output"
    assert "Q4 2026" in body or "2026-11" in body


def test_missing_sec_user_agent_aborts_immediately(tmp_path):
    """v1 forged a fake contact address, risking an SEC ban for the runner."""
    proc, manifest, _ = run_screener(
        tmp_path, STUB_HEALTHY, env_extra={"SEC_USER_AGENT": ""})

    assert proc.returncode == 1
    assert manifest["status"] == "FAILED"
    assert any(s["name"] == "config:sec_user_agent" and s["status"] == "FAILED"
               for s in manifest["sources"])


def test_allow_degraded_overrides_exit_code(tmp_path):
    proc, manifest, _ = run_screener(
        tmp_path, STUB_NO_SHORT_FLOAT, env_extra={"ALLOW_DEGRADED": "true"})
    assert proc.returncode == 0
    assert manifest["status"] == "FAILED", "status stays honest even when tolerated"
