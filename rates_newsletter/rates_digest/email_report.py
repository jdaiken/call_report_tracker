"""The short weekday email, plus pieces shared with the weekly look book
(the data bundle, summary bullets, extra flags and Gmail sending)."""
from __future__ import annotations

import html
import os
import smtplib
from dataclasses import dataclass, field
from datetime import date, timedelta
from email.mime.image import MIMEImage
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from typing import Optional

from .analysis import Flag, Reading, notable_today
from .extras import (CalendarResult, FailureResult, RegionalBanks, StablecoinResult,
                     fmt_thousands)
from .fetch import Headline
from .series import SECTIONS

BLOCKS = "▁▂▃▄▅▆▇█"
INK, QUIET, LINE, ACCENT, WATCH, BG, PAGE = "#1f2328", "#5f6670", "#e3e5e8", "#1d5fbf", "#b3261e", "#ffffff", "#f4f5f7"
FONT = "-apple-system,Segoe UI,Helvetica,Arial,sans-serif"

KEY_NUMBERS = ["EFFR", "SOFR", "DGS3MO", "DGS10", "MMNDR", "NDR12MCD",
               "DPSSCBW027SBOG", "SP500", "DJIA", "NASDAQCOM", "VIXCLS", "BAMLH0A0HYM2"]
SHORT_NAMES = {
    "EFFR": "Fed funds (effective)", "DGS3MO": "3-month T-bill", "DGS10": "10-year Treasury",
    "MMNDR": "Bank money market (avg)", "NDR12MCD": "12-month CD (avg)",
    "DPSSCBW027SBOG": "Small-bank deposits", "SP500": "S&P 500", "DJIA": "Dow",
    "NASDAQCOM": "Nasdaq", "VIXCLS": "VIX", "BAMLH0A0HYM2": "High-yield spread",
}


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


@dataclass
class Bundle:
    """Everything one run collected. Both emails render from this."""
    today: date
    readings: dict[str, Reading]
    flags: list[Flag]
    headlines: list[Headline] = field(default_factory=list)
    feed_errors: list[str] = field(default_factory=list)
    failures: FailureResult = field(default_factory=FailureResult)
    stablecoins: StablecoinResult = field(default_factory=StablecoinResult)
    calendar: CalendarResult = field(default_factory=CalendarResult)
    regional: RegionalBanks = field(default_factory=RegionalBanks)
    used_api: bool = False
    sample: bool = False

    @property
    def covered(self) -> frozenset[str]:
        return frozenset(k for f in self.flags for k in f.keys)


# ------------------------------------------------------------ small helpers
def _esc(s: str) -> str:
    return html.escape(s, quote=True)


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


def _pct_move(r: Optional[Reading]) -> Optional[str]:
    """'down 1.2%' since the previous trading day."""
    if not r or not r.ok or r.stale or r.changes.get("last") is None or not r.bases.get("last"):
        return None
    pct = r.changes["last"] / r.bases["last"] * 100
    if abs(pct) < 0.05:
        return "flat"
    return f"{'up' if pct > 0 else 'down'} {abs(pct):.1f}%"


def _signed_pct(v: Optional[float]) -> str:
    if v is None:
        return "n/a"
    if abs(v) < 0.05:
        return "flat"
    return f"{'up' if v > 0 else 'down'} {abs(v):.1f}%"


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


def regional_line(rb: RegionalBanks) -> Optional[str]:
    if not rb.ok:
        return None
    avg, down = rb.cap_weighted_change, rb.share_down
    worst = rb.worst(1)
    tail = f" Biggest decline: {worst[0].company} ({worst[0].change_pct:+.1f}%)." if worst and worst[0].change_pct < -2 else ""
    return (f"Regional banks: {_signed_pct(avg)} on average (weighted by size); "
            f"{down:.0f}% of {len(rb.quotes)} banks fell.{tail}")


def extra_flags(b: Bundle, window_days: int = 7) -> list[Flag]:
    """Watch flags from the non-FRED sources."""
    out: list[Flag] = []
    for f in b.failures.failures:
        if (b.today - f.fail_date).days <= window_days:
            out.append(Flag("watch",
                f"The FDIC closed {f.name} ({f.city_state}) on {f.fail_date:%b %-d}, with "
                f"{fmt_thousands(f.deposits_thousands)} in deposits. Check whether it was in IntraFi's network."))
    rb = b.regional
    if rb.ok and rb.cap_weighted_change is not None:
        sp = b.readings.get("SP500")
        sp_pct = (sp.changes["last"] / sp.bases["last"] * 100
                  if sp and sp.ok and sp.changes.get("last") is not None and sp.bases.get("last") else None)
        avg = rb.cap_weighted_change
        if avg <= -3:
            out.append(Flag("watch", f"Regional bank stocks fell {abs(avg):.1f}% on average, a stressed day for the sector."))
        elif sp_pct is not None and avg - sp_pct <= -2:
            out.append(Flag("watch", f"Regional bank stocks fell {abs(avg):.1f}% while the S&P 500 was {_signed_pct(sp_pct)}. "
                                     "Investors are singling out banks."))
    return out


def headline_for(flags: list[Flag], fallback: str = "A quiet day for rates") -> str:
    watch = [f for f in flags if f.level == "watch"]
    if watch:
        first = watch[0].text.split(". ")[0].rstrip(".")
        return first if len(first) <= 80 else first[:77] + "..."
    return fallback


def summary_bullets(b: Bundle, all_flags: list[Flag], max_flags: int = 5) -> list[tuple[str, str]]:
    out: list[tuple[str, str]] = []
    top, bottom = b.readings.get("DFEDTARU"), b.readings.get("DFEDTARL")
    if top and bottom and top.ok and bottom.ok:
        out.append(("info", f"The Fed's target range is {bottom.value:.2f}% to {top.value:.2f}%."))
    snap = market_snapshot(b.readings)
    if snap:
        out.append(("info", snap))
    rl = regional_line(b.regional)
    if rl:
        out.append(("info", rl))
    ordered = [f for f in all_flags if f.level == "watch"] + [f for f in all_flags if f.level != "watch"]
    for f in ordered[:max_flags]:
        out.append((f.level, f.text))
    if not all_flags:
        out.append(("info", "No warning signs today. Rates, deposits and markets are within their normal recent ranges."))
    return out


def _bullets_html(bullets: list[tuple[str, str]]) -> str:
    return "".join(
        f'<li style="margin:0 0 8px;color:{WATCH if lvl == "watch" else INK}">'
        f'{"<b>Watch:</b> " if lvl == "watch" else ""}{_esc(t)}</li>' for lvl, t in bullets)


def _h2(title: str, sub: str = "") -> str:
    s = f'<h2 style="margin:26px 0 6px;font-size:17px;color:{INK}">{_esc(title)}</h2>'
    if sub:
        s += f'<p style="margin:0 0 8px;font-size:13px;line-height:1.5;color:{QUIET}">{_esc(sub)}</p>'
    return s


def _td(content: str, align: str = "left", color: str = INK, bold: bool = False, mono: bool = False) -> str:
    style = (f"padding:6px 6px;border-bottom:1px solid {LINE};font-size:13px;color:{color};text-align:{align};"
             f"{'font-weight:600;' if bold else ''}{'letter-spacing:0;font-size:11px;' if mono else ''}"
             f"{'white-space:nowrap' if align == 'right' else ''}")
    return f'<td style="{style}">{content}</td>'


def _th(label: str, align: str = "left") -> str:
    return (f'<th style="padding:6px 6px;border-bottom:2px solid {LINE};font-size:11px;font-weight:600;'
            f'color:{QUIET};text-align:{align};text-transform:uppercase;letter-spacing:.04em">{_esc(label)}</th>')


def key_numbers_table(readings: dict[str, Reading]) -> str:
    rows = ""
    for k in KEY_NUMBERS:
        r = readings.get(k)
        if not r:
            continue
        name = SHORT_NAMES.get(k, r.name)
        rows += ("<tr>" + _td(_esc(name)) + _td(_esc(r.display_value), "right", WATCH if not r.ok or r.stale else INK, True)
                 + _td(_esc(r.compact_change("last")), "right", QUIET) + _td(_esc(r.compact_change("month")), "right", QUIET)
                 + _td(_esc(sparkline(r.history, 10)), "right", ACCENT, mono=True) + "</tr>")
    return (f'<table role="presentation" width="100%" cellpadding="0" cellspacing="0" style="border-collapse:collapse">'
            f"<tr>{_th('')}{_th('Today', 'right')}{_th('Since last', 'right')}{_th('1 month', 'right')}{_th('Past year', 'right')}</tr>"
            f"{rows}</table>")


def all_rates_table(readings: dict[str, Reading]) -> str:
    out = ""
    for key, title, _ in SECTIONS:
        rows = [r for r in readings.values() if r.section == key]
        if not rows:
            continue
        out += (f'<tr><td colspan="4" style="padding:14px 8px 4px;font-size:12px;font-weight:600;color:{INK}">'
                f"{_esc(title)}</td></tr>")
        for r in rows:
            as_of = f"{r.as_of:%b %-d}" if r.as_of else ""
            out += ("<tr>" + _td(_esc(r.name), color=INK) + _td(_esc(r.display_value), "right", WATCH if not r.ok or r.stale else INK)
                    + _td(_esc(r.compact_change("last")), "right", QUIET) + _td(_esc(as_of), "right", QUIET) + "</tr>")
    return (f'<table role="presentation" width="100%" cellpadding="0" cellspacing="0" style="border-collapse:collapse">'
            f"<tr>{_th('')}{_th('Latest', 'right')}{_th('Since last', 'right')}{_th('As of', 'right')}</tr>{out}</table>")


def calendar_html(cal: CalendarResult, today: date) -> str:
    if not cal.events:
        return f'<p style="margin:0;font-size:13px;color:{QUIET}">No major Fed meetings or data releases in the next week.</p>'
    items = "".join(
        f'<li style="margin:0 0 6px"><b>{e.when:%a %b %-d}</b> &middot; {_esc(e.label)}</li>' for e in cal.events)
    return f'<ul style="margin:0;padding-left:20px;font-size:14px;line-height:1.5;color:{INK}">{items}</ul>'


def headlines_html(headlines: list[Headline], feed_errors: list[str]) -> str:
    items = "".join(
        f'<li style="margin:0 0 8px"><a href="{_esc(h.link)}" style="color:{ACCENT}">{_esc(h.title)}</a>'
        f' <span style="color:{QUIET};font-size:12px">({_esc(h.feed)}, {h.published:%b %-d})</span></li>'
        for h in headlines[:10])
    if feed_errors:
        items += f'<li style="color:{QUIET}">Could not reach: {_esc(", ".join(feed_errors))}.</li>'
    return f'<ul style="margin:0;padding-left:20px;font-size:14px;line-height:1.5">{items}</ul>'


def missing_note(b: Bundle) -> str:
    missing = [r.name for r in b.readings.values() if not r.ok]
    for label, err in (("bank failures (FDIC)", b.failures.error), ("stablecoin supply", b.stablecoins.error),
                       ("regional bank stocks (Finviz)", b.regional.error),
                       ("data release calendar", b.calendar.error)):
        if err:
            missing.append(label)
    if not missing:
        return ""
    return (f'<p style="margin:0 0 8px;font-size:12px;color:{QUIET}">Did not load today: {_esc(", ".join(missing))}. '
            "These retry automatically on the next run.</p>")


def page(title_kicker: str, title: str, inner: str, b: Bundle, footer_extra: str = "") -> str:
    banner = ""
    if b.sample:
        banner = ('<div style="background:#fff4ce;color:#5c4400;padding:10px 16px;font:14px sans-serif;text-align:center">'
                  "SAMPLE DATA FOR PREVIEW ONLY. These numbers are made up, not real rates.</div>")
    source_note = "FRED API" if b.used_api else "FRED public downloads"
    warn = ""
    if b.calendar.warning:
        warn = f'<p style="margin:0 0 8px;font-size:12px;color:{WATCH}">{_esc(b.calendar.warning)}</p>'
    return f"""<!doctype html><html><body style="margin:0;padding:0;background:{PAGE}">{banner}
<table role="presentation" width="100%" cellpadding="0" cellspacing="0" style="background:{PAGE}"><tr><td align="center" style="padding:20px 10px">
<table role="presentation" width="680" cellpadding="0" cellspacing="0" style="max-width:680px;width:100%;background:{BG};border-radius:8px;font-family:{FONT}">
<tr><td style="padding:24px 28px 0">
  <div style="font-size:12px;letter-spacing:.06em;text-transform:uppercase;color:{QUIET}">{_esc(title_kicker)}</div>
  <h1 style="margin:4px 0 0;font-size:22px;color:{INK}">{_esc(title)}</h1>
</td></tr>
<tr><td style="padding:0 28px 8px">{inner}</td></tr>
<tr><td style="padding:16px 28px 26px;border-top:1px solid {LINE}">
  {warn}{missing_note(b)}
  <p style="margin:0;font-size:12px;line-height:1.5;color:{QUIET}">{footer_extra}Data: FRED (Federal Reserve Bank of St. Louis), republishing the Federal Reserve,
  New York Fed, Treasury, FDIC, Freddie Mac, ICE BofA and Dow Jones/Nasdaq, via {source_note}; FDIC BankFind (bank failures);
  DeFiLlama (stablecoins); Finviz (regional bank stocks, unofficial); federalreserve.gov (headlines, meeting dates).
  "Points" means percentage points (0.25 points = 25 basis points). Automated summary for personal reference, not investment advice.</p>
</td></tr>
</table></td></tr></table></body></html>"""


# ------------------------------------------------------------- daily email
def render_daily_html(b: Bundle) -> str:
    flags = b.flags + extra_flags(b)
    moved = notable_today(b.readings, b.today, covered=frozenset(k for f in flags for k in f.keys))
    inner = _h2("Today in 30 seconds") + f'<ul style="margin:0;padding-left:20px;font-size:14px;line-height:1.5">{_bullets_html(summary_bullets(b, flags))}</ul>'
    inner += _h2("Key numbers", "The small bars show the past year.") + key_numbers_table(b.readings)

    if moved:
        inner += _h2("What moved")
        for r in moved:
            inner += (f'<p style="margin:0 0 4px;font-size:14px;font-weight:600;color:{INK}">{_esc(r.name)}: {_esc(r.display_value)}</p>'
                      f'<p style="margin:0 0 12px;font-size:13.5px;line-height:1.5;color:{INK}">{_esc(r.summary())} '
                      f'<span style="color:{QUIET}">{_esc(r.why_it_matters)}</span></p>')

    recent_fail = [f for f in b.failures.failures if (b.today - f.fail_date).days <= 30]
    if recent_fail:
        inner += _h2("Bank failures in the past 30 days")
        inner += "<ul style=\"margin:0;padding-left:20px;font-size:14px;line-height:1.5\">" + "".join(
            f"<li>{f.fail_date:%b %-d}: {_esc(f.name)} ({_esc(f.city_state)}), {fmt_thousands(f.deposits_thousands)} in deposits</li>"
            for f in recent_fail) + "</ul>"

    inner += _h2("Coming up this week") + calendar_html(b.calendar, b.today)
    if b.headlines or b.feed_errors:
        inner += _h2("From the Fed this week") + headlines_html(b.headlines, b.feed_errors)
    inner += _h2("Everything else", "One line per rate. Full explanations and charts are in Monday's look book.") + all_rates_table(b.readings)
    return page("Rates & Markets Daily", f"{b.today:%A, %B %-d, %Y}", inner, b)


def render_daily_text(b: Bundle) -> str:
    flags = b.flags + extra_flags(b)
    lines = []
    if b.sample:
        lines.append("*** SAMPLE DATA FOR PREVIEW ONLY - NOT REAL RATES ***\n")
    lines += [f"RATES & MARKETS DAILY - {b.today:%A, %B %-d, %Y}", "", "TODAY IN 30 SECONDS"]
    lines += [f"  - {'WATCH: ' if lvl == 'watch' else ''}{t}" for lvl, t in summary_bullets(b, flags)]
    lines += ["", "KEY NUMBERS"]
    for k in KEY_NUMBERS:
        r = b.readings.get(k)
        if r:
            lines.append(f"  {SHORT_NAMES.get(k, r.name):26} {r.display_value:>16}  {r.change_text('last'):>20}  1 mo: {r.change_text('month')}")
    moved = notable_today(b.readings, b.today, covered=frozenset(k for f in flags for k in f.keys))
    if moved:
        lines += ["", "WHAT MOVED"] + [f"  {r.name}: {r.display_value}. {r.summary()}" for r in moved]
    lines += ["", "COMING UP THIS WEEK"]
    lines += [f"  {e.when:%a %b %-d}: {e.label}" for e in b.calendar.events] or ["  Nothing major scheduled."]
    if b.headlines:
        lines += ["", "FROM THE FED THIS WEEK"] + [f"  - {h.title} ({h.published:%b %-d}) {h.link}" for h in b.headlines[:10]]
    lines += ["", "EVERYTHING ELSE"]
    for key, title, _ in SECTIONS:
        rows = [r for r in b.readings.values() if r.section == key]
        if rows:
            lines.append(f"  {title}")
            lines += [f"    {r.name}: {r.display_value} ({r.change_text('last')})" for r in rows]
    lines += ["", "Data: FRED, FDIC, DeFiLlama, Finviz, federalreserve.gov. Not investment advice."]
    return "\n".join(lines)


def daily_subject(b: Bundle) -> str:
    return f"Rates & Markets Daily, {b.today:%a %b %-d}: {headline_for(b.flags + extra_flags(b))}"


# ------------------------------------------------------------------ sending
def send_email(config: EmailConfig, subject: str, text_body: str, html_body: Optional[str],
               images: Optional[dict[str, bytes]] = None) -> None:
    """`images` maps a content-id to PNG bytes; the HTML references them as
    <img src="cid:...">, so charts show inline without being attachments."""
    alt = MIMEMultipart("alternative")
    alt.attach(MIMEText(text_body, "plain", "utf-8"))
    if html_body:
        alt.attach(MIMEText(html_body, "html", "utf-8"))
    if images:
        msg = MIMEMultipart("related")
        msg.attach(alt)
        for cid, png in images.items():
            img = MIMEImage(png, "png")
            img.add_header("Content-ID", f"<{cid}>")
            img.add_header("Content-Disposition", "inline", filename=f"{cid}.png")
            msg.attach(img)
    else:
        msg = alt
    msg["Subject"] = subject
    msg["From"] = config.address
    msg["To"] = config.recipient
    with smtplib.SMTP_SSL("smtp.gmail.com", 465) as server:
        server.login(config.address, config.app_password)
        server.send_message(msg)
