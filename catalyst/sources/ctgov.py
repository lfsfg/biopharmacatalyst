"""ClinicalTrials.gov API v2.

Two v1 problems addressed here:

1. Sponsor matching was a raw text query using the Finviz company name.
   "Mirum Pharmaceuticals Inc" (Finviz) vs "Mirum Pharmaceuticals, Inc."
   (CT.gov) is a miss, and query.spons also matches collaborators, so a big
   sponsor's name pulls in trials it does not lead. Names are now normalised
   and the returned lead sponsor is verified rather than trusted.

2. Estimated completion dates were treated as equal in weight to a PDUFA
   date. They are not: CT.gov dates are sponsor-entered estimates and slip
   routinely. Everything from this source is tier SOFT.
"""

from __future__ import annotations

import logging
import re
from typing import Optional

import requests

from ..periods import Period, parse_period

API_URL = "https://clinicaltrials.gov/api/v2/studies"
TIMEOUT = 30
TIER = "SOFT"

FIELDS = ",".join([
    "protocolSection.identificationModule.nctId",
    "protocolSection.identificationModule.briefTitle",
    "protocolSection.sponsorCollaboratorsModule.leadSponsor.name",
    "protocolSection.designModule.phases",
    "protocolSection.statusModule.primaryCompletionDateStruct.date",
    "protocolSection.statusModule.primaryCompletionDateStruct.type",
    "protocolSection.statusModule.completionDateStruct.date",
    "protocolSection.statusModule.overallStatus",
    "protocolSection.conditionsModule.conditions",
])

_SUFFIXES = re.compile(
    r"\b(inc|incorporated|corp|corporation|co|company|ltd|limited|llc|lp|plc|"
    r"nv|sa|ag|as|ab|oyj|holdings?|group|therapeutics?|pharmaceuticals?|"
    r"pharma|biosciences?|bioscience|biotech|biotechnology|labs?|laboratories)\b",
    re.IGNORECASE,
)

log = logging.getLogger(__name__)


def normalise_sponsor(name: str) -> str:
    """Reduce a company name to a comparable core token set."""
    if not name:
        return ""
    n = name.lower()
    n = re.sub(r"[.,&/()']", " ", n)
    n = _SUFFIXES.sub(" ", n)
    n = re.sub(r"\s+", " ", n).strip()
    return n


def sponsor_matches(company: str, sponsor: str) -> bool:
    """Is `sponsor` plausibly the same entity as `company`?

    Compares normalised cores, requiring the shorter token sequence to be
    fully contained in the longer. This accepts "Mirum Pharmaceuticals, Inc."
    for "Mirum Pharmaceuticals Inc" and rejects a collaborator with an
    unrelated name.
    """
    a, b = normalise_sponsor(company), normalise_sponsor(sponsor)
    if not a or not b:
        return False
    if a == b:
        return True
    ta, tb = a.split(), b.split()
    short, long_ = (ta, tb) if len(ta) <= len(tb) else (tb, ta)
    return all(tok in long_ for tok in short)


def _search_query(company: str) -> str:
    """The distinctive part of the name makes a better query than the whole."""
    core = normalise_sponsor(company)
    return core or company


def phase3_completions(session: requests.Session, company: str,
                       target: Period) -> list[dict]:
    """Phase 3 trials led by `company` completing inside the target window."""
    if not company:
        return []
    advanced = (
        f"AREA[Phase]PHASE3 AND "
        f"AREA[PrimaryCompletionDate]RANGE[{target.start.isoformat()},"
        f"{target.end.isoformat()}]"
    )
    params = {
        "query.spons": _search_query(company),
        "filter.advanced": advanced,
        "fields": FIELDS,
        "pageSize": "50",
    }
    try:
        resp = session.get(API_URL, params=params, timeout=TIMEOUT,
                           headers={"Accept": "application/json"})
    except requests.RequestException as exc:
        log.warning("CT.gov request failed for %s: %s", company, exc)
        return []
    if resp.status_code != 200:
        log.warning("CT.gov HTTP %d for %s", resp.status_code, company)
        return []
    try:
        payload = resp.json()
    except ValueError:
        return []

    out: list[dict] = []
    for study in payload.get("studies", []):
        proto = study.get("protocolSection", {})
        ident = proto.get("identificationModule", {})
        status = proto.get("statusModule", {})
        lead = ((proto.get("sponsorCollaboratorsModule", {}) or {})
                .get("leadSponsor", {}) or {}).get("name", "")

        # Verify rather than trust: query.spons also matches collaborators.
        if not sponsor_matches(company, lead):
            log.debug("CT.gov sponsor mismatch: %r vs lead %r", company, lead)
            continue

        pcd_struct = status.get("primaryCompletionDateStruct") or {}
        pcd_raw = pcd_struct.get("date", "")
        period = parse_period(pcd_raw)
        if not period or not period.overlaps(target):
            continue

        out.append({
            "source": "CT.gov",
            "tier": TIER,
            "period": period,
            "nct_id": ident.get("nctId", ""),
            "title": ident.get("briefTitle", ""),
            "lead_sponsor": lead,
            "date_type": pcd_struct.get("type", ""),   # ACTUAL vs ESTIMATED
            "status": status.get("overallStatus", ""),
            "conditions": "; ".join(
                (proto.get("conditionsModule", {}) or {}).get("conditions", []) or []
            ),
        })
    return out


def probe(session: requests.Session) -> tuple[bool, str]:
    """A query that must return studies, to prove the API contract holds."""
    try:
        resp = session.get(API_URL, timeout=TIMEOUT,
                           params={"query.cond": "cancer", "pageSize": "1",
                                   "fields": "protocolSection.identificationModule.nctId"},
                           headers={"Accept": "application/json"})
    except requests.RequestException as exc:
        return False, f"probe request failed: {exc}"
    if resp.status_code != 200:
        return False, f"probe returned HTTP {resp.status_code}"
    try:
        studies = resp.json().get("studies", [])
    except ValueError:
        return False, "probe returned non-JSON"
    if not studies:
        return False, "probe for cancer studies returned zero results"
    return True, "probe OK"
