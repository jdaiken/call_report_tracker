"""The plain-English "What's going on" paragraph at the top of page 1.

Written by Claude (Anthropic API) from today's numbers when ANTHROPIC_API_KEY is set.
Claude only sees the figures in the report, and its paragraph is rejected if it contains
a number that isn't in those figures. Without a key, or if the call fails or is rejected,
a rule-based paragraph is used instead, so the report always has a summary.
"""
from __future__ import annotations

import logging
import os
import re
from typing import TYPE_CHECKING

import requests

if TYPE_CHECKING:
    from .monitor import Report

log = logging.getLogger(__name__)

API_URL = "https://api.anthropic.com/v1/messages"
DEFAULT_MODEL = "claude-sonnet-5-5"
MAX_CHARS = 650

PROMPT = """You write the opening paragraph of a daily rates and deposit risk report read by a bank risk manager.
The audience is a risk professional, not an economist: use plain English, no jargon without a short explanation.

Write ONE paragraph of 3 to 5 sentences, at most 90 words, saying what is going on today and what it means for
banks' deposits and funding. Lead with the most important thing. Mention anything in Watch or Risk, recent bank
failures, and an upcoming Fed decision if it is within a week. If everything is normal, say so plainly.

Rules:
- Use ONLY facts and numbers from the data below. Do not add any number, date, name or event that is not there.
- Copy numbers exactly as written in the data (same rounding and units).
- No predictions, no advice, no hedging filler, no headings, no bullet points, no markdown.
- Do not name any company that uses this report.

Today's data:
"""


def facts(rep: "Report") -> str:
    from .monitor import render_text
    lines = [render_text(rep), "", "RATES TABLE (latest; change over 1 week, 1 month, 1 year; as of)"]
    for title, rows in rep.rates:
        lines.append(title.upper())
        lines += [f"  {r.name}: {r.latest}; 1 wk {r.week}; 1 mo {r.month}; 1 yr {r.year}; as of {r.as_of}" for r in rows]
    if rep.recent_failures:
        lines.append("BANK FAILURES THIS YEAR (newest first)")
        lines += [f"  {f.fail_date:%b %-d}: {f.name}, {f.city_state}" for f in rep.recent_failures]
    return "\n".join(lines)


_NUM = re.compile(r"\d+(?:[.,]\d+)*")


def _numbers(text: str) -> set[str]:
    return {n.replace(",", "") for n in _NUM.findall(text)}


def grounded(summary: str, source: str) -> bool:
    """Every number in the summary must appear somewhere in the source data."""
    allowed = _numbers(source) | {str(i) for i in range(0, 13)}       # small counts ("two", "3 of 14") are fine
    return _numbers(summary) <= allowed


STATUS = "not run"          # last outcome, reported as a GitHub annotation by run.py


def _post(api_key: str, model: str, content: str) -> requests.Response:
    return requests.post(API_URL, timeout=60, headers={
        "x-api-key": api_key, "anthropic-version": "2023-06-01", "content-type": "application/json",
    }, json={"model": model, "max_tokens": 400, "messages": [{"role": "user", "content": content}]})


def _newest_sonnet(api_key: str) -> str | None:
    """If the configured model name is wrong, pick the newest Sonnet the key can use."""
    try:
        r = requests.get("https://api.anthropic.com/v1/models", timeout=30,
                         headers={"x-api-key": api_key, "anthropic-version": "2023-06-01"})
        r.raise_for_status()
        ids = [m["id"] for m in r.json().get("data", []) if "sonnet" in m.get("id", "")]
        return ids[0] if ids else None                      # the API lists newest first
    except Exception:
        return None


def claude_summary(rep: "Report", api_key: str, model: str | None = None) -> str | None:
    global STATUS
    data = facts(rep)
    model = model or os.environ.get("ANTHROPIC_MODEL") or DEFAULT_MODEL
    prompt = PROMPT + data
    for attempt in range(2):
        try:
            r = _post(api_key, model, prompt)
            if r.status_code == 404 and attempt == 0:          # unknown model name
                alt = _newest_sonnet(api_key)
                if alt and alt != model:
                    model = alt
                    r = _post(api_key, model, prompt)
            if r.status_code >= 400:
                STATUS = f"API error {r.status_code} ({model}): {r.text[:300]}"
                log.warning("Claude summary unavailable: %s", STATUS)
                return None
            text = " ".join(b.get("text", "") for b in r.json().get("content", []) if b.get("type") == "text")
        except Exception as e:                                   # network, timeout
            STATUS = f"API call failed: {e}"
            log.warning("Claude summary unavailable: %s", e)
            return None
        text = re.sub(r"\s+", " ", text.replace("**", "")).strip()
        if len(text) > MAX_CHARS:
            cut = text[:MAX_CHARS].rsplit(". ", 1)[0]
            text = cut if cut.endswith(".") else cut + "."
        bad = sorted(_numbers(text) - (_numbers(data) | {str(i) for i in range(0, 13)}))
        if text and not bad:
            STATUS = f"written by Claude ({model}){' on retry' if attempt else ''}"
            return text
        # One retry, telling Claude exactly what was wrong.
        prompt = (PROMPT + data + "\n\nYour previous draft used numbers that are not in the data: "
                  + ", ".join(bad) + ". Rewrite it using only numbers copied exactly from the data.")
    STATUS = f"rejected: numbers not in the data ({', '.join(bad)})"
    log.warning("Claude summary %s", STATUS)
    return None


def _nm(name: str) -> str:
    """'Market fear (VIX)' -> 'market fear (VIX)'; keep acronyms."""
    return " ".join(w if any(c.isupper() for c in w[1:]) else w.lower() for w in name.split())


def rule_summary(rep: "Report") -> str:
    rows = {r.name: r for _, grp in rep.groups for r in grp}
    risk = [r for r in rows.values() if r.tone == "risk"]
    watch = [r for r in rows.values() if r.tone == "watch"]
    parts = []
    if not risk and not watch:
        parts.append("Everything the report tracks is in its normal range today.")
    else:
        bits = []
        if risk:
            bits.append(f"{len(risk)} in Risk ({', '.join(_nm(r.name) for r in risk)})")
        if watch:
            bits.append(f"{len(watch)} on Watch ({', '.join(_nm(r.name) for r in watch)})")
        parts.append(f"Of {len(rows)} measures, {' and '.join(bits)}; the rest are normal.")
    fed = rows.get("Fed funds rate")
    if fed and fed.loaded:
        parts.append(f"The Fed's target range is {fed.reading}; {fed.signal.replace(' · ', ', ')}.")
    gap = rows.get("Treasuries vs. bank deposits")
    if gap and gap.loaded:
        pts = gap.reading.lstrip("+").replace(" pts", "")
        parts.append(f"Treasury bills pay {pts} points more than the average bank money market account: "
                     "what a depositor gives up by keeping cash in a bank.")
    week = [f for f in rep.recent_failures if (rep.today - f.fail_date).days <= 7]
    if week:
        parts.append(f"A bank failed in the past week: {week[0].name} ({week[0].city_state}).")
    elif rep.failures_ytd is not None:
        n = rep.failures_ytd
        parts.append(f"{n} bank {'failure' if n == 1 else 'failures'} so far in {rep.today.year}, none in the past week.")
    return " ".join(parts)


def write_summary(rep: "Report") -> tuple[str, str]:
    """(paragraph, who wrote it: 'Claude' or 'rules')."""
    global STATUS
    key = os.environ.get("ANTHROPIC_API_KEY", "").strip()
    if not key:
        STATUS = "no ANTHROPIC_API_KEY; rule-based summary used"
    elif rep.sample:
        STATUS = "sample data; rule-based summary used"
    if key and not rep.sample:
        text = claude_summary(rep, key)
        if text:
            return text, "Claude"
    return rule_summary(rep), "rules"
