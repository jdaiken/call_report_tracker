"""Render the digest (HTML + plain text) and send it the same way the FFIEC
monitor does: Gmail SMTP with an app password."""
from __future__ import annotations

import html
import os
import smtplib
from dataclasses import dataclass
from datetime import date
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from typing import Optional

from .analysis import Flag, Reading, notable_moves
from .fetch import Headline
from .series import SECTIONS

BLOCKS = "▁▂▃▄▅▆▇█"
INK, QUIET, LINE, ACCENT, WATCH, BG = "#1f2328", "#5f6670", "#e3e5e8", "#1d5fbf", "#b3261e", "#ffffff"


@dataclass(frozen=True)
class EmailConfig:
    address: str
    app_password: str
    recipient: str

    @classmethod
    def from_env(cls) -> "EmailConfig":
        def need(name: str) -> str:
            v = os.environ.get(name, "").strip()
            if not v:
                raise RuntimeError(f"Required environment variable {name!r} is not set.")
            return v
        address = need("EMAIL_ADDRESS")
        return cls(address, need("EMAIL_APP_PASSWORD"), os.environ.get("ALERT_RECIPIENT", "").strip() or address)


def sparkline(history: list[tuple[date, float]], width: int = 16) -> str:
    """A one-line text chart of the past year. Text, not an image, so every
    email client (including Outlook and Gmail) shows it."""
    if len(history) < 3:
        return ""
    vals = [v for _, v in history]
    step = max(1, len(vals) // width)
    sampled = vals[::step][-width:]
    if sampled[-1] != vals[-1]:
        sampled = sampled[1:] + [vals[-1]]
    lo, hi = min(sampled), max(sampled)
    if hi - lo < 1e-9:
        return BLOCKS[3] * len(sampled)
    return "".join(BLOCKS[min(7, int((v - lo) / (hi - lo) * 7.999))] for v in sampled)


def headline_for(flags: list[Flag], moves: list[Reading]) -> str:
    watch = [f for f in flags if f.level == "watch"]
    if watch:
        first = watch[0].text.split(". ")[0].rstrip(".")
        return first if len(first) <= 80 else first[:77] + "..."
    if moves:
        m = moves[0]
        return f"{m.name} {m.change_text('month')} this month"
    return "A quiet day for rates"


def _pct_move(r: Optional[Reading]) -> Optional[str]:
    """'Nasdaq down 1.2%' style move since the previous trading day."""
    if not r or not r.ok or r.stale or r.changes.get("last") is None or not r.bases.get("last"):
        return None
    pct = r.changes["last"] / r.bases["last"] * 100
    if abs(pct) < 0.05:
        return "flat"
    return f"{'up' if pct > 0 else 'down'} {abs(pct):.1f}%"


def market_snapshot(readings: dict[str, Reading]) -> Optional[str]:
    """One line on how stocks and the 10-year moved on the latest trading day."""
    parts = []
    for key, label in (("SP500", "S&P 500"), ("DJIA", "Dow"), ("NASDAQCOM", "Nasdaq")):
        mv = _pct_move(readings.get(key))
        if mv:
            parts.append(f"{label} {mv}")
    ten = readings.get("DGS10")
    if ten and ten.ok and not ten.stale and ten.changes.get("last") is not None:
        parts.append(f"10-year Treasury {ten.display_value} ({ten.change_text('last')})")
    if not parts:
        return None
    day = next((readings[k].as_of for k in ("SP500", "DJIA", "NASDAQCOM", "DGS10")
                if readings.get(k) and readings[k].ok), None)
    when = f"on {day:%A}" if day else "on the latest trading day"
    return f"Markets {when}: " + ", ".join(parts) + "."


def thirty_seconds(flags: list[Flag], moves: list[Reading], readings: dict[str, Reading]) -> list[tuple[str, str]]:
    """(level, text) bullets for the top of the email."""
    out: list[tuple[str, str]] = []
    top, bottom = readings.get("DFEDTARU"), readings.get("DFEDTARL")
    if top and bottom and top.ok and bottom.ok:
        out.append(("info", f"The Fed's target range is {bottom.value:.2f}% to {top.value:.2f}%."))
    snap = market_snapshot(readings)
    if snap:
        out.append(("info", snap))
    for f in flags:
        out.append((f.level, f.text))
    for m in moves:
        if any(m.name in t for _, t in out):
            continue
        out.append(("move", f"{m.name} is {m.display_value}, {m.change_text('month')} over the past month."))
    if len(out) <= 2:
        out.append(("info", "No unusual moves today. Rates and markets are within their normal recent ranges."))
    return out[:9]


# --------------------------------------------------------------------- HTML
def _esc(s: str) -> str:
    return html.escape(s, quote=True)


def _reading_html(r: Reading) -> str:
    fred = f"https://fred.stlouisfed.org/series/{r.key}" if r.kind != "spread" else None
    name = f'<a href="{fred}" style="color:{INK};text-decoration:none">{_esc(r.name)}</a>' if fred else _esc(r.name)
    as_of = f"as of {r.as_of:%b %-d}" if r.as_of else ""
    spark = sparkline(r.history)
    value_color = WATCH if r.stale or not r.ok else INK
    cells = ""
    for label, w in (("Last reading", "last"), ("1 month", "month"), ("1 year", "year")):
        cells += (f'<td style="padding:2px 14px 2px 0;font-size:12px;color:{QUIET};white-space:nowrap">'
                  f'{label}: <span style="color:{INK}">{_esc(r.change_text(w))}</span></td>')
    return f"""
<tr><td style="padding:14px 0 12px;border-top:1px solid {LINE}">
  <table role="presentation" width="100%" cellpadding="0" cellspacing="0"><tr>
    <td style="font-size:15px;font-weight:600;color:{INK}">{name}</td>
    <td align="right" style="font-size:17px;font-weight:600;color:{value_color};white-space:nowrap">{_esc(r.display_value)}</td>
  </tr><tr>
    <td style="font-size:12px;color:{QUIET}">{_esc(as_of)}</td>
    <td align="right" style="font-size:13px;color:{ACCENT};letter-spacing:1px;white-space:nowrap" title="Past year">{_esc(spark)}</td>
  </tr></table>
  <table role="presentation" cellpadding="0" cellspacing="0" style="margin-top:6px"><tr>{cells}</tr></table>
  <p style="margin:8px 0 6px;font-size:14px;line-height:1.5;color:{INK}">{_esc(r.summary())}</p>
  <p style="margin:0;font-size:12.5px;line-height:1.5;color:{QUIET}"><b>What it is:</b> {_esc(r.what_it_is)}
     <b>Why it matters:</b> {_esc(r.why_it_matters)}</p>
</td></tr>"""


def render_html(run_date: date, readings: dict[str, Reading], flags: list[Flag],
                headlines: list[Headline], feed_errors: list[str], used_api: bool) -> str:
    moves = notable_moves(readings, covered=frozenset(k for f in flags for k in f.keys))
    bullets = thirty_seconds(flags, moves, readings)
    bullet_html = "".join(
        f'<li style="margin:0 0 8px;color:{WATCH if lvl == "watch" else INK}">'
        f'{"<b>Watch:</b> " if lvl == "watch" else ""}{_esc(t)}</li>'
        for lvl, t in bullets)

    body_sections = ""
    for key, title, intro in SECTIONS:
        rows = [r for r in readings.values() if r.section == key]
        if not rows:
            continue
        body_sections += f"""
<tr><td style="padding:26px 0 4px">
  <h2 style="margin:0;font-size:18px;color:{INK}">{_esc(title)}</h2>
  <p style="margin:4px 0 0;font-size:13px;line-height:1.5;color:{QUIET}">{_esc(intro)}</p>
</td></tr>""" + "".join(_reading_html(r) for r in rows)

    if headlines:
        items = "".join(
            f'<li style="margin:0 0 8px"><a href="{_esc(h.link)}" style="color:{ACCENT}">{_esc(h.title)}</a>'
            f' <span style="color:{QUIET};font-size:12px">({_esc(h.feed)}, {h.published:%b %-d})</span></li>'
            for h in headlines[:10])
    else:
        items = f'<li style="color:{QUIET}">No Fed monetary-policy or bank-regulation releases in the past 7 days.</li>'
    if feed_errors:
        items += f'<li style="color:{QUIET}">Could not reach: {_esc(", ".join(feed_errors))}.</li>'

    missing = [r for r in readings.values() if not r.ok]
    missing_html = ""
    if missing:
        missing_html = (f'<p style="font-size:12px;color:{QUIET}">Did not load today: '
                        f'{_esc(", ".join(r.name for r in missing))}. These retry automatically on the next run.</p>')

    source_note = "FRED API" if used_api else "FRED public downloads"
    return f"""<!doctype html><html><body style="margin:0;padding:0;background:#f4f5f7">
<table role="presentation" width="100%" cellpadding="0" cellspacing="0" style="background:#f4f5f7"><tr><td align="center" style="padding:20px 10px">
<table role="presentation" width="640" cellpadding="0" cellspacing="0" style="max-width:640px;width:100%;background:{BG};border-radius:8px;font-family:-apple-system,Segoe UI,Helvetica,Arial,sans-serif">
<tr><td style="padding:24px 28px 8px">
  <div style="font-size:12px;letter-spacing:.06em;text-transform:uppercase;color:{QUIET}">Rates &amp; Markets Daily</div>
  <h1 style="margin:4px 0 0;font-size:22px;color:{INK}">{run_date:%A, %B %-d, %Y}</h1>
</td></tr>
<tr><td style="padding:8px 28px 0">
  <h2 style="margin:12px 0 8px;font-size:16px;color:{INK}">Today in 30 seconds</h2>
  <ul style="margin:0;padding-left:20px;font-size:14px;line-height:1.5">{bullet_html}</ul>
  <p style="margin:12px 0 0;font-size:12px;line-height:1.5;color:{QUIET}">How to read this: "points" means percentage points, so 0.25 points is 25 basis points.
  The small bar chart next to each number shows the past year. Daily rates usually reflect the prior business day.</p>
</td></tr>
<tr><td style="padding:0 28px"><table role="presentation" width="100%" cellpadding="0" cellspacing="0">{body_sections}
<tr><td style="padding:26px 0 4px"><h2 style="margin:0;font-size:18px;color:{INK}">From the Fed this week</h2></td></tr>
<tr><td style="padding:8px 0;font-size:14px;line-height:1.5"><ul style="margin:0;padding-left:20px">{items}</ul></td></tr>
<tr><td style="padding:22px 0 4px"><h2 style="margin:0;font-size:16px;color:{INK}">Not covered here</h2>
  <p style="margin:6px 0 0;font-size:13px;line-height:1.5;color:{QUIET}">FHLB advance rates, a key alternative to one-way placements, are published on each
  Federal Home Loan Bank's member rate sheet and have no free public feed. Money market fund yields come from paid sources such as Crane Data.</p></td></tr>
</table></td></tr>
<tr><td style="padding:16px 28px 26px;border-top:1px solid {LINE}">
  {missing_html}
  <p style="margin:0;font-size:12px;line-height:1.5;color:{QUIET}">Data: Federal Reserve Bank of St. Louis (FRED), republishing the Federal Reserve,
  New York Fed, U.S. Treasury, FDIC, Freddie Mac and ICE BofA, via {source_note}. Headlines: federalreserve.gov RSS. Click a rate's name for its full history.
  This is an automated summary for personal reference, not investment advice.</p>
</td></tr>
</table></td></tr></table></body></html>"""


# --------------------------------------------------------------- plain text
def render_text(run_date: date, readings: dict[str, Reading], flags: list[Flag],
                headlines: list[Headline]) -> str:
    moves = notable_moves(readings, covered=frozenset(k for f in flags for k in f.keys))
    lines = [f"RATES & MARKETS DAILY - {run_date:%A, %B %-d, %Y}", "", "TODAY IN 30 SECONDS"]
    for lvl, t in thirty_seconds(flags, moves, readings):
        lines.append(f"  - {'WATCH: ' if lvl == 'watch' else ''}{t}")
    lines += ["", '("points" = percentage points; 0.25 points = 25 basis points)']
    for key, title, _intro in SECTIONS:
        rows = [r for r in readings.values() if r.section == key]
        if not rows:
            continue
        lines += ["", title.upper(), "-" * len(title)]
        for r in rows:
            as_of = f" (as of {r.as_of:%b %-d})" if r.as_of else ""
            lines.append(f"{r.name}: {r.display_value}{as_of}  {sparkline(r.history)}")
            lines.append(f"  {r.summary()}")
            lines.append(f"  What it is: {r.what_it_is}")
            lines.append(f"  Why it matters: {r.why_it_matters}")
    lines += ["", "FROM THE FED THIS WEEK"]
    if headlines:
        lines += [f"  - {h.title} ({h.feed}, {h.published:%b %-d}) {h.link}" for h in headlines[:10]]
    else:
        lines.append("  No Fed monetary-policy or bank-regulation releases in the past 7 days.")
    lines += ["", "Data: FRED (Federal Reserve Bank of St. Louis). Not investment advice."]
    return "\n".join(lines)


def send_email(config: EmailConfig, subject: str, text_body: str, html_body: Optional[str]) -> None:
    alt = MIMEMultipart("alternative")
    alt.attach(MIMEText(text_body, "plain", "utf-8"))
    if html_body:
        alt.attach(MIMEText(html_body, "html", "utf-8"))
    alt["Subject"] = subject
    alt["From"] = config.address
    alt["To"] = config.recipient
    with smtplib.SMTP_SSL("smtp.gmail.com", 465) as server:
        server.login(config.address, config.app_password)
        server.send_message(alt)
