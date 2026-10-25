"""The designed daily email + PDF (ffiec_monitor/report.py, pdf_report.py, narrative.py)."""
import re
from datetime import date
from unittest import mock

from ffiec_monitor import alerts, db, email_report, narrative, ratios
from ffiec_monitor.watch_list import WatchListChange

RUN = date(2026, 10, 26)
Q3 = date(2026, 9, 30)


def _meta(i, assets, name=None):
    return db.InstitutionMeta(id_rssd=str(100000 + i), name=name or f"Community Bank {i}", city="Springfield",
                              state="IL", fdic_cert=str(9000 + i), total_assets=assets, as_of_period=Q3)


def sample_events(n_refilings=55, n_new=80):
    big = _meta(1, 2_400_000, "First Prairie Bank")
    triggers = alerts.classify_new_period({"tier1_leverage_ratio": 5.8, "total_rbc_ratio": 11.2, "roaa": 0.15},
                                          {"tier1_leverage_ratio": 4.6, "total_rbc_ratio": 11.0, "roaa": -0.20},
                                          [], None, None)
    t1 = [t for t in triggers if t.tier == alerts.TIER1]
    t2 = [t for t in triggers if t.tier == alerts.TIER2] or t1[:1]
    snap = ratios.full_snapshot_rows({"tier1_leverage_ratio": 4.6, "total_rbc_ratio": 11.0, "roaa": -0.2})
    return dict(
        watch_list_changes=[email_report.WatchListChangeEvent(big, WatchListChange(big.id_rssd, "ADDED", "Leverage below 5%"))],
        tier1_events=[email_report.Tier1Event(big, Q3, t1, True, snap)],
        watch_list_snapshot=[email_report.WatchListSnapshotEntry(big, snap)],
        tier2_events=[email_report.Tier2Event(_meta(2, 800_000), t2)],
        restatement_events=[email_report.RestatementEvent(_meta(10 + i, 50_000 + i * 1000), date(2026, 6, 30),
                                                          "Net Interest Margin 3.10% to 3.05%") for i in range(n_refilings)],
        new_filing_events=[email_report.NewFilingEvent(_meta(200 + i, 30_000 + i * 2000), Q3) for i in range(n_new)],
        run_date=RUN,
    )


def test_compose_renders_every_section_with_links(monkeypatch):
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    rep, html, pdf = email_report.compose(**sample_events())
    assert rep.subject == "FFIEC Call Report Monitor · Mon Oct 26 · 1 Tier 1 alert"
    assert rep.summary_by == "rules" and rep.summary.startswith("Tier 1 alert for First Prairie Bank")
    for title in ("Watch List changes", "Tier 1 alerts", "Watch List: current ratios", "Tier 2", "Refilings",
                  "New quarterly filings", "WHAT\'S GOING ON"):
        assert title in html, title
    # long lists: largest first, capped, with a pointer to the CSV
    assert rep.refilings_total == 55 and len(rep.refilings) == 40
    assert rep.refilings[0].bank.assets_raw >= rep.refilings[-1].bank.assets_raw
    assert "Showing the 40 largest of 55" in html
    # every institution links to its Call Report and FDIC profile, in the email and the PDF
    assert "cdr.ffiec.gov/public/ViewFacsimileDirect.aspx" in html
    assert "banks.data.fdic.gov/bankfind-suite/bankfind?cert=9001" in html
    assert pdf[:4] == b"%PDF" and b"bankfind?cert=9001" in pdf and b"ViewFacsimileDirect" in pdf
    assert len(re.findall(rb"/Type /Page[^s]", pdf)) >= 2


def test_quiet_day_summary_and_subject(monkeypatch):
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    ev = sample_events(3, 2)
    ev.update(watch_list_changes=[], tier1_events=[], watch_list_snapshot=[], tier2_events=[])
    rep, html, pdf = email_report.compose(**ev)
    assert rep.subject.endswith("3 refilings, 2 new filings")
    assert rep.summary.startswith("No alerts today")
    assert "Tier 1 alerts" not in html.split("WHAT")[1]          # empty sections are left out
    assert len(re.findall(rb"/Type /Page[^s]", pdf)) == 1


def test_claude_summary_is_grounded(monkeypatch):
    monkeypatch.setenv("ANTHROPIC_API_KEY", "key")
    ev = sample_events(3, 2)

    def reply(text):
        r = mock.Mock(status_code=200)
        r.json.return_value = {"content": [{"type": "text", "text": text}]}
        return r

    good = "First Prairie Bank crossed a Tier 1 threshold; its assets are $2.4B."
    with mock.patch.object(narrative.requests, "post", return_value=reply(good)):
        rep, html, _ = email_report.compose(**ev)
    assert rep.summary == good and rep.summary_by == "Claude" and "Written by Claude" in html

    # a number that is not in the data is rejected (after one retry) and the rule-based text is used
    with mock.patch.object(narrative.requests, "post", return_value=reply("Deposits fell 37.5% overnight.")) as post:
        rep, _, _ = email_report.compose(**ev)
    assert post.call_count == 2 and rep.summary_by == "rules"

    with mock.patch.object(narrative.requests, "post", side_effect=OSError("down")):
        rep, _, _ = email_report.compose(**ev)
    assert rep.summary_by == "rules" and "API call failed" in narrative.STATUS


def test_send_email_attaches_csv_and_pdf():
    from ffiec_monitor.config import EmailConfig
    cfg = EmailConfig.__new__(EmailConfig)
    for k, v in (("address", "a@example.com"), ("app_password", "x"), ("recipient", "a@example.com")):
        object.__setattr__(cfg, k, v)
    with mock.patch.object(email_report.smtplib, "SMTP_SSL") as smtp:
        email_report.send_email(cfg, "s", "body", html_body="<p>x</p>", attachment_filename="c.csv",
                                attachment_content="a,b\n", attachments=[("r.pdf", b"%PDF-1.4", "pdf")])
    msg = smtp.return_value.__enter__.return_value.send_message.call_args[0][0]
    names = [p.get_filename() for p in msg.walk() if p.get_filename()]
    assert names == ["c.csv", "r.pdf"]
