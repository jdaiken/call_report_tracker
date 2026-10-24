"""Shared email pieces: the data bundle, the page shell, and Gmail sending
(the same Gmail app-password setup as the FFIEC monitor)."""
from __future__ import annotations

import html
import os
import smtplib
from dataclasses import dataclass, field
from datetime import date
from email.mime.application import MIMEApplication
from email.mime.image import MIMEImage
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from typing import Optional

from .analysis import Flag, Reading
from .extras import CalendarResult, FailureResult

INK, QUIET, LINE, ACCENT, WATCH, BG, PAGE = "#1f2328", "#5f6670", "#e3e5e8", "#1d5fbf", "#b3261e", "#ffffff", "#f4f5f7"
FONT = "-apple-system,Segoe UI,Helvetica,Arial,sans-serif"


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
    """Everything one run collected."""
    today: date
    readings: dict[str, Reading]
    flags: list[Flag]
    failures: FailureResult = field(default_factory=FailureResult)
    calendar: CalendarResult = field(default_factory=CalendarResult)
    used_api: bool = False
    sample: bool = False


def _esc(s: str) -> str:
    return html.escape(s, quote=True)


def missing_note(b: Bundle) -> str:
    missing = [r.name for r in b.readings.values() if not r.ok]
    for label, err in (("bank failures (FDIC)", b.failures.error), ("data release calendar", b.calendar.error)):
        if err:
            missing.append(label)
    if not missing:
        return ""
    return (f'<p style="margin:0 0 8px;font-size:12px;color:{QUIET}">Did not load this week: {_esc(", ".join(missing))}. '
            "These retry automatically on the next run.</p>")


def page(kicker: str, title: str, inner: str, b: Bundle) -> str:
    banner = ""
    if b.sample:
        banner = ('<div style="background:#fff4ce;color:#5c4400;padding:10px 16px;font:14px sans-serif;text-align:center">'
                  "SAMPLE DATA FOR PREVIEW ONLY. These numbers are made up, not real rates.</div>")
    warn = ""
    if b.calendar.warning:
        warn = f'<p style="margin:0 0 8px;font-size:12px;color:{WATCH}">{_esc(b.calendar.warning)}</p>'
    source_note = "FRED API" if b.used_api else "FRED public downloads"
    return f"""<!doctype html><html><body style="margin:0;padding:0;background:{PAGE}">{banner}
<table role="presentation" width="100%" cellpadding="0" cellspacing="0" style="background:{PAGE}"><tr><td align="center" style="padding:20px 10px">
<table role="presentation" width="680" cellpadding="0" cellspacing="0" style="max-width:680px;width:100%;background:{BG};border-radius:8px;font-family:{FONT}">
<tr><td style="padding:24px 28px 0">
  <div style="font-size:12px;letter-spacing:.06em;text-transform:uppercase;color:{QUIET}">{_esc(kicker)}</div>
  <h1 style="margin:4px 0 0;font-size:22px;color:{INK}">{_esc(title)}</h1>
</td></tr>
<tr><td style="padding:0 28px 12px">{inner}</td></tr>
<tr><td style="padding:16px 28px 26px;border-top:1px solid {LINE}">
  {warn}{missing_note(b)}
  <p style="margin:0;font-size:12px;line-height:1.5;color:{QUIET}">Data: FRED (Federal Reserve Bank of St. Louis), republishing the
  Federal Reserve, New York Fed, Treasury, FDIC, ICE BofA, S&amp;P Dow Jones and Nasdaq, via {source_note}; FDIC BankFind (bank failures);
  federalreserve.gov (meeting dates). Automated summary for personal reference, not investment advice.</p>
</td></tr>
</table></td></tr></table></body></html>"""


def send_email(config: EmailConfig, subject: str, text_body: str, html_body: Optional[str],
               images: Optional[dict[str, bytes]] = None,
               attachments: Optional[list[tuple[str, bytes, str]]] = None) -> None:
    """`images` maps a content-id to PNG bytes; the HTML references them as
    <img src="cid:...">, so charts show inline. `attachments` are
    (filename, bytes, subtype) files attached at the end, e.g. the PDF."""
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
    if attachments:
        outer = MIMEMultipart("mixed")
        outer.attach(msg)
        for filename, data, subtype in attachments:
            part = MIMEApplication(data, _subtype=subtype)
            part.add_header("Content-Disposition", "attachment", filename=filename)
            outer.attach(part)
        msg = outer
    msg["Subject"] = subject
    msg["From"] = config.address
    msg["To"] = config.recipient
    with smtplib.SMTP_SSL("smtp.gmail.com", 465) as server:
        server.login(config.address, config.app_password)
        server.send_message(msg)
