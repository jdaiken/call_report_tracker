from ffiec_monitor.treasury_desk import DEFAULT_LIST_PATH, TreasuryDeskEntry, TreasuryDeskMatcher, load, normalize


def test_normalize_folds_cosmetic_differences_only():
    assert normalize("F&M Bank & Trust") == normalize("FM Bank and Trust") or normalize("F&M Bank and Trust") == "F AND M BANK AND TRUST"
    assert normalize("Texas Capital Bank, National Association") == "TEXAS CAPITAL BANK NA"
    assert normalize("The Apple Bank Company") == "APPLE BANK"


def test_aliases_and_state_pin():
    m = TreasuryDeskMatcher([TreasuryDeskEntry("WaFd Bank", ("Washington Federal Bank",)),
                             TreasuryDeskEntry("Pinnacle Bank", (), "TN")])
    assert m.match("WASHINGTON FEDERAL BANK") == "WaFd Bank"
    assert m.match("Pinnacle Bank", "TN") == "Pinnacle Bank" and m.match("Pinnacle Bank", "NE") is None
    assert m.match("Pioneer Bank") is None


def test_shipped_list_loads():
    m = load(DEFAULT_LIST_PATH)
    assert m.match("Raymond James Bank, National Association") == "Raymond James Bank"
