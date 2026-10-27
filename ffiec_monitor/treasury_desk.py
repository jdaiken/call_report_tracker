"""Treasury Desk special alert -- a user-supplied list of banks (per user request
2026-09-30) that gets its own section at the top of the daily email whenever
anything at all happens to one of them this run, regardless of tier.

The source list is names only (no RSSD, no FDIC cert, mostly no state), so
matching is by name. data/treasury-desk.csv holds each bank as written, plus
known name variations ("aliases") where the list's wording differs from the
legal/Panel-of-Reporters name (e.g. "Apple Bank" -> "Apple Bank for Savings",
"WaFd Bank" -> "Washington Federal Bank"). On top of that, normalize() folds
the purely cosmetic differences (case, punctuation, "&" vs "and", "N.A." vs
"National Association", a trailing "Company"/"Co.", a leading "The").

It deliberately does NOT fold "National Association" away entirely, or do
fuzzy matching: "Pioneer Bank National Association" should not match every
unrelated "Pioneer Bank" in the country. Generic names on the list ("Pinnacle
Bank", "Cornerstone Bank", ...) can still match more than one institution --
fill in the CSV's optional `state` column to pin those to one state. Every
match shows the institution's city/state/RSSD, so a wrong one is visible.

Same module (same matching rules) lives in edgar_bank_monitor; keep in sync.
"""
from __future__ import annotations

import csv
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

DEFAULT_LIST_PATH = Path(__file__).resolve().parent.parent / "data" / "treasury-desk.csv"

_NON_ALNUM = re.compile(r"[^A-Z0-9 ]")
_WHITESPACE = re.compile(r"\s+")
_TRAILING_WORDS = ("COMPANY", "CO")


def normalize(name: str) -> str:
    upper = name.upper().replace("&", " AND ").replace(".", "")
    upper = _WHITESPACE.sub(" ", _NON_ALNUM.sub(" ", upper)).strip()
    upper = re.sub(r"\bNATIONAL ASSOCIATION\b", "NA", upper)
    words = upper.split()
    if words and words[0] == "THE":
        words = words[1:]
    while words and words[-1] in _TRAILING_WORDS:
        words = words[:-1]
    return " ".join(words)


@dataclass(frozen=True)
class TreasuryDeskEntry:
    name: str
    aliases: tuple[str, ...] = ()
    state: str = ""  # optional two-letter pin for names shared by several banks


class TreasuryDeskMatcher:
    def __init__(self, entries: list[TreasuryDeskEntry]):
        self._by_norm: dict[str, list[TreasuryDeskEntry]] = {}
        for entry in entries:
            for variant in (entry.name, *entry.aliases):
                key = normalize(variant)
                if key:
                    self._by_norm.setdefault(key, []).append(entry)

    def match(self, name: str, state: str = "") -> Optional[str]:
        """The Treasury Desk list name `name` matches, or None."""
        for entry in self._by_norm.get(normalize(name), []):
            if not entry.state or entry.state.upper() == (state or "").strip().upper():
                return entry.name
        return None


def load(path: Path = DEFAULT_LIST_PATH) -> TreasuryDeskMatcher:
    entries = []
    with open(path, newline="", encoding="utf-8-sig") as f:
        for row in csv.DictReader(f):
            name = (row.get("name") or "").strip()
            if not name:
                continue
            aliases = tuple(a.strip() for a in (row.get("aliases") or "").split("|") if a.strip())
            entries.append(TreasuryDeskEntry(name, aliases, (row.get("state") or "").strip()))
    return TreasuryDeskMatcher(entries)
