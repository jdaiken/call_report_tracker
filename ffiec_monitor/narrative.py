"""The plain-English "What's going on" paragraph at the top of the daily email and PDF.

Written by Claude (Anthropic API) from the day's filings when ANTHROPIC_API_KEY is set.
Claude sees only what is in the report; a paragraph containing a number that is not in
that data is rejected (one retry), and a rule-based paragraph is used instead. The same
fallback is used with no key or if the API call fails, so the email never goes out
without a summary and never waits on the API.
"""
from __future__ import annotations

import logging
import os
import re
from typing import TYPE_CHECKING

import requests

if TYPE_CHECKING:
    from .report import Report

log = logging.getLogger(__name__)

API_URL = "https://api.anthropic.com/v1/messages"
DEFAULT_MODEL = "claude-sonnet-5-5"
MAX_CHARS = 650
STATUS = "not run"           # last outcome; run.py prints it as a GitHub annotation

PROMPT = """You write the opening paragraph of a daily bank Call Report monitoring email read by a bank risk manager.
Plain English for a risk professional; explain any term a non-specialist would not know in a few words.

Write ONE paragraph of 2 to 5 sentences, at most 100 words, saying what happened in today's filings and what deserves
attention. Lead with the most important thing: Tier 1 alerts first (name the bank and what crossed), then Watch List
changes, then Tier 2, then new filings the level screen flagged (name the most concerning and say in a few words
why it matters for a bank that takes reciprocal or brokered deposits: capital near the well-capitalized line, losses,
tight liquidity, or heavy brokered/reciprocal funding). If there are no alerts or flags, say so plainly, then briefly
describe the refilings and new filings. Do not list more than three banks.

Rules:
- Use ONLY facts and numbers from the data below. Do not add any number, date, name or event that is not there.
- Copy numbers exactly as written (same rounding and units).
- No predictions, no advice, no headings, no bullet points, no markdown.

Today's data:
"""


_NUM = re.compile(r"\d+(?:[.,]\d+)*")


def _numbers(text: str) -> set[str]:
    return {n.replace(",", "") for n in _NUM.findall(text)}


def _post(api_key: str, model: str, content: str) -> requests.Response:
    return requests.post(API_URL, timeout=60, headers={
        "x-api-key": api_key, "anthropic-version": "2023-06-01", "content-type": "application/json",
    }, json={"model": model, "max_tokens": 2000, "messages": [{"role": "user", "content": content}]})


def _newest_sonnet(api_key: str) -> str | None:
    try:
        r = requests.get("https://api.anthropic.com/v1/models", timeout=30,
                         headers={"x-api-key": api_key, "anthropic-version": "2023-06-01"})
        r.raise_for_status()
        ids = [m["id"] for m in r.json().get("data", []) if "sonnet" in m.get("id", "")]
        return ids[0] if ids else None
    except Exception:
        return None


def claude_summary(rep: "Report", api_key: str, model: str | None = None) -> str | None:
    global STATUS
    from .report import facts
    data = facts(rep)
    model = model or os.environ.get("ANTHROPIC_MODEL") or DEFAULT_MODEL
    prompt = PROMPT + data
    bad: list[str] = []
    for attempt in range(2):
        try:
            r = _post(api_key, model, prompt)
            if r.status_code == 404 and attempt == 0:
                alt = _newest_sonnet(api_key)
                if alt and alt != model:
                    model = alt
                    r = _post(api_key, model, prompt)
            if r.status_code >= 400:
                STATUS = f"API error {r.status_code} ({model}): {r.text[:300]}"
                log.warning("Claude summary unavailable: %s", STATUS)
                return None
            body = r.json()
            text = " ".join(b.get("text", "") for b in body.get("content", []) if b.get("type") == "text")
        except Exception as e:
            STATUS = f"API call failed: {e}"
            log.warning("Claude summary unavailable: %s", e)
            return None
        text = re.sub(r"\s+", " ", text.replace("**", "")).strip()
        if not text:
            STATUS = f"empty reply ({model}): stop_reason={body.get('stop_reason')}"
            return None
        if len(text) > MAX_CHARS:
            cut = text[:MAX_CHARS].rsplit(". ", 1)[0]
            text = cut if cut.endswith(".") else cut + "."
        bad = sorted(_numbers(text) - (_numbers(data) | {str(i) for i in range(0, 13)}))
        if not bad:
            STATUS = f"written by Claude ({model}){' on retry' if attempt else ''}"
            return text
        prompt = (PROMPT + data + "\n\nYour previous draft used numbers that are not in the data: "
                  + ", ".join(bad) + ". Rewrite it using only numbers copied exactly from the data.")
    STATUS = f"rejected: numbers not in the data ({', '.join(bad)})"
    log.warning("Claude summary %s", STATUS)
    return None


def _names(rows, n: int = 3) -> str:
    names = [f"{r.bank.name} ({r.bank.assets})" for r in rows[:n]]
    return ", ".join(names[:-1]) + (" and " if len(names) > 1 else "") + names[-1] if names else ""


def rule_summary(rep: "Report") -> str:
    c = rep.counts
    parts = []
    if rep.tier1:
        a = rep.tier1[0]
        first = a.triggers[0] if a.triggers else "a threshold was crossed"
        more = f" and {c['tier1'] - 1} other {'bank' if c['tier1'] == 2 else 'banks'}" if c["tier1"] > 1 else ""
        parts.append(f"Tier 1 alert for {a.bank.name} ({a.bank.location}){more}: {first}.")
    if rep.watch_changes:
        parts.append("The Watch List changed: " + "; ".join(f"{w.verb.lower()} {w.bank.name}" for w in rep.watch_changes[:3]) + ".")
    if rep.tier2:
        parts.append(f"{c['tier2']} Tier 2 {'item' if c['tier2'] == 1 else 'items'}, led by {rep.tier2[0].bank.name}.")
    if not (rep.tier1 or rep.watch_changes or rep.tier2):
        parts.append("No alerts today: no bank crossed a Tier 1 or Tier 2 threshold.")
    if rep.refilings_total:
        parts.append(f"{rep.refilings_total} {'bank' if rep.refilings_total == 1 else 'banks'} amended a Call Report "
                     f"without a material capital change; the largest {'is' if rep.refilings_total == 1 else 'are'} "
                     f"{_names(rep.refilings)}.")
    if c["new"]:
        parts.append(f"{c['new']} new quarterly {'filing' if c['new'] == 1 else 'filings'} came in.")
    if rep.flagged:
        r = rep.flagged[0]
        parts.append(f"The screen flagged {c['flagged']} of them; the most concerning is {r.bank.name} "
                     f"({r.bank.location}, {r.bank.assets}): {r.flags[0].text}.")
    return " ".join(parts)


def write_summary(rep: "Report") -> tuple[str, str]:
    """(paragraph, 'Claude' or 'rules')."""
    global STATUS
    key = os.environ.get("ANTHROPIC_API_KEY", "").strip()
    if key:
        text = claude_summary(rep, key)
        if text:
            return text, "Claude"
    else:
        STATUS = "no ANTHROPIC_API_KEY; rule-based summary used"
    return rule_summary(rep), "rules"
