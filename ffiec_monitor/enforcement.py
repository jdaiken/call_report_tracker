"""Public enforcement actions, matched to the banks in a report.

Sources (all public, fetched fresh each run; a source that fails is reported, not fatal):
  Fed     https://www.federalreserve.gov/supervisionreg/files/enforcementactions.csv
  OCC     https://apps.occ.gov/EASearch/Search/ExportToJSON  (all actions)
  FinCEN  https://www.fincen.gov/news/enforcement-actions    (HTML table, paged)
  FDIC    data/fdic_active_orders.csv, built from an export of the FDIC's Enforcement
          Decisions and Orders search (orders.fdic.gov has no live data feed) by
          scripts/build_fdic_orders.py: institution-level consent/C&D orders, written
          agreements, PCA and capital directives issued since 2016 with no later
          termination, withdrawal or dismissal under the same docket. Matched by FDIC cert.
          Refresh it by re-exporting and re-running the script.

Fed and OCC: only ACTIVE actions (no termination date) against an institution, not an
individual. FinCEN: actions in the last 5 years (FinCEN actions don't terminate).

Matching is by normalized bank name + state (ffiec_monitor/matching.normalize_bank_name).
That is conservative but not perfect: regulators sometimes use a holding company or a
former name, so a miss is possible, and every match links to the source to confirm.
"""
from __future__ import annotations

import csv
import io
import logging
import re
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from typing import Optional

import requests

from .matching import normalize_bank_name

log = logging.getLogger(__name__)

FED_CSV = "https://www.federalreserve.gov/supervisionreg/files/enforcementactions.csv"
FED_PAGE = "https://www.federalreserve.gov/supervisionreg/enforcementactions.htm"
OCC_JSON = ("https://apps.occ.gov/EASearch/Search/ExportToJSON?Search=&StartDateMinimum=&StartDateMaximum="
            "&TerminationDateMinimum=&TerminationDateMaximum=&ShowIndividualActionsOnly=false"
            "&ShowInstitutionActionsOnly=false&ShowTerminatedActionsOnly=false&ShowActiveOnly=false&Category="
            "&Sort=BankName&AutoCompleteSelection=&CurrentPageIndex=0&ItemsPerPage=10&view=Table&IsAdvanced=true")
OCC_PAGE = "https://apps.occ.gov/EASearch/"
FINCEN_PAGE = "https://www.fincen.gov/news/enforcement-actions"
FDIC_SEARCH = "https://orders.fdic.gov/s/"
UA = {"User-Agent": "Mozilla/5.0 (compatible; call-report-monitor/1.0)"}

STATES = {
    "ALABAMA": "AL", "ALASKA": "AK", "ARIZONA": "AZ", "ARKANSAS": "AR", "CALIFORNIA": "CA", "COLORADO": "CO",
    "CONNECTICUT": "CT", "DELAWARE": "DE", "DISTRICT OF COLUMBIA": "DC", "FLORIDA": "FL", "GEORGIA": "GA",
    "HAWAII": "HI", "IDAHO": "ID", "ILLINOIS": "IL", "INDIANA": "IN", "IOWA": "IA", "KANSAS": "KS",
    "KENTUCKY": "KY", "LOUISIANA": "LA", "MAINE": "ME", "MARYLAND": "MD", "MASSACHUSETTS": "MA",
    "MICHIGAN": "MI", "MINNESOTA": "MN", "MISSISSIPPI": "MS", "MISSOURI": "MO", "MONTANA": "MT",
    "NEBRASKA": "NE", "NEVADA": "NV", "NEW HAMPSHIRE": "NH", "NEW JERSEY": "NJ", "NEW MEXICO": "NM",
    "NEW YORK": "NY", "NORTH CAROLINA": "NC", "NORTH DAKOTA": "ND", "OHIO": "OH", "OKLAHOMA": "OK",
    "OREGON": "OR", "PENNSYLVANIA": "PA", "RHODE ISLAND": "RI", "SOUTH CAROLINA": "SC", "SOUTH DAKOTA": "SD",
    "TENNESSEE": "TN", "TEXAS": "TX", "UTAH": "UT", "VERMONT": "VT", "VIRGINIA": "VA", "WASHINGTON": "WA",
    "WEST VIRGINIA": "WV", "WISCONSIN": "WI", "WYOMING": "WY", "PUERTO RICO": "PR", "GUAM": "GU",
}


@dataclass
class Action:
    agency: str            # Fed | OCC | FinCEN
    name: str              # as the regulator wrote it
    state: str             # 2-letter, "" if unknown
    action: str            # e.g. "Cease and Desist Order", "Formal Agreement"
    start: Optional[date]
    url: str
    detail: str = ""       # extra text from the source (Fed note, OCC subject matters) for topic tagging
    cert: str = ""         # FDIC cert, when the source gives it (FDIC orders): matched exactly

    @property
    def topic(self) -> str:
        """AML (Bank Secrecy Act / anti-money-laundering) or Safety & soundness (incl. capital)."""
        t = f"{self.action} {self.detail}".lower()
        if self.agency == "FinCEN" or any(k in t for k in ("bank secrecy", "bsa", "aml", "money laundering", "ofac")):
            return "AML"
        if any(k in t for k in ("capital", "prompt corrective", "pca")) and "consent" not in t:
            return "Capital"
        if self.agency == "FDIC":
            return "Safety & soundness or BSA (see order)"
        return "Safety & soundness"

    @property
    def stale(self) -> bool:
        """An order over 10 years old with no termination date may simply never have been closed out in the data."""
        return self.agency != "FinCEN" and self.start is not None and self.start < date.today() - timedelta(days=3650)


import os as _os

FINCEN_YEARS = 3
# Fed/OCC civil money penalties (one-time fines, mostly flood insurance) are excluded: the
# report covers active orders/agreements and AML. Set ENF_PENALTY_YEARS to list them anyway.
PENALTY_YEARS = int(_os.environ.get("ENF_PENALTY_YEARS", "0"))


def is_penalty(a: "Action") -> bool:
    """Civil money penalties are one-time fines (often for flood-insurance lapses) and never
    'terminate', so they stay listed as active forever. Kept apart from orders/agreements."""
    return a.agency == "FinCEN" or "penalt" in a.action.lower()


@dataclass
class Sources:
    actions: list[Action] = field(default_factory=list)
    errors: dict[str, str] = field(default_factory=dict)
    loaded: dict[str, int] = field(default_factory=dict)
    fdic_as_of: str = ""


def _date(s) -> Optional[date]:
    s = (s or "").strip()
    for fmt in ("%m/%d/%Y", "%Y-%m-%d", "%Y-%m-%dT%H:%M:%S", "%B %d, %Y", "%b %d, %Y", "%m/%d/%y"):
        try:
            return datetime.strptime(s[:19] if "T" in s else s, fmt).date()
        except ValueError:
            continue
    return None


def _state(text: str) -> str:
    t = (text or "").strip().upper().rstrip(".")
    if len(t) == 2 and t.isalpha():
        return t
    return STATES.get(t, "")


# ------------------------------------------------------------------ parsers (pure, tested)
def parse_fed(text: str) -> list[Action]:
    """Fed CSV: Banking Organization ("First Bank, Creve Coeur, Missouri"), Individual, Effective Date,
    Termination Date, Action, URL. Active institution actions only."""
    out = []
    for row in csv.DictReader(io.StringIO(text.lstrip("﻿"))):
        row = {(k or "").strip(): (v or "").strip() for k, v in row.items()}
        org, person = row.get("Banking Organization", ""), row.get("Individual", "")
        if not org or person or row.get("Termination Date"):
            continue
        parts = [p.strip() for p in org.split(",")]
        state = _state(parts[-1]) if len(parts) >= 3 else ""
        name = parts[0] if state else org
        out.append(Action("Fed", name, state, row.get("Action", ""), _date(row.get("Effective Date")),
                          row.get("URL") or FED_PAGE, row.get("Note", "")))
    return out


def parse_occ(records: list[dict]) -> list[Action]:
    """OCC JSON export: Institution, Location ("City, ST"), Individual, StartDate, TerminationDate,
    TypeDescription. Active institution actions only."""
    out = []
    for r in records:
        if (r.get("Individual") or "").strip() or (r.get("TerminationDate") or "").strip():
            continue
        name = (r.get("Institution") or "").strip()
        if not name:
            continue
        loc = (r.get("Location") or "").strip()
        state = _state(loc.split(",")[-1]) if "," in loc else _state(loc)
        subj = r.get("SubjectMatters") or ""
        subj = " ".join(subj) if isinstance(subj, list) else str(subj)
        out.append(Action("OCC", name, state, (r.get("TypeDescription") or "").strip(), _date(r.get("StartDate")),
                          OCC_PAGE, subj))
    return out


def parse_fincen(html: str, today: date) -> list[Action]:
    """FinCEN enforcement table: Enforcement Action ("In the Matter of X"), Date, Matter Number,
    Financial Institution (type). Bank-type institutions, last 5 years."""
    from lxml import html as lh
    doc = lh.fromstring(html)
    out = []
    for tr in doc.xpath("//table//tr[td]"):
        cells = tr.xpath("./td")
        if len(cells) < 2:
            continue
        texts = [" ".join(c.text_content().split()) for c in cells]
        name = re.sub(r"(?i)^in the matter of\s*", "", texts[0]).strip(" :")
        when = next((d for d in (_date(t) for t in texts[1:]) if d), None)
        kind = texts[-1].lower()
        if not name or (when and when < today - timedelta(days=5 * 365)):
            continue
        if not any(k in kind for k in ("bank", "depository", "savings", "credit union", "trust")):
            continue
        href = cells[0].xpath(".//a/@href")
        url = href[0] if href else FINCEN_PAGE
        url = url if url.startswith("http") else "https://www.fincen.gov" + url
        out.append(Action("FinCEN", name, "", "FinCEN Bank Secrecy Act enforcement action", when, url))
    return out


FDIC_FILE = __import__("pathlib").Path(__file__).resolve().parent.parent / "data" / "fdic_active_orders.csv"


def load_fdic(path=FDIC_FILE) -> tuple[list[Action], str]:
    """(actions, as-of date) from the prepared FDIC file; ([], "") if it isn't there."""
    p = __import__("pathlib").Path(path)
    if not p.exists():
        return [], ""
    out = []
    with p.open(newline="", encoding="utf-8") as fh:
        for r in csv.DictReader(fh):
            out.append(Action("FDIC", r["bank"], _state(r["state"]), r["action"].replace(";", ", "), _date(r["issued"]),
                              r["url"] or FDIC_SEARCH, r.get("title", ""), r["cert"].strip()))
    as_of = (p.with_suffix(".asof").read_text().strip() if p.with_suffix(".asof").exists() else "")
    return out, as_of


# ------------------------------------------------------------------ fetching
def fetch_all(today: date) -> Sources:
    src = Sources()
    try:
        acts, src.fdic_as_of = load_fdic()
        if acts:
            src.actions += acts
            src.loaded["FDIC"] = len(acts)
    except Exception as e:
        src.errors["FDIC"] = str(e)[:200]
    s = requests.Session()
    s.headers.update(UA)
    try:
        r = s.get(FED_CSV, timeout=60)
        r.raise_for_status()
        acts = parse_fed(r.content.decode("utf-8-sig", errors="replace"))
        src.actions += acts
        src.loaded["Fed"] = len(acts)
    except Exception as e:
        src.errors["Fed"] = str(e)[:200]
    try:
        r = s.get(OCC_JSON, timeout=90)
        r.raise_for_status()
        acts = parse_occ(r.json())
        src.actions += acts
        src.loaded["OCC"] = len(acts)
    except Exception as e:
        src.errors["OCC"] = str(e)[:200]
    try:
        acts: list[Action] = []
        for page in range(0, 30):
            r = s.get(FINCEN_PAGE, params={"page": page} if page else None, timeout=60)
            r.raise_for_status()
            batch = parse_fincen(r.text, today)
            new = [a for a in batch if (a.name, a.start) not in {(x.name, x.start) for x in acts}]
            if not new and page:
                break
            acts += new
            if "page=" not in r.text:
                break
        src.actions += acts
        src.loaded["FinCEN"] = len(acts)
    except Exception as e:
        src.errors["FinCEN"] = str(e)[:200]
    return src


# ------------------------------------------------------------------ matching
@dataclass
class Match:
    bank: object           # report.Bank
    action: Action
    how: str               # "name + state" | "name only"


def match(banks: list, actions: list[Action]) -> list[Match]:
    """banks: report.Bank objects (name, location "City, ST")."""
    idx: dict[tuple[str, str], list] = {}
    by_name: dict[str, list] = {}
    by_cert: dict[str, list] = {}
    for b in banks:
        if b.cert:
            by_cert.setdefault(b.cert.lstrip("0"), []).append(b)
        st = b.location.split(",")[-1].strip().upper() if "," in b.location else ""
        n = normalize_bank_name(b.name)
        idx.setdefault((n, st), []).append(b)
        by_name.setdefault(n, []).append(b)
    out, seen = [], set()
    today = date.today()
    for a in actions:
        if a.agency == "FinCEN":
            if a.start is not None and a.start < today - timedelta(days=365 * FINCEN_YEARS):
                continue
        elif is_penalty(a) and (a.start is None or a.start < today - timedelta(days=365 * PENALTY_YEARS)):
            continue                                   # one-time fines aren't an active order
        n = normalize_bank_name(a.name)
        if a.cert:
            hits, how = by_cert.get(a.cert.lstrip("0"), []), "FDIC cert"
        elif a.state:
            hits, how = idx.get((n, a.state), []), "name + state"
        else:
            hits = by_name.get(n, [])
            hits, how = (hits, "name only") if len(hits) == 1 else ([], "")
        for b in hits:
            key = (b.rssd, a.agency, a.action, a.start)
            if key not in seen:
                seen.add(key)
                out.append(Match(b, a, how))
    # Orders and agreements first, then penalties; newest first within each.
    out.sort(key=lambda m: (m.action.topic == "AML", m.action.start or date.min), reverse=True)
    return out
