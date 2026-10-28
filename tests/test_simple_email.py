from datetime import date, datetime

from ffiec_monitor import db, simple_email

PERIOD = date(2026, 9, 30)


def _meta(conn, rssd, name, state="IA", cert=""):
    db.upsert_institution_meta(conn, db.InstitutionMeta(rssd, name, "Town", state, cert, None, PERIOD))


def test_list_matches_come_first_and_name_the_list():
    conn = db.connect(":memory:")
    _meta(conn, "1", "Alpha Bank")
    _meta(conn, "2", "Zeta Bank")
    db.add_to_watch_list(conn, "2", "LTWC", date(2026, 10, 1))

    rows = simple_email.build_rows(conn, [simple_email.Filing("1", PERIOD, False),
                                          simple_email.Filing("2", PERIOD, True)])

    assert [r.name for r in rows] == ["Zeta Bank", "Alpha Bank"]
    assert rows[0].lists == ["Watch List"] and rows[0].kind == "Refiling"
    assert rows[1].lists == [] and rows[1].kind == "New filing"
    assert "1 on our lists" in simple_email.subject(rows, "today")


def test_html_has_both_links_and_the_list_column():
    conn = db.connect(":memory:")
    _meta(conn, "1", "Alpha Bank")
    rows = simple_email.build_rows(conn, [simple_email.Filing("1", PERIOD, False, datetime(2026, 10, 27, 14, 5))])
    out = simple_email.render_html(rows, "today")
    assert "ViewFacsimileDirect.aspx?date=09302026&amp;ds=call&amp;id=1" in out
    assert "UbprReport.aspx?rptCycleIds=154" in out
    assert "On our lists?" in out and ">No<" in out


def test_window_query_reads_stored_submissions():
    conn = db.connect(":memory:")
    db.upsert_snapshot(conn, db.Snapshot("1", PERIOD, datetime(2026, 10, 27, 9), {}, datetime(2026, 10, 27, 12)))
    db.upsert_snapshot(conn, db.Snapshot("2", PERIOD, datetime(2026, 10, 20, 9), {}, datetime(2026, 10, 20, 12)))
    got = simple_email.filings_submitted_between(conn, datetime(2026, 10, 27), datetime(2026, 10, 29))
    assert [f.id_rssd for f in got] == ["1"]
