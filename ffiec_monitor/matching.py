"""Shared name+state -> RSSD matching, used to resolve risk_rankings rows (which
have no RSSD) against a panel of reporters -- whether that panel comes from the
live FFIEC API (scripts/resolve_risk_rankings.py) or an offline bulk POR file
(scripts/resolve_from_bulk_por.py).

Deliberately conservative: only an EXACT normalized name+state match auto-assigns.
RSSD is the join key for every subsequent API call and ratio; a wrong guess here
would silently attach one bank's Call Report data to a different bank's watch-list
entry, which is worse than leaving a row unresolved for a human to check.
"""
from __future__ import annotations

import difflib
import re
from dataclasses import dataclass
from typing import Optional

FUZZY_MATCH_THRESHOLD = 0.90

_SUFFIX_WORDS = {
    "NATIONAL", "ASSOCIATION", "N.A.", "NA", "THE", "INC", "INC.", "CO", "CO.",
    "COMPANY", "F.S.B.", "FSB", "S.S.B.", "SSB",
}


@dataclass(frozen=True)
class PanelEntry:
    id_rssd: str
    name: str
    state: str


def normalize_bank_name(name: str) -> str:
    # Drop periods first so "N.A." / "F.S.B." collapse to "NA" / "FSB" instead of
    # splitting into stray single-letter tokens; other punctuation (commas, hyphens)
    # becomes a space separator instead.
    name = name.upper().replace(".", "")
    name = re.sub(r"[^\w\s]", " ", name)
    words = [w for w in name.split() if w not in _SUFFIX_WORDS]
    return " ".join(words)


@dataclass
class ResolutionResult:
    resolved: list[tuple[int, str]]                          # (row_id, id_rssd) -- safe to auto-assign
    ambiguous: list[tuple[int, str, str, list[str]]]          # (row_id, bank_name, state, candidate rssds)
    fuzzy: list[tuple[int, str, str, list[str]]]              # (row_id, bank_name, state, close name candidates)
    unresolved: list[tuple[int, str, str]]                    # (row_id, bank_name, state)


def resolve(
    unresolved_rows: list[tuple[int, str, str]],  # (row_id, bank_name, state)
    panel: list[PanelEntry],
) -> ResolutionResult:
    by_state: dict[str, list[PanelEntry]] = {}
    exact_index: dict[tuple[str, str], list[PanelEntry]] = {}
    for entry in panel:
        state = entry.state.strip().upper()
        by_state.setdefault(state, []).append(entry)
        norm = normalize_bank_name(entry.name)
        exact_index.setdefault((norm, state), []).append(entry)

    result = ResolutionResult(resolved=[], ambiguous=[], fuzzy=[], unresolved=[])

    for row_id, bank_name, state in unresolved_rows:
        state = state.strip().upper()
        norm = normalize_bank_name(bank_name)
        candidates = exact_index.get((norm, state), [])

        if len(candidates) == 1:
            result.resolved.append((row_id, candidates[0].id_rssd))
            continue

        if len(candidates) > 1:
            result.ambiguous.append((row_id, bank_name, state, [c.id_rssd for c in candidates]))
            continue

        state_names = [e.name for e in by_state.get(state, [])]
        close = difflib.get_close_matches(bank_name, state_names, n=3, cutoff=FUZZY_MATCH_THRESHOLD)
        if close:
            result.fuzzy.append((row_id, bank_name, state, close))
        else:
            result.unresolved.append((row_id, bank_name, state))

    return result


def print_report(result: ResolutionResult) -> None:
    for _row_id, bank_name, state, rssds in result.ambiguous:
        print(f"  AMBIGUOUS  {bank_name!r} ({state}): {len(rssds)} exact-name matches -- {rssds}")
    for _row_id, bank_name, state, close in result.fuzzy:
        print(f"  FUZZY      {bank_name!r} ({state}): closest candidates -- {close}")
    for _row_id, bank_name, state in result.unresolved:
        print(f"  UNRESOLVED {bank_name!r} ({state}): no match on FFIEC panel")
    print(
        f"\nResolved {len(result.resolved)} exact matches (auto-assigned). "
        f"{len(result.ambiguous)} ambiguous, {len(result.fuzzy)} fuzzy, "
        f"{len(result.unresolved)} unresolved -- review those above and assign "
        "id_rssd by hand (UPDATE risk_rankings SET id_rssd = ? WHERE id = ?) once "
        "you've confirmed the right institution."
    )
